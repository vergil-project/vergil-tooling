"""Tests for vergil_tooling.lib.semgrep."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib.semgrep import (
    DEFAULT_EXCLUDED_RULES,
    ScanResult,
    resolve_rulesets,
    run_scan,
)

_MUTABLE_ACTION_TAG_RULE = (
    "yaml.github-actions.security.github-actions-mutable-action-tag"
    ".github-actions-mutable-action-tag"
)

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.semgrep"


class TestResolveRulesets:
    def test_python(self) -> None:
        assert resolve_rulesets("python") == ["p/python"]

    def test_go_maps_to_golang(self) -> None:
        assert resolve_rulesets("go") == ["p/golang"]

    def test_java(self) -> None:
        assert resolve_rulesets("java") == ["p/java"]

    def test_ruby(self) -> None:
        assert resolve_rulesets("ruby") == ["p/ruby"]

    def test_rust(self) -> None:
        assert resolve_rulesets("rust") == ["p/rust"]

    def test_unknown_language(self) -> None:
        assert resolve_rulesets("cobol") == []

    def test_dockerfiles(self) -> None:
        rulesets = resolve_rulesets("python", has_dockerfiles=True)
        assert "p/dockerfile" in rulesets

    def test_workflows(self) -> None:
        rulesets = resolve_rulesets("python", has_workflows=True)
        assert "p/github-actions" in rulesets

    def test_extra_config(self) -> None:
        rulesets = resolve_rulesets("python", extra_config=["p/security-audit"])
        assert "p/security-audit" in rulesets

    def test_all_options(self) -> None:
        rulesets = resolve_rulesets(
            "go",
            has_dockerfiles=True,
            has_workflows=True,
            extra_config=["p/custom"],
        )
        assert rulesets == ["p/golang", "p/dockerfile", "p/github-actions", "p/custom"]


class TestRunScan:
    def test_successful_scan(self, tmp_path: Path) -> None:
        output = tmp_path / "results.sarif"
        output.write_text('{"runs": []}')

        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = run_scan(["p/python"], tmp_path, output)

        assert result == ScanResult(returncode=0, sarif_produced=True)
        cmd = mock_run.call_args[0][0]
        assert "semgrep" in cmd
        assert "--config" in cmd
        assert "p/python" in cmd

    def test_findings_exit_code(self, tmp_path: Path) -> None:
        output = tmp_path / "results.sarif"
        output.write_text('{"runs": []}')

        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            result = run_scan(["p/python"], tmp_path, output)

        assert result.returncode == 1
        assert result.sarif_produced

    def test_no_sarif_produced(self, tmp_path: Path) -> None:
        output = tmp_path / "results.sarif"

        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = run_scan(["p/python"], tmp_path, output)

        assert not result.sarif_produced

    def test_multiple_rulesets(self, tmp_path: Path) -> None:
        output = tmp_path / "results.sarif"

        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan(["p/python", "p/dockerfile"], tmp_path, output)

        cmd = mock_run.call_args[0][0]
        config_indices = [i for i, v in enumerate(cmd) if v == "--config"]
        assert len(config_indices) == 2

    @staticmethod
    def _excluded_from(cmd: list[str]) -> list[str]:
        return [cmd[i + 1] for i, v in enumerate(cmd) if v == "--exclude-rule"]

    def test_mutable_action_tag_is_a_fleet_default(self) -> None:
        assert _MUTABLE_ACTION_TAG_RULE in DEFAULT_EXCLUDED_RULES

    def test_default_excludes_mutable_action_tag(self, tmp_path: Path) -> None:
        output = tmp_path / "results.sarif"

        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan(["p/python"], tmp_path, output)

        excluded = self._excluded_from(mock_run.call_args[0][0])
        assert _MUTABLE_ACTION_TAG_RULE in excluded

    def test_caller_supplied_added_to_defaults(self, tmp_path: Path) -> None:
        output = tmp_path / "results.sarif"

        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan(
                ["p/python"],
                tmp_path,
                output,
                exclude_rules=["custom.rule.id"],
            )

        excluded = self._excluded_from(mock_run.call_args[0][0])
        # Caller-supplied rule is added IN ADDITION to the fleet defaults.
        assert "custom.rule.id" in excluded
        assert _MUTABLE_ACTION_TAG_RULE in excluded

    def test_caller_supplied_default_is_not_duplicated(self, tmp_path: Path) -> None:
        output = tmp_path / "results.sarif"

        with patch(f"{_MOD}.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            run_scan(
                ["p/python"],
                tmp_path,
                output,
                exclude_rules=[_MUTABLE_ACTION_TAG_RULE],
            )

        excluded = self._excluded_from(mock_run.call_args[0][0])
        assert excluded.count(_MUTABLE_ACTION_TAG_RULE) == 1
