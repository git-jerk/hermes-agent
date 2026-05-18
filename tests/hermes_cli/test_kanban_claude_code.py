"""Claude Code background-lane tests for Hermes Kanban."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_claude_code as kcc


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_parse_claude_bg_session_id_from_current_cli_output():
    output = """Starting background service…
backgrounded · 0f40b52b
  claude agents             list sessions
  claude attach 0f40b52b    open in this terminal
"""

    assert kcc.parse_claude_bg_session_id(output) == "0f40b52b"


def test_build_claude_code_env_unsets_anthropic_api_key():
    env = kcc.build_claude_code_env(
        {
            "ANTHROPIC_API_KEY": "sk-test-secret",
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
