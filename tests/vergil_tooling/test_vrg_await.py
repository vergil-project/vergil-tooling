"""Tests for vergil_tooling.bin.vrg_await."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_await import main, parse_args

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_await"


def test_parse_args_path_only() -> None:
    args = parse_args(["/x/y.yml"])
    assert args.path == "/x/y.yml"
    assert args.since is None


def test_parse_args_with_since() -> None:
    args = parse_args(["/x/y.yml", "--since", "abc123"])
    assert args.path == "/x/y.yml"
    assert args.since == "abc123"


def test_main_prints_digest_and_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.await_file.wait_for_file", return_value="deadbeef") as waiter:
        result = main(["/some/path"])
    assert result == 0
    assert capsys.readouterr().out.strip() == "deadbeef"
    waiter.assert_called_once()


def test_main_passes_since_through() -> None:
    with patch(f"{_MOD}.await_file.wait_for_file", return_value="x") as waiter:
        main(["/some/path", "--since", "abc"])
    _, kwargs = waiter.call_args
    assert kwargs["since"] == "abc"


def test_main_passes_path_as_pathlike() -> None:
    with patch(f"{_MOD}.await_file.wait_for_file", return_value="x") as waiter:
        main(["/some/path"])
    args, _ = waiter.call_args
    assert str(args[0]) == "/some/path"
