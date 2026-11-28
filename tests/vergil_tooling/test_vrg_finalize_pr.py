"""Tests for vergil_tooling.bin.vrg_finalize_pr."""

from __future__ import annotations

import json
import re
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from subprocess import CompletedProcess
from typing import TYPE_CHECKING
from unittest.mock import ANY, MagicMock, patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.bin import vrg_pr_await
from vergil_tooling.bin.vrg_finalize_pr import (
    EXIT_HELD_BY_RELEASE,
    CDUnreadableError,
    FinalizeContext,
    FinalizeError,
    ReleaseGuard,
    _classify_failed_jobs,
    _clean_branch_images,
    _confirmed_merged_branch,
    _declares_cd_workflow,
    _delete_branch_and_worktree,
    _dirt_is_untracked_only,
    _head_of,
    _merged_branches,
    _parse_pr_list,
    _pr_row,
    _prune_orphan_branch_images,
    _prune_orphan_relay_refs,
    _report_cd_run,
    _resolve_open_prs,
    _stage_advisory_sync,
    _stage_cd_check,
    _stage_close_refs,
    _stage_provision,
    _stage_validation,
    _synced_commits,
    _wait_for_cd_run,
    _worktree_is_dirty,
    build_stages,
    main,
    parse_args,
)
from vergil_tooling.lib import (
    closing_refs,
    finalize_watch,
    github,
    progress,
    security_records,
    text_table,
    trust,
    worktrees,
)
from vergil_tooling.lib.cache_space import CacheSpaceError
from vergil_tooling.lib.cd_runs import (
    MAX_READ_FAILURES,
    CDRunProblem,
    CDRunSuperseded,
    _supersede_group,
    cd_timeout_secs,
)
from vergil_tooling.lib.checkout_lock import CheckoutLockError, checkout_lock
from vergil_tooling.lib.closing_refs import ClosingRefs
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.finalize_watch import FinalizeWatchError, Outcome, PRResult
from vergil_tooling.lib.github import ClosingIssue, GitHubAPIError
from vergil_tooling.lib.live_branches import RepoSetError
from vergil_tooling.lib.release.gate import unreleased_refusal as _real_unreleased_refusal
from vergil_tooling.lib.release_lock.model import LockRecord, RefReadError
from vergil_tooling.lib.release_lock.refs import LockState
from vergil_tooling.lib.release_lock.watch import LockWatch
from vergil_tooling.lib.trust import PRRef, Role, TrustReadError
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.worktrees import Worktree, WorktreeState, WorktreeStatus
from vergil_tooling.lib.worktrees import lock_protection as _real_lock_protection

_MOD = "vergil_tooling.bin.vrg_finalize_pr"
_PROJECTS = Path("/vergil/projects")

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from tests.vergil_tooling.conftest import FakeApp


@pytest.fixture(autouse=True)
def _no_cwd_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve no org from the cwd, so a ``gh`` token lookup never runs git.

    The CD lookup runs ``gh`` with the current identity's token, whose owner
    comes from the cwd's remote (#5140); the ``subprocess.run`` fakes here
    answer ``gh`` only. :func:`fake_app` sets its own.
    """
    monkeypatch.setattr(github, "detect_org", lambda *, cwd=None: None)


@pytest.fixture(autouse=True)
def _no_release_lock() -> Iterator[None]:
    """Default every test to a repo whose release lock is not held.

    Cleanup reads the lock for every worktree it would remove (Task 27 of epic
    vergil-project/.github#1664). The tests of the lock's key re-patch these,
    or patch the lock read beneath them — the innermost patch wins.
    """
    with (
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".worktrees.lock_protection", return_value=_NO_LOCK),
    ):
        yield


@pytest.fixture(autouse=True)
def _released_repo() -> Iterator[None]:
    """Default every test to a repo that declares [publish].release = true.

    The refusal tests override it — the innermost patch wins.
    """
    with patch(_MOD + ".gate.unreleased_refusal", return_value=None):
        yield


def _repo_and_number(pr: str, _purpose: str) -> tuple[str, int]:
    """``github.pr_repo_and_number`` for a test PR: its URL's number, or the PR as a number."""
    return "o/r", int(pr.rsplit("/", 1)[-1].lstrip("#"))


@pytest.fixture(autouse=True)
def _merged_prs_confirmed() -> Iterator[None]:
    """Default: the trust layer confirms each merged PR's branch.

    The tests of the confirmation itself re-patch these, or call
    :func:`_confirmed_merged_branch` directly — the innermost patch wins.
    """
    with (
        patch(_MOD + ".github.pr_repo_and_number", side_effect=_repo_and_number),
        patch(_MOD + "._confirmed_merged_branch", return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def _main_worktree() -> Iterator[None]:
    """Default every test to running in the main worktree.

    Individual tests can override by patching is_main_worktree directly —
    the innermost patch wins.
    """
    with patch(_MOD + ".git.is_main_worktree", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _interactive_defaults() -> Iterator[None]:
    """Default: TTY present, no worktree candidates, cleanup confirmed.

    Inference tests override by patching these targets directly — the
    innermost patch wins.
    """
    with (
        patch(_MOD + ".worktrees.require_tty"),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".confirm", return_value=True),
    ):
        yield


@pytest.fixture(autouse=True)
def _clean_working_tree() -> Iterator[None]:
    """Default every test to a clean working tree.

    Individual tests can override by patching working_tree_status
    directly — the innermost patch wins.
    """
    with patch(_MOD + ".git.working_tree_status", return_value=""):
        yield


@pytest.fixture(autouse=True)
def _relay_refs_inert() -> Iterator[None]:
    """Default: relay-ref cleanup touches nothing (issue #2369).

    Cleanup drops each deleted branch's pr-workflow relay ref and sweeps
    orphaned refs; both do remote ls-remote/push. Stub the seams so the many
    cleanup tests neither hit the network nor need to know about relay refs.
    Tests that assert relay behavior re-patch these directly — the innermost
    patch wins.
    """
    with (
        patch(_MOD + ".github_transport.GitHubTransport"),
        patch(_MOD + ".github_transport.list_relay_branches", return_value=[]),
        patch(_MOD + ".git.remote_branches", return_value=set()),
    ):
        yield


@pytest.fixture(autouse=True)
def _branch_images_inert() -> Iterator[None]:
    """Default: the cached-image sweep touches nothing (issue #2600).

    Cleanup sweeps orphaned per-branch dev images, which resolves the platform
    repo set and shells out to the container runtime. Stub those seams so the
    many cleanup tests neither hit docker/nerdctl nor need to know about image
    pruning. Tests that assert sweep behavior re-patch these directly — the
    innermost patch wins.
    """
    with (
        patch(_MOD + ".default_repo_roots", return_value=[_PROJECTS]),
        patch(_MOD + ".discover_repos", return_value=[_PROJECTS / "org" / "repo"]),
        patch(_MOD + ".prune_orphan_branch_images", return_value=(0, [])),
    ):
        yield


@pytest.fixture(autouse=True)
def _validation_passes() -> Iterator[None]:
    """Default: the post-finalization validation child succeeds.

    The validation stage streams through progress.run (issue #1479);
    tests that assert validation behavior re-patch it directly — the
    innermost patch wins.
    """
    with patch(_MOD + ".progress.run", return_value=0):
        yield


@pytest.fixture(autouse=True)
def _provisioning_passes() -> Iterator[None]:
    """Default: the provision stage warms the image without touching docker.

    The provision stage calls provision_dev_image (issue #2462); tests that
    assert provisioning behavior re-patch it directly — the innermost patch wins.
    """
    with patch(_MOD + ".provision_dev_image", return_value=("img--develop--test", "cached")):
        yield


@pytest.fixture(autouse=True)
def _no_closing_refs() -> Iterator[None]:
    """Default: the merged PR closes no issues, read without gh (issue #4002).

    Tests that assert close-refs behavior re-patch it directly — the innermost
    patch wins.
    """
    refs = ClosingRefs(
        repo="o/r",
        number=42,
        state="MERGED",
        merged_at="2026-11-08T00:00:00Z",
        counted=(),
        uncounted=(),
        skipped=(),
    )
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", return_value=refs),
        patch(_MOD + ".load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_advisory_sync() -> Iterator[MagicMock]:
    """Default: the advisory sync syncs nothing, with no gh call (vergil-tooling#4583)."""
    with patch(
        _MOD + ".security_records.sync_issues", return_value=security_records.IssueSyncResult()
    ) as sync:
        yield sync


_SYNCED_HEAD = "5ced5ced5ced"


@pytest.fixture(autouse=True)
def _synced_head() -> Iterator[None]:
    """Default: the cleanup stage reads the target branch's head without git.

    The sync records the head before and after it (for the CD check and the
    report of a develop that did not validate); a real ``git rev-parse``
    would read whatever repo the tests run in. Tests that assert on the range
    re-patch it directly — the innermost patch wins.
    """
    with patch(_MOD + "._head_of", return_value=_SYNCED_HEAD):
        yield


def _pr_result(
    pr: str, outcome: Outcome = "merged", detail: str = "", *, violation: bool = False
) -> PRResult:
    """A watch result for *pr*, as lib/finalize_watch.py reports it.

    PR ``…/pull/<n>`` is titled ``feat: change <n>`` on ``feature/<n>-thing``.
    """
    n = pr.rsplit("/", 1)[-1]
    return PRResult(
        pr=pr,
        outcome=outcome,
        detail=detail,
        head="headsha",
        title=f"feat: change {n}",
        branch=f"feature/{n}-thing",
        labels=(),
        provenance_violation=violation,
    )


def _all_merged(prs: list[str], **_kwargs: object) -> list[PRResult]:
    """A watch where every listed PR merged (the finalize_watch.watch fake)."""
    return [_pr_result(pr) for pr in prs]


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.target_branch == "develop"
    assert args.dry_run is False
    assert args.strategy is None


def test_parse_args_custom() -> None:
    args = parse_args(["--target-branch", "main", "--dry-run"])
    assert args.target_branch == "main"
    assert args.dry_run is True


def test_main_rejects_secondary_worktree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main_root = tmp_path / "main-wt"
    main_root.mkdir()

    with (
        patch(_MOD + ".git.is_main_worktree", return_value=False),
        patch(_MOD + ".git.main_worktree_root", return_value=main_root),
    ):
        result = main([])

    assert result == 1
    err = capsys.readouterr().err
    assert "must be run from the main worktree" in err
    assert str(main_root) in err


def _make_profile(tmp_path: Path, model: str) -> None:
    (tmp_path / "vergil.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "{model}"\nrelease-model = "tagged-release"\n'
        f'primary-language = "python"\n\n[dependencies]\nvergil = "v2.0"\n'
        f'\n[ci]\nversions = ["3.14"]\n'
    )


def _pr_ref(number: int, title: str, *, state: str = "OPEN", head_oid: str = "f" * 40) -> PRRef:
    """A same-repo trusted PR, as ``worktrees.open_pr_for_branch`` returns it."""
    return PRRef(
        number=number,
        url=f"https://github.com/o/r/pull/{number}",
        title=title,
        head=f"feature/{number}-x",
        head_oid=head_oid,
        base="develop",
        author_role=Role.HUMAN,
        state=state,
    )


_PR_EVIDENCE = _pr_ref(9, "Done", state="MERGED")


@contextmanager
def _sweep_guards_pass() -> Generator[None]:
    """Make the ancestry-sweep guards (issue #1445) pass.

    The candidate branch resolves to a different SHA than the target
    (it has commits of its own) and a closed PR exists as merge
    evidence, so the sweep proceeds to delete it.
    """
    with (
        patch(_MOD + ".git.branch_commit", side_effect=lambda ref: f"sha-of-{ref}"),
        patch(_MOD + ".worktrees.closed_pr_for_branch", return_value=_PR_EVIDENCE),
    ):
        yield


def test_main_library_release(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run") as mock_run,
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["feature/x", "develop"],
        ),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0
    mock_run.assert_any_call("checkout", "develop")
    mock_run.assert_any_call("branch", "-D", "feature/x")


def test_main_cleanup_syncs_target_without_pull(tmp_path: Path) -> None:
    """Cleanup must fetch then ff-merge the remote-tracking ref — never
    `pull`, whose FETCH_HEAD dependence races concurrent fetches
    (issue #1499)."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["feature/x", "develop"],
        ),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])

    assert result == 0
    fetch_idx = next(
        i
        for i, c in enumerate(git_run_calls)
        if c == ("fetch", "--tags", "--force", "origin", "develop")
    )
    merge_idx = next(
        i for i, c in enumerate(git_run_calls) if c == ("merge", "--ff-only", "origin/develop")
    )
    assert fetch_idx < merge_idx
    assert not any(c[0] == "pull" for c in git_run_calls)


def test_main_already_on_target(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0


def test_main_dry_run(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run") as mock_git_run,
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["feature/x"],
        ),
    ):
        result = main(["--dry-run"])
    assert result == 0
    mock_git_run.assert_not_called()


def test_main_no_profile(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0


def test_main_unrecognized_model(tmp_path: Path) -> None:
    _make_profile(tmp_path, "unknown-model")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
    ):
        result = main([])
    assert result == 1


def test_main_application_promotion(tmp_path: Path) -> None:
    _make_profile(tmp_path, "application-promotion")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(
            "vergil_tooling.bin.vrg_finalize_pr.git.merged_branches",
            return_value=["develop", "release", "main", "feature/y"],
        ),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0


def test_main_docs_single_branch(tmp_path: Path) -> None:
    _make_profile(tmp_path, "docs-single-branch")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0


def test_main_no_deleted_branches(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=["develop"]),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0


def test_main_validation_fails(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(
            _MOD + ".progress.run",
            side_effect=subprocess.CalledProcessError(1, ("vrg-container-run",)),
        ),
        # Mock rev-parse of the target branch so the cleanup-only CD check
        # resolves a head SHA deterministically instead of leaking to the real
        # git env (a CI PR checkout has no local ``develop`` ref, which sent
        # cd-check down the "could not resolve" early return and skipped the
        # _report_cd_run call this test asserts).
        patch(_MOD + ".git.read_output", return_value="deadbeefcafe"),
        patch(_MOD + "._report_cd_run") as cd_check,
    ):
        result = main([])
    assert result == 4  # develop did not validate (spec 6.3)
    # fail_defer: a validation failure must not skip the CD check.
    cd_check.assert_called_once()


def test_main_failed_rebuild_fails_finalize_and_still_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Validation's container would not launch and the rebuild was refused, so
    # validation never ran: the stage is recorded failed and finalize exits
    # non-zero (#3316, #3605), while the CD check and final summary still run.
    _make_profile(tmp_path, "library-release")
    launch = subprocess.CalledProcessError(
        125, ("vrg-container-run",), output="", stderr="parent snapshot x does not exist: not found"
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".progress.run", side_effect=[launch]) as run,
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(
            _MOD + ".provision_dev_image",
            side_effect=[("img", "cached"), CacheSpaceError("only 1.2G free")],
        ),
        patch(_MOD + ".git.read_output", return_value="deadbeefcafe"),
        patch(_MOD + "._report_cd_run") as cd_check,
    ):
        result = main([])
    assert result == 4  # a validate that could not run is 4 too
    assert run.call_count == 1  # no retry against an image that was never rebuilt
    cd_check.assert_called_once()
    out = capsys.readouterr().out
    assert "✗  failures:" in out
    assert "validation — FinalizeError: post-finalization validation did not run" in out
    assert "only 1.2G free" in out


# -- _wait_for_cd_run (issue #2753) ----------------------------------------

_HEAD_SHA = "abc123def456"

# A root with no .github/workflows: it declares no CD workflow.
_NO_WORKFLOWS_ROOT = Path("/nonexistent/vergil-finalize-test-root")


def _write_workflow(root: Path, filename: str, text: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / filename).write_text(text, encoding="utf-8")


def _cd_root(root: Path) -> Path:
    """*root* with a workflow that declares ``name: CD``."""
    _write_workflow(root, "cd.yml", "name: CD\non: push\njobs: {}\n")
    return root


def _cd_list_resp(runs: list[dict]) -> CompletedProcess:
    """A ``gh run list`` response carrying *runs*."""
    return CompletedProcess(args=(), returncode=0, stdout=json.dumps(runs))


def _cd_run(*, sha: str = _HEAD_SHA, run_id: int = 12345, status: str = "queued") -> dict:
    return {
        "databaseId": run_id,
        "headSha": sha,
        "status": status,
        "conclusion": None,
        "url": f"https://github.com/o/r/actions/runs/{run_id}",
    }


def _cd_view_resp(
    status: str,
    conclusion: str | None,
    url: str = "https://github.com/o/r/actions/runs/12345",
) -> CompletedProcess:
    """A ``gh run view`` response."""
    return CompletedProcess(
        args=(),
        returncode=0,
        stdout=json.dumps({"status": status, "conclusion": conclusion, "attempt": 1, "url": url}),
    )


def test_wait_returns_none_when_gh_list_fails() -> None:
    # gh errors (e.g. the repo has no CD workflow) -> do not block.
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=1, stdout="", stderr="oops"),
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None


def test_wait_returns_none_on_malformed_list_json() -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="not json"),
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None


def test_wait_returns_none_when_no_run_dispatched(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The CD workflow exists but no run matches this merge SHA within the
    # dispatch window (a change CD legitimately skips) -> warn, do not fail.
    with (
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".subprocess.run", return_value=_cd_list_resp([])),
    ):
        assert (
            _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT, dispatch_timeout=0.0)
            is None
        )
    assert "No CD workflow run registered" in capsys.readouterr().err


def test_wait_matches_run_by_sha_not_latest() -> None:
    # The most recent run on the branch is an unrelated FAILED run; the run
    # this merge triggered (matching headSha) succeeded. Must report success.
    unrelated = _cd_run(sha="deadbeef0000", run_id=999)
    mine = _cd_run(sha=_HEAD_SHA, run_id=555)
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([unrelated, mine]),
                _cd_view_resp("completed", "success"),
            ],
        ),
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None


def test_wait_fails_on_non_success_conclusion() -> None:
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run(run_id=12345)]),
                _cd_view_resp(
                    "completed",
                    "failure",
                    url="https://github.com/o/r/actions/runs/12345",
                ),
            ],
        ),
    ):
        problem = _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT)
    assert isinstance(problem, CDRunProblem)
    assert problem.timed_out is False
    assert problem.conclusion == "failure"
    msg = problem.describe()
    assert "12345" in msg
    assert "develop" in msg
    assert "abc123d" in msg  # short sha
    assert "ended with conclusion 'failure'" in msg
    assert "actions/runs/12345" in msg


def test_wait_treats_skipped_as_pass() -> None:
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run()]),
                _cd_view_resp("completed", "skipped"),
            ],
        ),
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None


def test_wait_polls_until_run_registers() -> None:
    # First list has no matching run (dispatch latency); second registers it;
    # then the run completes successfully.
    calls: list[CompletedProcess] = [
        _cd_list_resp([]),
        _cd_list_resp([_cd_run()]),
        _cd_view_resp("completed", "success"),
    ]
    with (
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".subprocess.run", side_effect=calls) as run,
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None
    assert run.call_count == 3


def test_wait_polls_run_until_completed() -> None:
    # The registered run is in progress on the first view, completed on the next.
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run()]),
                _cd_view_resp("in_progress", None),
                _cd_view_resp("completed", "success"),
            ],
        ),
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None


def test_wait_times_out_watching_run() -> None:
    # The run registers but never completes within the watch window.
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run()]),
                _cd_view_resp("in_progress", None),
            ],
        ),
    ):
        problem = _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT, watch_timeout=0.0)
    assert isinstance(problem, CDRunProblem)
    assert problem.timed_out is True
    assert problem.conclusion is None
    msg = problem.describe()
    assert "still in progress after 0s; result unknown" in msg
    assert "12345" in msg
    # A run still in progress is not a failed run (issue #3316).
    assert "conclusion" not in msg
    assert "did not succeed" not in msg


def _view_error(stderr: str) -> subprocess.CalledProcessError:
    """A failed ``gh run view``, as ``subprocess.run(check=True)`` raises it."""
    return subprocess.CalledProcessError(1, ["gh", "run", "view"], output="", stderr=stderr)


def test_wait_retries_a_transient_view_error() -> None:
    # A transient gh error while viewing the run is retried, with back-off.
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run()]),
                _view_error("HTTP 404: Not Found"),
                CompletedProcess(args=(), returncode=0, stdout="not json"),
                _cd_view_resp("completed", "success"),
            ],
        ),
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None


def test_wait_a_non_transient_view_error_is_a_read_failure_at_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A 403 is never retried to the deadline and reported as a run still in
    # progress: it is a read failure, naming the cause (#5174).
    with (
        patch(_MOD + ".time.sleep") as sleep,
        patch(
            _MOD + ".subprocess.run",
            side_effect=[_cd_list_resp([_cd_run()]), _view_error("HTTP 403: Forbidden")],
        ),
        pytest.raises(CDUnreadableError, match=r"(?s)could not be read.*403") as raised,
    ):
        _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT)
    assert "still in progress" not in str(raised.value)
    sleep.assert_not_called()
    err = capsys.readouterr().err
    assert "ERROR: CD workflow run 12345 (" in err
    assert "actions/runs/12345) for abc123d could not be read" in err


def test_wait_a_view_still_failing_at_the_deadline_is_a_read_failure() -> None:
    # A view that cannot be read when the wait runs out is a persistent read
    # failure, not a timeout (#5174).
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run()]),
                CompletedProcess(args=(), returncode=0, stdout="not json"),
            ],
        ),
        pytest.raises(CDUnreadableError, match="persistent read failure") as raised,
    ):
        _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT, watch_timeout=0.0)
    assert "still in progress" not in str(raised.value)


def test_wait_a_persistent_view_failure_is_a_read_failure() -> None:
    # More than MAX_READ_FAILURES failed reads in a row fail as a read
    # failure, well before the 90-minute deadline.
    bad = CompletedProcess(args=(), returncode=0, stdout="[]")
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[_cd_list_resp([_cd_run()]), *[bad] * (MAX_READ_FAILURES + 1)],
        ),
        pytest.raises(CDUnreadableError, match=r"read \d+ times in a row"),
    ):
        _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT)


def test_stage_cd_check_wait_cd_read_failure_is_a_tool_error(tmp_path: Path) -> None:
    # The stage fails as a tool error (exit 1, not develop broken), and the
    # final report repeats the cause.
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    error = CDUnreadableError("CD workflow run 12345 for abc123d could not be read: 403")
    with (
        patch(_MOD + "._wait_for_cd_run", side_effect=error),
        pytest.raises(CDUnreadableError),
    ):
        _stage_cd_check(ctx)
    assert ctx.cd_unreadable is True
    assert ctx.cd_line == f"CD: error ({error})"


def test_wait_handles_run_without_url() -> None:
    # A registered run missing its url must not crash the "waiting on" print.
    run_no_url = {"databaseId": 777, "headSha": _HEAD_SHA, "status": "queued"}
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([run_no_url]),
                CompletedProcess(
                    args=(),
                    returncode=0,
                    stdout=json.dumps(
                        {"status": "completed", "conclusion": "success", "attempt": 1}
                    ),
                ),
            ],
        ),
    ):
        assert _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT) is None


# -- cancelled CD runs: concurrency supersede vs. any other cancel (issue #3834) --

_RUN_URL = "https://github.com/o/r/actions/runs/12345"
_SUPERSEDE_MSG = "Canceling since a higher priority waiting request for docker-publish-dev exists"


def _job(job_id: int, conclusion: str) -> dict:
    return {"databaseId": job_id, "name": f"job {job_id}", "conclusion": conclusion}


def _annotations(*messages: str) -> list[dict]:
    return [
        {"annotation_level": "failure", "path": ".github", "start_line": 1, "message": m}
        for m in messages
    ]


class _FakeRunApi:
    """A ``github.read_json`` fake serving a run's jobs and each job's annotations."""

    def __init__(self, jobs: list[dict], annotations: dict[int, object]) -> None:
        self.jobs = jobs
        self.annotations = annotations
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> object:
        self.calls.append(args)
        if args[:2] == ("run", "view"):
            return {"jobs": self.jobs}
        if args[0] == "api":
            return self.annotations[int(args[1].split("/")[-2])]
        msg = f"unexpected gh call {args!r}"
        raise AssertionError(msg)


def test_supersede_group_reads_the_concurrency_annotation() -> None:
    # Shaped on vergil-containers run 40695987450: the docs and lint jobs pass,
    # release is skipped, every publish job is cancelled by the concurrency group.
    jobs = [_job(1, "success"), _job(2, "skipped"), _job(3, "cancelled"), _job(4, "cancelled")]
    fake = _FakeRunApi(
        jobs,
        {
            3: _annotations(_SUPERSEDE_MSG),
            4: _annotations(_SUPERSEDE_MSG, "The operation was canceled."),
        },
    )
    with patch(_MOD + ".github.read_json", side_effect=fake):
        assert _supersede_group(12345, _RUN_URL) == "docker-publish-dev"
    assert ("run", "view", "12345", "--repo", "o/r", "--json", "jobs") in fake.calls
    assert ("api", "repos/o/r/check-runs/3/annotations") in fake.calls
    assert ("api", "repos/o/r/check-runs/4/annotations") in fake.calls
    # Jobs that were not cancelled are not looked up.
    assert ("api", "repos/o/r/check-runs/1/annotations") not in fake.calls


def test_supersede_group_accepts_a_quoted_group() -> None:
    quoted = "Canceling since a higher priority waiting request for 'CD-refs/heads/develop' exists"
    fake = _FakeRunApi([_job(3, "cancelled")], {3: _annotations(quoted)})
    with patch(_MOD + ".github.read_json", side_effect=fake):
        assert _supersede_group(12345, _RUN_URL) == "CD-refs/heads/develop"


def test_supersede_group_none_for_a_human_cancel() -> None:
    fake = _FakeRunApi(
        [_job(3, "cancelled")],
        {3: _annotations("The run was canceled by @someone.", "The operation was canceled.")},
    )
    with patch(_MOD + ".github.read_json", side_effect=fake):
        assert _supersede_group(12345, _RUN_URL) is None


def test_supersede_group_none_when_any_cancelled_job_lacks_the_annotation() -> None:
    fake = _FakeRunApi(
        [_job(3, "cancelled"), _job(4, "cancelled")],
        {3: _annotations(_SUPERSEDE_MSG), 4: _annotations("The operation was canceled.")},
    )
    with patch(_MOD + ".github.read_json", side_effect=fake):
        assert _supersede_group(12345, _RUN_URL) is None


def test_supersede_group_none_when_another_job_failed() -> None:
    # A real failure alongside the supersede is still a failure.
    fake = _FakeRunApi(
        [_job(2, "failure"), _job(3, "cancelled")], {3: _annotations(_SUPERSEDE_MSG)}
    )
    with patch(_MOD + ".github.read_json", side_effect=fake):
        assert _supersede_group(12345, _RUN_URL) is None
    assert not any(call[0] == "api" for call in fake.calls)


def test_supersede_group_none_when_no_job_was_cancelled() -> None:
    # A run cancelled before any job started has no job to carry the annotation.
    fake = _FakeRunApi([], {})
    with patch(_MOD + ".github.read_json", side_effect=fake):
        assert _supersede_group(12345, _RUN_URL) is None


def test_supersede_group_rejects_an_unreadable_run_url() -> None:
    with pytest.raises(ValueError, match="cannot read the repository"):
        _supersede_group(12345, "")


def test_supersede_group_rejects_a_malformed_jobs_payload() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value={"jobs": "nope"}),
        pytest.raises(ValueError, match="jobs"),
    ):
        _supersede_group(12345, _RUN_URL)


def test_supersede_group_rejects_a_job_that_is_not_an_object() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value={"jobs": ["nope"]}),
        pytest.raises(ValueError, match="not an object"),
    ):
        _supersede_group(12345, _RUN_URL)


def test_supersede_group_rejects_a_malformed_annotations_payload() -> None:
    fake = _FakeRunApi([_job(3, "cancelled")], {3: {"message": _SUPERSEDE_MSG}})
    with (
        patch(_MOD + ".github.read_json", side_effect=fake),
        pytest.raises(ValueError, match="annotations"),
    ):
        _supersede_group(12345, _RUN_URL)


def _wait_with_conclusion(conclusion: str, *, group: object = None) -> object:
    """Run :func:`_wait_for_cd_run` to a completed run, faking the supersede lookup."""
    if isinstance(group, Exception):
        lookup = patch(_MOD + "._supersede_group", side_effect=group)
    else:
        lookup = patch(_MOD + "._supersede_group", return_value=group)
    with (
        patch(_MOD + ".time.sleep"),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run(run_id=12345)]),
                _cd_view_resp("completed", conclusion, url=_RUN_URL),
            ],
        ),
        lookup as supersede,
    ):
        result = _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT)
    if conclusion == "cancelled":
        supersede.assert_called_once_with(12345, _RUN_URL)
    else:
        supersede.assert_not_called()
    return result


def test_wait_outcome_success() -> None:
    assert _wait_with_conclusion("success") is None


def test_wait_outcome_failure() -> None:
    result = _wait_with_conclusion("failure")
    assert isinstance(result, CDRunProblem)
    assert result.conclusion == "failure"


def test_wait_outcome_superseded_is_not_a_problem() -> None:
    result = _wait_with_conclusion("cancelled", group="docker-publish-dev")
    assert isinstance(result, CDRunSuperseded)
    assert result.group == "docker-publish-dev"
    msg = result.describe()
    assert "CD workflow run 12345 on develop (abc123d)" in msg
    assert "superseded" in msg
    assert "docker-publish-dev" in msg
    assert _RUN_URL in msg


def test_wait_outcome_human_cancel_is_a_failure() -> None:
    result = _wait_with_conclusion("cancelled", group=None)
    assert isinstance(result, CDRunProblem)
    assert result.conclusion == "cancelled"
    msg = result.describe()
    assert "ended with conclusion 'cancelled'." in msg
    assert "could not tell" not in msg


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 502"),
        json.JSONDecodeError("bad", "x", 0),
        ValueError("cannot read the repository of CD run URL ''"),
    ],
)
def test_wait_outcome_unreadable_cancel_is_a_failure_that_says_why(error: Exception) -> None:
    result = _wait_with_conclusion("cancelled", group=error)
    assert isinstance(result, CDRunProblem)
    assert result.conclusion == "cancelled"
    msg = result.describe()
    assert "ended with conclusion 'cancelled'." in msg
    assert "could not tell whether a concurrency supersede cancelled it" in msg


def test_stage_cd_check_superseded_warns_and_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    outcome = CDRunSuperseded(12345, "develop", _HEAD_SHA, _RUN_URL, "docker-publish-dev")
    with (
        patch(_MOD + "._wait_for_cd_run", return_value=outcome),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
    ):
        _stage_cd_check(ctx)  # must not raise
    err = capsys.readouterr().err
    assert "WARNING: the CD workflow run triggered by this merge was superseded" in err
    assert _RUN_URL in err
    assert "docker-publish-dev" in err
    assert "not confirmed" in err
    assert "did not succeed" not in err
    mark_warn.assert_called_once()
    reason = mark_warn.call_args.args[0]
    assert "12345" in reason
    assert "superseded" in reason
    assert "docker-publish-dev" in reason


def test_main_returns_four_on_wait_cd_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value="deadbeefcafe"),
        patch(
            _MOD + "._wait_for_cd_run",
            return_value=CDRunProblem(999, "develop", "deadbeefcafe", "https://u", "failure", 5400),
        ),
        patch(_MOD + "._classify_failed_jobs", return_value=(["docker-publish"], [])),
    ):
        result = main(["--wait-cd"])
    assert result == 4  # with --wait-cd, a CD failure is 4
    assert "CD workflow" in capsys.readouterr().out


def test_main_skips_docs_check_on_dry_run(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(
            _MOD + "._wait_for_cd_run",
            return_value="should not appear",
        ) as mock_check,
    ):
        result = main(["--dry-run"])
    assert result == 0
    mock_check.assert_not_called()


# -- _worktree_is_dirty (issue #667) -------------------------------------------


def test_worktree_is_dirty_returns_true_when_status_output(tmp_path: Path) -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="M  foo.py\n"),
    ):
        assert _worktree_is_dirty(tmp_path) is True


def test_worktree_is_dirty_returns_false_when_clean(tmp_path: Path) -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout=""),
    ):
        assert _worktree_is_dirty(tmp_path) is False


def test_worktree_is_dirty_returns_true_on_git_failure(tmp_path: Path) -> None:
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=128, stdout=""),
    ):
        assert _worktree_is_dirty(tmp_path) is True


# -- worktree porcelain helper (lookup tests live in test_worktrees.py) ------


def _porcelain(*records: tuple[str, str | None]) -> str:
    """Build a `git worktree list --porcelain` output from (path, branch)
    tuples. branch=None means a detached worktree.
    """
    lines: list[str] = []
    for path, branch in records:
        lines.append(f"worktree {path}")
        lines.append("HEAD 0123456789abcdef0123456789abcdef01234567")
        if branch is None:
            lines.append("detached")
        else:
            lines.append(f"branch refs/heads/{branch}")
        lines.append("")
    return "\n".join(lines)


def test_main_removes_worktree_before_deleting_branch(tmp_path: Path) -> None:
    """Issue #315: when a merged branch is checked out in a `.worktrees/`
    worktree, finalize must `git worktree remove` it before
    `git branch -D` — otherwise -D refuses to delete a checked-out
    branch and the whole finalize crashes.
    """
    _make_profile(tmp_path, "library-release")
    wt_dir = tmp_path / ".worktrees" / "issue-99-x"
    wt_dir.mkdir(parents=True)

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/99-x"]),
        # Discovery itself is covered in test_worktrees.py; here the lookup
        # is patched directly (the autouse fixture stubs list_worktrees).
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=wt_dir.resolve()),
        patch(_MOD + "._worktree_is_dirty", return_value=False),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])

    assert result == 0
    remove_call = ("worktree", "remove", str(wt_dir.resolve()))
    delete_call = ("branch", "-D", "feature/99-x")
    assert remove_call in git_run_calls
    assert delete_call in git_run_calls
    assert git_run_calls.index(remove_call) < git_run_calls.index(delete_call)


def test_main_skips_worktree_remove_when_branch_not_in_worktree(tmp_path: Path) -> None:
    """If `_worktree_for_branch` returns None, no worktree-remove call
    fires — only `branch -D`. Pins the existing path for branches that
    aren't checked out anywhere.
    """
    _make_profile(tmp_path, "library-release")
    porcelain = _porcelain((str(tmp_path), "develop"))

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/99-x"]),
        patch(_MOD + ".git.read_output", return_value=porcelain),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])

    assert result == 0
    assert ("branch", "-D", "feature/99-x") in git_run_calls
    assert not any(c[:1] == ("worktree",) for c in git_run_calls)


def test_main_skips_dirty_worktree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #667: a worktree with uncommitted changes is skipped entirely
    — neither the worktree nor the branch is removed, and finalize
    continues with the remaining branches.
    """
    _make_profile(tmp_path, "library-release")
    wt_dir = tmp_path / ".worktrees" / "issue-42-wip"
    wt_dir.mkdir(parents=True)

    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/42-wip"]),
        # Discovery itself is covered in test_worktrees.py; here the lookup
        # is patched directly (the autouse fixture stubs list_worktrees).
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=wt_dir.resolve()),
        patch(_MOD + "._worktree_is_dirty", return_value=True),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])

    assert result == 0
    assert not any(c[0] == "worktree" for c in git_run_calls)
    assert not any(c == ("branch", "-D", "feature/42-wip") for c in git_run_calls)
    out = capsys.readouterr().out
    assert "Skipping feature/42-wip" in out
    assert "uncommitted changes" in out


def test_main_cleans_docker_cache_on_branch_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=["feature/x"]),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=(2, [])) as mock_clean,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0
    mock_clean.assert_called_once_with("feature/x", tmp_path)
    assert "Cleaned 2 cached container image(s)" in capsys.readouterr().out


