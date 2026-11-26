from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import branch_lookup, branch_pr, fork_skip
from vergil_tooling.lib import finalize_watch, github, pr_create, pr_merge, pr_path, trust
from vergil_tooling.lib.release.bump import BACK_MERGE_DEADLINE_SECS, back_merge_and_bump
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.release.bump"


_POST = "release/post-2.1.0"


@pytest.fixture(autouse=True)
def _branch_lookup() -> Iterator[MagicMock]:
    """No PR on the post branch unless a test says otherwise."""
    with branch_lookup() as m:
        yield m


@pytest.fixture(autouse=True)
def _no_post_branch() -> Iterator[None]:
    """No post branch, local or on origin, unless a test says otherwise.

    The fresh path first looks for a post branch an earlier run left behind
    (#4415); these mocked tests start with none. The resume paths are tested
    against real repositories in ``test_release_bump_resume.py``.
    """
    with (
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        yield


_MERGE_SHA = "b4c4me4ge5ha"


@pytest.fixture(autouse=True)
def _merge_commit() -> Iterator[MagicMock]:
    """The merged back-merge PR's merge commit, recorded on every path (#3329)."""
    with patch(_MOD + ".github.merge_commit_sha", return_value=_MERGE_SHA) as m:
        yield m


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
        # preflight chdir's into this worktree; the bump must land here.
        worktree_path=Path("/tmp/repo/.worktrees/release-2.1.0"),  # noqa: S108
    )
    ctx.issue_number = 42
    return ctx


def test_back_merge_creates_branch_and_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._arm_and_wait"),
    ):
        back_merge_and_bump(ctx)

    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    assert ctx.next_version == "2.1.1"


def test_back_merge_fetches_main_first() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._arm_and_wait"),
    ):
        back_merge_and_bump(ctx)

    fetch_idx = next(i for i, c in enumerate(git_run_calls) if c[0] == "fetch")
    checkout_idx = next(i for i, c in enumerate(git_run_calls) if c[:2] == ("checkout", "-b"))
    assert fetch_idx < checkout_idx


def test_back_merge_commits_version_bump() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._arm_and_wait"),
    ):
        back_merge_and_bump(ctx)

    commit_calls = [c for c in git_run_calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    assert "bump version to 2.1.1" in commit_calls[0][2]


def test_back_merge_bumps_in_worktree() -> None:
    """The version bump writes into the worktree, not the main checkout (#1626)."""
    ctx = _ctx()
    assert ctx.work_root == ctx.worktree_path
    assert ctx.work_root != ctx.repo_root
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1") as mock_bump,
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._arm_and_wait"),
    ):
        back_merge_and_bump(ctx)

    mock_bump.assert_called_once_with(ctx.work_root)


def test_back_merge_creates_pr_to_develop() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr") as mock_pr,
        patch(_MOD + "._arm_and_wait"),
    ):
        mock_pr.return_value = "https://github.com/owner/repo/pull/101"
        back_merge_and_bump(ctx)

    mock_pr.assert_called_once()
    call_kwargs = mock_pr.call_args
    assert call_kwargs.kwargs["base"] == "develop"


def test_back_merge_waits_and_merges() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._arm_and_wait") as mock_wm,
    ):
        back_merge_and_bump(ctx)

    mock_wm.assert_called_once_with(ctx, "https://github.com/owner/repo/pull/101", head="")


def test_back_merge_skips_when_listed_open_pr_merged_since() -> None:
    """Listed as open, then merged before its state was read: skip the merge."""
    ctx = _ctx()
    with (
        branch_lookup(branch_pr(9, "OPEN", branch=_POST)),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".version.bump") as m_bump,
        patch(_MOD + ".github.create_pr") as m_pr,
        patch(_MOD + "._arm_and_wait") as m_wm,
    ):
        back_merge_and_bump(ctx)
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/9"
    assert ctx.next_version == "2.1.1"
    m_bump.assert_not_called()
    m_pr.assert_not_called()
    m_wm.assert_not_called()


