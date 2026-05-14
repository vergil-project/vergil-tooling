"""Tests for vergil_tooling.bin.vrg_github_config."""

from __future__ import annotations

import argparse
import base64
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_github_config import (
    _apply_repo,
    _audit_repo,
    _fetch_remote_config,
    _load_local_config,
    _resolve_repos,
    main,
    parse_args,
)
from vergil_tooling.lib.config import (
    CiConfig,
    DockerConfig,
    MarkdownlintConfig,
    ProjectConfig,
    PublishConfig,
    StConfig,
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


def test_parse_project_mode() -> None:
    args = parse_args(["audit", "--owner", "acme", "--project", "3"])
    assert args.owner == "acme"
    assert args.project == "3"


def test_parse_no_target_defaults() -> None:
    args = parse_args(["audit"])
    assert args.repo is None
    assert args.owner is None
    assert args.project is None


def test_parse_owner_without_project_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["audit", "--owner", "acme"])


def test_parse_project_without_owner_fails() -> None:
    with pytest.raises(SystemExit):
        parse_args(["audit", "--project", "3"])


def test_parse_config_flag_audit() -> None:
    args = parse_args(["audit", "--repo", "o/r", "--config", "local/st.toml"])
    assert args.config == "local/st.toml"


def test_parse_config_flag_diff() -> None:
    args = parse_args(["diff", "--repo", "o/r", "--config", "./st.toml"])
    assert args.config == "./st.toml"


def test_parse_config_flag_apply() -> None:
    args = parse_args(["apply", "--repo", "o/r", "--config", "st.toml"])
    assert args.config == "st.toml"


def test_parse_config_flag_absent() -> None:
    args = parse_args(["audit", "--repo", "o/r"])
    assert args.config is None


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
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("vergil_tooling.bin.vrg_github_config._fetch_remote_config"),
    ):
        assert main(["audit", "--repo", "o/r"]) == 0


def test_audit_noncompliant_returns_one() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("vergil_tooling.bin.vrg_github_config._fetch_remote_config"),
    ):
        assert main(["audit", "--repo", "o/r"]) == 1


def test_diff_always_returns_zero() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            return_value=_mock_noncompliant(),
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("vergil_tooling.bin.vrg_github_config._fetch_remote_config"),
    ):
        assert main(["diff", "--repo", "o/r"]) == 0


def test_apply_returns_zero() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("vergil_tooling.bin.vrg_github_config._fetch_remote_config"),
    ):
        assert main(["apply", "--repo", "o/r"]) == 0


# -- _resolve_repos -----------------------------------------------------------


def test_resolve_repos_single_repo() -> None:
    args = argparse.Namespace(repo="o/r", owner=None, project=None)
    assert _resolve_repos(args) == ["o/r"]


def test_resolve_repos_project_mode() -> None:
    args = argparse.Namespace(repo=None, owner="acme", project="3")
    with patch(
        "vergil_tooling.bin.vrg_github_config.github.list_project_repos",
        return_value=["acme/a", "acme/b"],
    ):
        assert _resolve_repos(args) == ["acme/a", "acme/b"]


def test_resolve_repos_defaults_to_current_repo() -> None:
    args = argparse.Namespace(repo=None, owner=None, project=None)
    with patch(
        "vergil_tooling.bin.vrg_github_config.github.current_repo",
        return_value="acme/my-repo",
    ):
        assert _resolve_repos(args) == ["acme/my-repo"]


# -- _fetch_remote_config -----------------------------------------------------

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


def test_fetch_remote_config_success() -> None:
    encoded = base64.b64encode(_VALID_TOML).decode()
    with patch(
        "vergil_tooling.bin.vrg_github_config.github.read_json",
        return_value={"content": encoded},
    ):
        cfg = _fetch_remote_config("o/r")
    assert cfg.project.primary_language == "python"


def test_fetch_remote_config_non_dict_response() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_github_config.github.read_json",
            return_value=[],
        ),
        pytest.raises(RuntimeError, match="Unexpected response"),
    ):
        _fetch_remote_config("o/r")


def test_fetch_remote_config_no_content_field() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_github_config.github.read_json",
            return_value={"encoding": "base64"},
        ),
        pytest.raises(RuntimeError, match="No content field"),
    ):
        _fetch_remote_config("o/r")


# -- _load_local_config --------------------------------------------------------


def test_load_local_config_success(tmp_path: object) -> None:
    from pathlib import Path

    p = Path(str(tmp_path)) / "vergil.toml"
    p.write_bytes(_VALID_TOML)
    cfg = _load_local_config(str(p))
    assert cfg.project.primary_language == "python"
    assert cfg.project.versioning_scheme == "semver"


def test_load_local_config_file_not_found() -> None:
    with pytest.raises(FileNotFoundError):
        _load_local_config("/nonexistent/path/vergil.toml")


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
        dependencies={"vergil": "v1.4"},
        markdownlint=MarkdownlintConfig(ignore=[]),
        ci=CiConfig(versions=["3.14"], integration_tests=False),
        publish=PublishConfig(release=False, docs=True),
        docker=DockerConfig(image_prefix="prod"),
    )


