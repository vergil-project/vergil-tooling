"""Tests for vergil_tooling.bin.vrg_github_repo_config."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from vergil_tooling.bin.vrg_github_repo_config import (
    _apply_repo,
    _audit_repo,
    _fetch_remote_config,
    _load_local_config,
    _resolve_repo,
    main,
    parse_args,
)
from vergil_tooling.lib.config import (
    DEFAULT_VALIDATION_COMMAND,
    CiConfig,
    ContainerConfig,
    MarkdownlintConfig,
    ProjectConfig,
    PublishConfig,
    ValidationConfig,
    VergilConfig,
)
from vergil_tooling.lib.github_config import ConfigDiff, DiffItem

# -- Argument parsing ---------------------------------------------------------


def test_parse_audit_single_repo() -> None:
    args = parse_args(["audit", "--repo", "o/r"])
    assert args.command == "audit"
    assert args.repo == "o/r"


def test_parse_diff_single_repo() -> None:
    args = parse_args(["diff", "--repo", "o/r"])
    assert args.command == "diff"


def test_parse_apply_single_repo() -> None:
    args = parse_args(["apply", "--repo", "o/r"])
    assert args.command == "apply"


def test_parse_no_target_defaults() -> None:
    args = parse_args(["audit"])
    assert args.repo is None


def test_parse_config_flag() -> None:
    args = parse_args(["audit", "--repo", "o/r", "--config", "local/vergil.toml"])
    assert args.config == "local/vergil.toml"


def test_parse_config_flag_absent() -> None:
    args = parse_args(["audit", "--repo", "o/r"])
    assert args.config is None


def test_parse_no_command_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--repo", "o/r"])


def test_parse_owner_flag_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["audit", "--owner", "acme"])


def test_parse_project_flag_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["audit", "--project", "3"])


# -- _resolve_repo ------------------------------------------------------------


def test_resolve_repo_explicit() -> None:
    args = argparse.Namespace(repo="o/r")
    assert _resolve_repo(args) == "o/r"


def test_resolve_repo_defaults_to_current() -> None:
    args = argparse.Namespace(repo=None)
    with patch(
        "vergil_tooling.bin.vrg_github_repo_config.github.current_repo",
        return_value="acme/my-repo",
    ):
        assert _resolve_repo(args) == "acme/my-repo"


# -- Helpers ------------------------------------------------------------------

_MODULE = "vergil_tooling.bin.vrg_github_repo_config"


def _mock_local_compliant() -> ConfigDiff:
    return ConfigDiff(items=[])


def _mock_local_noncompliant() -> ConfigDiff:
    return ConfigDiff(
        items=[DiffItem(field="local.vergil_toml", expected="present", actual="missing")]
    )


def _mock_github_compliant() -> ConfigDiff:
    return ConfigDiff(items=[])


def _mock_github_noncompliant() -> ConfigDiff:
    return ConfigDiff(
        items=[
            DiffItem(field="repo_settings.allow_auto_merge", expected=False, actual=True),
        ]
    )


# -- Audit/diff mode (combined local + GitHub) --------------------------------


def test_audit_both_compliant_returns_zero() -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        assert main(["audit", "--repo", "o/r"]) == 0


def test_audit_local_noncompliant_returns_one() -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_noncompliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        assert main(["audit", "--repo", "o/r"]) == 1


def test_audit_github_noncompliant_returns_one() -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_noncompliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        assert main(["audit", "--repo", "o/r"]) == 1


def test_diff_always_returns_zero() -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_noncompliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_noncompliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        assert main(["diff", "--repo", "o/r"]) == 0


def test_audit_runs_local_checks_first(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_noncompliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_noncompliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "o/r"])
    output = capsys.readouterr().out
    local_pos = output.find("local:")
    github_pos = output.find("o/r:")
    assert local_pos < github_pos, "Local results should print before GitHub results"


# -- Apply mode ---------------------------------------------------------------


def test_apply_all_compliant_does_nothing() -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
        patch(f"{_MODULE}._apply_repo") as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 0
    mock_apply.assert_not_called()


def test_apply_github_noncompliant_applies() -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_noncompliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
        patch(f"{_MODULE}._apply_repo", return_value=[]) as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 0
    mock_apply.assert_called_once()


def test_apply_returns_one_when_local_issues_remain() -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_noncompliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 1


def test_apply_reports_legacy_protection_removed(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_noncompliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
        patch(f"{_MODULE}._apply_repo", return_value=["main", "develop"]),
    ):
        main(["apply", "--repo", "o/r"])
    output = capsys.readouterr().out
    assert "legacy protection removed" in output


def test_audit_prints_skipped_fields(capsys: pytest.CaptureFixture[str]) -> None:
    diff = ConfigDiff(
        items=[],
        skipped=[
            "security.secret_scanning",
            "security.secret_scanning_push_protection",
        ],
    )
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=diff),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        result = main(["audit", "--repo", "o/r"])
    assert result == 0
    output = capsys.readouterr().out
    assert "secret_scanning: skipped" in output
    assert "secret_scanning_push_protection: skipped" in output
    assert "requires GitHub Advanced Security" in output


def test_audit_compliant_public_repo_no_skipped(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "o/r"])
    output = capsys.readouterr().out
    assert "skipped" not in output


def test_audit_non_security_skipped_fields_not_printed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diff = ConfigDiff(items=[], skipped=["repo_settings.allow_forking"])
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=diff),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "o/r"])
    output = capsys.readouterr().out
    assert "skipped" not in output


# -- --config flag integration ------------------------------------------------

_VALID_TOML = b"""\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


