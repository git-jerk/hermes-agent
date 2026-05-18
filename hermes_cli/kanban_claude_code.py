"""Claude Code background-session lane for Hermes Kanban.

This module deliberately uses Claude Code's official interactive/background
session path (``claude --bg``), not ``claude -p`` / print mode and not the
Hermes Anthropic provider.  That matters for users whose automation should run
through an already-logged-in Claude Code subscription account rather than an
API-key billing lane.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from hermes_cli import kanban_db as kb


DEFAULT_CLAUDE_CODE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "assignees": [],
    "command": "claude",
    "poll_interval_seconds": 60,
    "launch_timeout_seconds": 60,
    # Always remove API-key auth by default. Claude Code should resolve its
    # logged-in account/keychain/OAuth state itself for this lane.
    "unset_env": ["ANTHROPIC_API_KEY"],
    "name_prefix": "kanban",
    "permission_mode": None,
    "model": None,
    "effort": None,
    "extra_args": [],
}

_FORBIDDEN_BG_ARGS = {"-p", "--print", "--bare"}
_AUTH_PRODUCT_RE = re.compile(r"\b(?:claude(?:\s+code)?|anthropic)\b", re.IGNORECASE)
_AUTH_CONTEXT_RE = re.compile(
    r"\b(?:auth(?:entication)?|account|credential|oauth|token|subscription)\b",
    re.IGNORECASE,
)
_EXPLICIT_AUTH_FAILURE_RE = re.compile(
    r"\b(?:auth(?:entication)?\s+(?:failed|required)|needs\s+auth(?:entication)?|"
    r"please\s+(?:run\s+)?/login|run\s+/login|"
    r"please\s+(?:log|sign)\s+in)\b",
    re.IGNORECASE,
)
_AUTH_FAILURE_RE = re.compile(
    r"(?:\b(?:401|unauthori[sz]ed|not\s+logged\s+in)\b|"
    + _EXPLICIT_AUTH_FAILURE_RE.pattern
    + r")",
    re.IGNORECASE,
)
_STANDALONE_LOGIN_PROMPT_RE = re.compile(
    r"^\s*(?:(?:please\s+)?run\s+)?/login(?:\s+first)?\s*[.!:]?\s*$",
    re.IGNORECASE,
)
_BENIGN_TEST_CONTEXT_RE = re.compile(
    r"\b(?:pytest|expects?|expecting|expected|mock|fixture|test\s+server|sdk|client|proxy)\b",
    re.IGNORECASE,
)
_BENIGN_HTTP_ROUTE_CONTEXT_RE = re.compile(
    r"\b(?:GET|POST|PUT|PATCH|DELETE|route|endpoint|demo\s+app|dashboard)\b",
    re.IGNORECASE,
)
_SECRETISH_LOG_VALUE_RE = re.compile(
    r"\b(api[_-]?key|token|credential|secret|password)\b\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SESSION_ID_RE = re.compile(r"\bbackgrounded\s+[·•:\-]\s*([A-Za-z0-9_-]+)\b")
_ATTACH_ID_RE = re.compile(r"\bclaude\s+attach\s+([A-Za-z0-9_-]+)\b")


def get_claude_code_config(config: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
    """Return merged ``kanban.claude_code`` config."""
    if config is None:
        try:
            from hermes_cli.config import load_config

            root = load_config()
        except Exception:
            root = {}
        raw = (root.get("kanban", {}) or {}).get("claude_code", {}) if isinstance(root, dict) else {}
    else:
        raw = dict(config)

    merged = dict(DEFAULT_CLAUDE_CODE_CONFIG)
    if isinstance(raw, Mapping):
        merged.update(dict(raw))
    if merged.get("assignees") is None:
        merged["assignees"] = []
    if merged.get("extra_args") is None:
        merged["extra_args"] = []
    if merged.get("unset_env") is None:
        merged["unset_env"] = ["ANTHROPIC_API_KEY"]
    return merged


def configured_assignees(config: Optional[Mapping[str, Any]] = None) -> set[str]:
    """Return assignee names configured for the Claude Code lane.

    ``assignees`` may be a list/tuple/set or a dict keyed by lane name.  The
    dict shape leaves room for future per-lane overrides without changing the
    membership check used by the dispatcher.
    """
    cfg = get_claude_code_config(config)
    if not bool(cfg.get("enabled", False)):
        return set()
    raw = cfg.get("assignees") or []
    if isinstance(raw, Mapping):
        names = raw.keys()
    elif isinstance(raw, str):
        names = [raw]
    else:
        names = raw
    return {str(name).strip() for name in names if str(name).strip()}


def is_configured_lane(assignee: Optional[str], config: Optional[Mapping[str, Any]] = None) -> bool:
    """Return True when ``assignee`` should spawn through Claude Code."""
    if not assignee:
        return False
    return str(assignee).strip() in configured_assignees(config)


def parse_claude_bg_session_id(output: str) -> str:
    """Extract the session id printed by ``claude --bg``.

    Current Claude Code prints a line like ``backgrounded · 0f40b52b`` and then
    helper commands such as ``claude attach 0f40b52b``.  Support both so minor
    CLI copy changes don't strand the adapter.
    """
    text = output or ""
    match = _SESSION_ID_RE.search(text) or _ATTACH_ID_RE.search(text)
    if not match:
        raise ValueError("Claude Code did not print a background session id")
    return match.group(1)


def _sanitize_auth_failure_excerpt(excerpt: str) -> str:
    """Keep auth-failure summaries short and avoid persisting token-shaped text."""
    compact = " ".join((excerpt or "").split())
    compact = _SECRETISH_LOG_VALUE_RE.sub(
        lambda match: f"{match.group(1)}=[redacted]",
        compact,
    )
    if len(compact) > 160:
        compact = compact[:157] + "..."
    return compact


def _is_benign_auth_failure_line(line: str) -> bool:
    """Return True for app/test 401/login mentions that should not block a task."""
    if _EXPLICIT_AUTH_FAILURE_RE.search(line) or _STANDALONE_LOGIN_PROMPT_RE.search(line):
        return False
    if _BENIGN_TEST_CONTEXT_RE.search(line):
        return True
    return bool(_BENIGN_HTTP_ROUTE_CONTEXT_RE.search(line) and not _AUTH_CONTEXT_RE.search(line))


def _line_has_claude_auth_failure(line: str) -> bool:
    """Return True when a single log line is a Claude/Anthropic auth failure."""
    if _STANDALONE_LOGIN_PROMPT_RE.search(line):
        return True
    if not (_AUTH_PRODUCT_RE.search(line) and _AUTH_FAILURE_RE.search(line)):
        return False
    return not _is_benign_auth_failure_line(line)


def _window_has_claude_auth_failure(lines: Sequence[str]) -> bool:
    """Return True when adjacent lines jointly identify Claude auth failure."""
    window = " ".join(lines)
    if not (_AUTH_PRODUCT_RE.search(window) and _AUTH_FAILURE_RE.search(window)):
        return False
    failure_lines = [line for line in lines if _AUTH_FAILURE_RE.search(line)]
    return any(not _is_benign_auth_failure_line(line) for line in failure_lines)


def detect_claude_auth_failure(output: str) -> Optional[str]:
    """Return a concise reason when ``claude logs`` shows auth failure.

    A background session id only proves that Claude Code accepted the launch.
    If the background daemon later renders auth-context ``/login`` or ``401``
    messages in its own logs, the worker cannot make progress and the kanban
    card should be blocked instead of emitting healthy heartbeats forever.
    """
    lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        if _line_has_claude_auth_failure(line):
            matched = _sanitize_auth_failure_excerpt(line)
            return f"Claude Code background auth failure detected in logs: {matched}"

        # Claude logs sometimes split the product/auth context and the concrete
        # failure across adjacent lines. Check a small local window instead of
        # the whole output so app/test output elsewhere cannot poison the match.
        start = max(0, idx - 1)
        end = min(len(lines), idx + 2)
        window_lines = lines[start:end]
        if _window_has_claude_auth_failure(window_lines):
            matched = _sanitize_auth_failure_excerpt(" ".join(window_lines))
            return f"Claude Code background auth failure detected in logs: {matched}"
    return None


def _reject_forbidden_bg_args(parts: Sequence[str]) -> None:
    forbidden = [arg for arg in parts if arg in _FORBIDDEN_BG_ARGS]
    if forbidden:
        raise ValueError(
            "Claude Code kanban lane forbids print/bare mode arguments: "
            + ", ".join(sorted(set(forbidden)))
        )


def _command_parts(command: Any) -> list[str]:
    if isinstance(command, (list, tuple)):
        parts = [str(p) for p in command if str(p)]
    else:
        parts = shlex.split(str(command or "claude"))
    return parts or ["claude"]


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def build_claude_code_env(
    base_env: Optional[Mapping[str, str]] = None,
    *,
    unset_env: Optional[Sequence[str]] = None,
) -> dict[str, str]:
    """Return environment for Claude Code with API-key auth scrubbed."""
    env = dict(base_env or os.environ)
    keys = {"ANTHROPIC_API_KEY"}
    if unset_env is not None:
        keys.update(str(k) for k in unset_env if str(k))
    for key in keys:
        env.pop(key, None)
    return env


def build_claude_bg_argv(
    *,
    prompt: str,
    command: Any = "claude",
    name: Optional[str] = None,
    permission_mode: Optional[str] = None,
    model: Optional[str] = None,
    effort: Optional[str] = None,
    extra_args: Optional[Sequence[str]] = None,
) -> list[str]:
    """Build a safe official Claude Code background-session argv."""
    command_parts = _command_parts(command)
    extras = _as_list(extra_args)
    _reject_forbidden_bg_args([*command_parts, *extras])

    argv = [*command_parts, "--bg"]
    if name:
        argv.extend(["--name", str(name)])
    if permission_mode:
        argv.extend(["--permission-mode", str(permission_mode)])
    if model:
        argv.extend(["--model", str(model)])
    if effort:
        argv.extend(["--effort", str(effort)])

    argv.extend(extras)
    argv.append(prompt)
    return argv


def build_worker_prompt(task: kb.Task, workspace: str, *, board: Optional[str] = None) -> str:
    """Prompt handed to the Claude Code background session."""
    board_slug = board or os.environ.get("HERMES_KANBAN_BOARD") or kb.get_current_board()
    run_id = task.current_run_id
    run_line = f"Active run id: {run_id}" if run_id is not None else "Active run id: unknown"
    return f"""You are a Claude Code background worker for Hermes Kanban task {task.id}.

