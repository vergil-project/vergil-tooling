"""The disarm and re-arm sweeps (epic vergil-project/.github#1664, Task 20).

The arming-ref wait runs against a local bare repository served as
``origin`` (see :mod:`tests.vergil_tooling.release_lock_stubs`), with a fake
clock that only the wait's own sleeps advance. The disarm and re-arm sweeps
run against :class:`FakeGitHub`, which answers the ``gh`` reads and the
GraphQL mutation the sweeps make; the one PR path's ``arm`` is replaced
where a re-arm sweep is judged.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.vergil_tooling.release_lock_stubs import REPO, Origin, make_origin
from vergil_tooling.lib import github, pr_path
from vergil_tooling.lib.release_lock import (
    RefReadError,
    RefWriteError,
    ReleaseLockError,
    arming,
    sweeps,
)
from vergil_tooling.lib.release_lock.model import (
    ARMING_EXPIRY_SECS,
    ARMING_POLL_SECS,
    ARMING_WAIT_BOUND_SECS,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# --- the arming-ref wait -----------------------------------------------------


@pytest.fixture
def origin(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Origin:
    return make_origin(tmp_path, monkeypatch)


class FakeTime:
    """A clock that only :meth:`sleep` advances; *events* run once their time has come."""

    def __init__(self, events: list[tuple[float, Callable[[], object]]] | None = None) -> None:
        self.now = 0.0
        self.events = sorted(events or [], key=lambda e: e[0])
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        assert secs >= 0
        self.sleeps.append(secs)
        self.now += secs
        while self.events and self.events[0][0] <= self.now:
            _when, event = self.events.pop(0)
            event()


def _deletions(monkeypatch: pytest.MonkeyPatch, time: FakeTime) -> list[tuple[int, float]]:
    """Record each arming-ref delete the wait makes that lands, with the time it landed."""
    landed: list[tuple[int, float]] = []
    real = arming.delete

    def delete(repo: str, pr: int, *, expected: str) -> bool:
        done = real(repo, pr, expected=expected)
        if done:
            landed.append((pr, time.now))
        return done

    monkeypatch.setattr(arming, "delete", delete)
    return landed


def _wait(time: FakeTime) -> list[int]:
    return sweeps.wait_for_arming_refs(REPO, clock=time.clock, sleep=time.sleep)


def test_wait_returns_at_once_with_no_arming_refs(origin: Origin) -> None:
    del origin
    time = FakeTime()
    assert _wait(time) == []
    assert time.sleeps == []


def test_wait_deletes_only_value_unchanged_for_expiry(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PR 3's ref is stuck; PR 7's caller renews it, then finishes and deletes
    # it itself; PR 9's ref first appears at t=100.
    stuck = arming.create(REPO, 3)
    seven = arming.create(REPO, 7)
    assert stuck is not None
    assert seven is not None
    renewed: dict[str, str] = {}
    own_delete = arming.delete  # PR 7's caller's, not the wait's (recorded below)

    def renew_seven() -> None:
        value = arming.renew(REPO, 7, expected=seven)
        assert value is not None
        renewed["7"] = value

    def finish_seven() -> None:
        assert own_delete(REPO, 7, expected=renewed["7"])

    def create_nine() -> None:
        assert arming.create(REPO, 9) is not None

    time = FakeTime([(100, create_nine), (200, renew_seven), (400, finish_seven)])
    landed = _deletions(monkeypatch, time)

    assert _wait(time) == [3, 9]
    deleted_at = dict(landed)
    assert set(deleted_at) == {3, 9}
    # Each deleted value was seen unchanged for the full expiry, from the
    # wait's own first sighting of it (PR 9's at t=100, not t=0).
    assert ARMING_EXPIRY_SECS <= deleted_at[3] < ARMING_EXPIRY_SECS + ARMING_POLL_SECS
    assert 100 + ARMING_EXPIRY_SECS <= deleted_at[9] < 100 + ARMING_EXPIRY_SECS + ARMING_POLL_SECS
    assert arming.list_arming(REPO) == {}


def test_wait_renewal_restarts_expiry_clock(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = arming.create(REPO, 5)
    assert first is not None

    def renew() -> None:
        assert arming.renew(REPO, 5, expected=first) is not None

    time = FakeTime([(250, renew)])
    landed = _deletions(monkeypatch, time)

    assert _wait(time) == [5]
    [(pr, when)] = landed
    assert pr == 5
    # Never deleted at t=300 for the value first seen at t=0: the renewal at
    # t=250 restarted the clock.
    assert 250 + ARMING_EXPIRY_SECS <= when < 250 + ARMING_EXPIRY_SECS + 2 * ARMING_POLL_SECS
    del origin


def test_wait_delete_mismatch_is_not_counted(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A value renewed between the listing and the delete stays, measured afresh."""
    first = arming.create(REPO, 6)
    assert first is not None
    time = FakeTime()
    real = arming.delete
    attempts: list[float] = []

    def delete(repo: str, pr: int, *, expected: str) -> bool:
        attempts.append(time.now)
        if len(attempts) == 1:
            assert arming.renew(REPO, 6, expected=first) is not None
        return real(repo, pr, expected=expected)

    monkeypatch.setattr(arming, "delete", delete)

    assert _wait(time) == [6]
    assert len(attempts) == 2
    assert attempts[1] - attempts[0] >= ARMING_EXPIRY_SECS
    del origin


