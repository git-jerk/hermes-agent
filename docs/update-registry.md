---
title: Update Registry — Hermes-Agent
description: Inventory of every updateable dependency, resource, and binary Hermes-Agent touches. Each row reports whether the entry is updateable and how to check whether an update is available.
last_updated: 2026-05-18 PT
last_updater: feat/update-registry initial commit
update_cadence: re-run probes before each release tag OR weekly during active development
mirror_of: ~/.openclaw/workspace/references/update-registry.md
---

# Update Registry — Hermes-Agent

Canonical inventory of every updateable thing Hermes-Agent depends on or
invokes. Mirrors the OpenClaw operator update-registry pattern
(`~/.openclaw/workspace/references/update-registry.md`) so the workflow
ports between projects.

**Source of truth.** The probes are defined in code at
`hermes_cli/update_registry.py`. This document renders the same
inventory in markdown form for operators. When you add a new updateable
target, register the probe in code AND append a row to the table in the
relevant section here.

**How to use:**

- Search by component name (e.g. `openai`, `playwright`, `nixpkgs`,
  `cua-driver`).
- Each row has: probe name → declared version → `Status` glyph →
  `Update method` → declaration source.
- 🔒 = frozen pin (no bump without written justification) — covers all
  Python core deps under the supply-chain policy.
- ✅ = current; ⚠️ = update available; ? = unknown until a probe is
  run; `-` = no version concept (group probe).
- "Refresh command" blocks under each section produce both
  current-vs-latest data for the rows in that section.

**Scope discipline** (mirror of OpenClaw):

- Tracks anything Hermes-Agent declares (pyproject.toml, package.json,
  flake.lock, Dockerfile) or invokes at runtime (LSP, cua-driver,
  playwright) AND has a version concept.
- Excludes provider APIs (Anthropic, OpenAI, etc. — the model name IS
  the version), the operator's host OS packages (not the agent's
  surface), and the agent's user-installed plugins (those publish their
  own registries).

**Update methods** mean:

- `frozen` — supply-chain policy says don't bump without written
  justification. Python core deps are all frozen; bumping requires
  editing `pyproject.toml` AND regenerating `uv.lock` AND a code review.
- `manual` — operator/maintainer reviews each bump (declared deps,
  Docker base, Node deps).
- `automatic` — tool can refresh without review (Nix inputs via
  `nix flake update`, LSP server binaries via auto-install strategy,
  Playwright browser cache).
- `external` — operator's host package manager owns this (apt inside
  the runtime image, cua-driver installed by `hermes tools`).
- `vendor` — vendor-bundled; bump via vendor script.

