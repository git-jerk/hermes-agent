"""Dialect translation helpers for SQLite ↔ PostgreSQL.

The kanban codebase carries ~186 inlined SQL strings written in SQLite
dialect. To avoid touching all of them when we route a connection at a
PostgreSQL backend, we translate the dialect surface at one place — when
SQL is handed to a :class:`psycopg.Connection`. The translations covered
here are exactly the ones present in ``kanban_db.py``; we deliberately
keep this conservative so changes elsewhere can't accidentally rely on
syntax that doesn't survive the round-trip.

Translations
------------

* ``?`` parameter placeholders → ``%s`` (psycopg3 default style).
  We skip ``?`` characters inside single-quoted string literals so
  payload text isn't corrupted. The codebase doesn't use double-quoted
  identifiers containing ``?``, so we don't need to track those.

* ``INSERT OR IGNORE INTO <table> ...`` →
  ``INSERT INTO <table> ... ON CONFLICT DO NOTHING``. The codebase only
  uses this form on tables with a clearly-defined PK or unique index,
  so a bare ``DO NOTHING`` (without a conflict target) is correct
  semantically and PostgreSQL infers the conflict target from the
  unique constraint hit.

* ``INSERT OR REPLACE INTO <table> ... VALUES ...`` →
  ``INSERT INTO <table> ... VALUES ... ON CONFLICT ... DO UPDATE SET
  ... = EXCLUDED. ...``. Currently unused in ``kanban_db.py`` but
  detected for safety.

* ``PRAGMA synchronous=...`` and ``PRAGMA foreign_keys=...`` → no-op
  (replaced with ``SELECT 1`` so :meth:`Connection.execute` still
  returns a usable cursor). Both behaviors are PG defaults: durability
  is governed by ``synchronous_commit`` and ``fsync`` at the server
  level, and FK enforcement is implicit once constraints are declared.

* ``PRAGMA table_info(<table>)`` → query against
  ``information_schema.columns`` returning rows with a ``name`` field,
  matching the shape ``kanban_db._migrate_add_optional_columns()``
  expects.

INSERT…RETURNING
----------------

SQLite exposes the last auto-generated PK via :attr:`cursor.lastrowid`.
psycopg3 does not. For the three tables where ``kanban_db.py`` reads
``lastrowid`` (``task_runs``, ``task_comments``, ``task_events``) we
append ``RETURNING id`` to bare ``INSERT INTO <table>`` statements when
the caller is the Postgres path. The wrapper then exposes the returned
id as ``cursor.lastrowid`` so caller code doesn't change.

Edge cases
----------

The transformation regexes here are intentionally simple — they don't
try to parse arbitrary SQL. They work because the kanban codebase
writes idiomatic, mostly-literal SQL with predictable shapes. If a
future query breaks one of these assumptions, the failure is loud
(syntax error at the server) rather than silent.
"""

from __future__ import annotations

import re
from typing import Optional

# --- placeholder translation ------------------------------------------------

# Match a literal '?' that is NOT inside a single-quoted string. We
# scan left-to-right tracking quote state.
def translate_placeholders(sql: str) -> str:
    """Convert SQLite-style ``?`` placeholders to psycopg-style ``%s``.

    Single-quoted string literals are preserved verbatim — a ``?`` inside
    ``'why?'`` stays a ``?``. SQL identifiers are double-quoted in
    PostgreSQL and don't contain ``?`` in this codebase, so we don't
    track them.
    """
    if "?" not in sql:
        return sql
    out = []
    i = 0
    n = len(sql)
    in_squote = False
    while i < n:
        ch = sql[i]
        if ch == "'":
            # Toggle quote state. Postgres-style escaped quote ('') is
            # two consecutive quotes, which our toggle handles correctly
            # (toggle off, toggle back on) without special casing.
            in_squote = not in_squote
            out.append(ch)
            i += 1
            continue
        if ch == "?" and not in_squote:
            # psycopg3 needs %s. We must also escape any pre-existing
            # '%' so they aren't interpreted as placeholder prefixes.
            # But the codebase doesn't contain raw '%' in SQL bodies —
            # if it ever does, the failure will surface as a psycopg
            # ProgrammingError, which is the loud-failure mode we want.
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# --- conflict-style upsert translation -------------------------------------

# Capture group 1: the table name (and any column list) immediately after
# ``INSERT OR IGNORE INTO``. Capture group 2 is whatever follows (VALUES, SELECT, …).
_RE_INSERT_OR_IGNORE = re.compile(
    r"\bINSERT\s+OR\s+IGNORE\s+INTO\s+",
    re.IGNORECASE,
)