# -- working-tree cleanliness gate (issue #472) -------------------------------


def test_main_fails_on_dirty_working_tree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #472: after cleanup, develop must be clean — any dirty state
    is a hard error so orphaned files don't silently accumulate.
    """
    _make_profile(tmp_path, "library-release")
    dirty_status = "?? orphan-spec.md\n?? stale-plan.md"
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.working_tree_status", return_value=dirty_status),
    ):
        result = main([])

    assert result == 1
    out = capsys.readouterr().out
    assert "working tree is not clean" in out
    assert "orphan-spec.md" in out
    assert "stale-plan.md" in out


def test_main_skips_dirty_check_on_dry_run(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.working_tree_status", return_value="?? should-not-fail.md"),
    ):
        result = main(["--dry-run"])
    assert result == 0


def test_no_pr_arg_is_cleanup_only(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.finalize_watch.watch") as mock_check,
        patch(f"{_MOD}.subprocess.run") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._report_cd_run"),
    ):
        result = main([])
    assert result == 0
    mock_check.assert_not_called()
    mock_merge.assert_not_called()


# -- PR inference and always-confirm (issue #1423) ----------------------------

_PR7 = _pr_ref(7, "Foo")
_PR8 = _pr_ref(8, "Bar")
_WT7 = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
_WT8 = Worktree(path=Path("/repo/.worktrees/issue-8-bar"), branch="feature/8-bar")


@contextmanager
def _cleanup_path_mocks(root: Path) -> Generator[None]:
    """Neutralize the post-merge cleanup path for inference-focused tests.

    Keeps main() off real git/config/gh calls after the part under test.
    The worktree-arm sweep (issue #1552) classifies each inferred worktree,
    so gather_worktree_status is stubbed to a non-removable verdict here —
    these tests cover inference, not cleanup deletion.
    """
    with (
        patch(_MOD + ".git.repo_root", return_value=root),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(
            _MOD + ".worktrees.gather_worktree_status",
            return_value=WorktreeStatus(
                worktree=Worktree(path=root, branch="x"),
                state=WorktreeState.OPEN_PR,
                pr_number=None,
                ahead=0,
                dirty=False,
            ),
        ),
    ):
        yield


def test_no_arg_single_candidate_confirms_and_finalizes(tmp_path: Path) -> None:
    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".worktrees.open_pr_for_branch", return_value=_PR7),
        patch(_MOD + ".confirm", return_value=True) as confirm,
        patch(_MOD + "._run_watched", return_value=0) as prov,
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    assert "PR #7" in confirm.call_args[0][0]
    assert prov.call_args[0][2] == ["https://github.com/o/r/pull/7"]


def test_yes_single_candidate_skips_tty_guard_and_finalizes(tmp_path: Path) -> None:
    """--yes pre-answers the single-PR confirm, so no interactive prompt is
    shown and the TTY guard is not required (issue #1644)."""
    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.require_tty") as guard,
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".worktrees.open_pr_for_branch", return_value=_PR7),
        patch(_MOD + "._run_watched", return_value=0) as prov,
    ):
        rc = main(["--yes", "--dry-run"])
    assert rc == 0
    guard.assert_not_called()
    assert prov.call_args[0][2] == ["https://github.com/o/r/pull/7"]


def test_yes_multiple_candidates_still_disambiguates(tmp_path: Path) -> None:
    """--yes never auto-picks among PRs: the disambiguation menu is still
    shown, and only the post-choice confirm is pre-answered (issue #1644)."""

    def _pr_for(branch: str) -> PRRef:
        return _PR7 if branch == "feature/7-foo" else _PR8

    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".worktrees.open_pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".prompt_choice", return_value="PR #8 (feature/8-bar): Bar") as menu,
        patch(_MOD + "._run_watched", return_value=0) as prov,
    ):
        rc = main(["--yes", "--dry-run"])
    assert rc == 0
    menu.assert_called_once()
    assert prov.call_args[0][2] == ["https://github.com/o/r/pull/8"]


def test_no_arg_single_candidate_decline_exits_zero_without_action() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".worktrees.open_pr_for_branch", return_value=_PR7),
        patch(_MOD + ".confirm", return_value=False),
        patch(_MOD + "._run_watched", return_value=0) as fin,
        patch(_MOD + ".git.current_branch") as branch,
    ):
        rc = main([])
    assert rc == 0
    fin.assert_not_called()
    branch.assert_not_called()  # cleanup never started


def test_no_arg_multiple_candidates_menu_then_confirm(tmp_path: Path) -> None:
    def _pr_for(branch: str) -> PRRef:
        return _PR7 if branch == "feature/7-foo" else _PR8

    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".worktrees.open_pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".prompt_choice", return_value="PR #8 (feature/8-bar): Bar") as menu,
        patch(_MOD + ".confirm", return_value=True),
        patch(_MOD + "._run_watched", return_value=0) as prov,
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    menu.assert_called_once()
    assert prov.call_args[0][2] == ["https://github.com/o/r/pull/8"]


def test_no_arg_no_candidates_confirms_cleanup_only() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".worktrees.open_pr_for_branch", return_value=None),
        patch(_MOD + ".confirm", return_value=False) as confirm,
    ):
        rc = main([])
    assert rc == 0
    assert "cleanup only" in confirm.call_args[0][0].lower()


def test_no_arg_excluded_worktree_prints_reason(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No silent exclusions: a worktree without an open PR says why it
    was skipped while another worktree proceeds."""

    def _pr_for(branch: str) -> PRRef | None:
        return _PR7 if branch == "feature/7-foo" else None

    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".worktrees.open_pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".confirm", return_value=True),
        patch(_MOD + "._run_watched", return_value=0),
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "issue-8-bar: no open PR for feature/8-bar" in out


def test_no_arg_non_tty_fails_fast_before_prompting() -> None:
    """The TTY guard fires before any prompt — interactivity is a
    requirement of inference mode, not an accident of menu count."""
    with (
        patch(
            _MOD + ".worktrees.require_tty",
            side_effect=SystemExit("requires an interactive terminal"),
        ),
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + "._run_watched", return_value=0) as fin,
        pytest.raises(SystemExit, match="interactive terminal"),
    ):
        main([])
    confirm.assert_not_called()
    fin.assert_not_called()


def test_explicit_pr_skips_inference_and_prompts(tmp_path: Path) -> None:
    # list_worktrees is now also called by the cleanup sweep (issue #1552),
    # so it is no longer an inference-only signal; the no-prompt + provenance
    # assertions prove inference was skipped. Empty list → worktree arm no-op.
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + "._run_watched", return_value=0) as fin,
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        rc = main(["123", "--dry-run"])
    assert rc == 0
    confirm.assert_not_called()
    fin.assert_called_once()


# -- --cleanup-only: non-interactive release path (issue #1448) ----------------


def test_parse_args_cleanup_only_defaults_false() -> None:
    assert parse_args([]).cleanup_only is False


def test_parse_args_cleanup_only_flag() -> None:
    assert parse_args(["--cleanup-only"]).cleanup_only is True


def test_cleanup_only_rejects_pr_argument(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PR argument and --cleanup-only contradict each other: the flag
    promises no merge and no prompts, the argument requests a merge."""
    with pytest.raises(SystemExit):
        parse_args(["123", "--cleanup-only"])
    err = capsys.readouterr().err
    assert "--cleanup-only" in err
    assert "not allowed with" in err


def test_cleanup_only_skips_inference_and_prompts(tmp_path: Path) -> None:
    """--cleanup-only is the scriptable release path: no TTY guard, no
    prompts, no merge — straight to cleanup. (The cleanup sweep itself
    enumerates worktrees; inference does not run.)"""
    with (
        patch(_MOD + ".worktrees.require_tty") as guard,
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + "._run_watched", return_value=0) as fin,
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
    ):
        rc = main(["--cleanup-only", "--dry-run"])
    assert rc == 0
    guard.assert_not_called()
    confirm.assert_not_called()
    fin.assert_not_called()


