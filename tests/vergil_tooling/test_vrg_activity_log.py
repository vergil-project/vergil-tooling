"""Tests for vergil_tooling.bin.vrg_activity_log."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_activity_log import main
from vergil_tooling.lib import activity_log, github
from vergil_tooling.lib.activity_log import ActivityItem

_MOD = "vergil_tooling.bin.vrg_activity_log"

if TYPE_CHECKING:
    from pathlib import Path


def test_main_prints_ledger(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "vergil_tooling.bin.vrg_activity_log.activity_log.gather", return_value=[]
    ) as mock_gather:
        rc = main(["--org", "vergil-project"])
    assert rc == 0
    assert "Activity log" in capsys.readouterr().out
    # the CLI computes and passes a YYYY-MM-DD cutoff
    since = mock_gather.call_args.args[0]
    assert len(since) == 10 and since.count("-") == 2
    assert mock_gather.call_args.kwargs["org"] == "vergil-project"


def test_main_defaults_org_to_current_repo() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_activity_log.github.current_org", return_value="acme"
        ) as mock_org,
        patch(
            "vergil_tooling.bin.vrg_activity_log.activity_log.gather", return_value=[]
        ) as mock_gather,
    ):
        rc = main([])
    assert rc == 0
    mock_org.assert_called_once()
    assert mock_gather.call_args.kwargs["org"] == "acme"


def test_main_scopes_token_to_org() -> None:
    """Listing a cross-org activity log mints for that org's installation (#2070)."""
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.activity_log.gather", return_value=[]),
    ):
        rc = main(["--org", "other-org"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_reports_missing_installation() -> None:
    with patch(
        f"{_MOD}.activity_log.gather",
        side_effect=github.NoInstallationError("other-org", []),
    ):
        rc = main(["--org", "other-org"])
    assert rc == 1


_ITEM = ActivityItem("vergil-project/vergil-tooling", 7, "seven", "u7", "2026-06-29")


def test_main_default_output_is_the_flat_org_ledger(capsys: pytest.CaptureFixture[str]) -> None:
    # The nightly docs refresh pipes this to activity.md; it must stay unchanged.
    with patch(f"{_MOD}.activity_log.gather", return_value=[_ITEM]) as mock_gather:
        rc = main(["--org", "vergil-project"])
    assert rc == 0
    assert capsys.readouterr().out == activity_log.render([_ITEM]) + "\n"
    assert mock_gather.call_args.kwargs["repo"] is None


def test_main_repo_prints_flat_repo_ledger(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.activity_log.gather", return_value=[_ITEM]) as mock_gather:
        rc = main(["--org", "vergil-project", "--repo", "vergil-project/vergil-tooling"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("# Activity log: vergil-tooling\n")
    assert "## 2026-06-29" in out
    assert "|" not in out  # no summary table on a per-repo page
    assert mock_gather.call_args.kwargs["repo"] == "vergil-project/vergil-tooling"


def test_main_out_dir_writes_pages_and_clears_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "days").mkdir()
    (tmp_path / "days" / "2026-01-01.md").write_text("old")
    keep = tmp_path / "notes.md"  # outside the owned subdirectories
    keep.write_text("mine")
    with patch(f"{_MOD}.activity_log.gather", return_value=[_ITEM]):
        rc = main(["--org", "vergil-project", "--out-dir", str(tmp_path)])
    assert rc == 0
    expected = activity_log.render_pages([_ITEM])
    for rel, text in expected.items():
        assert (tmp_path / rel).read_text(encoding="utf-8") == text
    assert not (tmp_path / "days" / "2026-01-01.md").exists()
    assert keep.read_text() == "mine"
    assert f"wrote {len(expected)} page(s)" in capsys.readouterr().out


def test_main_repo_and_out_dir_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--repo", "x", "--out-dir", str(tmp_path)])
