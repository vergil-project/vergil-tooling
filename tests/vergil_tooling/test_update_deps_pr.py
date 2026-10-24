from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vergil_tooling.lib import pr_merge
from vergil_tooling.lib.update_deps import pr
from vergil_tooling.lib.update_deps.context import (
    DepsFailure,
    FailureKind,
    UpdateDepsContext,
    UpdateDepsError,
)
from vergil_tooling.lib.update_deps.failure import failure_summary, tail_output
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult

_MOD = "vergil_tooling.lib.update_deps.pr"


def _ctx() -> UpdateDepsContext:
    ctx = UpdateDepsContext(
        repo="o/r",
        repo_root=Path("/tmp/r"),  # noqa: S108
        version="2.1.243",
        branch="deps/update-2.1.243",
        worktree_path=Path("/tmp/r/.worktrees/deps-update-2.1.243"),  # noqa: S108
    )
    ctx.results = [
        UpdateResult(
            updater="python",
            changed=True,
            summary="uv lock --upgrade",
            commit_message="m",
            held_back=[HeldBack(source="python", name="p", current="1", latest="2")],
        )
    ]
    return ctx


def _failed(kind: FailureKind = "validation") -> DepsFailure:
    return DepsFailure(
        kind=kind,
        message="m",
        output_tail="boom",
        reproduce="vrg-container-run -- vrg-validate",
    )


def test_fix_task_title() -> None:
    assert pr.fix_task_title("2.1.243") == "Fix failed dependency update for 2.1.243"


def test_pr_body_has_updates_and_report_and_no_stale_ref() -> None:
    body = pr.build_pr_body(_ctx())
    assert "**python** — uv lock --upgrade" in body
    assert "## Held back by declared bounds" in body
    assert "| python | p | 1 | 2 |  |" in body
    assert "#1379" not in body
    assert "## Failure" not in body
    assert pr.FIX_TASK_PREFIX not in body


def test_pr_body_includes_warnings_and_skips_unchanged() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    ctx.results = [
        UpdateResult(updater="a", changed=False, summary="", commit_message="", warnings=["w1"]),
        UpdateResult(updater="b", changed=True, summary="did b", commit_message="m"),
    ]
    body = pr.build_pr_body(ctx)
    assert "## Warnings" in body
    assert "- w1" in body
    assert "did b" in body
    assert "**a**" not in body
    assert "No dependencies are held back" in body


def test_pr_body_includes_failure_and_fix_task() -> None:
    ctx = _ctx()
    ctx.failure = _failed()
    ctx.fix_issue_url = "https://github.com/o/r/issues/10"
    body = pr.build_pr_body(ctx)
    assert "## Failure: validation" in body
    assert "\nFix task: https://github.com/o/r/issues/10\n" in body


def test_pr_body_stays_under_github_limit() -> None:
    """Review Focus 1: an enormous failure and report still fit GitHub's limit."""
    ctx = _ctx()
    ctx.results[0].held_back = [
        HeldBack(
            source="python",
            name=f"pkg{i}",
            current="1.0.0",
            latest="2.0.0",
            note="pyproject: pkg>=1,<2",
        )
        for i in range(2000)
    ]
    huge = "\n".join("E" * 300 for _ in range(10_000))
    ctx.failure = DepsFailure(
        kind="validation", message="m", output_tail=tail_output(huge), reproduce="r"
    )
    ctx.fix_issue_url = "https://github.com/o/r/issues/10"
    assert len(pr.build_pr_body(ctx)) < 65_536


def test_prepare_pr_pushes_ensures_label_and_labels_pr(monkeypatch) -> None:
    runs: list[list[str]] = []
    git_runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".ensure_labels", lambda repo, names: runs.append([repo, *names]))
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: git_runs.append(a))
    created: dict[str, object] = {}

    def fake_create(**kw: object) -> str:
        created.update(kw)
        created["body"] = Path(str(kw["body_file"])).read_text(encoding="utf-8")
        return "https://github.com/o/r/pull/9"

    monkeypatch.setattr(_MOD + ".github.create_pr", fake_create)
    ctx = _ctx()
    pr.prepare_pr(ctx)
    assert git_runs == [("push", "-u", "origin", "deps/update-2.1.243")]
    assert runs == [["o/r", "dependency-update"]]
    assert created["labels"] == ["dependency-update"]
    assert created["base"] == "develop"
    assert created["title"] == "chore(deps): dependency update for 2.1.243"
    assert "uv lock --upgrade" in str(created["body"])
    assert not Path(str(created["body_file"])).exists()
    assert ctx.pr_url == "https://github.com/o/r/pull/9"