def test_audit_repo_calls_compute_and_diff() -> None:
    cfg = _make_config()
    with (
        patch(
            "vergil_tooling.bin.vrg_github_config.fetch_actual_state",
        ) as mock_fetch,
        patch(
            "vergil_tooling.bin.vrg_github_config.compute_desired_state",
        ) as mock_desired,
        patch(
            "vergil_tooling.bin.vrg_github_config.compute_diff",
            return_value=ConfigDiff(items=[]),
        ) as mock_diff,
    ):
        result = _audit_repo("o/r", cfg)
    mock_fetch.assert_called_once_with("o/r")
    is_org = mock_fetch.return_value.owner_type == "Organization"
    mock_desired.assert_called_once_with(
        cfg, visibility=mock_fetch.return_value.visibility, is_org=is_org
    )
    mock_diff.assert_called_once_with(
        desired=mock_desired.return_value,
        actual=mock_fetch.return_value.state,
    )
    assert result.is_compliant()


# -- _apply_repo ---------------------------------------------------------------


def test_apply_repo_calls_apply_desired_state() -> None:
    cfg = _make_config()
    with (
        patch("vergil_tooling.bin.vrg_github_config.fetch_actual_state") as mock_fetch,
        patch("vergil_tooling.bin.vrg_github_config.compute_desired_state") as mock_desired,
        patch("vergil_tooling.bin.vrg_github_config.apply_desired_state") as mock_apply,
    ):
        _apply_repo("o/r", cfg)
    mock_fetch.assert_called_once_with("o/r")
    is_org = mock_fetch.return_value.owner_type == "Organization"
    mock_desired.assert_called_once_with(
        cfg, visibility=mock_fetch.return_value.visibility, is_org=is_org
    )
    mock_apply.assert_called_once_with("o/r", mock_desired.return_value)


# -- apply mode integration ----------------------------------------------------


def test_apply_all_compliant_does_nothing() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("vergil_tooling.bin.vrg_github_config._fetch_remote_config"),
        patch("vergil_tooling.bin.vrg_github_config._apply_repo") as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 0
    mock_apply.assert_not_called()


def test_apply_noncompliant() -> None:
    def audit_side_effect(*_args: object, **_kwargs: object) -> ConfigDiff:
        return _mock_noncompliant()

    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            side_effect=audit_side_effect,
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("vergil_tooling.bin.vrg_github_config._fetch_remote_config"),
        patch("vergil_tooling.bin.vrg_github_config._apply_repo", return_value=[]) as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 0
    mock_apply.assert_called_once()


def test_apply_reports_legacy_protection_removed() -> None:
    def audit_side_effect(*_args: object, **_kwargs: object) -> ConfigDiff:
        return _mock_noncompliant()

    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            side_effect=audit_side_effect,
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch("vergil_tooling.bin.vrg_github_config._fetch_remote_config"),
        patch(
            "vergil_tooling.bin.vrg_github_config._apply_repo",
            return_value=["main", "develop"],
        ),
        patch("builtins.print") as mock_print,
    ):
        result = main(["apply", "--repo", "o/r"])
    assert result == 0
    output = " ".join(str(c) for c in mock_print.call_args_list)
    assert "legacy protection removed" in output


# -- --config flag integration -------------------------------------------------


def test_config_flag_bypasses_remote_fetch(tmp_path: object) -> None:
    from pathlib import Path

    p = Path(str(tmp_path)) / "vergil.toml"
    p.write_bytes(_VALID_TOML)

    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            return_value=_mock_compliant(),
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._fetch_remote_config",
        ) as mock_remote,
    ):
        result = main(["audit", "--repo", "o/r", "--config", str(p)])
    assert result == 0
    mock_remote.assert_not_called()


def test_config_flag_passes_parsed_config_to_audit(tmp_path: object) -> None:
    from pathlib import Path

    p = Path(str(tmp_path)) / "vergil.toml"
    p.write_bytes(_VALID_TOML)

    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            return_value=_mock_compliant(),
        ) as mock_audit,
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
    ):
        main(["audit", "--repo", "o/r", "--config", str(p)])
    cfg = mock_audit.call_args[0][1]
    assert cfg.project.primary_language == "python"


def test_config_flag_apply_uses_local_config(tmp_path: object) -> None:
    from pathlib import Path

    p = Path(str(tmp_path)) / "vergil.toml"
    p.write_bytes(_VALID_TOML)

    def audit_side_effect(*_args: object, **_kwargs: object) -> ConfigDiff:
        return _mock_noncompliant()

    with (
        patch(
            "vergil_tooling.bin.vrg_github_config._audit_repo",
            side_effect=audit_side_effect,
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._resolve_repos",
            return_value=["o/r"],
        ),
        patch(
            "vergil_tooling.bin.vrg_github_config._fetch_remote_config",
        ) as mock_remote,
        patch(
            "vergil_tooling.bin.vrg_github_config._apply_repo",
            return_value=[],
        ) as mock_apply,
    ):
        result = main(["apply", "--repo", "o/r", "--config", str(p)])
    assert result == 0
    mock_remote.assert_not_called()
    mock_apply.assert_called_once()
    cfg = mock_apply.call_args[0][1]
    assert cfg.project.primary_language == "python"
