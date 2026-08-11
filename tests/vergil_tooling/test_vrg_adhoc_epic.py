"""Tests for vergil_tooling.bin.vrg_adhoc_epic."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_adhoc_epic import main, parse_args
from vergil_tooling.lib import epics

_MOD = "vergil_tooling.bin.vrg_adhoc_epic"


def test_parse_args_requires_subcommand() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


def test_ensure_current_repo(capsys: pytest.CaptureFixture[str]) -> None:
    # The ad-hoc epic for org/repo lives in org/.github (title-disambiguated).
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(
            f"{_MOD}.epics.ensure_adhoc_epic",
            return_value=epics.IssueRef("org", ".github", 5),
        ) as mock_ensure,
    ):
        rc = main(["ensure"])
    assert rc == 0
    mock_ensure.assert_called_once_with("org/repo")
    out = capsys.readouterr().out
    assert "epic home: org/.github [PUBLIC]" in out
    assert "Ad-hoc epic:" in out
    assert "org/.github#5" in out


def test_ensure_repo_override(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo") as mock_cur,
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PUBLIC"),
        patch(
            f"{_MOD}.epics.ensure_adhoc_epic",
            return_value=epics.IssueRef("org", ".github", 9),
        ) as mock_ensure,
    ):
        rc = main(["ensure", "--repo", "org/actions"])
    assert rc == 0
    mock_ensure.assert_called_once_with("org/actions")
    mock_cur.assert_not_called()
    assert "org/.github#9" in capsys.readouterr().out


def test_ensure_malformed_repo_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.epics.ensure_adhoc_epic") as mock_ensure:
        rc = main(["ensure", "--repo", "noslash"])
    assert rc == 1
    assert "owner/repo" in capsys.readouterr().err
    mock_ensure.assert_not_called()


def test_ensure_private_repo_echoes_self_home(capsys: pytest.CaptureFixture[str]) -> None:
    # A private target's ad-hoc epic homes in the repo itself; the echo shows it.
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab"),
        patch(f"{_MOD}.github.repo_visibility", return_value="PRIVATE"),
        patch(
            f"{_MOD}.epics.ensure_adhoc_epic",
            return_value=epics.IssueRef("org", "lab", 3),
        ),
    ):
        rc = main(["ensure", "--repo", "org/lab"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "epic home: org/lab [PRIVATE]" in out
    assert "org/lab#3" in out


def test_archive_repo_dry_run_default(capsys: pytest.CaptureFixture[str]) -> None:
    plan = epics.DrainPlan(
        epics.IssueRef("org", ".github", 40),
        moves=[(epics.IssueRef("org", ".github", 101), "2026-Q2")],
        close=[],
    )
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.github.target_org") as mock_scope,  # MagicMock is a ctx manager
        patch(f"{_MOD}.epics.drain_adhoc_repo", return_value=plan) as mock_drain,
    ):
        rc = main(["archive"])
    assert rc == 0
    assert mock_drain.call_args.kwargs["apply"] is False
    assert mock_scope.call_args.args[0] == "org"  # token scoped to the owner
    assert "org/.github#101" in capsys.readouterr().out


def test_archive_repo_override_apply(capsys: pytest.CaptureFixture[str]) -> None:
    plan = epics.DrainPlan(
        epics.IssueRef("org", ".github", 40),
        moves=[],
        close=[epics.IssueRef("org", ".github", 88)],
    )
    with (
        patch(f"{_MOD}.github.current_repo") as mock_cur,
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.drain_adhoc_repo", return_value=plan) as mock_drain,
    ):
        rc = main(["archive", "--repo", "org/tooling", "--apply"])
    assert rc == 0
    mock_cur.assert_not_called()
    assert mock_drain.call_args.args[0] == "org/tooling"
    assert mock_drain.call_args.kwargs["apply"] is True
    assert mock_scope.call_args.args[0] == "org"
    out = capsys.readouterr().out
    assert "[APPLY]" in out
    assert "close past archive org/.github#88" in out


def test_archive_repo_no_epic(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.drain_adhoc_repo", return_value=None),
    ):
        rc = main(["archive"])
    assert rc == 0
    assert "org/tooling: no ad-hoc epic" in capsys.readouterr().out


def test_archive_malformed_repo_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="noslash"),
        patch(f"{_MOD}.epics.drain_adhoc_repo") as mock_drain,
    ):
        rc = main(["archive"])
    assert rc == 1
    assert "owner/repo" in capsys.readouterr().err
    mock_drain.assert_not_called()


def test_archive_all_in_apply(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[]) as mock_org,
    ):
        rc = main(["archive", "--all-in", "org", "--apply"])
    assert rc == 0
    assert mock_org.call_args.args[0] == "org"
    assert mock_org.call_args.kwargs["apply"] is True
    assert mock_scope.call_args.args[0] == "org"  # token scoped to the org


def test_archive_all_in_renders_each_plan(capsys: pytest.CaptureFixture[str]) -> None:
    plan = epics.DrainPlan(
        epics.IssueRef("org", ".github", 40),
        moves=[(epics.IssueRef("org", ".github", 101), "2026-Q2")],
        close=[],
    )
    with (
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.drain_adhoc_org", return_value=[plan]),
    ):
        rc = main(["archive", "--all-in", "org"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[DRY-RUN]" in out
    assert "org: 1 ad-hoc epic(s) with work to archive" in out
    assert "org/.github#101 -> 2026-Q2" in out


def test_archive_repo_and_all_in_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        parse_args(["archive", "--repo", "org/tooling", "--all-in", "org"])


def test_normalize_dry_run_lists_conversions(capsys: pytest.CaptureFixture[str]) -> None:
    conv = [
        epics.ArchiveConversion(
            epics.IssueRef("org", ".github", 88),
            "Epic (ad hoc): tooling — 2026-Q2",
            "Archive (ad hoc): tooling — 2026-Q2",
        )
    ]
    with (
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.normalize_adhoc_archives", return_value=conv) as mock_norm,
    ):
        rc = main(["normalize", "--all-in", "org"])
    assert rc == 0
    mock_norm.assert_called_once_with("org", apply=False)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "1 archive(s)" in out
    assert "org/.github#88" in out and "Archive (ad hoc): tooling — 2026-Q2" in out


def test_normalize_apply_passes_apply_true() -> None:
    with (
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.normalize_adhoc_archives", return_value=[]) as mock_norm,
    ):
        rc = main(["normalize", "--all-in", "org", "--apply"])
    assert rc == 0
    mock_norm.assert_called_once_with("org", apply=True)
