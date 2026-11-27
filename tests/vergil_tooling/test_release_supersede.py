"""vrg-release --supersede (spec §4.7 of epic vergil-project/.github#1440)."""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import branch_lookup, branch_pr
from vergil_tooling.lib import progress, trust
from vergil_tooling.lib.pending_release import PendingRelease, ReleaseTags
from vergil_tooling.lib.release import checklist, confirm, records, state, supersede
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages
from vergil_tooling.lib.release.preflight import (
    _check_main_not_ahead_of_develop,
    _check_no_open_release,
    _release_contents,
    preflight,
)
from vergil_tooling.lib.release.prepare import prepare
from vergil_tooling.lib.release.resume import ALWAYS_RUN
from vergil_tooling.lib.release.state import IssueEntry, StateBlock
from vergil_tooling.lib.release.supersede import (
    Superseded,
    absorb,
    check_eligible,
    close_superseded,
    load_superseded,
)
from vergil_tooling.lib.release.tracking import create_tracking_issue
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

_ORCH = "vergil_tooling.lib.release.orchestrator"
_PREFLIGHT = "vergil_tooling.lib.release.preflight"
_SUPERSEDE = "vergil_tooling.lib.release.supersede"
_CONFIRM = "vergil_tooling.lib.release.confirm"
_TRACKING = "vergil_tooling.lib.release.tracking"
_PREPARE = "vergil_tooling.lib.release.prepare"
_RECORDS = "vergil_tooling.lib.release.records"
_RESUME_READ = "vergil_tooling.lib.release.resume.github.read_json"
_REPO = "owner/repo"
_OLD = "2.1.5"
_ROOT = Path("/tmp/repo")  # noqa: S108
_MAIN_COMMIT = "m" * 40
_OLD_ISSUES = (IssueEntry(ref=f"{_REPO}#10", prs=(7,)),)
# The tracked stages, in pipeline order.
_TRACKED = tuple(s.name for s in build_stages() if s.name not in ALWAYS_RUN)


# --- a simulated run of the pipeline writes the state blocks judged here ---


def _set(**facts: str) -> Callable[[ReleaseContext], None]:
    def stage(ctx: ReleaseContext) -> None:
        for name, value in facts.items():
            setattr(ctx, name, value)

    return stage


_FACTS: dict[str, Callable[[ReleaseContext], None]] = {
    "prepare": _set(release_pr_url=f"https://github.com/{_REPO}/pull/50"),
    "confirm-main": _set(
        tag=f"v{_OLD}",
        tag_commit=_MAIN_COMMIT,
        cd_run_id="111",
        release_url=f"https://github.com/{_REPO}/releases/tag/v{_OLD}",
    ),
    "back-merge-bump": _set(bump_pr_url=f"https://github.com/{_REPO}/pull/51"),
    "confirm-develop": _set(develop_cd_run_id="222"),
}
# Each tracked stage's function, by the name build_stages() reads it under.
_STAGE_FNS = {
    "prepare": "prepare",
    "merge-release": "merge_release",
    "confirm-main": "confirm_main",
    "back-merge-bump": "back_merge_and_bump",
    "teardown-worktree": "teardown_worktree",
    "confirm-develop": "confirm_develop",
    "promote": "_promote_phase",
    "cd-publish": "deliver_failed_jobs",
    "package-index": "wait_for_index",
    "finalize": "finalize",
    "update-deps": "update_deps",
    "install": "install",
    "release-records": "post_records",
    "close-tracking": "close_tracking",
}


def _failed_release(ctx: ReleaseContext) -> None:
    """confirm-main as it fails on a CD release job that did not succeed."""
    message = confirm._record_failed_release(ctx, "777")
    raise ReleaseError(phase="confirm-main", command="verify job", message=message)


