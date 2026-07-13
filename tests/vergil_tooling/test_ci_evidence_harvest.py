"""Tests for the GitHub harvest layer in vergil_tooling.lib.ci_evidence.

All GitHub I/O is mocked at the ``vergil_tooling.lib.github`` boundary
(``read_json`` / ``read_output`` / ``run``), mirroring the ``test_github.py``
monkeypatch style: the harvest functions are pure orchestration over those
wrappers, so the tests assert the selection/filter logic, never the network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib import ci_evidence, github
from vergil_tooling.lib.ci_evidence import (
    NoQualifyingRunError,
    download_evidence_artifacts,
    read_gate_conclusions,
    resolve_release_pr,
    select_ci_run,
)

if TYPE_CHECKING:
    from pathlib import Path


# --- resolve_release_pr -------------------------------------------------


def test_resolve_release_pr_prefers_merged_from_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {"number": 10, "merged_at": None},
            {"number": 42, "merged_at": "2026-07-12T00:00:00Z"},
        ],
    )
    assert resolve_release_pr("o/r", "abc") == 42


def test_resolve_release_pr_falls_back_to_first_unmerged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [{"number": 7, "merged_at": None}, {"number": 8}],
    )
    assert resolve_release_pr("o/r", "abc") == 7


def test_resolve_release_pr_uses_subject_when_api_has_no_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "read_json", lambda *a, **k: {})
    monkeypatch.setattr(
        github,
        "read_output",
        lambda *a, **k: "Merge pull request #55 from o/release/x\n\nbody",
    )
    assert resolve_release_pr("o/r", "abc") == 55


def test_resolve_release_pr_uses_squash_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", lambda *a, **k: [])
    monkeypatch.setattr(github, "read_output", lambda *a, **k: "chore(release): prepare (#99)")
    assert resolve_release_pr("o/r", "abc") == 99


def test_resolve_release_pr_raises_when_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", lambda *a, **k: [])
    monkeypatch.setattr(github, "read_output", lambda *a, **k: "")
    with pytest.raises(ValueError, match="cannot resolve release PR"):
        resolve_release_pr("o/r", "abc")


# --- select_ci_run ------------------------------------------------------


def test_select_ci_run_ignores_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "cancelled",
                "run_started_at": "2026-07-12T00:00:00Z",
                "id": 1,
            },
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "2026-07-12T00:05:00Z",
                "id": 42,
            },
        ],
    )
    assert select_ci_run("o/r", "abc")["id"] == 42


def test_select_ci_run_picks_latest_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "2026-07-12T00:00:00Z",
                "id": 1,
            },
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "2026-07-12T00:09:00Z",
                "id": 2,
            },
        ],
    )
    assert select_ci_run("o/r", "abc")["id"] == 2


def test_select_ci_run_ignores_other_workflow_and_in_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {
                "name": "Nightly",
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "t",
                "id": 1,
            },
            {
                "name": "CI",
                "status": "in_progress",
                "conclusion": None,
                "run_started_at": "t",
                "id": 2,
            },
            {
                "name": "CI",
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "t",
                "id": 3,
            },
        ],
    )
    assert select_ci_run("o/r", "abc")["id"] == 3


def test_select_ci_run_honors_workflow_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {
                "name": "Release",
                "status": "completed",
                "conclusion": "success",
                "run_started_at": "t",
                "id": 7,
            },
        ],
    )
    assert select_ci_run("o/r", "abc", workflow="Release")["id"] == 7


def test_select_ci_run_none_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {"name": "CI", "status": "completed", "conclusion": "cancelled", "run_started_at": "t"},
        ],
    )
    with pytest.raises(NoQualifyingRunError) as exc:
        select_ci_run("o/r", "deadbeef")
    assert exc.value.head_sha == "deadbeef"


# --- download_evidence_artifacts ----------------------------------------


def test_download_evidence_artifacts_filters_prefix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {"name": "ci-evidence-test"},
            {"name": "ci-evidence-security"},
            {"name": "build-logs"},
        ],
    )
    calls: list[tuple[str, ...]] = []

    def _record_run(*args: str) -> None:
        calls.append(args)

    monkeypatch.setattr(github, "run", _record_run)

    dest = tmp_path / "artifacts"
    result = download_evidence_artifacts("o/r", 99, dest)

    assert result == [dest / "security", dest / "test"]
    assert (dest / "test").is_dir()
    assert (dest / "security").is_dir()
    assert not (dest / "build-logs").exists()
    downloaded_names = {a[a.index("--name") + 1] for a in calls}
    assert downloaded_names == {"ci-evidence-test", "ci-evidence-security"}
    # run-id and repo are threaded to every download call.
    assert all("99" in a and "o/r" in a for a in calls)


def test_download_evidence_artifacts_none_matching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "read_json", lambda *a, **k: [{"name": "coverage"}])
    monkeypatch.setattr(github, "run", lambda *a: None)
    assert download_evidence_artifacts("o/r", 1, tmp_path) == []


# --- read_gate_conclusions ----------------------------------------------


def test_read_gate_conclusions_maps_name_to_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        github,
        "read_json",
        lambda *a, **k: [
            {"name": "test / unit / 3.14", "conclusion": "success"},
            {"name": "security / codeql", "conclusion": None},
        ],
    )
    result = read_gate_conclusions("o/r", "abc")
    assert result == {"test / unit / 3.14": "success", "security / codeql": ""}


def test_qualifying_run_predicate_is_reusable() -> None:
    run: dict[str, Any] = {"name": "CI", "status": "completed", "conclusion": "success"}
    assert ci_evidence._is_qualifying_run(run, "CI")
    assert not ci_evidence._is_qualifying_run({**run, "conclusion": "cancelled"}, "CI")
