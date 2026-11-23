from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import (
    BRANCH_LOOKUP,
    branch_lookup,
    branch_pr,
    fork_skip,
)
from vergil_tooling.lib import github, retry, trust
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.prepare import _create_pr, prepare

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.release.prepare"
_HEAD = "h0"


@pytest.fixture(autouse=True)
def _pushed_head() -> Iterator[None]:
    """The release branch's tip, read when the PR is opened; a test may patch its own."""
    with patch(_MOD + ".git.read_output", return_value=_HEAD):
        yield


def _ctx(*, version_override: str | None = None) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=version_override,
        # preflight creates the worktree (distinct from repo_root) and sets
        # the release branch, then chdir's into the worktree.
        release_branch="release/2.1.0",
        worktree_path=Path("/tmp/repo/.worktrees/release-2.1.0"),  # noqa: S108
    )
    ctx.issue_number = 42
    ctx.issue_url = "https://github.com/owner/repo/issues/42"
    return ctx


def test_prepare_pushes_branch_and_opens_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)
    assert ctx.release_branch == "release/2.1.0"
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"


def test_prepare_fails_without_release_branch() -> None:
    """preflight owns branch creation; prepare refuses if it did not run."""
    ctx = _ctx()
    ctx.release_branch = None
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(),
        pytest.raises(ReleaseError, match="No release branch"),
    ):
        prepare(ctx)


def test_prepare_never_switches_head() -> None:
    """The branch is the worktree's own — prepare never checks out (#1578)."""
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    checkout_calls = [c for c in git_run_calls if c and c[0] == "checkout"]
    assert checkout_calls == [], f"Unexpected checkout calls: {checkout_calls}"
    merge_calls = [c for c in git_run_calls if "merge" in c]
    assert merge_calls == [], f"Unexpected merge calls: {merge_calls}"


def test_prepare_with_version_override() -> None:
    """Version override bumps on the release branch before changelog."""
    ctx = _ctx(version_override="minor")
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.show", return_value="2.0.3"),
        patch(_MOD + ".version.bump", return_value="2.1.0") as m_bump,
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    m_bump.assert_called_once_with(ctx.work_root, "minor")
    commit_calls = [c for c in git_run_calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    assert "bump version to 2.1.0" in commit_calls[0][2]


def test_prepare_refuses_a_bump_that_misses_the_target_version() -> None:
    # #4899: the bump must reach the target the state block records.
    ctx = _ctx(version_override="minor")
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + ".version.bump") as m_bump,
        pytest.raises(ReleaseError, match=r"VERSION 2\.1\.0 gives 2\.2\.0, not .* 2\.1\.0"),
    ):
        prepare(ctx)
    m_bump.assert_not_called()
    m_run.assert_not_called()


def test_prepare_refuses_an_open_pr_while_the_bump_is_pending() -> None:
    # #4899: a branch still lacking its bump has no release PR to adopt.
    ctx = _ctx(version_override="minor")
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(100, "OPEN")),
        patch(_MOD + ".version.bump") as m_bump,
        pytest.raises(ReleaseError, match=r"pull/100 exists .* minor bump to the target"),
    ):
        prepare(ctx)
    m_bump.assert_not_called()
    assert ctx.release_pr_url is None


def test_prepare_refuses_a_merged_pr_while_the_bump_is_pending() -> None:
    ctx = _ctx(version_override="minor")
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(880, "MERGED")),
        patch(_MOD + ".version.bump") as m_bump,
        pytest.raises(ReleaseError, match=r"pull/880 exists"),
    ):
        prepare(ctx)
    m_bump.assert_not_called()
    assert ctx.release_pr_url is None


def test_prepare_without_version_override_skips_bump() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump") as mock_bump,
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)
    mock_bump.assert_not_called()


def test_prepare_creates_issue_when_not_yet_adopted() -> None:
    ctx = _ctx()
    ctx.issue_number = None
    with (
        patch(_MOD + ".create_tracking_issue") as m_create,
        branch_lookup(),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/o/r/pull/1"),
    ):
        prepare(ctx)
    m_create.assert_called_once_with(ctx)


def test_prepare_skips_when_release_pr_already_exists() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(100, "OPEN")),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + "._generate_changelog") as m_changelog,
        patch(_MOD + ".github.create_pr") as m_create_pr,
    ):
        prepare(ctx)
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"
    m_changelog.assert_not_called()
    m_create_pr.assert_not_called()
    m_run.assert_not_called()


def test_prepare_adopts_already_merged_release_pr() -> None:
    """Resume-safe: an already-merged release PR is adopted, not recreated (#2998).

    A failure at/after ``merge-release`` leaves the release PR merged. On resume
    the branch has no open PR, so without this guard ``prepare``
    would re-push and ``gh pr create`` would fail ("No commits between ..."). The
    merged PR is adopted so ``merge_release``'s MERGED-skip carries the pipeline
    forward.
    """
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(880, "MERGED", head="abc123")),
        patch(_MOD + ".git.read_output", return_value="abc123"),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + "._generate_changelog") as m_changelog,
        patch(_MOD + ".github.create_pr") as m_create_pr,
    ):
        prepare(ctx)
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/880"
    m_changelog.assert_not_called()
    m_create_pr.assert_not_called()
    m_run.assert_not_called()


