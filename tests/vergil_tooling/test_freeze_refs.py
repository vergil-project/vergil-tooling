"""Tests for vergil_tooling.lib.freeze_refs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.freeze_refs import (
    Finding,
    collect_yaml_files,
    freeze_references,
    validate_no_unfrozen,
)

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "vergil-project/vergil-actions"
_TAG = "v2.0.50"


class TestCollectYamlFiles:
    def test_collects_yml_and_yaml(self, tmp_path: Path) -> None:
        d = tmp_path / "workflows"
        d.mkdir()
        (d / "ci.yml").write_text("")
        (d / "cd.yaml").write_text("")
        (d / "notes.txt").write_text("")
        result = collect_yaml_files([d])
        names = [p.name for p in result]
        assert "ci.yml" in names
        assert "cd.yaml" in names
        assert "notes.txt" not in names

    def test_skips_missing_dirs(self, tmp_path: Path) -> None:
        result = collect_yaml_files([tmp_path / "nonexistent"])
        assert result == []

    def test_deduplicates(self, tmp_path: Path) -> None:
        d = tmp_path / "dir"
        d.mkdir()
        (d / "a.yml").write_text("")
        result = collect_yaml_files([d, d])
        assert len(result) == 1

    def test_recurses_subdirs(self, tmp_path: Path) -> None:
        sub = tmp_path / "actions" / "local"
        sub.mkdir(parents=True)
        (sub / "action.yml").write_text("")
        result = collect_yaml_files([tmp_path / "actions"])
        assert len(result) == 1


class TestFreezeReferences:
    def test_freezes_relative_ref(self) -> None:
        content = "    uses: ./actions/local/setup/action.yml"
        result = freeze_references(content, _OWNER, _TAG)
        assert result == f"    uses: {_OWNER}/actions/local/setup/action.yml@{_TAG}"

    def test_freezes_relative_workflow_ref(self) -> None:
        # A relative nested-reusable-workflow ref must also be fully-qualified so
        # it resolves when the reusable workflow is called cross-repo.
        content = "    uses: ./.github/workflows/cd-docs.yml"
        result = freeze_references(content, _OWNER, _TAG)
        assert result == f"    uses: {_OWNER}/.github/workflows/cd-docs.yml@{_TAG}"

    def test_freezes_develop_ref(self) -> None:
        content = f"    uses: {_OWNER}/.github/workflows/ci.yml@develop"
        result = freeze_references(content, _OWNER, _TAG)
        assert result == f"    uses: {_OWNER}/.github/workflows/ci.yml@{_TAG}"

    def test_ignores_non_uses_lines(self) -> None:
        content = "# ./actions/local/setup is important"
        result = freeze_references(content, _OWNER, _TAG)
        assert result == content

    def test_preserves_already_frozen(self) -> None:
        content = f"    uses: {_OWNER}/.github/workflows/ci.yml@v1.0.0"
        result = freeze_references(content, _OWNER, _TAG)
        assert result == content

    def test_preserves_sha_pinned(self) -> None:
        content = f"    uses: {_OWNER}/.github/workflows/ci.yml@abc123def"
        result = freeze_references(content, _OWNER, _TAG)
        assert result == content

    def test_freezes_multiple_lines(self) -> None:
        content = (
            "jobs:\n"
            "  build:\n"
            "    uses: ./actions/setup\n"
            f"    uses: {_OWNER}/actions/lint@develop\n"
            "    run: echo hello\n"
        )
        result = freeze_references(content, _OWNER, _TAG)
        assert "./actions/" not in result
        assert "@develop" not in result
        assert f"@{_TAG}" in result

    def test_different_owner_not_frozen(self) -> None:
        content = "    uses: other-org/other-repo/.github/workflows/ci.yml@develop"
        result = freeze_references(content, _OWNER, _TAG)
        assert "@develop" in result


class TestValidateNoUnfrozen:
    def test_clean_file(self) -> None:
        content = f"    uses: {_OWNER}/actions/setup@{_TAG}"
        findings = validate_no_unfrozen(content, "ci.yml", _OWNER)
        assert findings == []

    def test_detects_relative_ref(self) -> None:
        content = "    uses: ./actions/setup"
        findings = validate_no_unfrozen(content, "ci.yml", _OWNER)
        assert len(findings) == 1
        assert findings[0] == Finding(file="ci.yml", line=1, text="uses: ./actions/setup")

    def test_detects_relative_workflow_ref(self) -> None:
        content = "    uses: ./.github/workflows/cd-docs.yml"
        findings = validate_no_unfrozen(content, "cd-docs-refresh.yml", _OWNER)
        assert len(findings) == 1
        assert findings[0].line == 1

    def test_detects_develop_ref(self) -> None:
        content = f"    uses: {_OWNER}/actions/setup@develop"
        findings = validate_no_unfrozen(content, "ci.yml", _OWNER)
        assert len(findings) == 1
        assert findings[0].line == 1

    def test_ignores_non_uses_lines(self) -> None:
        content = "# reference to ./actions/setup"
        findings = validate_no_unfrozen(content, "ci.yml", _OWNER)
        assert findings == []

    def test_multiple_findings(self) -> None:
        content = "    uses: ./actions/a\n    uses: ./actions/b"
        findings = validate_no_unfrozen(content, "ci.yml", _OWNER)
        assert len(findings) == 2
        assert findings[0].line == 1
        assert findings[1].line == 2

    def test_different_owner_ignored(self) -> None:
        content = "    uses: other/repo/actions/setup@develop"
        findings = validate_no_unfrozen(content, "ci.yml", _OWNER)
        assert findings == []
