"""A batch member's span, on a real batch branch: its own commit and fixups, never a later issue's.

Real git (the ``batch_repo`` fixture), so the parent, the fixup run and the
rebase are git's own. Three issues, one commit each, in that order; a fixup
of an issue is made right after its commit, as ``vrg-commit --fixup`` makes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.merge_review.record import BatchSpan, RecordError, batch_member_span
from vergil_tooling.lib.pr_workflow.batch_marker import Marker

if TYPE_CHECKING:
    from tests.vergil_tooling.batch_repo import BatchRepo

NUMBERS = (1, 2, 3)
PATHS = {1: "c.txt", 2: "d.txt", 3: "e.txt"}


def _marker(batch_repo: BatchRepo, base_sha: str | None = None) -> Marker:
    """The batch marker of issues 1 to 3, with *base_sha* (default: the current develop tip)."""
    base = base_sha or batch_repo.git("rev-parse", "origin/develop")
    return Marker(issues=["#1", "#2", "#3"], base_sha=base)


def _branch(batch_repo: BatchRepo, fixup_of: int | None = None) -> dict[int, BatchSpan]:
    """Commit issues 1 to 3 in order, folding a fixup of *fixup_of* into its run; each issue's span.

    The span's parent is the commit before the issue's own commit on the
    branch, which is the develop tip for issue 1.
    """
    chain = [batch_repo.git("rev-parse", "origin/develop")]
    spans: dict[int, BatchSpan] = {}
    for number in NUMBERS:
        own = batch_repo.commit_issue(number, {PATHS[number]: f"{number}\n"})
        last = own
        if number == fixup_of:
            last = batch_repo.fixup(own, {PATHS[number]: f"{number}, fixed\n"})
        spans[number] = BatchSpan(parent=chain[-1], own=own, last=last)
        chain.extend([own] if last == own else [own, last])
    return spans


def _changed(batch_repo: BatchRepo, span: BatchSpan) -> list[str]:
    """The paths the span's change touches, as ``git diff --name-only`` lists them."""
    return batch_repo.git("diff", "--name-only", span.parent, span.last).splitlines()


def _sha_of(batch_repo: BatchRepo, subject: str) -> str:
    """The sha of the one commit of ``origin/develop..HEAD`` whose subject is *subject*."""
    lines = batch_repo.git("log", "--format=%H%x1f%s", "origin/develop..HEAD").splitlines()
    found = [line.split("\x1f", 1)[0] for line in lines if line.split("\x1f", 1)[1] == subject]
    assert len(found) == 1, found
    return found[0]


@pytest.mark.parametrize("number", NUMBERS)
@pytest.mark.parametrize("with_fixup", [False, True])
def test_span_is_the_members_own_commit(
    batch_repo: BatchRepo, number: int, with_fixup: bool
) -> None:
    spans = _branch(batch_repo, fixup_of=number if with_fixup else None)
    span = batch_member_span(batch_repo.path, _marker(batch_repo), f"#{number}", batch_repo.head())
    assert span == spans[number]
    assert _changed(batch_repo, span) == [PATHS[number]]


def test_middle_span_excludes_the_later_commit(batch_repo: BatchRepo) -> None:
    spans = _branch(batch_repo)
    span = batch_member_span(batch_repo.path, _marker(batch_repo), "#2", batch_repo.head())
    assert span.parent == batch_repo.git("rev-parse", f"{span.own}^")
    assert span.last == span.own == spans[2].own
    assert PATHS[3] not in _changed(batch_repo, span)


@pytest.mark.parametrize("number", NUMBERS)
def test_span_after_a_clean_rebase_is_the_rebased_own_commit(
    batch_repo: BatchRepo, number: int
) -> None:
    _branch(batch_repo, fixup_of=number)
    stale = _marker(batch_repo)
    batch_repo.advance_develop({"f.txt": "develop\n"})
    batch_repo.rebase_onto_develop()
    span = batch_member_span(batch_repo.path, stale, f"#{number}", batch_repo.head())
    own = _sha_of(batch_repo, f"change {number} (#{number})")
    assert span.own == own
    assert span.last == _sha_of(batch_repo, f"fixup! change {number} (#{number})")
    # The marker's base is the develop tip from before the rebase, so the span's
    # parent is the rebased parent, never the stale base.
    assert span.parent == batch_repo.git("rev-parse", f"{own}^")
    assert span.parent != stale.base_sha
    assert _changed(batch_repo, span) == [PATHS[number]]


def test_span_ignores_the_marker_base(batch_repo: BatchRepo) -> None:
    spans = _branch(batch_repo)
    stale = Marker(issues=["#1", "#2", "#3"], base_sha="0" * 40)
    assert batch_member_span(batch_repo.path, stale, "#2", batch_repo.head()) == spans[2]


def test_span_refuses_an_issue_not_in_the_batch(batch_repo: BatchRepo) -> None:
    _branch(batch_repo)
    with pytest.raises(RecordError, match="#9 is not in the batch"):
        batch_member_span(batch_repo.path, _marker(batch_repo), "#9", batch_repo.head())


def test_span_refuses_a_member_with_no_commit(batch_repo: BatchRepo) -> None:
    batch_repo.commit_issue(1, {PATHS[1]: "1\n"})
    batch_repo.commit_issue(3, {PATHS[3]: "3\n"})
    with pytest.raises(RecordError, match="holds 0"):
        batch_member_span(batch_repo.path, _marker(batch_repo), "#2", batch_repo.head())


def test_span_refuses_a_fixup_behind_another_commit(batch_repo: BatchRepo) -> None:
    batch_repo.commit_issue(1, {PATHS[1]: "1\n"})
    own = batch_repo.commit_issue(2, {PATHS[2]: "2\n"})
    batch_repo.commit_issue(3, {PATHS[3]: "3\n"})
    batch_repo.fixup(own, {PATHS[2]: "2, fixed\n"})
    with pytest.raises(RecordError, match="fold it into its commit first"):
        batch_member_span(batch_repo.path, _marker(batch_repo), "#2", batch_repo.head())
