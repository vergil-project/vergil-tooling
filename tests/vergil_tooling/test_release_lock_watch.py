"""Tests for the merge watch's release lock rule, the held-PR update (vergil-tooling#5414).

:class:`~vergil_tooling.lib.release_lock.watch.LockWatch` runs on the fake lock of
:mod:`tests.vergil_tooling.watch_lock_stubs`: the lock reads, the tracking issue, the
reclaim and the one PR path's arm are faked, and the update step
(``pr_path.update_held_pr``) is the fake's, scripted per test. The step itself, with
real git, is tested in ``test_pr_path.py``.
"""

from __future__ import annotations

import pytest

from tests.vergil_tooling.watch_lock_stubs import FakeLock, broken, record
from vergil_tooling.lib import pr_path
from vergil_tooling.lib.finalize_watch import LockView, Snapshot
from vergil_tooling.lib.release_lock.watch import LockWatch, LockWatchError

PR = "https://github.com/o/r/pull/1"
#: The PR's head before the release's bump PR moved ``develop`` on, and after the rebase.
OLD = "1" * 40
NEW = "2" * 40
PUSHED = f"rebased onto develop and pushed {NEW[:12]} (was {OLD[:12]}); CI re-running"

#: Lock states a hold can read as: a live release at each stage up to the unlock, an
#: unreadable lock, and a broken release whose tracking issue is still open.
HELD_STATES: list[object] = [
    record(stage="back-merge-bump"),
    record(stage="teardown-worktree"),
    record(stage="confirm-develop"),
    record(stage="unlock"),
    "the lock record cannot be parsed",
    broken(),
]


def snap(head: str = OLD, *, auto_merge_on: bool = False) -> Snapshot:
    return Snapshot(
        node_id="PR_1",
        url=PR,
        title="feat: change 1",
        state="OPEN",
        base="develop",
        head_ref="feature/1-thing",
        head=head,
        auto_merge_on=auto_merge_on,
        merge_state="BEHIND",
        rollup=(),
        merge_commit="",
        labels=(),
    )


def watch() -> LockWatch:
    return LockWatch(actor="human on test-host")


def test_nothing_is_updated_or_armed_while_the_lock_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = FakeLock(states=[*HELD_STATES[:4], None]).install(monkeypatch)
    lock_watch = watch()

    for _ in range(4):
        assert lock_watch(snap()).held
        assert lock.updates == []
        assert lock.arms == []

    view = lock_watch(snap())

    assert view.state == "free"
    assert lock.updates == [PR]
    assert lock.arms == [PR]


@pytest.mark.parametrize("held", HELD_STATES[4:], ids=["unreadable", "broken"])
def test_a_hold_that_is_unreadable_or_broken_also_ends_in_one_update(
    monkeypatch: pytest.MonkeyPatch, held: object
) -> None:
    lock = FakeLock(states=[held, None]).install(monkeypatch)
    lock_watch = watch()

    assert lock_watch(snap()).state in {"unreadable", "broken"}
    assert lock.updates == []

    assert lock_watch(snap()).state == "free"
    assert lock.updates == [PR]


def test_a_held_pr_is_rebased_pushed_and_reported_updated_then_rearmed_on_a_later_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    updated_at_old = pr_path.HeldUpdate("updated", PR, detail=PUSHED, old_head=OLD, new_head=NEW)
    lock = FakeLock(
        states=[record(stage="back-merge-bump"), None],
        update=[updated_at_old, pr_path.HeldUpdate("current", PR)],
    ).install(monkeypatch)
    lock_watch = watch()
    assert lock_watch(snap(OLD)).state == "held"

    updated = lock_watch(snap(OLD))

    assert updated == LockView("updated", PUSHED)
    assert not updated.held
    assert not updated.slow
    assert lock.updates == [PR]
    assert lock.arms == []
    # GitHub has not shown the pushed head yet: still pending, and not updated again.
    assert lock_watch(snap(OLD)) == LockView("updated", PUSHED)
    assert lock.updates == [PR]
    assert lock.arms == []
    # The pushed head is read: it is up to date, so the free-lock path re-arms the PR.
    assert lock_watch(snap(NEW)).state == "free"
    assert lock.updates == [PR, PR]
    assert lock.arms == [PR]


def test_a_held_pr_already_on_the_new_develop_is_rearmed_with_no_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = FakeLock(
        states=[record(stage="back-merge-bump"), None],
        update=pr_path.HeldUpdate("current", PR),
    ).install(monkeypatch)
    lock_watch = watch()
    assert lock_watch(snap(NEW)).state == "held"

    assert lock_watch(snap(NEW)).state == "free"
    assert lock.updates == [PR]
    assert lock.arms == [PR]


def test_a_conflicting_rebase_is_reported_conflicted_and_not_rearmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "the rebase of feature/1-thing onto develop conflicts in VERSION"
    update = pr_path.HeldUpdate("conflicted", PR, detail=detail, old_head=OLD)
    lock = FakeLock(states=[record(), None], update=update).install(monkeypatch)
    lock_watch = watch()
    assert lock_watch(snap(OLD)).state == "held"

    assert lock_watch(snap(OLD)) == LockView("conflicted", detail)
    assert lock.arms == []