def test_config_flag_bypasses_remote_fetch(tmp_path: Path) -> None:
    p = tmp_path / "vergil.toml"
    p.write_bytes(_VALID_TOML)

    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config") as mock_remote,
    ):
        result = main(["audit", "--repo", "o/r", "--config", str(p)])
    assert result == 0
    mock_remote.assert_not_called()


# -- _fetch_remote_config ----------------------------------------------------


def test_fetch_remote_config_success() -> None:
    import base64

    encoded = base64.b64encode(_VALID_TOML).decode()
    with patch(
        f"{_MODULE}.github.read_json",
        return_value={"content": encoded},
    ):
        cfg = _fetch_remote_config("o/r")
    assert cfg.project.primary_language == "python"


def test_fetch_remote_config_non_dict_response() -> None:
    with (
        patch(f"{_MODULE}.github.read_json", return_value=[]),
        pytest.raises(RuntimeError, match="Unexpected response"),
    ):
        _fetch_remote_config("o/r")


def test_fetch_remote_config_no_content_field() -> None:
    with (
        patch(f"{_MODULE}.github.read_json", return_value={"encoding": "base64"}),
        pytest.raises(RuntimeError, match="No content field"),
    ):
        _fetch_remote_config("o/r")


# -- _load_local_config -------------------------------------------------------


def test_load_local_config_success(tmp_path: Path) -> None:
    p = tmp_path / "vergil.toml"
    p.write_bytes(_VALID_TOML)
    cfg = _load_local_config(str(p))
    assert cfg.project.primary_language == "python"


# -- _audit_repo --------------------------------------------------------------


def _make_config() -> VergilConfig:
    return VergilConfig(
        project=ProjectConfig(
            repository_type="library",
            versioning_scheme="semver",
            branching_model="library-release",
            release_model="tagged-release",
            primary_language="python",
        ),
        dependencies={"vergil": "v2.0"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=CiConfig(versions=["3.14"], integration_tests=False),
        publish=PublishConfig(release=False, docs=True, consumer_refresh=None),
        container=ContainerConfig(env_prefixes=[]),
        validation=ValidationConfig(container_command=DEFAULT_VALIDATION_COMMAND),
    )


def test_audit_repo_calls_pipeline() -> None:
    cfg = _make_config()
    with (
        patch(f"{_MODULE}.fetch_actual_state") as mock_fetch,
        patch(f"{_MODULE}.compute_desired_state"),
        patch(f"{_MODULE}.compute_diff", return_value=ConfigDiff(items=[])) as mock_diff,
    ):
        result = _audit_repo("o/r", cfg)
    mock_fetch.assert_called_once_with("o/r")
    mock_diff.assert_called_once()
    assert result.is_compliant()


# -- _apply_repo --------------------------------------------------------------


def test_apply_repo_calls_pipeline() -> None:
    cfg = _make_config()
    with (
        patch(f"{_MODULE}.fetch_actual_state") as mock_fetch,
        patch(f"{_MODULE}.compute_desired_state"),
        patch(f"{_MODULE}.apply_desired_state", return_value=[]) as mock_apply,
    ):
        result = _apply_repo("o/r", cfg)
    mock_fetch.assert_called_once_with("o/r")
    mock_apply.assert_called_once()
    assert result == []


# -- Local check skip on --repo mismatch --------------------------------------


def test_audit_skips_local_checks_on_repo_mismatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MODULE}.github.current_repo", return_value="local/repo"),
        patch(f"{_MODULE}.audit_local_config") as mock_local,
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        result = main(["audit", "--repo", "other/repo"])
    mock_local.assert_not_called()
    assert result == 0


def test_audit_repo_mismatch_prints_warning_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MODULE}.github.current_repo", return_value="local/repo"),
        patch(f"{_MODULE}.audit_local_config"),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "other/repo"])
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "other/repo" in captured.err


def test_audit_repo_mismatch_reports_local_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MODULE}.github.current_repo", return_value="local/repo"),
        patch(f"{_MODULE}.audit_local_config"),
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "other/repo"])
    output = capsys.readouterr().out
    assert "local: skipped" in output


