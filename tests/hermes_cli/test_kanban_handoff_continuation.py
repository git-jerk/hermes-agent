"""Regression tests for expected review/fix handoff continuation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import profiles as profiles_mod


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles_mod, "profile_exists", lambda name: True)
    kb.init_db()
    return home


def _task(conn, task_id: str) -> kb.Task:
    task = kb.get_task(conn, task_id)
    assert task is not None
    return task


def _write_board_meta(board: str, **values: object) -> None:
    path = kb.board_metadata_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = kb.read_board_metadata(board)
    meta.pop("db_path", None)
    meta.update(values)
    path.write_text(json.dumps(meta), encoding="utf-8")


def test_review_required_block_routes_to_review_lane_and_spawns(kanban_home: Path) -> None:
    _write_board_meta("default", review_assignee="codexworker")
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 1234

    with kb.connect(board="default") as conn:
        tid = kb.create_task(conn, title="implement change", assignee="claude-code")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: implementation complete; needs independent review",
            expected_run_id=_task(conn, tid).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")

        task = _task(conn, tid)
        assert result.handoff_continued == 1
        assert spawned == [(tid, "codexworker")]
        assert task.status == "running"
        assert task.assignee == "codexworker"


def test_board_review_assignee_overrides_global_config(
    kanban_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        kb,
        "_load_kanban_cfg",
        lambda: {"review_assignee": "global-reviewer"},
    )
    _write_board_meta("default", review_assignee="board-reviewer")
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 1235

    with kb.connect(board="default") as conn:
        tid = kb.create_task(conn, title="implementation", assignee="impl-worker")
        assert kb.claim_task(conn, tid) is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: route through board lane",
            expected_run_id=_task(conn, tid).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")

        assert result.handoff_continued == 1
        assert spawned == [(tid, "board-reviewer")]
        assert _task(conn, tid).assignee == "board-reviewer"


def test_handoff_continuation_can_be_disabled_per_board(kanban_home: Path) -> None:
    _write_board_meta(
        "default",
        handoff_continuation_enabled=False,
        review_assignee="codexworker",
    )
    spawned: list[str] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append(task.id)
        return 1236

    with kb.connect(board="default") as conn:
        tid = kb.create_task(conn, title="implementation", assignee="claude-code")
        assert kb.claim_task(conn, tid) is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: disabled board should not continue",
            expected_run_id=_task(conn, tid).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")

        assert result.handoff_continued == 0
        assert spawned == []
        assert _task(conn, tid).status == "blocked"


def test_fix_card_created_reorients_dependency_and_routes_fix_to_impl_lane(kanban_home: Path) -> None:
    _write_board_meta(
        "default",
        review_assignee="codexworker",
        implementation_assignee="claude-code",
    )
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 2345

    with kb.connect(board="default") as conn:
        review_task = kb.create_task(conn, title="review original", assignee="codexworker")
        assert kb.claim_task(conn, review_task) is not None
        fix = kb.create_task(
            conn,
            title="fix reviewer finding",
            assignee="codexworker",
            parents=[review_task],
        )
        assert _task(conn, fix).status == "todo"
        assert kb.block_task(
            conn,
            review_task,
            reason=f"fix card created: {fix} addresses missing regression",
            expected_run_id=_task(conn, review_task).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")

        assert result.handoff_continued == 0
        assert spawned == [(fix, "claude-code")]
        assert _task(conn, review_task).status == "blocked"
        assert _task(conn, fix).status == "running"
        assert _task(conn, fix).assignee == "claude-code"
        old_edge = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (review_task, fix),
        ).fetchone()
        new_edge = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (fix, review_task),
        ).fetchone()
        assert old_edge is None
        assert new_edge is not None


def test_fix_card_handoff_ignores_unrelated_task_ids_in_non_handoff_comments(
    kanban_home: Path,
) -> None:
    _write_board_meta(
        "default",
        review_assignee="codexworker",
        implementation_assignee="claude-code",
    )
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 2346

    with kb.connect(board="default") as conn:
        open_parent = kb.create_task(conn, title="unrelated parent", assignee="default")
        assert kb.claim_task(conn, open_parent) is not None
        unrelated = kb.create_task(
            conn,
            title="unrelated task mentioned in context",
            assignee="codexworker",
            parents=[open_parent],
        )
        review_task = kb.create_task(conn, title="review original", assignee="codexworker")
        assert kb.claim_task(conn, review_task) is not None
        kb.add_comment(
            conn,
            review_task,
            "operator",
            f"Context only: sibling task {unrelated} is unrelated to this review.",
        )
        assert kb.block_task(
            conn,
            review_task,
            reason="fix card created: reviewer found a blocker, but no card id is present",
            expected_run_id=_task(conn, review_task).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, board="default")

        assert result.handoff_continued == 0
        assert spawned == []
        assert _task(conn, review_task).status == "blocked"
        unrelated_task = _task(conn, unrelated)
        assert unrelated_task.status == "todo"
        assert unrelated_task.assignee == "codexworker"
        unrelated_edge = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (unrelated, review_task),
        ).fetchone()
        assert unrelated_edge is None


def test_fix_card_handoff_reads_ids_only_from_explicit_handoff_comments(
    kanban_home: Path,
) -> None:
    _write_board_meta(
        "default",
        review_assignee="codexworker",
        implementation_assignee="claude-code",
    )
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 2347

    with kb.connect(board="default") as conn:
        open_parent = kb.create_task(conn, title="unrelated parent", assignee="default")
        assert kb.claim_task(conn, open_parent) is not None
        unrelated = kb.create_task(
            conn,
            title="unrelated task mentioned in context",
            assignee="codexworker",
            parents=[open_parent],
        )
        review_task = kb.create_task(conn, title="review original", assignee="codexworker")
        assert kb.claim_task(conn, review_task) is not None
        fix = kb.create_task(
            conn,
            title="fix reviewer finding",
            assignee="codexworker",
            parents=[review_task],
        )
        kb.add_comment(
            conn,
            review_task,
            "operator",
            f"Context only: sibling task {unrelated} is unrelated to this review.",
        )
        kb.add_comment(
            conn,
            review_task,
            "reviewer",
            f"fix card created: {fix} addresses the review finding.",
        )
        assert kb.block_task(
            conn,
            review_task,
            reason="fix card created: see reviewer comment for the card id",
            expected_run_id=_task(conn, review_task).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, board="default")

        assert result.handoff_continued == 0
        assert spawned == [(fix, "claude-code")]
        assert _task(conn, fix).status == "running"
        assert _task(conn, fix).assignee == "claude-code"
        unrelated_task = _task(conn, unrelated)
        assert unrelated_task.status == "todo"
        assert unrelated_task.assignee == "codexworker"
        unrelated_edge = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (unrelated, review_task),
        ).fetchone()
        assert unrelated_edge is None


def test_reviewer_is_redispatched_after_referenced_fix_card_completes(kanban_home: Path) -> None:
    _write_board_meta(
        "default",
        review_assignee="codexworker",
        implementation_assignee="claude-code",
    )
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 3456

    with kb.connect(board="default") as conn:
        review_task = kb.create_task(conn, title="review original", assignee="codexworker")
        fix = kb.create_task(conn, title="fix reviewer finding", assignee="claude-code")
        assert kb.claim_task(conn, review_task) is not None
        assert kb.block_task(
            conn,
            review_task,
            reason=f"fix card created: {fix} has been queued",
            expected_run_id=_task(conn, review_task).current_run_id,
        )
        assert kb.claim_task(conn, fix) is not None
        assert kb.complete_task(conn, fix, summary="fix complete")

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")

        assert result.handoff_continued == 1
        assert spawned == [(review_task, "codexworker")]
        assert _task(conn, review_task).status == "running"
        assert _task(conn, review_task).assignee == "codexworker"


def test_fix_card_created_ignores_unmarked_task_ids_in_comments(kanban_home: Path) -> None:
    _write_board_meta("default", implementation_assignee="claude-code")
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 3457

    with kb.connect(board="default") as conn:
        review_task = kb.create_task(conn, title="review original", assignee="codexworker")
        unrelated = kb.create_task(conn, title="unrelated follow-up", assignee="codexworker")
        kb.add_comment(conn, review_task, "reviewer", f"Related but not a fix card: {unrelated}")
        assert kb.claim_task(conn, review_task) is not None
        assert kb.block_task(
            conn,
            review_task,
            reason="fix card created: queued in a separate handoff comment",
            expected_run_id=_task(conn, review_task).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=2, board="default")

        assert result.handoff_continued == 0
        assert spawned == [(unrelated, "codexworker")]
        assert _task(conn, review_task).status == "blocked"
        assert _task(conn, unrelated).assignee == "codexworker"
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (unrelated, review_task),
        ).fetchone() is None


def test_fix_card_created_accepts_explicit_handoff_comment(kanban_home: Path) -> None:
    _write_board_meta("default", implementation_assignee="claude-code")
    spawned: list[tuple[str, str]] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append((task.id, task.assignee or ""))
        return 3458

    with kb.connect(board="default") as conn:
        review_task = kb.create_task(conn, title="review original", assignee="codexworker")
        fix = kb.create_task(conn, title="fix reviewer finding", assignee="codexworker")
        kb.add_comment(conn, review_task, "reviewer", f"fix card created: {fix}")
        assert kb.claim_task(conn, review_task) is not None
        assert kb.block_task(
            conn,
            review_task,
            reason="fix card created: see explicit handoff comment",
            expected_run_id=_task(conn, review_task).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")

        assert result.handoff_continued == 0
        assert spawned == [(fix, "claude-code")]
        assert _task(conn, review_task).status == "blocked"
        assert _task(conn, fix).assignee == "claude-code"


def test_true_blocker_mentioning_fix_card_stays_blocked(kanban_home: Path) -> None:
    _write_board_meta("default", implementation_assignee="claude-code")
    spawned: list[str] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append(task.id)
        return 3459

    with kb.connect(board="default") as conn:
        blocker = kb.create_task(conn, title="review original", assignee="codexworker")
        related = kb.create_task(conn, title="related task", assignee="codexworker")
        assert kb.claim_task(conn, blocker) is not None
        assert kb.block_task(
            conn,
            blocker,
            reason=f"fix card missing for {related}: need human decision",
            expected_run_id=_task(conn, blocker).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, board="default")

        assert result.handoff_continued == 0
        assert spawned == [related]
        assert _task(conn, blocker).status == "blocked"
        assert conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            (related, blocker),
        ).fetchone() is None


def test_review_required_dry_run_reports_handoff_without_mutating(kanban_home: Path) -> None:
    _write_board_meta("default", review_assignee="codexworker")

    with kb.connect(board="default") as conn:
        tid = kb.create_task(conn, title="implementation", assignee="impl-worker")
        assert kb.claim_task(conn, tid) is not None
        assert kb.block_task(
            conn,
            tid,
            reason="review-required: dry-run should preview only",
            expected_run_id=_task(conn, tid).current_run_id,
        )

        result = kb.dispatch_once(conn, dry_run=True, max_spawn=1, board="default")

        assert result.handoff_continued == 1
        assert result.spawned == []
        assert _task(conn, tid).status == "blocked"
        assert _task(conn, tid).assignee == "impl-worker"


def test_financial_live_execution_board_serializes_dry_run_dispatch(kanban_home: Path) -> None:
    kb.create_board("dry-trading")
    _write_board_meta("dry-trading", live_execution=True, parallel_dispatch_approved=False)

    with kb.connect(board="dry-trading") as conn:
        first = kb.create_task(conn, title="first live task", assignee="default")
        second = kb.create_task(conn, title="second live task", assignee="default")

        result = kb.dispatch_once(conn, dry_run=True, max_spawn=5, board="dry-trading")

        assert [row[0] for row in result.spawned] == [first]
        assert _task(conn, first).status == "ready"
        assert _task(conn, second).status == "ready"


def test_true_blocker_stays_blocked_and_does_not_spawn(kanban_home: Path) -> None:
    spawned: list[str] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append(task.id)
        return 4567

    with kb.connect(board="default") as conn:
        tid = kb.create_task(conn, title="needs credentials", assignee="default")
        assert kb.claim_task(conn, tid) is not None
        assert kb.block_task(
            conn,
            tid,
            reason="missing credentials: needs operator approval",
            expected_run_id=_task(conn, tid).current_run_id,
        )

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=5, board="default")

        assert result.handoff_continued == 0
        assert spawned == []
        assert _task(conn, tid).status == "blocked"


def test_financial_live_execution_board_serializes_dispatch_by_default(kanban_home: Path) -> None:
    kb.create_board("trading")
    _write_board_meta("trading", live_execution=True, parallel_dispatch_approved=False)
    spawned: list[str] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append(task.id)
        return 5678

    with kb.connect(board="trading") as conn:
        first = kb.create_task(conn, title="first live task", assignee="default")
        second = kb.create_task(conn, title="second live task", assignee="default")

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=5, board="trading")

        assert spawned == [first]
        assert result.spawned[0][0] == first
        assert _task(conn, first).status == "running"
        assert _task(conn, second).status == "ready"


def test_explicit_max_spawn_zero_pauses_regular_and_serialized_dispatch(
    kanban_home: Path,
) -> None:
    spawned: list[str] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append(task.id)
        return 6780

    with kb.connect(board="default") as conn:
        regular = kb.create_task(conn, title="regular paused task", assignee="default")
        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=0, board="default")

        assert spawned == []
        assert result.spawned == []
        assert _task(conn, regular).status == "ready"

    kb.create_board("paused-trading")
    _write_board_meta("paused-trading", live_execution=True, parallel_dispatch_approved=False)
    with kb.connect(board="paused-trading") as conn:
        sensitive = kb.create_task(conn, title="sensitive paused task", assignee="default")
        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=0, board="paused-trading")

        assert spawned == []
        assert result.spawned == []
        assert _task(conn, sensitive).status == "ready"


def test_explicit_max_in_progress_zero_pauses_regular_and_serialized_dispatch(
    kanban_home: Path,
) -> None:
    spawned: list[str] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append(task.id)
        return 6781

    with kb.connect(board="default") as conn:
        regular = kb.create_task(conn, title="regular in-progress paused", assignee="default")
        result = kb.dispatch_once(
            conn, spawn_fn=spawn, max_spawn=5, max_in_progress=0, board="default"
        )

        assert spawned == []
        assert result.spawned == []
        assert _task(conn, regular).status == "ready"

    kb.create_board("in-progress-paused-trading")
    _write_board_meta(
        "in-progress-paused-trading",
        live_execution=True,
        parallel_dispatch_approved=False,
    )
    with kb.connect(board="in-progress-paused-trading") as conn:
        sensitive = kb.create_task(conn, title="sensitive in-progress paused", assignee="default")
        result = kb.dispatch_once(
            conn,
            spawn_fn=spawn,
            max_spawn=5,
            max_in_progress=0,
            board="in-progress-paused-trading",
        )

        assert spawned == []
        assert result.spawned == []
        assert _task(conn, sensitive).status == "ready"


def test_parallel_dispatch_approved_escapes_sensitive_board_serialization(kanban_home: Path) -> None:
    kb.create_board("approved-trading")
    _write_board_meta(
        "approved-trading",
        financial=True,
        parallel_dispatch_approved=True,
    )
    spawned: list[str] = []

    def spawn(task: kb.Task, workspace: str, board: str | None = None) -> int:
        spawned.append(task.id)
        return 6789

    with kb.connect(board="approved-trading") as conn:
        first = kb.create_task(conn, title="first approved task", assignee="default")
        second = kb.create_task(conn, title="second approved task", assignee="default")

        result = kb.dispatch_once(conn, spawn_fn=spawn, max_spawn=5, board="approved-trading")

        assert spawned == [first, second]
        assert [row[0] for row in result.spawned] == [first, second]
        assert _task(conn, first).status == "running"
        assert _task(conn, second).status == "running"
