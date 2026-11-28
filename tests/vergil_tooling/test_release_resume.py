from __future__ import annotations

import contextlib
import re
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_lock_stubs import REPO, Origin, make_origin
from tests.vergil_tooling.release_stubs import FakeLockOwner
from vergil_tooling.lib.release import checklist, state
from vergil_tooling.lib.release import preflight as preflight_module
from vergil_tooling.lib.release.abandon import ReleaseAbandoned
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.lock_guard import release_lock
from vergil_tooling.lib.release.orchestrator import build_stages
from vergil_tooling.lib.release.prepare import _apply_bump
from vergil_tooling.lib.release.resume import (
    _SEARCH_LIST_LIMIT,
    ALWAYS_RUN,
    _open_release_issues,
    check_release_version,
    find_resume_target,
    first_undone,
    restore_context,
    verify_facts,
)
from vergil_tooling.lib.release_lock import history, holder, refs, sweeps
from vergil_tooling.lib.release_lock.holder import LockLost
from vergil_tooling.lib.release_lock.model import (
    STALE_AFTER_SECS,
    LockRecord,
    RefReadError,
    RefWriteError,
    from_json,
    to_json,
)
from vergil_tooling.lib.release_lock.refs import LockState

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_MOD = "vergil_tooling.lib.release.resume"
_STAGES = ["audit", "prepare"]


def _state(version: str, done: tuple[str, ...] = (), **facts: object) -> state.StateBlock:
    initial = state.initial_state(version=version, cut="c" * 40, issues=(), without_deps=False)
    return replace(initial, done=done, **facts)


def _issue(version: str, number: int, done: tuple[str, ...] = ()) -> dict[str, object]:
    body = (
        "## Release\n\n"
        + checklist.render(_STAGES, done)
        + "\n\n"
        + state.render_state(_state(version, done))
    )
    return {"number": number, "title": f"release: {version}", "body": body}


def test_find_resume_target_single() -> None:
    with patch(_MOD + ".github.read_json", return_value=[_issue("2.1.0", 42)]):
        assert find_resume_target("o/r", _STAGES) == ("2.1.0", 42)


def test_find_resume_target_none_raises() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[]),
        pytest.raises(ReleaseError, match="No in-flight release"),
    ):
        find_resume_target("o/r", _STAGES)


def _limited(rows: list[dict[str, object]]):
    """A ``gh issue list`` stand-in that returns at most ``--limit`` rows (30 without one)."""

    def fake(*args: str) -> list[dict[str, object]]:
        return rows[: int(args[args.index("--limit") + 1]) if "--limit" in args else 30]

    return fake


def _noise(count: int) -> list[dict[str, object]]:
    # The search is a broad net: these match it but are not release issues.
    return [{"number": i, "title": f"release: notes {i}", "body": ""} for i in range(count)]


def test_find_resume_target_sees_a_release_issue_past_row_30() -> None:
    rows = [*_noise(30), _issue("2.1.0", 42)]
    with patch(_MOD + ".github.read_json", side_effect=_limited(rows)):
        assert find_resume_target("o/r", _STAGES) == ("2.1.0", 42)


def test_find_resume_target_refuses_a_search_that_may_be_truncated() -> None:
    rows = _noise(_SEARCH_LIST_LIMIT)
    with (
        patch(_MOD + ".github.read_json", side_effect=_limited(rows)),
        pytest.raises(ReleaseError, match="possibly truncated list"),
    ):
        find_resume_target("o/r", _STAGES)


def test_find_resume_target_multiple_raises() -> None:
    issues = [_issue("2.1.0", 42), _issue("2.2.0", 43)]
    with (
        patch(_MOD + ".github.read_json", return_value=issues),
        pytest.raises(ReleaseError, match="Multiple in-flight"),
    ):
        find_resume_target("o/r", _STAGES)


@pytest.mark.parametrize(
    ("older", "newer"),
    [("2.0.9", "2.0.10"), ("2.9.0", "2.10.0")],
)
def test_find_resume_target_multiple_orders_by_semantic_version(older: str, newer: str) -> None:
    # As strings, "2.0.10" < "2.0.9" and "2.10.0" < "2.9.0" (#4877).
    issues = [_issue(newer, 43), _issue(older, 42)]
    with (
        patch(_MOD + ".github.read_json", return_value=issues),
        pytest.raises(ReleaseError, match=re.escape(f"({older}, {newer})")),
    ):
        find_resume_target("o/r", _STAGES)


def test_find_resume_target_version_disambiguates() -> None:
    issues = [_issue("2.1.0", 42), _issue("2.2.0", 43)]
    with patch(_MOD + ".github.read_json", return_value=issues):
        assert find_resume_target("o/r", _STAGES, version="2.2.0") == ("2.2.0", 43)


def test_find_resume_target_version_not_found_raises() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[_issue("2.1.0", 42)]),
        pytest.raises(ReleaseError, match="No open release issue for 2.9.9"),
    ):
        find_resume_target("o/r", _STAGES, version="2.9.9")


def test_find_resume_target_refuses_a_stage_this_tooling_lacks() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[_issue("2.1.0", 42, ("prepare",))]),
        pytest.raises(ReleaseError, match="another tooling version"),
    ):
        find_resume_target("o/r", ["audit", "merge"])


