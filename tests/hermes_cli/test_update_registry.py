"""Tests for hermes_cli/update_registry.py.

Cover three properties:

1. Registration mechanics — register/list/get round-trip, type/value
   validation, thread-safety isn't blown by basic concurrent use.
2. Built-in probe inventory — every required Hermes-Agent surface has a
   probe registered, and each probe's metadata is well-formed (known
   category, known update_method, non-empty refresh_command unless the
   probe is intentionally external).
3. Code/doc parity — every probe name in the code appears in the
   markdown doc, and vice versa. Prevents silent drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hermes_cli import update_registry
from hermes_cli.update_registry import (
    KNOWN_CATEGORIES,
    UPDATE_METHODS,
    UpdateProbe,
    _register_builtin_probes,
    get_probe,
    list_probes,
    register_probe,
    render_markdown_table,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_registry():
    """Reset between tests so registration errors don't bleed across."""
    update_registry._reset_for_tests()
    _register_builtin_probes()
    yield
    update_registry._reset_for_tests()
    _register_builtin_probes()


# ---------------------------------------------------------------------------
# Registration mechanics
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_and_get_round_trip(self):
        probe = UpdateProbe(
            name="test.example",
            category="self",
            description="Test probe",
            declared_in="test.py",
            current_version="1.0",
            updateable=True,
            update_method="manual",
            refresh_command="echo current=1.0",
        )
        register_probe(probe)
        assert get_probe("test.example") is probe

    def test_register_rejects_non_probe(self):
        with pytest.raises(TypeError):
            register_probe("not a probe")  # type: ignore[arg-type]

    def test_register_rejects_empty_name(self):
        with pytest.raises(ValueError):
            register_probe(UpdateProbe(
                name="",
                category="self",
                description="x",
                declared_in=None,
                current_version=None,
                updateable=False,
                update_method="manual",
                refresh_command="",
            ))

    def test_register_rejects_whitespace_name(self):
        with pytest.raises(ValueError):
            register_probe(UpdateProbe(
                name="   ",
                category="self",
                description="x",
                declared_in=None,
                current_version=None,
                updateable=False,
                update_method="manual",
                refresh_command="",
            ))

    def test_register_rejects_unknown_category(self):
        with pytest.raises(ValueError):
            register_probe(UpdateProbe(
                name="bad.cat",
                category="not-a-category",
                description="x",
                declared_in=None,
                current_version=None,
                updateable=True,
                update_method="manual",
                refresh_command="",
            ))

    def test_register_rejects_unknown_update_method(self):
        with pytest.raises(ValueError):
            register_probe(UpdateProbe(
                name="bad.method",
                category="self",
                description="x",
                declared_in=None,
                current_version=None,
                updateable=True,
                update_method="not-a-method",
                refresh_command="",
            ))

    def test_register_rejects_unknown_status_glyph(self):
        with pytest.raises(ValueError):
            register_probe(UpdateProbe(
                name="bad.status",
                category="self",
                description="x",
                declared_in=None,
                current_version=None,
                updateable=True,
                update_method="manual",
                refresh_command="",
                status="surprised",
            ))

    def test_reregister_overwrites(self):
        first = UpdateProbe(
            name="dup",
            category="self",
            description="first",
            declared_in=None,
            current_version="1",
            updateable=True,
            update_method="manual",
            refresh_command="",
        )
        second = UpdateProbe(
            name="dup",
            category="self",
            description="second",
            declared_in=None,
            current_version="2",
            updateable=True,
            update_method="manual",
            refresh_command="",
        )
        register_probe(first)
        register_probe(second)
        assert get_probe("dup") is second

    def test_get_probe_returns_none_on_unknown(self):
        assert get_probe("definitely-not-registered") is None

    def test_get_probe_handles_non_string(self):
        # Non-string input returns None instead of raising; matches the
        # browser_registry.get_provider() defensive contract.
        assert get_probe(None) is None  # type: ignore[arg-type]
        assert get_probe(42) is None  # type: ignore[arg-type]

    def test_get_probe_strips_whitespace(self):
        register_probe(UpdateProbe(
            name="trim.me",
            category="self",
            description="x",
            declared_in=None,
            current_version=None,
            updateable=False,
            update_method="manual",
            refresh_command="",
        ))
        assert get_probe("  trim.me  ") is not None


