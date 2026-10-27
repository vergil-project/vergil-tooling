"""Tests for local repo config audit checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import dependabot, gitignore, repo_config
from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.repo_config import audit_local_config

if TYPE_CHECKING:
    from vergil_tooling.lib.github_config import ConfigDiff, DiffItem

_MINIMAL_VERGIL_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]

[ci]
versions = ["3.12"]

[dependencies]
vergil = "v2.0.7"
"""

_MINIMAL_VERGIL_TOML_VER = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]

[ci]
versions = ["3.12"]

[dependencies]
vergil = "{version}"
"""


class TestVergilToml:
    def test_missing(self, tmp_path: Path) -> None:
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.vergil_toml" in fields

    def test_malformed(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text("not valid toml {{{")
        diff = audit_local_config(tmp_path)
        matches = [i for i in diff.items if i.field == "local.vergil_toml"]
        assert len(matches) == 1
        actual = str(matches[0].actual).lower()
        assert "not valid toml" in actual or "invalid" in actual

    def test_missing_required_field(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text("[project]\n")
        diff = audit_local_config(tmp_path)
        matches = [i for i in diff.items if i.field == "local.vergil_toml"]
        assert len(matches) == 1

    def test_valid(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.vergil_toml" not in fields


_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "vergil_tooling"
    / "data"
    / "claude_md_consumer.md"
)
_TEMPLATE_TEXT = _TEMPLATE_PATH.read_text(encoding="utf-8")


class TestHookGuardShim:
    def test_missing(self, tmp_path: Path) -> None:
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.hook_guard_shim" in fields

    def test_present(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "guard.sh").write_text("#!/usr/bin/env bash\nexec vrg-hook-guard\n")
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.hook_guard_shim" not in fields


class TestClaudeMd:
    def test_missing_file(self, tmp_path: Path) -> None:
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_md" in fields

    def test_without_template(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md\n\nSome other content.\n")
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_md" in fields

    def test_with_template(self, tmp_path: Path) -> None:
        content = "# CLAUDE.md\n\n" + _TEMPLATE_TEXT + "\n## Project Overview\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_md" not in fields

    def test_single_char_difference_fails(self, tmp_path: Path) -> None:
        modified = _TEMPLATE_TEXT.replace("vrg-git", "vrg-Git", 1)
        (tmp_path / "CLAUDE.md").write_text(modified)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_md" in fields

    def test_template_alone_is_compliant(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text(_TEMPLATE_TEXT)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_md" not in fields


_BEGIN = "<!-- vergil:template:claude-md:begin -->"
_END = "<!-- vergil:template:claude-md:end -->"


def _claude_md_items(tmp_path: Path) -> list:
    diff = audit_local_config(tmp_path)
    return [i for i in diff.items if i.field == "local.claude_md"]


class TestClaudeMdMarkers:
    def test_marked_region_matching_template_is_compliant(self, tmp_path: Path) -> None:
        content = f"# CLAUDE.md\n\n{_BEGIN}\n{_TEMPLATE_TEXT}{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        assert _claude_md_items(tmp_path) == []

    def test_repo_local_content_outside_markers_is_ignored(self, tmp_path: Path) -> None:
        content = (
            f"# CLAUDE.md\n\nIntro prose.\n\n{_BEGIN}\n{_TEMPLATE_TEXT}{_END}\n"
            "\n## Identity modes\n\nRepo-local guidance, including the words\n"
            "template not found, which must not confuse the check.\n"
        )
        (tmp_path / "CLAUDE.md").write_text(content)
        assert _claude_md_items(tmp_path) == []

    def test_blank_lines_around_region_are_tolerated(self, tmp_path: Path) -> None:
        content = f"# CLAUDE.md\n\n{_BEGIN}\n\n\n{_TEMPLATE_TEXT}\n\n{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        assert _claude_md_items(tmp_path) == []

    def test_divergent_line_reports_both_line_numbers(self, tmp_path: Path) -> None:
        first_line = _TEMPLATE_TEXT.splitlines()[0]
        mutated = _TEMPLATE_TEXT.replace(first_line, first_line + " (edited)", 1)
        # _BEGIN is line 1, so template line 1 lands on CLAUDE.md line 2.
        content = f"{_BEGIN}\n{mutated}{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert "template line 1" in str(items[0].expected)
        assert first_line in str(items[0].expected)
        assert "line 2" in str(items[0].actual)
        assert f"{first_line} (edited)" in str(items[0].actual)

    def test_truncated_region_reports_missing_template_line(self, tmp_path: Path) -> None:
        template_lines = _TEMPLATE_TEXT.splitlines()
        truncated = "\n".join(template_lines[:-1]) + "\n"
        content = f"{_BEGIN}\n{truncated}{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert f"template line {len(template_lines)}" in str(items[0].expected)
        assert "end of marked region" in str(items[0].actual)

    def test_extra_region_content_after_template_fails(self, tmp_path: Path) -> None:
        content = f"{_BEGIN}\n{_TEMPLATE_TEXT}## Extra section\n{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert "end of template" in str(items[0].expected)
        assert "## Extra section" in str(items[0].actual)

    def test_begin_without_end_fails(self, tmp_path: Path) -> None:
        content = f"{_BEGIN}\n{_TEMPLATE_TEXT}"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert "end marker" in str(items[0].actual)

    def test_end_without_begin_fails(self, tmp_path: Path) -> None:
        content = f"{_TEMPLATE_TEXT}{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert "begin marker" in str(items[0].actual)

    def test_multiple_begin_markers_fail(self, tmp_path: Path) -> None:
        content = f"{_BEGIN}\n{_BEGIN}\n{_TEMPLATE_TEXT}{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert "multiple begin markers" in str(items[0].actual)

    def test_multiple_end_markers_fail(self, tmp_path: Path) -> None:
        content = f"{_BEGIN}\n{_TEMPLATE_TEXT}{_END}\n{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert "multiple end markers" in str(items[0].actual)

    def test_end_before_begin_fails(self, tmp_path: Path) -> None:
        content = f"{_END}\n{_TEMPLATE_TEXT}{_BEGIN}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1
        assert "before begin marker" in str(items[0].actual)

    def test_markers_take_precedence_over_legacy_substring(self, tmp_path: Path) -> None:
        mutated = _TEMPLATE_TEXT.replace("vrg-git", "vrg-Git", 1)
        # The verbatim template appears outside the marked region, but the
        # marked region itself diverges — markers must win over the legacy
        # contiguous-substring fallback.
        content = f"{_TEMPLATE_TEXT}\n{_BEGIN}\n{mutated}{_END}\n"
        (tmp_path / "CLAUDE.md").write_text(content)
        items = _claude_md_items(tmp_path)
        assert len(items) == 1


_PREVIOUS_TEMPLATE_TEXT = (_TEMPLATE_PATH.parent / "claude_md_consumer_previous.md").read_text(
    encoding="utf-8"
)


class TestClaudeMdBridge:
    """The marked region and the legacy form accept the previous template with
    a warning until the bridge-removal PR (epic vergil-project/.github#558)."""

    @staticmethod
    def _audit(tmp_path: Path, content: str) -> ConfigDiff:
        (tmp_path / "CLAUDE.md").write_text(content)
        return audit_local_config(tmp_path)

    @staticmethod
    def _md_items(diff: ConfigDiff) -> list[DiffItem]:
        return [i for i in diff.items if i.field == "local.claude_md"]

    def test_templates_differ(self) -> None:
        assert _TEMPLATE_TEXT != _PREVIOUS_TEMPLATE_TEXT

    def test_template_within_line_budget(self) -> None:
        assert len(_TEMPLATE_TEXT.splitlines()) <= 60

    def test_claude_md_region_current_clean(self, tmp_path: Path) -> None:
        diff = self._audit(tmp_path, f"# CLAUDE.md\n\n{_BEGIN}\n{_TEMPLATE_TEXT}{_END}\n")
        assert self._md_items(diff) == []
        assert diff.warnings == []

    def test_claude_md_region_previous_warns(self, tmp_path: Path) -> None:
        content = f"# CLAUDE.md\n\n{_BEGIN}\n\n{_PREVIOUS_TEMPLATE_TEXT}\n{_END}\n\n## Body\n"
        diff = self._audit(tmp_path, content)
        assert self._md_items(diff) == []
        assert diff.warnings == [_CLAUDE_MD_WARNING]

    def test_claude_md_legacy_previous_warns(self, tmp_path: Path) -> None:
        content = f"# CLAUDE.md\n\nIntro.\n\n{_PREVIOUS_TEMPLATE_TEXT}\n## Project Overview\n"
        diff = self._audit(tmp_path, content)
        assert self._md_items(diff) == []
        assert diff.warnings == [_CLAUDE_MD_WARNING]

    def test_claude_md_legacy_current_clean(self, tmp_path: Path) -> None:
        diff = self._audit(tmp_path, f"# CLAUDE.md\n\n{_TEMPLATE_TEXT}\n## Project Overview\n")
        assert self._md_items(diff) == []
        assert diff.warnings == []

    def test_claude_md_region_other_fails_against_current(self, tmp_path: Path) -> None:
        mutated = _PREVIOUS_TEMPLATE_TEXT.replace("vrg-git", "vrg-Git", 1)
        diff = self._audit(tmp_path, f"{_BEGIN}\n{mutated}{_END}\n")
        [item] = self._md_items(diff)
        first_line = _TEMPLATE_TEXT.splitlines()[0]
        assert f"template line 1: {first_line}" in str(item.expected)
        assert diff.warnings == []

    def test_claude_md_legacy_other_fails(self, tmp_path: Path) -> None:
        mutated = _PREVIOUS_TEMPLATE_TEXT.replace("vrg-git", "vrg-Git", 1)
        diff = self._audit(tmp_path, mutated)
        [item] = self._md_items(diff)
        assert item.actual == "template not found"
        assert diff.warnings == []


_DATA_DIR = Path(__file__).resolve().parents[2] / "src" / "vergil_tooling" / "data"
_SETTINGS_TEMPLATE = json.loads((_DATA_DIR / "claude_settings.json").read_text(encoding="utf-8"))
_SETTINGS_PREVIOUS = json.loads(
    (_DATA_DIR / "claude_settings_previous.json").read_text(encoding="utf-8")
)

#: The exact settings shape pinned by epic vergil-project/.github#558's plan.
_PINNED_SETTINGS = {
    "permissions": {"allow": ["Bash(vrg-*)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh",
                    }
                ],
            }
        ]
    },
    "enabledPlugins": {"vergil@vergil-marketplace": True},
    "attribution": {"commit": "", "pr": ""},
}