def test_back_merge_adopts_already_merged_pr_on_resume(_branch_lookup: MagicMock) -> None:
    """Resume after the back-merge merged: adopt it, never re-create the branch (#3328).

    The branch has no open PR, but a merged one: it is adopted. The version is
    read from its pull ref, which outlives a deleted head branch.
    """
    ctx = _ctx()
    _branch_lookup.return_value = (
        [branch_pr(951, "MERGED", branch=_POST)],
        [],
    )
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.show", return_value="2.1.1") as m_show,
        patch(_MOD + ".version.bump") as m_bump,
        patch(_MOD + ".github.create_pr") as m_pr,
        patch(_MOD + "._arm_and_wait") as m_wm,
    ):
        back_merge_and_bump(ctx)

    _branch_lookup.assert_called_once_with("owner/repo", _POST, state="all")
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/951"
    assert ctx.next_version == "2.1.1"
    assert git_run_calls == [("fetch", "origin", "pull/951/head")]
    m_show.assert_called_once_with(ctx.repo_root, ref="FETCH_HEAD")
    m_bump.assert_not_called()
    m_pr.assert_not_called()
    m_wm.assert_not_called()


def test_back_merge_ignores_closed_unmerged_pr(_branch_lookup: MagicMock) -> None:
    """A closed but unmerged back-merge PR is not adopted: the fresh path runs."""
    ctx = _ctx()
    _branch_lookup.return_value = (
        [branch_pr(950, "CLOSED", branch=_POST)],
        [],
    )
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ) as m_pr,
        patch(_MOD + "._arm_and_wait") as m_wm,
    ):
        back_merge_and_bump(ctx)

    m_pr.assert_called_once()
    m_wm.assert_called_once_with(ctx, "https://github.com/owner/repo/pull/101", head="")
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    assert ctx.next_version == "2.1.1"


def test_bump_ignores_fork_post_release_pr(capsys: pytest.CaptureFixture[str]) -> None:
    """A fork PR on the post branch's name is never adopted or merged, only reported.

    GHSA-r4j2-cwcp-xg56: the fresh path runs and opens the release's own PR.
    """
    ctx = _ctx()
    with (
        branch_lookup(skipped=(fork_skip(666),)) as lookup,
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ) as m_pr,
        patch(_MOD + "._arm_and_wait") as m_wm,
    ):
        back_merge_and_bump(ctx)
    lookup.assert_called_once_with("owner/repo", _POST, state="all")
    m_pr.assert_called_once()
    m_wm.assert_called_once_with(ctx, "https://github.com/owner/repo/pull/101", head="")
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    err = capsys.readouterr().err
    assert "#666" in err
    assert trust.CROSS_REPOSITORY in err


def test_bump_refuses_several_open_prs_on_the_post_branch() -> None:
    ctx = _ctx()
    with (
        branch_lookup(branch_pr(9, "OPEN", branch=_POST), branch_pr(10, "OPEN", branch=_POST)),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + "._arm_and_wait") as m_wm,
        pytest.raises(ReleaseError, match=r"2 open PRs on 'release/post-2\.1\.0' \(#9, #10\)"),
    ):
        back_merge_and_bump(ctx)
    m_run.assert_not_called()
    m_wm.assert_not_called()
    assert ctx.bump_pr_url is None


def test_back_merge_merges_existing_open_pr() -> None:
    ctx = _ctx()
    with (
        branch_lookup(branch_pr(9, "OPEN", branch=_POST)),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr") as m_pr,
        patch(_MOD + "._arm_and_wait") as m_wm,
    ):
        back_merge_and_bump(ctx)
    m_wm.assert_called_once_with(ctx, "https://github.com/owner/repo/pull/9", head=None)
    m_pr.assert_not_called()
    assert ctx.next_version == "2.1.1"


def test_back_merge_existing_pr_state_retries_404() -> None:
    """The PR URL was just listed by GitHub, so a 404 on reading it is lag (#3137)."""
    ctx = _ctx()
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404: Not Found")
    with (
        branch_lookup(branch_pr(9, "OPEN", branch=_POST)),
        patch(_MOD + ".github.pr_state", side_effect=[err, "MERGED"]) as m_state,
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + "._arm_and_wait") as m_wm,
        patch("vergil_tooling.lib.retry.time.sleep"),
    ):
        back_merge_and_bump(ctx)
    assert m_state.call_count == 2
    m_wm.assert_not_called()