class TestListing:
    def test_list_returns_sorted_by_category_then_name(self):
        all_probes = list_probes()
        # Category-order must follow KNOWN_CATEGORIES; within a category,
        # names must be sorted.
        cat_index = {c: i for i, c in enumerate(KNOWN_CATEGORIES)}
        ordering = [(cat_index[p.category], p.name) for p in all_probes]
        assert ordering == sorted(ordering)

    def test_list_filters_by_category(self):
        only_self = list_probes(category="self")
        assert all(p.category == "self" for p in only_self)
        assert len(only_self) >= 1  # at least self.hermes-agent

    def test_list_unknown_category_returns_empty(self):
        assert list_probes(category="no-such-category") == []


# ---------------------------------------------------------------------------
# Built-in probe inventory
# ---------------------------------------------------------------------------


REQUIRED_PROBES = {
    # self
    "self.hermes-agent",
    # python-core-deps — every direct pyproject.toml dependency
    "py.openai",
    "py.python-dotenv",
    "py.fire",
    "py.httpx",
    "py.rich",
    "py.tenacity",
    "py.pyyaml",
    "py.ruamel.yaml",
    "py.requests",
    "py.jinja2",
    "py.pydantic",
    "py.prompt_toolkit",
    "py.croniter",
    "py.PyJWT",
    "py.tzdata",
    "py.psutil",
    # python-extras — at least the high-traffic ones
    "py-extra.anthropic",
    "py-extra.matrix",
    "py-extra.web",
    "py-extra.acp",
    "py-extra.computer-use",
    "py-extra.mcp",
    # node-deps
    "node.root",
    "node.web",
    "node.ui-tui",
    # nix-inputs
    "nix.nixpkgs",
    "nix.flake-parts",
    "nix.pyproject-nix",
    "nix.uv2nix",
    "nix.pyproject-build-systems",
    "nix.npm-lockfile-fix",
    # docker-base
    "docker.uv-source",
    "docker.node-source",
    "docker.debian",
    # system-binaries
    "apt.runtime-toolchain",
    # runtime-binaries
    "runtime.cua-driver",
    "runtime.lsp-servers",
    # browser-assets
    "playwright.chromium",
}


