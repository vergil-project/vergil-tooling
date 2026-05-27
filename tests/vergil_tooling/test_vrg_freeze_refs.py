"""Tests for vergil_tooling.bin.vrg_freeze_refs CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_freeze_refs import main

if TYPE_CHECKING:
    from pathlib import Path

_OWNER = "vergil-project/vergil-actions"
_TAG = "v2.0.50"
_MOD = "vergil_tooling.bin.vrg_freeze_refs"


def _make_workflow(tmp_path: Path, name: str, content: str) -> Path:
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(content)
    return f


def test_freezes_and_validates(tmp_path: Path) -> None:
    _make_workflow(
        tmp_path,
        "ci.yml",
        f"    uses: {_OWNER}/.github/workflows/audit.yml@develop\n",
    )
    rc = main(
        [
            "--owner-repo",
            _OWNER,
            "--tag",
            _TAG,
            "--scan-dirs",
            str(tmp_path / ".github" / "workflows"),
        ]
    )
    assert rc == 0
    content = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
    assert f"@{_TAG}" in content
    assert "@develop" not in content


def test_detects_unfreezing_failure(tmp_path: Path) -> None:
    d = tmp_path / "actions"
    d.mkdir()
    (d / "action.yml").write_text("    uses: ./actions/setup\n")
    with (
        patch(f"{_MOD}.freeze_references", side_effect=lambda c, o, t: c),
        patch(f"{_MOD}.emit_error") as mock_err,
    ):
        rc = main(
            [
                "--owner-repo",
                _OWNER,
                "--tag",
                _TAG,
                "--scan-dirs",
                str(d),
            ]
        )
    assert rc == 1
    mock_err.assert_called_once()
    assert "unfrozen" in mock_err.call_args[0][0]


def test_no_files_found(tmp_path: Path) -> None:
    with patch(f"{_MOD}.emit_warning") as mock_warn:
        rc = main(
            [
                "--owner-repo",
                _OWNER,
                "--tag",
                _TAG,
                "--scan-dirs",
                str(tmp_path / "empty"),
            ]
        )
    assert rc == 0
    mock_warn.assert_called_once()


def test_already_frozen_no_change(tmp_path: Path) -> None:
    f = _make_workflow(
        tmp_path,
        "ci.yml",
        f"    uses: {_OWNER}/.github/workflows/audit.yml@v1.0.0\n",
    )
    original = f.read_text()
    rc = main(
        [
            "--owner-repo",
            _OWNER,
            "--tag",
            _TAG,
            "--scan-dirs",
            str(tmp_path / ".github" / "workflows"),
        ]
    )
    assert rc == 0
    assert f.read_text() == original


def test_freezes_relative_refs(tmp_path: Path) -> None:
    _make_workflow(tmp_path, "ci.yml", "    uses: ./actions/local/setup\n")
    rc = main(
        [
            "--owner-repo",
            _OWNER,
            "--tag",
            _TAG,
            "--scan-dirs",
            str(tmp_path / ".github" / "workflows"),
        ]
    )
    assert rc == 0
    content = (tmp_path / ".github" / "workflows" / "ci.yml").read_text()
    assert f"{_OWNER}/actions/local/setup@{_TAG}" in content


def test_summary_on_findings(tmp_path: Path) -> None:
    d = tmp_path / "wf"
    d.mkdir()
    (d / "ci.yml").write_text("    uses: ./actions/setup\n")
    with (
        patch(f"{_MOD}.freeze_references", side_effect=lambda c, o, t: c),
        patch(f"{_MOD}.write_summary") as mock_sum,
        patch(f"{_MOD}.emit_error"),
    ):
        main(["--owner-repo", _OWNER, "--tag", _TAG, "--scan-dirs", str(d)])
    mock_sum.assert_called_once()
    assert "Unfrozen" in mock_sum.call_args[0][0]
