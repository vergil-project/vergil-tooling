"""The batch mechanics on a real repository: folds, conflicts and rebases, then the two checks.

The commit-per-issue rule and the merge-review gate both read the branch, so
these tests run real git through :mod:`tests.vergil_tooling.batch_repo` and
fake only the GitHub comment reads, through the shared
:mod:`tests.vergil_tooling.review_thread`.
"""

from __future__ import annotations

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.batch_repo import BatchRepo, RebaseConflictError
from tests.vergil_tooling.review_thread import AUTHORS, Thread
from vergil_tooling.lib import agent_gates, github
from vergil_tooling.lib.agent_gates import evaluate
from vergil_tooling.lib.change_fingerprint import commit_fingerprint
from vergil_tooling.lib.pr_workflow.batch_rules import BatchRuleError, check_commit_per_issue

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