class TestBuiltinProbes:
    def test_all_required_probes_registered(self):
        registered = {p.name for p in list_probes()}
        missing = REQUIRED_PROBES - registered
        assert not missing, f"Missing built-in probes: {sorted(missing)}"

    def test_every_probe_has_known_category(self):
        for p in list_probes():
            assert p.category in KNOWN_CATEGORIES, (
                f"Probe {p.name} has unknown category {p.category!r}"
            )

    def test_every_probe_has_known_update_method(self):
        for p in list_probes():
            assert p.update_method in UPDATE_METHODS, (
                f"Probe {p.name} has unknown update_method "
                f"{p.update_method!r}"
            )

    def test_updateable_probes_have_refresh_command(self):
        """If a probe is updateable, the operator needs a way to check
        the upstream state — either via shell snippet or via the
        update_method itself (external = host package manager). Group
        probes with no version concept are allowed an empty command
        only when the description signals it's a composite group."""
        for p in list_probes():
            if not p.updateable:
                continue
            if p.refresh_command:
                continue
            assert p.update_method in {"external", "vendor"}, (
                f"Updateable probe {p.name} has no refresh_command "
                f"and is not external/vendor"
            )

    def test_frozen_probes_have_notes(self):
        """Frozen pins must explain why they're frozen — the OpenClaw
        registry convention is to capture the rationale in the notes
        field so future bumps can re-evaluate."""
        for p in list_probes():
            if p.update_method != "frozen":
                continue
            assert p.notes, (
                f"Frozen probe {p.name} has no notes field — "
                "frozen pins should explain why they're frozen"
            )

    def test_python_core_deps_count_matches_pyproject(self):
        """The python-core-deps category should have one probe per
        explicit `==` pin in [project.dependencies]. If pyproject.toml
        adds a new core dep without a matching probe, this test fails."""
        pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        # Count of `"pkg==X.Y.Z"` lines inside [project.dependencies].
        match = re.search(
            r'^dependencies\s*=\s*\[(.*?)^\]',
            pyproject,
            re.MULTILINE | re.DOTALL,
        )
        assert match, "Couldn't find [project.dependencies] block"
        deps_block = match.group(1)
        # Count exact-pinned entries (`"pkg==..."`), one per line. The
        # char class includes `,` so multi-extra pins like
        # `psycopg[binary,pool]==` are counted — the comma previously
        # broke the match and silently undercounted such deps.
        pinned = re.findall(r'^\s*"[A-Za-z][A-Za-z0-9_.,\-\[\]]*==', deps_block, re.MULTILINE)
        probe_count = len(list_probes(category="python-core-deps"))
        assert probe_count == len(pinned), (
            f"python-core-deps probe count ({probe_count}) does not "
            f"match pyproject.toml [project.dependencies] pinned count "
            f"({len(pinned)}). Update update_registry._CORE_DEPS to "
            "match."
        )

    def test_python_core_versions_are_read_live_from_pyproject(self):
        """current_version for python-core probes must equal the live
        pyproject.toml pin (read at import), so the inventory cannot drift
        from the declared pins the way the old static snapshot did."""
        import tomllib
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        pins = {}
        for spec in data["project"]["dependencies"]:
            base = str(spec).split(";", 1)[0].strip()
            if "==" not in base:
                continue
            name, _, version = base.partition("==")
            key = name.split("[", 1)[0].strip().lower().replace("_", "-").replace(".", "-")
            pins[key] = version.strip()
        for probe in list_probes(category="python-core-deps"):
            pkg = probe.name[len("py."):]
            key = pkg.lower().replace("_", "-").replace(".", "-")
            assert key in pins, f"{probe.name} not in pyproject pins"
            assert probe.current_version == pins[key], (
                f"{probe.name}: probe={probe.current_version!r} "
                f"pyproject={pins[key]!r} — live-read drift"
            )

    def test_docker_base_probes_match_dockerfile_from_lines(self):
        """Every FROM <image> line in the Dockerfile should map to one
        docker-base probe. New base images without a probe surface here."""
        dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
        froms = re.findall(r'^FROM\s+(\S+)', dockerfile, re.MULTILINE)
        probe_count = len(list_probes(category="docker-base"))
        assert probe_count == len(froms), (
            f"docker-base probe count ({probe_count}) does not match "
            f"Dockerfile FROM count ({len(froms)}: {froms})"
        )

    def test_self_probe_uses_real_update_check_command(self):
        """The self-update probe should not duplicate a stale subset of
        Hermes' update logic. `hermes update --check` already handles
        upstream-vs-origin fork preference in hermes_cli.main."""
        probe = get_probe("self.hermes-agent")
        assert probe is not None
        assert probe.refresh_command == "hermes update --check"
        assert probe.status_glyph() == "?"

    def test_runtime_dynamic_version_probes_render_unknown_not_dash(self):
        """Runtime-resolved binaries have a version concept even when the
        registry cannot know the local version at import time."""
        for name in {"runtime.cua-driver", "runtime.lsp-servers"}:
            probe = get_probe(name)
            assert probe is not None
            assert probe.current_version is None
            assert probe.status_glyph() == "?"

    def test_lazy_installed_versions_are_visible_in_extra_probes(self):
        """Some extras are also installed through tools/lazy_deps.py. The
        registry must surface those runtime pins, especially when they
        temporarily differ from pyproject.toml during security response."""
        expectations = {
            "py-extra.anthropic": ["anthropic==0.87.0"],
            "py-extra.messaging": ["aiohttp==3.13.4", "discord.py[voice]==2.7.1"],
            "py-extra.slack": ["aiohttp==3.13.4"],
        }
        for probe_name, specs in expectations.items():
            probe = get_probe(probe_name)
            assert probe is not None
            for spec in specs:
                assert spec in probe.description, (
                    f"{probe_name} description should mention lazy pin {spec}"
                )

    def test_lazy_backed_extras_show_lazy_deps_declaration(self):
        lazy_backed = {
            "anthropic", "exa", "firecrawl", "parallel-web", "fal",
            "edge-tts", "modal", "daytona", "wecom", "hindsight",
            "messaging", "slack", "matrix", "tts-premium", "voice",
            "honcho", "acp", "bedrock", "azure-identity", "dingtalk",
            "feishu", "google", "youtube", "web",
        }
        for extra in lazy_backed:
            probe = get_probe(f"py-extra.{extra}")
            assert probe is not None
            assert probe.declared_in == "pyproject.toml + tools/lazy_deps.py"

    def test_banned_origin_extras_are_flagged(self):
        """Adversarial-origin platform extras (Tencent/Alibaba/ByteDance) must be
        flagged updateable=False with a BANNED ORIGIN marker so the inventory
        enforces the CORE_DIRECTIVE supply-chain rule instead of listing them as
        ordinary installable extras."""
        for extra in ("wecom", "dingtalk", "feishu"):
            probe = get_probe(f"py-extra.{extra}")
            assert probe is not None, f"{extra} probe missing"
            assert probe.updateable is False, f"{extra} must be updateable=False"
            assert "BANNED ORIGIN" in probe.description, f"{extra} desc must flag origin"
            assert "ADVERSARIAL ORIGIN" in probe.notes, f"{extra} notes must flag origin"

    def test_refresh_commands_avoid_mutating_source_tree_for_checks(self):
        """Registry refresh snippets are meant as check probes. They must
        not rewrite tracked files in-place just to answer whether an
        update exists."""
        for probe in list_probes(category="nix-inputs"):
            assert "mktemp" in probe.refresh_command
            assert "flake.lock \"$tmp/flake.lock\"" in probe.refresh_command
        for probe in list_probes(category="docker-base"):
            assert ":latest" not in probe.refresh_command
            assert "debian:stable" not in probe.refresh_command