def test_post_branch_version_reads_from_fetch_head() -> None:
    from vergil_tooling.lib.release.bump import _post_branch_version

    ctx = _ctx()
    with (
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".version.show", return_value="2.1.1") as m_show,
    ):
        assert _post_branch_version(ctx, "release/post-2.1.0") == "2.1.1"
    m_run.assert_called_once_with("fetch", "origin", "release/post-2.1.0")
    m_show.assert_called_once_with(ctx.repo_root, ref="FETCH_HEAD")


def test_back_merge_never_returns_to_develop() -> None:
    """Work happens in the release worktree, so back-merge never checks out
    develop in the root checkout (#1578). The root's develop is synced later
    by the finalize cleanup stage. The only checkout is the worktree's own
    `checkout -b release/post-<v>`."""
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._arm_and_wait"),
    ):
        back_merge_and_bump(ctx)

    checkout_calls = [c for c in git_run_calls if c and c[0] == "checkout"]
    assert checkout_calls == [("checkout", "-b", "release/post-2.1.0", "origin/main")]
    assert ("checkout", "develop") not in git_run_calls
    assert not any("merge" in c for c in git_run_calls)
    assert not any(c[0] == "pull" for c in git_run_calls)


# --- #3329: back-merge-bump records the back-merge's merge commit SHA ---


def test_back_merge_create_path_records_merge_sha(_merge_commit: MagicMock) -> None:
    """The fresh path reads the merge commit only after the PR merged."""
    ctx = _ctx()
    order: list[str] = []
    _merge_commit.side_effect = lambda _url: order.append("sha") or _MERGE_SHA
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/owner/repo/pull/101"),
        patch(_MOD + "._arm_and_wait", side_effect=lambda *_a, **_k: order.append("merge")),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/101")
    assert order == ["merge", "sha"]
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_adopt_open_path_records_merge_sha(_merge_commit: MagicMock) -> None:
    """Adopting a still-open PR merges it, then records its merge commit."""
    ctx = _ctx()
    order: list[str] = []
    _merge_commit.side_effect = lambda _url: order.append("sha") or _MERGE_SHA
    with (
        branch_lookup(branch_pr(9, "OPEN", branch=_POST)),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + "._arm_and_wait", side_effect=lambda *_a, **_k: order.append("merge")),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/9")
    assert order == ["merge", "sha"]
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_adopt_listed_open_but_merged_records_merge_sha(
    _merge_commit: MagicMock,
) -> None:
    ctx = _ctx()
    with (
        branch_lookup(branch_pr(9, "OPEN", branch=_POST)),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + "._arm_and_wait"),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/9")
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_adopt_merged_path_records_merge_sha(
    _branch_lookup: MagicMock, _merge_commit: MagicMock
) -> None:
    """Resume after the back-merge merged: the adopted PR's merge commit is recorded."""
    ctx = _ctx()
    _branch_lookup.return_value = (
        [branch_pr(951, "MERGED", branch=_POST)],
        [],
    )
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.show", return_value="2.1.1"),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/951")
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_records_none_when_no_merge_commit(_merge_commit: MagicMock) -> None:
    """An empty mergeCommit is recorded as None, never as an empty SHA."""
    ctx = _ctx()
    _merge_commit.return_value = ""
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/owner/repo/pull/101"),
        patch(_MOD + "._arm_and_wait"),
    ):
        back_merge_and_bump(ctx)
    assert ctx.bump_merge_sha is None


# --- a gh pr create that failed: adopt the PR it opened, or fail (#4699) ---

_BUMP_PR = "https://github.com/owner/repo/pull/101"
_BUMP_BRANCH = "release/post-2.1.0"
_BUMP_HEAD = "b0mp"
_TIMEOUT = github.GitHubAPIError(1, ("gh", "pr", "create"), "", "HTTP 504: Gateway Timeout")
_OPEN_BUMP_PR = trust.PRRef(
    number=101,
    url=_BUMP_PR,
    title="chore(release): back-merge 2.1.0 and bump to 2.1.1",
    head=_BUMP_BRANCH,
    head_oid=_BUMP_HEAD,
    base="develop",
    author_role=trust.Role.AGENTS,
    state="OPEN",
)