_RE_INSERT_OR_REPLACE = re.compile(
    r"\bINSERT\s+OR\s+REPLACE\s+INTO\s+",
    re.IGNORECASE,
)

# Detect a trailing semicolon (optionally with whitespace) so we can splice
# ``ON CONFLICT DO NOTHING`` in front of it.
_RE_TRAILING_SEMI = re.compile(r"\s*;\s*$")


def translate_or_ignore(sql: str) -> str:
    """Rewrite ``INSERT OR IGNORE`` to ``INSERT … ON CONFLICT DO NOTHING``.

    The ``OR IGNORE`` clause has no analogue in PostgreSQL but
    ``ON CONFLICT DO NOTHING`` is semantically equivalent: silently
    skip rows that would violate a unique constraint. We don't specify
    a conflict target — PG infers it from any matching unique index,
    which is the same behavior the codebase relies on for ``task_links``
    (PK ``(parent_id, child_id)``) and ``kanban_notify_subs``
    (PK ``(task_id, platform, chat_id, thread_id)``).
    """
    if not _RE_INSERT_OR_IGNORE.search(sql):
        return sql
    rewritten = _RE_INSERT_OR_IGNORE.sub("INSERT INTO ", sql)
    # Splice ON CONFLICT DO NOTHING before any trailing semicolon.
    trailing = _RE_TRAILING_SEMI.search(rewritten)
    if trailing:
        return rewritten[: trailing.start()] + " ON CONFLICT DO NOTHING" + rewritten[trailing.start():]
    return rewritten + " ON CONFLICT DO NOTHING"


def translate_or_replace(sql: str) -> str:
    """Rewrite ``INSERT OR REPLACE`` to ``INSERT … ON CONFLICT … DO UPDATE``.

    Currently unused in ``kanban_db.py``; included so a future query
    that uses this syntax doesn't silently misbehave. The naive
    ``REPLACE`` semantics in SQLite delete-then-insert, but
    ``ON CONFLICT DO UPDATE`` is the conventional PG equivalent for
    upsert patterns. If the codebase ever depends on the delete side
    (e.g. cascading FK cleanup), this translation must be revisited.
    """
    if not _RE_INSERT_OR_REPLACE.search(sql):
        return sql
    # We don't know the column list here without parsing, so we leave
    # this stub raising — the safer behavior is to fail loudly than to
    # silently corrupt data. When/if INSERT OR REPLACE is added to
    # kanban_db.py, we'll wire it through with the explicit
    # ON CONFLICT DO UPDATE form.
    raise NotImplementedError(
        "INSERT OR REPLACE → ON CONFLICT DO UPDATE translation requires "
        "knowledge of the conflict target columns; rewrite the source "
        "query to use explicit ON CONFLICT DO UPDATE SET ... = EXCLUDED.* "
        "instead."
    )


# --- PRAGMA translation -----------------------------------------------------

_RE_PRAGMA_TABLE_INFO = re.compile(
    r"\bPRAGMA\s+table_info\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)\s*;?\s*$",
    re.IGNORECASE,
)

_RE_PRAGMA_NOOP = re.compile(
    r"\bPRAGMA\s+(synchronous|foreign_keys|journal_mode|busy_timeout|temp_store)"
    r"\s*=\s*[A-Za-z0-9_]+\s*;?\s*$",
    re.IGNORECASE,
)

_RE_PRAGMA_QUERY_JOURNAL = re.compile(
    r"\bPRAGMA\s+journal_mode\s*;?\s*$",
    re.IGNORECASE,
)


def translate_pragma(sql: str, *, search_path_schema: Optional[str] = None) -> Optional[str]:
    """Translate a SQLite ``PRAGMA`` to its PostgreSQL equivalent.

    Returns the rewritten SQL, or ``None`` if the statement isn't a
    PRAGMA the wrapper handles (in which case the caller should pass
    the original SQL through unchanged).

    Supported translations:

    * ``PRAGMA table_info(<table>)`` → a SELECT over
      ``information_schema.columns`` that yields rows with a ``name``
      column, matching the shape ``_migrate_add_optional_columns()``
      expects. The schema is constrained to ``search_path_schema`` (the
      per-board PG schema) so we don't pick up columns from another
      board with the same table name.
    * ``PRAGMA synchronous=...`` / ``foreign_keys=...`` / etc. → ``SELECT 1``
      (no-op that still returns a cursor).
    * ``PRAGMA journal_mode`` (query form) → returns ``'wal'`` as a
      single-row result; the kanban code uses this only to verify WAL
      is on, and the equivalent guarantee on PG is provided by the
      server's WAL-by-default config.
    """
    m = _RE_PRAGMA_TABLE_INFO.search(sql)
    if m:
        table = m.group(1).lower()
        schema = search_path_schema or "current_schema()"
        if search_path_schema:
            schema_lit = f"'{search_path_schema}'"
        else:
            schema_lit = "current_schema()"
        return (
            "SELECT column_name AS name FROM information_schema.columns "
            f"WHERE table_schema = {schema_lit} AND table_name = '{table}' "
            "ORDER BY ordinal_position"
        )
    if _RE_PRAGMA_NOOP.search(sql):
        return "SELECT 1"
    if _RE_PRAGMA_QUERY_JOURNAL.search(sql):
        return "SELECT 'wal' AS journal_mode"
    return None