# -- --cleanup-only holds the checkout lock (epic vergil-project/.github#1218) --


def _lock_held(root: Path) -> bool:
    """True when this process holds *root*'s checkout lock (a probe acquire nests)."""
    try:
        with checkout_lock(root, purpose="probe"):
            return False
    except CheckoutLockError:
        return True


def test_cleanup_only_takes_lock(tmp_path: Path) -> None:
    """Cleanup, provision and validation run under the lock; cd-check does not."""
    held: dict[str, bool] = {}

    def record(name: str) -> Callable[[object], None]:
        return lambda _ctx: held.__setitem__(name, _lock_held(tmp_path))

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._stage_cleanup", side_effect=record("cleanup")),
        patch(_MOD + "._stage_provision", side_effect=record("provision")),
        patch(_MOD + "._stage_validation", side_effect=record("validation")),
        patch(_MOD + ".git.read_output", return_value="tip1234sha\n"),
        patch(
            _MOD + "._report_cd_run",
            side_effect=lambda *_a, **_k: held.__setitem__("cd-check", _lock_held(tmp_path)),
        ),
    ):
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert held == {"cleanup": True, "provision": True, "validation": True, "cd-check": False}
    assert not _lock_held(tmp_path)
    lock_file = tmp_path / "build" / ".vergil-state" / "checkout.lock"
    assert lock_file.read_text() == ""


def test_cleanup_only_releases_lock_when_cleanup_fails(tmp_path: Path) -> None:
    """A fail_fast cleanup stops the pipeline before cd-check; main still lets go."""
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._stage_cleanup", side_effect=FinalizeError("boom")),
    ):
        rc = main(["--cleanup-only"])
    assert rc == 1
    assert not _lock_held(tmp_path)


# -- engine swap + explicit-target cleanup (issue #1423) -----------------------


def test_explicit_target_cleanup_deletes_merged_pr_branch(tmp_path: Path) -> None:
    """After a squash merge, the just-merged branch is cleaned even though
    `git branch --merged` cannot see it."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".confirm") as confirm,
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),  # squash: sweep is blind
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        rc = main(["123"])
    assert rc == 0
    confirm.assert_not_called()  # explicit arg: no prompt
    assert ("branch", "-D", "feature/7-foo") in git_run_calls


def test_explicit_target_cleanup_respects_eternal_branches(tmp_path: Path) -> None:
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="develop"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._report_cd_run"),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls), (
        "must never delete an eternal branch"
    )


def test_explicit_target_cleanup_skips_missing_local_branch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A merged PR whose branch has no local counterpart is skipped loudly."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),  # no local branch
        patch(_MOD + "._report_cd_run"),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert "no local branch" in capsys.readouterr().out


def test_explicit_target_cleanup_skips_dirty_worktree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dirty worktree blocks the explicit-target deletion, loudly."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(
            _MOD + ".worktrees.worktree_for_branch",
            return_value=Path("/repo/.worktrees/issue-7-foo"),
        ),
        patch(_MOD + "._worktree_is_dirty", return_value=True),
        patch(_MOD + "._report_cd_run"),
    ):
        rc = main(["123"])
    assert rc == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert "uncommitted changes" in capsys.readouterr().out


# -- ancestry-sweep guards (issue #1445) ----------------------------------------


def test_sweep_skips_zero_commit_branch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #1445: a branch whose tip equals the target's tip is the
    starting state of a freshly created issue worktree — the sweep must
    not delete the branch or remove its worktree, and must not even need
    a gh lookup to decide."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/1439-fresh"]),
        patch(_MOD + ".git.branch_commit", return_value="same-sha"),
        patch(_MOD + ".worktrees.closed_pr_for_branch") as evidence,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])

    assert result == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert not any(c[:1] == ("worktree",) for c in git_run_calls)
    evidence.assert_not_called()  # guard 1 decides before the gh lookup
    out = capsys.readouterr().out
    assert "Skipping feature/1439-fresh" in out
    assert "zero-commit" in out


def test_sweep_skips_branch_without_merge_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #1445: an ancestor branch with no closed/merged PR may be a
    fresh branch created off an older target tip — without merge
    evidence the sweep leaves the branch and its worktree alone."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/77-stale-tip"]),
        patch(_MOD + ".git.branch_commit", side_effect=lambda ref: f"sha-of-{ref}"),
        patch(_MOD + ".worktrees.closed_pr_for_branch", return_value=None),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])

    assert result == 0
    assert not any(c[:2] == ("branch", "-D") for c in git_run_calls)
    assert not any(c[:1] == ("worktree",) for c in git_run_calls)
    out = capsys.readouterr().out
    assert "Skipping feature/77-stale-tip" in out
    assert "no closed or merged PR" in out


def test_sweep_deletes_straggler_with_commits_and_merge_evidence(tmp_path: Path) -> None:
    """A genuine straggler — own commits plus a closed/merged PR — is
    still swept; the guards relax the cleanup, they don't disable it."""
    _make_profile(tmp_path, "library-release")
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=["feature/55-straggler"]),
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])

    assert result == 0
    assert ("branch", "-D", "feature/55-straggler") in git_run_calls


def test_explicit_target_cleanup_bypasses_sweep_guards(tmp_path: Path) -> None:
    """Issue #1445: the guards gate the ancestry sweep only — the
    just-merged PR branch keeps its existing explicit-target cleanup,
    which already has PR evidence by construction."""
    git_run_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(_MOD + ".git.branch_commit") as sha,
        patch(_MOD + ".worktrees.closed_pr_for_branch") as evidence,
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        rc = main(["123"])
    assert rc == 0
    assert ("branch", "-D", "feature/7-foo") in git_run_calls
    sha.assert_not_called()
    evidence.assert_not_called()


# -- stage functions (issue #1479) ---------------------------------------------


def _stage_ctx(argv: list[str], root: Path | None = None) -> FinalizeContext:
    """A settle-phase context; a PR argument in *argv* is a PR the watch saw merge."""
    args = parse_args(argv)
    merged = _parse_pr_list(args.pr) if args.pr else []
    return FinalizeContext(args=args, root=root or Path("/repo"), merged_prs=merged)


def test_close_refs_handles_every_merged_pr_then_fails_naming_each() -> None:
    """One PR's failed read does not stop the next PR's closes (spec §6.3 step 1)."""
    ctx = _stage_ctx(["123,124"])
    err = GitHubAPIError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
    open_refs = _refs(_issue(7, "OPEN"))
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", side_effect=[err, open_refs, open_refs]),
        patch(_MOD + ".github.close_issue") as close,
        patch(_MOD + ".time.sleep"),
        pytest.raises(FinalizeError, match="PR 123") as raised,
    ):
        _stage_close_refs(ctx)
    close.assert_called_once()
    assert "124" not in str(raised.value)


# -- close-refs: close what GitHub failed to auto-close (issue #4002) ----------


def _refs(
    *issues: ClosingIssue, state: str = "MERGED", uncounted: tuple[ClosingIssue, ...] = ()
) -> ClosingRefs:
    return ClosingRefs(
        repo="o/r",
        number=123,
        state=state,
        merged_at="2026-11-08T12:00:00Z",
        counted=issues,
        uncounted=uncounted,
        skipped=(),
    )


def _issue(number: int, state: str, repo: str = "o/r") -> ClosingIssue:
    return ClosingIssue(repo=repo, number=number, state=state)


def test_close_refs_closes_issue_github_left_open(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _stage_ctx(["123"])
    open_refs = _refs(_issue(7, "OPEN"), uncounted=(_issue(8, "CLOSED", repo="o/other"),))
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", return_value=open_refs) as read,
        patch(_MOD + ".github.close_issue") as close,
        patch(_MOD + ".time.sleep") as sleep,
    ):
        _stage_close_refs(ctx)
    # One grace re-read before acting, so an auto-close in flight is not raced.
    sleep.assert_called_once()
    assert read.call_count == 2
    close.assert_called_once_with(
        "o/r",
        7,
        comment=(
            "Completed by PR o/r#123 (merged 2026-11-08T12:00:00Z); GitHub did not auto-close it."
        ),
    )
    out = capsys.readouterr().out
    assert "o/r#7: GitHub left it open after the merge; closed it as completed" in out
    assert "o/other#8: closed" in out


def test_close_refs_leaves_issue_auto_closed_during_grace() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(
            _MOD + ".closing_refs.read_closing_refs",
            side_effect=[_refs(_issue(7, "OPEN")), _refs(_issue(7, "CLOSED"))],
        ),
        patch(_MOD + ".github.close_issue") as close,
        patch(_MOD + ".time.sleep"),
    ):
        _stage_close_refs(ctx)
    close.assert_not_called()


def test_close_refs_all_closed_does_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", return_value=_refs(_issue(7, "CLOSED"))),
        patch(_MOD + ".github.close_issue") as close,
        patch(_MOD + ".time.sleep") as sleep,
    ):
        _stage_close_refs(ctx)
    close.assert_not_called()
    sleep.assert_not_called()
    assert "o/r#7: closed" in capsys.readouterr().out


def test_close_refs_none_listed(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", return_value=_refs()),
        patch(_MOD + ".github.close_issue") as close,
    ):
        _stage_close_refs(ctx)
    close.assert_not_called()
    assert "PR o/r#123 has no closing issue references." in capsys.readouterr().out


def test_close_refs_read_failure_is_loud() -> None:
    ctx = _stage_ctx(["123"])
    err = GitHubAPIError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", side_effect=err),
        patch(_MOD + ".github.close_issue") as close,
        pytest.raises(
            FinalizeError,
            match=r"(?s)could not read the closing issue references of PR 123:.*HTTP 502",
        ),
    ):
        _stage_close_refs(ctx)
    close.assert_not_called()


def test_close_refs_not_merged_refuses() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(
            _MOD + ".closing_refs.read_closing_refs",
            return_value=_refs(_issue(7, "OPEN"), state="OPEN"),
        ),
        patch(_MOD + ".github.close_issue") as close,
        pytest.raises(FinalizeError, match="not MERGED"),
    ):
        _stage_close_refs(ctx)
    close.assert_not_called()


def test_close_refs_close_failure_tries_every_issue_then_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _stage_ctx(["123"])
    refs = _refs(_issue(7, "OPEN"), _issue(9, "OPEN"))
    err = GitHubAPIError(1, ("gh", "issue", "close"), stderr="HTTP 403")
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", return_value=refs),
        patch(_MOD + ".github.close_issue", side_effect=[err, None]) as close,
        patch(_MOD + ".time.sleep"),
        pytest.raises(FinalizeError, match=r"could not close o/r#7, left open"),
    ):
        _stage_close_refs(ctx)
    assert close.call_count == 2
    captured = capsys.readouterr()
    assert "ERROR: could not close o/r#7:" in captured.err
    assert "HTTP 403" in captured.err
    assert "o/r#9: GitHub left it open after the merge; closed it as completed" in captured.out


def test_close_refs_dry_run_reads_and_closes_nothing() -> None:
    ctx = _stage_ctx(["123", "--dry-run"])
    with (
        patch(_MOD + ".closing_refs.read_closing_refs") as read,
        patch(_MOD + ".github.close_issue") as close,
    ):
        _stage_close_refs(ctx)
    read.assert_not_called()
    close.assert_not_called()


# -- close-refs through the trust layer (GHSA-g68j-q2mw-jqf9) -------------------

_REAL_READ_CLOSING_REFS = closing_refs.read_closing_refs


@contextmanager
def _layer(*answers: dict[str, object]) -> Generator[MagicMock]:
    """The real closing-references read, its GraphQL answered by *answers* in turn."""
    graphql = MagicMock(side_effect=list(answers))
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", _REAL_READ_CLOSING_REFS),
        patch(_MOD + ".github.pr_repo_and_number", return_value=("o/r", 123)),
        patch("vergil_tooling.lib.github.graphql", graphql),
        patch(_MOD + ".time.sleep"),
    ):
        yield graphql


def test_close_refs_closes_only_trusted_references(capsys: pytest.CaptureFixture[str]) -> None:
    """A reference an outsider linked by hand is reported and left open."""
    answer = trust_stubs.closing_pr(
        "o/r",
        123,
        ("o/r", 7, "OPEN"),
        ("o/r", 8, "OPEN"),
        body="Closes #7",
        links=((trust_stubs.OUTSIDER, "o/r", 8),),
    )
    ctx = _stage_ctx(["123"])
    with _layer(answer, answer), patch(_MOD + ".github.close_issue") as close:
        _stage_close_refs(ctx)
    close.assert_called_once()
    assert close.call_args.args == ("o/r", 7)
    captured = capsys.readouterr()
    assert "o/r#8: left open: not a closing reference of PR o/r#123" in captured.out
    assert "skipped untrusted content: o/r#123 event drive-by" in captured.err


def test_close_refs_untrusted_body_closes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    """An outsider's edit of the body leaves every reference it names open."""
    answer = trust_stubs.closing_pr("o/r", 123, ("o/r", 7, "OPEN"), editor=trust_stubs.OUTSIDER)
    ctx = _stage_ctx(["123"])
    with _layer(answer) as graphql, patch(_MOD + ".github.close_issue") as close:
        _stage_close_refs(ctx)
    close.assert_not_called()
    # Nothing to close, so no grace re-read.
    assert graphql.call_count == 1
    captured = capsys.readouterr()
    assert "o/r#7: left open" in captured.out
    assert "skipped untrusted content: o/r#123 edit drive-by" in captured.err
    assert "Closes" not in captured.err


def test_close_refs_reports_counts_under_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    answer = trust_stubs.closing_pr("o/r", 123, ("o/r", 7, "OPEN"), author=trust_stubs.OUTSIDER)
    with _layer(answer), patch(_MOD + ".github.close_issue") as close:
        _stage_close_refs(_stage_ctx(["123"]))
    close.assert_not_called()
    err = capsys.readouterr().err
    assert "skipped 1 untrusted item(s) (untrusted: 1)" in err
    assert "drive-by" not in err


def test_close_refs_other_repo_reference_left_open(capsys: pytest.CaptureFixture[str]) -> None:
    answer = trust_stubs.closing_pr("o/r", 123, ("o/other", 7, "OPEN"))
    with _layer(answer), patch(_MOD + ".github.close_issue") as close:
        _stage_close_refs(_stage_ctx(["123"]))
    close.assert_not_called()
    assert "o/other#7: left open" in capsys.readouterr().out


def test_close_refs_trust_read_failure_is_one_line() -> None:
    """A closing list the layer cannot read in full fails the PR, one line, nothing closed."""
    answer = trust_stubs.closing_pr("o/r", 123, ("o/r", 7, "OPEN"))
    refs = answer["repository"]["pullRequest"]["closingIssuesReferences"]
    refs["totalCount"] = 200
    with (
        _layer(answer),
        patch(_MOD + ".github.close_issue") as close,
        pytest.raises(FinalizeError) as raised,
    ):
        _stage_close_refs(_stage_ctx(["123"]))
    close.assert_not_called()
    message = str(raised.value)
    assert "\n" not in message
    assert message.startswith("could not read the closing issue references of PR 123:")
    assert "truncated" in message


def test_close_refs_trusted_authors_unconfigured_is_one_line() -> None:
    err = TrustedAuthorsNotConfiguredError("no trusted authors configured")
    with (
        patch(_MOD + ".load_trusted_authors", side_effect=err),
        patch(_MOD + ".closing_refs.read_closing_refs", _REAL_READ_CLOSING_REFS),
        patch(_MOD + ".github.close_issue") as close,
        pytest.raises(FinalizeError, match="PR 123: no trusted authors configured$"),
    ):
        _stage_close_refs(_stage_ctx(["123"]))
    close.assert_not_called()


def test_close_refs_failure_still_cleans_up_and_exits_one(tmp_path: Path) -> None:
    """A close failure fails finalize, but the merged PR is still cleaned up."""
    err = GitHubAPIError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
    with (
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(f"{_MOD}.closing_refs.read_closing_refs", side_effect=err),
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop") as branch,
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value=""),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._report_cd_run"),
    ):
        result = main(["42"])
    assert result == 1
    branch.assert_called()  # cleanup ran despite the close-refs failure


def test_build_stages_after_a_merge() -> None:
    names = [s.name for s in build_stages(close_refs=True)]
    assert names == [
        "close-refs",
        "advisory-sync",
        "checkout-lock",
        "cleanup",
        "provision",
        "validation",
        "cd-check",
    ]


def test_build_stages_cleanup_only() -> None:
    names = [s.name for s in build_stages(close_refs=False)]
    assert names == ["checkout-lock", "cleanup", "provision", "validation", "cd-check"]


def test_build_stages_failure_modes() -> None:
    modes = {s.name: s.mode for s in build_stages(close_refs=True)}
    # An issue left open fails finalize, but the merged PRs still settle
    # (issue #4002).
    assert modes["close-refs"] == "fail_defer"
    # A failed advisory sync fails finalize the same way (vergil-tooling#4583).
    assert modes["advisory-sync"] == "fail_defer"
    assert modes["checkout-lock"] == "fail_fast"
    assert modes["cleanup"] == "fail_fast"
    # Provisioning is a prerequisite: fail_fast so validation never runs against
    # a broken or absent image (issue #2462).
    assert modes["provision"] == "fail_fast"
    # fail_defer keeps a validation failure from stopping the pipeline before
    # cd-check runs and surfaces the CD status.
    assert modes["validation"] == "fail_defer"
    # cd-check is fail_fast: with --wait-cd, a CD run that did not succeed is a
    # hard finalize failure (issue #2753).
    assert modes["cd-check"] == "fail_fast"


# -- advisory-sync: sync the advisories of the closed issues (vergil-tooling#4583)

_GHSA = "GHSA-abcd-2345-wxyz"
_I7 = IssueRef("o", "r", 7)
_I8 = IssueRef("o", "r", 8)


def test_close_refs_records_every_closed_counted_issue() -> None:
    """Auto-closed and finalize-closed issues both reach the sync; uncounted ones do not."""
    ctx = _stage_ctx(["123"])
    refs = _refs(
        _issue(7, "CLOSED"), _issue(8, "OPEN"), uncounted=(_issue(9, "CLOSED", repo="o/x"),)
    )
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", return_value=refs),
        patch(_MOD + ".github.close_issue"),
        patch(_MOD + ".time.sleep"),
    ):
        _stage_close_refs(ctx)
    assert ctx.closed_issues == [_I7, _I8]


def test_close_refs_does_not_record_an_issue_it_failed_to_close() -> None:
    ctx = _stage_ctx(["123"])
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
    with (
        patch(_MOD + ".closing_refs.read_closing_refs", return_value=_refs(_issue(8, "OPEN"))),
        patch(_MOD + ".github.close_issue", side_effect=err),
        patch(_MOD + ".time.sleep"),
        pytest.raises(FinalizeError),
    ):
        _stage_close_refs(ctx)
    assert ctx.closed_issues == []


def test_advisory_sync_reports_each_state_it_set(
    _no_advisory_sync: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["123"])
    ctx.closed_issues.extend([_I7, _I8])
    _no_advisory_sync.return_value = security_records.IssueSyncResult(synced=[(_GHSA, "closed")])
    _stage_advisory_sync(ctx)
    _no_advisory_sync.assert_called_once_with([_I7, _I8])
    assert f"  {_GHSA}: set to closed" in capsys.readouterr().out


def test_advisory_sync_with_no_closed_issue_calls_nothing(
    _no_advisory_sync: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _stage_advisory_sync(_stage_ctx(["123"]))
    _no_advisory_sync.assert_not_called()
    assert "No closed issue to sync advisories for." in capsys.readouterr().out


def test_advisory_sync_says_when_nothing_changed(
    _no_advisory_sync: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["123"])
    ctx.closed_issues.append(_I7)
    _stage_advisory_sync(ctx)
    assert "No advisory state to sync." in capsys.readouterr().out


def test_advisory_sync_dry_run_syncs_nothing(_no_advisory_sync: MagicMock) -> None:
    ctx = _stage_ctx(["123", "--dry-run"])
    ctx.closed_issues.append(_I7)
    _stage_advisory_sync(ctx)
    _no_advisory_sync.assert_not_called()


def test_advisory_sync_held_is_a_warning_naming_the_next_step(
    _no_advisory_sync: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["123"])
    ctx.closed_issues.append(_I7)
    _no_advisory_sync.return_value = security_records.IssueSyncResult(held=[(_GHSA, (_I7,))])
    with patch(_MOD + ".progress.mark_warn") as warn:
        _stage_advisory_sync(ctx)
    warn.assert_called_once()
    assert _GHSA in warn.call_args.args[0]
    assert "vrg-security-record close" in warn.call_args.args[0]
    assert f"- held: {_GHSA} (o/r#7)" in capsys.readouterr().out


def test_advisory_sync_problem_fails_the_stage_after_the_rest(
    _no_advisory_sync: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["123"])
    ctx.closed_issues.append(_I7)
    problem = f"o/r#7: names {_GHSA}, which does not track it"
    _no_advisory_sync.return_value = security_records.IssueSyncResult(
        synced=[("GHSA-zzzz-9999-aaaa", "closed")], problems=[problem]
    )
    with pytest.raises(FinalizeError, match="vrg-security-record sync") as raised:
        _stage_advisory_sync(ctx)
    assert problem in str(raised.value)
    captured = capsys.readouterr()
    assert "GHSA-zzzz-9999-aaaa: set to closed" in captured.out
    assert f"ERROR: advisory sync: {problem}" in captured.err


@pytest.mark.parametrize(
    "error",
    [
        GitHubAPIError(1, ("gh", "api"), stderr="HTTP 403"),
        TrustReadError("read_bodies o/r#7…#7", "a missing field"),
        TrustedAuthorsNotConfiguredError("no trusted authors"),
        github.NoInstallationError("o", []),
    ],
)
def test_advisory_sync_failure_fails_the_stage_loudly(
    _no_advisory_sync: MagicMock, error: Exception
) -> None:
    ctx = _stage_ctx(["123"])
    ctx.closed_issues.append(_I7)
    _no_advisory_sync.side_effect = error
    with pytest.raises(FinalizeError, match="the advisory sync failed: .*vrg-security-record sync"):
        _stage_advisory_sync(ctx)


def test_advisory_sync_failure_still_cleans_up_and_exits_one(
    tmp_path: Path, _no_advisory_sync: MagicMock
) -> None:
    """A failed sync fails finalize, but the merged PR is still cleaned up."""
    _no_advisory_sync.side_effect = GitHubAPIError(1, ("gh", "api"), stderr="HTTP 403")
    with (
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(
            f"{_MOD}.closing_refs.read_closing_refs",
            return_value=_refs(_issue(7, "CLOSED")),
        ),
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop") as branch,
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value=""),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}._report_cd_run"),
    ):
        result = main(["42"])
    assert result == 1
    _no_advisory_sync.assert_called_once_with([_I7])
    branch.assert_called()  # cleanup ran despite the advisory-sync failure