def _simulated_state(fail: str, fn: Callable[[ReleaseContext], None] | None = None) -> StateBlock:
    """The state block a fresh run of the pipeline leaves when stage *fail* fails.

    Every tracked stage runs through the pipeline's own wrapper, which
    records it done with its facts; *fail* raises instead (or runs *fn*,
    which must raise). The hold rule and fail-fast decide what else runs.
    """
    ctx = ReleaseContext(repo=_REPO, version=_OLD, repo_root=_ROOT, version_override=None)
    ctx.issue_number = 40
    ctx.state = state.initial_state(
        version=_OLD, cut="c" * 40, issues=_OLD_ISSUES, without_deps=False
    )
    run_state = ReleaseState(version_override=None, repo_root=_ROOT)
    run_state.ctx = ctx

    def write(c: ReleaseContext, new: StateBlock) -> None:
        c.state = new

    def stage_fn(name: str) -> Callable[[ReleaseContext], None]:
        if name == fail:
            if fn is not None:
                return fn
            return MagicMock(side_effect=ReleaseError(phase=name, command=name, message="boom"))
        return _FACTS.get(name, lambda _ctx: None)

    with contextlib.ExitStack() as stack:
        for target in ("comment_phase_complete", "comment_phase_failed", "ensure_checklist"):
            stack.enter_context(patch(f"{_ORCH}.{target}"))
        stack.enter_context(patch(_ORCH + ".tick_stage"))
        stack.enter_context(patch(_ORCH + ".write_state", side_effect=write))
        stack.enter_context(patch(_CONFIRM + ".write_state", side_effect=write))
        for name, attr in _STAGE_FNS.items():
            stack.enter_context(patch(f"{_ORCH}.{attr}", side_effect=stage_fn(name)))
        stages = [s for s in build_stages() if s.name not in ("audit", "preflight")]
        args = argparse.Namespace(output_format="plain", output_window=None)
        # The pipeline writes its run log under the repo root it is given.
        logs = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        progress.run_pipeline(
            run_state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=logs
        )
    assert ctx.state is not None
    return ctx.state


def test_stages_before_records_follow_the_pipeline() -> None:
    assert _TRACKED[: _TRACKED.index("release-records")] == supersede.STAGES_BEFORE_RECORDS


@pytest.mark.parametrize("left", supersede.DELIVERY_STAGES)
def test_supersede_eligible_delivery_left(left: str) -> None:
    current = _simulated_state(left)
    assert current.tag == f"v{_OLD}"
    assert left not in current.done
    check_eligible(current)


def test_supersede_eligible_untagged_confirm_main() -> None:
    current = _simulated_state("confirm-main", _failed_release)
    assert current.tag is None
    assert current.failed_release_run == "777"
    assert current.done == ("prepare", "merge-release")
    check_eligible(current)


def test_supersede_refuses_confirm_main_halt_without_failed_release_run() -> None:
    current = _simulated_state("confirm-main")
    assert current.failed_release_run is None
    with pytest.raises(ReleaseError, match="records no failed CD release run") as info:
        check_eligible(current)
    assert f"vrg-release --resume {_OLD}" in str(info.value)


def test_supersede_refuses_merge_release_halt() -> None:
    current = _simulated_state("merge-release")
    with pytest.raises(ReleaseError, match="first stage not done is merge-release"):
        check_eligible(current)


@pytest.mark.parametrize("fail", ["confirm-develop", "promote", "finalize"])
def test_supersede_refuses_a_tagged_release_with_more_than_delivery_left(fail: str) -> None:
    current = _simulated_state(fail)
    assert current.tag is not None
    with pytest.raises(ReleaseError, match=f"other than delivery are not done: {fail}"):
        check_eligible(current)


def test_supersede_refuses_a_tagged_release_with_only_records_left() -> None:
    current = _simulated_state("release-records")
    with pytest.raises(ReleaseError, match="every stage before release-records is done"):
        check_eligible(current)


# --- a bare vrg-release never supersedes ---


def _release_issue(ver: str, number: int, body: str) -> dict[str, object]:
    return {"number": number, "title": f"release: {ver}", "body": body}


@contextlib.contextmanager
def _open_releases(issues: list[dict[str, object]]) -> Generator[None]:
    """The open-release search lists *issues*; the trust layer reads each body as trusted."""
    bodies = {int(str(issue["number"])): str(issue["body"]) for issue in issues}

    def _read_issue(_repo: str, number: int) -> trust.IssueContent:
        return trust.IssueContent(
            body=bodies[number],
            body_role=trust.Role.AGENTS,
            author_role=trust.Role.AGENTS,
            comments=(),
            skipped=(),
        )

    with (
        patch(_RESUME_READ, return_value=issues),
        patch(_PREFLIGHT + ".trust.read_issue", side_effect=_read_issue),
    ):
        yield


def _body(current: StateBlock) -> str:
    return f"## Release\n\n{state.render_state(current)}\n"


