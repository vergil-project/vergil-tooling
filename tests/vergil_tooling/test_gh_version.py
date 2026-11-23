"""Tests for vergil_tooling.lib.gh_version (the minimum gh vrg-gh needs, #4613)."""

from __future__ import annotations

import json
import os
import tomllib
from importlib import resources
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import gh_version

if TYPE_CHECKING:
    from pathlib import Path


def _fake_gh(bin_dir: Path, output: str, *, exit_code: int = 0) -> Path:
    """Put a ``gh`` on PATH that prints *output* and counts its runs."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    gh = bin_dir / "gh"
    calls = bin_dir / "calls"
    gh.write_text(
        f"#!/bin/sh\necho run >> \"{calls}\"\nprintf '%s\\n' '{output}'\nexit {exit_code}\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    return gh


def _calls(bin_dir: Path) -> int:
    calls = bin_dir / "calls"
    return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    return path


@pytest.fixture
def cache(tmp_path: Path) -> Path:
    return tmp_path / "cache" / "vergil" / "gh-version.json"


_CURRENT = "gh version 2.103.3 (2026-10-26)"


def test_minimum_is_the_allow_escape_sequences_release() -> None:
    # `gh api --allow-escape-sequences` arrived in gh v2.97.0 (GHSA-3m3g-3wcr-px46).
    assert gh_version.MIN_GH_VERSION == "2.97.0"


def test_vrg_host_manifest_declares_the_same_minimum() -> None:
    text = resources.files("vergil_tooling.data").joinpath("host_dependencies.toml").read_text()
    deps = {dep["name"]: dep for dep in tomllib.loads(text)["dependency"]}
    assert deps["gh"]["minimum"] == gh_version.MIN_GH_VERSION


@pytest.mark.parametrize(
    "output",
    [
        "gh version 2.97.0 (2026-07-31)",
        "gh version 2.103.3 (2026-10-26)\nhttps://github.com/cli/cli/releases/tag/v2.103.3",
        "gh version 3.0.0 (2027-01-01)",
    ],
)
def test_new_enough_gh_passes(bin_dir: Path, cache: Path, output: str) -> None:
    _fake_gh(bin_dir, output)
    gh_version.require_minimum(cache=cache)


def test_old_gh_is_refused_naming_both_versions(bin_dir: Path, cache: Path) -> None:
    gh = _fake_gh(bin_dir, "gh version 2.96.0 (2026-07-01)")
    with pytest.raises(gh_version.GhVersionError) as exc:
        gh_version.require_minimum(cache=cache)
    message = str(exc.value)
    assert "gh 2.96.0" in message
    assert "needs gh 2.97.0 or newer" in message
    assert str(gh) in message


def test_missing_gh_is_refused(bin_dir: Path, cache: Path) -> None:
    with pytest.raises(gh_version.GhVersionError, match="gh is not on PATH"):
        gh_version.require_minimum(cache=cache)


def test_failing_gh_is_refused(bin_dir: Path, cache: Path) -> None:
    _fake_gh(bin_dir, "boom", exit_code=3)
    with pytest.raises(gh_version.GhVersionError, match="exited 3"):
        gh_version.require_minimum(cache=cache)
    assert not cache.exists()


@pytest.mark.parametrize("output", ["", "gh version unknown", "gh version 2.97.0-rc1"])
def test_unreadable_version_is_refused(bin_dir: Path, cache: Path, output: str) -> None:
    _fake_gh(bin_dir, output)
    with pytest.raises(gh_version.GhVersionError):
        gh_version.require_minimum(cache=cache)
    assert not cache.exists()


def test_version_is_cached_per_binary(bin_dir: Path, cache: Path) -> None:
    _fake_gh(bin_dir, _CURRENT)
    gh_version.require_minimum(cache=cache)
    gh_version.require_minimum(cache=cache)
    gh_version.require_minimum(cache=cache)
    assert _calls(bin_dir) == 1
    assert json.loads(cache.read_text(encoding="utf-8"))["version"] == "2.103.3"


def test_a_changed_binary_is_probed_again(bin_dir: Path, cache: Path) -> None:
    gh = _fake_gh(bin_dir, _CURRENT)
    gh_version.require_minimum(cache=cache)
    # A downgrade rewrites the binary in place: a new size and mtime.
    _fake_gh(bin_dir, "gh version 2.40.1 (2023-12-13)")
    stat = gh.stat()
    os.utime(gh, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    with pytest.raises(gh_version.GhVersionError, match="gh 2.40.1"):
        gh_version.require_minimum(cache=cache)
    assert _calls(bin_dir) == 2


def test_cache_holds_the_version_not_a_verdict(
    bin_dir: Path, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_gh(bin_dir, _CURRENT)
    gh_version.require_minimum(cache=cache)
    monkeypatch.setattr(gh_version, "MIN_GH_VERSION", "2.200.0")
    with pytest.raises(gh_version.GhVersionError, match="needs gh 2.200.0"):
        gh_version.require_minimum(cache=cache)
    assert _calls(bin_dir) == 1


@pytest.mark.parametrize("content", ["not json", "[]", '{"binary": {}, "version": "9.9.9"}'])
def test_unusable_cache_is_a_miss(bin_dir: Path, cache: Path, content: str) -> None:
    _fake_gh(bin_dir, "gh version 2.96.0 (2026-07-01)")
    cache.parent.mkdir(parents=True)
    cache.write_text(content, encoding="utf-8")
    with pytest.raises(gh_version.GhVersionError, match="gh 2.96.0"):
        gh_version.require_minimum(cache=cache)
    assert _calls(bin_dir) == 1
    assert json.loads(cache.read_text(encoding="utf-8"))["version"] == "2.96.0"


def test_unwritable_cache_warns_and_still_checks(
    bin_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_gh(bin_dir, _CURRENT)
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    cache = blocker / "gh-version.json"
    gh_version.require_minimum(cache=cache)
    assert "cannot cache the gh version" in capsys.readouterr().err


def test_cache_path_follows_xdg_cache_home(tmp_path: Path) -> None:
    env = {"XDG_CACHE_HOME": str(tmp_path / "xdg")}
    assert gh_version.cache_path(env) == tmp_path / "xdg" / "vergil" / "gh-version.json"
    assert gh_version.cache_path({"XDG_CACHE_HOME": "relative"}, home=tmp_path) == (
        tmp_path / ".cache" / "vergil" / "gh-version.json"
    )
    assert gh_version.cache_path({}, home=tmp_path) == (
        tmp_path / ".cache" / "vergil" / "gh-version.json"
    )
