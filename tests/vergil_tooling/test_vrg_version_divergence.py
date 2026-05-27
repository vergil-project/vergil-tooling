"""Tests for vergil_tooling.bin.vrg_version_divergence CLI."""

from __future__ import annotations

from unittest.mock import patch

from vergil_tooling.bin.vrg_version_divergence import main

_MOD = "vergil_tooling.bin.vrg_version_divergence"


def test_diverged() -> None:
    with patch(f"{_MOD}.write_output") as mock_out:
        rc = main(["1.2.0", "1.1.0"])
    assert rc == 0
    mock_out.assert_any_call("status", "diverged")
    mock_out.assert_any_call("head_version", "1.2.0")
    mock_out.assert_any_call("main_version", "1.1.0")


def test_equal() -> None:
    with (
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.emit_error") as mock_err,
        patch(f"{_MOD}.write_summary") as mock_sum,
    ):
        rc = main(["1.0.0", "1.0.0"])
    assert rc == 1
    mock_err.assert_called_once()
    assert "not bumped" in mock_err.call_args[0][0]
    mock_sum.assert_called_once()
    assert "Divergence Failed" in mock_sum.call_args[0][0]


def test_first_release() -> None:
    with patch(f"{_MOD}.write_output") as mock_out:
        rc = main(["0.1.0"])
    assert rc == 0
    mock_out.assert_any_call("status", "first-release")
    mock_out.assert_any_call("main_version", "")


def test_first_release_explicit_empty() -> None:
    with patch(f"{_MOD}.write_output") as mock_out:
        rc = main(["0.1.0", ""])
    assert rc == 0
    mock_out.assert_any_call("status", "first-release")


def test_empty_head_version() -> None:
    with patch(f"{_MOD}.emit_error") as mock_err:
        rc = main(["", "1.0.0"])
    assert rc == 2
    mock_err.assert_called_once()


def test_output_keys_on_equal() -> None:
    with (
        patch(f"{_MOD}.write_output") as mock_out,
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_summary"),
    ):
        main(["2.0.0", "2.0.0"])
    calls = {c[0][0] for c in mock_out.call_args_list}
    assert calls == {"status", "head_version", "main_version"}


def test_no_error_on_diverged() -> None:
    with (
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.emit_error") as mock_err,
        patch(f"{_MOD}.write_summary") as mock_sum,
    ):
        main(["1.1.0", "1.0.0"])
    mock_err.assert_not_called()
    mock_sum.assert_not_called()
