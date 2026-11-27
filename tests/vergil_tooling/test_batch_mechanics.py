"""The batch mechanics on a real repository: folds, conflicts, rebases, records and the checks.

The commit-per-issue rule and the merge-review gate both read the branch, so
these tests run real git through :mod:`tests.vergil_tooling.batch_repo` and
fake only the GitHub comment reads, through the shared
:mod:`tests.vergil_tooling.review_thread`.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.batch_repo import BatchRepo, RebaseConflictError
from tests.vergil_tooling.review_thread import AUTHORS, Thread
from vergil_tooling.bin import vrg_pr_workflow
from vergil_tooling.lib import agent_gates, finalize_watch, github, pr_path, trust
from vergil_tooling.lib.agent_gates import evaluate
from vergil_tooling.lib.change_fingerprint import change_fingerprint, commit_fingerprint
from vergil_tooling.lib.github import ClosingIssue, PRClosingReferences
from vergil_tooling.lib.merge_review import record
from vergil_tooling.lib.merge_review.record import batch_member_change, covered_change
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.pr_workflow.batch_rules import BatchRuleError, check_commit_per_issue

if TYPE_CHECKING:
    from pathlib import Path

ONE = "o/r#7"
TWO = "o/r#8"
THREE = "o/r#9"
FOUR = "o/r#10"
REFS3 = [ONE, TWO, THREE]
REFS4 = [ONE, TWO, THREE, FOUR]


@pytest.fixture
def items(monkeypatch: pytest.MonkeyPatch) -> trust_stubs.FakeItems:
    fake = trust_stubs.FakeItems(strict=True)
    monkeypatch.setattr(github, "graphql", fake)
    return fake


@pytest.fixture
def thread(items: trust_stubs.FakeItems) -> Thread:
    return Thread(items)


def _gate(repo: BatchRepo, refs: list[str]) -> agent_gates.GateResult:
    return evaluate(repo.path, base="origin/develop", head="HEAD", issue_refs=refs, authors=AUTHORS)


def _check(repo: BatchRepo, monkeypatch: pytest.MonkeyPatch, refs: list[str]) -> None:
    monkeypatch.chdir(repo.path)
    check_commit_per_issue(refs, base="origin/develop", head="HEAD")


def _branch(repo: BatchRepo) -> list[str]:
    """The shas of the branch's commits since ``origin/develop``, oldest first."""
    return repo.git("rev-list", "--reverse", "origin/develop..HEAD").split()


def _record_all(repo: BatchRepo, thread: Thread, refs: list[str], shas: list[str]) -> None:
    for ref, sha in zip(refs, shas, strict=True):
        thread.record(ref, sha, commit_fingerprint(repo.path, sha))


def _four(repo: BatchRepo) -> list[str]:
    return [
        repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"}),
        repo.commit_issue(8, {"b.txt": "uno\nDOS\n"}),
        repo.commit_issue(9, {"c.txt": "tres\n"}),
        repo.commit_issue(10, {"d.txt": "cuatro\n"}),
    ]


# --- folds ----------------------------------------------------------------------------


def test_fold_into_middle_of_chain_of_four(
    batch_repo: BatchRepo, thread: Thread, monkeypatch: pytest.MonkeyPatch
) -> None:
    shas = _four(batch_repo)
    _record_all(batch_repo, thread, REFS4, shas)
    assert _gate(batch_repo, REFS4).ok

    batch_repo.fixup(shas[1], {"b.txt": "uno\nDOS\nfolded\n"})
    batch_repo.autosquash()
    folded = _branch(batch_repo)
    assert len(folded) == 4
    _check(batch_repo, monkeypatch, REFS4)

    # Commit 2 is re-reviewed; the records of commits 3 and 4 predate that review.
    thread.record(TWO, folded[1], commit_fingerprint(batch_repo.path, folded[1]))
    result = _gate(batch_repo, REFS4)
    assert not result.ok
    assert len(result.reasons) == 2
    assert f"commit {folded[2][:12]} (" in result.reasons[0]
    assert f"predates the review of commit {folded[1][:12]}" in result.reasons[0]
    assert f"commit {folded[3][:12]} (" in result.reasons[1]
    assert f"predates the review of commit {folded[2][:12]}" in result.reasons[1]
    assert not any(f"commit {folded[0][:12]}" in reason for reason in result.reasons)

    thread.record(THREE, folded[2], commit_fingerprint(batch_repo.path, folded[2]))
    thread.record(FOUR, folded[3], commit_fingerprint(batch_repo.path, folded[3]))
    assert _gate(batch_repo, REFS4).ok