def test_supersede_refuses_bare_release() -> None:
    eligible = _simulated_state("cd-publish")
    check_eligible(eligible)
    open_issues = [_release_issue(_OLD, 40, _body(eligible))]
    with (
        _open_releases(open_issues),
        pytest.raises(ReleaseError, match="One release runs at a time") as info,
    ):
        _check_no_open_release(_REPO)
    assert f"`vrg-release --resume {_OLD}`" in str(info.value)


def test_open_release_check_exempts_only_the_superseded_issue(
    capsys: pytest.CaptureFixture[str],
) -> None:
    eligible = _body(_simulated_state("cd-publish"))
    with _open_releases([_release_issue(_OLD, 40, eligible)]):
        _check_no_open_release(_REPO, exempt=40)
    assert "only the superseded release #40 is open" in capsys.readouterr().out
    other = _release_issue("2.1.6", 41, eligible.replace(_OLD, "2.1.6"))
    with (
        _open_releases([_release_issue(_OLD, 40, eligible), other]),
        pytest.raises(ReleaseError, match="release 2.1.6"),
    ):
        _check_no_open_release(_REPO, exempt=40)


# --- loading the superseded release ---


@pytest.fixture
def loaded() -> Iterator[dict[str, MagicMock]]:
    """load_superseded's reads: the open issues, the guarded state, the facts, the tag."""
    with (
        patch(_SUPERSEDE + "._open_release_issues") as m_open,
        patch(_SUPERSEDE + ".read_state") as m_read,
        patch(_SUPERSEDE + ".verify_facts") as m_verify,
        patch(_SUPERSEDE + ".github.read_json") as m_release,
        patch(_SUPERSEDE + ".git.read_output", return_value="") as m_ls_remote,
    ):
        yield {
            "open": m_open,
            "read": m_read,
            "verify": m_verify,
            "release": m_release,
            "ls_remote": m_ls_remote,
        }


def _load(loaded: dict[str, MagicMock], current: StateBlock) -> Superseded:
    loaded["open"].return_value = [(_OLD, 40, _body(current))]
    loaded["read"].return_value = current

    def verify(ctx: ReleaseContext, _current: StateBlock) -> None:
        ctx.release_merge_sha = _MAIN_COMMIT

    loaded["verify"].side_effect = verify
    return load_superseded(_REPO, _ROOT, _OLD, list(_TRACKED))


def test_load_superseded_tagged_reads_state_under_the_identity_rule(
    loaded: dict[str, MagicMock],
) -> None:
    current = _simulated_state("package-index")
    loaded["release"].return_value = {"tagName": f"v{_OLD}"}
    found = _load(loaded, current)
    assert found == Superseded(version=_OLD, number=40, issues=current.issues, main_commit=None)
    assert not found.untagged
    ctx = loaded["read"].call_args.args[0]
    assert (ctx.repo, ctx.version, ctx.issue_number) == (_REPO, _OLD, 40)
    loaded["verify"].assert_called_once()
    assert loaded["release"].call_args.args[:2] == ("release", "view")


def test_load_superseded_untagged_takes_the_release_merge(loaded: dict[str, MagicMock]) -> None:
    current = _simulated_state("confirm-main", _failed_release)
    found = _load(loaded, current)
    assert found.untagged
    assert found.main_commit == _MAIN_COMMIT
    loaded["ls_remote"].assert_called_once_with(
        "ls-remote", "--tags", "origin", f"refs/tags/v{_OLD}"
    )
    loaded["release"].assert_not_called()


def test_load_superseded_refuses_an_untagged_release_whose_tag_exists(
    loaded: dict[str, MagicMock],
) -> None:
    loaded["ls_remote"].return_value = "t" * 40 + f"\trefs/tags/v{_OLD}"
    with pytest.raises(ReleaseError, match=f"v{_OLD} exists on origin"):
        _load(loaded, _simulated_state("confirm-main", _failed_release))


def test_load_superseded_refuses_a_missing_github_release(loaded: dict[str, MagicMock]) -> None:
    loaded["release"].side_effect = subprocess.CalledProcessError(1, "gh", stderr="not found")
    with pytest.raises(ReleaseError, match="could not be read") as info:
        _load(loaded, _simulated_state("install"))
    assert "not found" in (info.value.detail or "")


