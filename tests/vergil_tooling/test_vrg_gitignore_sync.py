"""Tests for the ``vrg-gitignore-sync`` single-repo applicator CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_gitignore_sync
from vergil_tooling.lib import gitignore

if TYPE_CHECKING:
    from pathlib import Path

_VERGIL_TOML_PYTHON = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]

[ci]
versions = ["3.12"]

[dependencies]
vergil = "v2.0.7"
"""

_VERGIL_TOML_NO_LANG = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[project.co-authors]

[ci]
versions = ["3.12"]

[dependencies]
vergil = "v2.0.7"
"""


def _write_repo(root: Path, toml: str, gitignore_text: str | None = None) -> None:
    (root / "vergil.toml").write_text(toml, encoding="utf-8")
    if gitignore_text is not None:
        (root / ".gitignore").write_text(gitignore_text, encoding="utf-8")


def _monolith(*langs: str, extra: list[str] | None = None) -> str:
    """A loose (unfenced) union of base + language fragments, plus repo-local lines."""
    lines = list(gitignore.load_base())
    for lang in langs:
        lines += gitignore.load_fragment(lang)
    lines += extra or []
    return "\n".join(lines) + "\n"


class TestCheck:
    def test_compliant_python(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, _VERGIL_TOML_PYTHON, gitignore.render_block("python"))
        rc = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--check"])
        assert rc == 0

    def test_monolith_python_fails_with_reasons(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_repo(tmp_path, _VERGIL_TOML_PYTHON, _monolith("python", "cpp"))
        rc = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--check"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "vergil-managed fence" in err

    def test_base_only_no_language_compliant(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, _VERGIL_TOML_NO_LANG, gitignore.render_block(None))
        rc = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--check"])
        assert rc == 0

    def test_missing_gitignore_handled(self, tmp_path: Path) -> None:
        _write_repo(tmp_path, _VERGIL_TOML_PYTHON)
        rc = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--check"])
        assert rc == 1  # empty text has no fence

    def test_default_mode_is_check_and_repo_is_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_repo(tmp_path, _VERGIL_TOML_PYTHON, gitignore.render_block("python"))
        monkeypatch.chdir(tmp_path)
        rc = vrg_gitignore_sync.main([])  # no --repo, no mode flag
        assert rc == 0


class TestWrite:
    def test_python_monolith_rewrites_and_is_idempotent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_repo(
            tmp_path,
            _VERGIL_TOML_PYTHON,
            _monolith("python", "cpp", extra=["mylocal.log"]),
        )
        rc = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--write"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dropped" in out
        assert "this repo is python" in out

        written = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert written == gitignore.render_block("python") + "\nmylocal.log\n"
        assert "mylocal.log" in written  # genuine repo-local line preserved

        # Second write is a no-op.
        rc2 = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--write"])
        assert rc2 == 0
        assert capsys.readouterr().out.strip() == "already in sync"

    def test_base_only_monolith_reports_base_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_repo(tmp_path, _VERGIL_TOML_NO_LANG, _monolith("cpp"))
        rc = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--write"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dropped" in out
        assert "this repo is base-only" in out
        assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == gitignore.render_block(None)

    def test_missing_gitignore_bootstraps_without_removed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_repo(tmp_path, _VERGIL_TOML_NO_LANG)
        rc = vrg_gitignore_sync.main(["--repo", str(tmp_path), "--write"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "dropped" not in out
        assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == gitignore.render_block(None)


class TestArgs:
    def test_check_and_write_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            vrg_gitignore_sync.main(["--repo", str(tmp_path), "--check", "--write"])
        assert exc.value.code == 2
