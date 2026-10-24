"""Tests for vergil_tooling.lib.cache_prune."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.cache_prune import (
    CacheImage,
    images_in_use,
    list_cache_images,
    plan_prune,
    remove_images,
)

_RUN = "vergil_tooling.lib.cache_prune.subprocess.run"
_PY = "ghcr.io/vergil-project/prod-python"


def _done(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _img(branch: str, image_id: str = "aaaa11112222") -> CacheImage:
    return CacheImage(ref=f"{_PY}:3.14--{branch}--0123abcd", image_id=image_id, branch=branch)


# -- list_cache_images ------------------------------------------------------------


def test_lists_only_cached_images() -> None:
    stdout = (
        f"{_PY}:3.14--feature-1-a--0123abcd\tid1\n"
        f"{_PY}:3.14\tid2\n"  # base image
        "docker.io/library/redis:7--x--0123abcd\tid3\n"  # foreign image
        f"{_PY}:<none>\tid4\n"
        "\n"
    )
    with patch(_RUN, return_value=_done(stdout=stdout)) as mock_run:
        images = list_cache_images("nerdctl")
    assert images == [CacheImage(f"{_PY}:3.14--feature-1-a--0123abcd", "id1", "feature-1-a")]
    assert mock_run.call_args.args[0][:2] == ["nerdctl", "images"]


def test_list_failure_raises() -> None:
    with (
        patch(_RUN, return_value=_done(returncode=1, stderr="daemon down")),
        pytest.raises(RuntimeError, match="daemon down"),
    ):
        list_cache_images("docker")


def test_list_failure_without_stderr_still_explains() -> None:
    with (
        patch(_RUN, return_value=_done(returncode=1)),
        pytest.raises(RuntimeError, match="no output"),
    ):
        list_cache_images("docker")


# -- images_in_use -----------------------------------------------------------------


def test_images_in_use_reads_all_containers() -> None:
    with patch(_RUN, return_value=_done(stdout="img:a\nsha256:abc\n")) as mock_run:
        assert images_in_use("docker") == {"img:a", "sha256:abc"}
    assert mock_run.call_args.args[0] == ["docker", "ps", "-a", "--format", "{{.Image}}"]


def test_images_in_use_failure_raises() -> None:
    with (
        patch(_RUN, return_value=_done(returncode=1, stderr="boom")),
        pytest.raises(RuntimeError, match="boom"),
    ):
        images_in_use("docker")


# -- plan_prune ---------------------------------------------------------------------

_REPO_A = Path("/projects/org/a")
_REPO_B = Path("/projects/org/b")


def _verdicts(images: list[CacheImage], in_use: tuple[str, ...] = ()) -> dict[str, tuple[bool, str]]:
    live = {
        _REPO_A: frozenset({"develop", "feature/1-live"}),
        _REPO_B: frozenset({"main", "feature/2-other"}),
    }
    return {v.image.branch: (v.remove, v.reason) for v in plan_prune(images, live, in_use)}


def test_finished_branch_is_removed() -> None:
    remove, reason = _verdicts([_img("feature-3-gone")])["feature-3-gone"]
    assert remove
    assert "matches no live branch in 2 repo(s)" in reason


def test_live_branch_in_any_repo_is_kept_via_its_sanitized_name() -> None:
    verdicts = _verdicts([_img("feature-1-live"), _img("feature-2-other")])
    assert verdicts["feature-1-live"] == (False, "branch 'feature-1-live' live in org/a")
    assert verdicts["feature-2-other"] == (False, "branch 'feature-2-other' live in org/b")


@pytest.mark.parametrize("branch", ["develop", "main", "release"])
def test_protected_branch_is_kept_even_when_not_live(branch: str) -> None:
    live: dict[Path, frozenset[str]] = {_REPO_A: frozenset()}
    [verdict] = plan_prune([_img(branch)], live, ())
    assert not verdict.remove
    assert verdict.reason == f"protected branch '{branch}'"


@pytest.mark.parametrize(
    "used",
    [
        f"{_PY}:3.14--feature-3-gone--0123abcd",  # by ref
        "aaaa1111",  # by a short id
        "sha256:aaaa11112222ffff",  # by a full id
    ],
)
def test_image_used_by_a_container_is_kept(used: str) -> None:
    verdicts = _verdicts([_img("feature-3-gone")], in_use=("other:img", used))
    assert verdicts["feature-3-gone"] == (False, "in use by a container")


def test_unrelated_container_does_not_protect() -> None:
    verdicts = _verdicts([_img("feature-3-gone")], in_use=("bbbb2222",))
    assert verdicts["feature-3-gone"][0]


def test_image_without_id_matches_only_by_ref() -> None:
    image = _img("feature-3-gone", image_id="")
    verdicts = _verdicts([image], in_use=("aaaa1111",))
    assert verdicts["feature-3-gone"][0]


# -- remove_images ----------------------------------------------------------------


def test_remove_images_runs_rmi_without_force() -> None:
    with patch(_RUN, return_value=_done()) as mock_run:
        assert remove_images(["a:1", "b:2"], "nerdctl") == []
    assert [c.args[0] for c in mock_run.call_args_list] == [
        ["nerdctl", "rmi", "a:1"],
        ["nerdctl", "rmi", "b:2"],
    ]


def test_remove_images_reports_every_failure_and_keeps_going() -> None:
    results = [_done(returncode=1, stderr="image is in use"), _done(), _done(returncode=2)]
    with patch(_RUN, side_effect=results) as mock_run:
        failures = remove_images(["a:1", "b:2", "c:3"], "docker")
    assert failures == [("a:1", "image is in use"), ("c:3", "exit status 2")]
    assert mock_run.call_count == 3
