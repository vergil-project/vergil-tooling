from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from vergil_tooling.bin.vrg_release import main, parse_args
from vergil_tooling.lib.release.context import ReleaseContext

_MOD = "vergil_tooling.bin.vrg_release"


def test_parse_args_default() -> None:
    args = parse_args([])
    assert args.version_override is None


def test_parse_args_minor() -> None:
    args = parse_args(["minor"])
    assert args.version_override == "minor"


def test_parse_args_major() -> None:
    args = parse_args(["major"])
    assert args.version_override == "major"


def test_parse_args_no_promote() -> None:
    args = parse_args(["--no-promote"])
    assert args.no_promote is True
    assert args.version_override is None


def test_parse_args_default_promote() -> None:
    args = parse_args([])
    assert args.no_promote is False


def test_parse_args_skip_cd_docs() -> None:
    args = parse_args(["--skip-cd-docs"])
    assert args.skip_cd_docs is True
    assert args.version_override is None


def test_parse_args_default_skip_cd_docs() -> None:
    args = parse_args([])
    assert args.skip_cd_docs is False


def test_parse_args_skip_audit() -> None:
    args = parse_args(["--skip-audit"])
    assert args.skip_audit is True
    assert args.version_override is None


def test_parse_args_default_skip_audit() -> None:
    args = parse_args([])
    assert args.skip_audit is False


def test_parse_args_no_promote_with_minor() -> None:
    args = parse_args(["--no-promote", "minor"])
    assert args.no_promote is True
    assert args.version_override == "minor"


def test_main_passes_skip_audit_to_preflight() -> None:
    mock_root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + ".preflight") as mock_pf,
        patch(_MOD + ".run_release"),
        patch(_MOD + ".git.repo_root", return_value=mock_root),
    ):
        mock_pf.return_value = ReleaseContext(
            repo="o/r",
            version="1.0.0",
            repo_root=mock_root,
            version_override=None,
        )
        main(["--skip-audit"])
    mock_pf.assert_called_once()
    _, kwargs = mock_pf.call_args
    assert kwargs["skip_audit"] is True


def test_main_sets_promote_on_context() -> None:
    mock_root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + ".preflight") as mock_pf,
        patch(_MOD + ".run_release") as mock_run,
        patch(_MOD + ".git.repo_root", return_value=mock_root),
    ):
        ctx = ReleaseContext(
            repo="o/r",
            version="1.0.0",
            repo_root=mock_root,
            version_override=None,
        )
        mock_pf.return_value = ctx
        main(["--no-promote"])
    assert ctx.promote is False
    mock_run.assert_called_once_with(ctx)


def test_main_returns_zero_on_success() -> None:
    mock_root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + ".preflight") as mock_pf,
        patch(_MOD + ".run_release"),
        patch(_MOD + ".git.repo_root", return_value=mock_root),
    ):
        mock_pf.return_value = ReleaseContext(
            repo="o/r",
            version="1.0.0",
            repo_root=mock_root,
            version_override=None,
        )
        result = main([])
    assert result == 0


def test_main_returns_one_on_release_error() -> None:
    from vergil_tooling.lib.release.context import ReleaseError

    mock_root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(
            _MOD + ".preflight",
            side_effect=ReleaseError(
                phase="preflight",
                command="test",
                message="test failure",
            ),
        ),
        patch(_MOD + ".git.repo_root", return_value=mock_root),
    ):
        result = main([])
    assert result == 1


def test_main_returns_one_on_release_error_with_detail() -> None:
    from vergil_tooling.lib.release.context import ReleaseError

    mock_root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(
            _MOD + ".preflight",
            side_effect=ReleaseError(
                phase="preflight",
                command="test",
                message="test failure",
                detail="extra detail",
            ),
        ),
        patch(_MOD + ".git.repo_root", return_value=mock_root),
    ):
        result = main([])
    assert result == 1


def test_main_returns_one_on_unexpected_error() -> None:
    mock_root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + ".preflight", side_effect=RuntimeError("boom")),
        patch(_MOD + ".git.repo_root", return_value=mock_root),
    ):
        result = main([])
    assert result == 1