_SETTINGS_WARNING = (
    ".claude/settings.json matches the previous template; replace it with the "
    "current template (epic vergil-project/.github#558)."
)
_CLAUDE_MD_WARNING = (
    "CLAUDE.md template region matches the previous template; replace it with the "
    "current template (epic vergil-project/.github#558)."
)


def _write_settings(tmp_path: Path, settings: object, *, indent: int | None = None) -> None:
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(settings, indent=indent))


def _settings_items(diff: ConfigDiff) -> list[DiffItem]:
    return [i for i in diff.items if i.field.startswith("local.claude_settings")]


class TestSettingsTemplates:
    def test_current_template_is_the_pinned_shape(self) -> None:
        assert _SETTINGS_TEMPLATE == _PINNED_SETTINGS
        assert "extraKnownMarketplaces" not in _SETTINGS_TEMPLATE

    def test_previous_template_is_the_old_shape_with_main_seeded(self) -> None:
        source = _SETTINGS_PREVIOUS["extraKnownMarketplaces"]["vergil-marketplace"]["source"]
        assert source == {
            "source": "github",
            "repo": "vergil-project/vergil-claude-plugin",
            "ref": "main",
        }
        without_market = {
            k: v for k, v in _SETTINGS_PREVIOUS.items() if k != "extraKnownMarketplaces"
        }
        assert without_market == _SETTINGS_TEMPLATE