def test_load_superseded_refuses_when_not_open(loaded: dict[str, MagicMock]) -> None:
    loaded["open"].return_value = [("2.1.4", 39, "")]
    with pytest.raises(ReleaseError, match=f"no 'release: {_OLD}' tracking issue is open"):
        load_superseded(_REPO, _ROOT, _OLD, list(_TRACKED))
    loaded["read"].assert_not_called()


def test_load_superseded_refuses_an_ineligible_release_before_checking_facts(
    loaded: dict[str, MagicMock],
) -> None:
    with pytest.raises(ReleaseError, match="cannot be superseded"):
        _load(loaded, _simulated_state("merge-release"))
    loaded["verify"].assert_not_called()


# --- the superseding release's preflight ---


def _superseded(*, untagged: bool) -> Superseded:
    fail = ("confirm-main", _failed_release) if untagged else ("cd-publish", None)
    current = _simulated_state(*fail)
    return Superseded(
        version=_OLD,
        number=40,
        issues=current.issues,
        main_commit=_MAIN_COMMIT if untagged else None,
    )


def _fresh_preflight(
    develop_version: str,
    superseded: Superseded,
    *,
    version_override: str | None = None,
    ahead: str = "",
) -> tuple[ReleaseContext, dict[str, MagicMock]]:
    """Run a fresh preflight with --supersede; main is ahead of develop by *ahead*."""
    wt = _ROOT / ".worktrees" / "release-x"

    def git_read(*args: str) -> str:
        if args[0] == "describe":
            return f"v{_OLD}"
        if args[0] == "log":
            # Main is ahead of develop by *ahead*, all of it under the accepted commit.
            return ahead if args[-1] == "origin/develop..origin/main" else ""
        raise AssertionError(args)

    with (
        patch(_PREFLIGHT + "._check_host_prerequisites"),
        patch(_PREFLIGHT + ".check_gh_auth", return_value=_REPO),
        patch(_PREFLIGHT + "._read_and_validate_config"),
        patch(_PREFLIGHT + "._check_branch_and_tree"),
        patch(_PREFLIGHT + ".version.show", return_value=develop_version),
        patch(_PREFLIGHT + ".load_superseded", return_value=superseded) as m_load,
        patch(_PREFLIGHT + ".git.run"),
        patch(_PREFLIGHT + ".git.read_output", side_effect=git_read) as m_git,
        patch(_PREFLIGHT + "._check_no_open_release") as m_open,
        patch(_PREFLIGHT + "._check_packages_unpublished"),
        patch(_PREFLIGHT + ".pending_deps_prs", return_value=[]),
        patch(_PREFLIGHT + "._develop_cd_gate"),
        # The lock and quiesce, and the head the gates judged (unmoved), have tests
        # of their own in test_release_preflight.py.
        patch(_PREFLIGHT + "._develop_head", return_value="d" * 40),
        patch(_PREFLIGHT + "._lock_and_quiesce"),
        patch(_PREFLIGHT + "._acquire_release_worktree", return_value=wt) as m_worktree,
        patch(_PREFLIGHT + "._release_contents", return_value=("c" * 40, _pending(), ())),
        patch(_PREFLIGHT + ".os.chdir"),
    ):
        ctx = preflight(
            version_override=version_override,
            repo_root=_ROOT,
            stages=list(_TRACKED),
            supersede=_OLD,
        )
    return ctx, {"load": m_load, "open": m_open, "worktree": m_worktree, "git": m_git}


def _pending() -> PendingRelease:
    return PendingRelease(
        tags=ReleaseTags(latest=None, rolling=None, rolling_target=None),
        main_moved_ts=0.0,
        changes=(),
    )


def test_supersede_untagged_takes_next_patch_and_passes_branch_check() -> None:
    superseded = _superseded(untagged=True)
    ahead = f"{_MAIN_COMMIT[:7]} Merge pull request #50 from {_REPO}/release/{_OLD}"
    ctx, mocks = _fresh_preflight(_OLD, superseded, ahead=ahead)
    assert ctx.version == "2.1.6"
    assert ctx.version_override == "patch"
    assert ctx.release_branch == "release/2.1.6"
    assert ctx.superseded is superseded
    mocks["load"].assert_called_once_with(_REPO, _ROOT, _OLD, list(_TRACKED))
    mocks["open"].assert_called_once_with(_REPO, exempt=40)
    # The main-ahead check excluded the superseded release commit, and passed.
    log_calls = [c.args for c in mocks["git"].call_args_list if c.args[0] == "log"]
    assert log_calls[-1][-1] == f"^{_MAIN_COMMIT}"