def _bump_with_create(
    create: MagicMock, open_prs: list[object]
) -> tuple[ReleaseContext, MagicMock, MagicMock, MagicMock]:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_BUMP_HEAD),
        patch(_MOD + "._checkout_post_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr", create),
        patch("vergil_tooling.lib.pr_create.open_trusted_prs", side_effect=open_prs) as lookup,
        patch("vergil_tooling.lib.pr_create.time.sleep") as sleep,
        patch(_MOD + "._arm_and_wait") as wait,
    ):
        back_merge_and_bump(ctx)
    return ctx, lookup, sleep, wait


def test_back_merge_opens_the_pr_with_one_gh_attempt_per_try() -> None:
    create = MagicMock(return_value=_BUMP_PR)
    _ctx_, lookup, _sleep, _wait = _bump_with_create(create, [])
    assert create.call_args.kwargs["retry"] is False
    lookup.assert_not_called()


def test_back_merge_adopts_the_pr_a_failed_create_opened() -> None:
    create = MagicMock(side_effect=[_TIMEOUT])
    ctx, lookup, sleep, wait = _bump_with_create(create, [[_OPEN_BUMP_PR]])
    assert ctx.bump_pr_url == _BUMP_PR
    create.assert_called_once()
    [spec] = lookup.call_args.args
    assert (spec.head, spec.base) == (_BUMP_BRANCH, "develop")
    sleep.assert_not_called()
    wait.assert_called_once_with(ctx, _BUMP_PR, head=_BUMP_HEAD)


def test_back_merge_refuses_an_open_pr_it_did_not_open() -> None:
    other = dataclasses.replace(_OPEN_BUMP_PR, title="something else")
    with pytest.raises(ReleaseError, match="was not adopted") as exc:
        _bump_with_create(MagicMock(side_effect=[_TIMEOUT]), [[other]])
    assert exc.value.phase == "back-merge-bump"
    assert _BUMP_PR in str(exc.value)
    assert "its title is 'something else'" in str(exc.value)


def test_back_merge_create_that_really_failed_is_retried() -> None:
    create = MagicMock(side_effect=[_TIMEOUT, _BUMP_PR])
    ctx, lookup, sleep, wait = _bump_with_create(create, [[]])
    assert ctx.bump_pr_url == _BUMP_PR
    assert create.call_count == 2
    lookup.assert_called_once()
    sleep.assert_called_once()
    wait.assert_called_once_with(ctx, _BUMP_PR, head=_BUMP_HEAD)


# --- the back-merge arms through the one PR path (epic vergil-project/.github#1664) ---


def _arm_result(outcome: str, detail: str = "") -> pr_path.ArmResult:
    method = None if outcome == "refused" else "MERGE"
    return pr_path.ArmResult(
        outcome=outcome,  # type: ignore[arg-type]
        pr_url=_BUMP_PR,
        head=_BUMP_HEAD,
        method=method,  # type: ignore[arg-type]
        detail=detail,
    )


def _snapshot(head_ref: str) -> finalize_watch.Snapshot:
    return finalize_watch.Snapshot(
        node_id="PR_1",
        url=_BUMP_PR,
        title="chore(release): back-merge 2.1.0 and bump to 2.1.1",
        state="OPEN",
        base="develop",
        head_ref=head_ref,
        head=_BUMP_HEAD,
        auto_merge_on=False,
        merge_state="BLOCKED",
        rollup=(),
        merge_commit="",
        labels=(),
    )


def _fresh_bump(arm: MagicMock, wait: MagicMock) -> tuple[ReleaseContext, MagicMock]:
    """Run the fresh path, a new back-merge PR, with the path and the wait replaced."""
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_BUMP_HEAD),
        patch(_MOD + "._checkout_post_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr", return_value=_BUMP_PR),
        patch("vergil_tooling.lib.pr_create.open_trusted_prs", return_value=[]),
        patch("vergil_tooling.lib.pr_create.PRSpec", wraps=pr_create.PRSpec) as spec,
        patch(_MOD + ".pr_path.arm", arm),
        patch(_MOD + ".pr_merge.wait_until_merged", wait),
    ):
        back_merge_and_bump(ctx)
    return ctx, spec


