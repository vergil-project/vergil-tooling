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


class TestGithooks:
    def test_missing(self, tmp_path: Path) -> None:
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.githooks_pre_commit" in fields

    def test_present(self, tmp_path: Path) -> None:
        (tmp_path / ".githooks").mkdir()
        (tmp_path / ".githooks" / "pre-commit").write_text("#!/bin/sh\n")
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.githooks_pre_commit" not in fields


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
    "permissions": {
        "deny": [
            "Bash(git *)",
            "Bash(*/git *)",
            "Bash(gh *)",
            "Bash(*/gh *)",
        ]
    },
}


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
        assert "local.claude_settings.marketplace_repo" in fields

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

    def test_missing_deny_rules(self, tmp_path: Path) -> None:
        settings = {**_MINIMAL_SETTINGS, "permissions": {"deny": []}}
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.deny_rules" in fields

    def test_partial_deny_rules(self, tmp_path: Path) -> None:
        settings = {**_MINIMAL_SETTINGS, "permissions": {"deny": ["Bash(git *)"]}}
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        matches = [i for i in diff.items if i.field == "local.claude_settings.deny_rules"]
        assert len(matches) == 1

    def test_all_four_deny_rules(self, tmp_path: Path) -> None:
        _write_settings(tmp_path, _MINIMAL_SETTINGS)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.deny_rules" not in fields

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

    def test_permissions_not_an_object(self, tmp_path: Path) -> None:
        settings = {**_MINIMAL_SETTINGS, "permissions": "wrong type"}
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.deny_rules" in fields

    def test_deny_not_a_list(self, tmp_path: Path) -> None:
        settings = {**_MINIMAL_SETTINGS, "permissions": {"deny": "wrong type"}}
        _write_settings(tmp_path, settings)
        diff = audit_local_config(tmp_path)
        fields = {i.field for i in diff.items}
        assert "local.claude_settings.deny_rules" in fields

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
    (root / ".githooks").mkdir()
    (root / ".githooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    (root / "CLAUDE.md").write_text("# CLAUDE.md\n\n" + _TEMPLATE_TEXT + "\n")
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.json").write_text(json.dumps(_MINIMAL_SETTINGS))


class TestIntegration:
    def test_empty_directory_reports_all_missing(self, tmp_path: Path) -> None:
        diff = audit_local_config(tmp_path)
        assert not diff.is_compliant()
        fields = {i.field for i in diff.items}
        assert "local.vergil_toml" in fields
        assert "local.githooks_pre_commit" in fields
        assert "local.claude_md" in fields
        assert "local.claude_settings" in fields

    def test_compliant_repo(self, tmp_path: Path) -> None:
        _write_compliant_repo(tmp_path)
        diff = audit_local_config(tmp_path)
        assert diff.is_compliant(), [f"{i.field}: {i.actual}" for i in diff.items]