**Update procedures** (per category) live at the bottom of this file
under [Update procedures](#update-procedures).

**CORE rule reminder:** before recommending updates, verify
approved-origin. No adversarial-origin packages.

---

## Section 1 — Hermes-Agent self

The Hermes-Agent package itself. Self-update is wired via
`hermes update` and the prefetch banner in `hermes_cli/banner.py`.

| Probe | Current | Status | Update method | Declared in |
|---|---|---|---|---|
| `self.hermes-agent` | 0.14.0 (release 2026.5.16) | ? | manual | `hermes_cli/__init__.py` |

**Refresh command:**

```bash
hermes update --check
```

**Notes:** `hermes update` performs the actual upgrade. `hermes update
--check` is the canonical non-mutating check path: it prefers
`upstream/main` and falls back to `origin/main`, matching
`hermes_cli/main.py:_cmd_update_check`. The startup banner runs
`check_for_updates()` in a background thread (cached for 6h); see
`hermes_cli/banner.py:check_for_updates`.

---

## Section 2 — Python core dependencies

Exact-pinned per the supply-chain policy added 2026-05-12 in response to
the `mistralai 2.4.6` worm on PyPI. Every direct dep is pinned to
`==X.Y.Z` (no ranges). Bumping a core dep requires editing
`pyproject.toml` AND regenerating `uv.lock` AND a code review.

| Probe | Current | Status | Update method | Declared in |
|---|---|---|---|---|
| `py.openai` | 2.24.0 | 🔒 | frozen | `pyproject.toml` |
| `py.certifi` | 2026.5.20 | 🔒 | frozen | `pyproject.toml` |
| `py.python-dotenv` | 1.2.2 | 🔒 | frozen | `pyproject.toml` |
| `py.fire` | 0.7.1 | 🔒 | frozen | `pyproject.toml` |
| `py.httpx` | 0.28.1 | 🔒 | frozen | `pyproject.toml` |
| `py.rich` | 14.3.3 | 🔒 | frozen | `pyproject.toml` |
| `py.tenacity` | 9.1.4 | 🔒 | frozen | `pyproject.toml` |
| `py.pyyaml` | 6.0.3 | 🔒 | frozen | `pyproject.toml` |
| `py.ruamel.yaml` | 0.18.17 | 🔒 | frozen | `pyproject.toml` |
| `py.requests` | 2.33.0 | 🔒 | frozen | `pyproject.toml` |
| `py.jinja2` | 3.1.6 | 🔒 | frozen | `pyproject.toml` |
| `py.pydantic` | 2.13.4 | 🔒 | frozen | `pyproject.toml` |
| `py.prompt_toolkit` | 3.0.52 | 🔒 | frozen | `pyproject.toml` |
| `py.croniter` | 6.0.0 | 🔒 | frozen | `pyproject.toml` |
| `py.packaging` | 26.0 | 🔒 | frozen | `pyproject.toml` |
| `py.Markdown` | 3.10.2 | 🔒 | frozen | `pyproject.toml` |
| `py.psycopg` | 3.3.4 | 🔒 | frozen | `pyproject.toml` |
| `py.PyJWT` | 2.13.0 | 🔒 | frozen | `pyproject.toml` |
| `py.cryptography` | 46.0.7 | 🔒 | frozen | `pyproject.toml` |
| `py.tzdata` | 2025.3 | 🔒 | frozen | `pyproject.toml` |
| `py.psutil` | 7.2.2 | 🔒 | frozen | `pyproject.toml` |
| `py.websockets` | 15.0.1 | 🔒 | frozen | `pyproject.toml` |
| `py.pathspec` | 1.1.1 | 🔒 | frozen | `pyproject.toml` |
| `py.Pillow` | 12.2.0 | 🔒 | frozen | `pyproject.toml` |
| `py.concurrent-log-handler` | 0.9.29 | 🔒 | frozen | `pyproject.toml` |

**Refresh command:**

```bash
# Show all currently-pinned versions:
python3 -c "import tomllib; d=tomllib.load(open('pyproject.toml','rb')); [print(s) for s in d['project']['dependencies']]"

# Compare each pin to PyPI latest. Loop variant for paste-in:
for pkg in openai python-dotenv fire httpx rich tenacity pyyaml ruamel.yaml \
           requests jinja2 pydantic prompt_toolkit croniter packaging Markdown \
           psycopg PyJWT tzdata psutil pathspec Pillow; do
    latest=$(curl -s "https://pypi.org/pypi/${pkg}/json" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])' 2>/dev/null)
    pinned=$(grep -oE "^\s*\"${pkg}==[^\"]+\"" pyproject.toml | head -1)
    echo "  $pkg: pinned=$pinned latest=$latest"
done
```

**Notes:** CVE/PYSEC-tracked entries (`requests` 2.33.0 for CVE-2026-25645,
`PyJWT` 2.13.0 for PYSEC-2026-175/177/178/179) cite the fix reason in the
pyproject comment. When bumping, include the equivalent reason in the commit
message.

---

## Section 3 — Python optional dependencies (extras)

One probe per `[project.optional-dependencies]` group. Some extras are
also lazy-installable at runtime via `tools/lazy_deps.py` — those
delegate the actual install to first use of the backend (see
`tools/lazy_deps.py:LAZY_DEPS`).

> ⛔ **Banned origin — never install or enable** (CORE_DIRECTIVE supply-chain
> rule): `py-extra.wecom` (Tencent), `py-extra.dingtalk` (Alibaba),
> `py-extra.feishu` (ByteDance). Upstream ships these adversarial-origin platform
> adapters; they are tracked here only to record the risk surface and are **not
> installed or enabled** in this deployment.

| Probe | Composition | Status | Update method | Declared in |
|---|---|---|---|---|
| `py-extra.anthropic` | pyproject: `anthropic==0.86.0`; lazy: `anthropic==0.87.0` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.exa` | `exa-py==2.10.2` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.firecrawl` | `firecrawl-py==4.17.0` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.parallel-web` | `parallel-web==0.4.2` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.fal` | `fal-client==0.13.1` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.edge-tts` | `edge-tts==7.2.7` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.modal` | `modal==1.3.4` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.daytona` | `daytona==0.155.0` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.wecom` | `defusedxml==0.7.1` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.hindsight` | `hindsight-client==0.6.1` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.dev` | `debugpy==1.8.20`, `pytest==9.0.2`, `pytest-asyncio==1.3.0`, `pytest-xdist==3.8.0`, `pytest-split==0.11.0`, `mcp==1.26.0`, `ty==0.0.21`, `ruff==0.15.10` | 🔒 | frozen | `pyproject.toml` |
| `py-extra.messaging` | pyproject platform bundle plus lazy pins; lazy Slack currently uses `aiohttp==3.13.4` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.slack` | pyproject: `slack-bolt==1.27.0`, `slack-sdk==3.40.1`, `aiohttp==3.13.3`; lazy: `aiohttp==3.13.4` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.matrix` | `mautrix[encryption]==0.21.0`, `Markdown==3.10.2`, `aiosqlite==0.22.1`, `asyncpg==0.31.0`, `aiohttp-socks==0.11.0` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.cli` | `simple-term-menu==1.6.6` | 🔒 | frozen | `pyproject.toml` |
| `py-extra.tts-premium` | `elevenlabs==1.59.0` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.voice` | `faster-whisper==1.2.1`, `sounddevice==0.5.5`, `numpy==2.4.3` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.pty` | `ptyprocess==0.7.0`, `pywinpty==2.0.15` (platform-conditional) | 🔒 | frozen | `pyproject.toml` |
| `py-extra.honcho` | `honcho-ai==2.0.1` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.mcp` | `mcp==1.26.0` | 🔒 | frozen | `pyproject.toml` |
| `py-extra.homeassistant` | `aiohttp==3.13.3` | 🔒 | frozen | `pyproject.toml` |
| `py-extra.sms` | `aiohttp==3.13.3` | 🔒 | frozen | `pyproject.toml` |
| `py-extra.computer-use` | `mcp==1.26.0` | 🔒 | frozen | `pyproject.toml` |
| `py-extra.acp` | `agent-client-protocol==0.9.0` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.bedrock` | `boto3==1.42.89` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.azure-identity` | `azure-identity==1.25.3` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.dingtalk` | `dingtalk-stream==0.24.3`, `alibabacloud-dingtalk==2.2.42`, `qrcode==7.4.2` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.feishu` | `lark-oapi==1.5.3`, `qrcode==7.4.2` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.google` | `google-api-python-client==2.194.0`, `google-auth-oauthlib==1.3.1`, `google-auth-httplib2==0.3.1` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.youtube` | `youtube-transcript-api==1.2.4` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.web` | `fastapi==0.133.1`, `uvicorn[standard]==0.41.0` | 🔒 | frozen | `pyproject.toml` + `tools/lazy_deps.py` |
| `py-extra.cron` | (empty — back-compat for croniter, now core) | - | frozen | `pyproject.toml` |
| `py-extra.termux` | (composite for Android/Termux baseline) | - | frozen | `pyproject.toml` |
| `py-extra.termux-all` | (composite for Android/Termux full) | - | frozen | `pyproject.toml` |
| `py-extra.all` | (composite — see policy in `pyproject.toml`) | - | frozen | `pyproject.toml` |

**Refresh command:**

```bash
# Show every extra and its pinned specs:
python3 - <<'PY'
import tomllib
data = tomllib.load(open('pyproject.toml','rb'))
for name, specs in data['project']['optional-dependencies'].items():
    print(f"[{name}]")
    for s in specs:
        print(f"  {s}")
PY

# Per-extra version compare against PyPI: loop the package out of the spec.
```

**Notes:**

- The `mistral` extra was REMOVED 2026-05-12; PyPI quarantined the
  `mistralai` project after the 2.4.6 worm. Re-add only after a clean
  release per the restoration checklist in `pyproject.toml`.
- Extras included in `[all]` deliberately exclude lazy-installable
  backends (anthropic, exa, firecrawl, parallel-web, fal, edge-tts,
  modal, daytona, vercel, messaging adapters, matrix, slack, honcho,
  voice, dingtalk, feishu, bedrock, tts-premium). They install on first
  use through `tools/lazy_deps.py`. Bumping a pin in either table
  normally requires updating BOTH the extra spec AND `LAZY_DEPS`. If a
  security-response lazy pin temporarily differs from `pyproject.toml`,
  surface both values here until the pyproject/lockfile bump lands.

---

## Section 4 — Node.js dependencies

Three `package.json` files: root (browser tooling), `web/` (dashboard
SPA), `ui-tui/` (terminal UI). Each has its own lockfile.

| Probe | Manifest | Status | Update method | Declared in |
|---|---|---|---|---|
| `node.root` | `agent-browser ^0.26.0` | ? | manual | `package.json` |
| `node.web` | React, Vite, xterm, Tailwind, etc. | ? | manual | `web/package.json` |
| `node.ui-tui` | Ink, React, tsx, @hermes/ink workspace | ? | manual | `ui-tui/package.json` |

**Refresh command:**

```bash
npm outdated --prefix . --depth=0
(cd web && npm outdated --depth=0)
(cd ui-tui && npm outdated --depth=0)
# Show explicit pins per manifest:
for d in . web ui-tui; do
    echo "=== $d ==="
    python3 -c "import json; d=json.load(open('$d/package.json')); print(json.dumps({**d.get('dependencies',{}), **d.get('devDependencies',{})}, indent=2))"
done
```

**Notes:**

- `ui-tui/packages/hermes-ink/` is referenced as a `file:` workspace
  dependency from `ui-tui/package.json`. The Dockerfile copies that
  tree in full before `npm install` so the workspace resolves to real
  content.
- The Dockerfile sets `npm_config_install_links=false` to force
  symlink-mode on older Debian-bundled npm 9. The host-side
  `package-lock.json` is generated with a newer npm that uses
  symlinks — install-as-copy produces a hidden lockfile that disagrees
  with the root and trips the runtime `npm install` re-trigger.

---

## Section 5 — Nix flake inputs

Pinned in `flake.lock`. Refresh via `nix flake update` (all) or
`nix flake lock --update-input <name>` (one).

| Probe | Input ref | Status | Update method | Declared in |
|---|---|---|---|---|
| `nix.nixpkgs` | `nixos-unstable` | ? | automatic | `flake.nix` |
| `nix.flake-parts` | `main` (hercules-ci/flake-parts) | ? | automatic | `flake.nix` |
| `nix.pyproject-nix` | `main` | ? | automatic | `flake.nix` |
| `nix.uv2nix` | `main` | ? | automatic | `flake.nix` |
| `nix.pyproject-build-systems` | `main` | ? | automatic | `flake.nix` |
| `nix.npm-lockfile-fix` | `main` | ? | automatic | `flake.nix` |

**Refresh command:**

```bash
# Show locked input revisions without mutating flake.lock:
python3 - <<'PY'
import json
lock = json.load(open('flake.lock'))
for name, node in lock.get('nodes', {}).items():
    locked = node.get('locked') or {}
    if 'rev' in locked:
        print(name, locked.get('rev', '')[:12], locked.get('lastModified'))
PY

# Check one input in a temporary copy so the source tree stays clean:
name=nixpkgs
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cp flake.nix flake.lock "$tmp"/
(cd "$tmp" && nix flake lock --update-input "$name" --quiet >/dev/null)
git diff --no-index -- flake.lock "$tmp/flake.lock" >/dev/null
case $? in
  0) echo "$name: current" ;;
  1) echo "$name: update_available" ;;
  *) echo "$name: probe_failed" ;;