def test_find_resume_target_refuses_an_issue_with_no_state_block() -> None:
    # An issue the old tooling opened has a checklist but no state block: the
    # resume refuses, naming the block, never falling back to the checklist.
    body = "## Release\n\n" + checklist.render(_STAGES)
    issue = {"number": 42, "title": "release: 2.1.0", "body": body}
    with (
        patch(_MOD + ".github.read_json", return_value=[issue]),
        pytest.raises(ReleaseError, match="release state block: no release state block"),
    ):
        find_resume_target("o/r", _STAGES)


def test_find_resume_target_refuses_a_malformed_state_block() -> None:
    body = f"## Release\n\n{state.BEGIN}\n```json\n{{not json\n```\n{state.END}\n"
    issue = {"number": 42, "title": "release: 2.1.0", "body": body}
    with (
        patch(_MOD + ".github.read_json", return_value=[issue]),
        pytest.raises(ReleaseError, match="not valid JSON"),
    ):
        find_resume_target("o/r", _STAGES)


def test_find_resume_target_refuses_a_block_for_another_version() -> None:
    issue = _issue("2.1.0", 42)
    issue["title"] = "release: 2.1.1"
    with (
        patch(_MOD + ".github.read_json", return_value=[issue]),
        pytest.raises(ReleaseError, match="names version '2.1.0'"),
    ):
        find_resume_target("o/r", _STAGES)


def test_first_undone_all_done_is_none() -> None:
    stages = ["audit", "preflight", "prepare", "merge-release", "publish-status"]
    assert first_undone(_state("2.1.0", ("prepare", "merge-release")), stages) is None


def test_first_undone_ignores_the_always_run_stages() -> None:
    stages = ["audit", "preflight", "prepare", "merge-release", "publish-status"]
    assert set(ALWAYS_RUN) <= set(stages)
    assert first_undone(_state("2.1.0"), stages) == "prepare"
    assert first_undone(_state("2.1.0", ("prepare",)), stages) == "merge-release"


def test_open_release_issues_skips_non_release_titles() -> None:
    results = [
        {"number": 1, "title": "not a release", "body": ""},
        _issue("2.1.0", 42),
    ]
    with patch(_MOD + ".github.read_json", return_value=results):
        assert [(v, n) for v, n, _ in _open_release_issues("o/r")] == [("2.1.0", 42)]


def test_open_release_issues_non_dict_item_raises() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=["not a dict", _issue("2.1.0", 42)]),
        pytest.raises(ReleaseError, match="non-object item") as info,
    ):
        _open_release_issues("o/r")
    assert info.value.phase == "resume"


def test_open_release_issues_non_list_raises() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value={"error": "x"}),
        pytest.raises(ReleaseError, match="not a list") as info,
    ):
        _open_release_issues("o/r", phase="preflight")
    assert info.value.phase == "preflight"


# --- verify_facts and restore_context ---------------------------------------

_RELEASE_PR = "https://github.com/o/r/pull/100"
_BUMP_PR = "https://github.com/o/r/pull/101"
_RELEASE_MERGE = "a" * 40
_BUMP_MERGE = "b" * 40
_FREEZE = "f" * 40
_DONE = ("prepare", "merge-release", "confirm-main", "back-merge-bump", "teardown-worktree")


def _ctx() -> ReleaseContext:
    return ReleaseContext(
        repo="o/r",
        version="2.1.0",
        repo_root=Path("/tmp/r"),  # noqa: S108
        version_override=None,
    )


def _recorded(
    done: tuple[str, ...] = (*_DONE, "confirm-develop"), **overrides: object
) -> state.StateBlock:
    facts: dict[str, object] = {
        "release_pr": _RELEASE_PR,
        "bump_pr": _BUMP_PR,
        "tag": "v2.1.0",
        "tag_commit": _RELEASE_MERGE,
        "main_cd_run": "11",
        "develop_cd_run": "22",
    }
    facts.update(overrides)
    return _state("2.1.0", done, **facts)


def _github(
    *, run_heads: dict[str, str] | None = None
) -> tuple[Callable[[str], str], Callable[[str], str], Callable[..., str]]:
    merges = {_RELEASE_PR: _RELEASE_MERGE, _BUMP_PR: _BUMP_MERGE}
    heads = run_heads or {"11": _RELEASE_MERGE, "22": _BUMP_MERGE}

    def read_output(*args: str) -> str:
        assert args[:2] == ("run", "view")
        return heads[args[2]]

    return (lambda _pr: "MERGED"), merges.__getitem__, read_output


def _git(peeled: str, parents: dict[str, list[str]] | None = None) -> Callable[..., str]:
    parents = parents or {}

    def read_output(*args: str) -> str:
        if args[0] == "rev-parse":
            return peeled
        assert args[:4] == ("rev-list", "--parents", "-n", "1")
        return " ".join([args[4], *parents[args[4]]])

    return read_output


def _verify(
    current: state.StateBlock,
    *,
    peeled: str = _RELEASE_MERGE,
    parents: dict[str, list[str]] | None = None,
    run_heads: dict[str, str] | None = None,
    tag_exists: bool = True,
) -> ReleaseContext:
    pr_state, merge_sha, gh_read = _github(run_heads=run_heads)
    ctx = _ctx()
    with (
        patch(_MOD + ".github.pr_state", side_effect=pr_state),
        patch(_MOD + ".github.merge_commit_sha", side_effect=merge_sha),
        patch(_MOD + ".github.read_output", side_effect=gh_read),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", return_value=tag_exists),
        patch(_MOD + ".git.read_output", side_effect=_git(peeled, parents)),
    ):
        verify_facts(ctx, current)
    return ctx


