"""Tests for standard_tooling.bin.st_repo_profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

from standard_tooling.bin.st_repo_profile import _structural_check, main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_VALID_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]
claude = "Co-Authored-By: user-claude <111+user-claude@users.noreply.github.com>"

[dependencies]
standard-tooling = "v1.4"
"""


def _write_toml(tmp_path: Path, content: str) -> None:
    (tmp_path / "standard-tooling.toml").write_text(content)


# -- TOML validation ----------------------------------------------------------


def test_valid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, _VALID_TOML)
    assert main() == 0


def test_missing_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert main() == 2


def test_missing_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    toml = _VALID_TOML.replace('primary-language = "python"\n', "")
    _write_toml(tmp_path, toml)
    assert main() == 1


def test_invalid_enum(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    toml = _VALID_TOML.replace('"library"', '"banana"')
    _write_toml(tmp_path, toml)
    assert main() == 1


def test_malformed_co_author(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    toml = _VALID_TOML.replace(
        'claude = "Co-Authored-By: user-claude <111+user-claude@users.noreply.github.com>"',
        'claude = "not a trailer"',
    )
    _write_toml(tmp_path, toml)
    assert main() == 1


def test_missing_dependencies_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    toml = _VALID_TOML.replace('standard-tooling = "v1.4"', 'other = "v1.0"')
    _write_toml(tmp_path, toml)
    assert main() == 1


# -- _structural_check -------------------------------------------------------


def test_structural_valid(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\n## Table of Contents\n\n## Section\n")
    assert _structural_check(str(doc)) is True


def test_structural_no_h1(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("## Section\n\n## Table of Contents\n")
    assert _structural_check(str(doc)) is False


def test_structural_multiple_h1(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\n# Another\n\n## Table of Contents\n")
    assert _structural_check(str(doc)) is False


def test_structural_no_toc(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\n## Section\n")
    assert _structural_check(str(doc)) is False


def test_structural_heading_skip(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\n## Table of Contents\n\n#### Skipped\n")
    assert _structural_check(str(doc)) is False


def test_structural_code_fence_ignored(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\n## Table of Contents\n\n```\n# Not a heading\n```\n\n## End\n")
    assert _structural_check(str(doc)) is True


def test_structural_tilde_fence_ignored(tmp_path: Path) -> None:
    doc = tmp_path / "README.md"
    doc.write_text("# Title\n\n## Table of Contents\n\n~~~\n# Not a heading\n~~~\n\n## End\n")
    assert _structural_check(str(doc)) is True


# -- main: structural check integration --------------------------------------


def test_main_readme_structural_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, _VALID_TOML)
    (tmp_path / "README.md").write_text("## No H1\n")
    assert main() == 1


def test_main_readme_structural_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, _VALID_TOML)
    (tmp_path / "README.md").write_text("# Title\n\n## Table of Contents\n\n## Section\n")
    assert main() == 0


def test_main_no_readme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_toml(tmp_path, _VALID_TOML)
    assert main() == 0