esac
```

**Notes:** Inputs are all upstream-maintained — the registry mainly
tracks "are we current with origin?" not "is the dep updateable?" (yes,
always, via flake-update).

---

## Section 6 — Docker base images

Multi-stage Docker build uses pinned-by-digest images for reproducible
builds. Bumping requires picking a new tag, refreshing the `@sha256:`
digest, and rebuilding.

| Probe | Pinned tag | Status | Update method | Declared in |
|---|---|---|---|---|
| `docker.uv-source` | `0.11.6-python3.13-trixie` | ? | manual | `Dockerfile` |
| `docker.node-source` | `22-bookworm-slim` | ? | manual | `Dockerfile` |
| `docker.debian` | `13.4` | ? | manual | `Dockerfile` |

**Refresh command:**

```bash
# Show current pins:
grep -E '^FROM ' Dockerfile

# Compare each pinned tag to the registry digest it currently resolves to. uv:
docker pull ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie 2>&1 | tail -3
# node:
docker pull node:22-bookworm-slim 2>&1 | tail -3
# debian:
docker pull debian:13.4 2>&1 | tail -3

# Inspect the current digest:
docker manifest inspect ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie | python3 -c 'import json,sys; print(json.load(sys.stdin).get("config",{}).get("digest"))'
```

**Notes:**

- `uv-source` and `node-source` are multi-stage `FROM ... AS` source
  images — they don't ship to runtime, just supply binaries via
  `COPY --from=` to the runtime layer.
- `debian:13.4` is tag-pinned (not digest-pinned). Bumping picks up
  Debian point-release security patches automatically on rebuild.

---

## Section 7 — System binaries (inside the runtime image)

apt packages installed inside the runtime image via the Dockerfile's
single-layer `apt-get install`. Versions follow the `debian:13.4` base;
refresh by rebuilding the image.

| Probe | Components | Status | Update method | Declared in |
|---|---|---|---|---|
| `apt.runtime-toolchain` | build-essential, curl, nodejs, npm, python3, ripgrep, ffmpeg, gcc, python3-dev, libffi-dev, procps, git, openssh-client, docker-cli, tini | - | external | `Dockerfile` |

**Refresh command:**

```bash
docker run --rm debian:13.4 apt list --installed 2>/dev/null \
  | grep -E '^(build-essential|curl|nodejs|npm|python3|ripgrep|ffmpeg|gcc|python3-dev|libffi-dev|procps|git|openssh-client|docker-cli|tini)/'