def test_stage_provision_warms_image(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(
            _MOD + ".provision_dev_image", return_value=("img--develop--abcd1234", "cached")
        ) as prov,
    ):
        _stage_provision(ctx)
    # quiet_warmup=True: this stage runs under the live progress display, so the
    # warmup is captured (surfaced on failure), not streamed raw (#2906).
    prov.assert_called_once_with(tmp_path, "python", quiet_warmup=True)


def test_stage_provision_reports_env_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".provision_dev_image", return_value=("custom:img", "env")),
    ):
        _stage_provision(ctx)
    assert "DOCKER_DEV_IMAGE override" in capsys.readouterr().out


def test_stage_provision_failure_raises(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".provision_dev_image", side_effect=RuntimeError("Cache build failed")),
        pytest.raises(FinalizeError, match="dev-image provisioning failed"),
    ):
        _stage_provision(ctx)


def test_stage_provision_broken_package_json_raises(tmp_path: Path) -> None:
    # A repo with no declared language and a broken package.json fails the stage
    # with the parse message, before any provisioning.
    (tmp_path / "package.json").write_text("{not json")
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(_MOD + ".provision_dev_image") as prov,
        pytest.raises(FinalizeError, match=r"provisioning failed: package\.json at .* cannot be"),
    ):
        _stage_provision(ctx)
    prov.assert_not_called()


def test_stage_validation_reprovision_broken_package_json_raises(tmp_path: Path) -> None:
    # The launch-failure rebuild re-reads the language; a broken package.json
    # fails the stage with the parse message instead of a traceback.
    (tmp_path / "package.json").write_text("{not json")
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(_MOD + ".progress.run", side_effect=[_launch_err()]),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".provision_dev_image") as prov,
        pytest.raises(FinalizeError, match=r"provisioning failed: package\.json at .* cannot be"),
    ):
        _stage_validation(ctx)
    prov.assert_not_called()


def test_stage_provision_dry_run_skips(tmp_path: Path) -> None:
    ctx = _stage_ctx(["--dry-run"], root=tmp_path)
    with patch(_MOD + ".provision_dev_image") as prov:
        _stage_provision(ctx)
    prov.assert_not_called()


def test_stage_validation_streams_through_progress(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + ".progress.run", return_value=0) as run:
        _stage_validation(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-container-run", "--", "vrg-validate")


def test_stage_validation_uses_uv_for_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + ".progress.run", return_value=0) as run:
        _stage_validation(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-container-run", "--", "uv", "run", "vrg-validate")


def test_stage_validation_failure_raises(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    err = subprocess.CalledProcessError(1, ("vrg-container-run",))
    with (
        patch(_MOD + ".progress.run", side_effect=err),
        pytest.raises(FinalizeError, match="validation failed"),
    ):
        _stage_validation(ctx)


def _launch_err(
    returncode: int = 125,
    stderr: str = "docker: parent snapshot sha256:x does not exist: not found",
) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        returncode, ("vrg-container-run",), output="", stderr=stderr
    )


def test_stage_validation_launch_failure_reprovisions_and_retries(tmp_path: Path) -> None:
    # Exit 125 means the container never started — validation did not run, so it
    # is not a develop regression. Rebuild the dev image and retry once (#3016).
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(_MOD + ".progress.run", side_effect=[_launch_err(), 0]) as run,
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])) as clean,
        patch(_MOD + ".provision_dev_image", return_value=("img", "cached")) as prov,
    ):
        _stage_validation(ctx)  # must not raise
    assert run.call_count == 2
    clean.assert_called_once_with("develop", tmp_path)
    prov.assert_called_once()


def test_stage_validation_snapshot_signature_without_125_reprovisions(tmp_path: Path) -> None:
    # A runtime that reports the orphaned-snapshot failure with a non-125 exit is
    # still a launch failure, classified from its diagnostic text.
    ctx = _stage_ctx([], root=tmp_path)
    sig_err = subprocess.CalledProcessError(
        1, ("vrg-container-run",), output="", stderr="failed to prepare extraction snapshot"
    )
    with (
        patch(_MOD + ".progress.run", side_effect=[sig_err, 0]) as run,
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".provision_dev_image", return_value=("img", "cached")),
    ):
        _stage_validation(ctx)  # must not raise
    assert run.call_count == 2


def test_stage_validation_launch_failure_persists_raises_distinct_error(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    snapshot = "parent snapshot sha256:d798 does not exist: not found"
    with (
        patch(
            _MOD + ".progress.run",
            side_effect=[_launch_err(), _launch_err(stderr=snapshot)],
        ),
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".provision_dev_image", return_value=("img", "cached")),
        pytest.raises(FinalizeError) as excinfo,
    ):
        _stage_validation(ctx)
    msg = str(excinfo.value)
    assert "failed to launch" in msg
    assert "fix develop" not in msg  # not misattributed to a develop regression
    assert snapshot in msg  # surfaces the runtime stderr


def test_stage_validation_reprovision_then_real_failure_raises_fix_branch(tmp_path: Path) -> None:
    # If the retry reaches a genuine validation failure (the container launched
    # this time), report it as a develop problem, not a launch failure.
    ctx = _stage_ctx([], root=tmp_path)
    real_failure = subprocess.CalledProcessError(
        1, ("vrg-container-run",), output="", stderr="lint failed"
    )
    with (
        patch(_MOD + ".progress.run", side_effect=[_launch_err(), real_failure]),
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".provision_dev_image", return_value=("img", "cached")),
        pytest.raises(FinalizeError, match="fix develop"),
    ):
        _stage_validation(ctx)


def test_stage_validation_refused_rebuild_fails_as_unknown(tmp_path: Path) -> None:
    # The low-disk guard refusing the rebuild (#3571) means validation never ran:
    # an unknown result, not a pass (#3316), so the stage fails with a readable
    # message naming the image, the cause and the retry command, and validation
    # is not retried against an image that was never rebuilt (#3605).
    ctx = _stage_ctx([], root=tmp_path)
    refused = CacheSpaceError("only 1.2G free after pruning; the build needs 4.0G")
    with (
        patch(_MOD + ".progress.run", side_effect=[_launch_err()]) as run,
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".provision_dev_image", side_effect=refused),
        pytest.raises(FinalizeError) as excinfo,
    ):
        _stage_validation(ctx)
    assert run.call_count == 1
    msg = str(excinfo.value)
    assert msg.startswith("post-finalization validation did not run; result unknown")
    assert "could not rebuild the develop dev image" in msg
    assert "only 1.2G free after pruning; the build needs 4.0G" in msg
    assert "retry with: vrg-container-run -- vrg-validate" in msg
    assert "fix develop" not in msg  # not misattributed to a develop regression
    assert excinfo.value.__cause__ is refused


def test_stage_validation_failed_rebuild_keeps_full_error_text(tmp_path: Path) -> None:
    # A generic cache-build failure carries the captured build output on later
    # lines; none of it may be dropped from the failure.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    ctx = _stage_ctx([], root=tmp_path)
    failed = RuntimeError("Cache build failed\nE: Unable to locate package nope")
    with (
        patch(_MOD + ".progress.run", side_effect=[_launch_err()]) as run,
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".provision_dev_image", side_effect=failed),
        pytest.raises(FinalizeError) as excinfo,
    ):
        _stage_validation(ctx)
    assert run.call_count == 1
    msg = str(excinfo.value)
    assert "Cache build failed\nE: Unable to locate package nope" in msg
    assert "retry with: vrg-container-run -- uv run vrg-validate" in msg


@pytest.mark.parametrize(
    ("exc", "cause"),
    [
        (
            subprocess.CalledProcessError(
                1, ("docker", "commit"), stderr="no space left on device"
            ),
            "no space left on device",
        ),
        (VersionsError("no published version window"), "no published version window"),
    ],
)
def test_stage_validation_other_rebuild_failures_fail(
    tmp_path: Path, exc: Exception, cause: str
) -> None:
    # The cache build commits the image with check=True, and the version
    # resolver raises VersionsError: both are rebuild failures, reported the same.
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(_MOD + ".progress.run", side_effect=[_launch_err()]) as run,
        patch(_MOD + ".resolve_language", return_value="python"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".provision_dev_image", side_effect=exc),
        pytest.raises(FinalizeError, match="did not run; result unknown") as excinfo,
    ):
        _stage_validation(ctx)
    assert run.call_count == 1
    assert cause in str(excinfo.value)


def test_stage_validation_dry_run_skips(tmp_path: Path) -> None:
    ctx = _stage_ctx(["--dry-run"], root=tmp_path)
    with patch(_MOD + ".progress.run") as run:
        _stage_validation(ctx)
    run.assert_not_called()


def test_cd_reported_not_awaited_by_default(tmp_path: Path) -> None:
    # Spec §6.5 (epic vergil-project/.github#1218): with no flag the stage
    # reports the head's CD run and never waits on it.
    ctx = _stage_ctx([], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    with (
        patch(_MOD + "._report_cd_run") as report,
        patch(_MOD + "._wait_for_cd_run") as wait,
    ):
        _stage_cd_check(ctx)  # must not raise
    report.assert_called_once_with("develop", _HEAD_SHA, root=tmp_path)
    wait.assert_not_called()


def test_wait_cd_blocks(tmp_path: Path) -> None:
    # --wait-cd gives the blocking watch on the head's run, not the report.
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    with (
        patch(_MOD + "._report_cd_run") as report,
        patch(_MOD + "._wait_for_cd_run", return_value=None) as wait,
    ):
        _stage_cd_check(ctx)  # must not raise
    wait.assert_called_once_with("develop", _HEAD_SHA, root=tmp_path)
    report.assert_not_called()


def test_stage_cd_check_raises_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    problem = CDRunProblem(999, "develop", _HEAD_SHA, "https://u/999", "failure", 5400)
    with (
        patch(_MOD + "._wait_for_cd_run", return_value=problem),
        patch(
            _MOD + "._classify_failed_jobs", return_value=(["docker-publish"], ["docs"])
        ) as classify,
        pytest.raises(
            FinalizeError, match="^CD workflow run triggered by the merge did not succeed$"
        ),
    ):
        _stage_cd_check(ctx)
    classify.assert_called_once_with(999)
    err = capsys.readouterr().err
    assert "ERROR: the CD workflow run triggered by this merge did not succeed." in err
    assert "CD workflow run 999 on develop (abc123d) ended with conclusion 'failure'." in err
    assert "https://u/999" in err
    assert "Failed job(s): docker-publish" in err
    assert "investigate the run above" in err
    assert "result unknown" not in err


def test_wait_cd_docs_only_failure_warns_and_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # classify_jobs defers a docs job's failure, as the release gate does: a
    # failure only of docs jobs is a loud warning (stage ⚠), not a stop.
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    problem = CDRunProblem(999, "develop", _HEAD_SHA, "https://u/999", "failure", 5400)
    with (
        patch(_MOD + "._wait_for_cd_run", return_value=problem),
        patch(_MOD + "._classify_failed_jobs", return_value=([], ["docs"])),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
    ):
        _stage_cd_check(ctx)  # must not raise
    err = capsys.readouterr().err
    assert "WARNING: CD run 999 failed only in deferred publish job(s): docs." in err
    assert "https://u/999" in err
    assert "did not succeed" not in err
    mark_warn.assert_called_once()
    assert "docs" in mark_warn.call_args.args[0]


def test_wait_cd_unreadable_jobs_still_fails_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    problem = CDRunProblem(999, "develop", _HEAD_SHA, "https://u/999", "failure", 5400)
    with (
        patch(_MOD + "._wait_for_cd_run", return_value=problem),
        patch(
            _MOD + "._classify_failed_jobs",
            side_effect=subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
        ),
        pytest.raises(FinalizeError, match="did not succeed"),
    ):
        _stage_cd_check(ctx)
    err = capsys.readouterr().err
    assert "Could not read the jobs of CD run 999 to classify the failure" in err
    assert "HTTP 502" in err


def test_wait_cd_cancelled_run_is_not_classified(tmp_path: Path) -> None:
    # A cancel that was not a supersede fails as before; its jobs are not
    # judged, since a cancelled run's jobs did not fail.
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    problem = CDRunProblem(999, "develop", _HEAD_SHA, "https://u/999", "cancelled", 5400)
    with (
        patch(_MOD + "._wait_for_cd_run", return_value=problem),
        patch(_MOD + "._classify_failed_jobs") as classify,
        pytest.raises(FinalizeError, match="did not succeed"),
    ):
        _stage_cd_check(ctx)
    classify.assert_not_called()


def test_classify_failed_jobs_splits_with_classify_jobs() -> None:
    jobs = [
        {"name": "docker-publish / build", "status": "completed", "conclusion": "failure"},
        {"name": "docs / docs", "status": "completed", "conclusion": "failure"},
        {"name": "test", "status": "completed", "conclusion": "success"},
    ]
    with patch(_MOD + ".github.read_json", return_value={"jobs": jobs}) as read:
        assert _classify_failed_jobs(999) == (["docker-publish"], ["docs"])
    read.assert_called_once_with("run", "view", "999", "--json", "jobs")


def test_classify_failed_jobs_rejects_payload_without_jobs() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value={"nope": 1}),
        pytest.raises(ValueError, match="no jobs list"),
    ):
        _classify_failed_jobs(999)


def test_stage_cd_check_timeout_is_reported_as_unknown_not_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A watch timeout gets its own headline (issue #3316): still in progress,
    # result unknown, with the URL and how to re-check — and it still fails
    # the stage, because an unknown result is not a pass.
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    problem = CDRunProblem(38973347564, "develop", _HEAD_SHA, "https://u/389", None, 5400)
    with (
        patch(_MOD + "._wait_for_cd_run", return_value=problem),
        pytest.raises(
            FinalizeError, match="^CD run still in progress after 5400s; result unknown$"
        ),
    ):
        _stage_cd_check(ctx)
    err = capsys.readouterr().err
    assert "ERROR: CD run still in progress after 5400s; result unknown." in err
    assert "https://u/389" in err
    assert "this is not a CD failure" in err
    assert "gh run view 38973347564" in err
    assert "vrg-finalize-pr --cleanup-only" in err
    assert "VRG_CD_TIMEOUT=<seconds> (currently 5400s)" in err
    assert "did not succeed" not in err
    assert "investigate the run above" not in err


# -- CD watch deadline: default + $VRG_CD_TIMEOUT (issue #3316) ---------------


def test_cd_timeout_default_covers_longest_known_cd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VRG_CD_TIMEOUT", raising=False)
    assert cd_timeout_secs() == 5400
    # vergil-containers run 38973347564 took 43m31s; the default must clear it.
    assert cd_timeout_secs() > 43 * 60 + 31


def test_cd_timeout_blank_override_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_CD_TIMEOUT", "  ")
    assert cd_timeout_secs() == 5400


def test_cd_timeout_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_CD_TIMEOUT", " 7200 ")
    assert cd_timeout_secs() == 7200


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5", "90m"])
def test_cd_timeout_invalid_override_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("VRG_CD_TIMEOUT", raw)
    with pytest.raises(ValueError, match="VRG_CD_TIMEOUT must be a positive whole number"):
        cd_timeout_secs()


def test_wait_uses_cd_timeout_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit watch_timeout: the deadline comes from $VRG_CD_TIMEOUT and
    # is reported back on a timeout.
    monkeypatch.setenv("VRG_CD_TIMEOUT", "1")
    clock = iter([0.0, 0.0, 0.5, 2.0])
    with (
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".time.monotonic", side_effect=lambda: next(clock)),
        patch(
            _MOD + ".subprocess.run",
            side_effect=[
                _cd_list_resp([_cd_run()]),
                _cd_view_resp("in_progress", None),
                _cd_view_resp("in_progress", None),
            ],
        ),
    ):
        problem = _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT)
    assert isinstance(problem, CDRunProblem)
    assert problem.timed_out is True
    assert problem.waited_secs == 1


def test_wait_invalid_cd_timeout_fails_before_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_CD_TIMEOUT", "soon")
    with (
        patch(_MOD + ".subprocess.run") as run,
        pytest.raises(ValueError, match="VRG_CD_TIMEOUT"),
    ):
        _wait_for_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT)
    run.assert_not_called()


def test_stage_cd_check_dry_run_skips(tmp_path: Path) -> None:
    ctx = _stage_ctx(["--dry-run"], root=tmp_path)
    with (
        patch(_MOD + "._wait_for_cd_run") as check,
        patch(_MOD + "._report_cd_run") as report,
    ):
        _stage_cd_check(ctx)
    check.assert_not_called()
    report.assert_not_called()


def test_no_wait_cd_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    # --no-wait-cd was removed (spec §6.10): exit 1 with the exact message,
    # before any git call or stage.
    with (
        patch(_MOD + ".git.is_main_worktree") as is_main,
        patch(_MOD + ".progress.run_pipeline") as pipeline,
    ):
        assert main(["--cleanup-only", "--no-wait-cd"]) == 1
    is_main.assert_not_called()
    pipeline.assert_not_called()
    assert capsys.readouterr().err == (
        "vrg-finalize-pr: --no-wait-cd was removed; not waiting on CD is now the "
        "default (use --wait-cd to wait)\n"
    )


def _report(runs: list[dict], *, supersede: object = None) -> tuple[MagicMock, MagicMock]:
    """Run :func:`_report_cd_run` against a ``gh run list`` of *runs*.

    *supersede* is what the supersede lookup returns, or raises if an exception.
    """
    group = supersede if isinstance(supersede, Exception) else (lambda *_a: supersede)
    with (
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".subprocess.run", return_value=_cd_list_resp(runs)) as run,
        patch(_MOD + "._supersede_group", side_effect=group),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
    ):
        _report_cd_run("develop", _HEAD_SHA, root=_NO_WORKFLOWS_ROOT, dispatch_timeout=0.0)
    return run, mark_warn


def _completed(conclusion: str) -> dict:
    return {**_cd_run(status="completed"), "conclusion": conclusion}