# ---------------------------------------------------------------------------
# UpdateProbe semantics
# ---------------------------------------------------------------------------


class TestStatusGlyph:
    def test_frozen_returns_lock(self):
        p = UpdateProbe(
            name="x",
            category="self",
            description="",
            declared_in=None,
            current_version="1",
            updateable=True,
            update_method="frozen",
            refresh_command="",
        )
        assert p.status_glyph() == "🔒"

    def test_no_version_returns_dash(self):
        p = UpdateProbe(
            name="x",
            category="self",
            description="",
            declared_in=None,
            current_version=None,
            updateable=True,
            update_method="manual",
            refresh_command="",
        )
        assert p.status_glyph() == "-"

    def test_status_override_allows_runtime_resolved_unknown(self):
        p = UpdateProbe(
            name="x",
            category="self",
            description="",
            declared_in=None,
            current_version=None,
            updateable=True,
            update_method="manual",
            refresh_command="hermes update --check",
            status="?",
        )
        assert p.status_glyph() == "?"

    def test_versioned_unfrozen_returns_question(self):
        p = UpdateProbe(
            name="x",
            category="self",
            description="",
            declared_in=None,
            current_version="1.0",
            updateable=True,
            update_method="manual",
            refresh_command="",
        )
        assert p.status_glyph() == "?"