# Compare to apt-cache for available versions:
docker run --rm debian:13.4 sh -c 'apt-get update -qq && apt-cache policy ripgrep ffmpeg git'
```

**Notes:**

- `tini` reaps zombie children that would otherwise accumulate when
  Hermes runs as PID 1. See issue #15012.
- `docker-cli` is needed for the docker-in-docker terminal backend in
  `tools/environments/`.

---

## Section 8 — Runtime binaries (host-installed, not bundled)

Tools the agent shells out to but doesn't bundle in the image. Installed
by the operator (`hermes tools`) or auto-installed at first use.

| Probe | Binary | Status | Update method | Declared in |
|---|---|---|---|---|
| `runtime.cua-driver` | cua-driver (macOS computer-use binary) | ? | external | — |
| `runtime.lsp-servers` | LSP servers under `$HERMES_HOME/lsp/bin/` | ? | automatic | `agent/lsp/install.py` |

**Refresh command:**

```bash
cua-driver --version 2>/dev/null || echo 'cua-driver not installed'

# LSP servers — Hermes auto-install strategy + binary inventory:
hermes lsp status 2>/dev/null \
  || ls -la "${HERMES_HOME:-$HOME/.hermes}/lsp/bin/" 2>/dev/null
```

**Notes:**

- `cua-driver` is installed via a curl script run by `hermes tools` (see
  `tools/computer_use/` for the MCP-over-stdio client). The pyproject
  `computer-use` extra pins the MCP client, not the driver binary.
- LSP servers are pinned per-recipe in `agent/lsp/install.py:INSTALL_RECIPES`.
  Auto-install strategy is controlled by config
  `lsp.install_strategy` (`auto` / `manual` / `off`).

---

## Section 9 — Browser assets (Playwright)

Playwright downloads a chromium headless shell at install time. The
Dockerfile pins the path to `/opt/hermes/.playwright` so the build-time
install survives the runtime volume overlay at `/opt/data`.

| Probe | Asset | Status | Update method | Declared in |
|---|---|---|---|---|
| `playwright.chromium` | chromium (`--only-shell`) | ? | automatic | `Dockerfile` |

**Refresh command:**

```bash
npx playwright --version
PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/hermes/.playwright}" \
  npx playwright install --dry-run chromium
