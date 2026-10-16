from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.preflight import preflight

_MOD = "vergil_tooling.lib.update_deps.preflight"


def _sync_ok(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(
        _MOD + ".git.read_output",
        lambda *a: "deadbeef" if "rev-parse" in a else "",
    )
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    monkeypatch.setattr(_MOD + ".version.show", lambda root: "2.1.243")
    monkeypatch.setattr(_MOD + ".git.ref_exists", lambda ref: False)
    monkeypatch.setattr(_MOD + ".git.remote_branches", lambda: {"develop", "main"})


def test_preflight_creates_worktree_and_chdirs(monkeypatch) -> None:
    _sync_ok(monkeypatch)
    chdirs: list[Path] = []
    made: dict[str, object] = {}
    wt_path = Path("/tmp/repo/.worktrees/deps-update-2.1.243")  # noqa: S108

    def _fake_create(root, *, branch, base):  # noqa: ARG001
        made.update(branch=branch, base=base)
        return wt_path

    monkeypatch.setattr(_MOD + ".create_worktree", _fake_create)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(Path(p)))
    ctx = preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.repo == "owner/repo"
    assert ctx.branch == "deps/update-2.1.243"
    assert ctx.version == "2.1.243"
    assert made == {"branch": "deps/update-2.1.243", "base": "develop"}
    assert ctx.worktree_path == wt_path
    assert chdirs == [wt_path]


def test_preflight_wraps_worktree_error(monkeypatch) -> None:
    """A ManagedWorktreeError from the shared helper is translated into the
    pipeline's own UpdateDepsError at the preflight boundary."""
    from vergil_tooling.lib.managed_worktree import ManagedWorktreeError

    _sync_ok(monkeypatch)

    def _raise(root, *, branch, base):  # noqa: ARG001
        raise ManagedWorktreeError("path already exists")

    monkeypatch.setattr(_MOD + ".create_worktree", _raise)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: None)
    with pytest.raises(UpdateDepsError, match="path already exists"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_existing_deps_branch_fails_loud(monkeypatch) -> None:
    """A second off-cycle run on the same develop version must not reuse or
    clobber the in-flight deps/update-<version> branch (Review Focus 2)."""
    from vergil_tooling.lib.managed_worktree import ManagedWorktreeError

    _sync_ok(monkeypatch)

    def collide(root, *, branch, base):  # noqa: ARG001
        msg = f"branch {branch} already exists"
        raise ManagedWorktreeError(msg)

    monkeypatch.setattr(_MOD + ".create_worktree", collide)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: None)
    with pytest.raises(UpdateDepsError, match=r"deps/update-2\.1\.243 already exists") as info:
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert "dependency-update PR" in str(info.value)


def test_preflight_existing_local_deps_branch_fails_loud(monkeypatch) -> None:
    _sync_ok(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(_MOD + ".git.ref_exists", lambda ref: seen.append(ref) or True)
    monkeypatch.setattr(_MOD + ".create_worktree", lambda *a, **kw: pytest.fail("created"))
    with pytest.raises(UpdateDepsError, match=r"deps/update-2\.1\.243 already exists"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert seen == ["refs/heads/deps/update-2.1.243"]


def test_preflight_deps_branch_on_origin_fails_loud(monkeypatch) -> None:
    """A failed run deletes its local branch; the PR head survives on origin."""
    _sync_ok(monkeypatch)
    monkeypatch.setattr(_MOD + ".git.remote_branches", lambda: {"develop", "deps/update-2.1.243"})
    monkeypatch.setattr(_MOD + ".create_worktree", lambda *a, **kw: pytest.fail("created"))
    with pytest.raises(UpdateDepsError, match=r"deps/update-2\.1\.243 already exists"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_non_develop(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "feature/x")
    with pytest.raises(UpdateDepsError, match="Must be on develop"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_dirty_tree(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: " M file.py")
    with pytest.raises(UpdateDepsError, match="not clean"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_out_of_sync_develop(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    outs = iter(["", "local-sha", "remote-sha"])  # status, HEAD, origin/develop
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: next(outs))
    with pytest.raises(UpdateDepsError, match="sync"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
