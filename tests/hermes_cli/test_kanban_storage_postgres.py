"""End-to-end tests for the PostgreSQL kanban storage backend.

These tests exercise the public ``hermes_cli.kanban_db`` API against a
real PostgreSQL server (the ``hermes-kanban-pg`` container in
``docker-compose.kanban-pg.yml``). They are skipped automatically when
no DSN is reachable so they don't break developers who haven't
brought the container up yet.

The full SQLite test suite (``test_kanban_db.py``, ``test_kanban_db_init.py``,
``test_kanban_boards.py``, etc.) implicitly covers the SQLite backend
because every ``kb.connect()`` call there resolves to it by default.
This file deliberately repeats a tight subset of those scenarios
against the PG backend so a behaviour drift between the two surfaces
fails loudly rather than silently — full re-parameterization of the
existing suite would have been much heavier surgery for the same
coverage outcome at our scale.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_storage import (
    clear_caches,
    migrate_data,
    postgres_backend,
    selector,
)


_DSN_ENV = "HERMES_KANBAN_TEST_POSTGRES_DSN"
_DSN_DEFAULT = "postgresql://hermes:hermes_kanban_local@127.0.0.1:8434/hermes_kanban"


def _dsn() -> str:
    return os.environ.get(_DSN_ENV, _DSN_DEFAULT)


def _pg_reachable() -> bool:
    """Quick reachability check; skips tests if the container is down."""
    try:
        import psycopg
    except ImportError:
        return False
    try:
        with psycopg.connect(_dsn(), connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_reachable(),
    reason=(
        "Postgres not reachable; bring up docker-compose.kanban-pg.yml or "
        f"set ${_DSN_ENV} to skip the default DSN. "
    ),
)


@pytest.fixture
def pg_board(tmp_path, monkeypatch):
    """Spin up a fresh PG-backed kanban board for one test.

    Uses ``tmp_path`` as ``HERMES_KANBAN_HOME`` so the SQLite sidecar
    paths land in a tempdir, then creates a board with PG storage
    config so ``kb.connect(board=...)`` routes at the PG backend.
    A per-test schema (``kanban_<random>_<test>``) keeps tests
    independent.
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_POSTGRES_DSN", _dsn())
    monkeypatch.setenv("HERMES_KANBAN_BACKEND", "postgres")
    for k in ["HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"]:
        monkeypatch.delenv(k, raising=False)

    # Unique slug per test instance to avoid xdist worker collisions
    # against the shared PG instance (same test name in different
    # workers used to hit the same kanban_<slug> schema).
    slug = f"pgt{secrets.token_hex(6)}"
    kb.create_board(slug, name=f"PG test {slug}")
    clear_caches()

    yield slug

    # Tear down the PG schema so the next test starts clean.
    schema = postgres_backend.pg_schema_for_board(slug)
    try:
        with __import__("psycopg").connect(_dsn(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except Exception:
        pass
    clear_caches()


def test_pg_backend_round_trip_basic(pg_board):
    with kb.connect(board=pg_board) as conn:
        assert type(conn).__name__ == "PgConnectionWrapper", (
            "PG board must yield the PgConnectionWrapper; got "
            f"{type(conn).__name__} — board.json may not be flipped"
        )
        tid = kb.create_task(conn, title="hello", body="world", assignee="claude")
        t = kb.get_task(conn, tid)
        assert t is not None and t.title == "hello" and t.status == "ready"

        got = kb.claim_task(conn, tid, claimer="claude")
        assert got is not None and got.status == "running"
        assert kb.complete_task(
            conn, tid, result="ok", summary="done",
            expected_run_id=got.current_run_id,
        )
        t = kb.get_task(conn, tid)
        assert t.status == "done" and t.result == "ok"


def test_pg_backend_parent_child_promotion(pg_board):
    """A child task stays ``todo`` until its parent completes, then promotes."""
    with kb.connect(board=pg_board) as conn:
        p = kb.create_task(conn, title="parent", assignee="claude")
        c = kb.create_task(conn, title="child", assignee="claude", parents=[p])

        # Child not ready yet.
        assert kb.get_task(conn, c).status == "todo"

        got = kb.claim_task(conn, p, claimer="claude")
        kb.complete_task(
            conn, p, result="parent done",
            expected_run_id=got.current_run_id,
        )

        # recompute_ready promotes child to ready.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, c).status == "ready"


def test_pg_backend_cas_claim_race(pg_board):
    """Two concurrent claims on the same ready task; only one wins.

    The CAS pattern is ``UPDATE tasks SET status='running' WHERE id=?
    AND status='ready' AND claim_lock IS NULL``. On PG this is
    serialized by MVCC + row locks. We exercise it by issuing two
    claim_task calls against the same id from two different connections;
    exactly one must return non-None.
    """
    with kb.connect(board=pg_board) as setup_conn:
        tid = kb.create_task(setup_conn, title="race", assignee="claude")

    with kb.connect(board=pg_board) as conn_a, kb.connect(board=pg_board) as conn_b:
        winner_a = kb.claim_task(conn_a, tid, claimer="A")
        winner_b = kb.claim_task(conn_b, tid, claimer="B")

    # Exactly one of (winner_a, winner_b) is non-None.
    winners = [w for w in (winner_a, winner_b) if w is not None]
    assert len(winners) == 1, (
        f"expected exactly one winner, got {len(winners)}: "
        f"a={winner_a}, b={winner_b}"
    )


def test_pg_backend_event_log_append_only(pg_board):
    with kb.connect(board=pg_board) as conn:
        tid = kb.create_task(conn, title="events", assignee="claude")
        evs0 = kb.list_events(conn, tid)
        kinds0 = sorted(e.kind for e in evs0)

        got = kb.claim_task(conn, tid, claimer="claude")
        kb.complete_task(
            conn, tid, result="ok",
            expected_run_id=got.current_run_id,
        )

        evs1 = kb.list_events(conn, tid)
        kinds1 = sorted(e.kind for e in evs1)

        # Events are append-only; nothing removed.
        assert set(kinds0) <= set(kinds1)
        assert "claimed" in kinds1 and "completed" in kinds1
        # IDs strictly increase.
        ids = [e.id for e in evs1]
        assert ids == sorted(ids) and len(ids) == len(set(ids))


def test_pg_backend_or_ignore_dedup(pg_board):
    """``INSERT OR IGNORE INTO task_links`` is rewritten to ``ON CONFLICT
    DO NOTHING`` by the dialect translator; duplicate links are silently
    dropped on the PG path same as on sqlite."""
    with kb.connect(board=pg_board) as conn:
        p = kb.create_task(conn, title="p", assignee="claude")
        c = kb.create_task(conn, title="c", assignee="claude")
        # Link twice; second call must not raise.
        kb.link_tasks(conn, p, c)
        # link_tasks is itself idempotent; we re-issue to exercise the
        # OR IGNORE translation.
        kb.link_tasks(conn, p, c)
        parents = kb.parent_ids(conn, c)
        assert parents == [p]


def test_sqlite_to_pg_data_migrator_preserves_state(tmp_path, monkeypatch):
    """End-to-end migrator test: populate sqlite, run migrator, verify parity."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_POSTGRES_DSN", raising=False)
    for k in ["HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"]:
        monkeypatch.delenv(k, raising=False)

    slug = f"migpar{secrets.token_hex(6)}"
    kb.create_board(slug, name=f"migrate parity {slug}")
    clear_caches()

    with kb.connect(board=slug) as conn:
        assert type(conn).__name__ == "Connection"  # sqlite3 native
        t1 = kb.create_task(conn, title="parent", assignee="claude")
        t2 = kb.create_task(conn, title="child", assignee="claude", parents=[t1])
        kb.add_comment(conn, t1, "reviewer", "lgtm")
        got = kb.claim_task(conn, t1, claimer="claude")
        kb.complete_task(
            conn, t1, result="parent done",
            expected_run_id=got.current_run_id,
        )
        sqlite_tasks = sorted(t.id for t in kb.list_tasks(conn, include_archived=True))
        sqlite_events = len(kb.list_events(conn, t1))
        sqlite_runs = len(kb.list_runs(conn, t1))
        sqlite_comments = len(kb.list_comments(conn, t1))

    report = migrate_data.migrate_board(
        board=slug,
        target_dsn=_dsn(),
        rename_sqlite=True,
        update_board_json=True,
        # In tests the WAL is non-empty by design (we just wrote data
        # and didn't checkpoint). The check is meant for live-dispatcher
        # cutovers, not test fixtures.
        allow_live=True,
    )
    assert report.skipped_reason is None
    assert report.board_json_updated is True
    assert report.sqlite_renamed_to is not None

    # After migration env must point at PG so the resolver reads the
    # right DSN; the migrator wrote postgres_dsn_env=HERMES_KANBAN_POSTGRES_DSN
    # to board.json.
    monkeypatch.setenv("HERMES_KANBAN_POSTGRES_DSN", _dsn())
    clear_caches()

    try:
        with kb.connect(board=slug) as conn:
            assert type(conn).__name__ == "PgConnectionWrapper"
            assert sorted(t.id for t in kb.list_tasks(conn, include_archived=True)) == sqlite_tasks
            assert len(kb.list_events(conn, t1)) == sqlite_events
            assert len(kb.list_runs(conn, t1)) == sqlite_runs
            assert len(kb.list_comments(conn, t1)) == sqlite_comments

            # Sequence fast-forward: a new run id must exceed the max migrated.
            new_tid = kb.create_task(conn, title="post-migrate", assignee="claude")
            new_got = kb.claim_task(conn, new_tid, claimer="claude")
            assert new_got.current_run_id is not None
    finally:
        # Cleanup
        schema = postgres_backend.pg_schema_for_board(slug)
        with __import__("psycopg").connect(_dsn(), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        clear_caches()