def test_prepare_ignores_closed_unmerged_release_pr() -> None:
    """A closed-but-unmerged PR for the branch is not adopted (#2998)."""
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(880, "CLOSED", head="abc123")),
        patch(_MOD + ".git.read_output", return_value="abc123"),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
    ):
        prepare(ctx)
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/101"


def test_prepare_adopts_merged_pr_whose_head_descends_from_local_tip() -> None:
    """A server-side update-branch moved the PR head ahead of the local tip (#3172).

    ``merge-release`` updated the BEHIND release PR on GitHub (a merge of main
    into the branch), so the merged PR's head descends from — but no longer
    equals — the local tip. The PR is adopted and the local branch is
    fast-forwarded to the real head; nothing is pushed and no PR is created.
    """
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(880, "MERGED", head="prhead")),
        patch(_MOD + ".git.read_output", return_value="localtip"),
        patch(_MOD + ".git.is_ancestor", return_value=True) as m_anc,
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + "._generate_changelog") as m_changelog,
        patch(_MOD + ".github.create_pr") as m_create_pr,
    ):
        prepare(ctx)
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/880"
    m_anc.assert_called_once_with("localtip", "prhead")
    assert git_run_calls == [
        ("fetch", "origin", "pull/880/head"),
        ("merge", "--ff-only", "prhead"),
    ]
    m_changelog.assert_not_called()
    m_create_pr.assert_not_called()


def test_prepare_fails_loud_on_merged_pr_with_unrelated_tip() -> None:
    """A merged PR whose head does not descend from the local tip fails loud (#3172).

    Keeps the #1719 reused-branch-name straggler protection (an unrelated tip is
    never adopted) but, unlike before, never falls through to a push: pushing
    would re-create the deleted remote release branch at a stale tip.
    """
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(880, "MERGED", head="oldsha")),
        patch(_MOD + ".git.read_output", return_value="newsha"),
        patch(_MOD + ".git.is_ancestor", return_value=False),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + "._generate_changelog") as m_changelog,
        patch(_MOD + ".github.create_pr") as m_create_pr,
        pytest.raises(ReleaseError, match="already merged") as exc_info,
    ):
        prepare(ctx)
    assert "does not descend" in str(exc_info.value)
    assert exc_info.value.phase == "prepare"
    assert all(c[0] != "push" for c in git_run_calls)
    assert all(c[0] != "merge" for c in git_run_calls)
    m_changelog.assert_not_called()
    m_create_pr.assert_not_called()
    assert ctx.release_pr_url is None