@pytest.mark.parametrize(("bump", "expected"), [("minor", "2.2.0"), ("major", "3.0.0")])
def test_supersede_untagged_combines_with_a_bump(bump: str, expected: str) -> None:
    ctx, _ = _fresh_preflight(_OLD, _superseded(untagged=True), version_override=bump)
    assert ctx.version == expected
    assert ctx.version_override == bump


def test_supersede_untagged_refuses_a_moved_develop_version() -> None:
    with pytest.raises(ReleaseError, match="develop's VERSION is 2.1.6"):
        _fresh_preflight("2.1.6", _superseded(untagged=True))


def test_supersede_tagged_releases_develops_version() -> None:
    ctx, mocks = _fresh_preflight("2.1.6", _superseded(untagged=False))
    assert ctx.version == "2.1.6"
    assert ctx.version_override is None
    mocks["open"].assert_called_once_with(_REPO, exempt=40)


def test_supersede_accepts_untagged_main_commit(capsys: pytest.CaptureFixture[str]) -> None:
    ahead = "abc1234 Merge pull request #50\ndef5678 chore(release): prepare 2.1.5"

    def read(*args: str) -> str:
        return "" if args[-1] == f"^{_MAIN_COMMIT}" else ahead

    with (
        patch(_PREFLIGHT + ".git.run"),
        patch(_PREFLIGHT + ".git.read_output", side_effect=read),
    ):
        _check_main_not_ahead_of_develop(accept=_MAIN_COMMIT)
    assert "ahead only by the superseded release commit" in capsys.readouterr().out


def test_supersede_main_ahead_check_still_refuses_other_commits() -> None:
    def read(*args: str) -> str:
        if args[-1] == f"^{_MAIN_COMMIT}":
            return "fff0000 fix: pushed to main directly"
        return "abc1234 Merge pull request #50\nfff0000 fix: pushed to main directly"

    with (
        patch(_PREFLIGHT + ".git.run"),
        patch(_PREFLIGHT + ".git.read_output", side_effect=read),
        pytest.raises(ReleaseError, match="1 commit") as info,
    ):
        _check_main_not_ahead_of_develop(accept=_MAIN_COMMIT)
    assert info.value.detail == "fff0000 fix: pushed to main directly"


# --- absorbing the superseded release's issues ---


def test_absorb_keeps_old_issues_first_and_merges_a_shared_one() -> None:
    old = (IssueEntry(f"{_REPO}#10", (7,)), IssueEntry(f"{_REPO}#11", (8,)))
    new = (IssueEntry(f"{_REPO}#11", (9,)), IssueEntry(f"{_REPO}#12", (9,)))
    assert absorb(old, new) == (
        IssueEntry(f"{_REPO}#10", (7,)),
        IssueEntry(f"{_REPO}#11", (8, 9)),
        IssueEntry(f"{_REPO}#12", (9,)),
    )


def _new_ctx(superseded: Superseded) -> ReleaseContext:
    ctx = ReleaseContext(repo=_REPO, version="2.1.6", repo_root=_ROOT, version_override=None)
    ctx.progress_block = checklist.render(["audit", "prepare"], checked={"audit"})
    ctx.cut = "c" * 40
    ctx.pending_release = _pending()
    ctx.superseded = superseded
    return ctx