def test_prepare_pr_raises_without_branch() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    with pytest.raises(UpdateDepsError, match="No branch"):
        pr.prepare_pr(ctx)


def test_merge_pr_calls_wait_and_merge(monkeypatch) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        _MOD + ".pr_merge.wait_and_merge",
        lambda url, *, strategy, wait_checks=None: seen.update(pr=url, strategy=strategy),
    )
    deleted: list[str] = []
    monkeypatch.setattr(_MOD + ".delete_merged_branch", lambda ctx: deleted.append(ctx.branch))
    ctx = _ctx()
    ctx.pr_url = "https://x/pr/1"
    pr.merge_pr(ctx)
    assert seen == {"pr": "https://x/pr/1", "strategy": "merge"}
    assert ctx.failure is None
    assert deleted == ["deps/update-2.1.243"]


@pytest.mark.parametrize(
    ("existed", "message"),
    [
        (True, "Deleted merged branch deps/update-2.1.243 on origin."),
        (False, "Branch deps/update-2.1.243 already deleted on origin."),
    ],
)
def test_delete_merged_branch_deletes_on_origin(monkeypatch, capsys, existed, message) -> None:
    """The merged branch is deleted as its own API step, not gh pr merge --delete-branch."""
    endpoints: list[str] = []
    monkeypatch.setattr(
        _MOD + ".github.delete_if_exists", lambda ep: endpoints.append(ep) or existed
    )
    pr.delete_merged_branch(_ctx())
    assert endpoints == ["repos/o/r/git/refs/heads/deps/update-2.1.243"]
    assert message in capsys.readouterr().out


def test_delete_merged_branch_failure_warns_without_failing(monkeypatch, capsys) -> None:
    def _fail(ep: str) -> bool:
        raise subprocess.CalledProcessError(1, ["gh", "api", ep])

    monkeypatch.setattr(_MOD + ".github.delete_if_exists", _fail)
    ctx = _ctx()
    ctx.pr_url = "https://x/pr/1"
    pr.delete_merged_branch(ctx)
    out = capsys.readouterr().out
    assert "WARNING: merged https://x/pr/1, but could not delete" in out
    assert "git push origin --delete deps/update-2.1.243" in out
    assert ctx.failure is None


def test_merge_pr_raises_without_pr_url() -> None:
    ctx = _ctx()
    ctx.pr_url = None
    with pytest.raises(UpdateDepsError, match="No PR URL"):
        pr.merge_pr(ctx)


def test_merge_pr_records_abort_instead_of_raising(monkeypatch) -> None:
    def _abort(url, *, strategy, wait_checks=None):  # noqa: ARG001
        msg = "nope"
        raise pr_merge.MergeAbortError(msg)

    recorded: list[str] = []
    monkeypatch.setattr(_MOD + ".pr_merge.wait_and_merge", _abort)
    monkeypatch.setattr(_MOD + ".record_merge_failure", lambda ctx, msg: recorded.append(msg))
    ctx = _ctx()
    ctx.pr_url = "https://x/pr/1"
    pr.merge_pr(ctx)
    assert recorded == ["nope"]


def test_record_merge_failure_lists_failed_checks(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".github.failed_check_names", lambda url: ["test / 3.12"])
    edits: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _MOD + ".github.edit_pr_body", lambda url, *, body: edits.append((url, body))
    )
    ctx = _ctx()
    ctx.pr_url = "https://github.com/o/r/pull/9"
    pr.record_merge_failure(ctx, "required checks failed")
    assert ctx.failure is not None
    assert ctx.failure.kind == "merge"
    assert "required checks failed" in ctx.failure.output_tail
    assert "- test / 3.12" in ctx.failure.output_tail
    assert ctx.failure.reproduce == "https://github.com/o/r/pull/9/checks"
    assert edits[0][0] == "https://github.com/o/r/pull/9"
    assert "## Failure: merge" in edits[0][1]


def test_record_merge_failure_without_failed_checks(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".github.failed_check_names", lambda url: [])
    monkeypatch.setattr(_MOD + ".github.edit_pr_body", lambda url, *, body: None)
    ctx = _ctx()
    ctx.pr_url = "https://github.com/o/r/pull/9"
    pr.record_merge_failure(ctx, "merge conflict")
    assert ctx.failure is not None
    assert ctx.failure.output_tail == "merge conflict"


def test_record_merge_failure_raises_without_pr_url() -> None:
    ctx = _ctx()
    with pytest.raises(UpdateDepsError, match="No PR URL"):
        pr.record_merge_failure(ctx, "x")


