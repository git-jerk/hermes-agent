"""SQLite backend — extracts the original kanban_db.py storage path.

Behavior is bit-equivalent to what ``kanban_db.connect()`` /
``init_db()`` / ``write_txn()`` did before this package existed. The
extraction is purely structural: same PRAGMA setup, same WAL fallback,
same header validation, same per-path initialization cache. Anything
that used to work against the SQLite path continues to work exactly
the same once the selector routes back here.

Why extract at all if behavior is identical? Two reasons:

1. The selector can swap in the Postgres backend without ``kanban_db``
   having to import psycopg or know about pools — it just gets a
   connection object that quacks right.
2. Future SQLite-only changes (e.g. forcing ``mmap_size``, tuning
   ``cache_spill``) land in one place rather than threading through
   the 6,800-line module.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from pathlib import Path
from typing import ContextManager, Optional

from .base import KanbanBackend, SQLITE_DIALECT, StorageConfig

_log = logging.getLogger(__name__)

_SQLITE_HEADER = b"SQLite format 3\x00"

# Per-path init cache — same shape as the original module-level cache in
# kanban_db.py. Module-private because the backend instance is a
# singleton inside the selector.
_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.RLock()


def _looks_like_tls_record_at(data: bytes, offset: int) -> bool:
    """Return True for a TLS record header at ``data[offset:]``.

    Lifted verbatim from kanban_db.py. Used by header validation to
    fingerprint NFS/FUSE corruption that overwrites page 0 with what
    looks like a TLS record.
    """
    if len(data) < offset + 5:
        return False
    content_type = data[offset]
    major = data[offset + 1]
    minor = data[offset + 2]
    length = int.from_bytes(data[offset + 3 : offset + 5], "big")
    return (
        content_type in {0x14, 0x15, 0x16, 0x17}
        and major == 0x03
        and minor in {0x00, 0x01, 0x02, 0x03, 0x04}
        and 0 < length <= 18432
    )


def _validate_sqlite_header(path: Path) -> None:
    """Fail early with an actionable error for non-SQLite Kanban DB files."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.st_size == 0:
        return
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
    except OSError:
        return
    if head.startswith(_SQLITE_HEADER):
        return
    signature = ""
    if head.startswith(b"SQLit") and _looks_like_tls_record_at(head, 5):
        signature = " (TLS record header detected at byte offset 5)"
    elif _looks_like_tls_record_at(head, 0):
        signature = " (TLS record header detected at byte offset 0)"
    raise sqlite3.DatabaseError(
        "file is not a database: invalid SQLite header for "
        f"{path}{signature}; first_32={head[:32].hex(' ')}"
    )


def _load_schema_sql() -> str:
    """Read the SQLite-dialect schema bundled with this package."""
    here = Path(__file__).resolve().parent
    return (here / "schema_sqlite.sql").read_text()


class SqliteBackend:
    """SQLite implementation of :class:`KanbanBackend`."""

    dialect = SQLITE_DIALECT

    def open_connection(self, config: StorageConfig) -> sqlite3.Connection:
        assert config.backend == "sqlite", config
        assert config.sqlite_path is not None, "sqlite_path required for sqlite backend"
        path = Path(config.sqlite_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _validate_sqlite_header(path)
        resolved = str(path.resolve())
        conn = sqlite3.connect(str(path), isolation_level=None, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            with _INIT_LOCK:
                # WAL activation may need an exclusive lock for sidecar
                # creation; serialize with init so two concurrent gateway
                # connections can't race.
                try:
                    from hermes_state import apply_wal_with_fallback

                    apply_wal_with_fallback(
                        conn, db_label=f"kanban.db ({path.name})"
                    )
                except ImportError:
                    # Fallback if hermes_state isn't available (e.g.
                    # standalone tests). Plain WAL is fine for local FS.
                    conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                if resolved not in _INITIALIZED_PATHS:
                    self.init_schema(conn, config)
                    _INITIALIZED_PATHS.add(resolved)
        except Exception:
            conn.close()
            raise
        return conn

    def init_schema(
        self, conn: sqlite3.Connection, config: StorageConfig
    ) -> None:
        """Apply CREATE TABLE / INDEX statements, then run additive migrations.

        Idempotent. The additive migration (``ALTER TABLE ... ADD COLUMN``)
        for legacy DBs lives in :mod:`kanban_db` and is called by the
        selector after this. We do not duplicate it here because the
        additive pass also references kanban_db internals (write_txn,
        sticky-block backfill) that are out of scope for a storage
        backend module.
        """
        schema_sql = _load_schema_sql()
        conn.executescript(schema_sql)
        # Record initial schema version. The additive migration in
        # kanban_db._migrate_add_optional_columns is the bridge from
        # version 0 (pre-this-refactor) to version 1, but for a fresh
        # DB we declare version 1 directly since SCHEMA_SQL already
        # includes the post-v1 columns.
        import time as _time

        try:
            conn.execute(
                "INSERT OR IGNORE INTO kanban_schema_version (version, applied_at) VALUES (?, ?)",
                (1, int(_time.time())),
            )
        except sqlite3.OperationalError:
            # Table didn't exist in a legacy schema_sqlite.sql variant —
            # safe to ignore; the additive pass will create it next.
            pass

    @contextlib.contextmanager
    def write_txn(self, conn: sqlite3.Connection):
        """Exclusive write transaction via BEGIN IMMEDIATE.

        Same semantics as the original ``kanban_db.write_txn``. Commits
        on clean exit, rolls back on exception, never swallows.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


# Module-level singleton; the selector imports this and never builds
# new instances. There's no per-board state on the backend itself —
# everything board-specific lives on the connection.
INSTANCE = SqliteBackend()