def test_supersede_absorbs_issues_and_records_supersedes() -> None:
    superseded = _superseded(untagged=True)
    new_issue = IssueEntry(f"{_REPO}#12", (9,))
    with (
        patch(_PREFLIGHT + ".gather_release_contents", return_value=(_pending(), (new_issue,))),
        patch(_PREFLIGHT + ".git.read_output", return_value="c" * 40),
    ):
        _, _, issues = _release_contents(_ROOT, superseded)
    assert issues == (*_OLD_ISSUES, new_issue)

    # The tracking issue's state block and Contents record the superseded version.
    ctx = _new_ctx(superseded)
    ctx.issues = issues
    bodies: list[str] = []

    def create(*args: str) -> str:
        bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
        return f"https://github.com/{_REPO}/issues/60"

    with (
        patch(_TRACKING + ".github.read_output", side_effect=create),
        patch(_TRACKING + ".github.run"),
    ):
        create_tracking_issue(ctx)
    created = state.parse_state(bodies[0])
    assert created.supersedes == _OLD
    assert created.absorbed == (f"{_REPO}#10",)
    assert created.superseded_main_commit == _MAIN_COMMIT
    assert created.issues == issues
    assert "### Supersedes" in bodies[0]
    assert f"Release {_OLD}, which cannot finish" in bodies[0]
    assert f"- {_REPO}#10 (#7), absorbed from {_OLD}" in bodies[0]
    assert f"- {_REPO}#12 (#9)\n" in bodies[0]

    # The records name the new version; only an absorbed issue's says it supersedes.
    assert ctx.state is not None
    ctx.state = replace(ctx.state, done=("install",), install_done=False)
    ctx.tag = "v2.1.6"
    ctx.release_url = f"https://github.com/{_REPO}/releases/tag/v2.1.6"
    posted: dict[str, str] = {}

    def write(c: ReleaseContext, new: StateBlock) -> None:
        c.state = new

    with (
        patch(_RECORDS + ".record_comments", return_value=()),
        patch(_RECORDS + "._merge_commit", return_value="a" * 40),
        patch(_RECORDS + "._release_date", return_value="2026-11-22"),
        patch(_RECORDS + "._authors", return_value=TrustedAuthors(("human",), ())),
        patch(_RECORDS + "._post", side_effect=lambda ref, body: posted.__setitem__(ref, body)),
        patch(_RECORDS + ".write_state", side_effect=write),
    ):
        records.post_records(ctx)
    assert set(posted) == {f"{_REPO}#10", f"{_REPO}#12"}
    assert all("Released: owner/repo v2.1.6 on 2026-11-22" in body for body in posted.values())
    assert posted[f"{_REPO}#10"].endswith(f"- Supersedes: v{_OLD}")
    assert "Supersedes" not in posted[f"{_REPO}#12"]


# --- closing the superseded release ---


def _close(tmp_path: Path, *, untagged: bool, number: int | None = 40) -> dict[str, Any]:
    worktree = tmp_path / ".worktrees" / f"release-{_OLD}"
    worktree.mkdir(parents=True)
    with (
        patch(_SUPERSEDE + ".remove_worktree") as m_remove,
        patch(_SUPERSEDE + ".git.ref_exists", return_value=True) as m_ref,
        patch(_SUPERSEDE + ".git.read_output", return_value="h" * 40) as m_git,
        patch(_SUPERSEDE + ".github.run") as m_gh,
    ):
        close_superseded(
            _REPO,
            number,
            "2.1.6",
            superseded_version=_OLD,
            new_issue_url=f"https://github.com/{_REPO}/issues/60",
            untagged=untagged,
            repo_root=tmp_path,
        )
    return {"worktree": worktree, "remove": m_remove, "ref": m_ref, "git": m_git, "gh": m_gh}


def test_supersede_untagged_removes_worktree_and_branch(tmp_path: Path) -> None:
    mocks = _close(tmp_path, untagged=True)
    mocks["remove"].assert_called_once_with(
        mocks["worktree"], branch=f"release/{_OLD}", reason="superseded by a new release"
    )
    git_calls = [c.args for c in mocks["git"].call_args_list]
    branch = f"release/{_OLD}"
    assert git_calls == [
        ("branch", "-D", branch),
        ("ls-remote", "--heads", "origin", f"refs/heads/{branch}"),
        ("push", "origin", "--delete", branch),
        ("branch", "-dr", f"origin/{branch}"),
    ]
    close = mocks["gh"].call_args.args
    assert close[:5] == ("issue", "close", "40", "--repo", _REPO)
    comment = close[close.index("--comment") + 1]
    assert f"Superseded by release 2.1.6: https://github.com/{_REPO}/issues/60" in comment
    assert "posts no `Released:` records" in comment


def test_close_superseded_tagged_leaves_branches_alone(tmp_path: Path) -> None:
    mocks = _close(tmp_path, untagged=False)
    mocks["remove"].assert_not_called()
    mocks["git"].assert_not_called()
    mocks["gh"].assert_called_once()


def test_close_superseded_already_closed_still_removes_the_branch(tmp_path: Path) -> None:
    mocks = _close(tmp_path, untagged=True, number=None)
    mocks["remove"].assert_called_once()
    mocks["gh"].assert_not_called()