def test_verify_facts_passes_and_sets_the_merge_commits() -> None:
    ctx = _verify(_recorded())
    assert ctx.release_merge_sha == _RELEASE_MERGE
    assert ctx.bump_merge_sha == _BUMP_MERGE


def test_verify_facts_tag_moved_raises() -> None:
    with pytest.raises(ReleaseError, match="tag does not hold: v2.1.0 peels to"):
        _verify(_recorded(), peeled="9" * 40)


def test_verify_facts_freeze_commit_tag_passes() -> None:
    # vergil-actions' CD tags a single-parent freeze commit on the release merge.
    current = _recorded(tag_commit=_FREEZE)
    _verify(current, peeled=_FREEZE, parents={_FREEZE: [_RELEASE_MERGE]})


def test_verify_facts_tag_on_an_unrelated_commit_raises() -> None:
    current = _recorded(tag_commit=_FREEZE)
    with pytest.raises(ReleaseError, match="nor a single-parent commit on top of it"):
        _verify(current, peeled=_FREEZE, parents={_FREEZE: ["9" * 40]})


def test_verify_facts_develop_run_matches_back_merge() -> None:
    _verify(_recorded())
    with pytest.raises(ReleaseError, match="develop_cd_run does not hold: run 22 is for"):
        _verify(_recorded(), run_heads={"11": _RELEASE_MERGE, "22": "9" * 40})


def test_verify_facts_main_run_on_another_commit_raises() -> None:
    with pytest.raises(ReleaseError, match="main_cd_run does not hold"):
        _verify(_recorded(), run_heads={"11": "9" * 40, "22": _BUMP_MERGE})


def test_verify_facts_unmerged_release_pr_raises() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        pytest.raises(ReleaseError, match="release_pr does not hold: .* is OPEN, not MERGED"),
    ):
        verify_facts(ctx, _recorded())


def test_verify_facts_before_merge_reads_nothing() -> None:
    # Only prepare is done: the release PR is open by design, and nothing is checked.
    current = _state("2.1.0", ("prepare",), release_pr=_RELEASE_PR)
    with patch(_MOD + ".github.pr_state") as m_state:
        verify_facts(_ctx(), current)
    m_state.assert_not_called()


def test_verify_facts_merge_done_without_a_pr_raises() -> None:
    with pytest.raises(ReleaseError, match="no PR is recorded"):
        verify_facts(_ctx(), _state("2.1.0", ("prepare", "merge-release")))


def test_restore_context_rebuilds_the_facts() -> None:
    ctx = _ctx()
    restore_context(ctx, _recorded())
    assert ctx.release_pr_url == _RELEASE_PR
    assert ctx.bump_pr_url == _BUMP_PR
    assert ctx.tag == "v2.1.0"
    assert ctx.tag_commit == _RELEASE_MERGE
    assert ctx.release_url == "https://github.com/o/r/releases/tag/v2.1.0"
    assert ctx.cd_run_id == "11"
    assert ctx.cd_run_url == "https://github.com/o/r/actions/runs/11"
    assert ctx.develop_cd_run_id == "22"
    assert ctx.develop_cd_run_url == "https://github.com/o/r/actions/runs/22"


def test_verify_facts_no_merge_commit_raises() -> None:
    with (
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".github.merge_commit_sha", return_value=""),
        pytest.raises(ReleaseError, match="GitHub reports no merge commit"),
    ):
        verify_facts(_ctx(), _recorded())


def test_verify_facts_run_recorded_before_its_merge_raises() -> None:
    current = _state("2.1.0", ("prepare",), release_pr=_RELEASE_PR, main_cd_run="11")
    with pytest.raises(ReleaseError, match="run 11 is recorded, but the release PR is not merged"):
        verify_facts(_ctx(), current)


def test_verify_facts_tag_without_its_commit_raises() -> None:
    with pytest.raises(ReleaseError, match="recorded with no tag_commit"):
        _verify(_recorded(tag_commit=None))


def test_verify_facts_tag_before_the_release_merge_raises() -> None:
    current = _state("2.1.0", ("prepare",), tag="v2.1.0", tag_commit=_RELEASE_MERGE)
    with pytest.raises(ReleaseError, match="recorded, but the release PR is not merged"):
        verify_facts(_ctx(), current)


def test_verify_facts_missing_tag_raises() -> None:
    with pytest.raises(ReleaseError, match="v2.1.0 does not exist on origin"):
        _verify(_recorded(), tag_exists=False)


# --- the release branch's VERSION against the recorded target (#4899) ---


