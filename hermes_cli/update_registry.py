"""
Hermes-Agent Update Registry
============================

Inventory of every updateable component Hermes-Agent touches — declared
dependencies (Python, Node, Nix), pinned Docker base images, system
binaries the agent invokes at runtime, and self-update of the Hermes
package itself. Each entry is a :class:`UpdateProbe` that answers two
questions:

1. **Can it be updated?** (``updateable`` flag + ``update_method`` hint)
2. **Is there an update available?** (``refresh_command`` — a shell
   snippet that surfaces ``current → latest`` for the entry, suitable
   for paste into a terminal.)

The companion doc at ``docs/update-registry.md`` renders the same
inventory in markdown-table form (mirroring the OpenClaw operator
registry pattern at ``~/.openclaw/workspace/references/update-registry.md``).
When you add a new component the agent touches, register the probe here
AND append a row to the doc.

Why both code and markdown?

* The code is the source of truth: typed, testable, callable from the
  CLI/dashboard, and impossible to drift from the underlying
  ``pyproject.toml`` / ``Dockerfile`` / ``flake.lock`` without a
  reviewed edit.
* The markdown is what an operator opens to see "what does this agent
  depend on?" without launching Python. It mirrors the OpenClaw
  registry so the operator workflow ports across projects.

Scope discipline (mirror of OpenClaw's policy):

* Track anything Hermes-Agent declares or invokes at install/runtime
  AND has a version concept.
* Excludes provider APIs (no versions — the model name IS the version),
  user-side OS packages (operator's machine, not the agent), and the
  agent's plugins (those publish their own registries).
* The list reflects what's declared by the repo, not what's installed
  on any particular host — installed-state queries happen via each
  probe's ``refresh_command``.

Convention parity with OpenClaw:

* Same category names where applicable (``python-core-deps``,
  ``node-deps``, ``docker-base``, ``self``).
* Refresh commands are paste-able shell snippets, not Python callables —
  matches OpenClaw's manual-probe workflow.
* Status enum (``current``/``update_available``/``unknown``/``frozen``)
  matches the OpenClaw symbol set (``✅``/``⚠️``/``?``/``🔒``) — see
  :func:`status_glyph` for the mapping.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


# Categories mirror the OpenClaw update-registry section ordering. Adding
# a new category is allowed; existing categories should not be renamed
# without updating the markdown doc.
KNOWN_CATEGORIES: Tuple[str, ...] = (
    "self",                # The Hermes-Agent package itself
    "python-core-deps",    # pyproject.toml [project.dependencies]
    "python-extras",       # pyproject.toml [project.optional-dependencies]
    "node-deps",           # ui-tui/, web/, root package.json
    "nix-inputs",          # flake.lock inputs
    "docker-base",         # FROM <image> in Dockerfile
    "system-binaries",     # apt-installed binaries inside the image
    "runtime-binaries",    # External binaries the agent shells out to (cua-driver, LSP)
    "browser-assets",      # Playwright browser downloads
)


# Update method enum — mirrors the "how to refresh this row" section in
# OpenClaw's registry. ``manual`` means a human reviews the bump (typical
# for pinned deps); ``automatic`` means a tool can do it without review;
# ``frozen`` means the project policy is "do not bump without a written
# justification" (e.g., the supply-chain-pinned Python core deps).
UPDATE_METHODS: Tuple[str, ...] = (
    "manual",      # Bump version literal + regenerate lockfile
    "automatic",   # Tool can update without review (rare for this project)
    "frozen",      # Policy says don't bump without justification
    "external",    # Operator's package manager (brew/apt/winget)
    "vendor",      # Vendor-bundled; bump via vendor script
)


@dataclass(frozen=True)
class UpdateProbe:
    """Static metadata describing one updateable target.

    Probes are registered once at module-import time via
    :func:`_register_builtin_probes`. The dataclass is frozen so probes
    are safe to share across threads and cache.

    Attributes
    ----------
    name : str
        Stable identifier (e.g. ``"py.openai"``). Used as the registry
        key and in CLI output. Must be unique across all probes.
    category : str
        One of :data:`KNOWN_CATEGORIES`. Groups the probe in the
        markdown doc and CLI listing.
    description : str
        One-line human-readable description of what this entry is.
    declared_in : Optional[str]
        Repository-relative path where the version is declared (e.g.
        ``"pyproject.toml"``, ``"ui-tui/package.json"``, ``"Dockerfile"``).
        ``None`` for entries that live outside the repo (e.g., the
        Hermes package itself, queried from PyPI / git).
    current_version : Optional[str]
        Pinned/declared version as a string. Read from
        :attr:`declared_in` when this probe was registered; refresh by
        re-running ``hermes_cli.update_registry`` after the file
        changes. ``None`` when the entry has no single version literal
        (e.g., the Hermes package version is in ``hermes_cli/__init__.py``;
        the apt packages have no per-formula pin).
    updateable : bool
        ``True`` if the project is willing to bump this entry. ``False``
        means "load-bearing pin — do not change without a written
        justification" (set ``update_method="frozen"`` to surface that
        explicitly).
    update_method : str
        One of :data:`UPDATE_METHODS`. ``"manual"`` is the default for
        most declared deps; ``"frozen"`` for supply-chain-pinned
        entries; ``"external"`` for things the operator's host package
        manager owns.
    refresh_command : str
        Paste-able shell snippet that surfaces "current → latest" for
        this entry. Mirrors the OpenClaw registry's "Refresh command"
        blocks — designed for humans to copy and run, not for the
        registry to execute automatically. Empty string when no
        upstream registry exists to query.
    notes : str
        Free-text notes (constraints, CVE refs, vendor-bundle
        provenance, frozen-pin reason, etc.). Optional but encouraged
        for any ``frozen`` or constrained entry.
    status : Optional[str]
        Optional snapshot glyph override. Use this when ``current_version``
        is ``None`` because the value is runtime-resolved (``?``), not
        because the entry lacks a single version concept (``-``).
    """

    name: str
    category: str
    description: str
    declared_in: Optional[str]
    current_version: Optional[str]
    updateable: bool
    update_method: str
    refresh_command: str
    notes: str = ""
    status: Optional[str] = None

    def status_glyph(self) -> str:
        """Return the OpenClaw-style status glyph for this probe.

        ``🔒`` for frozen entries, ``-`` for entries without a version
        concept (no probe possible), ``?`` otherwise. This is the
        snapshot-time glyph; live update-available state requires
        running :attr:`refresh_command` against an upstream registry,
        which the static dataclass deliberately does not do.
        """
        if self.status is not None:
            return self.status
        if self.update_method == "frozen":
            return "🔒"
        if self.current_version is None:
            return "-"
        return "?"


# ---------------------------------------------------------------------------
# Registry storage + thread-safety
# ---------------------------------------------------------------------------


_probes: Dict[str, UpdateProbe] = {}
_lock = threading.Lock()


def register_probe(probe: UpdateProbe) -> None:
    """Register an update probe.

    Re-registration (same ``name``) overwrites the previous entry and
    logs a debug message — matches the hot-reload tolerance of the
    other ``agent/*_registry.py`` modules.
    """
    if not isinstance(probe, UpdateProbe):
        raise TypeError(
            f"register_probe() expects an UpdateProbe instance, "
            f"got {type(probe).__name__}"
        )
    if not probe.name or not probe.name.strip():
        raise ValueError("UpdateProbe.name must be a non-empty string")
    if probe.category not in KNOWN_CATEGORIES:
        raise ValueError(
            f"UpdateProbe.category {probe.category!r} not in KNOWN_CATEGORIES "
            f"({KNOWN_CATEGORIES!r})"
        )
    if probe.update_method not in UPDATE_METHODS:
        raise ValueError(
            f"UpdateProbe.update_method {probe.update_method!r} not in "
            f"UPDATE_METHODS ({UPDATE_METHODS!r})"
        )
    if probe.status is not None and probe.status not in {
        "✅", "⚠️", "?", "-", "🔒"
    }:
        raise ValueError(
            f"UpdateProbe.status {probe.status!r} is not a supported registry glyph"
        )
    with _lock:
        existing = _probes.get(probe.name)
        _probes[probe.name] = probe
    if existing is not None:
        logger.debug("Update probe %r re-registered", probe.name)


def list_probes(category: Optional[str] = None) -> List[UpdateProbe]:
    """Return registered probes, sorted by (category-order, name).

    When ``category`` is given, only probes in that category are
    returned. Unknown categories return an empty list (so callers can
    iterate :data:`KNOWN_CATEGORIES` defensively).
    """
    with _lock:
        items = list(_probes.values())
    cat_index = {c: i for i, c in enumerate(KNOWN_CATEGORIES)}
    items.sort(key=lambda p: (cat_index.get(p.category, len(cat_index)), p.name))
    if category is None:
        return items
    return [p for p in items if p.category == category]


def get_probe(name: str) -> Optional[UpdateProbe]:
    """Return the probe registered under ``name``, or ``None``."""
    if not isinstance(name, str):
        return None
    with _lock:
        return _probes.get(name.strip())


def _reset_for_tests() -> None:
    """Empty the registry. Test-only; not part of the public API."""
    with _lock:
        _probes.clear()


# ---------------------------------------------------------------------------
# Built-in probes
# ---------------------------------------------------------------------------


# Refresh-command snippets reused across multiple probes. Keeping them
# as module constants makes the markdown doc generator emit a single
# block per category instead of duplicating per row.
_PYPI_PROBE_TEMPLATE = (
    "uv pip show {pkg} 2>/dev/null | grep -E '^Version'; "
    "curl -s https://pypi.org/pypi/{pkg}/json "
    "| python3 -c 'import json,sys; print(\"latest:\", json.load(sys.stdin)[\"info\"][\"version\"])'"
)

_NPM_PROBE_TEMPLATE = (
    "(cd {workdir} && npm ls {pkg} --depth=0 2>/dev/null | grep {pkg}); "
    "npm view {pkg} version"
)

_DOCKER_PROBE_TEMPLATE = (
    "docker pull {image} 2>&1 | tail -5; "
    "echo 'pin in Dockerfile:'; "
    "grep -m1 '{image_match}' Dockerfile"
)


def _register_builtin_probes() -> None:
    """Register the static inventory of Hermes-Agent updateable targets.

    Idempotent — safe to call multiple times. Tests use
    :func:`_reset_for_tests` followed by this to repopulate.
    """

    # -----------------------------------------------------------------
    # self — the Hermes-Agent package
    # -----------------------------------------------------------------
    register_probe(UpdateProbe(
        name="self.hermes-agent",
        category="self",
        description=(
            "Hermes-Agent itself — version literal in hermes_cli/__init__.py, "
            "PyPI release, and origin/main git ref"
        ),
        declared_in="hermes_cli/__init__.py",
        current_version=None,  # Resolved at runtime by hermes_cli.banner
        updateable=True,
        update_method="manual",
        refresh_command="hermes update --check",
        notes=(
            "Self-update path: `hermes update`. `hermes update --check` "
            "prefers upstream/main and falls back to origin/main, matching "
            "hermes_cli/main.py:_cmd_update_check(). The startup banner uses "
            "hermes_cli/banner.py:check_for_updates() (cached for 6h)."
        ),
        status="?",
    ))

    # -----------------------------------------------------------------
    # python-core-deps — pyproject.toml [project.dependencies]
    # -----------------------------------------------------------------
    # Each entry is exact-pinned per the supply-chain policy added
    # 2026-05-12 in response to the mistralai 2.4.6 worm. Updating a
    # core dep requires editing pyproject.toml + regenerating uv.lock.
    _CORE_DEPS = [
        ("openai", "2.24.0", "OpenAI SDK (provider= openai, openrouter, custom aggregators)"),
        ("python-dotenv", "1.2.2", ".env file loader"),
        ("fire", "0.7.1", "Google Fire — CLI argument parser"),
        ("httpx", "0.28.1", "HTTP client (with SOCKS extras)"),
        ("rich", "14.3.3", "Terminal formatting"),
        ("tenacity", "9.1.4", "Retry library"),
        ("pyyaml", "6.0.3", "YAML parser"),
        ("ruamel.yaml", "0.18.17", "Round-trip YAML editor"),
        ("requests", "2.33.0", "HTTP client (CVE-2026-25645 fix)"),
        ("jinja2", "3.1.6", "Template engine"),
        ("pydantic", "2.13.4", "Data validation"),
        ("prompt_toolkit", "3.0.52", "Interactive CLI input (used directly by cli.py)"),
        ("croniter", "6.0.0", "Cron expression parser (built-in scheduler)"),
        ("psycopg", "3.3.4", "PostgreSQL driver (binary+pool extras; Kanban Postgres backend)"),
        ("PyJWT", "2.12.1", "JWT signing (Skills Hub GitHub App; CVE-2026-32597 fix)"),
        ("tzdata", "2025.3", "Windows IANA timezone data (Windows-only conditional)"),
        ("psutil", "7.2.2", "Cross-platform process management"),
    ]
    for pkg, ver, desc in _CORE_DEPS:
        register_probe(UpdateProbe(
            name=f"py.{pkg}",
            category="python-core-deps",
            description=desc,
            declared_in="pyproject.toml",
            current_version=ver,
            updateable=True,
            update_method="frozen",
            refresh_command=_PYPI_PROBE_TEMPLATE.format(pkg=pkg),
            notes=(
                "Exact-pinned per pyproject.toml supply-chain policy (no ranges). "
                "Bump via: edit pyproject.toml, then `uv lock`."
            ),
        ))

    # -----------------------------------------------------------------
    # python-extras — pyproject.toml [project.optional-dependencies]
    # -----------------------------------------------------------------
    # One probe per extra (group), not per package — matches how the
    # extras are organized in pyproject.toml and how users install them
    # (`pip install hermes-agent[anthropic]`).
    _EXTRAS = [
        (
            "anthropic",
            "pyproject: anthropic==0.86.0; lazy: anthropic==0.87.0",
            "Native Anthropic SDK (CVE-2026-34450/34452 watch)",
        ),
        ("exa", "exa-py==2.10.2", "Exa web search backend"),
        ("firecrawl", "firecrawl-py==4.17.0", "Firecrawl web search backend"),
        ("parallel-web", "parallel-web==0.4.2", "Parallel web search backend"),
        ("fal", "fal-client==0.13.1", "fal.ai image generation"),
        ("edge-tts", "edge-tts==7.2.7", "Edge TTS backend"),
        ("modal", "modal==1.3.4", "Modal terminal sandbox"),
        ("daytona", "daytona==0.155.0", "Daytona terminal sandbox"),
        ("wecom", "defusedxml==0.7.1", "WeCom (Enterprise WeChat) callback-mode adapter"),
        ("hindsight", "hindsight-client==0.6.1", "Hindsight memory backend"),
        ("dev", "debugpy==1.8.20, pytest==9.0.2, pytest-asyncio==1.3.0, pytest-xdist==3.8.0, pytest-split==0.11.0, mcp==1.26.0, ty==0.0.21, ruff==0.15.10", "Dev/test toolchain"),
        ("messaging", "pyproject: python-telegram-bot[webhooks]==22.6, discord.py[voice]==2.7.1, aiohttp==3.13.3, brotlicffi==1.2.0.1, slack-bolt==1.27.0, slack-sdk==3.40.1, qrcode==7.4.2; lazy platforms: telegram python-telegram-bot[webhooks]==22.6; discord discord.py[voice]==2.7.1 + brotlicffi==1.2.0.1; slack slack-bolt==1.27.0 + slack-sdk==3.40.1 + aiohttp==3.13.4", "Messaging adapters (composite)"),
        ("cron", "(empty — back-compat)", "Cron extra kept for back-compat (croniter is core)"),
        (
            "slack",
            "pyproject: slack-bolt==1.27.0, slack-sdk==3.40.1, aiohttp==3.13.3; lazy: slack-bolt==1.27.0, slack-sdk==3.40.1, aiohttp==3.13.4",
            "Slack platform",
        ),
        ("matrix", "mautrix[encryption]==0.21.0, Markdown==3.10.2, aiosqlite==0.22.1, asyncpg==0.31.0, aiohttp-socks==0.11.0", "Matrix platform (encryption-enabled)"),
        ("cli", "simple-term-menu==1.6.6", "Interactive terminal menu"),
        ("tts-premium", "elevenlabs==1.59.0", "ElevenLabs TTS"),
        ("voice", "faster-whisper==1.2.1, sounddevice==0.5.5, numpy==2.4.3", "Local STT (wheel-only transitive deps)"),
        ("pty", "ptyprocess==0.7.0; pywinpty==2.0.15", "Platform-conditional PTY"),
        ("honcho", "honcho-ai==2.0.1", "Honcho memory backend"),
        ("mcp", "mcp==1.26.0", "MCP client/server"),
        ("homeassistant", "aiohttp==3.13.3", "Home Assistant gateway"),
        ("sms", "aiohttp==3.13.3", "SMS gateway"),
        ("computer-use", "mcp==1.26.0", "Computer-use MCP client (talks to cua-driver)"),
        ("acp", "agent-client-protocol==0.9.0", "Agent Client Protocol (VS Code / Zed / JetBrains)"),
        ("bedrock", "boto3==1.42.89", "AWS Bedrock provider"),
        ("azure-identity", "azure-identity==1.25.3", "Microsoft Foundry Entra ID auth"),
        ("termux", "(composite extra)", "Android/Termux baseline"),
        ("termux-all", "(composite extra)", "Android/Termux full"),
        ("dingtalk", "dingtalk-stream==0.24.3, alibabacloud-dingtalk==2.2.42, qrcode==7.4.2", "DingTalk platform"),
        ("feishu", "lark-oapi==1.5.3, qrcode==7.4.2", "Feishu platform"),
        ("google", "google-api-python-client==2.194.0, google-auth-oauthlib==1.3.1, google-auth-httplib2==0.3.1", "Google Workspace skill"),
        ("youtube", "youtube-transcript-api==1.2.4", "YouTube transcript fetch (media skills)"),
        ("web", "fastapi==0.133.1, uvicorn[standard]==0.41.0", "Dashboard SPA + API"),
        ("all", "(composite of safe extras)", "Composite extra excluding lazy-installable backends"),
    ]
    _LAZY_BACKED_EXTRAS = {
        "anthropic",
        "exa",
        "firecrawl",
        "parallel-web",
        "fal",
        "edge-tts",
        "modal",
        "daytona",
        "wecom",
        "hindsight",
        "messaging",
        "slack",
        "matrix",
        "tts-premium",
        "voice",
        "honcho",
        "acp",
        "bedrock",
        "azure-identity",
        "dingtalk",
        "feishu",
        "google",
        "youtube",
        "web",
    }
    for extra, specs, desc in _EXTRAS:
        register_probe(UpdateProbe(
            name=f"py-extra.{extra}",
            category="python-extras",
            description=f"{desc} — pip extras `[{extra}]`: {specs}",
            declared_in=(
                "pyproject.toml + tools/lazy_deps.py"
                if extra in _LAZY_BACKED_EXTRAS
                else "pyproject.toml"
            ),
            current_version=None,  # extra is a group, not a single version
            updateable=True,
            update_method="frozen",
            refresh_command=(
                "python3 -c \"import tomllib, sys; "
                f"d=tomllib.load(open('pyproject.toml','rb')); "
                f"print(d['project']['optional-dependencies'].get('{extra}', []))\"; "
                "echo 'check each pinned package via PyPI:'; "
                f"echo '  curl -s https://pypi.org/pypi/<pkg>/json | python3 -c \"import json,sys; print(json.load(sys.stdin)[chr(34)+\"info\"+chr(34)])\"'"
            ),
            notes=(
                "mistral extra REMOVED 2026-05-12 (PyPI quarantine); see "
                "pyproject.toml comment for restoration checklist. Each "
                "pinned package in the extra follows the same no-ranges policy."
            ),
            status="-" if extra in {"cron", "termux", "termux-all", "all"} else None,
        ))

    # -----------------------------------------------------------------
    # node-deps — package.json (root, web/, ui-tui/)
    # -----------------------------------------------------------------
    register_probe(UpdateProbe(
        name="node.root",
        category="node-deps",
        description="Root npm package.json — agent-browser CLI",
        declared_in="package.json",
        current_version="agent-browser ^0.26.0",
        updateable=True,
        update_method="manual",
        refresh_command="npm outdated --prefix . --depth=0; npm view agent-browser version",
        notes="Used by browser tool subsystem; lodash override pinned to 4.18.1.",
    ))
    register_probe(UpdateProbe(
        name="node.web",
        category="node-deps",
        description="Dashboard SPA (web/) — React, Vite, xterm, Tailwind, etc.",
        declared_in="web/package.json",
        current_version=None,  # many packages
        updateable=True,
        update_method="manual",
        refresh_command="(cd web && npm outdated --depth=0)",
        notes=(
            "Refreshes when the dashboard frontend bumps. Built via `npm run build` "
            "in CI/Dockerfile. Lock at web/package-lock.json."
        ),
        status="?",
    ))
    register_probe(UpdateProbe(
        name="node.ui-tui",
        category="node-deps",
        description=(
            "Terminal UI (ui-tui/) — Ink/React/tsx + @hermes/ink workspace package"
        ),
        declared_in="ui-tui/package.json",
        current_version=None,
        updateable=True,
        update_method="manual",
        refresh_command="(cd ui-tui && npm outdated --depth=0)",
        notes=(
            "ui-tui/packages/hermes-ink is a file: workspace dependency. "
            "Lock at ui-tui/package-lock.json. The Dockerfile sets "
            "npm_config_install_links=false to force symlink-mode on older "
            "Debian-bundled npm 9."
        ),
        status="?",
    ))

    # -----------------------------------------------------------------
    # nix-inputs — flake.lock pins
    # -----------------------------------------------------------------
    _NIX_INPUTS = [
        ("nixpkgs", "nixos-unstable", "Nix package repository (unstable channel)"),
        ("flake-parts", "main", "Flake-parts framework (hercules-ci/flake-parts)"),
        ("pyproject-nix", "main", "pyproject.nix — pyproject.toml -> Nix bridge"),
        ("uv2nix", "main", "uv2nix — uv.lock -> Nix bridge"),
        ("pyproject-build-systems", "main", "pyproject-build-systems — wheel build deps"),
        ("npm-lockfile-fix", "main", "npm-lockfile-fix — lockfile normalization"),
    ]
    for name, ref, desc in _NIX_INPUTS:
        register_probe(UpdateProbe(
            name=f"nix.{name}",
            category="nix-inputs",
            description=desc,
            declared_in="flake.nix",
            current_version=f"flake input '{name}' @ {ref}",
            updateable=True,
            update_method="automatic",
            refresh_command=(
                "tmp=$(mktemp -d); trap 'rm -rf \"$tmp\"' EXIT; "
                "cp flake.nix flake.lock \"$tmp\"/ && "
                f"(cd \"$tmp\" && nix flake lock --update-input {name} --quiet >/dev/null) && "
                "git diff --no-index -- flake.lock \"$tmp/flake.lock\" >/dev/null; "
                "case $? in 0) echo current;; 1) echo update_available;; *) echo probe_failed;; esac"
            ),
            notes=(
                "Refresh via `nix flake update` (all inputs) or "
                "`nix flake lock --update-input <name>` (one). Pinned rev "
                "in flake.lock."
            ),
        ))

    # -----------------------------------------------------------------
    # docker-base — Dockerfile FROM lines
    # -----------------------------------------------------------------
    register_probe(UpdateProbe(
        name="docker.uv-source",
        category="docker-base",
        description="ghcr.io/astral-sh/uv multi-stage source image (uv binary)",
        declared_in="Dockerfile",
        current_version="0.11.6-python3.13-trixie",
        updateable=True,
        update_method="manual",
        refresh_command=_DOCKER_PROBE_TEMPLATE.format(
            image="ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie",
            image_match="ghcr.io/astral-sh/uv",
        ),
        notes=(
            "Pinned by digest (@sha256:...). Bump strategy: pick a new "
            "0.11.x or 0.12.x tag, refresh the digest pin, regenerate "
            "uv.lock to match the new uv resolver."
        ),
    ))
    register_probe(UpdateProbe(
        name="docker.node-source",
        category="docker-base",
        description="node:22-bookworm-slim multi-stage source image (node/npm/corepack)",
        declared_in="Dockerfile",
        current_version="22-bookworm-slim",
        updateable=True,
        update_method="manual",
        refresh_command=_DOCKER_PROBE_TEMPLATE.format(
            image="node:22-bookworm-slim",
            image_match="node:",
        ),
        notes=(
            "Pinned by digest. node/npm/corepack are COPYed from this stage "
            "into the debian:13.4 runtime (see the Dockerfile node_source "
            "stage). Bump strategy: pick a newer node LTS tag + refresh digest."
        ),
    ))
    register_probe(UpdateProbe(
        name="docker.debian",
        category="docker-base",
        description="debian:13.4 — Hermes runtime image base",
        declared_in="Dockerfile",
        current_version="13.4",
        updateable=True,
        update_method="manual",
        refresh_command=_DOCKER_PROBE_TEMPLATE.format(
            image="debian:13.4",
            image_match="debian:",
        ),
        notes=(
            "Tag-pinned (not digest-pinned). Bump strategy: watch Debian "
            "point releases; tag refreshes pick up security patches."
        ),
    ))

    # -----------------------------------------------------------------
    # system-binaries — apt packages installed inside the runtime image
    # -----------------------------------------------------------------
    # Versions follow the debian:13.4 base — refresh by rebuilding the
    # image. Listed as a single group probe since they share a refresh
    # path.
    register_probe(UpdateProbe(
        name="apt.runtime-toolchain",
        category="system-binaries",
        description=(
            "apt packages in the runtime image: build-essential, curl, "
            "nodejs, npm, python3, ripgrep, ffmpeg, gcc, python3-dev, "
            "libffi-dev, procps, git, openssh-client, docker-cli, tini"
        ),
        declared_in="Dockerfile",
        current_version=None,
        updateable=True,
        update_method="external",
        refresh_command=(
            "docker run --rm debian:13.4 apt list --installed 2>/dev/null "
            "| grep -E '^(build-essential|curl|nodejs|npm|python3|ripgrep|"
            "ffmpeg|gcc|python3-dev|libffi-dev|procps|git|openssh-client|"
            "docker-cli|tini)/'"
        ),
        notes=(
            "Bumped by debian:13.4 base bumps. tini reaps zombie children "
            "(see #15012). docker-cli is needed for the docker-in-docker "
            "terminal backend."
        ),
    ))

    # -----------------------------------------------------------------
    # runtime-binaries — external tools the agent invokes (not bundled)
    # -----------------------------------------------------------------
    register_probe(UpdateProbe(
        name="runtime.cua-driver",
        category="runtime-binaries",
        description=(
            "cua-driver — macOS computer-use binary (installed via curl "
            "script by `hermes tools`; talked to over MCP stdio)"
        ),
        declared_in=None,
        current_version=None,
        updateable=True,
        update_method="external",
        refresh_command="cua-driver --version 2>/dev/null || echo 'cua-driver not installed'",
        notes=(
            "Not bundled with hermes-agent — `hermes tools` installs it "
            "post-setup. Pyproject `computer-use` extra pins the MCP "
            "client, not the driver binary."
        ),
        status="?",
    ))
    register_probe(UpdateProbe(
        name="runtime.lsp-servers",
        category="runtime-binaries",
        description=(
            "LSP server binaries auto-installed under $HERMES_HOME/lsp/bin/ "
            "(typescript-language-server, pyright, gopls, etc.)"
        ),
        declared_in="agent/lsp/install.py",
        current_version=None,
        updateable=True,
        update_method="automatic",
        refresh_command=(
            "hermes lsp status 2>/dev/null || "
            "ls -la \"${HERMES_HOME:-$HOME/.hermes}/lsp/bin/\" 2>/dev/null"
        ),
        notes=(
            "Auto-install strategy controlled by config "
            "`lsp.install_strategy` (auto/manual/off). Per-package "
            "recipes live in agent/lsp/install.py INSTALL_RECIPES."
        ),
        status="?",
    ))

    # -----------------------------------------------------------------
    # browser-assets — Playwright browser downloads
    # -----------------------------------------------------------------
    register_probe(UpdateProbe(
        name="playwright.chromium",
        category="browser-assets",
        description=(
            "Playwright chromium (headless shell) — installed at "
            "$PLAYWRIGHT_BROWSERS_PATH or /opt/hermes/.playwright in the image"
        ),
        declared_in="Dockerfile",
        current_version=None,
        updateable=True,
        update_method="automatic",
        refresh_command=(
            "npx playwright --version && "
            "PLAYWRIGHT_BROWSERS_PATH=\"${PLAYWRIGHT_BROWSERS_PATH:-/opt/hermes/.playwright}\" "
            "npx playwright install --dry-run chromium"
        ),
        notes=(
            "Dockerfile runs `npx playwright install --with-deps chromium "
            "--only-shell` once per build. Refreshes when ui-tui/package.json "
            "or web/package.json bumps the playwright peer."
        ),
        status="?",
    ))


# Register at module-import time so consumers (CLI, dashboard, tests via
# the explicit reset hook) see a populated registry without extra setup.
_register_builtin_probes()


# ---------------------------------------------------------------------------
# Markdown rendering — keeps the doc in sync with the code
# ---------------------------------------------------------------------------


def render_markdown_table(category: str) -> str:
    """Render the probes in ``category`` as a markdown table.

    Used by ``docs/update-registry.md`` to keep the operator-facing
    inventory in sync with the code. The table columns mirror OpenClaw's
    update-registry schema (Component | Current | Status | Update method
    | Refresh).
    """
    rows = list_probes(category=category)
    if not rows:
        return f"_(no probes registered in category `{category}`)_\n"
    header = (
        "| Probe | Current | Status | Update method | Declared in |\n"
        "|---|---|---|---|---|\n"
    )
    body_lines = []
    for p in rows:
        current = p.current_version or "—"
        declared = p.declared_in or "—"
        body_lines.append(
            f"| `{p.name}` | {current} | {p.status_glyph()} | "
            f"{p.update_method} | `{declared}` |"
        )
    return header + "\n".join(body_lines) + "\n"
