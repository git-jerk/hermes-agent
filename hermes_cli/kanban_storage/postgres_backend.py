"""PostgreSQL backend — psycopg3 + connection pool, one schema per board.

This is the durability win that motivated the storage abstraction.
PG handles many concurrent writers from independent processes
correctly: every connection is a proper TCP client, the server
serializes via MVCC + row locks, and the WAL plus checkpoint flow
is server-side. No file-handle racing on shm/wal sidecars, no
``database disk image is malformed`` after a process crash.

Each board lives in its own PostgreSQL schema named ``kanban_<slug>``.
Bare table names in the existing SQL strings continue to work because
we set ``search_path`` at connection time so the unqualified
``tasks`` / ``task_events`` / etc. resolve inside the board's schema.

Connection pooling
------------------

One :class:`psycopg_pool.ConnectionPool` is shared across all boards
that point at the same DSN — that's the common case (one PG instance,
many schemas). Each acquired connection gets its ``search_path`` set
before being handed back to the caller, then reset on release. The
pool is created lazily on first use and reused for the process'
lifetime.

Multi-process safety: each Python process has its own pool; that's
fine because PG handles inter-process concurrency on the server. The
pool just optimizes intra-process connection reuse.

Schema initialization
---------------------

On first open of a given (DSN, schema) pair, we:

1. ``CREATE SCHEMA IF NOT EXISTS kanban_<slug>``
2. Apply ``schema_postgres.sql`` against that schema
3. Cache the (DSN, schema) tuple in :data:`_INITIALIZED_KEYS` so
   subsequent opens skip the DDL.

The initialization is locked per process so two threads racing the
first open of a board don't both try to run DDL. Across processes,
``CREATE SCHEMA IF NOT EXISTS`` and ``CREATE TABLE IF NOT EXISTS``
are themselves idempotent — at worst two backends both run the
no-op DDL.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from pathlib import Path
from typing import Dict, Iterator, Optional

import psycopg
from psycopg import sql as psql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .base import KanbanBackend, POSTGRES_DIALECT, StorageConfig
from .connection import PgConnectionWrapper

_log = logging.getLogger(__name__)

_POOLS: Dict[str, ConnectionPool] = {}
_POOL_LOCK = threading.RLock()
_INITIALIZED_KEYS: set[tuple[str, str]] = set()  # (dsn, schema)
_INIT_LOCK = threading.RLock()

# Strict identifier check before we splice a schema name into SQL.
# board slug validation in kanban_db allows hyphens, but PG identifiers
# don't (without quoting). We auto-derive schema names from slugs by
# replacing hyphens with underscores in :func:`pg_schema_for_board`;
# this validator stays strict so any other input fails loudly.
_RE_PG_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _validate_pg_identifier(name: str) -> str:
    if not _RE_PG_IDENT.match(name):
        raise ValueError(f"invalid PG identifier {name!r}")
    return name


def pg_schema_for_board(slug: str) -> str:
    """Derive the per-board PG schema name from a board slug.

    Hyphens (legal in kanban slugs) are not legal in unquoted PG
    identifiers, so we substitute underscores. ``trading-lab`` →
    ``kanban_trading_lab``. Idempotent — already-safe slugs pass
    through unchanged.
    """
    safe = slug.replace("-", "_")
    return _validate_pg_identifier(f"kanban_{safe}")


def _load_schema_sql() -> str:
    here = Path(__file__).resolve().parent
    return (here / "schema_postgres.sql").read_text()


def _split_statements(script: str) -> list[str]:
    """Split a multi-statement SQL script on top-level ``;``.

    Our schema files use simple statements (no ``$$`` dollar-quoted
    function bodies, no triggers with embedded semicolons), so a flat
    split-by-semicolon outside of single-quoted strings is correct.
    """
    out: list[str] = []
    buf: list[str] = []
    in_squote = False
    in_line_comment = False
    i = 0
    n = len(script)
    while i < n:
        ch = script[i]
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            buf.append(ch)
            i += 1
            continue
        if not in_squote and ch == "-" and i + 1 < n and script[i + 1] == "-":
            in_line_comment = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_squote = not in_squote
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and not in_squote:
            stmt = "".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _get_pool(config: StorageConfig) -> ConnectionPool:
    assert config.backend == "postgres", config
    assert config.postgres_dsn, "postgres_dsn required for postgres backend"
    key = config.postgres_dsn
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = ConnectionPool(
                conninfo=config.postgres_dsn,
                min_size=config.postgres_pool_min,
                max_size=config.postgres_pool_max,
                # Validate connections on checkout so a server restart
                # doesn't poison the pool with dead handles.
                check=ConnectionPool.check_connection,
                kwargs={
                    "autocommit": True,
                    # row_factory at connection scope so cursors
                    # default to dict rows without each call site
                    # asking for them.
                    "row_factory": dict_row,
                },
                # We set search_path per checkout via configure(), so
                # don't pin it in conninfo.
                name=f"kanban-pg[{key.rsplit('@', 1)[-1] if '@' in key else key}]",
                # Be explicit about the open behaviour — psycopg-pool's
                # default is being flipped to False in a future release
                # (DeprecationWarning since 3.3.0).
                open=False,
            )
            pool.open(wait=True, timeout=10)
            _POOLS[key] = pool
        return pool


class PostgresBackend:
    """PostgreSQL implementation of :class:`KanbanBackend`."""

    dialect = POSTGRES_DIALECT

    def open_connection(self, config: StorageConfig) -> PgConnectionWrapper:
        assert config.backend == "postgres", config
        schema = config.postgres_schema or pg_schema_for_board(config.board)
        _validate_pg_identifier(schema)
        pool = _get_pool(config)

        # Initialize the schema once per (dsn, schema) per process.
        key = (config.postgres_dsn or "", schema)
        with _INIT_LOCK:
            if key not in _INITIALIZED_KEYS:
                self._ensure_schema_exists(pool, schema, config)
                _INITIALIZED_KEYS.add(key)

        # Pull a connection from the pool. We DON'T return it to the
        # pool on close — that would defeat the purpose of the wrapper.
        # Instead we mark this as a "checked out" connection and the
        # caller's `conn.close()` puts it back. psycopg-pool's
        # ConnectionPool.getconn() / putconn() lets us do exactly this.
        raw = pool.getconn(timeout=10)
        try:
            with raw.cursor() as cur:
                cur.execute(
                    psql.SQL("SET search_path = {}, public").format(
                        psql.Identifier(schema)
                    )
                )
        except Exception:
            pool.putconn(raw)
            raise

        wrapper = PgConnectionWrapper(raw, schema=schema)

        # Hook close() so calling conn.close() returns the connection
        # to the pool rather than tearing down the socket. We also
        # reset search_path on release so the next checkout for a
        # different schema doesn't inherit stale state. Idempotent:
        # close() clears the hook before invoking it.
        def _return_to_pool() -> None:
            try:
                if not raw.closed:
                    try:
                        with raw.cursor() as reset_cur:
                            reset_cur.execute("RESET search_path")
                    except Exception:
                        # If the connection is in a bad state, the
                        # pool will discard it on putconn anyway.
                        pass
                pool.putconn(raw)
            except Exception:
                # Pool discards bad connections; swallow stray errors
                # so close() never raises on already-closed sockets.
                pass

        wrapper._on_close = _return_to_pool
        return wrapper

    def _ensure_schema_exists(
        self, pool: ConnectionPool, schema: str, config: StorageConfig
    ) -> None:
        """Run CREATE SCHEMA + schema.sql against the target schema.

        Uses a short-lived connection from the pool so we don't tie up
        the caller's connection slot. Idempotent: CREATE … IF NOT EXISTS
        and our schema_postgres.sql is all-IF-NOT-EXISTS.
        """
        ddl_statements = _split_statements(_load_schema_sql())
        with pool.connection() as raw:
            with raw.cursor() as cur:
                cur.execute(
                    psql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        psql.Identifier(schema)
                    )
                )
                cur.execute(
                    psql.SQL("SET search_path = {}, public").format(
                        psql.Identifier(schema)
                    )
                )
                for stmt in ddl_statements:
                    cur.execute(stmt)
                # Stamp version 1.
                cur.execute(
                    "INSERT INTO kanban_schema_version (version, applied_at) "
                    "VALUES (1, extract(epoch from now())::bigint) "
                    "ON CONFLICT (version) DO NOTHING"
                )

    def init_schema(self, conn: PgConnectionWrapper, config: StorageConfig) -> None:
        # No-op: _ensure_schema_exists already ran in open_connection.
        # We expose this method for protocol parity with the sqlite
        # backend. Callers that explicitly want to re-apply DDL (e.g.
        # the migrator) call _ensure_schema_exists directly via the
        # backend instance below.
        pass

    @contextlib.contextmanager
    def write_txn(self, conn: PgConnectionWrapper) -> Iterator[PgConnectionWrapper]:
        """Postgres write transaction.

        Unlike SQLite there's no need for ``BEGIN IMMEDIATE`` —
        PG's MVCC + row locks handle concurrency. We do a plain
        ``BEGIN`` and rely on the existing CAS patterns in the WHERE
        clauses to serialize claim/heartbeat/complete races.
        """
        # The pool's connections are autocommit-mode, so each statement
        # is its own transaction unless we explicitly start one. Start
        # a transaction now; psycopg-pool will return autocommit on
        # release.
        raw = conn.raw
        prev_autocommit = raw.autocommit
        raw.autocommit = False
        try:
            yield conn
        except Exception:
            raw.rollback()
            raise
        finally:
            try:
                if not raw.closed and not raw.autocommit:
                    raw.commit()
            finally:
                raw.autocommit = prev_autocommit


INSTANCE = PostgresBackend()


def shutdown_all_pools() -> None:
    """Close every pool created during this process.

    Intended for tests and orderly daemon shutdown. Live boards should
    just let the process exit; psycopg-pool closes pools at interpreter
    teardown automatically.
    """
    with _POOL_LOCK:
        for pool in list(_POOLS.values()):
            try:
                pool.close()
            except Exception:
                _log.debug("error closing pool", exc_info=True)
        _POOLS.clear()
        _INITIALIZED_KEYS.clear()
