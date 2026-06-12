"""Tests for vergil_tooling.lib.confirm (issue #1644)."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest

from vergil_tooling.lib.confirm import add_yes_argument, confirm

_MOD = "vergil_tooling.lib.confirm"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    add_yes_argument(parser)
    return parser


def test_add_yes_argument_defaults_false() -> None:
    assert _parser().parse_args([]).yes is False


def test_add_yes_argument_long_flag() -> None:
    assert _parser().parse_args(["--yes"]).yes is True


def test_add_yes_argument_short_flag() -> None:
    assert _parser().parse_args(["-y"]).yes is True


def test_assume_yes_returns_true_without_reading_stdin(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--yes pre-answers without touching stdin, but still echoes the
    decision so the transcript records it."""
    with patch(_MOD + ".input", side_effect=AssertionError("stdin read")) as inp:
        assert confirm("Proceed?", assume_yes=True) is True
    inp.assert_not_called()
    assert "(--yes)" in capsys.readouterr().out


def test_assume_yes_echo_reflects_default_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    confirm("Proceed?", assume_yes=True, default=True)
    assert "[Y/n]" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["y", "yes", "Y", "YES"])
def test_interactive_yes(answer: str) -> None:
    with patch(_MOD + ".input", return_value=answer):
        assert confirm("Proceed?", assume_yes=False) is True


@pytest.mark.parametrize("answer", ["n", "no", "N", "NO"])
def test_interactive_no(answer: str) -> None:
    with patch(_MOD + ".input", return_value=answer):
        assert confirm("Proceed?", assume_yes=False) is False


def test_empty_input_uses_default_false() -> None:
    with patch(_MOD + ".input", return_value=""):
        assert confirm("Proceed?", assume_yes=False, default=False) is False


def test_empty_input_uses_default_true() -> None:
    with patch(_MOD + ".input", return_value=""):
        assert confirm("Proceed?", assume_yes=False, default=True) is True


def test_garbage_input_reprompts_until_valid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(_MOD + ".input", side_effect=["maybe", "y"]):
        assert confirm("Proceed?", assume_yes=False) is True
    assert "Enter y or n." in capsys.readouterr().out


def test_eof_is_treated_as_decline() -> None:
    with patch(_MOD + ".input", side_effect=EOFError):
        assert confirm("Proceed?", assume_yes=False) is False


def test_keyboard_interrupt_is_treated_as_decline() -> None:
    with patch(_MOD + ".input", side_effect=KeyboardInterrupt):
        assert confirm("Proceed?", assume_yes=False) is False