def test_back_merge_arms_through_path_with_merge_commit() -> None:
    arm = MagicMock(return_value=_arm_result("armed"))
    wait = MagicMock()
    ctx, spec = _fresh_bump(arm, wait)
    arm.assert_called_once_with(_BUMP_PR, clone=ctx.work_root, head=_BUMP_HEAD)
    wait.assert_called_once()
    assert ctx.bump_pr_url == _BUMP_PR
    assert ctx.bump_merge_sha == _MERGE_SHA
    # The PR the back-merge opens is one the path merges with a merge commit:
    # a release/post-* head, from a trusted author on a same-repo branch.
    head_ref = spec.call_args.kwargs["head"]
    assert head_ref == _BUMP_BRANCH
    pr = pr_path._PR(  # noqa: SLF001
        snap=_snapshot(head_ref), draft=False, repo="owner/repo", number=101, branch=head_ref
    )
    assert pr_path._method(pr) == "MERGE"  # noqa: SLF001


def test_back_merge_never_calls_a_merge_itself() -> None:
    """The back-merge only arms and waits: no direct merge and no wait-and-merge."""
    with (
        patch("vergil_tooling.lib.github.merge") as merge,
        patch("vergil_tooling.lib.pr_merge.wait_and_merge") as engine,
    ):
        _fresh_bump(MagicMock(return_value=_arm_result("armed")), MagicMock())
    merge.assert_not_called()
    engine.assert_not_called()


def test_back_merge_merged_by_the_path_does_not_wait() -> None:
    wait = MagicMock()
    ctx, _spec = _fresh_bump(MagicMock(return_value=_arm_result("merged")), wait)
    wait.assert_not_called()
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_resume_rearms_open_post_pr() -> None:
    """A resume finds the back-merge PR still open: it is armed again, at its current head."""
    ctx = _ctx()
    arm = MagicMock(return_value=_arm_result("armed"))
    with (
        branch_lookup(branch_pr(101, "OPEN", branch=_POST)),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr") as create,
        patch(_MOD + ".pr_path.arm", arm),
        patch(_MOD + ".pr_merge.wait_until_merged") as wait,
    ):
        back_merge_and_bump(ctx)
    arm.assert_called_once_with(_BUMP_PR, clone=ctx.work_root, head=None)
    wait.assert_called_once_with(_BUMP_PR, deadline_secs=BACK_MERGE_DEADLINE_SECS)
    create.assert_not_called()
    assert ctx.next_version == "2.1.1"


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        ("refused", "provenance violations found (mallory (outsider): pushed)"),
        ("held", "held for release 2.1.0"),
    ],
)
def test_back_merge_refused_raises_naming_outcome(outcome: str, detail: str) -> None:
    wait = MagicMock()
    with pytest.raises(ReleaseError) as excinfo:
        _fresh_bump(MagicMock(return_value=_arm_result(outcome, detail)), wait)
    assert excinfo.value.phase == "back-merge-bump"
    assert f"{outcome}: {detail}" in str(excinfo.value)
    assert _BUMP_PR in str(excinfo.value)
    wait.assert_not_called()


def test_back_merge_path_error_raises_release_error() -> None:
    arm = MagicMock(side_effect=pr_path.PRPathError(f"{_BUMP_PR}: reading the PR failed: boom"))
    wait = MagicMock()
    with pytest.raises(ReleaseError, match="Arming the back-merge PR failed: .*boom") as excinfo:
        _fresh_bump(arm, wait)
    assert excinfo.value.phase == "back-merge-bump"
    wait.assert_not_called()


def test_back_merge_wait_uses_explicit_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait's deadline is the stage's own constant, not $VRG_FINALIZE_DEADLINE."""
    monkeypatch.setenv(finalize_watch.DEADLINE_ENV, "5")
    wait = MagicMock()
    _fresh_bump(MagicMock(return_value=_arm_result("armed")), wait)
    wait.assert_called_once_with(_BUMP_PR, deadline_secs=BACK_MERGE_DEADLINE_SECS)
    assert BACK_MERGE_DEADLINE_SECS > 0


def test_back_merge_wait_that_ends_unmerged_raises_release_error() -> None:
    wait = MagicMock(side_effect=pr_merge.MergeDeadlineError(f"PR {_BUMP_PR} did not merge"))
    with pytest.raises(ReleaseError, match="did not merge") as excinfo:
        _fresh_bump(MagicMock(return_value=_arm_result("armed")), wait)
    assert excinfo.value.phase == "back-merge-bump"
    assert "vrg-release --resume" in str(excinfo.value)