def test_fix_task_body_explains_branch_and_reproduce() -> None:
    ctx = _ctx()
    ctx.pr_url = "https://github.com/o/r/pull/9"
    ctx.failure = _failed()
    body = pr.build_fix_task_body(ctx)
    assert "https://github.com/o/r/pull/9" in body
    assert "chore/<this issue number>-dep-update-2.1.243" in body
    assert "origin/deps/update-2.1.243" in body
    assert "boom" in body
    assert "`vrg-container-run -- vrg-validate`" in body


def test_fix_task_body_says_release_closes_the_pr_not_github() -> None:
    # Squash-merged fix PRs never mark the deps PR merged (#3286): the next
    # vrg-release preflight closes it once this fix task closes as completed.
    ctx = _ctx()
    ctx.pr_url = "https://github.com/o/r/pull/9"
    ctx.failure = _failed()
    body = " ".join(pr.build_fix_task_body(ctx).split())
    assert "marks the dependency-update PR merged" not in body
    assert "the next `vrg-release` closes the dependency-update PR and the gate clears" in body


def _update_failed(updater: str | None = "hook") -> DepsFailure:
    return DepsFailure(
        kind="update",
        message="hook: exit 3",
        output_tail="hook trace",
        reproduce="./scripts/deps-hook.sh",
        updater=updater,
    )


def test_update_fix_task_body_stands_alone() -> None:
    # No PR exists for an update failure (#3550): the task names the updater,
    # the error and how to reproduce it, and points at develop, not a deps branch.
    ctx = _ctx()
    ctx.failure = _update_failed()
    body = " ".join(pr.build_fix_task_body(ctx).split())
    assert "failed in the `hook` updater, before validation" in body
    assert "no PR was opened" in body
    assert "None" not in body
    assert "origin/deps/update" not in body
    assert "`chore/<this issue number>-dep-update-2.1.243` from `origin/develop`" in body
    assert "re-run `vrg-update-deps --only hook`" in body
    assert "hook: exit 3" in body
    assert "hook trace" in body
    assert "Reproduce on `develop`: `./scripts/deps-hook.sh`" in body


def test_update_fix_task_body_without_updater_name() -> None:
    ctx = _ctx()
    ctx.failure = _update_failed(updater=None)
    body = " ".join(pr.build_fix_task_body(ctx).split())
    assert "failed in an updater, before validation" in body
    assert "re-run `vrg-update-deps` from a clean `develop`" in body


def _no_open_tasks(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".github.read_json", lambda *a: [])