def test_a_behind_pr_this_clone_cannot_update_is_rearmed_as_before_and_its_detail_shown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    detail = "behind develop and not updated from this clone: it has another branch checked out"
    update = pr_path.HeldUpdate("not_updated", PR, detail=detail)
    lock = FakeLock(states=[record(), None], update=update).install(monkeypatch)
    lock_watch = watch()
    assert lock_watch(snap(OLD)).state == "held"

    assert lock_watch(snap(OLD)).state == "free"
    assert lock.arms == [PR]
    assert detail in capsys.readouterr().out


def test_a_behind_pr_this_clone_cannot_update_with_auto_merge_on_reports_the_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "behind develop and not updated from this clone: its tree has changes"
    update = pr_path.HeldUpdate("not_updated", PR, detail=detail)
    lock = FakeLock(states=[record(), None], update=update).install(monkeypatch)
    lock_watch = watch()
    assert lock_watch(snap(OLD, auto_merge_on=True)).state == "held"

    assert lock_watch(snap(OLD, auto_merge_on=True)) == LockView("free", detail)
    assert lock.arms == []


def test_a_pr_freed_between_two_runs_is_updated_by_the_first_read_of_the_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first run saw the hold and exited; the next run's watch has no record of it.
    updated_at_old = pr_path.HeldUpdate("updated", PR, detail=PUSHED, old_head=OLD, new_head=NEW)
    lock = FakeLock(
        states=[None], update=[updated_at_old, pr_path.HeldUpdate("current", PR)]
    ).install(monkeypatch)
    next_run = watch()

    assert next_run(snap(OLD)) == LockView("updated", PUSHED)
    assert lock.updates == [PR]
    assert lock.arms == []
    # Still the pushed head is not on GitHub: pending, not updated again, not re-armed.
    assert next_run(snap(OLD)) == LockView("updated", PUSHED)
    assert lock.updates == [PR]
    assert lock.arms == []
    # The pushed head is read: it is up to date, so the free-lock path re-arms the PR.
    assert next_run(snap(NEW)).state == "free"
    assert lock.updates == [PR, PR]
    assert lock.arms == [PR]


def test_a_fresh_run_on_a_pr_already_up_to_date_leaves_it_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = FakeLock(states=[None], update=pr_path.HeldUpdate("current", PR)).install(monkeypatch)

    assert watch()(snap(NEW, auto_merge_on=True)) == LockView("free", "")
    assert lock.updates == [PR]
    assert lock.arms == []


def test_a_pr_never_read_held_behind_develop_is_updated_and_not_rearmed_in_the_same_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = pr_path.HeldUpdate("updated", PR, detail=PUSHED, old_head=OLD, new_head=NEW)
    lock = FakeLock(states=[None], update=update).install(monkeypatch)

    assert watch()(snap(OLD)) == LockView("updated", PUSHED)
    assert lock.updates == [PR]
    assert lock.arms == []


def test_a_behind_pr_this_clone_cannot_update_reports_its_detail_once_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    detail = "behind develop and not updated from this clone: its tree has changes"
    lock = FakeLock(
        states=[None], update=pr_path.HeldUpdate("not_updated", PR, detail=detail)
    ).install(monkeypatch)
    lock_watch = watch()

    assert lock_watch(snap(OLD, auto_merge_on=True)) == LockView("free", detail)
    assert lock_watch(snap(OLD, auto_merge_on=True)) == LockView("free", "")
    assert lock_watch(snap(OLD, auto_merge_on=True)) == LockView("free", "")
    assert lock.updates == [PR, PR, PR]
    assert lock.arms == []

    # A fresh run reports the detail again, once.
    assert watch()(snap(OLD, auto_merge_on=True)) == LockView("free", detail)


def test_a_reclaimed_hold_updates_the_pr_before_it_is_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = pr_path.HeldUpdate("updated", PR, detail=PUSHED, old_head=OLD, new_head=NEW)
    lock = FakeLock(states=[broken()], issue_state="CLOSED", update=update).install(monkeypatch)

    view = watch()(snap(OLD))

    assert view.state == "updated"
    assert view.detail.startswith("reclaimed the stale release lock of release 2.1.400")
    assert view.detail.endswith(PUSHED)
    assert len(lock.reclaims) == 1
    assert lock.updates == [PR]
    assert lock.arms == []


def test_an_update_that_cannot_be_completed_raises_naming_the_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = FakeLock(
        states=[record(), None],
        update=pr_path.PRPathError("push mode: cannot read the current clone"),
    ).install(monkeypatch)
    lock_watch = watch()
    assert lock_watch(snap(OLD)).state == "held"

    with pytest.raises(LockWatchError, match=f"{PR}: bringing the held PR up to date failed"):
        lock_watch(snap(OLD))
    assert lock.arms == []
