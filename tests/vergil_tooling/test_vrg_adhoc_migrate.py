"""Tests for vergil_tooling.bin.vrg_adhoc_migrate."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from vergil_tooling.bin.vrg_adhoc_migrate import main
from vergil_tooling.lib.adhoc_migrate import Relocation
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_adhoc_migrate"


def test_dry_run_prints_plan(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.adhoc_migrate.plan", return_value=[]),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out
    assert "nothing to migrate" in out


def test_apply_refused_for_agent(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.identity_mode.is_human", return_value=False):
        rc = main(["--apply"])
    assert rc == 1
    assert "human action" in capsys.readouterr().err


def test_apply_as_human_executes(capsys: pytest.CaptureFixture[str]) -> None:
    reloc = Relocation(standing=IssueRef("org", "tooling", 100), open_children=())
    apply_one = MagicMock(return_value="org/tooling#100 → org/.github#40 (0 open child(ren) moved)")
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="org"),
        patch(f"{_MOD}.adhoc_migrate.plan", return_value=[reloc]),
        patch(f"{_MOD}.adhoc_migrate.apply_one", apply_one),
    ):
        rc = main(["--apply"])
    assert rc == 0
    apply_one.assert_called_once_with(reloc)
    out = capsys.readouterr().out
    assert "applied" in out
    assert "org/tooling#100 → org/.github#40" in out


def test_errors_when_org_undetectable(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=False),
        patch(f"{_MOD}.github.detect_org", return_value=None),
    ):
        rc = main([])
    assert rc == 1
    assert "could not determine the GitHub org" in capsys.readouterr().err
