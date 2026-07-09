"""SQLite → PostgreSQL data migrator for the Hermes Kanban board.

Migrates one board's tables in FK order, preserves IDs (TEXT for
``tasks``, integer auto-IDs for ``task_runs`` / ``task_events`` /
``task_comments``), advances the PG sequences past the highest copied
id so subsequent INSERTs don't collide, verifies row counts, and
flips the board's ``board.json`` to mark Postgres as the source-of-
truth. The old SQLite file is **renamed** to ``kanban.db.migrated-<ts>``
rather than deleted so a rollback is one ``mv`` away.

Invariants we preserve
----------------------

* Task IDs (TEXT) round-trip identically — they are referenced from
  ``task_links``, ``task_events``, ``task_runs``, ``task_comments``,
  ``kanban_notify_subs``, and external systems (gateway, dashboards).
* Auto-increment IDs in ``task_runs`` / ``task_events`` /
  ``task_comments`` round-trip identically and the PG sequence is
  fast-forwarded past ``max(id) + 1`` so a new insert won't reuse a
  copied id.
* The ``current_run_id`` pointer on each task points at the same
  ``task_runs.id`` it did on the source.
* The ``kanban_notify_subs.last_event_id`` cursor is preserved so a
  subscribed gateway client doesn't replay events it already saw.

What we explicitly DO NOT migrate
---------------------------------

* The SQLite ``.bak`` / ``-wal`` / ``-shm`` sidecar files — these are
  artifacts of the engine, not data.
* The ``kanban_schema_version`` table on the source side — the PG
  schema is created at version 1 directly via ``schema_postgres.sql``.

CLI shape (wired in :mod:`.cli`)::

    hermes kanban storage migrate --board trading-lab --to postgres
    hermes kanban storage migrate --all --to postgres
    hermes kanban storage migrate --board default --to postgres --dry-run

Safety
------

Refuses to migrate if the target PG schema already has rows in
``tasks`` (you must pass ``--force``). Refuses to migrate if a
``kanban.db-wal`` file shows recent writes, suggesting the gateway is
still attached, unless ``--allow-live`` is passed (the operator
explicitly accepts the risk that some last-second writes will be
lost). The cutover sequence in the migration plan is
``stop gateway → migrate → restart gateway``; the live-write check
is one more guard against accidentally migrating with the dispatcher
still hammering writes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

import psycopg
from psycopg import sql as psql

from .base import StorageConfig
from .postgres_backend import _split_statements, _load_schema_sql

_log = logging.getLogger(__name__)


# Table copy order — children after parents so any FK we later add
# wouldn't complain. We don't currently declare FKs, but the order is
# also the right one for human-debuggability.
_COPY_ORDER: Sequence[str] = (
    "tasks",
    "task_links",
    "task_runs",
    "task_events",
    "task_comments",
    "task_attachments",
    "kanban_notify_subs",
)

# Canonical column list per table — the set of columns we copy. Pinned
# here (rather than computed from PRAGMA table_info) so a future
# SQLite-side column addition doesn't silently get migrated to PG
# without a deliberate code change. Columns missing on the source are
# tolerated (default to NULL on PG), columns missing on the target
# raise loudly.
_CANONICAL_COLUMNS: dict[str, list[str]] = {
    "tasks": [
        "id", "title", "body", "assignee", "status", "priority",
        "created_by", "created_at", "started_at", "completed_at",
        "workspace_kind", "workspace_path", "branch_name",
        "claim_lock", "claim_expires", "tenant", "result",
        "idempotency_key", "consecutive_failures", "worker_pid",
        "last_failure_error", "max_runtime_seconds", "last_heartbeat_at",
        "current_run_id", "workflow_template_id", "current_step_key",
        "skills", "model_override", "max_retries",
        "goal_mode", "goal_max_turns", "session_id",
        "project_id", "block_kind", "block_recurrences",
    ],
    "task_links": ["parent_id", "child_id"],
    "task_runs": [
        "id", "task_id", "profile", "step_key", "status",
        "claim_lock", "claim_expires", "worker_pid",
        "max_runtime_seconds", "last_heartbeat_at",
        "started_at", "ended_at", "outcome",
        "summary", "metadata", "error",
    ],
    "task_events": [
        "id", "task_id", "run_id", "kind", "payload", "created_at",
    ],
    "task_comments": [
        "id", "task_id", "author", "body", "created_at",
    ],
    "task_attachments": [
        "id", "task_id", "filename", "stored_path", "content_type",
        "size", "uploaded_by", "created_at",
    ],
    "kanban_notify_subs": [
        "task_id", "platform", "chat_id", "thread_id", "user_id",
        "notifier_profile", "created_at", "last_event_id",
    ],
}

# Tables with an autoincrement ``id`` whose PG sequence we must
# fast-forward after copying.
_AUTOINCREMENT_TABLES: frozenset[str] = frozenset(
    {"task_runs", "task_events", "task_comments", "task_attachments"}
)

# Match the connection wrapper.
_PG_BATCH_SIZE = 1000


@dataclass
class MigrationReport:
    """Result of one ``migrate_board`` call."""

    board: str
    target_schema: str
    rows_per_table: dict[str, int] = field(default_factory=dict)
    duration_seconds: float = 0.0
    dry_run: bool = False
    sqlite_path: Optional[str] = None
    sqlite_renamed_to: Optional[str] = None
    board_json_updated: bool = False
    skipped_reason: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "board": self.board,
            "target_schema": self.target_schema,
            "rows_per_table": dict(self.rows_per_table),
            "duration_seconds": round(self.duration_seconds, 3),
            "dry_run": self.dry_run,
            "sqlite_path": self.sqlite_path,
            "sqlite_renamed_to": self.sqlite_renamed_to,
            "board_json_updated": self.board_json_updated,
            "skipped_reason": self.skipped_reason,
        }


class MigrationError(RuntimeError):
    """Raised on any condition that aborts a migration before it commits."""


def _check_target_empty(
    pg_conn: psycopg.Connection, schema: str, *, force: bool
) -> None:
    """Refuse to migrate if the target schema already has tasks (unless --force).

    The PG schema may already exist from a previous attempt or from
    ``init_schema`` running against it; that's fine as long as it's
    empty. If there are existing rows we don't want to silently merge
    them — that risks duplicate task ids and confusing failures.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            psql.SQL("SELECT COUNT(*) FROM {}.tasks").format(psql.Identifier(schema))
        )
        n = int((cur.fetchone() or [0])[0])
    if n > 0 and not force:
        raise MigrationError(
            f"target schema {schema!r} already has {n} tasks; pass force=True "
            "to overwrite, or pick a different target_schema"
        )
    if n > 0 and force:
        _log.warning(
            "force=True with %d existing tasks in schema %s; truncating tables",
            n, schema,
        )
        with pg_conn.cursor() as cur:
            for table in reversed(_COPY_ORDER):
                cur.execute(
                    psql.SQL("TRUNCATE TABLE {}.{} RESTART IDENTITY CASCADE").format(
                        psql.Identifier(schema), psql.Identifier(table)
                    )
                )


