"""Postgres connection wrapper that quacks like :class:`sqlite3.Connection`.

The kanban codebase has ~186 ``conn.execute(sql, params)`` call sites
written for SQLite. Rather than rewrite every one of them to switch on
backend, we ship a thin wrapper around :class:`psycopg.Connection` that
exposes the same surface:

* ``conn.execute(sql, params=())`` returns a cursor-like object whose
  ``fetchone() / fetchall() / fetchmany() / rowcount / lastrowid /
  __iter__`` all work.
* ``conn.executemany(sql, seq_of_params)`` for batched inserts.
* ``conn.commit() / conn.rollback() / conn.close()`` pass through.
* ``conn.row_factory = sqlite3.Row`` is silently accepted as a no-op —
  the wrapper already returns dict-style rows via psycopg3's
  ``dict_row`` row factory, so every existing ``row["col"]`` access
  works unchanged.

Inside ``execute()`` we apply the SQL translations from :mod:`.dialect`
(placeholder rewrite, ``INSERT OR IGNORE``, ``PRAGMA``) and, for the
three autoincrement tables, append ``RETURNING id`` when the original
statement is a bare INSERT — the wrapper captures the id and exposes
it as ``cursor.lastrowid`` so calling code that does
``cur.lastrowid`` keeps working.

Exception aliasing
------------------

The codebase catches ``sqlite3.IntegrityError`` (and a few others) in a
handful of places. To avoid rewriting those except clauses, we
re-export the matching psycopg3 exception classes under
:mod:`sqlite3`-compatible names inside the wrapper module. Modules
that want backend-agnostic exception handling can import from
:mod:`hermes_cli.kanban_storage.exceptions`.

Per-board schema isolation
--------------------------

Each board's tables live in their own PG schema named
``kanban_<slug>``. The wrapper sets ``search_path`` at connection time
so that bare table names in the SQL strings continue to refer to the
right tables without any change to the SQL. PRAGMA table_info
translation also injects the schema name so column-existence checks
look at the right place.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable, Optional, Sequence, Union

import psycopg
from psycopg import sql as psql
from psycopg.rows import dict_row

from .dialect import (
    AUTOINCREMENT_TABLES,
    append_returning_id,
    needs_returning_id,
    translate_sql_for_postgres,
)

_log = logging.getLogger(__name__)


# Some psycopg3 exception classes we re-export as sqlite3-compatible
# aliases. The kanban codebase catches sqlite3.IntegrityError in one
# place (link_tasks duplicate parent_id, child_id) — psycopg3's
# IntegrityError is the matching class.
IntegrityError = psycopg.errors.IntegrityError
OperationalError = psycopg.errors.OperationalError
DatabaseError = psycopg.errors.DatabaseError
ProgrammingError = psycopg.errors.ProgrammingError


class PgCursorWrapper:
    """psycopg3 cursor proxy that mimics :class:`sqlite3.Cursor`.

    The two visible differences we paper over:

    1. :attr:`lastrowid` — sqlite3 exposes it; psycopg3 doesn't. We
       capture it manually after INSERTs into autoincrement tables (we
       inject ``RETURNING id`` if the caller's SQL didn't include one).
    2. Iteration semantics — both support ``for row in cur``; row type
       is a dict from psycopg3's ``dict_row``.

    Other methods (``fetchone``, ``fetchall``, ``fetchmany``,
    ``rowcount``, ``description``, ``close``) come from psycopg3
    directly.
    """

    __slots__ = ("_cursor", "_lastrowid", "_returning_pending")

    def __init__(self, cursor: psycopg.Cursor):
        self._cursor = cursor
        self._lastrowid: Optional[int] = None
        self._returning_pending: bool = False

    # --- attribute pass-through for everything else ---
    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    # We re-implement fetchone() only so that, when we forced a
    # RETURNING id, we transparently consume it before returning the
    # original row shape. Other fetch* methods don't need this because
    # an INSERT … RETURNING never has a useful row to return to the
    # caller — it's always followed by lastrowid access, never by
    # fetchone().
    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def fetchmany(self, size: Optional[int] = None) -> list[Any]:
        if size is None:
            return self._cursor.fetchmany()
        return self._cursor.fetchmany(size)

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> Optional[int]:
        if self._returning_pending:
            try:
                row = self._cursor.fetchone()
            except psycopg.ProgrammingError:
                row = None
            if row is not None:
                # dict_row factory returns {"id": <int>}
                if isinstance(row, dict):
                    self._lastrowid = row.get("id")
                else:
                    # Fall back to first column for safety.
                    self._lastrowid = row[0] if row else None
            self._returning_pending = False
        return self._lastrowid

    @property
    def description(self) -> Any:
        return self._cursor.description

    def __iter__(self):
        return iter(self._cursor)

    def close(self) -> None:
        self._cursor.close()


class PgConnectionWrapper:
    """psycopg3 connection proxy that mimics :class:`sqlite3.Connection`.

    Created by :class:`hermes_cli.kanban_storage.postgres_backend.PostgresBackend`.
    Holds a reference to the underlying :class:`psycopg.Connection` plus
    the per-board schema name (so PRAGMA table_info translation can
    constrain its lookup).

    Lifecycle: callers either close explicitly via ``conn.close()`` or
    use the connection as a context manager. We do NOT auto-commit on
    close (sqlite3 doesn't either). The wrapper is intentionally not a
    pool entry — pooling is the backend's responsibility.
    """

    __slots__ = (
        "_conn",
        "_schema",
        "_returning_pending_cursor",
        "_on_close",
        "row_factory",
        "_hermes_kanban_config",
    )

    def __init__(self, conn: psycopg.Connection, schema: str):
        self._conn = conn
        self._schema = schema
        self._returning_pending_cursor: Optional[PgCursorWrapper] = None
        # row_factory is accepted but ignored; we already use dict_row.
        # Setting this is a no-op so the existing
        # ``conn.row_factory = sqlite3.Row`` assignment in kanban_db.py
        # doesn't raise.
        self.row_factory: Any = None
        # Selector stashes the StorageConfig here so write_txn() can
        # dispatch to the right backend without recomputing.
        self._hermes_kanban_config: Any = None
        # Optional close hook for pool return (set by PostgresBackend
        # immediately after construction). Default is a no-op.
        self._on_close: Optional[Any] = None

    # --- core methods ---
    def execute(
        self,
        sql_str: str,
        params: Optional[Union[Sequence[Any], dict[str, Any]]] = None,
    ) -> PgCursorWrapper:
        """Execute a single statement and return a cursor wrapper.

        The SQL is translated for psycopg3 dialect first (placeholders,
        OR-IGNORE, PRAGMA). For INSERTs into autoincrement tables we
        append ``RETURNING id`` if absent so the caller can read
        ``cursor.lastrowid``.
        """
        translated = translate_sql_for_postgres(
            sql_str, search_path_schema=self._schema
        )

        # Detect INSERT-into-autoincrement; if so and the SQL doesn't
        # already include RETURNING, append it. We use the
        # post-translation SQL so we catch the case where a sqlite-style
        # INSERT OR IGNORE was rewritten and now lacks RETURNING.
        capture_returning = False
        if needs_returning_id(translated):
            translated = append_returning_id(translated)
            capture_returning = True

        cur = self._conn.cursor(row_factory=dict_row)
        try:
            if params is None:
                cur.execute(translated)
            else:
                cur.execute(translated, params)
        except Exception:
            cur.close()
            raise

        wrapper = PgCursorWrapper(cur)
        if capture_returning:
            wrapper._returning_pending = True
        return wrapper

    def executemany(
        self,
        sql_str: str,
        seq_of_params: Iterable[Sequence[Any]],
    ) -> PgCursorWrapper:
        translated = translate_sql_for_postgres(
            sql_str, search_path_schema=self._schema
        )
        cur = self._conn.cursor(row_factory=dict_row)
        try:
            cur.executemany(translated, list(seq_of_params))
        except Exception:
            cur.close()
            raise
        return PgCursorWrapper(cur)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        # If a close hook is set (the pool-return path), let it own the
        # connection lifecycle. Otherwise close the raw connection
        # directly. The hook is responsible for invoking the real close
        # if needed.
        hook = self._on_close
        if hook is not None:
            try:
                hook()
            finally:
                # Avoid reusing the hook; subsequent close() calls
                # should be no-ops.
                self._on_close = None
            return
        if not self._conn.closed:
            self._conn.close()

    @property
    def closed(self) -> bool:
        return self._conn.closed

    # Allow ``with conn:`` to behave like sqlite3 (commit on success,
    # rollback on exception). psycopg3's default is similar but the
    # exit-with-no-error path doesn't always commit; we keep sqlite3
    # semantics for parity with the existing code.
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            try:
                self.rollback()
            except Exception:
                pass
        return False  # never swallow

    # The underlying psycopg3 connection is exposed for the rare caller
    # that needs server-side cursors or COPY support (e.g. the data
    # migrator). Most code never touches this.
    @property
    def raw(self) -> psycopg.Connection:
        return self._conn
