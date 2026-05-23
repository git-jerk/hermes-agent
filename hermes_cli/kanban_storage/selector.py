"""Backend selector — resolves a :class:`StorageConfig` per board.

This module is the single entry point that ``kanban_db`` calls into.
It owns the resolution policy (board.json → env → defaults) and the
caching of :class:`StorageConfig` so we don't re-read board.json on
every connection.

Public entry points
-------------------

* :func:`resolve_storage_config` — pure resolution, returns the
  config that would be used for ``board`` without actually opening
  a connection. Used by ``hermes kanban storage status``.
* :func:`open_connection` — opens a new connection through the
  resolved backend.
* :func:`init_schema` — applies idempotent schema creation. For PG
  this is implicit on first connection, but tests and the migrator
  call it explicitly.
* :func:`write_txn` — backend-dispatched write transaction.
* :func:`clear_caches` — resets the per-board config cache. Used by
  tests that mutate board.json between cases.

Resolution order (first match wins)
-----------------------------------

1. Explicit ``backend=`` argument to ``open_connection`` (tests).
2. ``HERMES_KANBAN_BACKEND`` env var (``sqlite`` | ``postgres``).
3. ``board.json`` → ``kanban.storage.backend``.
4. Default: ``sqlite``.

For Postgres, the DSN comes from (in order):

1. Explicit ``postgres_dsn=`` argument.
2. ``HERMES_KANBAN_POSTGRES_DSN`` env var.
3. ``board.json`` → ``kanban.storage.postgres_dsn``.

For SQLite, the path comes from ``kanban_db.kanban_db_path(board=...)``
unchanged. We import it lazily to avoid a circular import.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, ContextManager, Optional

from .base import (
    BackendName,
    KanbanBackend,
    StorageConfig,
)

_log = logging.getLogger(__name__)


def _resolve_board(board: Optional[str]) -> str:
    """Resolve the active board if not given explicitly.

    Defers entirely to :func:`kanban_db.get_current_board` so that the
    full precedence (``HERMES_KANBAN_BOARD`` env → ``<root>/kanban/current``
    file → ``DEFAULT_BOARD``) and the *existence* fallback (stale env
    pointing at a removed board falls through to the next layer) stay
    in one place. Replicating only the env-check here was a regression
    that recreated removed boards (#27021 in spirit, caught by
    ``test_connect_stale_env_uses_fallback_board_without_recreating_it``).
    """
    if board:
        return board
    from hermes_cli.kanban_db import get_current_board

    return get_current_board()


def _read_board_storage_config(board: str) -> dict[str, Any]:
    """Pull the ``kanban.storage`` block out of board.json (or {} if absent).

    Safe on missing/malformed board.json — returns empty dict and lets
    the caller fall through to env/defaults.
    """
    try:
        from hermes_cli.kanban_db import read_board_metadata
    except ImportError:
        return {}
    try:
        meta = read_board_metadata(board=board)
    except Exception as exc:  # pragma: no cover — defensive
        _log.debug("could not read board.json for %s: %s", board, exc)
        return {}
    kanban_block = meta.get("kanban") or {}
    storage = kanban_block.get("storage") or {}
    return storage if isinstance(storage, dict) else {}


def resolve_storage_config(
    board: Optional[str] = None,
    *,
    backend: Optional[BackendName] = None,
    postgres_dsn: Optional[str] = None,
) -> StorageConfig:
    """Resolve the storage config for ``board``.

    Re-resolves on every call. We deliberately do NOT cache here:
    board.json is ~1 KB on disk and the resolver runs once per
    ``connect()``, which happens at most a handful of times per second
    even in the gateway's busiest tick. Caching would speed this up
    by sub-millisecond at the cost of stale state when tests
    monkeypatch HERMES_HOME mid-run; the trade-off isn't worth it.
    """
    resolved_board = _resolve_board(board)
    storage = _read_board_storage_config(resolved_board)

    # Resolve backend name.
    if backend is None:
        backend_env = os.environ.get("HERMES_KANBAN_BACKEND", "").strip().lower()
        if backend_env in {"sqlite", "postgres"}:
            backend = backend_env  # type: ignore[assignment]
        else:
            cfg_backend = (storage.get("backend") or "").strip().lower()
            if cfg_backend in {"sqlite", "postgres"}:
                backend = cfg_backend  # type: ignore[assignment]
            else:
                backend = "sqlite"

    if backend == "sqlite":
        from hermes_cli.kanban_db import kanban_db_path

        path = kanban_db_path(board=resolved_board)
        config = StorageConfig(
            backend="sqlite",
            board=resolved_board,
            sqlite_path=str(path),
        )
    elif backend == "postgres":
        # DSN resolution: explicit arg → env var named in board.json's
        # ``postgres_dsn_env`` → ``HERMES_KANBAN_POSTGRES_DSN`` → literal
        # ``postgres_dsn`` in board.json (least secure; keeps credentials
        # on disk).
        env_name = (storage.get("postgres_dsn_env") or "").strip()
        env_dsn = os.environ.get(env_name, "").strip() if env_name else ""
        if not env_dsn:
            env_dsn = os.environ.get("HERMES_KANBAN_POSTGRES_DSN", "").strip()
        dsn = (
            postgres_dsn
            or env_dsn
            or (storage.get("postgres_dsn") or "").strip()
            or None
        )
        if not dsn:
            raise RuntimeError(
                f"backend=postgres but no DSN configured for board {resolved_board!r}; "
                "set HERMES_KANBAN_POSTGRES_DSN or board.json kanban.storage.postgres_dsn"
            )
        schema_name = (storage.get("postgres_schema") or "").strip()
        if not schema_name:
            from .postgres_backend import pg_schema_for_board
            schema_name = pg_schema_for_board(resolved_board)
        pool_min = int(storage.get("postgres_pool_min") or 2)
        pool_max = int(storage.get("postgres_pool_max") or 10)
        config = StorageConfig(
            backend="postgres",
            board=resolved_board,
            postgres_dsn=dsn,
            postgres_schema=schema_name,
            postgres_pool_min=pool_min,
            postgres_pool_max=pool_max,
        )
    else:  # pragma: no cover — exhaustive above
        raise ValueError(f"unknown backend {backend!r}")

    return config


def get_backend(config: StorageConfig) -> KanbanBackend:
    """Return the backend implementation matching ``config.backend``."""
    if config.backend == "sqlite":
        from . import sqlite_backend

        return sqlite_backend.INSTANCE
    if config.backend == "postgres":
        from . import postgres_backend

        return postgres_backend.INSTANCE
    raise ValueError(f"unknown backend {config.backend!r}")


def open_connection(
    board: Optional[str] = None,
    *,
    db_path: Optional[Path] = None,
    backend: Optional[BackendName] = None,
    postgres_dsn: Optional[str] = None,
) -> Any:
    """Open a connection for the resolved board / backend.

    Back-compat behaviour:

    * ``db_path=`` always wins (legacy callers, tests). When passed,
      we force backend=sqlite and skip board.json resolution.
    * ``board=`` resolves through the normal cascade.
    """
    if db_path is not None:
        # Legacy path: caller pinned the SQLite file directly. Skip the
        # board.json read entirely so tests with synthetic db paths work
        # without inventing a board.
        from hermes_cli.kanban_db import _normalize_board_slug

        # Use the file's parent dir name as a synthetic board slug for
        # logging — it doesn't affect resolution since we already have
        # the path.
        slug = _normalize_board_slug(Path(db_path).parent.name) or "default"
        config = StorageConfig(
            backend="sqlite",
            board=slug,
            sqlite_path=str(db_path),
        )
    else:
        config = resolve_storage_config(
            board, backend=backend, postgres_dsn=postgres_dsn
        )
    backend_impl = get_backend(config)
    conn = backend_impl.open_connection(config)
    # Stash the config on the connection so write_txn / init_schema can
    # dispatch back without recomputing. We use a name unlikely to
    # collide with sqlite3.Connection attributes.
    try:
        conn._hermes_kanban_config = config  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        # sqlite3.Connection refuses arbitrary attribute assignment on
        # some Python builds; fall back to a side-table.
        _CONN_CONFIG[id(conn)] = config
    return conn


_CONN_CONFIG: dict[int, StorageConfig] = {}


def _config_for_conn(conn: Any) -> Optional[StorageConfig]:
    cached = getattr(conn, "_hermes_kanban_config", None)
    if cached is not None:
        return cached
    return _CONN_CONFIG.get(id(conn))


def init_schema(conn: Any, *, board: Optional[str] = None) -> None:
    """Apply schema DDL via the right backend."""
    config = _config_for_conn(conn)
    if config is None:
        config = resolve_storage_config(board)
    get_backend(config).init_schema(conn, config)


def write_txn(conn: Any) -> ContextManager[Any]:
    """Open a backend-appropriate write transaction."""
    config = _config_for_conn(conn)
    if config is None:
        # Legacy callers passing a raw sqlite3.Connection without a
        # config tag get the sqlite semantics — that's the original
        # behaviour. We fall through to the sqlite backend directly.
        from . import sqlite_backend

        return sqlite_backend.INSTANCE.write_txn(conn)
    return get_backend(config).write_txn(conn)


def clear_caches() -> None:
    """Reset the backend-side init caches.

    Tests and the migrator call this so a fresh open re-applies schema
    initialization. The selector itself no longer caches (see the
    note in :func:`resolve_storage_config`); only the SQLite path
    cache and the PG (dsn, schema) cache remain.
    """
    _CONN_CONFIG.clear()
    from . import sqlite_backend

    with sqlite_backend._INIT_LOCK:
        sqlite_backend._INITIALIZED_PATHS.clear()
    try:
        from . import postgres_backend

        with postgres_backend._INIT_LOCK:
            postgres_backend._INITIALIZED_KEYS.clear()
    except ImportError:
        # psycopg may not be installed in minimal environments.
        pass