def test_close_superseded_close_failure_raises(tmp_path: Path) -> None:
    with (
        patch(_SUPERSEDE + ".github.run", side_effect=subprocess.CalledProcessError(1, "gh")),
        pytest.raises(ReleaseError, match="could not be closed") as info,
    ):
        close_superseded(
            _REPO,
            40,
            "2.1.6",
            superseded_version=_OLD,
            new_issue_url="u",
            untagged=False,
            repo_root=tmp_path,
        )
    assert "vrg-release --resume 2.1.6" in str(info.value)


def test_remove_branch_git_failure_raises(tmp_path: Path) -> None:
    with (
        patch(_SUPERSEDE + ".git.ref_exists", return_value=True),
        patch(
            _SUPERSEDE + ".git.read_output",
            side_effect=subprocess.CalledProcessError(1, ("git", "branch"), stderr="locked"),
        ),
        pytest.raises(ReleaseError, match="removing the superseded release's branch") as info,
    ):
        supersede.remove_release_branch(tmp_path, f"release/{_OLD}", phase="prepare")
    assert "locked" in (info.value.detail or "")


# --- prepare closes the superseded tracking issue once its own exists ---


def _prepare_ctx(current: StateBlock) -> ReleaseContext:
    ctx = ReleaseContext(
        repo=_REPO,
        version="2.1.6",
        repo_root=_ROOT,
        version_override=None,
        release_branch="release/2.1.6",
    )
    ctx.issue_number = 60
    ctx.issue_url = f"https://github.com/{_REPO}/issues/60"
    ctx.state = current
    return ctx


def _superseding_state(*, untagged: bool) -> StateBlock:
    return state.initial_state(
        version="2.1.6",
        cut="c" * 40,
        issues=_OLD_ISSUES,
        without_deps=False,
        supersedes=_OLD,
        absorbed=(f"{_REPO}#10",),
        superseded_main_commit=_MAIN_COMMIT if untagged else None,
    )


@pytest.fixture
def adopted_pr() -> Iterator[None]:
    """The release PR already exists, so prepare stops right after the tracking issues."""
    with branch_lookup(branch_pr(100, "OPEN")):
        yield


@pytest.mark.usefixtures("adopted_pr")
def test_prepare_closes_the_superseded_issue_on_a_fresh_run() -> None:
    ctx = _prepare_ctx(_superseding_state(untagged=True))
    ctx.superseded = _superseded(untagged=True)
    with (
        patch(_PREPARE + ".close_superseded") as m_close,
        patch(_PREPARE + ".superseded_issue") as m_lookup,
    ):
        prepare(ctx)
    m_lookup.assert_not_called()
    m_close.assert_called_once_with(
        _REPO,
        40,
        "2.1.6",
        superseded_version=_OLD,
        new_issue_url=f"https://github.com/{_REPO}/issues/60",
        untagged=True,
        repo_root=_ROOT,
    )


@pytest.mark.usefixtures("adopted_pr")
def test_prepare_on_resume_finds_the_superseded_issue() -> None:
    ctx = _prepare_ctx(_superseding_state(untagged=False))
    with (
        patch(_PREPARE + ".close_superseded") as m_close,
        patch(_PREPARE + ".superseded_issue", return_value=None) as m_lookup,
    ):
        prepare(ctx)
    m_lookup.assert_called_once_with(_REPO, _OLD)
    assert m_close.call_args.args[1] is None
    assert m_close.call_args.kwargs["untagged"] is False


@pytest.mark.usefixtures("adopted_pr")
def test_prepare_without_supersede_closes_nothing() -> None:
    ctx = _prepare_ctx(state.initial_state(version="2.1.6", cut="c", issues=(), without_deps=False))
    with patch(_PREPARE + ".close_superseded") as m_close:
        prepare(ctx)
    m_close.assert_not_called()


def test_superseded_issue_lookup() -> None:
    with patch(
        _SUPERSEDE + "._open_release_issues", return_value=[("2.1.6", 60, ""), (_OLD, 40, "")]
    ):
        assert supersede.superseded_issue(_REPO, _OLD) == 40
    with patch(_SUPERSEDE + "._open_release_issues", return_value=[("2.1.6", 60, "")]):
        assert supersede.superseded_issue(_REPO, _OLD) is None