def _run_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def release_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A release worktree on ``release/2.1.0`` with VERSION 2.0.3 committed: no bump yet.

    The run is in it, as preflight leaves a resume before ``merge-release``.
    """
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        '\n[dependencies]\nvergil = "v2.0"\n\n[ci]\nversions = ["3.14"]\n'
    )
    (worktree / "VERSION").write_text("2.0.3\n")
    _run_git(worktree, "init", "-q", "-b", "release/2.1.0")
    _run_git(worktree, "add", "-A")
    _run_git(worktree, "commit", "-q", "-m", "base")
    monkeypatch.chdir(worktree)
    return worktree


def _branch_ctx(worktree: Path) -> ReleaseContext:
    ctx = _ctx()
    ctx.release_branch = "release/2.1.0"
    ctx.worktree_path = worktree
    return ctx


def _minor(done: tuple[str, ...] = ()) -> state.StateBlock:
    return _state("2.1.0", done, version_bump="minor")


def _commit_version(worktree: Path, value: str) -> None:
    (worktree / "VERSION").write_text(f"{value}\n")
    _run_git(worktree, "commit", "-q", "-am", f"VERSION {value}")


def test_resume_completes_a_minor_bump_prepare_stopped_before(release_branch: Path) -> None:
    # prepare stopped after the tracking issue, before the bump: the resume
    # completes it, and the branch then holds the recorded target.
    ctx = _branch_ctx(release_branch)
    check_release_version(ctx, _minor())
    assert ctx.version_override == "minor"
    _apply_bump(ctx, "minor")
    assert _run_git(release_branch, "show", "HEAD:VERSION") == "2.1.0"
    assert _run_git(release_branch, "log", "-1", "--format=%s") == (
        "chore(release): bump version to 2.1.0"
    )
    assert _run_git(release_branch, "status", "--porcelain") == ""
    rerun = _branch_ctx(release_branch)
    check_release_version(rerun, _minor())
    assert rerun.version_override is None


def test_resume_with_the_bump_committed_changes_nothing(release_branch: Path) -> None:
    _commit_version(release_branch, "2.1.0")
    ctx = _branch_ctx(release_branch)
    check_release_version(ctx, _minor())
    assert ctx.version_override is None


def test_resume_with_no_bump_recorded_releases_version_as_it_is(release_branch: Path) -> None:
    _commit_version(release_branch, "2.1.0")
    ctx = _branch_ctx(release_branch)
    check_release_version(ctx, _state("2.1.0"))
    assert ctx.version_override is None


@pytest.mark.parametrize(
    ("committed", "current", "recorded"),
    [
        ("1.9.0", _minor(), "a minor bump"),
        ("2.2.0", _minor(), "a minor bump"),
        ("2.0.3", _state("2.1.0"), "no bump"),
        ("2.0.3", _minor(("prepare",)), "a minor bump"),
    ],
    ids=["unrelated", "past-the-target", "no-bump-recorded", "prepare-done"],
)
def test_resume_refuses_a_version_mismatch_naming_both(
    release_branch: Path, committed: str, current: state.StateBlock, recorded: str
) -> None:
    if committed != "2.0.3":
        _commit_version(release_branch, committed)
    ctx = _branch_ctx(release_branch)
    expected = (
        rf"'release/2\.1\.0' has VERSION {re.escape(committed)}, but the release state "
        rf"records the target version 2\.1\.0 \({recorded}\)"
    )
    with pytest.raises(ReleaseError, match=expected):
        check_release_version(ctx, current)
    assert ctx.version_override is None


def test_resume_refuses_an_uncommitted_version_change(release_branch: Path) -> None:
    # A stopped run that wrote the bump but never committed it: completing
    # it again would overshoot, and adopting it would push a branch without it.
    (release_branch / "VERSION").write_text("2.1.0\n")
    ctx = _branch_ctx(release_branch)
    with pytest.raises(
        ReleaseError, match=r"VERSION 2\.1\.0 uncommitted over the committed 2\.0\.3"
    ):
        check_release_version(ctx, _minor())
    assert ctx.version_override is None


def test_resume_refuses_an_unreadable_version(release_branch: Path) -> None:
    (release_branch / "VERSION").unlink()
    _run_git(release_branch, "commit", "-q", "-am", "drop VERSION")
    with pytest.raises(ReleaseError, match="Cannot read the VERSION of 'release/2.1.0'"):
        check_release_version(_branch_ctx(release_branch), _minor())


def test_resume_refuses_a_malformed_version_file(release_branch: Path) -> None:
    (release_branch / ".claude-plugin").mkdir()
    (release_branch / ".claude-plugin" / "plugin.json").write_text('{"name": ')
    with pytest.raises(
        ReleaseError,
        match=r"Cannot read the VERSION of 'release/2\.1\.0'.*plugin\.json is not valid JSON",
    ):
        check_release_version(_branch_ctx(release_branch), _minor())


# --- the release lock on a resume (spec §5.3 and §5.6 of epic #1664, Task 25) ---------

_PREFLIGHT = "vergil_tooling.lib.release.preflight"
_GUARD = "vergil_tooling.lib.release.lock_guard"
_LOCK_REPO = "owner/repo"
_OURS = f"{_LOCK_REPO}#42"
_LOCK_SHA = "a" * 40
_PIPELINE = [stage.name for stage in build_stages()]
_RESUME_WT = Path("/tmp/repo/.worktrees/release-2.3.0")  # noqa: S108


def _ago(seconds: int) -> str:
    """The UTC timestamp *seconds* before now, as the lock records it."""
    return (datetime.now(UTC) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(**fields: str) -> LockRecord:
    """This release's lock record (2.3.0, #42, maintainer on desk), with *fields* changed."""
    base = LockRecord(
        holder_identity="maintainer",
        holder_host="desk",
        version="2.3.0",
        worktree="/elsewhere/.worktrees/release-2.3.0",
        tracking_issue=_OURS,
        stage="merge-release",
        health="running",
        broken_stage="",
        started_at="2026-11-27T09:00:00Z",
        heartbeat_at=_ago(STALE_AFTER_SECS + 60),
        token="o" * 32,
    )
    return replace(base, **cast("dict[str, Any]", fields))