This is an automated background session launched with `claude --bg` so it uses the logged-in Claude Code account/session. Do not switch to `claude -p`, `--print`, `--bare`, or any direct Anthropic API-key path.

Kanban context:
- Task id: {task.id}
- Assignee/lane: {task.assignee or '(none)'}
- Board: {board_slug}
- Workspace: {workspace}
- {run_line}

Required workflow:
1. First inspect the card with `hermes kanban --board {board_slug} show {task.id} --json`.
2. Work only inside the task workspace unless the card explicitly authorizes a broader path.
3. Treat the Kanban context above as authoritative. Claude Code background daemons can reuse shell environment from an earlier background session; do not rely on `$HERMES_KANBAN_TASK`, `$HERMES_KANBAN_BOARD`, or `$HERMES_KANBAN_WORKSPACE` unless they exactly match the Task id / Board / Workspace shown above. Prefer literal task ids in Kanban commands.
4. For long work, leave progress with `hermes kanban --board {board_slug} comment {task.id} "..."`.
5. End this run with exactly one terminal Kanban transition:
   - success: `hermes kanban --board {board_slug} complete {task.id} --summary "..." --metadata '{{"changed_files": [], "tests": []}}'`
   - needs human input/review: `hermes kanban --board {board_slug} block {task.id} "review-required: ..."`