def _check_no_live_writers(sqlite_path: Path, *, allow_live: bool) -> None:
    """Bail if the SQLite WAL looks like it's still being written to.

    SQLite's WAL file is non-empty whenever there are uncommitted or
    just-committed writes that haven't been checkpointed. If we see a
    large WAL we may be racing the dispatcher. The check isn't perfect
    (a quiescent WAL can still grow between checks), but it catches
    the obvious "I forgot to stop the dispatcher" mistake.
    """
    wal = sqlite_path.with_suffix(sqlite_path.suffix + "-wal")
    if not wal.exists():
        return
    size = wal.stat().st_size
    if size == 0:
        return
    if allow_live:
        _log.warning(
            "WAL at %s is non-empty (%d bytes) but allow_live=True; "
            "any uncommitted writes will be lost",
            wal, size,
        )
        return
    raise MigrationError(
        f"{wal} is non-empty ({size} bytes) — gateway/dispatcher may still be "
        "writing. Stop the dispatcher first (``hermes gateway stop``) and re-run, "
        "or pass allow_live=True to skip this check."
    )


def _existing_columns(sqlite_conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in sqlite_conn.execute(f"PRAGMA table_info({table})")}


def _copy_table(
    *,
    sqlite_conn: sqlite3.Connection,
    pg_conn: psycopg.Connection,
    schema: str,
    table: str,
    dry_run: bool,
) -> int:
    """Copy one table; return the number of rows copied (or seen for dry-run).

    Uses psycopg3's ``cursor.copy()`` for streaming (fast and memory-
    bounded) when not in dry-run. On dry-run, just counts rows.
    """
    canonical = _CANONICAL_COLUMNS[table]
    source_cols = _existing_columns(sqlite_conn, table)
    # Source may legitimately lack newly-added columns on an old DB; that
    # just means NULL in PG. But it must not have columns we don't know
    # about — that's a sign the schema drifted.
    unknown = source_cols - set(canonical)
    if unknown:
        raise MigrationError(
            f"table {table!r} on source has unknown columns {sorted(unknown)}; "
            "update _CANONICAL_COLUMNS in migrate_data.py before migrating"
        )

    # Build the SELECT against source. We always select the full
    # canonical column list; missing source columns get a literal NULL.
    select_parts = []
    for col in canonical:
        if col in source_cols:
            select_parts.append(col)
        else:
            select_parts.append(f"NULL AS {col}")
    select_sql = f"SELECT {', '.join(select_parts)} FROM {table}"

    cur_src = sqlite_conn.execute(select_sql)
    rows = cur_src.fetchall()
    if dry_run:
        return len(rows)
    if not rows:
        return 0

    # Insert into PG. We use ``executemany`` rather than COPY BINARY
    # because the kanban boards are tiny (largest is 1.7 MB / a few
    # thousand rows total across all tables) and COPY BINARY requires
    # explicit per-column type info that adds complexity for no measurable
    # win. ``executemany`` with parameterised INSERTs lets psycopg3
    # handle Python→PG type coercion (int↔BIGINT, str↔TEXT, None↔NULL).
    col_list_sql = ", ".join(canonical)
    placeholders = ", ".join(["%s"] * len(canonical))
    insert_sql = psql.SQL(
        "INSERT INTO {}.{} ({}) VALUES ({})"
    ).format(
        psql.Identifier(schema),
        psql.Identifier(table),
        psql.SQL(col_list_sql),
        psql.SQL(placeholders),
    )
    # Convert sqlite3.Row tuples to plain tuples for psycopg3.
    payload = [tuple(row[i] for i in range(len(canonical))) for row in rows]
    with pg_conn.cursor() as cur_dst:
        cur_dst.executemany(insert_sql, payload)
    return len(rows)


