"""Tests for the ``vrg-dependabot-sync`` single-repo applicator CLI (epic .github#420)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_dependabot_sync
from vergil_tooling.lib import dependabot

if TYPE_CHECKING:
    from pathlib import Path

_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
{lang_line}
[ci]
versions = ["3.12"]

[dependencies]
vergil = "v2.0.7"
"""


def _write_repo(root: Path, lang: str | None, dependabot_text: str | None = None) -> Path:
    lang_line = f'primary-language = "{lang}"\n' if lang else ""
    (root / "vergil.toml").write_text(_TOML.format(lang_line=lang_line), encoding="utf-8")
    path = root / ".github" / "dependabot.yml"
    if dependabot_text is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dependabot_text, encoding="utf-8")
    return path


# --- --check ---------------------------------------------------------------


def test_sync_check_missing_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_repo(tmp_path, "python")
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path), "--check"]) == 1
    assert "missing .github/dependabot.yml" in capsys.readouterr().err


def test_sync_check_is_default(tmp_path: Path) -> None:
    path = _write_repo(tmp_path, "python")
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path)]) == 1
    assert not path.exists()


def test_sync_check_differs_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_repo(tmp_path, "python", dependabot.render("python") + "# local\n")
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path), "--check"]) == 1
    assert "differs from the canonical render" in capsys.readouterr().err


def test_sync_check_canonical_exits_0(tmp_path: Path) -> None:
    _write_repo(tmp_path, "go", dependabot.render("go"))
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path), "--check"]) == 0


def test_sync_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path, None, dependabot.render(None))
    monkeypatch.chdir(tmp_path)
    assert vrg_dependabot_sync.main(["--check"]) == 0


# --- --write ---------------------------------------------------------------


def test_sync_write_creates_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _write_repo(tmp_path, "python")
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert path.read_text(encoding="utf-8") == dependabot.render("python")
    assert capsys.readouterr().out.strip() == "wrote .github/dependabot.yml (python)"


def test_sync_write_no_language_labels_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_repo(tmp_path, None)
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert path.read_text(encoding="utf-8") == dependabot.render(None)
    assert capsys.readouterr().out.strip() == "wrote .github/dependabot.yml (none)"


def test_sync_write_idempotent_prints_already_in_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_repo(tmp_path, "rust", dependabot.render("rust"))
    before = path.stat().st_mtime_ns
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert capsys.readouterr().out.strip() == "already in sync"
    assert path.stat().st_mtime_ns == before


def test_sync_write_overwrites_noncanonical(tmp_path: Path) -> None:
    """Exact match: local additions are replaced, not preserved."""
    path = _write_repo(tmp_path, "python", dependabot.render("python") + "# local addition\n")
    assert vrg_dependabot_sync.main(["--repo", str(tmp_path), "--write"]) == 0
    assert path.read_text(encoding="utf-8") == dependabot.render("python")


def test_sync_check_and_write_are_exclusive(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_dependabot_sync.main(["--repo", str(tmp_path), "--check", "--write"])
    assert exc.value.code == 2
