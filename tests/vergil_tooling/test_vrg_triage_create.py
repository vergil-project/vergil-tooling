"""Tests for the deprecated vergil_tooling.bin.vrg_triage_create alias."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin import vrg_triage_create

if TYPE_CHECKING:
    import pytest

_TARGET = "vergil_tooling.bin.vrg_triage_create.vrg_intake_create.main"
_DEPRECATED = "vrg-triage-create is deprecated; use vrg-intake-create"


def test_triage_alias_warns_ignores_kind_and_forwards(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_TARGET, return_value=0) as forward:
        rc = vrg_triage_create.main(["--kind", "idea", "--title", "T"])
    assert rc == 0
    forward.assert_called_once_with(["--title", "T"])
    err = capsys.readouterr().err
    assert _DEPRECATED in err
    assert "--kind is ignored" in err


def test_triage_alias_strips_kind_equals_form(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_TARGET, return_value=0) as forward:
        vrg_triage_create.main(["--title", "T", "--kind=research", "--label", "bug"])
    forward.assert_called_once_with(["--title", "T", "--label", "bug"])
    assert "--kind is ignored" in capsys.readouterr().err


def test_triage_alias_without_kind_only_warns_deprecation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(_TARGET, return_value=1) as forward:
        rc = vrg_triage_create.main(["--title", "T"])
    assert rc == 1
    forward.assert_called_once_with(["--title", "T"])
    err = capsys.readouterr().err
    assert _DEPRECATED in err
    assert "--kind" not in err


def test_triage_alias_dangling_kind_is_dropped(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_TARGET, return_value=0) as forward:
        vrg_triage_create.main(["--title", "T", "--kind"])
    forward.assert_called_once_with(["--title", "T"])
    assert "--kind is ignored" in capsys.readouterr().err


def test_triage_alias_defaults_to_sys_argv(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-triage-create", "--kind", "triage", "--title", "T"]),
        patch(_TARGET, return_value=0) as forward,
    ):
        vrg_triage_create.main()
    forward.assert_called_once_with(["--title", "T"])
    assert _DEPRECATED in capsys.readouterr().err
