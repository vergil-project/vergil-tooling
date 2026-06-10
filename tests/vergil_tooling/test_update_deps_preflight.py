from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps import preflight as preflight_mod
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
    monkeypatch.setattr(_MOD + "._today", lambda: "20260610")


def test_today_returns_yyyymmdd() -> None:
    stamp = preflight_mod._today()  # noqa: SLF001
    assert len(stamp) == 8
    assert stamp.isdigit()


def test_preflight_creates_worktree_and_chdirs(monkeypatch) -> None:
    _sync_ok(monkeypatch)
    chdirs: list[Path] = []
    made: dict[str, object] = {}
    wt_path = Path("/tmp/repo/.worktrees/chore-dep-update-20260610")  # noqa: S108

    def _fake_create(root, *, branch, base):  # noqa: ARG001
        made.update(branch=branch, base=base)
        return wt_path

    monkeypatch.setattr(_MOD + ".create_worktree", _fake_create)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(Path(p)))
    ctx = preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.repo == "owner/repo"
    assert ctx.branch == "chore/dep-update-20260610"
    assert made == {"branch": "chore/dep-update-20260610", "base": "develop"}
    assert ctx.worktree_path == wt_path
    assert chdirs == [wt_path]


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