6. Do not finish the Claude Code session while the task remains `running`; block it if you cannot complete it safely.
"""


def _task_env(task: kb.Task, workspace: str, *, board: Optional[str], config: Mapping[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    if task.tenant:
        env["HERMES_TENANT"] = task.tenant
    env["HERMES_KANBAN_TASK"] = task.id
    env["HERMES_KANBAN_WORKSPACE"] = workspace
    if task.current_run_id is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if task.claim_lock:
        env["HERMES_KANBAN_CLAIM_LOCK"] = task.claim_lock
    env["HERMES_KANBAN_DB"] = str(kb.kanban_db_path(board=board))
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(kb.workspaces_root(board=board))
    env["HERMES_KANBAN_BOARD"] = board or kb.get_current_board()
    env["HERMES_PROFILE"] = task.assignee or "claude-code"
    return build_claude_code_env(env, unset_env=_as_list(config.get("unset_env")))


def session_map_path(board: Optional[str] = None) -> Path:
    """Return the JSONL task→Claude-session map path for a board."""
    slug = board or kb.get_current_board()
    if slug == kb.DEFAULT_BOARD:
        return kb.kanban_home() / "kanban" / "claude_code_sessions.jsonl"
    return kb.board_dir(slug) / "claude_code_sessions.jsonl"


def _append_session_mapping(
    *,
    task: kb.Task,
    session_id: str,
    board: Optional[str],
    log_path: Optional[Path],
) -> Path:
    path = session_map_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "task_id": task.id,
        "run_id": task.current_run_id,
        "assignee": task.assignee,
        "board": board or kb.get_current_board(),
        "session_id": session_id,
        "workspace": task.workspace_path,
        "log_path": str(log_path) if log_path else None,
        "created_at": int(time.time()),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def launch_claude_code_session(
    conn,
    task: kb.Task,
    workspace: str,
    *,
    board: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
    log_path: Optional[Path] = None,
) -> str:
    """Launch ``claude --bg`` for a claimed task and record its session id."""
    cfg = get_claude_code_config(config)
    prompt = build_worker_prompt(task, workspace, board=board)
    name_prefix = str(cfg.get("name_prefix") or "kanban")
    argv = build_claude_bg_argv(
        prompt=prompt,
        command=cfg.get("command") or "claude",
        name=f"{name_prefix}:{task.id}",
        permission_mode=cfg.get("permission_mode"),
        model=cfg.get("model"),
        effort=cfg.get("effort"),
        extra_args=cfg.get("extra_args"),
    )
    env = _task_env(task, workspace, board=board, config=cfg)
    cwd = workspace if os.path.isdir(workspace) else None
    try:
        proc = subprocess.run(  # noqa: S603 -- argv list, no shell
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            timeout=float(cfg.get("launch_timeout_seconds") or 60),
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "`claude` executable not found on PATH. Install Claude Code and run `claude /login` before enabling kanban.claude_code."
        ) from exc

    output = (proc.stdout or "") + (proc.stderr or "")
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab") as f:
                f.write(b"\n--- claude-code launch ---\n")
                f.write(output.encode("utf-8", errors="replace"))
                if not output.endswith("\n"):
                    f.write(b"\n")
        except OSError:
            pass

    if proc.returncode != 0:
        excerpt = output[-2000:].strip()
        raise RuntimeError(f"claude --bg failed with exit code {proc.returncode}: {excerpt}")

    session_id = parse_claude_bg_session_id(output)
    map_path = _append_session_mapping(
        task=task,
        session_id=session_id,
        board=board,
        log_path=log_path,
    )
    with kb.write_txn(conn):
        kb._append_event(  # type: ignore[attr-defined]
            conn,
            task.id,
            "claude_code_backgrounded",
            {
                "session_id": session_id,
                "map_path": str(map_path),
                "workspace": workspace,
                "command": _command_parts(cfg.get("command") or "claude")[0],
            },
            run_id=task.current_run_id,
        )
    return session_id


def build_claude_logs_argv(session_id: str, *, command: Any = "claude") -> list[str]:
    return [*_command_parts(command), "logs", session_id]


def build_claude_stop_argv(session_id: str, *, command: Any = "claude") -> list[str]:
    return [*_command_parts(command), "stop", session_id]


def monitor_task(
    task_id: str,
    *,
    workspace: str,
    board: Optional[str] = None,
    config: Optional[Mapping[str, Any]] = None,
) -> int:
    """Monitor wrapper process used by the dispatcher.

    The wrapper keeps a host-local PID alive for the kanban kernel while the
    actual Claude Code work happens in a Claude-managed background session.
    The Claude session must still complete/block the card via the kanban CLI;
    the monitor exits once the DB status is no longer ``running``.
    """
    cfg = get_claude_code_config(config)
    poll_interval = max(5.0, float(cfg.get("poll_interval_seconds") or 60))
    log_path = kb.worker_logs_dir(board=board) / f"{task_id}.log"
    session_id: Optional[str] = None
    stop_requested = False

    def _stop_session() -> None:
        if not session_id:
            return
        try:
            subprocess.run(  # noqa: S603 -- argv list, no shell
                build_claude_stop_argv(session_id, command=cfg.get("command") or "claude"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=20,
                check=False,
                env=build_claude_code_env(os.environ, unset_env=_as_list(cfg.get("unset_env"))),
            )
        except Exception:
            pass

    def _handle_signal(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True
        _stop_session()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_signal)
            except (ValueError, OSError):
                pass

    with contextlib.closing(kb.connect(board=board)) as conn:
        task = kb.get_task(conn, task_id)
        if task is None:
            print(f"kanban claude-code monitor: unknown task {task_id}", file=sys.stderr)
            return 2
        expected_run_id = task.current_run_id
        session_id = launch_claude_code_session(
            conn,
            task,
            workspace,
            board=board,
            config=cfg,
            log_path=log_path,
        )
        print(f"kanban claude-code monitor: launched {session_id} for {task_id}", flush=True)

    last_logs = ""
    while not stop_requested:
        with contextlib.closing(kb.connect(board=board)) as conn:
            task = kb.get_task(conn, task_id)
            if task is None or task.status != "running":
                return 0
            if expected_run_id is not None and task.current_run_id != expected_run_id:
                return 0
            kb.heartbeat_worker(
                conn,
                task_id,
                note=f"claude-code background session {session_id}",
                expected_run_id=expected_run_id,
            )

        try:
            logs = subprocess.run(  # noqa: S603 -- argv list, no shell
                build_claude_logs_argv(session_id, command=cfg.get("command") or "claude"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=30,
                check=False,
                env=build_claude_code_env(os.environ, unset_env=_as_list(cfg.get("unset_env"))),
            )
            if logs.stdout and logs.stdout != last_logs:
                last_logs = logs.stdout
                try:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("ab") as f:
                        f.write(b"\n--- claude logs snapshot ---\n")
                        f.write(logs.stdout.encode("utf-8", errors="replace"))
                        if not logs.stdout.endswith("\n"):
                            f.write(b"\n")
                except OSError:
                    pass
                auth_failure = detect_claude_auth_failure(logs.stdout)
                if auth_failure:
                    reason = f"review-required: {auth_failure}"
                    current: Optional[kb.Task] = None
                    with contextlib.closing(kb.connect(board=board)) as conn:
                        blocked = kb.block_task(
                            conn,
                            task_id,
                            reason=reason,
                            expected_run_id=expected_run_id,
                        )
                        if not blocked:
                            current = kb.get_task(conn, task_id)
                    if blocked:
                        print(
                            f"kanban claude-code monitor: blocked {task_id}: {auth_failure}",
                            file=sys.stderr,
                            flush=True,
                        )
                        return 1
                    if (
                        current is None
                        or current.status != "running"
                        or (
                            expected_run_id is not None
                            and current.current_run_id != expected_run_id
                        )
                    ):
                        return 0
                    print(
                        f"kanban claude-code monitor: auth failure seen but could not block {task_id}: {auth_failure}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return 1
        except Exception as exc:
            print(f"kanban claude-code monitor: logs poll failed: {exc}", file=sys.stderr, flush=True)

        time.sleep(poll_interval)

    return 130


def spawn_monitor(task: kb.Task, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Spawn the long-lived monitor process and return its PID."""
    cfg = get_claude_code_config()
    log_dir = kb.worker_logs_dir(board=board)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{task.id}.log"
    kb._rotate_worker_log(log_path, kb.DEFAULT_LOG_ROTATE_BYTES)  # type: ignore[attr-defined]

    cmd = [
        sys.executable,
        "-m",
        "hermes_cli.kanban_claude_code",
        "monitor",
        task.id,
        "--workspace",
        workspace,
    ]
    if board:
        cmd.extend(["--board", board])

    env = _task_env(task, workspace, board=board, config=cfg)
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(  # noqa: S603 -- argv list, no shell
            cmd,
            cwd=workspace if os.path.isdir(workspace) else None,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    except Exception:
        log_f.close()
        raise
    return proc.pid


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes Kanban Claude Code worker lane")
    sub = parser.add_subparsers(dest="command", required=True)
    p_mon = sub.add_parser("monitor", help="launch and monitor a Claude Code background session")
    p_mon.add_argument("task_id")
    p_mon.add_argument("--workspace", required=True)
    p_mon.add_argument("--board", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.command == "monitor":
        return monitor_task(args.task_id, workspace=args.workspace, board=args.board)
    parser.error("unknown command")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
