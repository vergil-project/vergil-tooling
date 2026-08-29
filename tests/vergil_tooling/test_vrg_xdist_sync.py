"""Tests for the ``vrg-xdist-sync`` CLI (epic vergil-project/.github#333, Task 9)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from vergil_tooling.bin import vrg_xdist_sync
from vergil_tooling.lib.fleet_sweep import RepoResult, SweepSpec
from vergil_tooling.lib.xdist_applicator import add_xdist


@dataclass
class _FakeProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


def test_main_builds_xdist_spec_and_uses_add_xdist(monkeypatch: pytest.MonkeyPatch) -> None:
    """main builds the xdist SweepSpec, passes add_xdist, and threads dry_run."""
    captured: dict[str, object] = {}

    def fake_run_sweep(spec, applicator, *, dry_run):  # noqa: ANN001, ANN202
        captured["spec"] = spec
        captured["applicator"] = applicator
        captured["dry_run"] = dry_run
        return [RepoResult(repo="/clones/x", status="ready", detail="added")]

    monkeypatch.setattr(vrg_xdist_sync, "run_sweep", fake_run_sweep)

    rc = vrg_xdist_sync.main(["--repos", "/clones/x", "/clones/y", "--dry-run"])

    assert rc == 0
    spec = captured["spec"]
    assert isinstance(spec, SweepSpec)
    assert spec.repos == ["/clones/x", "/clones/y"]
    assert spec.branch_slug == "add-pytest-xdist"
    assert spec.commit_type == "chore"
    assert spec.commit_scope == "test"
    assert spec.epic is None
    assert "pytest-xdist" in spec.title
    assert captured["applicator"] is add_xdist
    assert captured["dry_run"] is True


def test_main_returns_nonzero_when_a_repo_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        vrg_xdist_sync,
        "run_sweep",
        lambda *a, **k: [RepoResult(repo="/clones/x", status="error", detail="boom")],
    )
    assert vrg_xdist_sync.main(["--repos", "/clones/x"]) == 1


def test_main_prints_per_repo_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        vrg_xdist_sync,
        "run_sweep",
        lambda *a, **k: [
            RepoResult(repo="/clones/x", status="ready", detail="added pytest-xdist"),
            RepoResult(repo="/clones/y", status="skipped", detail="already present"),
        ],
    )
    rc = vrg_xdist_sync.main(["--repos", "/clones/x", "/clones/y"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ready" in out and "/clones/x" in out
    assert "skipped" in out and "/clones/y" in out


def test_main_reports_and_files_followup_for_unknown_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A needs_followup repo triggers a prominent summary and a filed issue."""
    monkeypatch.setattr(
        vrg_xdist_sync,
        "run_sweep",
        lambda *a, **k: [
            RepoResult(
                repo="/clones/weird", status="skipped", detail="NOT added", needs_followup=True
            ),
        ],
    )
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):  # noqa: ANN001, ANN202
        calls.append(args)
        return _FakeProc(returncode=0, stdout="https://github.com/org/weird/issues/42\n")

    monkeypatch.setattr(vrg_xdist_sync.subprocess, "run", fake_run)

    rc = vrg_xdist_sync.main(["--repos", "/clones/weird"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "NEEDS FOLLOW-UP" in out
    assert "/clones/weird" in out
    assert "filed follow-up issue #42" in out
    assert calls and calls[0][0] == "vrg-issue-create"


def test_followup_filing_failure_is_surfaced_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        vrg_xdist_sync,
        "run_sweep",
        lambda *a, **k: [
            RepoResult(
                repo="/clones/weird", status="skipped", detail="NOT added", needs_followup=True
            ),
        ],
    )
    monkeypatch.setattr(
        vrg_xdist_sync.subprocess,
        "run",
        lambda *a, **k: _FakeProc(returncode=1, stderr="gh exploded"),
    )
    vrg_xdist_sync.main(["--repos", "/clones/weird"])
    out = capsys.readouterr().out
    assert "FAILED to file follow-up issue" in out
    assert "gh exploded" in out


def test_followup_filing_without_parseable_url(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        vrg_xdist_sync,
        "run_sweep",
        lambda *a, **k: [
            RepoResult(
                repo="/clones/weird", status="skipped", detail="NOT added", needs_followup=True
            ),
        ],
    )
    monkeypatch.setattr(
        vrg_xdist_sync.subprocess,
        "run",
        lambda *a, **k: _FakeProc(returncode=0, stdout="created (no url in output)"),
    )
    vrg_xdist_sync.main(["--repos", "/clones/weird"])
    out = capsys.readouterr().out
    assert "filed follow-up issue" in out
    assert "#" not in out.split("filed follow-up issue")[1].splitlines()[0]


def test_dry_run_followup_files_no_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dry-run needs_followup summary touches no GitHub state."""
    monkeypatch.setattr(
        vrg_xdist_sync,
        "run_sweep",
        lambda *a, **k: [
            RepoResult(
                repo="/clones/weird", status="skipped", detail="NOT added", needs_followup=True
            ),
        ],
    )

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("dry-run must not shell out")

    monkeypatch.setattr(vrg_xdist_sync.subprocess, "run", boom)
    vrg_xdist_sync.main(["--repos", "/clones/weird", "--dry-run"])
    out = capsys.readouterr().out
    assert "dry-run: would file a follow-up issue" in out


def test_main_requires_repos() -> None:
    with pytest.raises(SystemExit):
        vrg_xdist_sync.main([])