def test_prepare_ignores_fork_release_pr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork PR on the release branch's name is never adopted, only reported.

    GHSA-r4j2-cwcp-xg56: the fresh path runs and opens the release's own PR.
    """
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # per-item report, not counts
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(skipped=(fork_skip(666),)) as lookup,
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/owner/repo/pull/100"),
    ):
        prepare(ctx)
    lookup.assert_called_once_with("owner/repo", "release/2.1.0", state="all")
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"
    err = capsys.readouterr().err
    assert "skipped untrusted content" in err
    assert "#666" in err
    assert trust.CROSS_REPOSITORY in err


def test_prepare_refuses_several_open_prs_on_the_branch() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(branch_pr(100, "OPEN"), branch_pr(101, "OPEN")),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".github.create_pr") as m_create_pr,
        pytest.raises(ReleaseError, match=r"2 open PRs on 'release/2\.1\.0' \(#100, #101\)"),
    ):
        prepare(ctx)
    m_run.assert_not_called()
    m_create_pr.assert_not_called()
    assert ctx.release_pr_url is None


def test_prepare_fails_loud_when_the_pr_lookup_fails() -> None:
    """A failed lookup is never "no PR": prepare pushes and opens nothing."""
    ctx = _ctx()
    failure = trust.TrustReadError("prs_for_branch owner/repo release/2.1.0", "the call failed")
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(BRANCH_LOOKUP, side_effect=failure),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".github.create_pr") as m_create_pr,
        pytest.raises(ReleaseError, match="Could not read the PRs") as info,
    ):
        prepare(ctx)
    assert info.value.phase == "prepare"
    assert "the call failed" in str(info.value.detail)
    m_run.assert_not_called()
    m_create_pr.assert_not_called()


def test_generate_changelog_skips_when_prepare_commit_present() -> None:
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    with (
        patch(
            _MOD + ".git.read_output",
            return_value="chore(release): prepare 2.1.0",
        ),
        patch(_MOD + ".changelog.generate_changelog") as m_cl,
        patch(_MOD + ".git.run") as m_run,
    ):
        _generate_changelog(ctx)
    m_cl.assert_not_called()
    m_run.assert_not_called()


def test_generate_changelog_targets_worktree() -> None:
    """Artifacts are written into the managed worktree, not the main checkout.

    Regression test for #1626: preflight chdir's into the worktree, so every
    ``git add`` runs there. Writing CHANGELOG/notes to ``repo_root`` (the main
    checkout) instead left ``git add <abs-notes-path>`` pointing outside the
    worktree ("fatal: ... is outside repository") and broke every release.
    """
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    assert ctx.work_root == ctx.worktree_path
    assert ctx.work_root != ctx.repo_root
    notes_path = ctx.work_root / "releases" / "v2.1.0.md"
    with (
        patch(_MOD + ".changelog.generate_changelog") as mock_cl,
        patch(
            _MOD + ".changelog.generate_release_notes",
            return_value=notes_path,
        ) as mock_rn,
        patch(_MOD + ".git.run") as mock_git,
        patch(_MOD + ".git.read_output", return_value="M CHANGELOG.md"),
    ):
        _generate_changelog(ctx)
    mock_cl.assert_called_once_with(ctx.work_root, ctx.version)
    mock_rn.assert_called_once_with(ctx.work_root, ctx.version)
    # The notes path git adds must live under the worktree, never repo_root.
    add_calls = [c.args for c in mock_git.call_args_list if c.args and c.args[0] == "add"]
    assert ("add", str(notes_path)) in add_calls
    assert str(ctx.worktree_path) in str(notes_path)


def test_generate_changelog_fails_on_no_changes() -> None:
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    notes_path = Path("/tmp/repo/releases/v2.1.0.md")  # noqa: S108
    with (
        patch(_MOD + ".changelog.generate_changelog"),
        patch(
            _MOD + ".changelog.generate_release_notes",
            return_value=notes_path,
        ),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=""),
        pytest.raises(ReleaseError, match="No publishable changes"),
    ):
        _generate_changelog(ctx)


# --- a gh pr create that failed: adopt the PR it opened, or fail (#4699) ---

_RELEASE_PR = "https://github.com/owner/repo/pull/100"
_TIMEOUT = github.GitHubAPIError(1, ("gh", "pr", "create"), "", "HTTP 504: Gateway Timeout")


_OPEN_RELEASE_PR = github.OpenPR(
    url=_RELEASE_PR,
    title="release: 2.1.0",
    head_ref="release/2.1.0",
    base_ref="main",
    head_oid=_HEAD,
    cross_repo=False,
)


def _prepare_with_create(
    create_effect: list[object], open_prs: list[object]
) -> tuple[ReleaseContext, MagicMock, MagicMock, MagicMock]:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        branch_lookup(),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(_MOD + ".github.create_pr", side_effect=create_effect) as create,
        patch(_MOD + ".github.open_prs_for_head", side_effect=open_prs) as lookup,
        patch("vergil_tooling.lib.pr_create.time.sleep") as sleep,
    ):
        prepare(ctx)
    return ctx, create, lookup, sleep


def test_prepare_opens_the_pr_with_one_gh_attempt_per_try() -> None:
    _ctx_, create, lookup, _sleep = _prepare_with_create([_RELEASE_PR], [])
    assert create.call_args.kwargs["retry"] is False
    assert create.call_args.kwargs["base"] == "main"
    lookup.assert_not_called()


def test_prepare_adopts_the_release_pr_a_failed_create_opened() -> None:
    ctx, create, lookup, sleep = _prepare_with_create([_TIMEOUT], [[_OPEN_RELEASE_PR]])
    assert ctx.release_pr_url == _RELEASE_PR
    create.assert_called_once()
    lookup.assert_called_once_with("release/2.1.0", base="main")
    sleep.assert_not_called()


def test_prepare_refuses_an_open_pr_it_did_not_open() -> None:
    other = dataclasses.replace(_OPEN_RELEASE_PR, head_oid="ffff")
    with pytest.raises(ReleaseError, match="was not adopted") as exc:
        _prepare_with_create([_TIMEOUT], [[other]])
    assert exc.value.phase == "prepare"
    assert _RELEASE_PR in str(exc.value)
    assert "its head commit is 'ffff', not 'h0'" in str(exc.value)


def test_prepare_create_that_really_failed_is_retried_then_raised() -> None:
    attempts = retry.MAX_RETRIES + 1
    create = MagicMock(side_effect=[_TIMEOUT] * attempts)
    with (
        patch(_MOD + ".github.create_pr", create),
        patch(_MOD + ".github.open_prs_for_head", return_value=[]) as lookup,
        patch("vergil_tooling.lib.pr_create.time.sleep") as sleep,
        pytest.raises(github.GitHubAPIError, match="Gateway Timeout"),
    ):
        _create_pr(_ctx(), "release/2.1.0")
    assert create.call_count == attempts
    assert lookup.call_count == attempts
    assert sleep.call_count == retry.MAX_RETRIES
