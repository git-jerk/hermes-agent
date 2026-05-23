"""Types and protocols for the pluggable kanban storage layer.

The two concrete backends live in :mod:`.sqlite_backend` and
:mod:`.postgres_backend`. Both implement :class:`KanbanBackend` and
both return connection objects that quack like :class:`sqlite3.Connection`
(i.e. ``conn.execute(sql, params)`` returns a cursor with ``.fetchone()``,
``.fetchall()``, ``.rowcount``, ``.lastrowid``; rows expose dict-style
access via ``row["col"]``). The Postgres path achieves this duck-typing
via the wrapper in :mod:`.connection`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ContextManager, Literal, Optional, Protocol, runtime_checkable


BackendName = Literal["sqlite", "postgres"]


@dataclass(frozen=True)
class Dialect:
    """Static facts about a backend's SQL dialect.

    Used by :mod:`kanban_db` for the rare cases where we still need to
    branch on backend (e.g. ``cur.lastrowid`` vs ``RETURNING``, or
    ``PRAGMA`` introspection vs ``information_schema``). Most of the
    branching is hidden inside the connection wrapper instead.
    """

    name: BackendName
    placeholder: str  # "?" for sqlite, "%s" for psycopg
    supports_pragma: bool
    supports_returning: bool  # both do, but sqlite added it later (3.35+)
    json_column_sql_type: str  # "TEXT" or "JSONB"


SQLITE_DIALECT = Dialect(
    name="sqlite",
    placeholder="?",
    supports_pragma=True,
    supports_returning=True,
    json_column_sql_type="TEXT",
)

POSTGRES_DIALECT = Dialect(
    name="postgres",
    placeholder="%s",
    supports_pragma=False,
    supports_returning=True,
    json_column_sql_type="JSONB",
)


@dataclass(frozen=True)
class StorageConfig:
    """Resolved storage settings for a single board.

    Derived from (in order): the board's ``board.json`` ``kanban.storage``
    block, environment overrides, and built-in defaults. Resolution lives
    in :mod:`.selector`; this dataclass is the result.
    """

    backend: BackendName
    board: str
    # sqlite-only
    sqlite_path: Optional[str] = None
    # postgres-only
    postgres_dsn: Optional[str] = None
    postgres_schema: Optional[str] = None  # kanban_<slug> if not set
    postgres_pool_min: int = 2
    postgres_pool_max: int = 10

    def display(self) -> str:
        """Human-readable one-liner for logs (no credentials)."""
        if self.backend == "sqlite":
            return f"sqlite[{self.board}] @ {self.sqlite_path}"
        # Strip password from DSN for logging — psycopg3's conninfo
        # parser exists, but a substring check is enough for our DSN
        # shapes.
        dsn = self.postgres_dsn or ""
        if "@" in dsn:
            dsn = dsn.split("@", 1)[1]
        return f"postgres[{self.board}] schema={self.postgres_schema} @ {dsn}"


@runtime_checkable
class KanbanBackend(Protocol):
    """Common interface that both sqlite and postgres backends implement.

    Implementations should be process-safe: a single backend instance
    may be shared by the gateway, the dispatcher loop, workers, and
    CLI commands inside the same process. Cross-process safety comes
    from the underlying database (sqlite WAL or postgres MVCC + locks).
    """

    dialect: Dialect

    def open_connection(self, config: StorageConfig) -> Any:
        """Open a new connection for the given board config.

        Returns a connection object that quacks like
        :class:`sqlite3.Connection` — see module docstring. Caller is
        responsible for closing it (or using it as a context manager).
        """
        ...

    def init_schema(self, conn: Any, config: StorageConfig) -> None:
        """Ensure the schema exists for this board.

        Must be idempotent: running it twice in a row, or running it
        concurrently from two processes, must converge on the same
        state and not corrupt data.
        """
        ...

    def write_txn(self, conn: Any) -> ContextManager[Any]:
        """Open an exclusive write transaction.

        On sqlite this is ``BEGIN IMMEDIATE``. On postgres it's a
        default transaction (which is already MVCC-serialized for the
        rows touched by the WHERE clauses in our CAS patterns). The
        ``__enter__``/``__exit__`` semantics match the original
        ``kanban_db.write_txn``: commit on clean exit, rollback on
        exception, never swallow.
        """
        ...