def test_file_fix_task_for_update_failure_needs_no_pr(monkeypatch) -> None:
    _no_open_tasks(monkeypatch)
    bodies: list[str] = []

    def fake_run(cmd, **_kw):
        bodies.append(Path(cmd[cmd.index("--body-file") + 1]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Created https://github.com/o/r/issues/11, linked under epic o/.github#5.\n",
            stderr="",
        )

    def no_edit(url, *, body):  # noqa: ARG001
        msg = "an update failure has no PR body to edit"
        raise AssertionError(msg)

    monkeypatch.setattr(_MOD + ".subprocess.run", fake_run)
    monkeypatch.setattr(_MOD + ".github.edit_pr_body", no_edit)
    ctx = _ctx()
    ctx.failure = _update_failed()
    pr.file_fix_task(ctx)
    assert ctx.fix_issue_url == "https://github.com/o/r/issues/11"
    assert "`hook` updater" in bodies[0]


def test_fix_task_body_requires_failure() -> None:
    with pytest.raises(ValueError, match="without a recorded failure"):
        pr.build_fix_task_body(_ctx())


def test_file_fix_task_creates_adhoc_issue_and_links_pr(monkeypatch) -> None:
    _no_open_tasks(monkeypatch)
    seen: dict[str, object] = {}
    cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        seen["kw"] = kw
        seen["body"] = Path(cmd[cmd.index("--body-file") + 1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Created https://github.com/o/r/issues/10, linked under epic o/.github#5.\n",
            stderr="",
        )

    monkeypatch.setattr(_MOD + ".subprocess.run", fake_run)
    edits: list[str] = []
    monkeypatch.setattr(_MOD + ".github.edit_pr_body", lambda url, *, body: edits.append(body))
    ctx = _ctx()
    ctx.pr_url = "https://github.com/o/r/pull/9"
    ctx.failure = _failed()
    pr.file_fix_task(ctx)
    [cmd] = cmds
    assert cmd[:5] == ["vrg-issue-create", "--epic", "adhoc", "--repo", "o/r"]
    assert cmd[cmd.index("--title") + 1] == "Fix failed dependency update for 2.1.243"
    assert "origin/deps/update-2.1.243" in str(seen["body"])
    assert not Path(cmd[cmd.index("--body-file") + 1]).exists()
    assert seen["kw"] == {"check": False, "capture_output": True, "text": True}
    assert ctx.fix_issue_url == "https://github.com/o/r/issues/10"
    assert "Fix task: https://github.com/o/r/issues/10" in edits[0]


@pytest.mark.parametrize(
    ("version", "url", "kind"),
    [(None, "u", "validation"), ("2.1.243", None, "validation"), (None, None, "update")],
)
def test_file_fix_task_requires_version_and_pr(version, url, kind) -> None:
    ctx = _ctx()
    ctx.version = version
    ctx.pr_url = url
    ctx.failure = _failed(kind)
    with pytest.raises(UpdateDepsError, match="No version or PR"):
        pr.file_fix_task(ctx)


def test_file_fix_task_without_failure_requires_pr() -> None:
    ctx = _ctx()
    with pytest.raises(UpdateDepsError, match="No version or PR"):
        pr.file_fix_task(ctx)


def test_file_fix_task_requires_failure() -> None:
    ctx = _ctx()
    ctx.pr_url = "u"
    with pytest.raises(ValueError, match="without a recorded failure"):
        pr.file_fix_task(ctx)


def test_file_fix_task_fails_loud_on_error(monkeypatch) -> None:
    _no_open_tasks(monkeypatch)
    monkeypatch.setattr(
        _MOD + ".subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no installation"),
    )
    ctx = _ctx()
    ctx.pr_url = "u"
    ctx.failure = _failed()
    with pytest.raises(UpdateDepsError, match="vrg-issue-create failed") as info:
        pr.file_fix_task(ctx)
    assert info.value.detail == "no installation"


def test_file_fix_task_fails_loud_on_unparseable_output(monkeypatch) -> None:
    _no_open_tasks(monkeypatch)
    monkeypatch.setattr(
        _MOD + ".subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr=""),
    )
    ctx = _ctx()
    ctx.pr_url = "u"
    ctx.failure = _failed()
    with pytest.raises(UpdateDepsError, match="could not find the created issue URL"):
        pr.file_fix_task(ctx)


def test_cleanup_worktree_chdir_remove_delete(monkeypatch) -> None:
    chdirs: list[Path] = []
    removed: list[Path] = []
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(Path(p)))
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p: removed.append(p))
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    ctx = _ctx()
    pr.cleanup_worktree(ctx)
    assert chdirs == [Path("/tmp/r")]  # noqa: S108
    assert removed == [ctx.worktree_path]
    assert ("branch", "-D", "deps/update-2.1.243") in runs


def test_cleanup_worktree_noop_without_path() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    ctx.worktree_path = None
    pr.cleanup_worktree(ctx)  # returns early; no error


def test_cleanup_worktree_skips_branch_delete_when_none(monkeypatch) -> None:
    removed: list[Path] = []
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: None)
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p: removed.append(p))
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    ctx = _ctx()
    ctx.branch = None
    pr.cleanup_worktree(ctx)
    assert removed == [ctx.worktree_path]
    assert runs == []


def _task(number: int, title: str, kind: str) -> dict[str, object]:
    return {
        "number": number,
        "url": f"https://github.com/o/r/issues/{number}",
        "title": title,
        "body": f"Intro.\n\n## Failure: {kind}\n\nm\n",
    }


def test_find_open_fix_task_matches_title_and_failure_kind(monkeypatch) -> None:
    # GitHub search is a broad net: filter for the exact title and the same
    # failure kind, and reuse the oldest match (#3560).
    title = "Fix failed dependency update for 2.1.243"
    calls: list[tuple[str, ...]] = []

    def fake_read_json(*args):
        calls.append(args)
        return [
            _task(30, title, "update"),
            _task(29, "Fix failed dependency update for 2.1.24", "update"),
            _task(28, title, "validation"),
            _task(27, title, "update"),
            "not an issue",
        ]

    monkeypatch.setattr(_MOD + ".github.read_json", fake_read_json)
    found = pr.find_open_fix_task("o/r", "2.1.243", "update")
    assert found == "https://github.com/o/r/issues/27"
    [args] = calls
    assert args[:4] == ("issue", "list", "--repo", "o/r")
    assert args[args.index("--search") + 1] == f'"{title}" in:title'
    assert args[args.index("--state") + 1] == "open"
    assert set(args[args.index("--json") + 1].split(",")) == {"number", "url", "title", "body"}