def _held(**fields: str) -> LockState:
    """A readable lock holding :func:`_record` with *fields* changed."""
    rec = _record(**fields)
    return LockState(sha=_LOCK_SHA, record=rec, error="", text=to_json(rec))


_FREE = LockState(sha=None, record=None, error="")


def _resume_ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo=_LOCK_REPO,
        version="2.3.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
        release_branch="release/2.3.0",
        issue_number=42,
        issue_url=f"https://github.com/{_LOCK_REPO}/issues/42",
    )
    ctx.state = state.initial_state(version="2.3.0", cut="c" * 40, issues=(), without_deps=False)
    ctx.worktree_path = _RESUME_WT
    return ctx


class _ResumeLock:
    """A resume's lock step, its every release-lock call logged to :attr:`events`.

    The lock reads as :attr:`found`; adopt returns :attr:`owner`, take
    :attr:`take_result`; the stray-merge check finds :attr:`strays`.
    """

    def __init__(self, stack: contextlib.ExitStack) -> None:
        self.events: list[tuple[object, ...]] = []
        self.owner = FakeLockOwner(self.events)
        self.found = _held()
        self.take_result: FakeLockOwner | None = self.owner
        self.taken: list[LockRecord] = []
        self.appended: list[history.HistoryEntry] = []
        self.strays: list[str] = []
        log = self.events.append

        def adopt(repo: str, found: LockState) -> FakeLockOwner:
            log(("adopt", found.sha))
            return self.owner

        def take(repo: str, rec: LockRecord) -> FakeLockOwner | None:
            log(("take",))
            self.taken.append(rec)
            return self.take_result

        def append(repo: str, entry: history.HistoryEntry) -> None:
            log(("history", entry.action))
            self.appended.append(entry)

        def quiesce(repo: str, **_kwargs: object) -> sweeps.QuiesceResult:
            log(("quiesce",))
            return sweeps.QuiesceResult(expired=(), disarmed=(), late=(), arming_at_check=())

        def strays(ctx: ReleaseContext) -> list[str]:
            log(("strays",))
            return self.strays

        def abandon(ctx: ReleaseContext, *, reason: str) -> None:
            log(("abandon", reason))

        enter = stack.enter_context
        enter(patch(_PREFLIGHT + "._holder_identity", return_value="maintainer"))
        enter(patch(_PREFLIGHT + ".socket.gethostname", return_value="laptop"))
        enter(patch(_PREFLIGHT + ".lock_refs.read_lock", side_effect=lambda repo: self.found))
        enter(patch(_PREFLIGHT + ".LockOwner.adopt", side_effect=adopt))
        enter(patch(_PREFLIGHT + ".LockOwner.take", side_effect=take))
        enter(patch(_PREFLIGHT + ".history.append", side_effect=append))
        enter(patch(_PREFLIGHT + ".sweeps.quiesce", side_effect=quiesce))
        enter(patch(_PREFLIGHT + ".stray_merges", side_effect=strays))
        enter(patch(_PREFLIGHT + ".abandon_release", side_effect=abandon))
        self.comments: list[tuple[str, int, str]] = []

        def comment(repo: str, number: int, body: str, *, phase: str = "") -> None:
            log(("comment", number))
            self.comments.append((repo, number, body))

        enter(patch(_PREFLIGHT + ".comment_lock_takeover", side_effect=comment))

    def names(self) -> list[object]:
        return [event[0] for event in self.events]

    def run(self, first: str | None, ctx: ReleaseContext | None = None) -> ReleaseContext:
        ctx = ctx or _resume_ctx()
        preflight_module._resume_lock(ctx, first, _PIPELINE)
        return ctx


@pytest.fixture
def resume_lock() -> Iterator[_ResumeLock]:
    with contextlib.ExitStack() as stack:
        yield _ResumeLock(stack)


def test_resume_adopts_own_lock_on_other_host(resume_lock: _ResumeLock) -> None:
    # This release's lock, written on another machine, broken there and gone quiet.
    resume_lock.found = _held(health="broken", broken_stage="confirm-main")
    ctx = resume_lock.run("confirm-main")
    assert ctx.lock is resume_lock.owner
    assert resume_lock.events[0] == ("adopt", _LOCK_SHA)
    # Health back to running, on this run's worktree: never a take or a quiesce.
    assert resume_lock.events[1] == (
        "update",
        {
            "stage": "preflight",
            "health": "running",
            "broken_stage": "",
            "worktree": str(_RESUME_WT),
        },
    )
    assert not {"take", "quiesce", "history", "strays"} & set(resume_lock.names())


def test_resume_adopt_states_on_the_tracking_issue_that_it_adopted_the_lock(
    resume_lock: _ResumeLock,
) -> None:
    resume_lock.found = _held(health="broken", broken_stage="confirm-main")
    resume_lock.run("confirm-main")
    assert resume_lock.names() == ["adopt", "update", "comment"]
    repo, number, body = resume_lock.comments[0]
    assert (repo, number) == (_LOCK_REPO, 42)
    assert "<!-- vrg-release:lock-resumed -->" in body
    assert "adopted" in body
    assert "maintainer on laptop" in body
    assert "release 2.1.0" in body
    assert f"`{resume_lock.owner.sha}`" in body
    assert "broken at confirm-main" in body