@pytest.mark.parametrize(
    ("run", "state"),
    [
        (_cd_run(status="queued"), "queued"),
        (_cd_run(status="pending"), "queued"),
        (_cd_run(status="in_progress"), "in progress"),
        (_completed("success"), "succeeded"),
        (_completed("skipped"), "succeeded"),
    ],
)
def test_cd_report_prints_state_and_url_without_waiting(
    run: dict, state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    gh, mark_warn = _report([run])
    assert capsys.readouterr().out == f"  CD: {state} https://github.com/o/r/actions/runs/12345\n"
    # One listing, no view and no watch: the report never waits on the run.
    gh.assert_called_once()
    assert list(gh.call_args.args[0][:3]) == ["gh", "run", "list"]
    mark_warn.assert_not_called()


@pytest.mark.parametrize(
    ("conclusion", "supersede", "state"),
    [
        ("failure", None, "failed"),
        ("cancelled", None, "cancelled"),
        ("cancelled", "docker-publish-dev", "superseded"),
    ],
)
def test_cd_report_names_a_state_that_is_not_green(
    conclusion: str, supersede: object, state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _, mark_warn = _report([_completed(conclusion)], supersede=supersede)
    assert capsys.readouterr().out == f"  CD: {state} https://github.com/o/r/actions/runs/12345\n"
    mark_warn.assert_called_once_with(f"CD run 12345 {state} for {_HEAD_SHA[:7]}")


def test_cd_report_unreadable_cancel_says_why(capsys: pytest.CaptureFixture[str]) -> None:
    _, mark_warn = _report(
        [_completed("cancelled")],
        supersede=subprocess.CalledProcessError(1, ["gh", "api"], stderr="HTTP 502"),
    )
    captured = capsys.readouterr()
    assert "CD: cancelled https://github.com/o/r/actions/runs/12345" in captured.out
    assert "could not tell whether a concurrency supersede cancelled it" in captured.err
    assert "HTTP 502" in captured.err
    mark_warn.assert_called_once()


def test_no_cd_run_reported(capsys: pytest.CaptureFixture[str]) -> None:
    _, mark_warn = _report([_cd_run(sha="otherfeed1234")])
    assert capsys.readouterr().out == "  CD: no CD run for this head\n"
    mark_warn.assert_not_called()


def _failed_listing() -> CompletedProcess:
    return CompletedProcess(args=(), returncode=1, stdout="", stderr="oops")


def test_cd_report_lookup_failure_is_an_error_naming_the_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The repo declares a CD workflow, so a failed listing is a real failure:
    # an error naming its cause, never a CD state reported as unknown (#5140).
    with (
        patch(_MOD + ".subprocess.run", return_value=_failed_listing()),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
        pytest.raises(CDUnreadableError, match="oops") as raised,
    ):
        _report_cd_run("develop", _HEAD_SHA, root=_cd_root(tmp_path), dispatch_timeout=0.0)
    assert f"the CD runs for {_HEAD_SHA[:7]} could not be listed" in str(raised.value)
    captured = capsys.readouterr()
    assert "ERROR: the CD runs for" in captured.err
    assert "oops" in captured.err
    assert "unknown" not in captured.err
    assert "no CD workflow" not in captured.out
    mark_warn.assert_not_called()


def test_cd_report_without_credentials_names_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Agent mode whose App token cannot be minted: the cause is the error.
    error = github.AppAuthError("o", "12345", Path("app.pem"), "the key was rejected")

    def no_token(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(github, "get_installation_token", no_token)
    with (
        patch(_MOD + ".subprocess.run", side_effect=AssertionError("gh must not run")),
        pytest.raises(CDUnreadableError, match="no credentials to list the CD runs") as raised,
    ):
        _report_cd_run("develop", _HEAD_SHA, root=_cd_root(tmp_path), dispatch_timeout=0.0)
    assert "the key was rejected" in str(raised.value)


def test_cd_report_lists_runs_with_the_agents_app_token(
    tmp_path: Path, fake_app: FakeApp, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_app.cwd_org = "cwd-org"
    with patch(_MOD + ".subprocess.run", return_value=_cd_list_resp([_cd_run()])) as gh:
        _report_cd_run("develop", _HEAD_SHA, root=_cd_root(tmp_path), dispatch_timeout=0.0)
    assert gh.call_args.kwargs["env"]["GH_TOKEN"] == "ghs_fake_cwd-org"
    assert "CD: queued" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["--wait-cd"]])
def test_stage_cd_check_lookup_failure_fails_the_stage(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(argv, root=_cd_root(tmp_path))
    ctx.synced_to = _HEAD_SHA
    with (
        patch(_MOD + ".subprocess.run", return_value=_failed_listing()),
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
        pytest.raises(CDUnreadableError, match="oops"),
    ):
        _stage_cd_check(ctx)
    assert ctx.cd_line.startswith(f"CD: error (the CD runs for {_HEAD_SHA[:7]} could not be")
    assert "oops" in ctx.cd_line
    assert ctx.cd_unreadable
    assert "ERROR: the CD runs for" in capsys.readouterr().err
    mark_warn.assert_not_called()


def test_cd_report_no_cd_workflow_passes_quietly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A repo whose workflows declare no CD (vergil-project/.github, say):
    # one plain line, no warning, as before the default became a report.
    _write_workflow(tmp_path, "ci.yml", "name: CI\non: push\njobs: {}\n")
    with (
        patch(_MOD + ".subprocess.run", return_value=_failed_listing()),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
    ):
        _report_cd_run("develop", _HEAD_SHA, root=tmp_path, dispatch_timeout=0.0)
    captured = capsys.readouterr()
    assert captured.out == "  CD: no CD workflow in this repo\n"
    assert captured.err == ""
    mark_warn.assert_not_called()


def test_wait_cd_no_cd_workflow_reports_and_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx(["--wait-cd"], root=tmp_path)
    ctx.synced_to = _HEAD_SHA
    with (
        patch(_MOD + ".subprocess.run", return_value=_failed_listing()),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
    ):
        _stage_cd_check(ctx)  # must not raise
    captured = capsys.readouterr()
    assert captured.out == "  CD: no CD workflow in this repo\n"
    assert captured.err == ""
    mark_warn.assert_not_called()


def test_wait_cd_lookup_failure_with_cd_workflow_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(_MOD + ".subprocess.run", return_value=_failed_listing()),
        patch(_MOD + ".progress.mark_warn") as mark_warn,
        pytest.raises(CDUnreadableError, match="oops"),
    ):
        _wait_for_cd_run("develop", _HEAD_SHA, root=_cd_root(tmp_path))
    assert "ERROR: the CD runs for" in capsys.readouterr().err
    mark_warn.assert_not_called()


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({}, False),
        ({"ci.yml": "name: CI\n"}, False),
        ({"ci.yml": "name: CI\n", "release.yaml": "name: CD\non: push\n"}, True),
        ({"cd.yml": "name: 'CD'\n"}, True),
        ({"notes.txt": "name: CD\n"}, False),
        ({"broken.yml": "name: [unclosed\n"}, None),
        ({"broken.yml": "name: [unclosed\n", "cd.yml": "name: CD\n"}, True),
    ],
)
def test_declares_cd_workflow(tmp_path: Path, files: dict[str, str], expected: bool | None) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    for name, text in files.items():
        _write_workflow(tmp_path, name, text)
    assert _declares_cd_workflow(tmp_path) is expected


def test_declares_cd_workflow_without_workflows_dir(tmp_path: Path) -> None:
    assert _declares_cd_workflow(tmp_path) is False


def test_cd_report_unparsable_workflow_keeps_the_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A workflow that cannot be parsed might be the CD one: the failed
    # listing is not explained away, so it stays an error naming its cause.
    _write_workflow(tmp_path, "broken.yml", "name: [unclosed\n")
    with (
        patch(_MOD + ".subprocess.run", return_value=_failed_listing()),
        pytest.raises(CDUnreadableError, match="oops"),
    ):
        _report_cd_run("develop", _HEAD_SHA, root=tmp_path, dispatch_timeout=0.0)
    assert "ERROR: the CD runs for" in capsys.readouterr().err


def test_stage_cd_check_falls_back_to_target_tip(tmp_path: Path) -> None:
    # cleanup-only path: no merge this invocation, so wait on the CD run for
    # the current tip of the target branch.
    ctx = _stage_ctx([], root=tmp_path)  # synced_to stays empty
    with (
        patch(_MOD + ".git.read_output", return_value="tip1234sha\n") as read,
        patch(_MOD + "._report_cd_run") as wait,
    ):
        _stage_cd_check(ctx)
    read.assert_called_once_with(
        "rev-parse", "--verify", "--end-of-options", "refs/heads/develop^{commit}"
    )
    wait.assert_called_once_with("develop", "tip1234sha", root=tmp_path)


def test_stage_cd_check_warns_when_tip_unresolvable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If the target tip cannot be resolved, warn and skip the wait rather than
    # crash finalize after cleanup already succeeded — and do not call the wait.
    ctx = _stage_ctx([], root=tmp_path)  # synced_to stays empty
    err = subprocess.CalledProcessError(128, ("git", "rev-parse", "develop"))
    with (
        patch(_MOD + ".git.read_output", side_effect=err),
        patch(_MOD + "._report_cd_run") as wait,
    ):
        _stage_cd_check(ctx)  # must not raise
    wait.assert_not_called()
    assert "could not resolve the tip" in capsys.readouterr().err


# -- squash-merge-aware straggler sweep (issue #1552) ------------------------


def test_sweep_removes_squash_merged_worktree_branch(tmp_path: Path) -> None:
    """A squash-merged branch is invisible to `git branch --merged`, so the
    worktree arm must sweep it via its classify_worktree removable verdict."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.MERGED, pr_number=1, ahead=2, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=removable),
        patch(_MOD + "._delete_branch_and_worktree", return_value=True) as deleter,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_any_call("feature/1-x", tmp_path, dry_run=False, reason="PR merged", guard=ANY)


def test_sweep_keeps_open_pr_worktree(tmp_path: Path) -> None:
    """Race-safety (issue #1445): a worktree whose PR is still open is not
    removable, so the worktree arm never deletes live work. The same branch
    also appears in `git branch --merged`, exercising the ancestry arm's
    dedup against worktree branches (issue #1552)."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-2-y", branch="feature/2-y")
    not_removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.OPEN_PR, pr_number=2, ahead=2, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=["feature/2-y"]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=not_removable),
        patch(_MOD + ".worktrees.closed_pr_for_branch") as ancestry_evidence,
        patch(_MOD + "._delete_branch_and_worktree") as deleter,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_not_called()
    # The ancestry arm skipped feature/2-y as a worktree branch before
    # reaching the merge-evidence guard.
    ancestry_evidence.assert_not_called()


def test_sweep_worktree_delete_decline_does_not_record(tmp_path: Path) -> None:
    """If _delete_branch_and_worktree declines (e.g. the tree went dirty
    between classification and deletion), the branch is not recorded as
    deleted and the sweep continues without error."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-3-z", branch="feature/3-z")
    removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.MERGED, pr_number=3, ahead=1, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=removable),
        patch(_MOD + "._delete_branch_and_worktree", return_value=False) as deleter,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_called_once_with(
        "feature/3-z", tmp_path, dry_run=False, reason="PR merged", guard=ANY
    )


def test_sweep_keeps_reused_branch_name_worktree(tmp_path: Path) -> None:
    """Issue #1719: a branch name reused after a same-named PR merged keeps a
    worktree whose tip carries unmerged commits. Driving the REAL
    gather_worktree_status (only its git/github primitives are stubbed)
    proves the worktree arm withholds the merged verdict and never deletes
    the in-progress branch."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(
        path=tmp_path / ".worktrees" / "issue-286-build-buckets",
        branch="feature/286-build-buckets",
    )
    _wt_mod = "vergil_tooling.lib.worktrees"
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        # The reused branch tip is NOT an ancestor of develop, so the
        # ancestry arm never lists it — only the worktree arm can see it.
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        # Real gather_worktree_status: stub only its primitives.
        patch(_wt_mod + ".git.commits_ahead", return_value=9),
        patch(_wt_mod + ".git.read_output", return_value=""),
        patch(_wt_mod + ".git.branch_commit", return_value="7ead128"),
        patch(
            _wt_mod + ".branch_prs",
            return_value=[_pr_ref(293, "docs", state="MERGED", head_oid="0ldd0cs")],
        ),
        patch(_MOD + "._delete_branch_and_worktree") as deleter,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_not_called()


def test_sweep_skips_eternal_branch_worktree(tmp_path: Path) -> None:
    """A worktree checked out on an eternal branch is never classified or
    swept — the eternal guard short-circuits before gather_worktree_status."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "eternal", branch="develop")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status") as gather,
        patch(_MOD + "._delete_branch_and_worktree") as deleter,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    gather.assert_not_called()
    deleter.assert_not_called()


# -- cleanup safety while a release is running (issue #5187) ------------------


_RELEASE_OPEN = worktrees.ReleaseProtection(
    active=True, reason="open tracking issue: release 2.1.9 (#77)"
)
_NO_RELEASE = worktrees.ReleaseProtection(active=False, reason="no release tracking issue is open")
_NO_LOCK = worktrees.ReleaseProtection(active=False, reason="no release lock is held")


@contextmanager
def _sweep_one_worktree(
    tmp_path: Path, wt: Worktree, *, protection: object
) -> Generator[list[tuple[str, ...]]]:
    """Drive a cleanup-only run whose one worktree, *wt*, is merged and removable.

    The real ``_delete_branch_and_worktree`` runs; ``git.run`` is captured and
    yielded. *protection* is what ``worktrees.release_protection`` returns, or
    the exception it raises.
    """
    removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.MERGED, pr_number=1, ahead=2, dirty=False
    )
    calls: list[tuple[str, ...]] = []
    if isinstance(protection, BaseException):
        guard = patch(_MOD + ".worktrees.release_protection", side_effect=protection)
    else:
        guard = patch(_MOD + ".worktrees.release_protection", return_value=protection)
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=lambda *a: calls.append(a)),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=removable),
        patch(_MOD + "._worktree_is_dirty", return_value=False),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + "._report_cd_run"),
        guard,
    ):
        yield calls


def _release_worktree(tmp_path: Path) -> Worktree:
    return Worktree(path=tmp_path / ".worktrees" / "release-2.1.9", branch="release/2.1.9")


def _run_log_text(root: Path) -> str:
    """The text of the one vrg-finalize-pr run log under *root*."""
    (log,) = sorted((root / ".vergil").glob("vrg-finalize-pr-*.log"))
    return log.read_text(encoding="utf-8")


def test_cleanup_skips_release_worktree_while_release_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merged release/* worktree is kept while a release tracking issue is open."""
    _make_profile(tmp_path, "library-release")
    wt = _release_worktree(tmp_path)
    with _sweep_one_worktree(tmp_path, wt, protection=_RELEASE_OPEN) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert not any(c[:1] == ("worktree",) for c in calls)
    assert ("branch", "-D", "release/2.1.9") not in calls
    out = capsys.readouterr().out
    assert "kept .worktrees/release-2.1.9: a release is running" in out
    assert "release 2.1.9 (#77)" in out