def test_wait_past_bound_raises_naming_refs(origin: Origin) -> None:
    del origin
    sha = arming.create(REPO, 4)
    assert sha is not None
    value = {"sha": sha}

    def renew() -> None:
        renewed = arming.renew(REPO, 4, expected=value["sha"])
        assert renewed is not None
        value["sha"] = renewed

    # Renewed every 200s, so it never stays unchanged for the expiry.
    time = FakeTime([(t, renew) for t in range(200, 2000, 200)])
    with pytest.raises(sweeps.SweepError) as caught:
        _wait(time)
    assert "refs/vergil/release-lock-arming/4" in str(caught.value)
    assert ARMING_WAIT_BOUND_SECS <= time.now < ARMING_WAIT_BOUND_SECS + ARMING_POLL_SECS
    assert isinstance(caught.value, ReleaseLockError)


def test_wait_listing_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(repo: str) -> dict[int, str]:
        raise RefReadError("ls-remote failed")

    monkeypatch.setattr(arming, "list_arming", fail)
    with pytest.raises(sweeps.SweepError, match="ls-remote failed"):
        _wait(FakeTime())


def test_wait_delete_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def delete(repo: str, pr: int, *, expected: str) -> bool:
        raise RefWriteError("push failed")

    monkeypatch.setattr(arming, "list_arming", lambda repo: {8: "a" * 40})
    monkeypatch.setattr(arming, "delete", delete)
    with pytest.raises(sweeps.SweepError, match=r"#8.*push failed"):
        _wait(FakeTime())


# --- a fake GitHub for the disarm and re-arm sweeps ---------------------------

_AUTO = {"mergeMethod": "SQUASH"}


def _url(number: int) -> str:
    return f"https://github.com/{REPO}/pull/{number}"


