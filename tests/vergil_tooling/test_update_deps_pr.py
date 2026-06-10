from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.update_deps.context import UpdateDepsContext
from vergil_tooling.lib.update_deps.pr import build_pr_body, cleanup_worktree, merge_pr, prepare_pr
from vergil_tooling.lib.update_deps.updater import UpdateResult

_MOD = "vergil_tooling.lib.update_deps.pr"


def _ctx() -> UpdateDepsContext:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    ctx.branch = "chore/dep-update-20260610"
    ctx.worktree_path = Path("/tmp/r/.worktrees/chore-dep-update-20260610")  # noqa: S108
    ctx.results = [
        UpdateResult(
            updater="python-uv",
            changed=True,
            summary="uv lock --upgrade",
            commit_message="m",
        )
    ]
    return ctx


def test_build_pr_body_lists_changed_updaters() -> None:
    body = build_pr_body(_ctx())
    assert "python-uv" in body
    assert "uv lock --upgrade" in body
    assert "Ref #1379" in body


def test_prepare_pr_pushes_and_creates(monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    monkeypatch.setattr(_MOD + ".github.create_pr", lambda **kw: "https://x/pr/1")
    ctx = _ctx()
    prepare_pr(ctx)
    assert ("push", "-u", "origin", "chore/dep-update-20260610") in runs
    assert ctx.pr_url == "https://x/pr/1"


def test_merge_pr_calls_wait_and_merge(monkeypatch) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        _MOD + ".pr_merge.wait_and_merge",
        lambda pr, *, strategy, wait_checks=None: seen.update(pr=pr, strategy=strategy),
    )
    ctx = _ctx()
    ctx.pr_url = "https://x/pr/1"
    merge_pr(ctx)
    assert seen == {"pr": "https://x/pr/1", "strategy": "merge"}


def test_cleanup_worktree_chdir_remove_delete(monkeypatch) -> None:
    chdirs: list[Path] = []
    removed: list[Path] = []
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(Path(p)))
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p: removed.append(p))
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    ctx = _ctx()
    cleanup_worktree(ctx)
    assert chdirs == [Path("/tmp/r")]  # noqa: S108
    assert removed == [ctx.worktree_path]
    assert ("branch", "-D", "chore/dep-update-20260610") in runs