def test_fold_conflicting_with_later_commit(
    batch_repo: BatchRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"})
    second = batch_repo.commit_issue(8, {"b.txt": "uno\nDOS\n"})
    batch_repo.commit_issue(9, {"b.txt": "uno\ntres\n"})
    fixup = batch_repo.fixup(second, {"b.txt": "uno\nFIXED\n"})
    before = batch_repo.head()

    with pytest.raises(RebaseConflictError):
        batch_repo.autosquash()
    assert batch_repo.head() == before == fixup
    assert not batch_repo.rebase_in_progress()

    monkeypatch.chdir(batch_repo.path)
    with pytest.raises(BatchRuleError, match="unfolded fixup") as caught:
        check_commit_per_issue(REFS3, base="origin/develop", head="HEAD")
    assert fixup[:12] in str(caught.value)


# --- rebases --------------------------------------------------------------------------


def test_long_lived_batch_rebased_onto_moved_develop(
    batch_repo: BatchRepo, thread: Thread, monkeypatch: pytest.MonkeyPatch
) -> None:
    shas = [
        batch_repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"}),
        batch_repo.commit_issue(8, {"b.txt": "uno\nDOS\n"}),
        batch_repo.commit_issue(9, {"c.txt": "tres\n"}),
    ]
    _record_all(batch_repo, thread, REFS3, shas)

    batch_repo.advance_develop({"d.txt": "moved\n"})
    batch_repo.advance_develop({"e.txt": "moved again\n"})
    before = batch_repo.head()
    batch_repo.rebase_onto_develop()
    assert batch_repo.head() != before
    assert len(_branch(batch_repo)) == 3

    _check(batch_repo, monkeypatch, REFS3)
    assert _gate(batch_repo, REFS3).ok


def test_rebase_after_merge_review_keeps_records(
    batch_repo: BatchRepo, thread: Thread, monkeypatch: pytest.MonkeyPatch
) -> None:
    shas = [
        batch_repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"}),
        batch_repo.commit_issue(8, {"b.txt": "uno\nDOS\n"}),
        batch_repo.commit_issue(9, {"c.txt": "tres\n"}),
    ]
    _record_all(batch_repo, thread, REFS3, shas)
    assert _gate(batch_repo, REFS3).ok

    # develop changes a file the batch changes, away from the batch's own lines,
    # so the rebase is clean and every commit keeps its fingerprint.
    batch_repo.advance_develop({"a.txt": "one\ntwo\nthree\nfour\n"})
    batch_repo.rebase_onto_develop()
    assert not batch_repo.rebase_in_progress()

    _check(batch_repo, monkeypatch, REFS3)
    assert _gate(batch_repo, REFS3).ok


# --- review records after the fold ----------------------------------------------------


def _marker(repo: BatchRepo, refs: list[str]) -> batch_marker.Marker:
    issues = [f"#{ref.partition('#')[2]}" for ref in refs]
    return batch_marker.Marker(issues=issues, base_sha=repo.git("rev-parse", "origin/develop"))


def test_records_name_commits_after_autosquash(batch_repo: BatchRepo, thread: Thread) -> None:
    marker = _marker(batch_repo, [ONE, TWO])
    first = batch_repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"})
    second = batch_repo.commit_issue(8, {"b.txt": "uno\nDOS\n"})
    fixup = batch_repo.fixup(second, {"b.txt": "uno\nDOS\nfolded\n"})
    thread.record(ONE, first, commit_fingerprint(batch_repo.path, first))

    # The record of issue 8, as the merge review writes it before the fold.
    covered = record.batch_member_change(batch_repo.path, marker, "#8", fixup)
    assert covered.head == fixup
    thread.record(TWO, covered.head, covered.fingerprint)

    batch_repo.autosquash()
    folded = _branch(batch_repo)
    assert len(folded) == 2
    assert fixup not in folded
    assert batch_repo.git("log", "--format=%s", "origin/develop..HEAD") == (
        "change 8 (#8)\nchange 7 (#7)"
    )
    # The folded commit's own fingerprint is the one the record carries.
    assert commit_fingerprint(batch_repo.path, folded[1]) == covered.fingerprint
    assert _gate(batch_repo, [ONE, TWO]).ok


def test_fixup_alone_is_never_a_covered_commit(batch_repo: BatchRepo, thread: Thread) -> None:
    first = batch_repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"})
    second = batch_repo.commit_issue(8, {"b.txt": "uno\nDOS\n"})
    fixup = batch_repo.fixup(second, {"b.txt": "uno\nDOS\nfolded\n"})
    thread.record(ONE, first, commit_fingerprint(batch_repo.path, first))
    # A record naming the fixup commit's own change covers no issue's commit.
    thread.record(TWO, fixup, commit_fingerprint(batch_repo.path, fixup))

    result = _gate(batch_repo, [ONE, TWO])
    assert not result.ok
    assert any(f"commit {second[:12]}" in reason for reason in result.reasons)
    assert any(f"commit {fixup[:12]}" in reason for reason in result.reasons)


def test_records_after_autosquash_then_clean_rebase(
    batch_repo: BatchRepo, thread: Thread, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = _marker(batch_repo, REFS3)
    first = batch_repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"})
    second = batch_repo.commit_issue(8, {"b.txt": "uno\nDOS\n"})
    third = batch_repo.commit_issue(9, {"c.txt": "tres\n"})
    fixup = batch_repo.fixup(third, {"c.txt": "tres\nfolded\n"})
    thread.record(ONE, first, commit_fingerprint(batch_repo.path, first))
    thread.record(TWO, second, commit_fingerprint(batch_repo.path, second))
    covered = record.batch_member_change(batch_repo.path, marker, "#9", fixup)
    thread.record(THREE, covered.head, covered.fingerprint)

    batch_repo.autosquash()
    assert len(_branch(batch_repo)) == 3
    assert _gate(batch_repo, REFS3).ok

    batch_repo.advance_develop({"a.txt": "one\ntwo\nthree\nfour\n"})
    before = _branch(batch_repo)
    batch_repo.rebase_onto_develop()
    assert _branch(batch_repo) != before
    assert not batch_repo.rebase_in_progress()

    _check(batch_repo, monkeypatch, REFS3)
    assert _gate(batch_repo, REFS3).ok


# --- a fix after submit ---------------------------------------------------------------

BRANCH = "feature"
PR_URL = "https://github.com/o/r/pull/7"


class _Pushes:
    """The ``git push`` calls push mode makes, never run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, **_kwargs: object) -> str:
        self.calls.append(args)
        return ""


@pytest.fixture
def push_mode(monkeypatch: pytest.MonkeyPatch, batch_repo: BatchRepo, items: object) -> _Pushes:
    """An open batch PR of issues 7, 8 and 9 for the clone, with the real gates and git.

    Only GitHub's side is stubbed: the PR, its refs and the push itself.
    """
    pushes = _Pushes()
    pr = pr_path._PR(  # noqa: SLF001
        snap=cast("finalize_watch.Snapshot", SimpleNamespace(url=PR_URL)),
        draft=False,
        repo="o/r",
        number=7,
        branch=BRANCH,
    )
    ref = SimpleNamespace(url=PR_URL)
    monkeypatch.setattr(pr_path, "_here", lambda: (batch_repo.path, "o/r"))
    monkeypatch.setattr(pr_path, "_open_develop_pr", lambda _repo, _branch: ref)
    monkeypatch.setattr(pr_path, "_read", lambda _url: pr)
    monkeypatch.setattr(pr_path, "_refusal", lambda _pr: None)
    monkeypatch.setattr(pr_path, "_violations", lambda _url: "")
    monkeypatch.setattr(pr_path.git, "run", pushes.run)
    monkeypatch.setattr(pr_path.identity_mode, "any_agent_signal", lambda: True)
    monkeypatch.setattr(finalize_watch, "pr_repo_and_number", lambda _snap: ("o/r", 7))
    monkeypatch.setattr(finalize_watch, "require_clone_of", lambda _clone, _repo, _pr: None)
    monkeypatch.setattr(finalize_watch, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(finalize_watch, "ready_state", lambda _clone, _repo, _branch: None)
    monkeypatch.setattr(finalize_watch, "task_issue_refs", lambda _repo, _n, _ready: REFS3)
    monkeypatch.setattr(
        finalize_watch, "_fetch_develop_for_gates", lambda _clone, _head: "origin/develop"
    )
    return pushes


def _submitted_batch(repo: BatchRepo, thread: Thread) -> list[str]:
    shas = [
        repo.commit_issue(7, {"a.txt": "one\nTWO\nthree\n"}),
        repo.commit_issue(8, {"b.txt": "uno\nDOS\n"}),
        repo.commit_issue(9, {"c.txt": "tres\n"}),
    ]
    _record_all(repo, thread, REFS3, shas)
    assert _gate(repo, REFS3).ok
    return shas


def test_fix_after_submit_through_push_mode(
    batch_repo: BatchRepo, thread: Thread, push_mode: _Pushes
) -> None:
    shas = _submitted_batch(batch_repo, thread)

    batch_repo.fixup(shas[1], {"b.txt": "uno\nDOS\nfolded\n"})
    batch_repo.autosquash()
    head = batch_repo.head()

    refused = pr_path.push_to_open_pr(BRANCH, head=head, lease=shas[2])
    assert not refused.pushed
    assert "the gates refused the head" in refused.detail
    assert push_mode.calls == []

    whole = change_fingerprint(batch_repo.path, "origin/develop", head)
    thread.record(ONE, head, whole)
    pushed = pr_path.push_to_open_pr(BRANCH, head=head, lease=shas[2])
    assert pushed.pushed
    assert push_mode.calls == [
        ("push", f"--force-with-lease={BRANCH}:{shas[2]}", "origin", f"{head}:refs/heads/{BRANCH}")
    ]


# --- the batch check ------------------------------------------------------------------

BATCH_BODY = "## Issue Linkage\n\nCloses #7\nCloses #8\nCloses #9\n"
MODULE = "vergil_tooling.bin.vrg_pr_workflow"


def _closing() -> PRClosingReferences:
    return PRClosingReferences(
        repo="o/r",
        number=7,
        state="OPEN",
        merged_at=None,
        issues=tuple(ClosingIssue(repo="o/r", number=n, state="OPEN") for n in (7, 8, 9)),
    )


def _batch_check(repo: BatchRepo, monkeypatch: pytest.MonkeyPatch, head: str) -> int:
    monkeypatch.chdir(repo.path)
    content = trust.IssueContent(
        body=BATCH_BODY,
        body_role=trust.Role.AGENTS,
        author_role=trust.Role.AGENTS,
        comments=(),
        skipped=(),
    )
    pr_view = {"baseRefName": "develop", "headRefOid": head}
    with (
        patch(f"{MODULE}.github.pr_repo_and_number", return_value=("o/r", 7)),
        patch(f"{MODULE}.github.read_json", return_value=pr_view),
        patch(f"{MODULE}.trust.read_issue", return_value=content),
        patch(f"{MODULE}.github.pr_closing_references", return_value=_closing()),
        patch(f"{MODULE}._clone_repo", return_value="o/r"),
    ):
        return vrg_pr_workflow.main(["--base", "develop", "batch-check", "--pr", PR_URL])


def test_fix_after_submit_batch_check_passes_and_refuses(
    batch_repo: BatchRepo,
    thread: Thread,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shas = _submitted_batch(batch_repo, thread)
    fixup = batch_repo.fixup(shas[1], {"b.txt": "uno\nDOS\nfolded\n"})
    assert _batch_check(batch_repo, monkeypatch, fixup) == 1
    err = capsys.readouterr().err
    assert "unfolded fixup" in err
    assert fixup[:12] in err

    batch_repo.autosquash()
    folded = batch_repo.head()
    assert _batch_check(batch_repo, monkeypatch, folded) == 0
    assert f"Batch check passed for PR {PR_URL} at {folded}" in capsys.readouterr().out


# --- the record of one batch member, through the real record path -----------------------


@pytest.fixture
def main_root(batch_repo: BatchRepo) -> Path:
    """The main clone's root for the markers: a directory of its own, beside the clone."""
    root = batch_repo.root / "main"
    root.mkdir()
    return root


def _mark(main_root: Path, batch_repo: BatchRepo, refs: list[str]) -> None:
    """Write every member's marker, with ``origin/develop`` as the batch's base."""
    base = batch_repo.git("rev-parse", "origin/develop")
    issues = [f"#{ref.split('#')[1]}" for ref in refs]
    batch_marker.write_markers(main_root, issues, base_sha=base)


def _record(batch_repo: BatchRepo, thread: Thread, main_root: Path, ref: str) -> None:
    """Post *ref*'s record as the record path computes it; the GitHub post is the only stub."""
    covered = covered_change(batch_repo.path, int(ref.split("#")[1]), main_root=main_root)
    thread.record(ref, covered.head, covered.fingerprint)


def test_member_record_for_middle_commit_with_later_commits(
    batch_repo: BatchRepo, main_root: Path
) -> None:
    shas = _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    marker = batch_marker.read_marker(main_root, "#8")
    assert marker is not None
    covered = covered_change(batch_repo.path, 8, main_root=main_root)
    assert covered.head == shas[1]
    assert covered.fingerprint == commit_fingerprint(batch_repo.path, shas[1])
    assert batch_member_change(batch_repo.path, marker, "#8", batch_repo.head()) == covered


def test_rereview_restores_coverage_after_middle_fold(
    batch_repo: BatchRepo, thread: Thread, main_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    for ref in REFS4:
        _record(batch_repo, thread, main_root, ref)
    assert _gate(batch_repo, REFS4).ok

    batch_repo.fixup(_branch(batch_repo)[1], {"b.txt": "uno\nDOS\nfolded\n"})
    batch_repo.autosquash()
    assert not _gate(batch_repo, REFS4).ok

    _record(batch_repo, thread, main_root, TWO)
    _record(batch_repo, thread, main_root, THREE)
    _record(batch_repo, thread, main_root, FOUR)
    assert _gate(batch_repo, REFS4).ok
    _check(batch_repo, monkeypatch, REFS4)


def test_member_change_excludes_later_commits(batch_repo: BatchRepo, main_root: Path) -> None:
    shas = _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    eight = covered_change(batch_repo.path, 8, main_root=main_root)
    assert eight.head == shas[1]
    assert eight.fingerprint == commit_fingerprint(batch_repo.path, shas[1])

    fixup = batch_repo.fixup(shas[3], {"d.txt": "cuatro\nfixed\n"})
    ten = covered_change(batch_repo.path, 10, main_root=main_root)
    assert ten.head == fixup
    assert ten.fingerprint == change_fingerprint(batch_repo.path, shas[2], fixup)
    assert ten.fingerprint != change_fingerprint(batch_repo.path, "origin/develop", fixup)


def test_member_record_for_first_commit_after_rebase(
    batch_repo: BatchRepo, main_root: Path
) -> None:
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    batch_repo.advance_develop({"moved.txt": "moved\n"})
    batch_repo.advance_develop({"moved2.txt": "moved again\n"})
    batch_repo.rebase_onto_develop()
    assert len(_branch(batch_repo)) == 4

    batch_repo.fixup(_branch(batch_repo)[0], {"a.txt": "one\nTWO\nfolded\n"})
    batch_repo.autosquash()
    first = _branch(batch_repo)[0]
    covered = covered_change(batch_repo.path, 7, main_root=main_root)
    assert covered.head == first
    assert covered.fingerprint == commit_fingerprint(batch_repo.path, first)
    assert covered.fingerprint != change_fingerprint(batch_repo.path, "origin/develop", "HEAD")


# --- the read-only probe: vrg-pr-workflow batch-gates ------------------------------------


@pytest.fixture
def probe(
    batch_repo: BatchRepo,
    main_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Run the probe in the batch's clone, its markers under *main_root*, GitHub reads faked."""
    monkeypatch.setattr(vrg_pr_workflow, "_clone_repo", lambda: "o/r")
    monkeypatch.setattr(vrg_pr_workflow, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(vrg_pr_workflow.git, "main_worktree_root", lambda: main_root)
    monkeypatch.chdir(batch_repo.path)
    return main_root


def _probe(*args: str) -> int:
    return vrg_pr_workflow.main(["batch-gates", *args])


def _verdicts(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(capsys.readouterr().out)
    return found


def test_batch_gates_reports_uncovered_after_fold(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    for ref in REFS4:
        _record(batch_repo, thread, main_root, ref)
    capsys.readouterr()
    assert _probe("--json") == 0
    assert [c["covered"] for c in _verdicts(capsys)["commits"]] == [True] * 4

    batch_repo.fixup(_branch(batch_repo)[1], {"b.txt": "uno\nDOS\nfolded\n"})
    batch_repo.autosquash()
    assert _probe("--json") == 1
    report = _verdicts(capsys)
    assert report["head"] == batch_repo.head()
    assert report["branch"] == "feature"
    commits = report["commits"]
    assert [c["issue"] for c in commits] == REFS4
    assert [c["covered"] for c in commits] == [True, False, False, False]
    assert commits[1]["sha"] == _branch(batch_repo)[1]
    assert commits[1]["reason"] is not None
    # Commit 3's record predates the review of the folded commit 2, which is the break.
    assert f"predates the review of commit {_branch(batch_repo)[1][:12]}" in commits[2]["reason"]


def test_batch_gates_all_covered_exits_0(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    for ref in REFS4:
        _record(batch_repo, thread, main_root, ref)
    capsys.readouterr()
    assert _probe() == 0
    lines = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in lines] == REFS4
    assert [line.split()[2] for line in lines] == ["covered"] * 4


def test_batch_gates_before_report_ready(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    for ref in REFS4:
        _record(batch_repo, thread, main_root, ref)
    handoff = batch_repo.path / ".vergil" / "pr-workflow.json"
    assert not handoff.exists()
    capsys.readouterr()
    assert _probe("--json") == 0
    assert not handoff.exists()
    assert _verdicts(capsys)["branch"] == "feature"


def test_batch_gates_after_report_ready_refuses(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    # report-ready removes the members' markers once it has recorded the state.
    batch_marker.remove_markers(main_root, ["#7", "#8", "#9", "#10"])
    capsys.readouterr()
    assert _probe() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no batch marker" in captured.err
    assert "report-ready removes the markers" in captured.err


def test_member_change_for_rebased_moved_develop_is_unchanged(
    batch_repo: BatchRepo, main_root: Path
) -> None:
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    before = covered_change(batch_repo.path, 9, main_root=main_root)
    batch_repo.advance_develop({"moved.txt": "moved\n"})
    batch_repo.rebase_onto_develop()
    after = covered_change(batch_repo.path, 9, main_root=main_root)
    third = _branch(batch_repo)[2]
    assert after.head == third
    assert after.fingerprint == before.fingerprint == commit_fingerprint(batch_repo.path, third)


def test_batch_gates_after_rebase_onto_moved_develop(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    for ref in REFS4:
        _record(batch_repo, thread, main_root, ref)
    batch_repo.advance_develop({"moved.txt": "moved\n"})
    batch_repo.advance_develop({"moved2.txt": "moved again\n"})
    batch_repo.rebase_onto_develop()
    capsys.readouterr()
    assert _probe("--json") == 0
    commits = _verdicts(capsys)["commits"]
    assert [c["covered"] for c in commits] == [True] * 4
    # develop's new commits are not part of the batch: the verdicts are the branch's own.
    assert [c["sha"] for c in commits] == _branch(batch_repo)
    assert len(commits) == 4


def test_batch_gates_matches_submit_gate(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    shas = _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    _record(batch_repo, thread, main_root, ONE)
    _record(batch_repo, thread, main_root, TWO)
    # An unfolded fixup of commit 8 after commit 10: no issue claims it.
    batch_repo.fixup(shas[1], {"b.txt": "uno\nDOS\nagain\n"})
    capsys.readouterr()

    expected = _gate(batch_repo, REFS4)
    assert not expected.ok
    assert _probe("--json") == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    per_commit = [c["reason"] for c in report["commits"] if c["reason"] is not None]
    extra = [line.removeprefix("batch-gates: ") for line in captured.err.splitlines()]
    assert per_commit + extra == list(expected.reasons)
    assert [c["covered"] for c in report["commits"]] == [True, True, False, False]
    assert len(extra) == 1
    assert "is no issue's own commit" in extra[0]


def _verdicts_of(batch_repo: BatchRepo, refs: list[str]) -> agent_gates.CommitVerdicts:
    return agent_gates.evaluate_commits(
        batch_repo.path, base="origin/develop", head="HEAD", issue_refs=refs, authors=AUTHORS
    )


def test_evaluate_commits_whole_change_covers_every_issue(
    batch_repo: BatchRepo, thread: Thread
) -> None:
    shas = _four(batch_repo)
    whole = change_fingerprint(batch_repo.path, "origin/develop", "HEAD")
    thread.record(ONE, batch_repo.head(), whole)
    verdicts = _verdicts_of(batch_repo, REFS4)
    assert verdicts.ok
    assert verdicts.reasons == ()
    assert [(c.issue, c.sha, c.covered, c.reason) for c in verdicts.commits] == [
        (ref, sha, True, None) for ref, sha in zip(REFS4, shas, strict=True)
    ]


def test_evaluate_commits_whole_change_for_a_single_issue_names_the_head(
    batch_repo: BatchRepo, thread: Thread
) -> None:
    _four(batch_repo)
    thread.record(
        ONE, batch_repo.head(), change_fingerprint(batch_repo.path, "origin/develop", "HEAD")
    )
    verdicts = _verdicts_of(batch_repo, [ONE])
    assert verdicts.ok
    assert verdicts.commits == (agent_gates.CommitVerdict(ONE, batch_repo.head(), True, None),)


def test_evaluate_commits_whole_change_names_no_commit_for_an_issue_without_one(
    batch_repo: BatchRepo, thread: Thread
) -> None:
    _four(batch_repo)
    thread.record(
        ONE, batch_repo.head(), change_fingerprint(batch_repo.path, "origin/develop", "HEAD")
    )
    verdicts = _verdicts_of(batch_repo, [*REFS4, "o/r#11"])
    assert verdicts.ok
    assert verdicts.commits[-1] == agent_gates.CommitVerdict("o/r#11", None, True, None)


def test_evaluate_commits_single_issue_uncovered_names_the_head(
    batch_repo: BatchRepo, thread: Thread
) -> None:
    _four(batch_repo)
    thread.empty(ONE)
    verdicts = _verdicts_of(batch_repo, [ONE])
    assert not verdicts.ok
    (only,) = verdicts.commits
    assert only.sha == batch_repo.head()
    assert not only.covered
    assert only.reason is not None
    assert "run merge review" in only.reason


def test_evaluate_commits_refuses_an_unreadable_head(
    batch_repo: BatchRepo, thread: Thread, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four(batch_repo)
    thread.empty(ONE)
    real = agent_gates.git.read_output

    def unreadable(*args: str) -> str:
        if args[-1] == "HEAD^{commit}":
            raise subprocess.CalledProcessError(128, ["git", "rev-parse"])
        return real(*args)

    monkeypatch.setattr(agent_gates.git, "read_output", unreadable)
    verdicts = _verdicts_of(batch_repo, [ONE])
    assert verdicts.read_failure
    assert verdicts.commits == ()
    assert "cannot read the commit HEAD" in verdicts.extra_reasons[0]


def test_evaluate_commits_reports_an_unusable_issue_ref(batch_repo: BatchRepo) -> None:
    _four(batch_repo)
    verdicts = _verdicts_of(batch_repo, ["not-a-ref"])
    assert not verdicts.ok
    assert verdicts.commits == ()
    assert verdicts.extra_reasons[0].startswith("task issue:")


def test_batch_gates_writes_nothing(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    for ref in REFS4:
        _record(batch_repo, thread, main_root, ref)
    batch_repo.fixup(_branch(batch_repo)[1], {"b.txt": "uno\nDOS\nfolded\n"})
    batch_repo.autosquash()
    before = _snapshot(batch_repo, main_root)
    capsys.readouterr()
    assert _probe("--json") == 1
    assert _snapshot(batch_repo, main_root) == before


def test_batch_gates_read_failure_exits_1(
    batch_repo: BatchRepo,
    thread: Thread,
    main_root: Path,
    probe: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    thread.empty(*REFS4)
    _four(batch_repo)
    _mark(main_root, batch_repo, REFS4)
    batch_repo.git("remote", "set-url", "origin", str(batch_repo.root / "gone.git"))
    capsys.readouterr()
    assert _probe("--json") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cannot fetch origin develop" in captured.err


def _snapshot(batch_repo: BatchRepo, main_root: Path) -> dict[str, object]:
    """Everything a probe could change: the clone's status and refs, the markers, the handoff."""
    markers = {
        str(path.relative_to(main_root)): path.read_text()
        for path in sorted(main_root.rglob("*"))
        if path.is_file()
    }
    dirs = sorted(
        str(path.relative_to(main_root)) for path in main_root.rglob("*") if path.is_dir()
    )
    return {
        "status": batch_repo.git("status", "--porcelain=v1", "--ignored"),
        "refs": batch_repo.git("for-each-ref", "--format=%(refname) %(objectname)"),
        "head": batch_repo.head(),
        "markers": markers,
        "dirs": dirs,
        "handoff": (batch_repo.path / ".vergil" / "pr-workflow.json").exists(),
    }