def test_load_superseded_refuses_duplicate_open_issues(loaded: dict[str, MagicMock]) -> None:
    loaded["open"].return_value = [(_OLD, 40, ""), (_OLD, 41, "")]
    with pytest.raises(ReleaseError, match="2 open tracking issues") as info:
        load_superseded(_REPO, _ROOT, _OLD, list(_TRACKED))
    assert "#40, #41" in str(info.value)


def test_load_superseded_refuses_an_unconfirmed_release_merge(
    loaded: dict[str, MagicMock],
) -> None:
    current = _simulated_state("confirm-main", _failed_release)
    loaded["open"].return_value = [(_OLD, 40, _body(current))]
    loaded["read"].return_value = current
    with pytest.raises(ReleaseError, match="merge commit could not be confirmed"):
        load_superseded(_REPO, _ROOT, _OLD, list(_TRACKED))


def test_load_superseded_refuses_another_release(loaded: dict[str, MagicMock]) -> None:
    loaded["release"].return_value = {"tagName": "v2.1.4"}
    with pytest.raises(ReleaseError, match=f"no GitHub Release of v{_OLD}"):
        _load(loaded, _simulated_state("install"))


def test_load_superseded_refuses_a_failed_tag_read(loaded: dict[str, MagicMock]) -> None:
    loaded["ls_remote"].side_effect = subprocess.CalledProcessError(128, "git", stderr="offline")
    with pytest.raises(ReleaseError, match="could not be asked") as info:
        _load(loaded, _simulated_state("confirm-main", _failed_release))
    assert "offline" in (info.value.detail or "")


def test_superseded_issue_refuses_duplicates() -> None:
    issues = [(_OLD, 40, ""), (_OLD, 41, "")]
    with (
        patch(_SUPERSEDE + "._open_release_issues", return_value=issues),
        pytest.raises(ReleaseError, match="2 open tracking issues"),
    ):
        supersede.superseded_issue(_REPO, _OLD)


def test_remove_branch_worktree_failure_raises(tmp_path: Path) -> None:
    (tmp_path / ".worktrees" / f"release-{_OLD}").mkdir(parents=True)
    failure = subprocess.CalledProcessError(1, ("git", "worktree"), stderr="dirty")
    with (
        patch(_SUPERSEDE + ".remove_worktree", side_effect=failure),
        pytest.raises(ReleaseError, match="could not be removed") as info,
    ):
        supersede.remove_release_branch(tmp_path, f"release/{_OLD}", phase="prepare")
    assert "dirty" in (info.value.detail or "")


def test_remove_branch_skips_what_is_already_gone(tmp_path: Path) -> None:
    with (
        patch(_SUPERSEDE + ".remove_worktree") as m_remove,
        patch(_SUPERSEDE + ".git.ref_exists", return_value=False),
        patch(_SUPERSEDE + ".git.read_output", return_value="") as m_git,
    ):
        supersede.remove_release_branch(tmp_path, f"release/{_OLD}", phase="prepare")
    m_remove.assert_not_called()
    m_git.assert_called_once_with("ls-remote", "--heads", "origin", f"refs/heads/release/{_OLD}")


def test_prepare_closes_the_superseded_issue_only_after_the_bump_and_the_pr() -> None:
    """A failed close sends the human to --resume, which keeps no bump: it must be pushed first."""
    ctx = _prepare_ctx(_superseding_state(untagged=True))
    ctx.superseded = _superseded(untagged=True)
    ctx.version_override = "patch"
    calls: list[str] = []
    failure = ReleaseError(phase="prepare", command="gh issue close", message="closed?")
    with (
        branch_lookup(),
        patch(_PREPARE + ".version.show", return_value="2.1.5"),
        patch(_PREPARE + ".version.bump", side_effect=lambda *a: calls.append("bump")),
        patch(_PREPARE + ".git.run", side_effect=lambda *a: calls.append(" ".join(a[:2]))),
        patch(_PREPARE + "._generate_changelog"),
        patch(_PREPARE + "._create_pr", side_effect=lambda *a: calls.append("pr") or "pr"),
        patch(_PREPARE + ".close_superseded", side_effect=failure) as m_close,
        pytest.raises(ReleaseError, match="closed"),
    ):
        prepare(ctx)
    m_close.assert_called_once()
    assert calls == ["bump", "add -A", "commit -m", "push -u", "pr"]
    assert ctx.release_pr_url == "pr"