class FakeGitHub:
    """The open PRs into develop of :data:`REPO`, as ``gh`` and GraphQL show them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.prs: dict[int, dict[str, Any]] = {}
        self.disabled: list[int] = []
        self.lists = 0
        #: Run after the listing with this (1-based) index has been answered.
        self.after_list: dict[int, Callable[[], object]] = {}
        #: Numbers whose disable mutation fails, and whether it still lands.
        self.fail_disable: dict[int, bool] = {}
        #: Numbers whose disable mutation succeeds but leaves auto-merge on.
        self.stuck: set[int] = set()
        #: Numbers that merge the moment the disable mutation is sent.
        self.merge_on_disable: set[int] = set()
        monkeypatch.setattr(github, "read_json", self.read_json)
        monkeypatch.setattr(github, "graphql", self.graphql)

    def add(self, number: int, *, armed: bool, draft: bool = False) -> str:
        self.prs[number] = {
            "number": number,
            "url": _url(number),
            "id": f"PR_{number}",
            "isDraft": draft,
            "state": "OPEN",
            "autoMergeRequest": dict(_AUTO) if armed else None,
        }
        return _url(number)

    def _by_url(self, url: str) -> dict[str, Any]:
        [pr] = [p for p in self.prs.values() if p["url"] == url]
        return pr

    def read_json(self, *args: str) -> object:
        if args[:2] == ("pr", "list"):
            assert args[args.index("--repo") + 1] == REPO
            assert args[args.index("--base") + 1] == "develop"
            fields = args[args.index("--json") + 1].split(",")
            listing = [{f: p[f] for f in fields} for p in self.prs.values() if p["state"] == "OPEN"]
            self.lists += 1
            hook = self.after_list.get(self.lists)
            if hook is not None:
                hook()
            return listing
        if args[:2] == ("pr", "view"):
            pr = self._by_url(args[2])
            return {f: pr[f] for f in args[args.index("--json") + 1].split(",")}
        msg = f"unexpected gh call: {args}"
        raise AssertionError(msg)

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        assert "disablePullRequestAutoMerge" in query
        [pr] = [p for p in self.prs.values() if p["id"] == variables["id"]]
        number = pr["number"]
        self.disabled.append(number)
        if number in self.merge_on_disable:
            pr["state"] = "MERGED"
        if number in self.fail_disable:
            if self.fail_disable[number]:
                pr["autoMergeRequest"] = None
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
        if number not in self.stuck:
            pr["autoMergeRequest"] = None
        return {"disablePullRequestAutoMerge": {"pullRequest": {"number": number}}}

    def armed(self, number: int) -> bool:
        return self.prs[number]["autoMergeRequest"] is not None


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    return FakeGitHub(monkeypatch)


# --- the disarm sweep ---------------------------------------------------------


def test_disarm_turns_off_every_armed_pr_and_confirms(gh: FakeGitHub) -> None:
    one = gh.add(1, armed=True)
    gh.add(2, armed=False)
    three = gh.add(3, armed=True, draft=True)
    assert sweeps.disarm_all(REPO) == [one, three]
    assert gh.disabled == [1, 3]
    assert not any(gh.armed(n) for n in (1, 2, 3))


def test_disarm_failure_raises(gh: FakeGitHub) -> None:
    url = gh.add(1, armed=True)
    gh.fail_disable[1] = False
    with pytest.raises(sweeps.SweepError) as caught:
        sweeps.disarm_all(REPO)
    assert url in str(caught.value)
    assert "HTTP 502" in str(caught.value)


def test_disarm_error_that_landed_is_success(gh: FakeGitHub) -> None:
    url = gh.add(1, armed=True)
    gh.fail_disable[1] = True
    assert sweeps.disarm_all(REPO) == [url]


def test_disarm_not_confirmed_raises(gh: FakeGitHub) -> None:
    url = gh.add(1, armed=True)
    gh.stuck.add(1)
    with pytest.raises(sweeps.SweepError, match="still on") as caught:
        sweeps.disarm_all(REPO)
    assert url in str(caught.value)
    assert gh.armed(1)


def test_disarm_pr_merged_meanwhile_is_not_counted(gh: FakeGitHub) -> None:
    gh.add(1, armed=True)
    two = gh.add(2, armed=True)
    gh.merge_on_disable.add(1)
    gh.fail_disable[1] = False
    assert sweeps.disarm_all(REPO) == [two]


def test_disarm_listing_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: str) -> object:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 500")

    monkeypatch.setattr(github, "read_json", fail)
    with pytest.raises(sweeps.SweepError, match="HTTP 500"):
        sweeps.disarm_all(REPO)


def test_disarm_node_id_read_failure_raises(gh: FakeGitHub) -> None:
    gh.add(1, armed=True)
    gh.prs[1]["id"] = None
    with pytest.raises(sweeps.SweepError, match="node ID"):
        sweeps.disarm_all(REPO)


def test_disarm_non_object_view_raises(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    gh.add(1, armed=True)
    real = gh.read_json

    def read_json(*args: str) -> object:
        return [] if args[:2] == ("pr", "view") else real(*args)

    monkeypatch.setattr(github, "read_json", read_json)
    with pytest.raises(sweeps.SweepError, match="gh pr view returned"):
        sweeps.disarm_all(REPO)


def test_disarm_unexpected_state_raises(gh: FakeGitHub) -> None:
    gh.add(1, armed=True)
    gh.stuck.add(1)
    gh.after_list[1] = lambda: gh.prs[1].update(state="WEIRD")
    with pytest.raises(sweeps.SweepError, match="WEIRD"):
        sweeps.disarm_all(REPO)


# --- the quiesce ----------------------------------------------------------------


def _no_arming_refs(monkeypatch: pytest.MonkeyPatch, *, expired: list[int]) -> None:
    monkeypatch.setattr(sweeps, "wait_for_arming_refs", lambda repo, **kw: list(expired))
    monkeypatch.setattr(arming, "list_arming", lambda repo: {})


def test_quiesce_disarms_once_with_nothing_expired(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_arming_refs(monkeypatch, expired=[])
    url = gh.add(1, armed=True)
    result = sweeps.quiesce(REPO)
    assert result == sweeps.QuiesceResult(expired=(), disarmed=(url,), late=(), arming_at_check=())
    # The first sweep, then the final check: no second sweep.
    assert gh.lists == 2


def test_quiesce_second_disarm_after_expired_ref_deleted(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The arm call of PR 12's expired arming ref lands after the first sweep.
    _no_arming_refs(monkeypatch, expired=[12])
    url = gh.add(12, armed=False)
    gh.after_list[1] = lambda: gh.prs[12].update(autoMergeRequest=dict(_AUTO))
    result = sweeps.quiesce(REPO)
    assert result.expired == (12,)
    assert result.disarmed == (url,)
    assert result.late == ()
    assert gh.lists == 3
    assert not gh.armed(12)


def test_quiesce_final_check_disarms_late_arm(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_arming_refs(monkeypatch, expired=[])
    url = gh.add(13, armed=False)
    gh.after_list[1] = lambda: gh.prs[13].update(autoMergeRequest=dict(_AUTO))
    result = sweeps.quiesce(REPO)
    assert result.disarmed == ()
    assert result.late == (url,)
    assert not gh.armed(13)


def test_quiesce_reports_arming_refs_at_final_check(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweeps, "wait_for_arming_refs", lambda repo, **kw: [])
    monkeypatch.setattr(arming, "list_arming", lambda repo: {21: "b" * 40})
    gh.add(1, armed=False)
    assert sweeps.quiesce(REPO).arming_at_check == (21,)


def test_quiesce_final_listing_failure_raises(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(repo: str) -> dict[int, str]:
        raise RefReadError("ls-remote failed")

    monkeypatch.setattr(sweeps, "wait_for_arming_refs", lambda repo, **kw: [])
    monkeypatch.setattr(arming, "list_arming", fail)
    with pytest.raises(sweeps.SweepError, match="ls-remote failed"):
        sweeps.quiesce(REPO)
    del gh


def test_quiesce_passes_clock_and_sleep_to_the_wait(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def wait(repo: str, **kw: object) -> list[int]:
        seen.update(kw)
        return []

    monkeypatch.setattr(sweeps, "wait_for_arming_refs", wait)
    monkeypatch.setattr(arming, "list_arming", lambda repo: {})
    time = FakeTime()
    sweeps.quiesce(REPO, clock=time.clock, sleep=time.sleep)
    assert seen == {"clock": time.clock, "sleep": time.sleep}
    del gh


# --- the re-arm sweep -----------------------------------------------------------


class FakeArm:
    """``pr_path.arm``: each PR's scripted outcome, or an exception to raise."""

    def __init__(self, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
        self.gh = gh
        self.outcomes: dict[str, str | Exception] = {}
        self.details: dict[str, str] = {}
        self.calls: list[tuple[str, str | None]] = []
        #: Run when the PR is offered, before its outcome: another caller's work.
        self.before: dict[str, Callable[[], object]] = {}
        monkeypatch.setattr(pr_path, "arm", self)

    def __call__(self, pr_url: str, *, holder_pass: str | None = None) -> pr_path.ArmResult:
        self.calls.append((pr_url, holder_pass))
        if pr_url in self.before:
            self.before[pr_url]()
        outcome = self.outcomes.get(pr_url, "armed")
        if isinstance(outcome, Exception):
            raise outcome
        pr = self.gh._by_url(pr_url)  # noqa: SLF001
        if outcome == "armed":
            pr["autoMergeRequest"] = dict(_AUTO)
        elif outcome == "merged":
            pr["state"] = "MERGED"
        return pr_path.ArmResult(
            outcome=cast("pr_path.ArmOutcome", outcome),
            pr_url=pr_url,
            head="c" * 40,
            method="SQUASH",
            detail=self.details.get(pr_url, ""),
        )


@pytest.fixture
def arm(gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> FakeArm:
    return FakeArm(gh, monkeypatch)


def test_rearm_skips_drafts(gh: FakeGitHub, arm: FakeArm) -> None:
    one = gh.add(1, armed=False)
    gh.add(2, armed=False, draft=True)
    gh.add(3, armed=True)
    result = sweeps.rearm_all(REPO)
    assert [url for url, _pass in arm.calls] == [one]
    assert result == sweeps.RearmResult(armed=[one], merged=[], held=[], refused=[])


def test_rearm_idempotent(gh: FakeGitHub, arm: FakeArm) -> None:
    one = gh.add(1, armed=False)
    two = gh.add(2, armed=False)
    assert sweeps.rearm_all(REPO).armed == [one, two]
    # A re-run offers only the PRs still unarmed: none.
    assert sweeps.rearm_all(REPO) == sweeps.RearmResult(armed=[], merged=[], held=[], refused=[])
    assert len(arm.calls) == 2


def test_rearm_reports_refused_outsider_pr(gh: FakeGitHub, arm: FakeArm) -> None:
    url = gh.add(1, armed=False)
    arm.outcomes[url] = "refused"
    arm.details[url] = "the one PR path does not accept it: not a same-repo PR"
    result = sweeps.rearm_all(REPO)
    assert result.refused == [(url, "the one PR path does not accept it: not a same-repo PR")]
    assert result.armed == []
    assert not gh.armed(1)


def test_rearm_clean_status_pr_merges(gh: FakeGitHub, arm: FakeArm) -> None:
    url = gh.add(1, armed=False)
    arm.outcomes[url] = "merged"
    assert sweeps.rearm_all(REPO).merged == [url]


def test_rearm_reports_held_pr(gh: FakeGitHub, arm: FakeArm) -> None:
    url = gh.add(1, armed=False)
    arm.outcomes[url] = "held"
    arm.details[url] = "held for release 2.3.0"
    assert sweeps.rearm_all(REPO).held == [url]


def test_rearm_passes_holder_pass(gh: FakeGitHub, arm: FakeArm) -> None:
    url = gh.add(1, armed=False)
    sweeps.rearm_all(REPO, holder_pass="tok")
    assert arm.calls == [(url, "tok")]


def test_rearm_collision_with_watch_counted_not_raised(gh: FakeGitHub, arm: FakeArm) -> None:
    # Each PR's own watch holds its arming ref. By the time the path gives up
    # waiting, the watch has armed PR 1 and merged PR 2; PR 3 is still unarmed.
    by_watch, merged, still = (gh.add(n, armed=False) for n in (1, 2, 3))
    for url in (by_watch, merged, still):
        arm.outcomes[url] = pr_path.ArmingCollisionError(f"{url}: another caller is arming it")
    arm.before[by_watch] = lambda: gh.prs[1].update(autoMergeRequest=dict(_AUTO))
    arm.before[merged] = lambda: gh.prs[2].update(state="MERGED")
    result = sweeps.rearm_all(REPO)
    assert result.armed == [by_watch]
    assert result.merged == [merged]
    assert result.held == []
    [(url, reason)] = result.refused
    assert url == still
    assert "another caller is arming it" in reason


def test_rearm_failed_arm_raises_after_trying_all(gh: FakeGitHub, arm: FakeArm) -> None:
    one = gh.add(1, armed=False)
    two = gh.add(2, armed=False)
    arm.outcomes[one] = pr_path.PRPathError(f"{one}: reading the PR failed: HTTP 502")
    with pytest.raises(sweeps.RearmSweepError) as caught:
        sweeps.rearm_all(REPO)
    assert one in str(caught.value)
    assert "HTTP 502" in str(caught.value)
    assert caught.value.result == sweeps.RearmResult(armed=[two], merged=[], held=[], refused=[])
    assert caught.value.failed == [(one, f"{one}: reading the PR failed: HTTP 502")]
    assert isinstance(caught.value, sweeps.SweepError)


def test_rearm_collision_state_read_failure_is_a_failure(
    gh: FakeGitHub, arm: FakeArm, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = gh.add(1, armed=False)
    arm.outcomes[url] = pr_path.ArmingCollisionError(f"{url}: another caller is arming it")
    real = gh.read_json

    def read_json(*args: str) -> object:
        if args[:2] == ("pr", "view"):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 503")
        return real(*args)

    monkeypatch.setattr(github, "read_json", read_json)
    with pytest.raises(sweeps.RearmSweepError, match="HTTP 503"):
        sweeps.rearm_all(REPO)


def test_rearm_listing_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: str) -> object:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 500")

    monkeypatch.setattr(github, "read_json", fail)
    with pytest.raises(sweeps.SweepError, match="HTTP 500"):
        sweeps.rearm_all(REPO)
