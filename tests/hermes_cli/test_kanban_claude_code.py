"""Claude Code background-lane tests for Hermes Kanban."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_claude_code as kcc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_known_assignees_include_configured_claude_code_lanes(kanban_home, monkeypatch):
    monkeypatch.setattr(
        kcc,
        "configured_assignees",
        lambda config=None: {"claude-code", "claude-review"},
    )

    with kb.connect() as conn:
        names = {entry["name"]: entry for entry in kb.known_assignees(conn)}

    assert "claude-code" in names
    assert "claude-review" in names
    assert names["claude-review"]["on_disk"] is False
    assert names["claude-review"]["counts"] == {}


def test_parse_claude_bg_session_id_from_current_cli_output():
    output = """Starting background service…
backgrounded · 0f40b52b
  claude agents             list sessions
  claude attach 0f40b52b    open in this terminal
"""

    assert kcc.parse_claude_bg_session_id(output) == "0f40b52b"


def test_detect_claude_auth_failure_from_logs():
    auth_401 = kcc.detect_claude_auth_failure(
        "Claude Code authentication failed: 401 Unauthorized"
    )
    auth_login = kcc.detect_claude_auth_failure("Claude Code: please run /login first")
    auth_token = kcc.detect_claude_auth_failure(
        "Claude authentication failed token=abc123: 401 Unauthorized"
    )

    assert auth_401 is not None and "401" in auth_401
    assert auth_login is not None and "/login" in auth_login
    assert auth_token is not None and "abc123" not in auth_token
    assert kcc.detect_claude_auth_failure("Claude Code\nPlease run /login first") is not None
    assert kcc.detect_claude_auth_failure("Claude Code authentication failed:\n401 Unauthorized") is not None
    assert kcc.detect_claude_auth_failure("Please run /login first") is not None
    assert kcc.detect_claude_auth_failure("pytest\nPlease run /login first") is not None
    assert kcc.detect_claude_auth_failure("Please run /login first\npytest") is not None
    assert kcc.detect_claude_auth_failure("Claude Code needs authentication\npytest captured previous output") is not None
    assert kcc.detect_claude_auth_failure("Claude Code\n401 Unauthorized while running tests") is not None
    assert kcc.detect_claude_auth_failure("Claude Code\n401 Unauthorized during integration tests") is not None
    assert kcc.detect_claude_auth_failure("Anthropic\n401 Unauthorized in unit test") is not None
    assert kcc.detect_claude_auth_failure("Claude Code\nnot logged in while running tests") is not None
    assert kcc.detect_claude_auth_failure("Anthropic OAuth endpoint returned 401 Unauthorized") is not None
    assert kcc.detect_claude_auth_failure("Claude returned 401 Unauthorized") is not None
    assert kcc.detect_claude_auth_failure("Anthropic returned 401 Unauthorized") is not None
    assert kcc.detect_claude_auth_failure("Claude Code returned 401 Unauthorized") is not None
    assert kcc.detect_claude_auth_failure("Anthropic API returned 401 Unauthorized") is not None
    assert kcc.detect_claude_auth_failure("Claude Code authentication failed while running tests") is not None
    assert kcc.detect_claude_auth_failure("background task is healthy") is None
    assert kcc.detect_claude_auth_failure("pytest: expected 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("pytest: expected 401 Unauthorized from login endpoint") is None
    assert kcc.detect_claude_auth_failure("Claude: GET /login returned 200") is None
    assert kcc.detect_claude_auth_failure("Claude: GET /login returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Claude Code: running pytest expecting 401 Unauthorized from /login") is None
    assert kcc.detect_claude_auth_failure("Anthropic SDK test expects 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Claude mock returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Anthropic mock returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Claude fixture returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Anthropic test server returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Anthropic SDK returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Claude SDK returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Anthropic client returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("Claude proxy returned 401 Unauthorized") is None
    assert kcc.detect_claude_auth_failure("GET /login returned 200") is None
    assert kcc.detect_claude_auth_failure("/login returned 200") is None
    assert kcc.detect_claude_auth_failure("/login endpoint") is None
    assert kcc.detect_claude_auth_failure("/login route for demo app") is None
    assert kcc.detect_claude_auth_failure("app error: please run /login first to obtain a demo session") is None
    assert kcc.detect_claude_auth_failure("pytest captured: run /login before accessing dashboard") is None
    assert kcc.detect_claude_auth_failure("user is not logged in to the demo app") is None


def test_build_worker_prompt_warns_about_stale_background_env():
    task = kb.Task(
        id="t_current",
        title="current task",
        body=None,
        assignee="claude-code",
        status="running",
        priority=0,
        created_by="user",
        created_at=0,
        started_at=0,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path="/tmp/t_current",
        claim_lock=None,
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )

    prompt = kcc.build_worker_prompt(task, "/tmp/t_current", board="default")

    assert "Task id: t_current" in prompt
    assert "Claude Code background daemons can reuse shell environment" in prompt
    assert "$HERMES_KANBAN_TASK" in prompt
    assert "Prefer literal task ids in Kanban commands" in prompt


def test_build_claude_code_env_unsets_anthropic_api_key():
    env = kcc.build_claude_code_env(
        {
            "ANTHROPIC_API_KEY": "placeholder-key",
            "HERMES_KANBAN_TASK": "t_123",
            "PATH": "/usr/bin",
        }
    )

    assert "ANTHROPIC_API_KEY" not in env
    assert env["HERMES_KANBAN_TASK"] == "t_123"
    assert env["PATH"] == "/usr/bin"


def test_build_claude_bg_argv_uses_background_mode_not_print_mode():
    argv = kcc.build_claude_bg_argv(
        prompt="work kanban task t_123",
        command="claude",
        name="kanban:t_123",
        permission_mode="bypassPermissions",
        model="sonnet",
        effort="medium",
    )

    assert argv[:2] == ["claude", "--bg"]
    assert "-p" not in argv
    assert "--print" not in argv
    assert "--bare" not in argv  # would bypass OAuth/keychain login paths
    assert argv[-1] == "work kanban task t_123"
    assert argv[argv.index("--name") + 1] == "kanban:t_123"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


@pytest.mark.parametrize(
    ("kwargs", "forbidden"),
    [
        ({"extra_args": ["--print"]}, "--print"),
        ({"command": "claude --print"}, "--print"),
        ({"command": ["claude", "--bare"]}, "--bare"),
        ({"command": "claude -p"}, "-p"),
    ],
)
def test_build_claude_bg_argv_rejects_print_or_bare_from_any_config_source(kwargs, forbidden):
    with pytest.raises(ValueError, match=forbidden):
        kcc.build_claude_bg_argv(prompt="work kanban task t_123", **kwargs)


def test_claude_code_spawn_records_session_mapping_and_audit_event(
    kanban_home, monkeypatch
):
    launched = {}

    def fake_run(argv, **kwargs):
        launched["argv"] = argv
        launched["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout="backgrounded · ccabc123\nclaude logs ccabc123\n",
            stderr="",
        )

    monkeypatch.setattr(kcc.subprocess, "run", fake_run)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="use claude code", assignee="claude-code")
        task = kb.claim_task(conn, tid)
        assert task is not None
        workspace = kb.resolve_workspace(task)
        session_id = kcc.launch_claude_code_session(
            conn,
            task,
            str(workspace),
            board=None,
            config={"command": "claude", "permission_mode": "bypassPermissions"},
        )
        events = kb.list_events(conn, tid)

    assert session_id == "ccabc123"
    assert launched["argv"][:2] == ["claude", "--bg"]
    assert "-p" not in launched["argv"]
    assert "ANTHROPIC_API_KEY" not in launched["env"]
    assert any(
        e.kind == "claude_code_backgrounded"
        and e.payload
        and e.payload.get("session_id") == "ccabc123"
        for e in events
    )

    map_path = kanban_home / "kanban" / "claude_code_sessions.jsonl"
    assert map_path.exists()
    assert '"task_id": "' + tid + '"' in map_path.read_text()
    assert '"session_id": "ccabc123"' in map_path.read_text()


def test_monitor_blocks_task_when_claude_logs_show_auth_failure(kanban_home, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "--bg" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="backgrounded · authbad\nclaude logs authbad\n",
                stderr="",
            )
        if "logs" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="Claude Code needs authentication. Run /login. 401 Unauthorized\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess argv: {argv}")

    monkeypatch.setattr(kcc.subprocess, "run", fake_run)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="auth failure", assignee="claude-code")
        task = kb.claim_task(conn, tid)
        assert task is not None
        workspace = kb.resolve_workspace(task)

    rc = kcc.monitor_task(
        tid,
        workspace=str(workspace),
        config={"command": "claude", "poll_interval_seconds": 5},
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)

    assert rc == 1
    assert task is not None and task.status == "blocked"
    assert runs and runs[0].outcome == "blocked"
    assert "auth failure" in (runs[0].summary or "").lower()
    assert any("logs" in argv for argv in calls)


def test_monitor_auth_failure_exits_cleanly_when_block_loses_run(
    kanban_home, monkeypatch
):
    calls = []
    block_attempted = {"value": False}
    real_get_task = kb.get_task

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "--bg" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="backgrounded · raced\nclaude logs raced\n",
                stderr="",
            )
        if "logs" in argv:
            return SimpleNamespace(
                returncode=0,
                stdout="Claude Code authentication failed: 401 Unauthorized\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess argv: {argv}")

    def fake_block_task(*args, **kwargs):
        block_attempted["value"] = True
        return False

    def fake_get_task(conn, task_id):
        task = real_get_task(conn, task_id)
        if block_attempted["value"] and task is not None:
            task.status = "done"
        return task

    monkeypatch.setattr(kcc.subprocess, "run", fake_run)
    monkeypatch.setattr(kcc.kb, "block_task", fake_block_task)
    monkeypatch.setattr(kcc.kb, "get_task", fake_get_task)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="auth race", assignee="claude-code")
        task = kb.claim_task(conn, tid)
        assert task is not None
        workspace = kb.resolve_workspace(task)

    rc = kcc.monitor_task(
        tid,
        workspace=str(workspace),
        config={"command": "claude", "poll_interval_seconds": 5},
    )

    assert rc == 0
    assert block_attempted["value"] is True
    assert any("logs" in argv for argv in calls)


def test_dispatch_spawns_configured_claude_code_lane_even_without_profile(
    kanban_home, monkeypatch
):
    from hermes_cli import profiles

    spawned = []

    def fake_claude_spawn(task, workspace, *, board=None):
        spawned.append((task.id, task.assignee, workspace, board))
        return 4242

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    monkeypatch.setattr(kb, "_is_claude_code_lane", lambda assignee: assignee == "claude-code")
    monkeypatch.setattr(kb, "_claude_code_spawn", fake_claude_spawn)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="external lane", assignee="claude-code")
        res = kb.dispatch_once(conn)
        task = kb.get_task(conn, tid)
        assert task is not None

    assert res.spawned and res.spawned[0][0] == tid
    assert spawned and spawned[0][1] == "claude-code"
    assert task.status == "running"
    assert task.worker_pid == 4242


def test_has_spawnable_ready_counts_configured_claude_code_lane(
    kanban_home, monkeypatch
):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    monkeypatch.setattr(kb, "_is_claude_code_lane", lambda assignee: assignee == "claude-code")

    with kb.connect() as conn:
        kb.create_task(conn, title="external lane", assignee="claude-code")
        assert kb.has_spawnable_ready(conn) is True
