from __future__ import annotations

import re
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release import checklist, state
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
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

if TYPE_CHECKING:
    from collections.abc import Callable

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