```

**Notes:** Refreshes when the Playwright npm peer bumps. The build-time
install survives the `/opt/data` volume overlay because
`PLAYWRIGHT_BROWSERS_PATH` is set to a path outside the volume mount.

---

## How to update this registry

When a new updateable component is added to the codebase (new
pyproject dep, new package.json package, new Dockerfile FROM, new
runtime binary), edit BOTH:

1. `hermes_cli/update_registry.py` — register the probe via the
   relevant `_register_*` section in `_register_builtin_probes()`.
2. This file (`docs/update-registry.md`) — append a row to the
   relevant section's table.

A test (`tests/hermes_cli/test_update_registry.py`) verifies that every
probe in the code has a matching row format, so divergence between code
and doc fails CI.

**Quick inventory script** (paste-able):

```bash
python3 - <<'PY'
from hermes_cli.update_registry import list_probes, KNOWN_CATEGORIES
for cat in KNOWN_CATEGORIES:
    rows = list_probes(category=cat)
    print(f"=== {cat} ({len(rows)}) ===")
    for p in rows:
        print(f"  {p.status_glyph()} {p.name:32} {p.update_method:10} {p.current_version or '-'}")
PY
```

After running this, update the version columns in the relevant tables
and bump `last_updated` in the frontmatter. **Append a Changelog entry**
below describing what changed.

---

## Update procedures

### Hermes-Agent self

```bash
hermes update
```

`hermes update` performs the upgrade. It clears stale .pyc caches and
restarts the dashboard process if the running backend's hash diverges
from the freshly-updated frontend.

### Python core deps (frozen)

```bash
# 1. Edit the version literal in pyproject.toml
# 2. Regenerate the lockfile:
uv lock
# 3. (Optional) re-sync the venv:
uv sync --frozen
# 4. Run the tests for the touched area:
.venv/bin/pytest tests/<area> -x
```

Bumping a `frozen` entry requires the commit message to include the
"why" (CVE, transitive break, etc.). The supply-chain comment at the
top of `pyproject.toml` is the canonical policy.

### Python extras

Same as core — edit the extra's spec in `pyproject.toml`, regenerate
the lockfile, and run tests. If the extra is mirrored in
`tools/lazy_deps.py:LAZY_DEPS`, bump both in the same commit.

### Node.js deps (per workspace)

```bash
# Per workspace:
(cd ui-tui && npm install <pkg>@<version>)
# Or use the matching upgrade script under scripts/, when one exists.
```

### Nix flake inputs

```bash
nix flake update --commit-lock-file        # all inputs
nix flake lock --update-input nixpkgs       # one input
```

### Docker base images

```bash
# Pull the new tag, capture the new digest:
docker pull ghcr.io/astral-sh/uv:<new-tag>
docker inspect ghcr.io/astral-sh/uv:<new-tag> --format='{{index .RepoDigests 0}}'
# Then edit the Dockerfile FROM line with the new tag + digest.
docker build .
```

### Runtime binaries

`hermes tools` installs cua-driver. LSP server binaries auto-install on
first use unless `lsp.install_strategy: off` is set in config.

---

## Open items

1. **No automatic uv-vs-PyPI drift detection in CI.** A nice follow-up
   is a Github Action that fails CI when any frozen pin in the registry
   diverges from a fresh PyPI fetch. Track here.
2. **node-deps versions are coarse-grained.** The probes currently
   report at the manifest level, not per-package; consider expanding
   high-traffic packages (Vite, React, Ink) into individual probes.
3. **No CVE feed integration.** The frozen-pin policy cites CVE fixes
   in the pyproject comments and in the registry's `notes` field, but
   neither auto-cross-references against an upstream feed. Folded into
   the same potential CI job.

---

## References

- `hermes_cli/update_registry.py` — code source of truth.
- `tests/hermes_cli/test_update_registry.py` — keeps code and doc in sync.
- `pyproject.toml` — Python deps (core + extras) and the supply-chain policy.
- `package.json`, `web/package.json`, `ui-tui/package.json` — Node manifests.
- `flake.nix`, `flake.lock` — Nix inputs.
- `Dockerfile` — Docker base images + apt toolchain.
- `agent/lsp/install.py` — LSP auto-install recipes.
- `tools/lazy_deps.py` — Runtime lazy-install map (mirror of extras for opt-in backends).
- `hermes_cli/banner.py:check_for_updates` — Self-update probe (PyPI + git).
- OpenClaw counterpart: `~/.openclaw/workspace/references/update-registry.md`.

---

## Changelog

Append-only log of every edit to this registry. Newest entries first.
Format:

```
### YYYY-MM-DD — <one-line summary>
**Editor**: <name / session>
**Touched**: <comma-separated list of section names or rows changed>
**Why**: <reason for the change>
**Commit**: <git short SHA, if committed>
```

---

### 2026-05-18 — Initial registry creation

**Editor**: claude-code background lane (Hermes Kanban task t_1d1ec667)
**Touched**: ALL sections — initial inventory
**Why**: Bootstrap a Hermes-Agent-specific update registry mirroring the
OpenClaw operator pattern. Probes registered in
`hermes_cli/update_registry.py`; this doc renders the same inventory in
markdown form. Covers self-update, Python core + extras, Node deps
across three workspaces, Nix flake inputs, Docker base images, apt
runtime toolchain, runtime binaries (cua-driver, LSP), and Playwright
browser assets.
**Commit**: pending