def test_find_open_fix_task_none_for_other_version_or_kind(monkeypatch) -> None:
    monkeypatch.setattr(
        _MOD + ".github.read_json",
        lambda *a: [
            _task(29, "Fix failed dependency update for 2.1.242", "update"),
            _task(28, "Fix failed dependency update for 2.1.243", "merge"),
        ],
    )
    assert pr.find_open_fix_task("o/r", "2.1.243", "update") is None


def test_find_open_fix_task_fails_loud_when_lookup_fails(monkeypatch) -> None:
    # Never guess: a failed lookup must not silently file a possible duplicate.
    def _fail(*args):
        raise subprocess.CalledProcessError(1, ["gh", *args], stderr="HTTP 502")

    monkeypatch.setattr(_MOD + ".github.read_json", _fail)
    with pytest.raises(UpdateDepsError, match="Could not look for an open fix task") as info:
        pr.find_open_fix_task("o/r", "2.1.243", "update")
    assert "HTTP 502" in (info.value.detail or "")


def test_find_open_fix_task_fails_loud_on_unexpected_json(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".github.read_json", lambda *a: {"message": "odd"})
    with pytest.raises(UpdateDepsError, match="unexpected JSON"):
        pr.find_open_fix_task("o/r", "2.1.243", "update")


def _no_issue_create(cmd, **_kw):
    msg = f"a repeat failure must not file a new fix task: {cmd}"
    raise AssertionError(msg)


def test_file_fix_task_repeat_update_failure_comments_on_open_task(monkeypatch, capsys) -> None:
    # Re-running at the same version after a transient failure (#3560): the
    # open task gets the new failure as a comment, and the summary links it.
    monkeypatch.setattr(
        _MOD + ".github.read_json",
        lambda *a: [_task(11, "Fix failed dependency update for 2.1.243", "update")],
    )
    comments: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".github.run", lambda *a: comments.append(a))
    monkeypatch.setattr(_MOD + ".subprocess.run", _no_issue_create)
    ctx = _ctx()
    ctx.failure = _update_failed()
    pr.file_fix_task(ctx)
    assert ctx.fix_issue_url == "https://github.com/o/r/issues/11"
    [args] = comments
    assert args[:3] == ("issue", "comment", "https://github.com/o/r/issues/11")
    body = args[args.index("--body") + 1]
    assert "failed again" in body
    assert "No PR was opened" in body
    assert "## Failure: update" in body
    assert "hook trace" in body
    assert "Fix task already open" in capsys.readouterr().out
    assert "Fix task: https://github.com/o/r/issues/11." in failure_summary(ctx)


def test_file_fix_task_repeat_validation_failure_links_pr(monkeypatch) -> None:
    # A validation failure after the earlier PR was closed unmerged: the new
    # PR links the existing task, and the comment names the new PR.
    monkeypatch.setattr(
        _MOD + ".github.read_json",
        lambda *a: [_task(12, "Fix failed dependency update for 2.1.243", "validation")],
    )
    comments: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".github.run", lambda *a: comments.append(a))
    monkeypatch.setattr(_MOD + ".subprocess.run", _no_issue_create)
    edits: list[str] = []
    monkeypatch.setattr(_MOD + ".github.edit_pr_body", lambda url, *, body: edits.append(body))
    ctx = _ctx()
    ctx.pr_url = "https://github.com/o/r/pull/40"
    ctx.failure = _failed()
    pr.file_fix_task(ctx)
    assert ctx.fix_issue_url == "https://github.com/o/r/issues/12"
    body = comments[0][comments[0].index("--body") + 1]
    assert "Its PR stays open: https://github.com/o/r/pull/40" in body
    assert "Fix task: https://github.com/o/r/issues/12" in edits[0]


def test_file_fix_task_files_new_task_for_a_different_version(monkeypatch) -> None:
    monkeypatch.setattr(
        _MOD + ".github.read_json",
        lambda *a: [_task(11, "Fix failed dependency update for 2.1.242", "update")],
    )

    def fake_run(cmd, **_kw):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="Created https://github.com/o/r/issues/13, linked under epic o/.github#5.\n",
            stderr="",
        )

    monkeypatch.setattr(_MOD + ".subprocess.run", fake_run)
    ctx = _ctx()
    ctx.failure = _update_failed()
    pr.file_fix_task(ctx)
    assert ctx.fix_issue_url == "https://github.com/o/r/issues/13"