# --- sqlite_master translation ---------------------------------------------

# kanban_db.py probes sqlite_master twice (lines 1274, 1293) to check whether
# the kanban_notify_subs and task_runs tables exist before running their
# respective additive migrations. PostgreSQL has no sqlite_master; the
# equivalent is querying information_schema.tables. We catch the exact shape
# the codebase uses and rewrite it.

_RE_SQLITE_MASTER_TABLE_CHECK = re.compile(
    r"SELECT\s+name\s+FROM\s+sqlite_master\s+"
    r"WHERE\s+type\s*=\s*'table'\s+AND\s+name\s*=\s*'([^']+)'\s*;?\s*$",
    re.IGNORECASE,
)


def translate_sqlite_master(
    sql: str, *, search_path_schema: Optional[str] = None
) -> Optional[str]:
    """If ``sql`` is the table-existence probe pattern, rewrite for PG.

    Returns the rewritten SQL, or ``None`` if it doesn't match — same
    convention as :func:`translate_pragma`.
    """
    m = _RE_SQLITE_MASTER_TABLE_CHECK.search(sql)
    if not m:
        return None
    table = m.group(1).lower()
    if search_path_schema:
        schema_lit = f"'{search_path_schema}'"
    else:
        schema_lit = "current_schema()"
    return (
        f"SELECT table_name AS name FROM information_schema.tables "
        f"WHERE table_schema = {schema_lit} AND table_name = '{table}'"
    )


# --- INSERT … RETURNING ----------------------------------------------------

# Tables whose primary key is an auto-increment ID column called ``id``.
# When a caller does a bare ``INSERT INTO <table> (...) VALUES (...)`` on
# one of these and then reads ``cursor.lastrowid``, we must append
# ``RETURNING id`` so psycopg3 captures the value. See the wrapper in
# :mod:`.connection`.
AUTOINCREMENT_TABLES: frozenset[str] = frozenset(
    {"task_runs", "task_comments", "task_events"}
)

_RE_INSERT_TARGET = re.compile(
    r"\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)

_RE_HAS_RETURNING = re.compile(r"\bRETURNING\b", re.IGNORECASE)


def needs_returning_id(sql: str) -> bool:
    """True if this SQL is an INSERT into an autoincrement table that
    lacks an explicit RETURNING clause. The wrapper appends
    ``RETURNING id`` so cursor.lastrowid works on the Postgres path.
    """
    m = _RE_INSERT_TARGET.search(sql)
    if not m:
        return False
    if m.group(1).lower() not in AUTOINCREMENT_TABLES:
        return False
    return not _RE_HAS_RETURNING.search(sql)


def append_returning_id(sql: str) -> str:
    """Append ``RETURNING id`` before any trailing semicolon."""
    trailing = _RE_TRAILING_SEMI.search(sql)
    if trailing:
        return sql[: trailing.start()] + " RETURNING id" + sql[trailing.start():]
    return sql + " RETURNING id"


# --- composite translator ---------------------------------------------------

def translate_sql_for_postgres(
    sql: str,
    *,
    search_path_schema: Optional[str] = None,
) -> str:
    """Apply the full sqlite-→-postgres SQL translation pipeline.

    Order matters: PRAGMA replacement first (some PRAGMAs become
    ``SELECT 1`` and should not have placeholder translation applied
    after that), then OR-IGNORE/OR-REPLACE rewrites, then placeholder
    translation last. INSERT…RETURNING handling is done separately by
    the connection wrapper because it depends on whether the caller
    will read ``lastrowid`` (we can't tell at translation time).
    """
    pragma = translate_pragma(sql, search_path_schema=search_path_schema)
    if pragma is not None:
        return pragma
    sqm = translate_sqlite_master(sql, search_path_schema=search_path_schema)
    if sqm is not None:
        return sqm
    sql = translate_or_ignore(sql)
    # OR REPLACE is intentionally not run by default — see translate_or_replace().
    sql = translate_placeholders(sql)
    return sql
