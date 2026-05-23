"""Backend-pluggable storage layer for the Hermes Kanban board.

Why this package exists
-----------------------
The original ``kanban_db.py`` opened SQLite directly from every gateway,
dispatcher, worker, CLI command, and cron watchdog. Under the OpenClaw
24/7 workload this produced :class:`sqlite3.DatabaseError` ("database
disk image is malformed") and ``disk I/O error`` on several boards
(``trading-lab``, ``research``, ``qraav``). The root cause is APFS plus
WAL/shm sidecars racing across many independent process lifecycles.
Single-host SQLite is not the right source-of-truth for this workload;
see ``docs/kanban-storage-postgres-migration.md`` for the full write-up.

This package introduces a thin backend abstraction so that boards can
opt in to PostgreSQL while existing installs keep working unchanged on
SQLite. There is one resolver entry point::

    from hermes_cli.kanban_storage import open_connection, init_schema

    conn = open_connection(board="trading-lab")  # picks the backend
    init_schema(conn, board="trading-lab")

The connection object that comes back quacks like :class:`sqlite3.Connection`
for both backends — the ~80 public functions in :mod:`kanban_db` keep their
signatures unchanged. The dialect translation (``?`` → ``%s``,
``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``, ``PRAGMA table_info`` →
``information_schema.columns``) lives in :mod:`.dialect` and is applied by
the Postgres connection wrapper transparently.

Backend selection (highest precedence first):

1. ``board.json`` → ``kanban.storage.backend`` (``sqlite`` | ``postgres``)
2. ``HERMES_KANBAN_BACKEND`` env var
3. Default: ``sqlite`` (zero-config back-compat)

When the resolved backend is ``postgres``, the DSN comes from
``HERMES_KANBAN_POSTGRES_DSN`` (preferred) or
``board.json`` → ``kanban.storage.postgres_dsn``. Each board lives in its
own PG schema (``kanban_<slug>``) so multi-board installs stay isolated
just like the per-file SQLite layout.
"""

from __future__ import annotations

from .base import BackendName, Dialect, KanbanBackend, StorageConfig
from .selector import (
    open_connection,
    init_schema,
    write_txn,
    get_backend,
    resolve_storage_config,
    clear_caches,
)

__all__ = [
    "BackendName",
    "Dialect",
    "KanbanBackend",
    "StorageConfig",
    "open_connection",
    "init_schema",
    "write_txn",
    "get_backend",
    "resolve_storage_config",
    "clear_caches",
]