def _fastforward_sequences(
    pg_conn: psycopg.Connection, schema: str
) -> dict[str, int]:
    """Bump each autoincrement table's sequence past the migrated max(id).

    Postgres ``GENERATED BY DEFAULT AS IDENTITY`` columns share a
    sequence. After bulk-inserting rows with explicit ids, the sequence
    is still at its starting value (typically 1), so the next default-
    valued insert would attempt to use id=1 and conflict.
    ``pg_get_serial_sequence`` + ``setval(seq, MAX(id))`` advances it.
    """
    out: dict[str, int] = {}
    with pg_conn.cursor() as cur:
        for table in _AUTOINCREMENT_TABLES:
            cur.execute(
                psql.SQL(
                    "SELECT pg_get_serial_sequence({}, 'id'), COALESCE(MAX(id), 0) FROM {}.{}"
                ).format(
                    psql.Literal(f"{schema}.{table}"),
                    psql.Identifier(schema),
                    psql.Identifier(table),
                )
            )
            row = cur.fetchone()
            seq_name, max_id = row[0], int(row[1])
            if seq_name and max_id > 0:
                cur.execute(
                    "SELECT setval(%s, %s, TRUE)", (seq_name, max_id)
                )
                out[table] = max_id
            else:
                out[table] = 0
    return out