def test_resume_retake_states_on_the_tracking_issue_that_it_re_took_the_lock(
    resume_lock: _ResumeLock,
) -> None:
    resume_lock.found = _FREE
    resume_lock.run("confirm-main")
    assert resume_lock.names() == ["take", "comment", "history", "quiesce"]
    _, number, body = resume_lock.comments[0]
    assert number == 42
    assert "re-took" in body
    assert "none was held" in body
    assert f"`{resume_lock.owner.sha}`" in body


def test_resume_adopts_own_stale_running_lock(resume_lock: _ResumeLock) -> None:
    # A laptop that slept mid-release: still running, but its heartbeat is stale.
    ctx = resume_lock.run("merge-release")
    assert ctx.lock is resume_lock.owner
    assert resume_lock.names() == ["adopt", "update", "comment"]


def test_resume_adopt_in_the_root_checkout_keeps_the_worktree_recorded(
    resume_lock: _ResumeLock,
) -> None:
    ctx = _resume_ctx()
    ctx.worktree_path = None
    resume_lock.run("confirm-develop", ctx)
    assert resume_lock.events[1] == (
        "update",
        {"stage": "preflight", "health": "running", "broken_stage": ""},
    )


def test_resume_refuses_own_live_lock(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _held(heartbeat_at=_ago(30))
    ctx = _resume_ctx()
    with pytest.raises(ReleaseError) as info:
        resume_lock.run("confirm-main", ctx)
    message = str(info.value)
    assert "desk" in message
    assert re.search(r"heartbeat is 3\ds old", message)
    assert "vrg-release-lock break" in message
    # Nothing written: no adopt, no take, no update.
    assert resume_lock.events == []
    assert ctx.lock is None


def test_resume_refuses_lock_of_other_release(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _held(
        holder_identity="other-maintainer", version="2.4.0", tracking_issue=f"{_LOCK_REPO}#77"
    )
    with pytest.raises(ReleaseError) as info:
        resume_lock.run("confirm-main")
    message = str(info.value)
    assert "release 2.4.0" in message
    assert "other-maintainer" in message
    assert f"{_LOCK_REPO}#77" in message
    assert resume_lock.events == []


def test_resume_refuses_own_issue_under_another_identity(resume_lock: _ResumeLock) -> None:
    # The holder is the identity and the tracking issue together.
    resume_lock.found = _held(holder_identity="other-maintainer")
    with pytest.raises(ReleaseError, match="other-maintainer"):
        resume_lock.run("confirm-main")
    assert resume_lock.events == []


def test_resume_refuses_an_unreadable_lock(resume_lock: _ResumeLock) -> None:
    resume_lock.found = LockState(sha=_LOCK_SHA, record=None, error="not valid JSON", text="{")
    with pytest.raises(ReleaseError, match="not valid JSON") as info:
        resume_lock.run("confirm-main")
    assert "vrg-release-lock break" in str(info.value)
    assert resume_lock.events == []


def test_resume_refuses_when_the_lock_cannot_be_read(resume_lock: _ResumeLock) -> None:
    with (
        patch(_PREFLIGHT + ".lock_refs.read_lock", side_effect=RefReadError("ls-remote failed")),
        pytest.raises(ReleaseError, match="ls-remote failed"),
    ):
        resume_lock.run("confirm-main")
    assert resume_lock.events == []


def test_resume_retakes_absent_lock_quiesces_and_records(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _FREE
    ctx = resume_lock.run("confirm-main")
    assert ctx.lock is resume_lock.owner
    # Take, record the re-take, then quiesce; at confirm-main no stray check.
    assert resume_lock.names() == ["take", "comment", "history", "quiesce"]
    rec = resume_lock.taken[0]
    assert (rec.holder_identity, rec.holder_host, rec.version) == ("maintainer", "laptop", "2.3.0")
    assert (rec.tracking_issue, rec.stage, rec.health, rec.worktree) == (
        _OURS,
        "preflight",
        "running",
        str(_RESUME_WT),
    )
    entry = resume_lock.appended[0]
    assert entry.action == "retake"
    assert entry.actor == "maintainer on laptop (vrg-release --resume)"
    assert "confirm-main" in entry.reason
    assert entry.old == ""


def test_resume_retake_refused_when_another_release_wins(resume_lock: _ResumeLock) -> None:
    resume_lock.take_result = None
    with (
        patch(
            _PREFLIGHT + ".lock_refs.read_lock",
            side_effect=[_FREE, _held(holder_identity="other", tracking_issue="o/r#9")],
        ),
        pytest.raises(ReleaseError, match="held by other"),
    ):
        resume_lock.run("confirm-main")
    assert resume_lock.names() == ["take"]


def test_resume_before_merge_release_stray_check_after_disarm_abandons(
    resume_lock: _ResumeLock,
) -> None:
    resume_lock.found = _FREE
    stray = f"https://github.com/{_LOCK_REPO}/pull/77"
    resume_lock.strays = [stray]
    with pytest.raises(ReleaseAbandoned, match="fresh `vrg-release`"):
        resume_lock.run("prepare")
    # The stray check runs only once develop is disarmed, and abandons on a hit.
    assert resume_lock.names() == ["take", "comment", "history", "quiesce", "strays", "abandon"]
    assert stray in str(resume_lock.events[-1][1])


def test_resume_before_merge_release_without_strays_goes_on(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _FREE
    ctx = resume_lock.run("prepare")
    assert resume_lock.names() == ["take", "comment", "history", "quiesce", "strays"]
    assert ctx.lock is resume_lock.owner


def test_resume_adopt_runs_no_stray_check(resume_lock: _ResumeLock) -> None:
    # The lock was held all along, so develop could not move.
    resume_lock.run("prepare")
    assert "strays" not in resume_lock.names()


def test_resume_lock_failure_after_retake_marks_it_broken(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _FREE
    with (
        patch(_PREFLIGHT + ".sweeps.quiesce", side_effect=sweeps.SweepError("disarm failed")),
        pytest.raises(ReleaseError, match="disarm failed"),
    ):
        resume_lock.run("confirm-main")
    # The lock is the resume's now: marked broken, so the next resume adopts it at once.
    assert resume_lock.events[-1] == ("update", {"health": "broken", "broken_stage": "preflight"})


def test_resume_lock_failure_marking_broken_names_both(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _FREE
    resume_lock.owner.update_error = RefWriteError("push rejected")
    with (
        patch(_PREFLIGHT + ".sweeps.quiesce", side_effect=sweeps.SweepError("disarm failed")),
        pytest.raises(ReleaseError) as info,
    ):
        resume_lock.run("confirm-main")
    assert "disarm failed" in str(info.value)
    assert "push rejected" in str(info.value)


@pytest.mark.parametrize("first", ["unlock", "rearm", "close-tracking", None])
def test_resume_at_unlock_or_later_takes_no_lock(
    resume_lock: _ResumeLock, first: str | None
) -> None:
    ctx = resume_lock.run(first)
    assert resume_lock.events == []
    assert ctx.lock is None


def test_resume_at_rearm_takes_no_lock(resume_lock: _ResumeLock) -> None:
    with patch(_PREFLIGHT + ".lock_refs.read_lock") as read:
        ctx = resume_lock.run("rearm")
    read.assert_not_called()
    assert ctx.lock is None


# --- the unlock stage on a resume -------------------------------------------------


def _unlock(found: LockState, *, deleted: bool = True) -> tuple[ReleaseContext, MagicMock]:
    ctx = _resume_ctx()
    with (
        patch(_GUARD + ".lock_refs.read_lock", return_value=found),
        patch(_GUARD + ".lock_refs.delete_lock", return_value=deleted) as delete,
        patch(_GUARD + ".holder_identity", return_value="maintainer"),
    ):
        release_lock(ctx, phase="unlock", remedy="Re-run it.")
    return ctx, delete


def test_resume_at_unlock_deletes_own_remaining_lock(capsys: pytest.CaptureFixture[str]) -> None:
    # Its own lock, live or not: the stage deletes it against the sha just read.
    ctx, delete = _unlock(_held(heartbeat_at=_ago(5)))
    delete.assert_called_once_with(_LOCK_REPO, expected=_LOCK_SHA)
    assert ctx.lock is None
    assert "Deleted" in capsys.readouterr().out


def test_resume_at_unlock_absent_lock_done() -> None:
    _ctx, delete = _unlock(_FREE)
    delete.assert_not_called()


def test_resume_at_unlock_refuses_other_holder() -> None:
    other = _held(holder_identity="other-maintainer", version="2.4.0", tracking_issue="o/r#77")
    with pytest.raises(ReleaseError) as info:
        _unlock(other)
    message = str(info.value)
    assert "release 2.4.0" in message
    assert "other-maintainer" in message
    assert "vrg-release-lock break" in message
    # Unchanged by issue #5352: a readable lock of another release is named as such.
    assert "It is not release 2.3.0's own" in message
    assert "a resume deletes only its own release's lock" in message


def test_resume_at_unlock_refuses_a_lock_changed_meanwhile() -> None:
    with pytest.raises(ReleaseError, match="changed") as info:
        _unlock(_held(), deleted=False)
    assert "vrg-release-lock status" in str(info.value)


def test_resume_at_unlock_delete_failure_refuses() -> None:
    ctx = _resume_ctx()
    with (
        patch(_GUARD + ".lock_refs.read_lock", return_value=_held()),
        patch(_GUARD + ".lock_refs.delete_lock", side_effect=RefWriteError("push failed")),
        patch(_GUARD + ".holder_identity", return_value="maintainer"),
        pytest.raises(ReleaseError, match="push failed"),
    ):
        release_lock(ctx, phase="unlock", remedy="Re-run it.")


# --- the lock owner a resume makes, against a local origin ------------------------


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    monkeypatch.setattr(holder, "HEARTBEAT_INTERVAL_SECS", 0.05)
    return make_origin(tmp_path, monkeypatch)


def _real_resume(first: str) -> ReleaseContext:
    ctx = _resume_ctx()
    ctx.repo = REPO
    quiet = sweeps.QuiesceResult(expired=(), disarmed=(), late=(), arming_at_check=())
    with (
        patch(_PREFLIGHT + "._holder_identity", return_value="maintainer"),
        patch(_PREFLIGHT + ".socket.gethostname", return_value="laptop"),
        patch(_PREFLIGHT + ".sweeps.quiesce", return_value=quiet),
        patch(_PREFLIGHT + ".stray_merges", return_value=[]),
        patch(_PREFLIGHT + ".comment_lock_takeover"),
    ):
        preflight_module._resume_lock(ctx, first, _PIPELINE)
    return ctx


def _beats(ctx: ReleaseContext, origin: Origin) -> None:
    """Wait until the lock on *origin* moves past the owner's write: the heartbeat ran."""
    owner = ctx.lock
    assert owner is not None
    assert owner.heartbeat_running
    first = origin.sha(refs.LOCK_REF)
    deadline = time.monotonic() + 10
    try:
        while origin.sha(refs.LOCK_REF) == first:
            assert time.monotonic() < deadline, "the heartbeat never wrote the lock"
            time.sleep(0.02)
    finally:
        owner.stop_heartbeat()


def test_resume_adopt_starts_heartbeat(origin: Origin) -> None:
    rec = _record(tracking_issue=f"{REPO}#42", health="broken", broken_stage="install")
    origin.set_ref(refs.LOCK_REF, origin.commit(refs.LOCK_FILE, to_json(rec)))
    ctx = _real_resume("install")
    on_origin = from_json(origin.file(refs.LOCK_REF, refs.LOCK_FILE))
    assert on_origin.token != rec.token
    assert (on_origin.health, on_origin.broken_stage) == ("running", "")
    _beats(ctx, origin)


def test_resume_retake_starts_heartbeat(origin: Origin) -> None:
    ctx = _real_resume("confirm-main")
    assert [entry.action for entry in history.read_history(REPO)] == ["retake"]
    _beats(ctx, origin)


def test_lost_holder_stops_with_clear_error(origin: Origin) -> None:
    # A resumed run whose lock is broken under it stops at its next write,
    # naming the break, and never re-takes the lock.
    ctx = _real_resume("confirm-main")
    owner = ctx.lock
    assert owner is not None
    owner.stop_heartbeat()
    old = origin.file(refs.LOCK_REF, refs.LOCK_FILE)
    history.append(
        REPO,
        history.HistoryEntry(
            action="break", old=old, actor="human", at=_ago(0), reason="stuck", rearmed=()
        ),
    )
    origin.git("update-ref", "-d", refs.LOCK_REF)
    with pytest.raises(LockLost, match="broken by human") as info:
        owner.update(stage="confirm-main")
    assert "does not re-take the lock" in str(info.value)
    assert origin.sha(refs.LOCK_REF) is None


# --- the lock step's failures -----------------------------------------------------


def test_resume_adopt_of_a_lock_changed_meanwhile_refuses(resume_lock: _ResumeLock) -> None:
    lost = LockLost("it moved", phase="preflight", cause=None, outside_change=True)
    ctx = _resume_ctx()
    with (
        patch(_PREFLIGHT + ".LockOwner.adopt", side_effect=lost),
        pytest.raises(ReleaseError, match="changed while this resume adopted it") as info,
    ):
        resume_lock.run("confirm-main", ctx)
    assert "vrg-release-lock status" in str(info.value)
    assert ctx.lock is None


def test_resume_adopt_write_failure_refuses(resume_lock: _ResumeLock) -> None:
    ctx = _resume_ctx()
    with (
        patch(_PREFLIGHT + ".LockOwner.adopt", side_effect=RefWriteError("push failed")),
        pytest.raises(ReleaseError, match="push failed"),
    ):
        resume_lock.run("confirm-main", ctx)
    assert ctx.lock is None


def test_resume_adopt_update_lost_is_not_marked_broken(resume_lock: _ResumeLock) -> None:
    # A lock lost under the adopting write is no longer this run's to mark.
    resume_lock.owner.update_error = LockLost(
        "broken meanwhile", phase="preflight", cause=None, outside_change=False
    )
    with pytest.raises(LockLost, match="broken meanwhile"):
        resume_lock.run("confirm-main")
    assert resume_lock.names() == ["adopt", "update"]


def test_resume_retake_write_failure_refuses(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _FREE
    ctx = _resume_ctx()
    with (
        patch(_PREFLIGHT + ".LockOwner.take", side_effect=RefWriteError("push failed")),
        pytest.raises(ReleaseError, match="push failed"),
    ):
        resume_lock.run("confirm-main", ctx)
    assert ctx.lock is None


def test_resume_retake_history_failure_marks_the_lock_broken(resume_lock: _ResumeLock) -> None:
    resume_lock.found = _FREE
    with (
        patch(_PREFLIGHT + ".history.append", side_effect=RefWriteError("history push failed")),
        pytest.raises(ReleaseError, match="history push failed"),
    ):
        resume_lock.run("confirm-main")
    assert "quiesce" not in resume_lock.names()
    assert resume_lock.events[-1] == ("update", {"health": "broken", "broken_stage": "preflight"})


def test_resume_abandon_failure_after_the_lock_went_marks_nothing(
    resume_lock: _ResumeLock,
) -> None:
    # The abandon step released the lock, then its re-arm sweep failed: the
    # lock is gone, so there is nothing to mark broken.
    resume_lock.found = _FREE
    resume_lock.strays = ["https://github.com/owner/repo/pull/77"]
    failure = ReleaseError(phase="merge-release", command="re-arm", message="re-arm failed")

    def abandon(ctx: ReleaseContext, *, reason: str) -> None:
        ctx.lock = None
        raise failure

    with (
        patch(_PREFLIGHT + ".abandon_release", side_effect=abandon),
        pytest.raises(ReleaseError, match="re-arm failed"),
    ):
        resume_lock.run("prepare")
    assert resume_lock.names() == ["take", "comment", "history", "quiesce", "strays"]


def test_resume_lock_record_without_a_tracking_issue_number_refuses() -> None:
    ctx = _resume_ctx()
    ctx.issue_number = None
    with pytest.raises(ReleaseError, match="no tracking issue number"):
        preflight_module._record_resume_lock(ctx, cast("Any", FakeLockOwner()), "adopted it")
