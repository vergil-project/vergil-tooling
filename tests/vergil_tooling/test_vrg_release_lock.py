"""``vrg-release-lock status`` and the lock status it reports (epic .github#1664, Task 18)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin import vrg_release_lock
from vergil_tooling.lib.pr_path import PRPathError
from vergil_tooling.lib.release_lock import status as lock_status
from vergil_tooling.lib.release_lock.model import LockRecord, RefReadError
from vergil_tooling.lib.release_lock.refs import LockState
from vergil_tooling.lib.release_lock.stale import IssueReadError, Staleness
from vergil_tooling.lib.release_lock.status import HeldPR, LockStatus, StatusError

_REPO = "vergil-project/vergil-tooling"
_SHA = "b" * 40
_NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
_STATUS = "vergil_tooling.lib.release_lock.status"
_BIN = "vergil_tooling.bin.vrg_release_lock"

_RECORD_FIELDS = (
    "holder_identity",
    "holder_host",
    "version",
    "worktree",
    "tracking_issue",
    "stage",
    "health",
    "broken_stage",
    "started_at",
    "heartbeat_at",
    "token",
)


def _record(**changes: str) -> LockRecord:
    fields = {
        "holder_identity": "human",
        "holder_host": "mac.local",
        "version": "2.1.400",
        "worktree": "/w/.worktrees/release-2.1.400",
        "tracking_issue": f"{_REPO}#5300",
        "stage": "cut",
        "health": "running",
        "broken_stage": "",
        "started_at": "2026-11-27T11:00:00Z",
        "heartbeat_at": (_NOW - timedelta(seconds=65)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token": "a1b2c3",
    }
    fields.update(changes)
    return LockRecord(**fields)  # type: ignore[arg-type]


def _pr(number: int, *, draft: bool = False, armed: bool = False) -> dict[str, object]:
    return {
        "number": number,
        "url": f"https://github.com/{_REPO}/pull/{number}",
        "isDraft": draft,
        "autoMergeRequest": {"enabledAt": "2026-11-27T10:00:00Z"} if armed else None,
    }


# -- gather -----------------------------------------------------------------------


def _gather(
    state: LockState | Exception,
    *,
    staleness: Staleness | Exception | None = None,
    prs: object = None,
    refusals: dict[int, str | None | Exception] | None = None,
) -> tuple[LockStatus, dict[str, MagicMock]]:
    refusals = refusals or {}

    def accepts(url: str) -> str | None:
        found = refusals.get(int(url.rsplit("/", 1)[1]))
        if isinstance(found, Exception):
            raise found
        return found

    judged = staleness or Staleness(stale=False, live=True, reason="live", tracking_issue="")
    mocks = {
        "read_lock": MagicMock(
            side_effect=state if isinstance(state, Exception) else None,
            return_value=None if isinstance(state, Exception) else state,
        ),
        "judge": MagicMock(
            side_effect=judged if isinstance(judged, Exception) else None,
            return_value=None if isinstance(judged, Exception) else judged,
        ),
        "read_json": MagicMock(
            side_effect=prs if isinstance(prs, Exception) else None,
            return_value=[] if prs is None else prs,
        ),
        "accepts": MagicMock(side_effect=accepts),
    }
    with (
        patch(f"{_STATUS}.read_lock", mocks["read_lock"]),
        patch(f"{_STATUS}.stale.judge", mocks["judge"]),
        patch(f"{_STATUS}.github.read_json", mocks["read_json"]),
        patch(f"{_STATUS}.pr_path.accepts", mocks["accepts"]),
    ):
        result = lock_status.gather(_REPO, now=_NOW)
    return result, mocks


def test_gather_absent_reads_no_prs() -> None:
    result, mocks = _gather(LockState(sha=None, record=None, error=""))
    assert not result.state.held
    assert result.held_prs == ()
    assert result.heartbeat_age_secs is None
    mocks["read_json"].assert_not_called()


def test_gather_held_lists_unarmed_prs_the_path_accepts() -> None:
    prs = [_pr(1), _pr(2, draft=True), _pr(3, armed=True), _pr(4), _pr(5)]
    result, mocks = _gather(
        LockState(sha=_SHA, record=_record(), error=""),
        prs=prs,
        refusals={4: "it is not a same-repo PR with a trusted author on its branch"},
    )
    assert [p.number for p in result.held_prs] == [1, 5]
    assert result.held_prs[0].url == f"https://github.com/{_REPO}/pull/1"
    assert result.heartbeat_age_secs == 65
    # Only an open, non-draft, unarmed PR is judged by the path.
    judged = [c.args[0].rsplit("/", 1)[1] for c in mocks["accepts"].call_args_list]
    assert judged == ["1", "4", "5"]
    args = mocks["read_json"].call_args.args
    assert args[:2] == ("pr", "list")
    assert args[args.index("--base") + 1] == "develop"
    assert args[args.index("--state") + 1] == "open"
    assert args[args.index("--repo") + 1] == _REPO


def test_gather_unreadable_lock_still_lists_held_prs() -> None:
    result, _ = _gather(LockState(sha=_SHA, record=None, error="not JSON"), prs=[_pr(7)])
    assert [p.number for p in result.held_prs] == [7]
    assert result.heartbeat_age_secs is None


@pytest.mark.parametrize(
    "prs",
    [
        subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
        {"not": "a list"},
        [{"number": 1}],
        [_pr(1)] * 200,
    ],
)
def test_gather_pr_listing_failure_raises(prs: object) -> None:
    with pytest.raises(StatusError, match="open PRs"):
        _gather(LockState(sha=_SHA, record=_record(), error=""), prs=prs)


def test_gather_pr_judgement_failure_raises() -> None:
    with pytest.raises(StatusError, match="#1"):
        _gather(
            LockState(sha=_SHA, record=_record(), error=""),
            prs=[_pr(1)],
            refusals={1: PRPathError("reading the PR failed")},
        )


# -- the JSON and text forms ---------------------------------------------------------


def _status(state: LockState, **kwargs: Any) -> LockStatus:
    fields: dict[str, Any] = {
        "repo": _REPO,
        "state": state,
        "staleness": Staleness(stale=False, live=True, reason="live", tracking_issue=""),
        "heartbeat_age_secs": None,
        "held_prs": (),
    }
    fields.update(kwargs)
    return LockStatus(**fields)


def test_status_json_fields() -> None:
    rec = _record()
    st = _status(
        LockState(sha=_SHA, record=rec, error=""),
        staleness=Staleness(
            stale=False,
            live=True,
            reason="running with a fresh heartbeat",
            tracking_issue=rec.tracking_issue,
        ),
        heartbeat_age_secs=65,
        held_prs=(HeldPR(number=12, url=f"https://github.com/{_REPO}/pull/12"),),
    )
    data = st.as_json()
    for name in _RECORD_FIELDS:
        assert data[name] == getattr(rec, name)
    assert data["repo"] == _REPO
    assert data["held"] is True
    assert data["sha"] == _SHA
    assert data["error"] == ""
    assert data["stale"] is False
    assert data["live"] is True
    assert data["reason"] == "running with a fresh heartbeat"
    assert data["judged_tracking_issue"] == rec.tracking_issue
    assert data["heartbeat_age_secs"] == 65
    assert data["held_prs"] == [{"number": 12, "url": f"https://github.com/{_REPO}/pull/12"}]
    json.dumps(data)


def test_status_json_no_lock_has_every_key() -> None:
    absent = _status(
        LockState(sha=None, record=None, error=""),
        staleness=Staleness(
            stale=False, live=False, reason="no release lock held", tracking_issue=""
        ),
    ).as_json()
    held = _status(LockState(sha=_SHA, record=_record(), error="")).as_json()
    assert absent.keys() == held.keys()
    assert absent["held"] is False
    assert absent["sha"] is None
    for name in _RECORD_FIELDS:
        assert absent[name] is None
    assert absent["held_prs"] == []


def test_status_text_lines() -> None:
    rec = _record(health="broken", broken_stage="cut")
    lines = lock_status.text_lines(
        _status(
            LockState(sha=_SHA, record=rec, error=""),
            staleness=Staleness(
                stale=False,
                live=False,
                reason=f"its tracking issue {rec.tracking_issue} is open",
                tracking_issue=rec.tracking_issue,
            ),
            heartbeat_age_secs=1000,
            held_prs=(HeldPR(number=12, url="u12"), HeldPR(number=14, url="u14")),
        )
    )
    text = "\n".join(lines)
    assert "holder: human on mac.local" in text
    assert "version: 2.1.400" in text
    assert f"tracking issue: {_REPO}#5300" in text
    assert "stage: cut" in text
    assert "health: broken (at cut)" in text
    assert "heartbeat: " in text
    assert "(1000s ago)" in text
    assert f"stale: no (its tracking issue {_REPO}#5300 is open)" in text
    assert "held PRs: #12, #14" in text


def test_status_text_tracking_issue_found_by_version() -> None:
    rec = _record(tracking_issue="")
    lines = lock_status.text_lines(
        _status(
            LockState(sha=_SHA, record=rec, error=""),
            staleness=Staleness(
                stale=False, live=False, reason="r", tracking_issue=f"{_REPO}#5301"
            ),
        )
    )
    assert f"tracking issue: {_REPO}#5301 (open release issue of 2.1.400; not in the lock)" in lines


def test_status_text_no_tracking_issue() -> None:
    lines = lock_status.text_lines(
        _status(LockState(sha=_SHA, record=_record(tracking_issue="", worktree=""), error=""))
    )
    assert "tracking issue: none" in lines
    assert "worktree: none" in lines
    assert any(line.startswith("heartbeat: ") and "ago" not in line for line in lines)


def test_status_text_unreadable() -> None:
    lines = lock_status.text_lines(
        _status(
            LockState(sha=_SHA, record=None, error="refs/vergil/release-lock is unreadable: x"),
            staleness=Staleness(stale=False, live=False, reason="unreadable", tracking_issue=""),
        )
    )
    assert lines[0] == "release lock: held, unreadable: refs/vergil/release-lock is unreadable: x"
    assert "stale: no (unreadable)" in lines
    assert "held PRs: none" in lines


# -- the command --------------------------------------------------------------------


def _main(
    argv: list[str], *, result: LockStatus | Exception, remote: str | None = None
) -> tuple[int, MagicMock]:
    gather = MagicMock(
        side_effect=result if isinstance(result, Exception) else None,
        return_value=None if isinstance(result, Exception) else result,
    )
    url = remote if remote is not None else f"https://github.com/{_REPO}.git"
    with (
        patch(f"{_BIN}.lock_status.gather", gather),
        patch(f"{_STATUS}.git.remote_url", return_value=url),
    ):
        rc = vrg_release_lock.main(argv)
    return rc, gather


def test_status_no_lock(capsys: pytest.CaptureFixture[str]) -> None:
    absent = _status(
        LockState(sha=None, record=None, error=""),
        staleness=Staleness(
            stale=False, live=False, reason="no release lock held", tracking_issue=""
        ),
    )
    rc, gather = _main(["status"], result=absent)
    assert rc == 0
    assert capsys.readouterr().out == "no release lock held\n"
    assert gather.call_args.args == (_REPO,)
    rc, _ = _main(["status", "--json"], result=absent)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["held"] is False


def test_status_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    held = _status(LockState(sha=_SHA, record=_record(), error=""), heartbeat_age_secs=65)
    rc, _ = _main(["status", "--json"], result=held)
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == "2.1.400"
    assert data["heartbeat_age_secs"] == 65


def test_status_repo_flag(capsys: pytest.CaptureFixture[str]) -> None:
    absent = _status(LockState(sha=None, record=None, error=""))
    rc, gather = _main(["status", "--repo", "vergil-project/.github"], result=absent)
    assert rc == 0
    assert gather.call_args.args == ("vergil-project/.github",)
    capsys.readouterr()


@pytest.mark.parametrize(
    "error",
    [
        RefReadError("reading refs/vergil/release-lock from origin failed: HTTP 502"),
        IssueReadError("could not read the state of x#1"),
        StatusError("could not list the open PRs"),
    ],
)
def test_status_read_failure_exits_1(error: Exception, capsys: pytest.CaptureFixture[str]) -> None:
    rc, _ = _main(["status", "--json"], result=error)
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(error) in captured.err


def test_status_non_github_origin_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    rc, gather = _main(
        ["status"], result=_status(LockState(None, None, "")), remote="git@example.com:x/y.git"
    )
    assert rc == 1
    gather.assert_not_called()
    assert "--repo" in capsys.readouterr().err


def test_status_bad_repo_is_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_release_lock.main(["status", "--repo", "not a repo"])
    assert exc.value.code == 2


def test_status_runs_under_an_agent_identity(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    rc, _ = _main(["status"], result=_status(LockState(None, None, "")))
    assert rc == 0
    capsys.readouterr()


def test_status_origin_read_failure_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    failure = subprocess.CalledProcessError(2, ["git", "remote", "get-url"], stderr="no origin")
    with (
        patch(f"{_STATUS}.git.remote_url", side_effect=failure),
        patch(f"{_BIN}.lock_status.gather") as gather,
    ):
        rc = vrg_release_lock.main(["status"])
    assert rc == 1
    gather.assert_not_called()
    assert "could not read the origin remote" in capsys.readouterr().err