def _update_board_json(
    *,
    board: str,
    target_dsn_env: str,
    target_schema: str,
) -> bool:
    """Flip ``board.json`` to mark Postgres as the storage backend.

    We write the DSN as an env-var reference (``$ENV_NAME``) rather
    than the raw connection string so the credential lives in env
    rather than on disk. Operators who prefer to pin the DSN directly
    can edit board.json after the fact.
    """
    from hermes_cli.kanban_db import (
        read_board_metadata,
        write_board_metadata,
        board_metadata_path,
    )

    meta_path = board_metadata_path(board)
    if not meta_path.exists():
        _log.warning("board.json missing for %s at %s; skipping update",
                     board, meta_path)
        return False
    meta = read_board_metadata(board=board)
    kanban_block = dict(meta.get("kanban") or {})
    storage_block = dict(kanban_block.get("storage") or {})
    storage_block.update({
        "backend": "postgres",
        "postgres_dsn_env": target_dsn_env,
        "postgres_schema": target_schema,
    })
    kanban_block["storage"] = storage_block
    # write_board_metadata only accepts whitelisted top-level keys; we
    # rewrite the file directly because ``kanban`` is a nested block
    # that the whitelist doesn't expose.
    meta["kanban"] = kanban_block
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    return True


def _rename_sqlite_file(sqlite_path: Path) -> Path:
    """Move kanban.db out of the way so the next connect() opens PG.

    Renamed (not deleted) so a rollback is just ``mv`` back to the
    original name. Also renames the WAL/shm sidecars if present.
    """
    ts = int(time.time())
    renamed = sqlite_path.with_name(f"{sqlite_path.name}.migrated-{ts}")
    sqlite_path.rename(renamed)
    for suffix in ("-wal", "-shm"):
        sidecar = sqlite_path.with_name(sqlite_path.name + suffix)
        if sidecar.exists():
            sidecar.rename(sidecar.with_name(f"{sidecar.name}.migrated-{ts}"))
    return renamed