def test_cleanup_removes_release_worktree_when_no_release_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no release running, a merged release/* worktree is removed and logged."""
    _make_profile(tmp_path, "library-release")
    wt = _release_worktree(tmp_path)
    with _sweep_one_worktree(tmp_path, wt, protection=_NO_RELEASE) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert ("worktree", "remove", str(wt.path)) in calls
    assert ("branch", "-D", "release/2.1.9") in calls
    line = f"removed worktree {wt.path} (branch release/2.1.9): PR merged"
    assert line in capsys.readouterr().out
    assert line in _run_log_text(tmp_path)


def test_cleanup_treats_failed_protection_read_as_protected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed protection read keeps every release/* worktree, says why and exits non-zero."""
    _make_profile(tmp_path, "library-release")
    wt = _release_worktree(tmp_path)
    failure = worktrees.ProtectionReadError("could not read the open release tracking issues")
    with _sweep_one_worktree(tmp_path, wt, protection=failure) as calls:
        rc = main(["--cleanup-only"])
    assert rc != 0
    assert not any(c[:1] == ("worktree",) for c in calls)
    assert ("branch", "-D", "release/2.1.9") not in calls
    out = capsys.readouterr().out
    assert "could not read the open release tracking issues" in out
    assert "kept .worktrees/release-2.1.9" in out


def test_cleanup_reads_only_the_lock_for_a_feature_worktree(tmp_path: Path) -> None:
    """A feature worktree's removal reads the lock, never the tracking issues."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    with (
        _sweep_one_worktree(tmp_path, wt, protection=_RELEASE_OPEN) as calls,
        patch(_MOD + ".worktrees.release_protection") as read,
        patch(_MOD + ".worktrees.lock_protection", return_value=_NO_LOCK) as lock,
    ):
        rc = main(["--cleanup-only"])
    assert rc == 0
    read.assert_not_called()
    lock.assert_called_once_with("o/r")
    assert ("worktree", "remove", str(wt.path)) in calls


def test_every_removal_is_logged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A merged feature worktree's removal is logged, with its reason, on the
    terminal and in the run log."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    with _sweep_one_worktree(tmp_path, wt, protection=_NO_RELEASE):
        rc = main(["--cleanup-only"])
    assert rc == 0
    line = f"removed worktree {wt.path} (branch feature/1-x): PR merged"
    assert line in capsys.readouterr().out
    assert _run_log_text(tmp_path).count(line) == 1


def test_release_guard_reads_the_protection_once_per_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two release worktrees, one read: the protection is judged once per run."""
    guard = ReleaseGuard()
    with (
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".worktrees.release_protection", return_value=_RELEASE_OPEN) as read,
    ):
        first = guard.keeps("release/2.1.9", tmp_path / ".worktrees" / "release-2.1.9", tmp_path)
        second = guard.keeps("release/post-2.1.9", Path("/elsewhere/post"), tmp_path)
    assert first is True
    assert second is True
    read.assert_called_once_with("o/r", lock=_NO_LOCK)
    out = capsys.readouterr().out
    assert "kept /elsewhere/post: a release is running" in out


def test_release_guard_treats_an_unresolvable_repo_as_protected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A repo that cannot be named on GitHub is a failed read: the worktree is kept."""
    guard = ReleaseGuard()
    failure = subprocess.CalledProcessError(1, ("gh", "repo", "view"))
    with (
        patch(_MOD + ".github.current_repo", side_effect=failure),
        patch(_MOD + ".worktrees.release_protection") as read,
    ):
        kept = guard.keeps("release/2.1.9", tmp_path / ".worktrees" / "release-2.1.9", tmp_path)
    assert kept is True
    read.assert_not_called()
    assert guard.error is not None
    assert guard.error.startswith("could not resolve this repository on GitHub")
    assert "kept .worktrees/release-2.1.9" in capsys.readouterr().err


def test_clean_dirty_keeps_release_worktree_while_release_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--clean-dirty never clears a release/* worktree while a release is running,
    and asks nothing about it."""
    _make_profile(tmp_path, "library-release")
    wt = _release_worktree(tmp_path)
    status = WorktreeStatus(
        worktree=wt,
        state=WorktreeState.MERGED,
        pr_number=5,
        ahead=0,
        dirty=True,
        detail="worktree is dirty",
        needs_attention=True,
    )
    with (
        _sweep_with_stuck(tmp_path, status) as calls,
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".worktrees.release_protection", return_value=_RELEASE_OPEN),
        patch(_MOD + ".confirm") as confirm,
    ):
        rc = main(["--cleanup-only", "--clean-dirty"])
    assert rc == 0
    confirm.assert_not_called()
    assert not any(c[:1] == ("worktree",) for c in calls)
    assert "kept .worktrees/release-2.1.9: a release is running" in capsys.readouterr().out


def test_clean_dirty_failed_protection_read_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed protection read during --clean-dirty keeps the worktree and fails the run."""
    _make_profile(tmp_path, "library-release")
    wt = _release_worktree(tmp_path)
    status = WorktreeStatus(
        worktree=wt,
        state=WorktreeState.MERGED,
        pr_number=5,
        ahead=0,
        dirty=True,
        detail="worktree is dirty",
        needs_attention=True,
    )
    failure = worktrees.ProtectionReadError("could not read the open release tracking issues")
    with (
        _sweep_with_stuck(tmp_path, status) as calls,
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".worktrees.release_protection", side_effect=failure),
        patch(_MOD + ".confirm") as confirm,
    ):
        rc = main(["--cleanup-only", "--clean-dirty"])
    assert rc != 0
    confirm.assert_not_called()
    assert not any(c[:1] == ("worktree",) for c in calls)
    assert "could not read the open release tracking issues" in capsys.readouterr().err


def test_clean_dirty_removal_is_logged_in_the_run_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The post-pipeline --clean-dirty removal still reaches the run log."""
    _make_profile(tmp_path, "library-release")
    status = _stuck_status(tmp_path)
    with (
        _sweep_with_stuck(tmp_path, status),
        patch(_MOD + "._worktree_porcelain", return_value="?? out.log\n"),
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = main(["--cleanup-only", "--clean-dirty"])
    assert rc == 0
    line = (
        f"removed worktree {status.worktree.path} (branch feature/9-x): "
        "PR merged, its dirt untracked only (--clean-dirty)"
    )
    assert line in capsys.readouterr().err
    assert _run_log_text(tmp_path).count(line) == 1


# -- surface + guarded clear of stuck merged worktrees (issue #2348) ----------


def _stuck_status(
    tmp_path: Path,
    *,
    branch: str = "feature/9-x",
    state: WorktreeState = WorktreeState.MERGED,
    detail: str | None = "merged PR #9 but worktree is dirty — 1 uncommitted path(s): out.log.",
) -> WorktreeStatus:
    """A merged/closed-but-stuck worktree status: dirty, not removable, and
    flagged needs_attention (what gather_worktree_status returns for the
    build-turd case, issue #2347)."""
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-9-x", branch=branch)
    return WorktreeStatus(
        worktree=wt,
        state=state,
        pr_number=9,
        ahead=1,
        dirty=True,
        detail=detail,
        needs_attention=True,
    )


@contextmanager
def _sweep_with_stuck(tmp_path: Path, status: WorktreeStatus) -> Generator[list[tuple[str, ...]]]:
    """Drive a cleanup-only run whose single worktree classifies as *status*.

    Yields the captured git.run call list so a test can assert on (or against)
    the deletion commands. gather_worktree_status is stubbed to *status*, so
    the worktree arm records it as stuck (issue #2348).
    """
    calls: list[tuple[str, ...]] = []
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=lambda *a: calls.append(a)),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[status.worktree]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=status),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
    ):
        yield calls


def test_parse_args_clean_dirty_defaults_false() -> None:
    assert parse_args([]).clean_dirty is False


def test_parse_args_clean_dirty_flag() -> None:
    assert parse_args(["--clean-dirty"]).clean_dirty is True


def test_dirt_is_untracked_only_true_for_all_untracked() -> None:
    assert _dirt_is_untracked_only("?? out.log\n?? .coverage\n") is True


def test_dirt_is_untracked_only_false_for_modified_tracked() -> None:
    assert _dirt_is_untracked_only(" M src/app.py\n") is False
    assert _dirt_is_untracked_only("M  src/app.py\n") is False
    assert _dirt_is_untracked_only("A  new.py\n") is False


def test_dirt_is_untracked_only_false_when_mixed() -> None:
    assert _dirt_is_untracked_only("?? out.log\n M src/app.py\n") is False


def test_dirt_is_untracked_only_false_when_empty() -> None:
    assert _dirt_is_untracked_only("") is False
    assert _dirt_is_untracked_only("\n  \n") is False


def test_stuck_merged_worktree_is_surfaced_not_buried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A merged-but-dirty worktree the sweep leaves behind is reported
    prominently after the pipeline, with its reason — not just in the
    collapsed stage log (issue #2348)."""
    _make_profile(tmp_path, "library-release")
    status = _stuck_status(tmp_path)
    with _sweep_with_stuck(tmp_path, status) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    # Default (no --clean-dirty): never deletes.
    assert not any(c[:1] == ("worktree",) for c in calls)
    assert not any(c[:2] == ("branch", "-D") for c in calls)
    out = capsys.readouterr().out
    assert "needs attention" in out
    assert "feature/9-x" in out
    assert "worktree is dirty" in out
    assert "--clean-dirty" in out  # points at the opt-in remedy


def test_clean_dirty_clears_untracked_only_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--clean-dirty force-removes a merged worktree dirtied only by untracked
    build output, after confirmation."""
    _make_profile(tmp_path, "library-release")
    status = _stuck_status(tmp_path)
    with (
        _sweep_with_stuck(tmp_path, status) as calls,
        patch(_MOD + "._worktree_porcelain", return_value="?? out.log\n?? .coverage\n"),
        patch(_MOD + ".confirm", return_value=True) as confirm,
    ):
        rc = main(["--cleanup-only", "--clean-dirty"])
    assert rc == 0
    confirm.assert_called_once()
    assert ("worktree", "remove", "--force", str(status.worktree.path)) in calls
    assert ("branch", "-D", "feature/9-x") in calls
    out = capsys.readouterr().out
    assert "untracked: out.log" in out
    # Cleared → not re-surfaced as needing attention.
    assert "needs attention" not in out


def test_clean_dirty_refuses_modified_tracked_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--clean-dirty never discards modified tracked files: the worktree is
    refused (no deletion) and surfaced as still needing attention."""
    _make_profile(tmp_path, "library-release")
    status = _stuck_status(tmp_path)
    with (
        _sweep_with_stuck(tmp_path, status) as calls,
        patch(_MOD + "._worktree_porcelain", return_value=" M src/app.py\n?? out.log\n"),
        patch(_MOD + ".confirm") as confirm,
    ):
        rc = main(["--cleanup-only", "--clean-dirty"])
    assert rc == 0
    confirm.assert_not_called()
    assert not any(c[:1] == ("worktree",) for c in calls)
    assert not any(c[:2] == ("branch", "-D") for c in calls)
    out = capsys.readouterr().out
    assert "refusing feature/9-x" in out
    assert "modified tracked files" in out
    assert "needs attention" in out


def test_clean_dirty_refuses_reused_branch_straggler(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #1719: a reused branch name classifies as NO_PR (its merged
    verdict withheld). --clean-dirty must never clear it — its current commits
    are unmerged — so the porcelain is not even inspected."""
    _make_profile(tmp_path, "library-release")
    status = _stuck_status(
        tmp_path,
        branch="feature/286-reused",
        state=WorktreeState.NO_PR,
        detail="closed PR #293 head 0ldd0cs does not match branch tip — reused (issue #1719)",
    )
    with (
        _sweep_with_stuck(tmp_path, status) as calls,
        patch(_MOD + "._worktree_porcelain") as porcelain,
        patch(_MOD + ".confirm") as confirm,
    ):
        rc = main(["--cleanup-only", "--clean-dirty"])
    assert rc == 0
    porcelain.assert_not_called()  # state guard decides before reading dirt
    confirm.assert_not_called()
    assert not any(c[:1] == ("worktree",) for c in calls)
    assert not any(c[:2] == ("branch", "-D") for c in calls)
    out = capsys.readouterr().out
    assert "refusing feature/286-reused" in out
    assert "reused" in out


def test_clean_dirty_decline_leaves_worktree_and_surfaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Declining the confirmation leaves the worktree untouched and still
    surfaced as needing attention. This status carries no detail, exercising
    the fallback description in the surfaced report."""
    _make_profile(tmp_path, "library-release")
    status = _stuck_status(tmp_path, detail=None)
    with (
        _sweep_with_stuck(tmp_path, status) as calls,
        patch(_MOD + "._worktree_porcelain", return_value="?? out.log\n"),
        patch(_MOD + ".confirm", return_value=False),
    ):
        rc = main(["--cleanup-only", "--clean-dirty"])
    assert rc == 0
    assert not any(c[:1] == ("worktree",) for c in calls)
    out = capsys.readouterr().out
    assert "left feature/9-x in place" in out
    assert "needs attention" in out
    assert "merged, not removable" in out  # detail=None → fallback


def test_clean_dirty_dry_run_shows_plan_without_removing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--clean-dirty --dry-run shows what would be deleted and neither prompts
    nor removes anything."""
    _make_profile(tmp_path, "library-release")
    status = _stuck_status(tmp_path)
    with (
        _sweep_with_stuck(tmp_path, status) as calls,
        patch(_MOD + "._worktree_porcelain", return_value="?? out.log\n"),
        patch(_MOD + ".confirm") as confirm,
    ):
        rc = main(["--cleanup-only", "--clean-dirty", "--dry-run"])
    assert rc == 0
    confirm.assert_not_called()
    assert not any(c[:1] == ("worktree",) for c in calls)
    out = capsys.readouterr().out
    assert "would remove the worktree and:" in out
    assert "[dry-run] git worktree remove --force" in out


def test_clean_dirty_reads_porcelain_from_worktree_path() -> None:
    """_worktree_porcelain shells git into the worktree and returns its
    porcelain; a git failure yields '' so the caller refuses (safe default)."""
    from vergil_tooling.bin.vrg_finalize_pr import _worktree_porcelain

    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=0, stdout="?? out.log\n"),
    ):
        assert _worktree_porcelain(Path("/wt")) == "?? out.log\n"
    with patch(
        _MOD + ".subprocess.run",
        return_value=CompletedProcess(args=(), returncode=128, stdout=""),
    ):
        assert _worktree_porcelain(Path("/wt")) == ""


# -- the watched run: watch, settle once, report, release (epic .github#1218) --

_PR_A = "https://github.com/o/r/pull/1"
_PR_B = "https://github.com/o/r/pull/2"
_PR_C = "https://github.com/o/r/pull/3"
_CD_LINE = "  CD: succeeded https://github.com/o/r/actions/runs/1"


@contextmanager
def _settle_env(
    root: Path,
    calls: list[str],
    *,
    validation: Callable[[FinalizeContext], None] | None = None,
) -> Generator[None]:
    """Record each settle step in *calls*, as ``<step>:<lock held>``.

    The steps' own behavior is covered above; these tests cover which run,
    in what order, under the lock or not, and what the run then reports and
    returns. *validation* replaces the validate step (default: it passes).
    """

    def record(name: str) -> Callable[[FinalizeContext], None]:
        def step(_ctx: FinalizeContext) -> None:
            calls.append(f"{name}:{_lock_held(root)}")

        return step

    def validate(ctx: FinalizeContext) -> None:
        calls.append(f"validation:{_lock_held(root)}")
        if validation is not None:
            validation(ctx)

    def cd(*_args: object, **_kwargs: object) -> str:
        calls.append(f"cd-check:{_lock_held(root)}")
        return _CD_LINE

    with (
        patch(_MOD + ".git.repo_root", return_value=root),
        patch(_MOD + "._stage_close_refs", side_effect=record("close-refs")),
        patch(_MOD + "._stage_cleanup", side_effect=record("cleanup")),
        patch(_MOD + "._stage_provision", side_effect=record("provision")),
        patch(_MOD + "._stage_validation", side_effect=validate),
        patch(_MOD + ".git.read_output", return_value="tip1234sha\n"),
        patch(_MOD + "._report_cd_run", side_effect=cd),
    ):
        yield


def _watch_returning(*results: PRResult) -> MagicMock:
    return MagicMock(return_value=list(results))


_SETTLED = [
    "close-refs:False",
    "cleanup:True",
    "provision:True",
    "validation:True",
    "cd-check:False",
]


def test_settle_once_after_mixed_batch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Review Focus 1: a PR merged before the run, one merged in the watch, one failed.

    The settle phase runs once, at the end, for the whole batch.
    """
    calls: list[str] = []
    watch = _watch_returning(
        _pr_result(_PR_A),
        _pr_result(_PR_B),
        _pr_result(_PR_C, "failed: CI", "required check failed: test"),
    )
    with _settle_env(tmp_path, calls), patch(_MOD + ".finalize_watch.watch", watch):
        rc = main([f"{_PR_A},{_PR_B},{_PR_C}"])
    assert rc == 3
    watch.assert_called_once_with([_PR_A, _PR_B, _PR_C], lock=ANY)
    assert calls == _SETTLED
    assert "Validate: passed" in capsys.readouterr().out


def test_settle_gets_only_the_merged_prs(tmp_path: Path) -> None:
    seen: list[list[str]] = []
    watch = _watch_returning(_pr_result(_PR_A), _pr_result(_PR_B, "failed: conflict"))
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + "._stage_close_refs", side_effect=lambda ctx: seen.append(ctx.merged_prs)),
        patch(_MOD + ".finalize_watch.watch", watch),
    ):
        main([f"{_PR_A},{_PR_B}"])
    assert seen == [[_PR_A]]


def test_nothing_merged_skips_settle(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    watch = _watch_returning(_pr_result(_PR_A, "timed out", "still pending"))
    with _settle_env(tmp_path, calls), patch(_MOD + ".finalize_watch.watch", watch):
        rc = main([_PR_A])
    assert rc == 3
    assert calls == []
    assert "No PR merged: the settle phase was skipped." in capsys.readouterr().out


def test_all_merged_is_full_success(tmp_path: Path) -> None:
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([f"{_PR_A},{_PR_B}"]) == 0
    assert calls == _SETTLED


def test_exit_3_when_one_failed(tmp_path: Path) -> None:
    watch = _watch_returning(_pr_result(_PR_A), _pr_result(_PR_B, "failed: closed"))
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([f"{_PR_A},{_PR_B}"]) == 3


# --- held by release (epic vergil-project/.github#1664, Task 26) --------------------

_HELD = "still held by release 2.1.400 (stage cut, running) at the 7200s batch deadline"


def test_exit_held_by_release_is_6_in_both_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert EXIT_HELD_BY_RELEASE == 6
    assert vrg_pr_await.EXIT_HELD_BY_RELEASE == 6
    assert finalize_watch.EXIT_HELD_BY_RELEASE == 6
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "6 every PR that did not merge is held by release" in out


def test_watch_runs_under_the_lock_watch(tmp_path: Path) -> None:
    watch = _watch_returning(_pr_result(_PR_A))
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        main([_PR_A])
    assert isinstance(watch.call_args.kwargs["lock"], LockWatch)


def test_all_held_exits_held_by_release(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    watch = _watch_returning(_pr_result(_PR_A), _pr_result(_PR_B, "held by release", _HELD))
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([f"{_PR_A},{_PR_B}"]) == 6
    out = capsys.readouterr().out
    assert f"held by release: {_HELD}" in out
    assert f"next: a release holds the lock; run `vrg-finalize-pr {_PR_B}` again" in out


def test_finalize_batch_failed_and_held_exits_not_all_merged(tmp_path: Path) -> None:
    watch = _watch_returning(
        _pr_result(_PR_A, "held by release", _HELD), _pr_result(_PR_B, "failed: CI", "why")
    )
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([f"{_PR_A},{_PR_B}"]) == 3


def test_held_and_provenance_violation_exits_not_all_merged(tmp_path: Path) -> None:
    watch = _watch_returning(
        _pr_result(_PR_A, violation=True), _pr_result(_PR_B, "held by release", _HELD)
    )
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([f"{_PR_A},{_PR_B}"]) == 3


def _validate_fails(_ctx: FinalizeContext) -> None:
    raise FinalizeError("post-finalization validation failed (exit 1)")


def test_exit_4_validate_failed_precedes_3(tmp_path: Path) -> None:
    calls: list[str] = []
    watch = _watch_returning(_pr_result(_PR_A), _pr_result(_PR_B, "failed: CI"))
    with (
        _settle_env(tmp_path, calls, validation=_validate_fails),
        patch(_MOD + ".finalize_watch.watch", watch),
    ):
        assert main([f"{_PR_A},{_PR_B}"]) == 4
    assert calls == _SETTLED  # fail_defer: the CD report still runs


def test_exit_4_validate_could_not_run_reports_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A validate that never ran (the container would not start) is 4, with its log."""
    with (
        _settle_env(tmp_path, []),
        # The real validate stage, whose command cannot be started at all.
        patch(_MOD + "._stage_validation", side_effect=_stage_validation),
        patch(
            _MOD + ".progress.run",
            side_effect=OSError("vrg-container-run: no such file or directory"),
        ),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        rc = main([_PR_A])
    assert rc == 4
    out = capsys.readouterr().out
    assert "DID NOT VALIDATE" in out
    # The settle summary names no exit status: finalize exits 4 here, which a
    # status fixed before the pipeline ran could not name (#4712).
    summary = next(line for line in out.splitlines() if line.startswith("PipelineError:"))
    assert "· exit" not in summary
    log_line = next(line for line in out.splitlines() if "Validate log:" in line)
    log = Path(log_line.split("Validate log:", 1)[1].strip())
    assert log.parent == tmp_path / ".vergil"
    assert "no such file or directory" in log.read_text()
    assert "    vrg-container-run: no such file or directory" in out


def test_exit_4_when_provision_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A dev image that cannot be provisioned means the validate could not run: 4."""
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(
            _MOD + "._stage_provision", side_effect=FinalizeError("dev-image provisioning failed")
        ),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A]) == 4
    assert "validation:True" not in calls
    assert not _lock_held(tmp_path)
    out = capsys.readouterr().out
    assert "Validate could not run: FinalizeError: dev-image provisioning failed" in out


def test_report_broken_develop_names_head_and_commits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A develop that did not validate is said loudly, with head, commits, log and tail."""

    def cleanup(ctx: FinalizeContext) -> None:
        ctx.synced_from = "aaaa111"
        ctx.synced_to = "bbbb222"

    output = "\n".join(f"gate line {i}" for i in range(50))
    # stderr's warnings must not push the end of stdout out of the report.
    stderr = "\n".join(f"uv warning {i}" for i in range(45))

    def read_output(*args: str) -> str:
        if args[0] == "log":
            assert args == ("log", "--oneline", "aaaa111..bbbb222")
            return "bbbb222 feat: second\naaab12 fix: first"
        return "bbbb222\n"

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._stage_close_refs"),
        patch(_MOD + "._stage_cleanup", side_effect=cleanup),
        patch(_MOD + "._stage_provision"),
        patch(_MOD + ".git.read_output", side_effect=read_output),
        patch(
            _MOD + ".progress.run",
            side_effect=subprocess.CalledProcessError(
                1, ("vrg-container-run",), output=output, stderr=stderr
            ),
        ),
        patch(_MOD + "._report_cd_run", return_value=_CD_LINE),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        rc = main([_PR_A])
    assert rc == 4
    out = capsys.readouterr().out
    report = out[out.index("DID NOT VALIDATE") :]
    assert "Head: bbbb222" in report
    assert "    bbbb222 feat: second" in report
    stdout_tail = report[report.index("Last 40 lines of the validate's stdout:") :]
    stdout_tail = stdout_tail[: stdout_tail.index("Last 40 lines of the validate's stderr:")]
    assert "    gate line 49" in stdout_tail
    assert "    gate line 10" in stdout_tail
    assert "gate line 9\n" not in stdout_tail
    assert "    uv warning 44" in report
    log_line = next(line for line in report.splitlines() if "Validate log:" in line)
    assert "gate line 0" in Path(log_line.split("Validate log:", 1)[1].strip()).read_text()


def test_wait_cd_failure_after_a_passing_validate_names_cd(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With --wait-cd, a CD failure is 4, but the report blames CD, not the validate."""
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + "._stage_cd_check", side_effect=FinalizeError("CD run did not succeed")),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A, "--wait-cd"]) == 4
    out = capsys.readouterr().out
    assert "Validate: passed on develop" in out
    assert "DID NOT VALIDATE" not in out
    assert "ERROR: develop's CD did not succeed (--wait-cd)." in out
    assert "  CD: FinalizeError: CD run did not succeed" in out


@pytest.mark.parametrize(
    ("argv", "stage"), [([], "_report_cd_run"), (["--wait-cd"], "_wait_for_cd_run")]
)
def test_unreadable_cd_is_a_tool_error_naming_the_cause(
    argv: list[str], stage: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CD runs that cannot be listed exit 1, never 4: CD's state is not known (#5140)."""
    cause = "the CD runs for bbbb222 could not be listed: no credentials to list the CD runs"
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + "." + stage, side_effect=CDUnreadableError(cause)),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A, *argv]) == 1
    out = capsys.readouterr().out
    assert f"CD: error ({cause})" in out
    assert "CD did not succeed" not in out


def test_cd_check_failure_without_wait_cd_is_a_tool_error(tmp_path: Path) -> None:
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + "._stage_cd_check", side_effect=FinalizeError("lookup crashed")),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A]) == 1


def test_settle_under_real_lock(tmp_path: Path) -> None:
    """The real lock, not a mock: each settle step runs once, with no nested acquire."""
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A]) == 0
    assert calls == _SETTLED
    assert not _lock_held(tmp_path)
    assert (tmp_path / "build" / ".vergil-state" / "checkout.lock").read_text() == ""


def test_close_refs_runs_outside_lock(tmp_path: Path) -> None:
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        main([_PR_A])
    assert calls[0] == "close-refs:False"


def test_close_refs_failure_still_settles_and_exits_1(tmp_path: Path) -> None:
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + "._stage_close_refs", side_effect=FinalizeError("could not close o/r#7")),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A]) == 1
    assert calls == _SETTLED[1:]


def test_cleanup_failure_exits_1_and_lets_go_of_the_lock(tmp_path: Path) -> None:
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + "._stage_cleanup", side_effect=FinalizeError("working tree is not clean")),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A]) == 1
    assert not _lock_held(tmp_path)


def test_watch_error_is_a_tool_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(
            _MOD + ".finalize_watch.watch",
            side_effect=FinalizeWatchError(f"{_PR_A}: reading the PR failed: HTTP 502"),
        ),
    ):
        assert main([_PR_A]) == 1
    assert calls == []
    assert "HTTP 502" in capsys.readouterr().err


def test_malformed_deadline_is_a_tool_error_before_any_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VRG_FINALIZE_DEADLINE", "soon")
    calls: list[str] = []
    with _settle_env(tmp_path, calls):
        assert main([_PR_A]) == 1
    assert calls == []
    assert "VRG_FINALIZE_DEADLINE" in capsys.readouterr().err


def test_report_lists_merged_and_unmerged_with_reasons(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch = _watch_returning(
        _pr_result(_PR_A),
        _pr_result(_PR_B, "failed: CI", "required check failed: test"),
        _pr_result(_PR_C, "failed: conflict", "the PR can no longer merge cleanly"),
    )
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        main([f"{_PR_A},{_PR_B},{_PR_C}"])
    out = capsys.readouterr().out
    report = out[out.index("vrg-finalize-pr report") :]
    table = (
        "vrg-finalize-pr report\n"
        "  PR  BRANCH           TYPE  OUTCOME           TITLE\n"
        "  #1  feature/1-thing  feat  merged            feat: change 1\n"
        "  #2  feature/2-thing  feat  failed: CI        feat: change 2\n"
        "  #3  feature/3-thing  feat  failed: conflict  feat: change 3\n"
        "\n"
    )
    assert report.startswith(table)
    assert f"  #2 {_PR_B} (failed: CI: required check failed: test)\n" in report
    assert f"  #3 {_PR_C} (failed: conflict: the PR can no longer merge cleanly)\n" in report
    # One table, no per-outcome groups and no placeholder rows.
    assert "Merged (" not in report
    assert "Not merged" not in report
    assert "(none)" not in report
    assert "Validate: passed on develop" in report
    assert _CD_LINE.strip() in report


def test_report_all_merged_has_no_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """With every PR merged cleanly, the table is followed straight by the settle lines."""
    watch = _watch_returning(_pr_result(_PR_A), _pr_result(_PR_B))
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        main([f"{_PR_A},{_PR_B}"])
    out = capsys.readouterr().out
    report = out[out.index("vrg-finalize-pr report") :]
    assert report.startswith(
        "vrg-finalize-pr report\n"
        "  PR  BRANCH           TYPE  OUTCOME  TITLE\n"
        "  #1  feature/1-thing  feat  merged   feat: change 1\n"
        "  #2  feature/2-thing  feat  merged   feat: change 2\n"
        "  Validate: passed on develop"
    )
    assert "next:" not in report


def test_report_types_rows_like_repo_status() -> None:
    """TYPE comes from the branch, the dependency-update label or the title's type."""
    rows = [
        _pr_row(replace(_pr_result(_PR_A), title="fix(x): y")),
        _pr_row(replace(_pr_result(_PR_B), branch="deps/update-2026-11-18")),
        _pr_row(replace(_pr_result(_PR_C), branch="x", labels=("dependency-update",))),
        _pr_row(replace(_pr_result(_PR_A), title="no conventional type")),
    ]
    assert [row[2] for row in rows] == ["fix", "deps", "deps", "-"]


def test_report_title_is_truncated() -> None:
    row = _pr_row(replace(_pr_result(_PR_A), title="feat: " + "x" * 100))
    assert len(row[4]) == text_table.TITLE_WIDTH
    assert row[4].endswith("…")


@pytest.mark.parametrize(
    ("outcome", "step"),
    [
        ("failed: CI", f"hand it to an agent, then rerun `vrg-finalize-pr {_PR_B}`"),
        (
            "failed: cancelled",
            f"re-run its cancelled checks, then rerun `vrg-finalize-pr {_PR_B}`",
        ),
        ("failed: conflict", f"hand it to an agent, then rerun `vrg-finalize-pr {_PR_B}`"),
        ("failed: closed", f"hand it to an agent, then rerun `vrg-finalize-pr {_PR_B}`"),
        ("timed out", f"hand it to an agent, then rerun `vrg-finalize-pr {_PR_B}`"),
        (
            "failed: refused",
            "the one PR path refused to re-arm it; deal with its reason, then rerun"
            f" `vrg-finalize-pr {_PR_B}`",
        ),
        ("held by release", f"a release holds the lock; run `vrg-finalize-pr {_PR_B}` again"),
        ("failed: not a develop PR", "finalize watches develop PRs only"),
    ],
)
def test_report_names_next_step_per_unmerged_pr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], outcome: Outcome, step: str
) -> None:
    watch = _watch_returning(_pr_result(_PR_A), _pr_result(_PR_B, outcome, "why"))
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        main([f"{_PR_A},{_PR_B}"])
    assert f"\n    next: {step}" in capsys.readouterr().out


def _release_ok() -> MagicMock:
    run = MagicMock()
    run.return_value.returncode = 0
    return run


def test_release_only_on_full_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    watch = _watch_returning(_pr_result(_PR_A), _pr_result(_PR_B, "failed: CI"))
    run = _release_ok()
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".finalize_watch.watch", watch),
        patch(_MOD + ".subprocess.run", run),
    ):
        assert main([f"{_PR_A},{_PR_B}", "--release"]) == 3
    run.assert_not_called()
    assert "Release skipped: the run is not a full success (exit 3)." in capsys.readouterr().out


def test_release_skipped_when_validate_fails(tmp_path: Path) -> None:
    run = _release_ok()
    with (
        _settle_env(tmp_path, [], validation=_validate_fails),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".subprocess.run", run),
    ):
        assert main([_PR_A, "--release"]) == 4
    run.assert_not_called()


def test_release_installs_by_default(tmp_path: Path) -> None:
    run = _release_ok()
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".subprocess.run", run),
    ):
        assert main([_PR_A, "--release"]) == 0
    assert run.call_args.args[0] == ("vrg-release",)
    assert run.call_args.kwargs["cwd"] == tmp_path


def _assert_removed(capsys: pytest.CaptureFixture[str], argv: list[str], message: str) -> None:
    """*argv* exits 1 with *message* alone, before any side effect."""
    with (
        patch(_MOD + ".git.repo_root") as repo_root,
        patch(_MOD + ".gate.unreleased_refusal") as gate_check,
        patch(_MOD + ".finalize_watch.watch") as watch,
        patch(_MOD + ".subprocess.run") as run,
    ):
        assert main(argv) == 1
    for mock in (repo_root, gate_check, watch, run):
        mock.assert_not_called()
    assert capsys.readouterr().err == f"{message}\n"


def test_finalize_install_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    """--install is refused by name before any side effect."""
    _assert_removed(
        capsys,
        [_PR_A, "--install"],
        "vrg-finalize-pr: --install was removed; install is part of every release",
    )


def test_no_install_removed_message(capsys: pytest.CaptureFixture[str]) -> None:
    """--no-install is refused, with or without --release: install is part of every release."""
    for argv in ([_PR_A, "--no-install"], [_PR_A, "--release", "--no-install"]):
        _assert_removed(
            capsys,
            argv,
            "vrg-finalize-pr: --no-install was removed; install is part of every release",
        )


def test_exit_5_release_failed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = MagicMock()
    run.return_value.returncode = 2
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".subprocess.run", run),
    ):
        assert main([_PR_A, "--release"]) == 5
    err = capsys.readouterr().err
    assert "release failed (exit 2)" in err
    assert "  Once it has opened its tracking issue, finish it with: vrg-release --resume\n" in err
    assert "  If it stopped before that, re-run the release alone with: vrg-release\n" in err


def test_chained_release_runs_after_lock_released(tmp_path: Path) -> None:
    """Review Focus 3: the release starts only once the lock is free to take."""
    held: list[bool] = []

    def release(*_args: object, **_kwargs: object) -> MagicMock:
        held.append(_lock_held(tmp_path))
        done = MagicMock()
        done.returncode = 0
        return done

    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(_MOD + ".subprocess.run", side_effect=release),
    ):
        assert main([_PR_A, "--release"]) == 0
    assert held == [False]


def test_merged_violation_blocks_release(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A violation that has already merged blocks the release (vergil-tooling#4662, #4695)."""
    calls: list[str] = []
    watch = _watch_returning(_pr_result(_PR_A, detail="x (audit): closed", violation=True))
    run = _release_ok()
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".finalize_watch.watch", watch),
        patch(_MOD + ".subprocess.run", run),
    ):
        assert main([_PR_A, "--release"]) == 3
    watch.assert_called_once_with([_PR_A], lock=ANY)
    assert calls == _SETTLED  # develop still settles: the PR is merged
    run.assert_not_called()
    out = capsys.readouterr().out
    assert "  #1  feature/1-thing  feat  merged, PROVENANCE VIOLATION  feat: change 1\n" in out
    assert "  #1 PROVENANCE VIOLATION (x (audit): closed)" in out
    assert "not a full success" in out
    assert "--allow-provenance-violation" not in out
    assert "a human must look before any release" in out
    assert "Release skipped: the run is not a full success (exit 3)." in out


def test_merged_violation_with_failed_validate_is_4(tmp_path: Path) -> None:
    """A develop that did not validate takes precedence over the violation's 3."""
    watch = _watch_returning(_pr_result(_PR_A, detail="x (audit): closed", violation=True))
    run = _release_ok()
    with (
        _settle_env(tmp_path, [], validation=_validate_fails),
        patch(_MOD + ".finalize_watch.watch", watch),
        patch(_MOD + ".subprocess.run", run),
    ):
        assert main([_PR_A, "--release"]) == 4
    run.assert_not_called()


def test_allow_provenance_violation_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--allow-provenance-violation is refused by name before any side effect (#4695)."""
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".finalize_watch.watch") as watch,
        patch(_MOD + ".subprocess.run") as run,
    ):
        assert main([_PR_A, "--allow-provenance-violation"]) == 1
    watch.assert_not_called()
    run.assert_not_called()
    assert calls == []
    err = capsys.readouterr().err
    assert "vrg-finalize-pr: --allow-provenance-violation was removed" in err
    assert "a provenance violation always fails" in err
    assert "fix the classifier" in err


def test_skip_post_checks_fatal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--skip-post-checks is refused by name before any watch or settle step."""
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main([_PR_A, "--skip-post-checks"]) == 1
    watch.assert_not_called()
    assert calls == []
    err = capsys.readouterr().err
    assert "vrg-finalize-pr: --skip-post-checks was removed" in err
    assert "settles develop once" in err


def test_all_selects_worktree_prs(tmp_path: Path) -> None:
    wts = [_WT8, _WT7]

    def pr_for(branch: str) -> PRRef:
        return _PR7 if branch == "feature/7-foo" else _PR8

    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".worktrees.list_worktrees", return_value=wts),
        patch(_MOD + ".worktrees.open_pr_for_branch", side_effect=pr_for),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged) as watch,
    ):
        assert main(["--all"]) == 0
    watch.assert_called_once_with([_PR7.url, _PR8.url], lock=ANY)


def test_all_with_no_open_prs_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main(["--all"]) == 0
    watch.assert_not_called()
    assert "no open PRs found in worktrees" in capsys.readouterr().out


_SPAM_LOGIN = "spam-login-xyz"
_SPAM_TITLE = "SPAM-MARKER-TEXT get the fix here: [download](https://example.invalid/a)"


def _fork_pr_page(number: int, branch: str) -> dict[str, object]:
    """A GraphQL page of one open fork PR on *branch*, shaped as GitHub returns it."""
    node = {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": _SPAM_TITLE,
        "headRefName": branch,
        "headRefOid": "e" * 40,
        "state": "OPEN",
        "createdAt": "2026-11-01T09:30:00Z",
        "author": {"__typename": "User", "login": _SPAM_LOGIN},
        "isCrossRepository": True,
        "headRepositoryOwner": {"login": "mallory"},
        "headRepository": {"name": "r"},
    }
    page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [node]}
    return {"repository": {"pullRequests": page}}


def test_finalize_never_adopts_fork_pr(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A fork PR on a worktree's branch name is never watched (GHSA-r4j2-cwcp-xg56):
    it is reported on stderr, metadata only, and its text appears nowhere."""
    authors = TrustedAuthors(human=("the-human",), agents=("the-agent",), orgs={})
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch("vergil_tooling.lib.worktrees.github.current_repo", return_value="o/r"),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=authors),
        patch(
            "vergil_tooling.lib.github.graphql",
            return_value=_fork_pr_page(66, _WT7.branch),
        ),
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main(["--all"]) == 0
    watch.assert_not_called()
    captured = capsys.readouterr()
    assert "no open PRs found in worktrees" in captured.out
    assert captured.err == (
        f"skipped untrusted content: o/r#66 pr {_SPAM_LOGIN} at 2026-11-01T09:30:00Z"
        " on head (cross-repository)\n"
    )
    assert "SPAM-MARKER-TEXT" not in captured.out + captured.err


def test_all_refuses_several_open_prs_on_one_branch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Several open PRs on a branch are an error naming them, never the first."""
    err = worktrees.BranchPRError("2 open PRs on feature/7-foo (#7, #8): name the PR to act on")
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".worktrees.open_pr_for_branch", side_effect=err),
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main(["--all"]) == 1
    watch.assert_not_called()
    assert "#7, #8" in capsys.readouterr().err


_TRUST_FAILURES = [
    pytest.param(
        TrustReadError("prs_for_branch o/r feature/7-foo", "the GraphQL call failed: 502"),
        "the GraphQL call failed: 502",
        id="read-failure",
    ),
    pytest.param(
        TrustedAuthorsNotConfiguredError("no trusted authors are configured"),
        "no trusted authors are configured",
        id="not-configured",
    ),
]


@pytest.mark.parametrize(("argv", "prefix"), [([], "vrg-finalize-pr: "), (["--all"], "--all: ")])
@pytest.mark.parametrize(("err", "reason"), _TRUST_FAILURES)
def test_branch_lookup_trust_failure_exits_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    prefix: str,
    err: Exception,
    reason: str,
) -> None:
    """A branch lookup the trust layer cannot read, or cannot judge for want of
    a trusted-authors configuration, is a one-line error and exit 1, never 'no
    PR' and never a traceback: in the interactive pick and under --all."""
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".worktrees.open_pr_for_branch", side_effect=err),
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main(argv) == 1
    watch.assert_not_called()
    err_text = capsys.readouterr().err
    assert f"{prefix}{err}\n" in err_text
    assert reason in err_text
    assert "Traceback" not in err_text


def test_cleanup_only_settles_without_watch(tmp_path: Path) -> None:
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main(["--cleanup-only"]) == 0
    watch.assert_not_called()
    assert calls == _SETTLED[1:]  # no close-refs: no PR merged in this run


def test_cleanup_only_log_is_headed_by_the_version_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A settle with no watch opens its own run log, and its first line names
    the tooling version, as the run's first output line does (issue #4994)."""
    with (
        _settle_env(tmp_path, []),
        patch("vergil_tooling.lib.progress.metadata.version", return_value="2.1.400"),
    ):
        assert main(["--cleanup-only"]) == 0
    line = "vrg-finalize-pr: vergil-tooling 2.1.400"
    assert capsys.readouterr().out.splitlines()[0] == line
    (log,) = sorted((tmp_path / ".vergil").glob("vrg-finalize-pr-*.log"))
    assert log.read_text().splitlines()[0] == line


def test_version_line_heads_even_a_refused_run(capsys: pytest.CaptureFixture[str]) -> None:
    """The version line comes before a refusal, so every run's output names it."""
    with patch("vergil_tooling.lib.progress.metadata.version", return_value="2.1.400"):
        assert main([_PR_A, "--install"]) == 1
    assert capsys.readouterr().out == "vrg-finalize-pr: vergil-tooling 2.1.400\n"


def test_cleanup_only_failed_validate_is_4(tmp_path: Path) -> None:
    with _settle_env(tmp_path, [], validation=_validate_fails):
        assert main(["--cleanup-only"]) == 4


def test_interactive_no_open_prs_offers_cleanup_only(tmp_path: Path) -> None:
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".confirm", return_value=True) as offer,
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main([]) == 0
    assert "Run cleanup only" in offer.call_args.args[0]
    watch.assert_not_called()
    assert calls == _SETTLED[1:]


def test_dry_run_watches_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run") as git_run,
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".github.head_ref") as head_ref,
        patch(_MOD + ".github.close_issue") as close,
        patch(_MOD + ".finalize_watch.watch") as watch,
        patch(_MOD + ".subprocess.run") as run,
    ):
        assert main([f"{_PR_A},{_PR_B}", "--release", "--dry-run"]) == 0
    for mock in (git_run, head_ref, close, watch, run):
        mock.assert_not_called()
    out = capsys.readouterr().out
    assert f"[dry-run] would watch until each merges or fails: {_PR_A}, {_PR_B}" in out
    assert "[dry-run] would chain into: vrg-release\n" in out


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["1", "--strategy", "squash"], "--strategy was removed"),
        (["1", "--no-install"], "--no-install was removed"),
        (["1", "--release", "--no-install"], "--no-install was removed"),
        (["1", "--install"], "--install was removed"),
        (["1", "--install", "--no-install"], "--install was removed"),
        (["1", "--skip-post-checks"], "--skip-post-checks was removed"),
        (["1", "--skip-post-checks", "--release"], "--skip-post-checks was removed"),
    ],
)
def test_flag_refusals_before_any_side_effect(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    with (
        patch(_MOD + ".git.repo_root") as repo_root,
        patch(_MOD + ".finalize_watch.watch") as watch,
    ):
        assert main(argv) == 1
    repo_root.assert_not_called()
    watch.assert_not_called()
    assert message in capsys.readouterr().err


def test_strategy_flag_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    """--strategy is refused by name, naming the new default."""
    with patch(_MOD + ".finalize_watch.watch") as watch:
        assert main(["123", "--strategy", "rebase"]) == 1
    watch.assert_not_called()
    err = capsys.readouterr().err
    assert "vrg-finalize-pr: --strategy was removed" in err
    assert "the one PR path chooses the merge method from the PR" in err
    assert "a merge commit for a back-merge or a dependency-update PR" in err


def test_no_merge_call_anywhere() -> None:
    """Finalize never merges a PR and never turns auto-merge on (spec §6.3)."""
    from vergil_tooling.bin import vrg_finalize_pr as fin  # noqa: PLC0415

    source = Path(fin.__file__).read_text(encoding="utf-8")
    assert "pr_merge" not in source
    assert "wait_and_merge" not in source
    assert "enable_auto_merge" not in source
    assert not hasattr(fin, "pr_merge")


def test_empty_pr_list_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_MOD + ".finalize_watch.watch") as watch:
        assert main([" , "]) == 1
    watch.assert_not_called()
    assert "vrg-finalize-pr: no PR in ' , '" in capsys.readouterr().err


def test_interrupted_settle_keeps_its_exit_code(tmp_path: Path) -> None:
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + "._stage_cleanup", side_effect=KeyboardInterrupt),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([_PR_A]) == 130
    assert not _lock_held(tmp_path)


def test_synced_commits_that_cannot_be_listed_say_why() -> None:
    ctx = _stage_ctx([])
    ctx.synced_from, ctx.synced_to = "aaa", "bbb"
    err = subprocess.CalledProcessError(128, ("git", "log"), stderr="bad revision")
    with patch(_MOD + ".git.read_output", side_effect=err):
        (line,) = _synced_commits(ctx)
    assert line.startswith("(could not list them: ")
    assert "bad revision" in line


def test_synced_commits_unknown_without_a_range() -> None:
    assert _synced_commits(_stage_ctx([])) == ["(unknown: the sync did not record its range)"]


def test_head_of_reads_the_branch() -> None:
    with patch(_MOD + ".git.read_output", return_value="abc123\n") as read:
        assert _head_of("develop") == "abc123"
    read.assert_called_once_with(
        "rev-parse", "--verify", "--end-of-options", "refs/heads/develop^{commit}"
    )


def test_head_of_failure_fails_the_stage() -> None:
    err = subprocess.CalledProcessError(128, ("git", "rev-parse"), stderr="unknown revision")
    with (
        patch(_MOD + ".git.read_output", side_effect=err),
        pytest.raises(
            FinalizeError, match=r"(?s)could not read the head of develop:.*unknown revision"
        ),
    ):
        _head_of("develop")


def test_merged_branch_read_failure_fails_the_stage() -> None:
    ctx = _stage_ctx([_PR_A])
    err = subprocess.CalledProcessError(1, ("gh", "pr", "view"), stderr="HTTP 502")
    with (
        patch(_MOD + ".github.head_ref", side_effect=err),
        pytest.raises(FinalizeError, match=rf"(?s)head branch of merged PR {_PR_A}:.*HTTP 502"),
    ):
        _merged_branches(ctx)


def test_merged_branches_reads_each_merged_pr() -> None:
    ctx = _stage_ctx([f"{_PR_A},{_PR_B}"])
    with patch(_MOD + ".github.head_ref", side_effect=["feature/1-a\n", "feature/2-b"]):
        assert _merged_branches(ctx) == ["feature/1-a", "feature/2-b"]


def test_merged_branches_skips_an_empty_head_branch() -> None:
    ctx = _stage_ctx([_PR_A])
    with (
        patch(_MOD + ".github.head_ref", return_value="\n"),
        patch(_MOD + "._confirmed_merged_branch") as confirm,
    ):
        assert _merged_branches(ctx) == []
    confirm.assert_not_called()


def test_merged_branches_keeps_an_unconfirmed_prs_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A PR the trust layer does not confirm names no branch to delete."""
    ctx = _stage_ctx([f"{_PR_A},{_PR_B}"])
    with (
        patch(_MOD + ".github.head_ref", side_effect=["feature/1-a", "feature/2-b"]),
        patch(_MOD + "._confirmed_merged_branch", side_effect=[False, True]) as confirm,
    ):
        assert _merged_branches(ctx) == ["feature/2-b"]
    assert [c.args for c in confirm.call_args_list] == [
        ("o/r", 1, "feature/1-a"),
        ("o/r", 2, "feature/2-b"),
    ]
    out = capsys.readouterr().out
    assert f"Merged PR {_PR_A} is not a same-repo PR with a trusted author" in out
    assert "feature/1-a" not in out


@pytest.mark.parametrize(
    "exc",
    [
        TrustReadError("prs_for_branch o/r feature/1-a", "the repository is missing"),
        TrustedAuthorsNotConfiguredError("no trusted authors"),
    ],
)
def test_merged_branches_trust_failure_fails_the_stage(exc: Exception) -> None:
    ctx = _stage_ctx([_PR_A])
    with (
        patch(_MOD + ".github.head_ref", return_value="feature/1-a"),
        patch(_MOD + "._confirmed_merged_branch", side_effect=exc),
        pytest.raises(FinalizeError, match=rf"could not confirm merged PR {_PR_A} through"),
    ):
        _merged_branches(ctx)


def _merged_ref(number: int, branch: str) -> PRRef:
    return replace(_pr_ref(number, "t", state="MERGED"), head=branch)


def test_confirmed_merged_branch_needs_the_prs_own_number() -> None:
    """Only the PR itself confirms its branch: a fork is skipped, another PR does not count."""
    fork = trust.Skipped(
        "o/r", 1, "pr", "mallory", "2026-01-01T00:00:00Z", "head", trust.CROSS_REPOSITORY
    )
    with patch(
        _MOD + ".trust.prs_for_branch", return_value=([_merged_ref(9, "feature/1-a")], [fork])
    ) as lookup:
        assert not _confirmed_merged_branch("o/r", 1, "feature/1-a")
        assert _confirmed_merged_branch("o/r", 9, "feature/1-a")
    lookup.assert_called_with("o/r", "feature/1-a", state="merged")


# -- refuse a release cascade on a repo that is not released (issue #4175) --


@pytest.mark.parametrize("flag", ["--release"])
def test_release_cascade_refused_when_repo_not_released(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    """No [publish].release: refuse before any watch, cleanup or chain."""
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".gate.unreleased_refusal", side_effect=_real_unreleased_refusal),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.run") as m_git_run,
        patch(_MOD + ".progress.run_pipeline") as m_pipeline,
        patch(_MOD + ".finalize_watch.watch") as m_watch,
        patch(_MOD + ".subprocess.run") as mock_run,
    ):
        result = main([flag, "--all"])
    assert result == 1
    m_git_run.assert_not_called()
    m_pipeline.assert_not_called()
    m_watch.assert_not_called()
    mock_run.assert_not_called()
    err = capsys.readouterr().err
    assert f"vrg-finalize-pr {flag}: this repo is not released" in err


@pytest.mark.parametrize("mode", ["user", "audit"])
def test_agent_release_refused_before_watch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Releasing is a human action: an agent's --release stops before any watch."""
    monkeypatch.setenv("VRG_IDENTITY_MODE", mode)
    calls: list[str] = []
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + ".gate.unreleased_refusal") as m_gate,
        patch(_MOD + ".finalize_watch.watch") as m_watch,
        patch(_MOD + ".subprocess.run") as m_run,
    ):
        assert main([_PR_A, "--release"]) == 1
    m_watch.assert_not_called()
    m_gate.assert_not_called()
    m_run.assert_not_called()
    assert calls == []
    assert "vrg-finalize-pr --release: releasing is a human maintainer action" in (
        capsys.readouterr().err
    )


def test_agent_cleanup_only_release_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--cleanup-only --release is refused for an agent before the settle phase."""
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    calls: list[str] = []
    with _settle_env(tmp_path, calls), patch(_MOD + ".subprocess.run") as m_run:
        assert main(["--cleanup-only", "--release"]) == 1
    assert calls == []
    m_run.assert_not_called()
    assert "Agents cannot chain into vrg-release" in capsys.readouterr().err


def test_agent_without_release_still_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only --release is refused: an agent's plain finalize settles as before."""
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    calls: list[str] = []
    with _settle_env(tmp_path, calls):
        assert main(["--cleanup-only"]) == 0
    assert calls


def test_finalize_without_release_ignores_unreleased_repo(tmp_path: Path) -> None:
    """A plain finalize on a repo that is not released is unaffected."""
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".gate.unreleased_refusal", side_effect=_real_unreleased_refusal) as m_gate,
    ):
        assert main(["--cleanup-only"]) == 0
    m_gate.assert_not_called()


def test_parse_args_release_flag() -> None:
    assert parse_args([]).release is False
    assert parse_args(["--release"]).release is True


@pytest.mark.parametrize(
    "flag", ["--install", "--no-install", "--skip-post-checks", "--allow-provenance-violation"]
)
def test_removed_flags_hidden_from_help(capsys: pytest.CaptureFixture[str], flag: str) -> None:
    """A removed flag is still parsed, so it can be refused by name, but not advertised."""
    with pytest.raises(SystemExit):
        main(["--help"])
    assert flag not in capsys.readouterr().out


_FIN_MOD = "vergil_tooling.bin.vrg_finalize_pr"


def test_parse_pr_list_splits_and_trims() -> None:
    assert _parse_pr_list("123, 124 ,125") == ["123", "124", "125"]
    assert _parse_pr_list("123") == ["123"]


def test_resolve_open_prs_skips_worktrees_without_pr() -> None:
    wts = [
        Worktree(path=Path("/r/.worktrees/issue-2-b"), branch="feature/2-b"),
        Worktree(path=Path("/r/.worktrees/issue-1-a"), branch="feature/1-a"),
    ]

    def pr_for(branch: str) -> PRRef | None:
        return _pr_ref(1, "a") if branch == "feature/1-a" else None

    with (
        patch(_FIN_MOD + ".worktrees.list_worktrees", return_value=wts),
        patch(_FIN_MOD + ".worktrees.open_pr_for_branch", side_effect=pr_for),
    ):
        # branch-sorted: feature/1-a precedes feature/2-b; only 1-a has a PR
        assert _resolve_open_prs(Path("/r")) == ["https://github.com/o/r/pull/1"]


# -- relay-ref cleanup (issue #2369) ---------------------------------------


def test_delete_branch_and_worktree_prunes_relay_ref(tmp_path: Path) -> None:
    """Both cleanup paths funnel through _delete_branch_and_worktree, so the
    branch's pr-workflow relay ref is dropped alongside it (issue #2369)."""
    with (
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".github_transport.GitHubTransport") as mock_transport,
    ):
        assert _delete_branch_and_worktree(
            "feature/9-x", tmp_path, dry_run=False, reason="PR merged", guard=ReleaseGuard()
        )
    mock_transport.assert_called_once_with("feature/9-x")
    mock_transport.return_value.delete.assert_called_once_with()


def test_delete_branch_and_worktree_skips_relay_ref_on_dry_run(tmp_path: Path) -> None:
    """--dry-run never pushes a ref deletion; it only announces it."""
    with (
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + ".github_transport.GitHubTransport") as mock_transport,
    ):
        assert _delete_branch_and_worktree(
            "feature/9-x", tmp_path, dry_run=True, reason="PR merged", guard=ReleaseGuard()
        )
    mock_transport.assert_not_called()


def test_main_merged_branch_prunes_relay_ref(tmp_path: Path) -> None:
    """Merged-PR path: the explicit-target cleanup drops the relay ref."""
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
        patch(f"{_MOD}.github.head_ref", return_value="feature/42-x"),
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.read_output", return_value="feature/42-x"),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.worktrees.worktree_for_branch", return_value=None),
        patch(f"{_MOD}.clean_branch_images", return_value=(0, [])),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}._report_cd_run"),
        patch(f"{_MOD}.github_transport.GitHubTransport") as mock_transport,
    ):
        result = main(["42"])
    assert result == 0
    mock_transport.assert_any_call("feature/42-x")
    mock_transport.return_value.delete.assert_called_once_with()


def test_main_closed_pr_cleanup_prunes_relay_ref(tmp_path: Path) -> None:
    """Closed-PR path (ancestry sweep): each swept branch drops its relay ref."""
    _make_profile(tmp_path, "library-release")
    with (
        _sweep_guards_pass(),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="feature/x"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=["feature/x", "develop"]),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
        patch(_MOD + ".github_transport.GitHubTransport") as mock_transport,
    ):
        result = main([])
    assert result == 0
    mock_transport.assert_any_call("feature/x")
    mock_transport.return_value.delete.assert_called_once_with()


def test_prune_orphan_relay_refs_deletes_dead_branch() -> None:
    """A relay ref whose branch no longer exists on the remote is pruned."""
    with (
        patch(_MOD + ".git.remote_branches", return_value={"feature/live"}),
        patch(
            _MOD + ".github_transport.list_relay_branches",
            return_value=["feature/dead", "feature/live"],
        ),
        patch(_MOD + ".github_transport.GitHubTransport") as mock_transport,
    ):
        _prune_orphan_relay_refs(dry_run=False)
    mock_transport.assert_called_once_with("feature/dead")
    mock_transport.return_value.delete.assert_called_once_with()


def test_prune_orphan_relay_refs_keeps_live_branch() -> None:
    """A relay ref whose branch still exists is left untouched (no-op)."""
    with (
        patch(_MOD + ".git.remote_branches", return_value={"feature/live"}),
        patch(
            _MOD + ".github_transport.list_relay_branches",
            return_value=["feature/live"],
        ),
        patch(_MOD + ".github_transport.GitHubTransport") as mock_transport,
    ):
        _prune_orphan_relay_refs(dry_run=False)
    mock_transport.assert_not_called()


def test_prune_orphan_relay_refs_skipped_on_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run neither reads the remote nor pushes any deletion."""
    with (
        patch(_MOD + ".git.remote_branches") as mock_remote,
        patch(_MOD + ".github_transport.list_relay_branches") as mock_list,
        patch(_MOD + ".github_transport.GitHubTransport") as mock_transport,
    ):
        _prune_orphan_relay_refs(dry_run=True)
    mock_remote.assert_not_called()
    mock_list.assert_not_called()
    mock_transport.assert_not_called()
    assert "[dry-run] prune orphaned" in capsys.readouterr().out


def test_prune_orphan_branch_images_checks_every_repo_in_the_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The sweep asks the whole platform repo set, not the finalized repo (#3566)."""
    repos = [_PROJECTS / "org" / "a", _PROJECTS / "other-org" / "b"]
    with (
        patch(_MOD + ".default_repo_roots", return_value=[_PROJECTS]) as mock_roots,
        patch(_MOD + ".discover_repos", return_value=repos) as mock_discover,
        patch(_MOD + ".prune_orphan_branch_images", return_value=(3, [])) as mock_prune,
    ):
        _prune_orphan_branch_images(dry_run=False)
    mock_roots.assert_called_once_with()
    mock_discover.assert_called_once_with([_PROJECTS])
    mock_prune.assert_called_once_with(repos)
    out = capsys.readouterr()
    assert "Pruned 3 orphaned cached container image(s)" in out.out
    assert out.err == ""


def test_prune_orphan_branch_images_quiet_when_nothing_removed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _prune_orphan_branch_images(dry_run=False)
    assert capsys.readouterr() == ("", "")


def test_prune_orphan_branch_images_warns_on_rmi_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused rmi is named with the runtime's error; finalize carries on (#3566)."""
    failure = ("ghcr.io/vergil-project/dev-python:3.14--feature-1-x--0123abcd", "image is in use")
    with patch(_MOD + ".prune_orphan_branch_images", return_value=(1, [failure])):
        _prune_orphan_branch_images(dry_run=False)
    out = capsys.readouterr()
    assert "Pruned 1 orphaned cached container image(s)" in out.out
    assert f"WARNING: could not remove cached image {failure[0]}: image is in use" in out.err


def test_prune_orphan_branch_images_skipped_without_a_repo_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No confirmable repo set (the physical host) skips the sweep and says so."""
    err = RepoSetError("no default repo set on the physical host. Pass --root DIR.")
    with (
        patch(_MOD + ".default_repo_roots", side_effect=err),
        patch(_MOD + ".prune_orphan_branch_images") as mock_prune,
    ):
        _prune_orphan_branch_images(dry_run=False)
    mock_prune.assert_not_called()
    out = capsys.readouterr().out
    assert "Skipping the orphaned-image sweep: no default repo set" in out
    assert "vrg-container-cache prune --root DIR" in out


def test_prune_orphan_branch_images_refuses_an_empty_repo_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(_MOD + ".discover_repos", return_value=[]),
        patch(_MOD + ".prune_orphan_branch_images") as mock_prune,
    ):
        _prune_orphan_branch_images(dry_run=False)
    mock_prune.assert_not_called()
    assert f"WARNING: no git repositories under {_PROJECTS}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("'docker images' failed: daemon down"),
        subprocess.CalledProcessError(128, ["git", "branch"], stderr="not a git repository"),
    ],
)
def test_prune_orphan_branch_images_lookup_failure_warns_and_continues(
    exc: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed image or branch lookup skips the sweep loudly but never fails finalize."""
    with patch(_MOD + ".prune_orphan_branch_images", side_effect=exc):
        _prune_orphan_branch_images(dry_run=False)
    assert "WARNING: skipped the orphaned-image sweep:" in capsys.readouterr().err


def test_prune_orphan_branch_images_skipped_on_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run neither resolves the repo set nor removes any image."""
    with (
        patch(_MOD + ".default_repo_roots") as mock_roots,
        patch(_MOD + ".prune_orphan_branch_images") as mock_prune,
    ):
        _prune_orphan_branch_images(dry_run=True)
    mock_roots.assert_not_called()
    mock_prune.assert_not_called()
    assert "[dry-run] prune orphaned cached dev images" in capsys.readouterr().out


def test_clean_branch_images_warns_on_rmi_failure(capsys: pytest.CaptureFixture[str]) -> None:
    """The finalized branch's image cleanup reports a refused rmi (#3566)."""
    failure = ("ghcr.io/vergil-project/dev-python:3.14--feature-1-x--0123abcd", "conflict")
    with patch(_MOD + ".clean_branch_images", return_value=(0, [failure])):
        _clean_branch_images("feature/1-x", Path("/r"))
    out = capsys.readouterr()
    assert "Cleaned" not in out.out
    assert f"WARNING: could not remove cached image {failure[0]}: conflict" in out.err


def test_clean_branch_images_warns_when_images_cannot_be_listed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(_MOD + ".clean_branch_images", side_effect=RuntimeError("daemon down")):
        _clean_branch_images("feature/1-x", Path("/r"))
    assert "WARNING: could not clean cached images for feature/1-x: daemon down" in (
        capsys.readouterr().err
    )


def test_clean_branch_images_warns_when_the_repo_cannot_be_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The images to clean are the repo's own (#4162); naming it runs git, and a
    # git failure is a warning, as an unlistable image store is.
    err = subprocess.CalledProcessError(128, ["git", "rev-parse"], stderr="not a git repository")
    with patch(_MOD + ".clean_branch_images", side_effect=err):
        _clean_branch_images("feature/1-x", Path("/r"))
    assert "stderr: not a git repository" in capsys.readouterr().err


def test_main_sweeps_orphan_relay_ref(tmp_path: Path) -> None:
    """The cleanup stage sweeps a relay ref whose branch is gone (issue #2369)."""
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.remote_branches", return_value=set()),
        patch(
            _MOD + ".github_transport.list_relay_branches",
            return_value=["feature/orphan"],
        ),
        patch(_MOD + ".github_transport.GitHubTransport") as mock_transport,
        patch(_MOD + "._report_cd_run"),
    ):
        result = main([])
    assert result == 0
    mock_transport.assert_any_call("feature/orphan")
    mock_transport.return_value.delete.assert_called_once_with()


# --- the watch as a progress stage (issue #4694) -----------------------------------


def _run_logs(root: Path) -> list[Path]:
    return sorted((root / ".vergil").glob("vrg-finalize-pr-*.log"))


def test_watch_is_a_stage_sharing_the_settle_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        _settle_env(tmp_path, []),
        patch(_MOD + ".finalize_watch.watch", side_effect=_all_merged),
    ):
        assert main([f"{_PR_A},{_PR_B}"]) == 0
    out = capsys.readouterr().out
    assert "→ watch  starting...\n" in out
    assert re.search(r"\n✓ watch  \d+\.\ds — 2 merged, 0 failed\n", out)
    # The watch's line comes before the settle phase's stages.
    assert out.index("✓ watch") < out.index("→ close-refs")
    # One run log holds the watch and the settle phase.
    (log,) = _run_logs(tmp_path)
    text = log.read_text()
    # The shared log's first line names the tooling version (issue #4994).
    assert text.splitlines()[0] == progress.tooling_version_line("vrg-finalize-pr")
    assert text.index("=== watch: started ===") < text.index("=== close-refs: started ===")
    assert "=== watch: ok (2 merged, 0 failed, " in text
    assert f"full log → {log}" in out


@pytest.mark.parametrize(
    ("results", "line"),
    [
        (
            [_pr_result(_PR_A), _pr_result(_PR_B, "failed: CI", "required check failed: test")],
            "⚠ watch  {t} — 1 merged, 1 failed",
        ),
        (
            [_pr_result(_PR_A, violation=True), _pr_result(_PR_B)],
            "⚠ watch  {t} — 2 merged, 0 failed",
        ),
        (
            [_pr_result(_PR_A), _pr_result(_PR_B, "timed out", "still pending")],
            "⚠ watch  {t} — 1 merged, 0 failed, 1 timed out",
        ),
    ],
)
def test_watch_stage_warns_unless_every_pr_merged_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], results: list[PRResult], line: str
) -> None:
    watch = _watch_returning(*results)
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([f"{_PR_A},{_PR_B}"]) == 3
    pattern = re.escape(line).replace(re.escape("{t}"), r"\d+\.\ds")
    assert re.search(rf"\n{pattern}\n", capsys.readouterr().out)


def test_nothing_merged_names_the_watch_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    watch = _watch_returning(_pr_result(_PR_A, "failed: closed", "closed without merging"))
    with _settle_env(tmp_path, []), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([_PR_A]) == 3
    out = capsys.readouterr().out
    (log,) = _run_logs(tmp_path)
    assert out.endswith(f"No PR merged: the settle phase was skipped.\n   full log → {log}\n")
    assert "=== watch: warn (0 merged, 1 failed, " in log.read_text()


def test_a_watch_error_fails_the_stage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []
    watch = MagicMock(side_effect=FinalizeWatchError(f"{_PR_A}: reading the PR failed: boom"))
    with _settle_env(tmp_path, calls), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([_PR_A]) == 1
    captured = capsys.readouterr()
    assert calls == []
    assert re.search(r"\n✗ watch  \d+\.\ds — FinalizeWatchError: ", captured.out)
    assert "vrg-finalize-pr: the watch stopped: " in captured.err
    (log,) = _run_logs(tmp_path)
    assert "=== watch: failed (FinalizeWatchError: " in log.read_text()


def test_watch_without_trusted_authors_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The watch's provenance check with no trusted-authors configuration stops
    the run with one line and exit 1, never a traceback."""
    calls: list[str] = []
    watch = MagicMock(side_effect=TrustedAuthorsNotConfiguredError("no trusted authors"))
    with _settle_env(tmp_path, calls), patch(_MOD + ".finalize_watch.watch", watch):
        assert main([_PR_A]) == 1
    assert calls == []
    err = capsys.readouterr().err
    assert "vrg-finalize-pr: the watch stopped: no trusted authors\n" in err
    assert "Traceback" not in err


def test_watch_keeps_close_refs_cd_and_advisory_stages(tmp_path: Path) -> None:
    """Finalize reduced to its primary job keeps every settle stage (epic .github#1664, Task 10).

    The watch takes the PRs alone: it judges no gates, so it needs no clone.
    """
    calls: list[str] = []
    watch = _watch_returning(_pr_result(_PR_A))
    with (
        _settle_env(tmp_path, calls),
        patch(_MOD + "._stage_advisory_sync", side_effect=lambda _ctx: calls.append("advisory")),
        patch(_MOD + ".finalize_watch.watch", watch),
    ):
        assert main([_PR_A]) == 0
    watch.assert_called_once_with([_PR_A], lock=ANY)
    assert calls == [
        "close-refs:False",
        "advisory",
        "cleanup:True",
        "provision:True",
        "validation:True",
        "cd-check:False",
    ]


# -- cleanup safety on the release lock (Task 27 of epic .github#1664) ------

_LOCK_SHA = "b" * 40
_LOCK_MOD = "vergil_tooling.lib.worktrees"


def _lock_record(worktree: str) -> LockRecord:
    return LockRecord(
        holder_identity="human",
        holder_host="mac.local",
        version="2.1.9",
        worktree=worktree,
        tracking_issue="o/r#77",
        stage="cut",
        health="running",
        broken_stage="",
        started_at="2026-11-27T10:00:00Z",
        heartbeat_at="2026-11-27T10:05:00Z",
        token="a1b2c3",
    )


@contextmanager
def _sweep_under_lock(
    tmp_path: Path,
    wt: Worktree,
    *,
    lock: LockState | BaseException,
    issues: list[tuple[str, int, str]],
) -> Generator[list[tuple[str, ...]]]:
    """A cleanup-only run of one merged worktree, *wt*, judged by the real protection.

    The lock read beneath ``worktrees.lock_protection`` returns (or raises)
    *lock*; the open trusted release tracking issues are *issues*.
    """
    removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.MERGED, pr_number=1, ahead=2, dirty=False
    )
    calls: list[tuple[str, ...]] = []
    if isinstance(lock, BaseException):
        read_lock = patch(_LOCK_MOD + ".refs.read_lock", side_effect=lock)
    else:
        read_lock = patch(_LOCK_MOD + ".refs.read_lock", return_value=lock)
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run", side_effect=lambda *a: calls.append(a)),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=removable),
        patch(_MOD + "._worktree_is_dirty", return_value=False),
        patch(_MOD + ".clean_branch_images", return_value=(0, [])),
        patch(_MOD + "._report_cd_run"),
        patch(_MOD + ".worktrees.lock_protection", wraps=_real_lock_protection),
        read_lock,
        patch(_LOCK_MOD + "._open_release_issues", return_value=issues),
        patch(_LOCK_MOD + "._trusted_release_issues", return_value=issues),
    ):
        yield calls


def _removed(calls: list[tuple[str, ...]], wt: Worktree) -> bool:
    return any(c[:1] == ("worktree",) for c in calls) or ("branch", "-D", wt.branch) in calls


def test_cleanup_skips_worktree_lock_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The worktree the lock names is kept whatever its branch, not only release/*."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    held = LockState(sha=_LOCK_SHA, record=_lock_record(str(wt.path)), error="")
    with _sweep_under_lock(tmp_path, wt, lock=held, issues=[]) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert not _removed(calls, wt)
    out = capsys.readouterr().out
    assert "kept .worktrees/issue-1-x: the release lock names it" in out


def test_cleanup_removes_a_feature_worktree_the_held_lock_does_not_name(tmp_path: Path) -> None:
    """A held lock keeps only its own worktree and every release/* worktree."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    named = str(tmp_path / ".worktrees" / "release-2.1.9")
    held = LockState(sha=_LOCK_SHA, record=_lock_record(named), error="")
    with _sweep_under_lock(tmp_path, wt, lock=held, issues=[]) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert ("worktree", "remove", str(wt.path)) in calls


def test_cleanup_skips_release_worktrees_while_lock_held(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A held lock keeps every release/* worktree, the ones it does not name too,
    with no tracking issue open."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "release-post-2.1.8", branch="release/post-2.1.8")
    named = str(tmp_path / ".worktrees" / "release-2.1.9")
    held = LockState(sha=_LOCK_SHA, record=_lock_record(named), error="")
    with _sweep_under_lock(tmp_path, wt, lock=held, issues=[]) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert not _removed(calls, wt)
    out = capsys.readouterr().out
    assert "kept .worktrees/release-post-2.1.8: a release is running" in out
    assert "release lock held: release 2.1.9" in out


def test_cleanup_unreadable_lock_protects(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lock that is present but unreadable is held: release/* worktrees are kept."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "release-2.1.9", branch="release/2.1.9")
    unreadable = LockState(
        sha=_LOCK_SHA, record=None, error="refs/vergil/release-lock is unreadable: bad json"
    )
    with _sweep_under_lock(tmp_path, wt, lock=unreadable, issues=[]) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert not _removed(calls, wt)
    out = capsys.readouterr().out
    assert "kept .worktrees/release-2.1.9: a release is running" in out
    assert "the release lock cannot be read" in out


def test_cleanup_failed_lock_read_keeps_the_worktree_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """When whether the lock is held cannot be told, no worktree is removed, the
    error is printed, and the run exits non-zero."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    failure = RefReadError("reading refs/vergil/release-lock from origin failed: no network")
    with _sweep_under_lock(tmp_path, wt, lock=failure, issues=[]) as calls:
        rc = main(["--cleanup-only"])
    assert rc != 0
    assert not _removed(calls, wt)
    out = capsys.readouterr().out
    assert "kept .worktrees/issue-1-x" in out
    assert "could not read the release lock of o/r" in out


def test_cleanup_removes_release_worktree_with_no_lock_and_no_open_issue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No lock and no open tracking issue: a merged release/* worktree is removed and logged."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "release-2.1.9", branch="release/2.1.9")
    absent = LockState(sha=None, record=None, error="")
    with _sweep_under_lock(tmp_path, wt, lock=absent, issues=[]) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert ("worktree", "remove", str(wt.path)) in calls
    assert ("branch", "-D", "release/2.1.9") in calls
    assert f"removed worktree {wt.path} (branch release/2.1.9): PR merged" in (
        capsys.readouterr().out
    )


def test_cleanup_skips_release_worktree_after_break_while_issue_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A break deleted the lock; the broken release's tracking issue is still open,
    so its worktree stays protected until that issue closes."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "release-2.1.9", branch="release/2.1.9")
    absent = LockState(sha=None, record=None, error="")
    issues = [("2.1.9", 77, "trusted body")]
    with _sweep_under_lock(tmp_path, wt, lock=absent, issues=issues) as calls:
        rc = main(["--cleanup-only"])
    assert rc == 0
    assert not _removed(calls, wt)
    out = capsys.readouterr().out
    assert "kept .worktrees/release-2.1.9: a release is running" in out
    assert "release 2.1.9 (#77)" in out


def test_release_guard_reads_the_lock_once_for_a_feature_then_a_release_worktree(
    tmp_path: Path,
) -> None:
    """A feature worktree reads the lock; a release worktree after it reuses that
    read and adds only the tracking issues."""
    guard = ReleaseGuard()
    with (
        patch(_MOD + ".worktrees.lock_protection", return_value=_NO_LOCK) as lock,
        patch(_MOD + ".worktrees.release_protection", return_value=_RELEASE_OPEN) as read,
    ):
        feature = guard.keeps("feature/1-x", tmp_path / ".worktrees" / "issue-1-x", tmp_path)
        release = guard.keeps("release/2.1.9", tmp_path / ".worktrees" / "release-2.1.9", tmp_path)
    assert feature is False
    assert release is True
    lock.assert_called_once_with("o/r")
    read.assert_called_once_with("o/r", lock=_NO_LOCK)
