from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_release import main, parse_args

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


def test_main_returns_zero_on_success() -> None:
    with (
        patch(_MOD + ".preflight") as mock_pf,
        patch(_MOD + ".run_release"),
        patch(_MOD + ".git.repo_root", return_value=Path("/tmp/repo")),
    ):
        mock_pf.return_value = object()
        result = main([])
    assert result == 0


def test_main_returns_one_on_release_error() -> None:
    from vergil_tooling.lib.release.context import ReleaseError

    with (
        patch(
            _MOD + ".preflight",
            side_effect=ReleaseError(
                phase="preflight",
                command="test",
                message="test failure",
            ),
        ),
        patch(_MOD + ".git.repo_root", return_value=Path("/tmp/repo")),
    ):
        result = main([])
    assert result == 1
