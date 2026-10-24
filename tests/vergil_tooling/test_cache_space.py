"""Tests for vergil_tooling.lib.cache_space."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.cache_space import (
    DEFAULT_PRUNE_FREE_GB,
    PRUNE_FREE_GB_ENV,
    CacheSpaceError,
    ensure_build_space,
    free_gb,
    image_store_path,
    prune_threshold_gb,
)
from vergil_tooling.lib.live_branches import RepoSetError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from unittest.mock import MagicMock

_MOD = "vergil_tooling.lib.cache_space"
_STORE = Path("/var/lib/store")
_ROOT = Path("/projects")
_REPO = Path("/projects/org/a")
_GONE = "ghcr.io/vergil-project/prod-python:3.14--feature-3-gone--89abcdef"


def _done(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _no_threshold_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRUNE_FREE_GB_ENV, raising=False)


# -- prune_threshold_gb -------------------------------------------------------


def test_threshold_defaults_when_unset_or_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    assert prune_threshold_gb() == DEFAULT_PRUNE_FREE_GB
    monkeypatch.setenv(PRUNE_FREE_GB_ENV, "  ")
    assert prune_threshold_gb() == DEFAULT_PRUNE_FREE_GB


@pytest.mark.parametrize(("raw", "expected"), [("25", 25.0), ("2.5", 2.5), ("0", 0.0)])
def test_threshold_reads_the_env(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: float
) -> None:
    monkeypatch.setenv(PRUNE_FREE_GB_ENV, raw)
    assert prune_threshold_gb() == expected


@pytest.mark.parametrize("raw", ["ten", "-1", "nan"])
def test_threshold_rejects_a_bad_value(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    # A typo must stop the build, never silently change or disable the prune.
    monkeypatch.setenv(PRUNE_FREE_GB_ENV, raw)
    with pytest.raises(CacheSpaceError, match=PRUNE_FREE_GB_ENV):
        prune_threshold_gb()


# -- image_store_path ---------------------------------------------------------


def test_docker_store_is_its_root_dir() -> None:
    with patch(f"{_MOD}.subprocess.run", return_value=_done(stdout="/var/lib/docker\n")) as run:
        assert image_store_path("docker") == Path("/var/lib/docker")
    assert run.call_args.args[0] == ["docker", "info", "--format", "{{.DockerRootDir}}"]


def test_rootless_nerdctl_store_is_under_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "/data")
    out = '["name=seccomp,profile=builtin","name=rootless"]\n'
    with patch(f"{_MOD}.subprocess.run", return_value=_done(stdout=out)):
        assert image_store_path("nerdctl") == Path("/data/containerd")


def test_rootless_nerdctl_store_defaults_under_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with patch(f"{_MOD}.subprocess.run", return_value=_done(stdout='["name=rootless"]')):
        assert image_store_path("nerdctl") == tmp_path / ".local" / "share" / "containerd"


@pytest.mark.parametrize("out", ['["name=seccomp,profile=builtin"]', "null"])
def test_rootful_nerdctl_store(out: str) -> None:
    with patch(f"{_MOD}.subprocess.run", return_value=_done(stdout=out)):
        assert image_store_path("nerdctl") == Path("/var/lib/containerd")


def test_store_lookup_failure_raises() -> None:
    with (
        patch(f"{_MOD}.subprocess.run", return_value=_done(returncode=1, stderr="daemon down")),
        pytest.raises(RuntimeError, match="daemon down"),
    ):
        image_store_path("docker")


def test_store_lookup_failure_without_output_raises() -> None:
    with (
        patch(f"{_MOD}.subprocess.run", return_value=_done(returncode=1)),
        pytest.raises(RuntimeError, match="no output"),
    ):
        image_store_path("docker")


# -- free_gb ------------------------------------------------------------------


def test_free_gb_reads_the_filesystem() -> None:
    usage = type("Usage", (), {"free": 9_500_000_000})()
    with patch(f"{_MOD}.shutil.disk_usage", return_value=usage) as disk:
        assert free_gb(_STORE) == 9.5
    disk.assert_called_once_with(_STORE)


# -- ensure_build_space -------------------------------------------------------


class _Fakes:
    """The patched seams of :func:`ensure_build_space`."""

    def __init__(self, free: MagicMock, prune: MagicMock, roots: MagicMock) -> None:
        self.free = free
        self.prune = prune
        self.roots = roots


@pytest.fixture
def fakes() -> Iterator[_Fakes]:
    with (
        patch(f"{_MOD}.image_store_path", return_value=_STORE),
        patch(f"{_MOD}.free_gb") as free,
        patch(f"{_MOD}.default_repo_roots", return_value=[_ROOT]) as roots,
        patch(f"{_MOD}.discover_repos", return_value=[_REPO]),
        patch(f"{_MOD}.prune_finished_images", return_value=([_GONE], [])) as prune,
    ):
        yield _Fakes(free, prune, roots)


def test_enough_space_is_silent(fakes: _Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.free.return_value = 10.0
    ensure_build_space("nerdctl")
    fakes.prune.assert_not_called()
    assert capsys.readouterr() == ("", "")


def test_zero_threshold_turns_the_check_off(fakes: _Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PRUNE_FREE_GB_ENV, "0")
    ensure_build_space("nerdctl")
    fakes.free.assert_not_called()
    fakes.prune.assert_not_called()


def test_low_space_prunes_and_reports_what_it_removed(
    fakes: _Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.free.side_effect = [4.0, 12.0]
    ensure_build_space("nerdctl")
    fakes.prune.assert_called_once_with([_REPO], runtime="nerdctl")
    out = capsys.readouterr().out
    assert f"Low disk: 4.0 GB free under {_STORE}" in out
    assert "Auto-prune removed 1 cached image(s)" in out
    assert f"removed {_GONE}" in out
    assert "Free space now 12.0 GB." in out


def test_threshold_comes_from_the_env(fakes: _Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PRUNE_FREE_GB_ENV, "20")
    fakes.free.side_effect = [15.0, 25.0]
    ensure_build_space("nerdctl")
    fakes.prune.assert_called_once()


def test_still_low_but_buildable_warns_and_continues(
    fakes: _Fakes, capsys: pytest.CaptureFixture[str]
) -> None:
    fakes.free.side_effect = [4.0, 5.0]
    ensure_build_space("nerdctl")
    err = capsys.readouterr().err
    assert "WARNING: still only 5.0 GB free" in err
    assert "building anyway" in err
    assert "vrg-container-cache prune --apply" in err


def test_too_little_space_after_prune_fails_with_the_numbers(fakes: _Fakes) -> None:
    fakes.free.side_effect = [2.0, 2.5]
    with pytest.raises(CacheSpaceError) as excinfo:
        ensure_build_space("nerdctl")
    message = str(excinfo.value)
    assert "2.5 GB is free" in message
    assert "at least 3 GB" in message
    assert "vrg-container-cache prune --apply" in message
    assert "auto-prune failed" not in message


def test_floor_never_exceeds_a_lower_threshold(
    fakes: _Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A threshold under the build floor is itself the floor.
    monkeypatch.setenv(PRUNE_FREE_GB_ENV, "2")
    fakes.free.side_effect = [1.0, 1.5]
    with pytest.raises(CacheSpaceError, match="at least 2 GB"):
        ensure_build_space("nerdctl")


def test_no_repo_set_does_not_guess(fakes: _Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    # The physical host has no default repo set: the prune is skipped, loudly,
    # and the build still goes ahead when there is room for it.
    fakes.roots.side_effect = RepoSetError("no default repo set on the physical host")
    fakes.free.side_effect = [5.0, 5.0]
    ensure_build_space("docker")
    fakes.prune.assert_not_called()
    err = capsys.readouterr().err
    assert "WARNING: cannot auto-prune cached images: no default repo set" in err
    assert "still only 5.0 GB free" in err


def test_no_repo_set_and_no_room_fails(fakes: _Fakes) -> None:
    fakes.roots.side_effect = RepoSetError("no default repo set on the physical host")
    fakes.free.side_effect = [1.0, 1.0]
    with pytest.raises(CacheSpaceError, match="the auto-prune failed and 1.0 GB is free"):
        ensure_build_space("docker")
    fakes.prune.assert_not_called()


def test_empty_repo_set_does_not_prune(fakes: _Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.free.side_effect = [5.0, 5.0]
    with patch(f"{_MOD}.discover_repos", return_value=[]):
        ensure_build_space("nerdctl")
    fakes.prune.assert_not_called()
    assert f"no git repositories under {_ROOT}" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("'nerdctl images' failed: daemon down"),
        subprocess.CalledProcessError(128, ["git", "branch"], stderr="not a git repository"),
    ],
)
def test_prune_failure_is_loud(
    fakes: _Fakes, capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    fakes.prune.side_effect = error
    fakes.free.side_effect = [5.0, 5.0]
    ensure_build_space("nerdctl")
    assert "WARNING: auto-prune of cached images failed:" in capsys.readouterr().err


def test_rmi_failures_are_loud(fakes: _Fakes, capsys: pytest.CaptureFixture[str]) -> None:
    fakes.prune.return_value = ([], [(_GONE, "image is in use")])
    fakes.free.side_effect = [2.0, 2.0]
    with pytest.raises(CacheSpaceError, match="the auto-prune failed"):
        ensure_build_space("nerdctl")
    assert f"WARNING: auto-prune could not remove {_GONE}: image is in use" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("'docker info' failed: daemon down"),
        FileNotFoundError(2, "No such file or directory"),
        ValueError("Expecting value"),
    ],
)
def test_unmeasurable_store_skips_with_a_note(
    capsys: pytest.CaptureFixture[str], error: Exception
) -> None:
    # Docker Desktop keeps its store inside its own VM: nothing to measure here.
    with (
        patch(f"{_MOD}.image_store_path", side_effect=error),
        patch(f"{_MOD}.prune_finished_images") as prune,
    ):
        ensure_build_space("docker")
    prune.assert_not_called()
    assert "skipping the low-disk auto-prune" in capsys.readouterr().err