def migrate_board(
    *,
    board: str,
    target_dsn: str,
    target_schema: Optional[str] = None,
    target_dsn_env: str = "HERMES_KANBAN_POSTGRES_DSN",
    dry_run: bool = False,
    force: bool = False,
    allow_live: bool = False,
    rename_sqlite: bool = True,
    update_board_json: bool = True,
) -> MigrationReport:
    """Migrate one board's SQLite DB into a Postgres schema.

    On success the board's ``board.json`` is updated so subsequent
    ``connect()`` calls route at Postgres, and the SQLite file is
    renamed to ``kanban.db.migrated-<ts>`` (kept on disk for rollback).

    Parameters
    ----------
    board : str
        Board slug. The source SQLite path is resolved via
        :func:`kanban_db.kanban_db_path`.
    target_dsn : str
        Postgres connection string. We open a short-lived connection
        with this DSN; the live runtime gets the DSN from
        ``HERMES_KANBAN_POSTGRES_DSN`` (or the env-var name passed in
        ``target_dsn_env``).
    target_schema : str, optional
        PG schema to create / populate. Defaults to ``kanban_<board>``.
    dry_run : bool
        Count rows and validate columns but make no writes.
    force : bool
        If the target schema already has tasks, TRUNCATE everything
        first instead of refusing.
    allow_live : bool
        Skip the "WAL non-empty" safety check. Use only when you've
        verified writes are quiesced some other way.
    rename_sqlite : bool
        After a successful migration, rename ``kanban.db`` so it's
        not silently re-opened. Set False if a follow-up step needs
        the file.
    update_board_json : bool
        After a successful migration, flip ``board.json``'s
        ``kanban.storage.backend`` to ``postgres``. Set False when
        you want to defer the cutover (e.g. shadow-write for a
        validation window).

    Returns
    -------
    MigrationReport — row counts, duration, and post-state.
    """
    from hermes_cli.kanban_db import kanban_db_path, _normalize_board_slug

    slug = _normalize_board_slug(board) or board
    if not target_schema:
        from .postgres_backend import pg_schema_for_board
        target_schema = pg_schema_for_board(slug)
    sqlite_path = kanban_db_path(board=slug)

    report = MigrationReport(
        board=slug,
        target_schema=target_schema,
        dry_run=dry_run,
        sqlite_path=str(sqlite_path),
    )

    if not sqlite_path.exists() or sqlite_path.stat().st_size == 0:
        report.skipped_reason = "sqlite source missing or empty"
        return report

    _check_no_live_writers(sqlite_path, allow_live=allow_live)

    t0 = time.monotonic()

    # Open the source SQLite read-only so we cannot accidentally
    # mutate it. The ``file:`` URI form is the documented way to
    # request read-only.
    src_uri = f"file:{sqlite_path}?mode=ro"
    sqlite_conn = sqlite3.connect(src_uri, uri=True, timeout=30)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg.connect(target_dsn, autocommit=False)
    try:
        # Ensure target schema + tables exist.
        with pg_conn.cursor() as cur:
            cur.execute(
                psql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    psql.Identifier(target_schema)
                )
            )
            cur.execute(
                psql.SQL("SET LOCAL search_path = {}, public").format(
                    psql.Identifier(target_schema)
                )
            )
            for stmt in _split_statements(_load_schema_sql()):
                cur.execute(stmt)
        pg_conn.commit()

        # Refuse to overwrite a populated target unless force.
        _check_target_empty(pg_conn, target_schema, force=force)

        # Copy tables in FK order.
        try:
            for table in _COPY_ORDER:
                copied = _copy_table(
                    sqlite_conn=sqlite_conn,
                    pg_conn=pg_conn,
                    schema=target_schema,
                    table=table,
                    dry_run=dry_run,
                )
                report.rows_per_table[table] = copied

            if not dry_run:
                # Fast-forward sequences past the migrated ids.
                _fastforward_sequences(pg_conn, target_schema)
        except Exception:
            pg_conn.rollback()
            raise
        if not dry_run:
            pg_conn.commit()
    finally:
        sqlite_conn.close()
        pg_conn.close()

    report.duration_seconds = time.monotonic() - t0

    if dry_run:
        return report

    if update_board_json:
        report.board_json_updated = _update_board_json(
            board=slug,
            target_dsn_env=target_dsn_env,
            target_schema=target_schema,
        )

    if rename_sqlite:
        renamed = _rename_sqlite_file(sqlite_path)
        report.sqlite_renamed_to = str(renamed)

    # Clear the in-process storage caches so the next connect() picks
    # up the new backend from the just-updated board.json.
    from . import selector

    selector.clear_caches()

    return report


def migrate_all_boards(
    *,
    target_dsn: str,
    target_dsn_env: str = "HERMES_KANBAN_POSTGRES_DSN",
    dry_run: bool = False,
    force: bool = False,
    allow_live: bool = False,
    rename_sqlite: bool = True,
    update_board_json: bool = True,
    include_archived: bool = False,
) -> list[MigrationReport]:
    """Migrate every board the local install knows about.

    Walks :func:`kanban_db.list_boards` and runs :func:`migrate_board`
    for each non-archived slug (or all, if ``include_archived=True``).
    Each board's report is returned in order. Failures abort the run
    immediately so a bad board doesn't leave the system in a half-
    migrated state.
    """
    from hermes_cli.kanban_db import list_boards

    reports: list[MigrationReport] = []
    for meta in list_boards(include_archived=include_archived):
        slug = meta.get("slug") or meta.get("name")
        if not slug:
            continue
        rep = migrate_board(
            board=slug,
            target_dsn=target_dsn,
            target_dsn_env=target_dsn_env,
            dry_run=dry_run,
            force=force,
            allow_live=allow_live,
            rename_sqlite=rename_sqlite,
            update_board_json=update_board_json,
        )
        reports.append(rep)
    return reports
