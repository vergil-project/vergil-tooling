"""Tests for vergil_tooling.lib.live_branches."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.live_branches import (
    RepoSetError,
    default_repo_roots,
    discover_repos,
    live_branches,
    repo_live_branches,
)
from vergil_tooling.lib.platform_env import Platform

_MOD = "vergil_tooling.lib.live_branches"
_GITHUB_ORIGIN = "https://github.com/org/repo.git"


# -- default_repo_roots ---------------------------------------------------------


def test_cloud_vm_default_is_the_checkout_volume() -> None:
    with patch(_MOD + ".current_platform", return_value=Platform.CLOUD_VM):
        assert default_repo_roots() == [Path("/vergil/projects")]


def test_lima_default_is_the_projects_mount_above_the_repo(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    with (
        patch(_MOD + ".current_platform", return_value=Platform.LOCAL_VM),
        patch(_MOD + ".git.main_worktree_root", return_value=projects / "org" / "repo"),
        patch.object(Path, "is_mount", return_value=True),
    ):
        assert default_repo_roots() == [projects]


def test_lima_refuses_when_the_projects_dir_is_not_a_mount(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".current_platform", return_value=Platform.LOCAL_VM),
        patch(_MOD + ".git.main_worktree_root", return_value=tmp_path / "org" / "repo"),
        patch.object(Path, "is_mount", return_value=False),
        pytest.raises(RepoSetError, match="is not a mount point"),
    ):
        default_repo_roots()


def test_lima_refuses_outside_a_repo() -> None:
    err = subprocess.CalledProcessError(128, ("git", "rev-parse"))
    with (
        patch(_MOD + ".current_platform", return_value=Platform.LOCAL_VM),
        patch(_MOD + ".git.main_worktree_root", side_effect=err),
        pytest.raises(RepoSetError, match="not inside a git repository"),
    ):
        default_repo_roots()


def test_physical_host_has_no_default() -> None:
    with (
        patch(_MOD + ".current_platform", return_value=Platform.PHYSICAL_HOST),
        pytest.raises(RepoSetError, match="--root"),
    ):
        default_repo_roots()


# -- discover_repos -------------------------------------------------------------


def _repo(path: Path, *, worktree_file: bool = False) -> Path:
    path.mkdir(parents=True)
    if worktree_file:
        (path / ".git").write_text("gitdir: /elsewhere\n")
    else:
        (path / ".git").mkdir()
    return path


def test_discover_finds_org_repo_layout(tmp_path: Path) -> None:
    a = _repo(tmp_path / "org1" / "a")
    b = _repo(tmp_path / "org2" / "b", worktree_file=True)
    (tmp_path / "org1" / "not-a-repo").mkdir()
    (tmp_path / "stray.txt").write_text("")
    assert discover_repos([tmp_path]) == [a.resolve(), b.resolve()]


def test_discover_accepts_a_repo_or_a_flat_dir_as_root(tmp_path: Path) -> None:
    a = _repo(tmp_path / "flat" / "a")
    b = _repo(tmp_path / "solo")
    assert discover_repos([tmp_path / "flat", b, b]) == sorted([a.resolve(), b.resolve()])


def test_discover_does_not_descend_into_a_repo_or_past_depth_two(tmp_path: Path) -> None:
    outer = _repo(tmp_path / "org" / "outer")
    _repo(outer / ".worktrees" / "issue-1-x", worktree_file=True)
    _repo(tmp_path / "a" / "b" / "too-deep")
    assert discover_repos([tmp_path]) == [outer.resolve()]


def test_discover_refuses_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(RepoSetError, match="does not exist"):
        discover_repos([tmp_path / "typo"])


def test_discover_empty_root_finds_nothing(tmp_path: Path) -> None:
    assert discover_repos([tmp_path]) == []


# -- live branches ----------------------------------------------------------------


def test_repo_live_branches_unions_local_and_origin(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".git.local_branches", return_value={"develop", "feature/1-a"}) as m_local,
        patch(_MOD + ".git.remote_names", return_value={"origin"}),
        patch(_MOD + ".git.remote_url", return_value=_GITHUB_ORIGIN),
        patch(_MOD + ".git.remote_branches", return_value={"develop", "feature/2-b"}) as m_rem,
    ):
        live = repo_live_branches(tmp_path)
    assert live == {"develop", "feature/1-a", "feature/2-b"}
    m_local.assert_called_once_with(tmp_path)
    m_rem.assert_called_once_with("origin", tmp_path)


def test_repo_without_origin_contributes_local_branches(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".git.local_branches", return_value={"main"}),
        patch(_MOD + ".git.remote_names", return_value={"upstream"}),
        patch(_MOD + ".git.remote_branches") as m_rem,
    ):
        assert repo_live_branches(tmp_path) == {"main"}
    m_rem.assert_not_called()


@pytest.mark.parametrize(
    "url", ["https://gitlab.com/someone/repo.git", "git@gitlab.com:someone/repo.git", "/srv/r.git"]
)
def test_origin_off_github_reads_tracking_refs_without_the_network(
    tmp_path: Path, url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(_MOD + ".git.local_branches", return_value={"main"}),
        patch(_MOD + ".git.remote_names", return_value={"origin"}),
        patch(_MOD + ".git.remote_url", return_value=url) as m_url,
        patch(_MOD + ".git.remote_branches") as m_rem,
        patch(
            _MOD + ".git.remote_tracking_branches", return_value={"feature/4-tracked"}
        ) as m_tracking,
    ):
        assert repo_live_branches(tmp_path) == {"main", "feature/4-tracked"}
    m_url.assert_called_once_with("origin", tmp_path)
    m_rem.assert_not_called()
    m_tracking.assert_called_once_with("origin", tmp_path)
    err_out = capsys.readouterr().err
    assert f"warning: origin of {tmp_path} is not on GitHub" in err_out
    assert "refs/remotes/origin/*" in err_out
    assert url not in err_out  # a URL may embed credentials


def test_unreadable_origin_falls_back_to_tracking_refs_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    err = subprocess.CalledProcessError(
        128, ("git", "ls-remote"), stderr="remote: Repository not found.\n"
    )
    with (
        patch(_MOD + ".git.local_branches", return_value={"develop"}),
        patch(_MOD + ".git.remote_names", return_value={"origin"}),
        patch(_MOD + ".git.remote_url", return_value=_GITHUB_ORIGIN),
        patch(_MOD + ".git.remote_branches", side_effect=err),
        patch(
            _MOD + ".git.remote_tracking_branches", return_value={"feature/7-tracked"}
        ) as m_tracking,
    ):
        assert repo_live_branches(tmp_path) == {"develop", "feature/7-tracked"}
    m_tracking.assert_called_once_with("origin", tmp_path)
    err_out = capsys.readouterr().err
    assert f"warning: cannot read origin of {tmp_path}" in err_out
    assert "Repository not found" in err_out
    assert "refs/remotes/origin/*" in err_out


def test_unreadable_tracking_refs_propagate(tmp_path: Path) -> None:
    err = subprocess.CalledProcessError(128, ("git", "ls-remote"))
    with (
        patch(_MOD + ".git.local_branches", return_value={"develop"}),
        patch(_MOD + ".git.remote_names", return_value={"origin"}),
        patch(_MOD + ".git.remote_url", return_value=_GITHUB_ORIGIN),
        patch(_MOD + ".git.remote_branches", side_effect=err),
        patch(_MOD + ".git.remote_tracking_branches", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        repo_live_branches(tmp_path)


def test_unreadable_local_branches_propagate(tmp_path: Path) -> None:
    err = subprocess.CalledProcessError(128, ("git", "branch"))
    with (
        patch(_MOD + ".git.local_branches", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        repo_live_branches(tmp_path)


def test_live_branches_maps_each_repo(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    with patch(
        _MOD + ".repo_live_branches",
        side_effect=lambda repo: frozenset({repo.name}),
    ):
        assert live_branches([a, b]) == {a: frozenset({"a"}), b: frozenset({"b"})}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_repo_live_branches_against_real_git(tmp_path: Path) -> None:
    # End to end through git -C: a local bare origin holds a branch the clone
    # never checked out, and the clone holds a local-only branch.
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _git("init", "-q", "-b", "develop", cwd=seed)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=seed)
    _git("branch", "feature/9-remote-only", cwd=seed)
    _git("clone", "-q", "--bare", str(seed), str(origin), cwd=tmp_path)
    clone = tmp_path / "org" / "clone"
    _git("clone", "-q", str(origin), str(clone), cwd=tmp_path)
    _git("branch", "feature/8-local-only", cwd=clone)

    # The local-path origin stands in for GitHub so ls-remote really runs.
    with (
        patch(_MOD + ".github.is_github_url", return_value=True),
        patch("vergil_tooling.lib.git.github.repo_installation_token", return_value=None),
    ):
        live = repo_live_branches(clone)
    assert live == {"develop", "feature/8-local-only", "feature/9-remote-only"}

    # The origin disappears: ls-remote fails, and the clone's tracking refs
    # (still holding the remote-only branch) stand in, with a warning.
    origin.rename(tmp_path / "gone.git")
    with (
        patch(_MOD + ".github.is_github_url", return_value=True),
        patch("vergil_tooling.lib.git.github.repo_installation_token", return_value=None),
        patch("vergil_tooling.lib.git.time.sleep"),  # the failure may look transient
    ):
        assert repo_live_branches(clone) == live

    # Taken as what it is — an origin off GitHub — it is never read over the
    # network: the tracking refs answer, and no token is asked for.
    with patch("vergil_tooling.lib.git.github.repo_installation_token") as m_token:
        assert repo_live_branches(clone) == live
    m_token.assert_not_called()
