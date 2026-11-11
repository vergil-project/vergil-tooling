"""Tests for the declared-identity release gate (issue #4175)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.release.gate import unreleased_refusal

if TYPE_CHECKING:
    from pathlib import Path

_BASE_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


def _write(root: Path, extra: str = "") -> None:
    (root / "vergil.toml").write_text(_BASE_TOML + extra)


def test_released_repo_passes(tmp_path: Path) -> None:
    _write(tmp_path, "\n[publish]\nrelease = true\n")
    assert unreleased_refusal(tmp_path) is None


def test_release_false_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "\n[publish]\nrelease = false\n")
    refusal = unreleased_refusal(tmp_path)
    assert refusal is not None
    assert "this repo is not released" in refusal
    assert "[publish].release" in refusal
    assert "nothing was changed" in refusal


def test_absent_publish_section_is_refused(tmp_path: Path) -> None:
    _write(tmp_path)
    assert unreleased_refusal(tmp_path) is not None


def test_absent_release_key_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, "\n[publish]\ndocs = true\n")
    assert unreleased_refusal(tmp_path) is not None


def test_missing_config_raises(tmp_path: Path) -> None:
    """An unreadable declaration is an error, never taken as a release."""
    with pytest.raises(FileNotFoundError):
        unreleased_refusal(tmp_path)


def test_invalid_config_raises(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("not = [valid")
    with pytest.raises(ConfigError):
        unreleased_refusal(tmp_path)


def test_defaults_to_main_worktree_root(tmp_path: Path) -> None:
    _write(tmp_path, "\n[publish]\nrelease = true\n")
    with patch(
        "vergil_tooling.lib.release.gate.git.main_worktree_root", return_value=tmp_path
    ) as m_root:
        assert unreleased_refusal() is None
    m_root.assert_called_once_with()