class TestClaudeSettings:
    def test_missing_file(self, tmp_path: Path) -> None:
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings" in fields

    def test_invalid_json(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text("{bad json")
        diff = audit_local_config(tmp_path)
        matches = [i for i in diff.items if i.field == "local.claude_settings"]
        assert len(matches) == 1
        assert "valid JSON" in str(matches[0].expected)

    def test_not_an_object(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text('"just a string"')
        diff = audit_local_config(tmp_path)
        matches = [i for i in diff.items if i.field == "local.claude_settings"]
        assert len(matches) == 1
        assert "JSON object" in str(matches[0].expected)

    def test_settings_current_template_passes_clean(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, _SETTINGS_TEMPLATE, indent=2)
        diff = audit_local_config(tmp_path)
        assert _settings_items(diff) == []
        assert diff.warnings == []

    def test_settings_key_order_irrelevant(self, tmp_path: Path) -> None:
        reordered = dict(reversed(list(_SETTINGS_TEMPLATE.items())))
        reordered["attribution"] = {"pr": "", "commit": ""}
        assert list(reordered) != list(_SETTINGS_TEMPLATE)
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text(
            json.dumps(reordered, separators=(",", ":"))
        )
        diff = audit_local_config(tmp_path)
        assert _settings_items(diff) == []
        assert diff.warnings == []

    def test_settings_previous_template_warns(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, _SETTINGS_PREVIOUS, indent=4)
        diff = audit_local_config(tmp_path)
        assert _settings_items(diff) == []
        assert diff.warnings == [_SETTINGS_WARNING]

    def test_settings_other_shape_fails(self, tmp_path: Path) -> None:
        # The current template plus a marketplace entry. With the seeded
        # ``ref: main`` that would be exactly the previous template, so this
        # uses the old packaged (ref-less) entry, which matches neither.
        other = {
            **_SETTINGS_TEMPLATE,
            "extraKnownMarketplaces": {
                "vergil-marketplace": {
                    "source": {"source": "github", "repo": "vergil-project/vergil-claude-plugin"}
                }
            },
        }
        _write_settings(tmp_path, other)
        diff = audit_local_config(tmp_path)
        [item] = _settings_items(diff)
        assert item.field == "local.claude_settings"
        assert "unexpected key: extraKnownMarketplaces" in str(item.actual)
        assert "claude_settings.json" in str(item.expected)
        assert diff.warnings == []

    def test_previous_template_with_old_ref_fails(self, tmp_path: Path) -> None:
        stale = json.loads(json.dumps(_SETTINGS_PREVIOUS))
        stale["extraKnownMarketplaces"]["vergil-marketplace"]["source"]["ref"] = "develop"
        _write_settings(tmp_path, stale)
        diff = audit_local_config(tmp_path)
        [item] = _settings_items(diff)
        assert item.actual == "unexpected key: extraKnownMarketplaces"
        assert diff.warnings == []

    def test_missing_key_named(self, tmp_path: Path) -> None:
        partial = {k: v for k, v in _SETTINGS_TEMPLATE.items() if k != "attribution"}
        _write_settings(tmp_path, partial)
        [item] = _settings_items(audit_local_config(tmp_path))
        assert item.actual == "missing key: attribution"

    def test_attribution_false_fails(self, tmp_path: Path) -> None:
        # ``attribution: false`` needs a newer Claude Code and makes older
        # versions skip the whole file, so the empty-string form is required.
        _write_settings(tmp_path, {**_SETTINGS_TEMPLATE, "attribution": False})
        [item] = _settings_items(audit_local_config(tmp_path))
        assert item.actual == "differs: attribution"

    def test_plugin_manager_clobber_detected(self, tmp_path: Path) -> None:
        # Regression for issue #1427: Claude Code's plugin manager rewrote a
        # checked-in settings.json, emptying enabledPlugins.
        _write_settings(tmp_path, {**_SETTINGS_TEMPLATE, "enabledPlugins": {}})
        [item] = _settings_items(audit_local_config(tmp_path))
        assert item.actual == "differs: enabledPlugins"

    def test_several_differences_listed_together(self, tmp_path: Path) -> None:
        settings = {k: v for k, v in _SETTINGS_TEMPLATE.items() if k != "hooks"}
        settings["permissions"] = {"allow": []}
        settings["env"] = {}
        _write_settings(tmp_path, settings)
        [item] = _settings_items(audit_local_config(tmp_path))
        assert item.actual == "missing key: hooks; unexpected key: env; differs: permissions"


def _write_compliant_repo(root: Path) -> None:
    """Scaffold a fully compliant repo structure."""
    (root / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML_VER.format(version="v2.1"))
    hooks_dir = root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "guard.sh").write_text("#!/usr/bin/env bash\nexec vrg-hook-guard\n")
    (root / "CLAUDE.md").write_text("# CLAUDE.md\n\n" + _TEMPLATE_TEXT + "\n")
    (root / ".claude" / "settings.json").write_text(json.dumps(_SETTINGS_TEMPLATE))
    # A fully compliant repo carries the fenced managed .gitignore for its
    # declared language (python) and a wired, scheduled ops.yml (epic
    # vergil-project/.github#311, #325). The ops.yml ref is pinned to the repo's
    # declared vergil version (v2.1) so _check_workflow_refs stays clean.
    (root / ".gitignore").write_text(gitignore.render_block("python"), encoding="utf-8")
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "ops.yml").write_text(_SCHED + "\njobs:\n" + _WIRED, encoding="utf-8")
    # ...and the canonical report-only dependabot.yml for python (epic #420).
    (root / dependabot.DEPENDABOT_PATH).write_text(dependabot.render("python"), encoding="utf-8")