def test_apply_skips_local_on_repo_mismatch() -> None:
    with (
        patch(f"{_MODULE}.github.current_repo", return_value="local/repo"),
        patch(f"{_MODULE}.audit_local_config") as mock_local,
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_noncompliant()),
        patch(f"{_MODULE}._fetch_remote_config"),
        patch(f"{_MODULE}._apply_repo", return_value=[]) as mock_apply,
    ):
        result = main(["apply", "--repo", "other/repo"])
    mock_local.assert_not_called()
    mock_apply.assert_called_once()
    assert result == 0


def test_audit_runs_local_when_repo_matches_cwd() -> None:
    with (
        patch(f"{_MODULE}.github.current_repo", return_value="o/r"),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()) as mock_local,
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        result = main(["audit", "--repo", "o/r"])
    mock_local.assert_called_once()
    assert result == 0


# -- Issue #1210: delta output for status check mismatches -------------------


def test_print_diff_shows_delta_for_rules_mismatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_rules: list[dict[str, object]] = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": "quality / common", "integration_id": 15368},
                ],
            },
        },
    ]
    actual_rules: list[dict[str, object]] = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": "quality / common", "integration_id": 15368},
                    {"context": "CodeQL", "integration_id": 57789},
                ],
            },
        },
    ]
    diff = ConfigDiff(
        items=[
            DiffItem(
                field="rulesets.CI gates.rules",
                expected=expected_rules,
                actual=actual_rules,
            )
        ]
    )
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=diff),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "o/r"])
    output = capsys.readouterr().out
    assert "extra (1)" in output
    assert "CodeQL (integration_id: 57789)" in output
    assert "missing (0)" in output


def test_print_local_diff_shows_delta_for_rules_mismatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vergil_tooling.bin.vrg_github_repo_config import _print_local_diff

    expected_rules: list[dict[str, object]] = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": "quality / common", "integration_id": 15368},
                    {"context": "security / trivy", "integration_id": 15368},
                ],
            },
        },
    ]
    actual_rules: list[dict[str, object]] = [
        {
            "type": "required_status_checks",
            "parameters": {
                "strict_required_status_checks_policy": True,
                "required_status_checks": [
                    {"context": "quality / common", "integration_id": 15368},
                ],
            },
        },
    ]
    diff = ConfigDiff(
        items=[
            DiffItem(
                field="rulesets.CI gates.rules",
                expected=expected_rules,
                actual=actual_rules,
            )
        ]
    )
    _print_local_diff(diff)
    output = capsys.readouterr().out
    assert "missing (1)" in output
    assert "security / trivy (integration_id: 15368)" in output
    assert "extra (0)" in output


def test_print_diff_falls_back_for_rules_without_status_checks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected_rules: list[dict[str, object]] = [{"type": "deletion"}]
    actual_rules: list[dict[str, object]] = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
    ]
    diff = ConfigDiff(
        items=[
            DiffItem(
                field="rulesets.Branch protection.rules",
                expected=expected_rules,
                actual=actual_rules,
            )
        ]
    )
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=diff),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "o/r"])
    output = capsys.readouterr().out
    assert "expected=" in output
    assert "actual=" in output


def test_print_diff_falls_back_for_non_rules_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    diff = ConfigDiff(
        items=[DiffItem(field="repo_settings.allow_auto_merge", expected=False, actual=True)]
    )
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=diff),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        main(["audit", "--repo", "o/r"])
    output = capsys.readouterr().out
    assert "expected=False, actual=True" in output


# -- Local check skip on --repo mismatch --------------------------------------


def test_audit_skips_local_when_cwd_origin_unavailable() -> None:
    import subprocess

    with (
        patch(
            f"{_MODULE}.github.current_repo",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ),
        patch(f"{_MODULE}.audit_local_config") as mock_local,
        patch(f"{_MODULE}._audit_repo", return_value=_mock_github_compliant()),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        result = main(["audit", "--repo", "other/repo"])
    mock_local.assert_not_called()
    assert result == 0


# -- Issue #1288: bypass_actors skipped in App mode ---------------------------


def test_audit_prints_skipped_bypass_actors(capsys: pytest.CaptureFixture[str]) -> None:
    diff = ConfigDiff(
        items=[],
        skipped=[
            "rulesets.Tag protection.bypass_actors",
            "rulesets.Branch protection.bypass_actors",
            "rulesets.CI gates.bypass_actors",
        ],
    )
    with (
        patch(f"{_MODULE}._cwd_matches_repo", return_value=True),
        patch(f"{_MODULE}.audit_local_config", return_value=_mock_local_compliant()),
        patch(f"{_MODULE}._audit_repo", return_value=diff),
        patch(f"{_MODULE}._resolve_repo", return_value="o/r"),
        patch(f"{_MODULE}._fetch_remote_config"),
    ):
        result = main(["audit", "--repo", "o/r"])
    assert result == 0
    output = capsys.readouterr().out
    assert "bypass_actors: skipped" in output
    assert "GitHub App credentials" in output
