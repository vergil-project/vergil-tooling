"""Tests for local repo config audit checks."""

from __future__ import annotations

import json
from pathlib import Path

from vergil_tooling.lib.repo_config import audit_local_config

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


_MINIMAL_SETTINGS = {
    "extraKnownMarketplaces": {
        "vergil-marketplace": {
            "source": {
                "source": "github",
                "repo": "vergil-project/vergil-claude-plugin",
            }
        }
    },
    "enabledPlugins": {
        "vergil@vergil-marketplace": True,
    },
}

_SETTINGS_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "vergil_tooling" / "data" / "claude_settings.json"
)
_SETTINGS_TEMPLATE = json.loads(_SETTINGS_TEMPLATE_PATH.read_text(encoding="utf-8"))


def _write_settings(tmp_path: Path, settings: dict) -> None:
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps(settings))


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

    def test_missing_marketplace(self, tmp_path: Path) -> None:
        settings = {"enabledPlugins": {"vergil@vergil-marketplace": True}}
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.marketplace" in fields

    def test_wrong_marketplace_repo(self, tmp_path: Path) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "vergil-marketplace": {"source": {"source": "github", "repo": "wrong/repo"}}
            },
            "enabledPlugins": {"vergil@vergil-marketplace": True},
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.marketplace" in fields

    def test_marketplace_source_drift(self, tmp_path: Path) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "vergil-marketplace": {
                    "source": {"source": "git", "repo": "vergil-project/vergil-claude-plugin"}
                }
            },
            "enabledPlugins": {"vergil@vergil-marketplace": True},
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.marketplace" in fields

    def test_plugin_truthy_but_not_true(self, tmp_path: Path) -> None:
        settings = {
            **_MINIMAL_SETTINGS,
            "enabledPlugins": {"vergil@vergil-marketplace": "yes"},
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.plugin" in fields

    def test_plugin_manager_clobber_detected(self, tmp_path: Path) -> None:
        # Regression for issue #1427: Claude Code's plugin manager
        # rewrote a checked-in settings.json, emptying both keys.
        settings = {
            "permissions": {"allow": ["Bash(vrg-*)"]},
            "extraKnownMarketplaces": {},
            "enabledPlugins": {},
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.marketplace" in fields
        assert "local.claude_settings.plugin" in fields

    def test_canonical_template_sections_compliant(self, tmp_path: Path) -> None:
        settings = {
            "extraKnownMarketplaces": _SETTINGS_TEMPLATE["extraKnownMarketplaces"],
            "enabledPlugins": _SETTINGS_TEMPLATE["enabledPlugins"],
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        settings_fields = {
            i.field for i in diff.items if i.field.startswith("local.claude_settings")
        }
        assert not settings_fields

    def test_extra_entries_allowed(self, tmp_path: Path) -> None:
        settings = {
            "extraKnownMarketplaces": {
                **_SETTINGS_TEMPLATE["extraKnownMarketplaces"],
                "other-marketplace": {"source": {"source": "github", "repo": "other/repo"}},
            },
            "enabledPlugins": {
                **_SETTINGS_TEMPLATE["enabledPlugins"],
                "other@other-marketplace": True,
            },
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        settings_fields = {
            i.field for i in diff.items if i.field.startswith("local.claude_settings")
        }
        assert not settings_fields

    def test_plugin_not_enabled(self, tmp_path: Path) -> None:
        settings = {
            "extraKnownMarketplaces": {
                "vergil-marketplace": {
                    "source": {"source": "github", "repo": "vergil-project/vergil-claude-plugin"}
                }
            },
            "enabledPlugins": {},
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.plugin" in fields

    def test_marketplace_not_an_object(self, tmp_path: Path) -> None:
        settings = {**_MINIMAL_SETTINGS, "extraKnownMarketplaces": "wrong type"}
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.marketplace" in fields

    def test_marketplace_source_not_an_object(self, tmp_path: Path) -> None:
        settings = {
            **_MINIMAL_SETTINGS,
            "extraKnownMarketplaces": {"vergil-marketplace": {"source": "bad"}},
        }
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.marketplace" in fields

    def test_plugins_not_an_object(self, tmp_path: Path) -> None:
        settings = {**_MINIMAL_SETTINGS, "enabledPlugins": "wrong type"}
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.plugin" in fields

    def test_compliant_settings(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, _MINIMAL_SETTINGS)
        diff = audit_local_config(tmp_path)
        settings_fields = {
            i.field for i in diff.items if i.field.startswith("local.claude_settings")
        }
        assert not settings_fields


def _write_compliant_repo(root: Path) -> None:
    """Scaffold a fully compliant repo structure."""
    (root / "vergil.toml").write_text(_MINIMAL_VERGIL_TOML)
    hooks_dir = root / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "guard.sh").write_text("#!/usr/bin/env bash\nexec vrg-hook-guard\n")
    (root / "CLAUDE.md").write_text("# CLAUDE.md\n\n" + _TEMPLATE_TEXT + "\n")
    (root / ".claude" / "settings.json").write_text(json.dumps(_MINIMAL_SETTINGS))


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