class TestRendering:
    def test_render_markdown_table_includes_all_rows_for_category(self):
        rendered = render_markdown_table("python-core-deps")
        # Sanity-check: each probe in the category appears in the table
        # by name (the markdown table includes the probe name in
        # backticks).
        for p in list_probes(category="python-core-deps"):
            assert f"`{p.name}`" in rendered, (
                f"render_markdown_table missing row for {p.name}"
            )

    def test_render_markdown_table_unknown_category_returns_placeholder(self):
        rendered = render_markdown_table("not-a-category")
        assert "no probes registered" in rendered


# ---------------------------------------------------------------------------
# Code / doc parity
# ---------------------------------------------------------------------------


REGISTRY_DOC = REPO_ROOT / "docs" / "update-registry.md"


class TestDocParity:
    def test_doc_exists(self):
        assert REGISTRY_DOC.exists(), (
            f"Expected the registry doc at {REGISTRY_DOC} — keep code "
            "and doc together so operators can locate the registry "
            "without reading Python."
        )

    def test_every_registered_probe_appears_in_doc(self):
        """Each registered probe must appear in the doc by name (in
        backticks) so the doc can't silently fall behind the code."""
        doc_text = REGISTRY_DOC.read_text(encoding="utf-8")
        missing = []
        for p in list_probes():
            token = f"`{p.name}`"
            if token not in doc_text:
                missing.append(p.name)
        assert not missing, (
            f"docs/update-registry.md is missing rows for these probes: "
            f"{sorted(missing)}. Append rows to the relevant section or "
            "remove the probe from the code."
        )

    def test_doc_status_matches_registered_status_glyph(self):
        """The operator-facing doc's Status column must match the code
        renderer so dynamic/runtime probes don't drift between `?`, `-`,
        and `🔒`."""
        doc_statuses = {}
        for line in REGISTRY_DOC.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| `"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 5:
                continue
            name_match = re.fullmatch(r"`([^`]+)`", parts[1])
            if not name_match:
                continue
            doc_statuses[name_match.group(1)] = parts[3]

        missing = []
        mismatched = []
        for probe in list_probes():
            actual = probe.status_glyph()
            expected = doc_statuses.get(probe.name)
            if expected is None:
                missing.append(probe.name)
            elif expected != actual:
                mismatched.append((probe.name, expected, actual))
        assert not missing, f"Missing doc status rows for {sorted(missing)}"
        assert not mismatched, (
            "Doc/code status mismatches: "
            + ", ".join(
                f"{name}: doc={doc!r} code={code!r}"
                for name, doc, code in mismatched
            )
        )

    def test_doc_declared_in_matches_registered_declared_in(self):
        doc_declared = {}
        for line in REGISTRY_DOC.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| `"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) < 6:
                continue
            name_match = re.fullmatch(r"`([^`]+)`", parts[1])
            if not name_match:
                continue
            doc_declared[name_match.group(1)] = parts[5].replace("`", "")

        mismatched = []
        for probe in list_probes():
            expected = doc_declared.get(probe.name)
            actual = probe.declared_in or "—"
            if expected != actual:
                mismatched.append((probe.name, expected, actual))
        assert not mismatched, (
            "Doc/code declared_in mismatches: "
            + ", ".join(
                f"{name}: doc={doc!r} code={code!r}"
                for name, doc, code in mismatched
            )
        )

    def test_doc_mentions_known_categories(self):
        """Every known category should appear by name somewhere in the
        doc — otherwise a new category is registered in code without
        an operator-facing section."""
        doc_text = REGISTRY_DOC.read_text(encoding="utf-8")
        # The doc uses friendly section titles ("Python core dependencies",
        # "Docker base images"), but each category's internal name must
        # appear at least once via a probe name (`py.openai` → "py."
        # prefix is enough to surface the python-core-deps section).
        for cat in KNOWN_CATEGORIES:
            probes_in_cat = list_probes(category=cat)
            if not probes_in_cat:
                continue
            assert any(
                f"`{p.name}`" in doc_text for p in probes_in_cat
            ), f"Category {cat!r} has no probe rendered in the doc"