class TestIntegration:
    def test_empty_directory_reports_all_missing(self, tmp_path: Path) -> None:
        diff = audit_local_config(tmp_path)
        assert not diff.is_compliant()
        fields = {i.field for i in diff.items}
        assert "local.vergil_toml" in fields
        assert "local.hook_guard_shim" in fields
        assert "local.claude_md" in fields
        assert "local.claude_settings" in fields

    def test_compliant_repo(self, tmp_path: Path) -> None:
        _write_compliant_repo(tmp_path)
        diff = audit_local_config(tmp_path)
        assert diff.is_compliant(), [f"{i.field}: {i.actual}" for i in diff.items]
        assert diff.warnings == []

    def test_previous_shapes_keep_repo_compliant_with_both_warnings(self, tmp_path: Path) -> None:
        # The point of the bridge: a repo not yet swept still passes the
        # nightly audit, with one deprecation warning per previous-shape file.
        _write_compliant_repo(tmp_path)
        (tmp_path / "CLAUDE.md").write_text("# CLAUDE.md\n\n" + _PREVIOUS_TEMPLATE_TEXT + "\n")
        (tmp_path / ".claude" / "settings.json").write_text(json.dumps(_SETTINGS_PREVIOUS))
        diff = audit_local_config(tmp_path)
        assert diff.is_compliant(), [f"{i.field}: {i.actual}" for i in diff.items]
        assert diff.warnings == [_CLAUDE_MD_WARNING, _SETTINGS_WARNING]


