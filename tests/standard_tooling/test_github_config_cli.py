"""Tests for standard_tooling.bin.github_config."""

from __future__ import annotations

import argparse
import base64
from unittest.mock import patch

import pytest

from standard_tooling.bin.github_config import (
    _apply_repo,
    _audit_repo,
    _fetch_remote_config,
    _resolve_repos,
    main,
    parse_args,
)
from standard_tooling.lib.config import (
    GithubOverrides,
    MarkdownlintConfig,
    ProjectConfig,
    StConfig,
)
from standard_tooling.lib.github_config import ConfigDiff, DiffItem

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
    assert args.yes is False


def test_parse_apply_with_yes() -> None:
    args = parse_args(["apply", "--repo", "o/r", "--yes"])
    assert args.yes is True


def test_parse_project_mode() -> None:
    args = parse_args(["audit", "--owner", "acme", "--project", "3"])
    assert args.owner == "acme"
    assert args.project == "3"


def test_parse_no_target_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["audit"])


def test_parse_no_command_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--repo", "o/r"])


# -- Audit mode ---------------------------------------------------------------


def _mock_compliant() -> ConfigDiff:
    return ConfigDiff(items=[])


def _mock_noncompliant() -> ConfigDiff:
    return ConfigDiff(
        items=[
            DiffItem(
                field="repo_settings.allow_auto_merge",
                expected=False,
                actual=True,
            ),
        ]
    )


def test_audit_compliant_returns_zero() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
    ):
        assert main(["audit", "--repo", "o/r"]) == 0


def test_audit_noncompliant_returns_one() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
    ):
        assert main(["audit", "--repo", "o/r"]) == 1


def test_diff_always_returns_zero() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
    ):
        assert main(["diff", "--repo", "o/r"]) == 0


def test_apply_returns_zero() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
    ):
        assert main(["apply", "--repo", "o/r"]) == 0


# -- _resolve_repos -----------------------------------------------------------


def test_resolve_repos_single_repo() -> None:
    args = argparse.Namespace(repo="o/r", owner=None, project=None)
    assert _resolve_repos(args) == ["o/r"]


def test_resolve_repos_project_mode() -> None:
    args = argparse.Namespace(repo=None, owner="acme", project="3")
    with patch(
        "standard_tooling.bin.github_config.github.list_project_repos",
        return_value=["acme/a", "acme/b"],
    ):
        assert _resolve_repos(args) == ["acme/a", "acme/b"]


# -- _fetch_remote_config -----------------------------------------------------

_VALID_TOML = b"""\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
standard-tooling = "v1.4"
"""


def test_fetch_remote_config_success() -> None:
    encoded = base64.b64encode(_VALID_TOML).decode()
    with patch(
        "standard_tooling.bin.github_config.github.read_json",
        return_value={"content": encoded},
    ):
        cfg = _fetch_remote_config("o/r")
    assert cfg.project.primary_language == "python"


def test_fetch_remote_config_non_dict_response() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config.github.read_json",
            return_value=[],
        ),
        pytest.raises(RuntimeError, match="Unexpected response"),
    ):
        _fetch_remote_config("o/r")


def test_fetch_remote_config_no_content_field() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config.github.read_json",
            return_value={"encoding": "base64"},
        ),
        pytest.raises(RuntimeError, match="No content field"),
    ):
        _fetch_remote_config("o/r")


# -- _audit_repo ---------------------------------------------------------------


def _make_config() -> StConfig:
    return StConfig(
        project=ProjectConfig(
            repository_type="library",
            versioning_scheme="semver",
            branching_model="library-release",
            release_model="tagged-release",
            primary_language="python",
            co_authors={},
        ),
        dependencies={"standard-tooling": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=None,
        github=GithubOverrides(skip_rulesets=True),
    )


def test_audit_repo_calls_compute_and_diff() -> None:
    cfg = _make_config()
    with (
        patch(
            "standard_tooling.bin.github_config.fetch_actual_state",
        ) as mock_fetch,
        patch(
            "standard_tooling.bin.github_config.compute_desired_state",
        ) as mock_desired,
        patch(
            "standard_tooling.bin.github_config.compute_diff",
            return_value=ConfigDiff(items=[]),
        ) as mock_diff,
    ):
        result = _audit_repo("o/r", cfg)
    mock_desired.assert_called_once_with(cfg)
    mock_fetch.assert_called_once_with("o/r")
    mock_diff.assert_called_once()
    assert result.is_compliant()


# -- _apply_repo ---------------------------------------------------------------


def test_apply_repo_calls_apply_desired_state() -> None:
    cfg = _make_config()
    with (
        patch("standard_tooling.bin.github_config.compute_desired_state") as mock_desired,
        patch("standard_tooling.bin.github_config.apply_desired_state") as mock_apply,
    ):
        _apply_repo("o/r", cfg)
    mock_desired.assert_called_once_with(cfg)
    mock_apply.assert_called_once_with("o/r", mock_desired.return_value)


# -- apply mode integration ----------------------------------------------------


def test_apply_all_compliant_does_nothing() -> None:
    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch("standard_tooling.bin.github_config._apply_repo") as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r", "--yes"])
    assert result == 0
    mock_apply.assert_not_called()


def test_apply_with_yes_applies_noncompliant() -> None:
    call_count = [0]

    def audit_side_effect(*_args: object, **_kwargs: object) -> ConfigDiff:
        call_count[0] += 1
        return _mock_noncompliant()

    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            side_effect=audit_side_effect,
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch("standard_tooling.bin.github_config._apply_repo", return_value=[]) as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r", "--yes"])
    assert result == 0
    mock_apply.assert_called_once()


def test_apply_without_yes_prompts_and_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "n")
    call_count = [0]

    def audit_side_effect(*_args: object, **_kwargs: object) -> ConfigDiff:
        call_count[0] += 1
        return _mock_noncompliant()

    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            side_effect=audit_side_effect,
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch("standard_tooling.bin.github_config._apply_repo") as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 1
    mock_apply.assert_not_called()


def test_apply_without_yes_prompts_and_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "y")
    call_count = [0]

    def audit_side_effect(*_args: object, **_kwargs: object) -> ConfigDiff:
        call_count[0] += 1
        return _mock_noncompliant()

    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            side_effect=audit_side_effect,
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch("standard_tooling.bin.github_config._apply_repo", return_value=[]) as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 0
    mock_apply.assert_called_once()


def test_apply_reports_legacy_protection_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "y")

    def audit_side_effect(*_args: object, **_kwargs: object) -> ConfigDiff:
        return _mock_noncompliant()

    with (
        patch(
            "standard_tooling.bin.github_config._audit_repo",
            side_effect=audit_side_effect,
        ),
        patch(
            "standard_tooling.bin.github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("standard_tooling.bin.github_config._fetch_remote_config"),
        patch(
            "standard_tooling.bin.github_config._apply_repo",
            return_value=["main", "develop"],
        ),
        patch("builtins.print") as mock_print,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 0
    output = " ".join(str(c) for c in mock_print.call_args_list)
    assert "legacy protection removed" in output
