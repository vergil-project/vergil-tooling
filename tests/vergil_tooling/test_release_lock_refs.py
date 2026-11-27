"""The lock ref and the lock history ref (epic vergil-project/.github#1664, Task 17).

Exercised against a local bare repository served as ``origin``; see
:mod:`tests.vergil_tooling.release_lock_stubs`.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.vergil_tooling.release_lock_stubs import REPO, FlakyPush, Origin, make_origin
from vergil_tooling.lib import git, retry, trust
from vergil_tooling.lib.release_lock import RefReadError, RefWriteError, history, refs
from vergil_tooling.lib.release_lock.model import LockRecord, to_json

if TYPE_CHECKING:
    from pathlib import Path

#: The real ``origin_url``, kept before the fixture points it at the bare origin.
_REAL_ORIGIN_URL = refs.origin_url


def _fail_git(monkeypatch: pytest.MonkeyPatch, subcommand: str) -> None:
    """Make every ``lib.git`` read of *subcommand* fail."""
    real = git.read_output

    def read_output(*args: str) -> str:
        if subcommand in args:
            raise subprocess.CalledProcessError(1, ("git", *args), stderr=f"{subcommand} broke")
        return real(*args)

    monkeypatch.setattr(git, "read_output", read_output)


_IDENT = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    return make_origin(tmp_path, monkeypatch)


def _record(stage: str = "pre-check", token: str = "t1") -> LockRecord:
    return LockRecord(
        holder_identity="human",
        holder_host="mac.local",
        version="2.1.400",
        worktree="",
        tracking_issue="",
        stage=stage,
        health="running",
        broken_stage="",
        started_at="2026-11-27T10:00:00Z",
        heartbeat_at="2026-11-27T10:01:00Z",
        token=token,
    )


def _entry(action: str = "reclaim", actor: str = "user-agent") -> history.HistoryEntry:
    return history.HistoryEntry(
        action=action,  # type: ignore[arg-type]
        old=to_json(_record()),
        actor=actor,
        at="2026-11-27T11:00:00Z",
        reason="stale",
        rearmed=(),
    )


# --- the lock ref -----------------------------------------------------------------


def test_absent_lock_reads_as_free(origin: Origin) -> None:
    del origin
    state = refs.read_lock(REPO)
    assert state == refs.LockState(sha=None, record=None, error="")
    assert not state.held


def test_take_lock_create_only_second_take_returns_none(origin: Origin) -> None:
    sha = refs.take_lock(REPO, _record())
    assert sha is not None
    assert origin.sha(refs.LOCK_REF) == sha
    assert refs.take_lock(REPO, _record(token="t2")) is None
    state = refs.read_lock(REPO)
    assert state.held
    assert state.sha == sha
    assert state.record == _record()
    assert state.error == ""
    assert json.loads(origin.file(refs.LOCK_REF, refs.LOCK_FILE))["token"] == "t1"


def test_update_lock_cas_mismatch_returns_none(origin: Origin) -> None:
    first = refs.take_lock(REPO, _record())
    assert first is not None
    second = refs.update_lock(REPO, _record(stage="cut"), expected=first)
    assert second is not None
    assert second != first
    assert refs.update_lock(REPO, _record(stage="tag"), expected=first) is None
    assert origin.sha(refs.LOCK_REF) == second
    assert refs.read_lock(REPO).record == _record(stage="cut")


def test_update_absent_lock_is_a_mismatch(origin: Origin) -> None:
    del origin
    assert refs.update_lock(REPO, _record(), expected="1" * 40) is None


def test_delete_lock_cas_mismatch_returns_false_and_keeps_ref(origin: Origin) -> None:
    first = refs.take_lock(REPO, _record())
    assert first is not None
    second = refs.update_lock(REPO, _record(stage="cut"), expected=first)
    assert refs.delete_lock(REPO, expected=first) is False
    assert origin.sha(refs.LOCK_REF) == second
    assert second is not None
    assert refs.delete_lock(REPO, expected=second) is True
    assert origin.sha(refs.LOCK_REF) is None
    assert not refs.read_lock(REPO).held


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("{not json", "not valid JSON"),
        (json.dumps({"health": "running"}), "missing"),
        (None, "UTF-8"),
    ],
)
def test_unparseable_lock_reads_as_held(origin: Origin, text: str | None, match: str) -> None:
    if text is None:
        blob = _non_utf8_blob(origin)
        tree = origin.git("mktree", stdin=f"100644 blob {blob}\t{refs.LOCK_FILE}\n")
        sha = origin.git("commit-tree", tree, "-m", "bad", env=_IDENT)
    else:
        sha = origin.commit(refs.LOCK_FILE, text)
    origin.set_ref(refs.LOCK_REF, sha)
    state = refs.read_lock(REPO)
    assert state.held
    assert state.sha == sha
    assert state.record is None
    assert match in state.error
    # Nothing takes or overwrites it.
    assert refs.take_lock(REPO, _record()) is None
    assert origin.sha(refs.LOCK_REF) == sha


def _non_utf8_blob(origin: Origin) -> str:
    path = origin.path / "bad.bin"
    path.write_bytes(b"\xff\xfe{")
    return origin.git("hash-object", "-w", str(path))


def test_lock_commit_without_its_file_reads_as_held(origin: Origin) -> None:
    sha = origin.commit("other.json", "{}")
    origin.set_ref(refs.LOCK_REF, sha)
    state = refs.read_lock(REPO)
    assert state.held
    assert state.record is None
    assert refs.LOCK_FILE in state.error


def test_lock_ref_at_a_non_commit_reads_as_held(origin: Origin) -> None:
    blob = origin.git("hash-object", "-w", "--stdin", stdin="{}")
    origin.set_ref(refs.LOCK_REF, blob)
    state = refs.read_lock(REPO)
    assert state.held
    assert state.sha == blob
    assert state.record is None
    assert state.error


def test_lock_read_has_no_trusted_author_check(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_trust(*args: object, **kwargs: object) -> None:
        raise AssertionError("the lock is read with no trusted-author check")

    monkeypatch.setattr(trust, "require_trusted_commit", no_trust)
    monkeypatch.setattr(trust, "load_trusted_authors", no_trust)
    sha = origin.commit(
        refs.LOCK_FILE, to_json(_record()), author="Outsider", email="outsider@example.com"
    )
    origin.set_ref(refs.LOCK_REF, sha)
    assert origin.git("log", "-1", "--format=%an", sha) == "Outsider"
    state = refs.read_lock(REPO)
    assert state.record == _record()
    assert state.sha == sha


def test_transport_failure_raises_not_returns_none(
    origin: Origin, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del origin
    missing = str(tmp_path / "no-such-origin.git")
    monkeypatch.setattr(refs, "origin_url", lambda repo: missing)
    monkeypatch.setattr(retry, "compute_delay", lambda attempt: 0.0)
    with pytest.raises(RefWriteError, match="refs/vergil/release-lock"):
        refs.take_lock(REPO, _record())
    with pytest.raises(RefWriteError):
        refs.update_lock(REPO, _record(), expected="1" * 40)
    with pytest.raises(RefWriteError):
        refs.delete_lock(REPO, expected="1" * 40)
    with pytest.raises(RefReadError, match="refs/vergil/release-lock"):
        refs.read_lock(REPO)
    with pytest.raises(RefWriteError):
        history.append(REPO, _entry())


def test_a_refused_push_with_the_ref_unchanged_raises(origin: Origin) -> None:
    taken = refs.take_lock(REPO, _record())
    assert taken is not None
    origin.reject_pushes()
    with pytest.raises(RefWriteError, match="refused by the test hook"):
        refs.update_lock(REPO, _record(stage="cut"), expected=taken)
    with pytest.raises(RefWriteError, match="refused by the test hook"):
        refs.delete_lock(REPO, expected=taken)
    assert origin.sha(refs.LOCK_REF) == taken


def test_a_refused_create_raises(origin: Origin) -> None:
    origin.reject_pushes()
    with pytest.raises(RefWriteError, match="refused by the test hook"):
        refs.take_lock(REPO, _record())


def test_applied_then_failed_push_reconciled_as_success(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    flaky = FlakyPush(monkeypatch)
    taken = refs.take_lock(REPO, _record())
    assert flaky.pushes == 2  # the landed push, then the retry's rejection
    assert taken is not None
    assert origin.sha(refs.LOCK_REF) == taken

    flaky = FlakyPush(monkeypatch)
    updated = refs.update_lock(REPO, _record(stage="cut"), expected=taken)
    assert flaky.pushes == 2
    assert updated is not None
    assert origin.sha(refs.LOCK_REF) == updated

    flaky = FlakyPush(monkeypatch)
    assert refs.delete_lock(REPO, expected=updated) is True
    assert flaky.pushes == 2
    assert origin.sha(refs.LOCK_REF) is None


def test_rejection_with_foreign_value_is_mismatch(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = refs.take_lock(REPO, _record())
    assert taken is not None
    foreign = origin.commit(refs.LOCK_FILE, to_json(_record(token="other")))

    FlakyPush(monkeypatch, apply=False, meanwhile=lambda: origin.set_ref(refs.LOCK_REF, foreign))
    assert refs.update_lock(REPO, _record(stage="cut"), expected=taken) is None
    assert origin.sha(refs.LOCK_REF) == foreign

    FlakyPush(monkeypatch, apply=False, meanwhile=lambda: None)
    assert refs.delete_lock(REPO, expected=taken) is False
    assert origin.sha(refs.LOCK_REF) == foreign

    origin.git("update-ref", "-d", refs.LOCK_REF)
    FlakyPush(monkeypatch, apply=False, meanwhile=lambda: origin.set_ref(refs.LOCK_REF, foreign))
    assert refs.take_lock(REPO, _record()) is None
    assert origin.sha(refs.LOCK_REF) == foreign


def test_writes_keep_stdout_clean(origin: Origin, capsys: pytest.CaptureFixture[str]) -> None:
    del origin
    taken = refs.take_lock(REPO, _record())
    assert taken is not None
    refs.read_lock(REPO)
    refs.delete_lock(REPO, expected=taken)
    history.append(REPO, _entry())
    assert capsys.readouterr().out == ""


def test_lock_commit_is_one_file_with_no_parent(origin: Origin) -> None:
    taken = refs.take_lock(REPO, _record())
    assert taken is not None
    assert origin.git("ls-tree", "--name-only", taken) == refs.LOCK_FILE
    assert origin.git("rev-list", "--count", taken) == "1"


def test_a_malformed_repo_is_refused(origin: Origin) -> None:
    del origin
    with pytest.raises(ValueError, match="not an <owner>"):
        refs.read_lock("https://example.com/x")


# --- the history ref --------------------------------------------------------------


def test_history_append_creates_then_chains(origin: Origin) -> None:
    assert history.read_history(REPO) == []
    history.append(REPO, _entry("break", "human"))
    history.append(REPO, _entry("reclaim-rearm"))
    assert [e.action for e in history.read_history(REPO)] == ["break", "reclaim-rearm"]
    tip = origin.sha(refs.HISTORY_REF)
    assert tip is not None
    assert origin.git("rev-list", "--count", tip) == "2"
    assert history.read_history(REPO)[0] == _entry("break", "human")


def test_history_append_concurrent_both_kept(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    history.append(REPO, _entry("break", "human"))

    def other_writer() -> None:
        tip = origin.sha(refs.HISTORY_REF)
        text = history.entry_json(_entry("takeover", "other"))
        origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, text, parent=tip))

    flaky = FlakyPush(monkeypatch, apply=False, meanwhile=other_writer)
    history.append(REPO, _entry("reclaim", "agent"))
    assert flaky.pushes == 3  # the failure, the retry's rejection, the rebuilt append
    assert [(e.action, e.actor) for e in history.read_history(REPO)] == [
        ("break", "human"),
        ("takeover", "other"),
        ("reclaim", "agent"),
    ]


def test_history_applied_then_failed_is_not_duplicated(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    del origin
    FlakyPush(monkeypatch)
    history.append(REPO, _entry())
    assert len(history.read_history(REPO)) == 1


def test_history_applied_then_buried_by_another_append_is_success(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    def other_writer() -> None:
        tip = origin.sha(refs.HISTORY_REF)
        text = history.entry_json(_entry("takeover", "other"))
        origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, text, parent=tip))

    FlakyPush(monkeypatch, meanwhile=other_writer)
    history.append(REPO, _entry("reclaim", "agent"))
    assert [e.action for e in history.read_history(REPO)] == ["reclaim", "takeover"]


def test_history_append_gives_up_after_bounded_contention(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    history.append(REPO, _entry("break", "human"))
    real_write = refs.write_ref

    def always_beaten(where: Path, ref: str, *, new: str | None, expected: str | None) -> bool:
        tip = origin.sha(refs.HISTORY_REF)
        text = history.entry_json(_entry("takeover", "other"))
        origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, text, parent=tip))
        return real_write(where, ref, new=new, expected=expected)

    monkeypatch.setattr(refs, "write_ref", always_beaten)
    with pytest.raises(RefWriteError, match="concurrent"):
        history.append(REPO, _entry("reclaim", "agent"))


def test_history_entry_rejects_unknown_action() -> None:
    with pytest.raises(ValueError, match="action"):
        _entry("done")


def test_unparseable_history_entry_raises(origin: Origin) -> None:
    origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, "{bad"))
    with pytest.raises(RefReadError, match="history"):
        history.read_history(REPO)


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"action": "break"},
        {
            "action": "break",
            "old": "",
            "actor": "h",
            "at": "x",
            "reason": "r",
            "rearmed": "not-a-list",
        },
        {"action": "break", "old": "", "actor": 1, "at": "x", "reason": "r", "rearmed": []},
    ],
)
def test_malformed_history_entries_raise(origin: Origin, data: object) -> None:
    origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, json.dumps(data)))
    with pytest.raises(RefReadError, match="history"):
        history.read_history(REPO)


def test_history_entry_with_an_unknown_action_raises(origin: Origin) -> None:
    text = history.entry_json(_entry()).replace('"reclaim"', '"done"')
    origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, text))
    with pytest.raises(RefReadError, match="not valid: action"):
        history.read_history(REPO)


def test_history_listing_failure_raises(origin: Origin, monkeypatch: pytest.MonkeyPatch) -> None:
    del origin
    history.append(REPO, _entry())
    _fail_git(monkeypatch, "rev-list")
    with pytest.raises(RefReadError, match="rev-list broke"):
        history.read_history(REPO)


def test_history_deleted_under_an_append_is_recreated(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    history.append(REPO, _entry("break", "human"))
    FlakyPush(
        monkeypatch, apply=False, meanwhile=lambda: origin.git("update-ref", "-d", refs.HISTORY_REF)
    )
    history.append(REPO, _entry("reclaim", "agent"))
    assert [e.action for e in history.read_history(REPO)] == ["reclaim"]


def test_history_ancestry_check_failure_raises(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    history.append(REPO, _entry("break", "human"))

    def other_writer() -> None:
        tip = origin.sha(refs.HISTORY_REF)
        text = history.entry_json(_entry("takeover", "other"))
        origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, text, parent=tip))

    def broken(*args: Any, **kwargs: Any) -> bool:
        raise subprocess.CalledProcessError(128, ("git", "merge-base"), stderr="merge-base broke")

    FlakyPush(monkeypatch, apply=False, meanwhile=other_writer)
    monkeypatch.setattr(git, "is_ancestor", broken)
    with pytest.raises(RefWriteError, match="merge-base broke"):
        history.append(REPO, _entry("reclaim", "agent"))


# --- the shared transport ---------------------------------------------------------


def test_origin_url_is_the_github_clone_url() -> None:
    assert _REAL_ORIGIN_URL("org/repo") == "https://github.com/org/repo.git"


def test_scratch_failure_raises_the_callers_error(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    del origin
    _fail_git(monkeypatch, "init")
    with pytest.raises(RefReadError, match="scratch repository"):
        refs.read_lock(REPO)
    with pytest.raises(RefWriteError, match="scratch repository"):
        refs.take_lock(REPO, _record())


def test_present_lock_that_cannot_be_fetched_reads_as_held(
    origin: Origin, monkeypatch: pytest.MonkeyPatch
) -> None:
    taken = refs.take_lock(REPO, _record())
    _fail_git(monkeypatch, "fetch")
    state = refs.read_lock(REPO)
    assert state.held
    assert state.sha == taken
    assert state.record is None
    assert "fetch broke" in state.error
    assert origin.sha(refs.LOCK_REF) == taken


def test_build_failure_raises(origin: Origin, monkeypatch: pytest.MonkeyPatch) -> None:
    _fail_git(monkeypatch, "commit-tree")
    with pytest.raises(RefWriteError, match="commit-tree broke"):
        refs.take_lock(REPO, _record())
    assert origin.sha(refs.LOCK_REF) is None


@pytest.mark.parametrize("expected", ["0" * 40, "abc", "G" * 40, ""])
def test_a_malformed_expected_value_is_refused(origin: Origin, expected: str) -> None:
    del origin
    with pytest.raises(ValueError, match="not a commit sha"):
        refs.update_lock(REPO, _record(), expected=expected)
    with pytest.raises(ValueError, match="not a commit sha"):
        refs.delete_lock(REPO, expected=expected)