class TestWorkflowRefs:
    def _write_wf(self, base: Path, version: str, pin: str) -> None:
        (base / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML_VER.format(version=version))
        wf = base / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text(
            f"jobs:\n  a:\n    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@{pin}\n"
        )

    def test_matching_pin_clean(self, tmp_path: Path) -> None:
        self._write_wf(tmp_path, "v2.0", "v2.0")
        diff = audit_local_config(tmp_path)
        assert not [i for i in diff.items if i.field == "local.workflow_ref"]

    def test_drifted_pin_flagged(self, tmp_path: Path) -> None:
        self._write_wf(tmp_path, "v2.1", "v2.0")
        diff = audit_local_config(tmp_path)
        flagged = [i for i in diff.items if i.field == "local.workflow_ref"]
        assert len(flagged) == 1
        assert "v2.1" in str(flagged[0].expected)
        assert "v2.0" in str(flagged[0].actual)

    def test_no_workflows_no_flag(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML_VER.format(version="v2.0"))
        diff = audit_local_config(tmp_path)
        assert not [i for i in diff.items if i.field == "local.workflow_ref"]


def _write_gitignore(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


class TestGitignorePatterns:
    def test_baseline_has_required_patterns(self) -> None:
        # The composed base + python set (and the fleet-wide managed vocabulary)
        # must carry these canonical patterns (epic vergil-project/.github#325).
        composed = gitignore.compose("python")
        vocab = gitignore.managed_vocabulary()
        for required in (".venv/", ".worktrees/", "quality-ruff.json", "docs/site/site/"):
            assert required in composed
            assert required in vocab


_VERGIL_TOML_NO_LANG = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[project.co-authors]

[ci]
versions = ["3.12"]

[dependencies]
vergil = "v2.0.7"
"""


class TestCheckGitignore:
    """Fenced-only acceptance: only a correct ``base + <language>`` fence passes.

    The transitional legacy-superset arm was removed in Task 10 (epic #325) once
    the whole fleet was fenced; base + all-fragments is now the single source of
    truth for what the audit accepts.
    """

    def test_fenced_python_block_passes(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        _write_gitignore(tmp_path / ".gitignore", gitignore.render_block("python"))
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        assert items == []

    def test_base_only_fence_on_no_language_repo_passes(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_VERGIL_TOML_NO_LANG)
        _write_gitignore(tmp_path / ".gitignore", gitignore.render_block(None))
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        assert items == []

    def test_neither_form_fails(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        _write_gitignore(tmp_path / ".gitignore", "just-some-random-line/\nanother\n")
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        assert items, "expected a DiffItem when the fence is absent"
        assert {i.field for i in items} == {"local.gitignore"}

    def test_unfenced_managed_patterns_rejected(self, tmp_path: Path) -> None:
        # The core of the Task-10 tightening: an unfenced file that carries the
        # managed patterns loose (the old legacy-superset form) must now FAIL —
        # a bare, comment-free superset no longer satisfies the audit.
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        loose = "\n".join(sorted(gitignore.managed_vocabulary())) + "\n"
        _write_gitignore(tmp_path / ".gitignore", loose)
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        assert items, "fenced-only mode must reject an unfenced managed superset"
        assert {i.field for i in items} == {"local.gitignore"}

    def test_absent_gitignore_fails(self, tmp_path: Path) -> None:
        # No .gitignore at all: the fence is missing, so the repo is non-compliant.
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        assert items, "an absent .gitignore has no fence and must fail"
        assert {i.field for i in items} == {"local.gitignore"}

    def test_unreadable_config_is_an_explicit_failure(self, tmp_path: Path) -> None:
        # #3451: a vergil.toml that fails to load must not resolve to base-only
        # and pass a base-only fence; the audit names the config failure instead.
        (tmp_path / "vergil.toml").write_text("not = [valid toml")
        _write_gitignore(tmp_path / ".gitignore", gitignore.render_block(None))
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        assert len(items) == 1
        assert items[0].field == "local.gitignore"
        assert items[0].expected == "resolvable [project].primary-language"
        assert items[0].actual.startswith("cannot resolve the fence language: ")
        assert "not valid TOML" in items[0].actual

    def test_absent_config_is_base_only(self, tmp_path: Path) -> None:
        _write_gitignore(tmp_path / ".gitignore", gitignore.render_block(None))
        items: list = []
        repo_config._check_gitignore(tmp_path, items)
        assert items == []


class TestGitignoreLanguage:
    def test_config_error_propagates(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text("not = [valid toml")
        with pytest.raises(ConfigError, match="not valid TOML"):
            repo_config._gitignore_language(tmp_path)

    def test_absent_config_is_none(self, tmp_path: Path) -> None:
        assert repo_config._gitignore_language(tmp_path) is None

    def test_undeclared_language_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_VERGIL_TOML_NO_LANG)
        assert repo_config._gitignore_language(tmp_path) is None


def _write_dependabot(root: Path, text: str) -> None:
    path = root / dependabot.DEPENDABOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestCheckDependabotYml:
    def test_audit_dependabot_canonical_passes(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        _write_dependabot(tmp_path, dependabot.render("python"))
        items: list = []
        repo_config._check_dependabot_yml(tmp_path, items)
        assert items == []

    def test_audit_dependabot_absent_config_is_actions_only(self, tmp_path: Path) -> None:
        _write_dependabot(tmp_path, dependabot.render(None))
        items: list = []
        repo_config._check_dependabot_yml(tmp_path, items)
        assert items == []

    def test_audit_dependabot_missing(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        items: list = []
        repo_config._check_dependabot_yml(tmp_path, items)
        assert len(items) == 1
        assert items[0].field == "local.dependabot"
        assert items[0].expected == "canonical report-only dependabot.yml"
        assert items[0].actual == "missing .github/dependabot.yml"

    def test_audit_dependabot_noncanonical(self, tmp_path: Path) -> None:
        # The actions-only render is non-canonical for a python repo: it drops
        # the uv ecosystem.
        (tmp_path / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
        _write_dependabot(tmp_path, dependabot.render(None))
        items: list = []
        repo_config._check_dependabot_yml(tmp_path, items)
        assert len(items) == 1
        assert items[0].field == "local.dependabot"
        assert items[0].actual == "differs from the canonical render"

    def test_audit_dependabot_unreadable_config_is_an_explicit_failure(
        self, tmp_path: Path
    ) -> None:
        # #3451: a vergil.toml that fails to load must not resolve to the
        # actions-only render and pass it; the audit names the config failure.
        (tmp_path / "vergil.toml").write_text("not = [valid toml")
        _write_dependabot(tmp_path, dependabot.render(None))
        items: list = []
        repo_config._check_dependabot_yml(tmp_path, items)
        assert len(items) == 1
        assert items[0].field == "local.dependabot"
        assert items[0].expected == "resolvable [project].primary-language"
        assert items[0].actual.startswith("cannot resolve the dependabot language: ")
        assert "not valid TOML" in items[0].actual


_WIRED = (
    "  github-config:\n"
    "    uses: vergil-project/vergil-actions/.github/workflows/ops-github-config.yml@v2.1\n"
)
_SCHED = "on:\n  schedule:\n    - cron: '7 6 * * *'\n  workflow_dispatch:\n"


def _write_ops(dir_: Path, body: str) -> None:
    wf = dir_ / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "ops.yml").write_text(body, encoding="utf-8")


class TestCheckRequiredWorkflows:
    def test_present_wired_scheduled_passes(self, tmp_path: Path) -> None:
        _write_ops(tmp_path, _SCHED + "\njobs:\n" + _WIRED)
        items: list = []
        repo_config._check_required_workflows(tmp_path, items)
        assert items == []

    def test_absent_fails(self, tmp_path: Path) -> None:
        items: list = []
        repo_config._check_required_workflows(tmp_path, items)
        assert [i.field for i in items] == ["local.ops_workflow"]
        assert items[0].actual == "missing"

    def test_present_but_not_wired_fails(self, tmp_path: Path) -> None:
        _write_ops(
            tmp_path,
            _SCHED
            + "\njobs:\n  x:\n"
            + "    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@v2.1\n",
        )
        items: list = []
        repo_config._check_required_workflows(tmp_path, items)
        assert all(i.field == "local.ops_workflow" for i in items)
        assert any("does not wire" in i.actual for i in items)

    def test_wired_but_unscheduled_fails(self, tmp_path: Path) -> None:
        _write_ops(tmp_path, "on:\n  workflow_dispatch:\n\njobs:\n" + _WIRED)
        items: list = []
        repo_config._check_required_workflows(tmp_path, items)
        assert all(i.field == "local.ops_workflow" for i in items)
        assert any("no schedule" in i.actual for i in items)
