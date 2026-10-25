"""Tests for the shape-aware ``pytest-xdist`` applicator (epic .github#333, Task 9)."""

from __future__ import annotations

import subprocess
import tomllib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.xdist_applicator import (
    UvLockError,
    _insert_into_array,
    _select_poetry_table,
    add_xdist,
)

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.xdist_applicator"

# --- uv dependency-groups -------------------------------------------------


def test_adds_to_uv_dependency_groups_inline(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    result = add_xdist(tmp_path)
    assert result.changed is True
    assert result.needs_followup is False
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "pytest-xdist" in data["dependency-groups"]["dev"]


def test_adds_to_uv_dependency_groups_multiline_no_trailing_comma(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = [\n    "pytest",\n    "ruff"\n]\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is True
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert data["dependency-groups"]["dev"] == ["pytest", "ruff", "pytest-xdist"]


def test_adds_to_uv_dependency_groups_multiline(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[dependency-groups]\ndev = [\n    "pytest",\n    "ruff",\n]\n\n[tool.uv]\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is True
    text = (tmp_path / "pyproject.toml").read_text()
    data = tomllib.loads(text)
    assert "pytest-xdist" in data["dependency-groups"]["dev"]
    # The trailing table is untouched and the file still parses.
    assert "[tool.uv]" in text


def test_adds_to_empty_uv_dependency_groups_array(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[dependency-groups]\ndev = []\n")
    result = add_xdist(tmp_path)
    assert result.changed is True
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert data["dependency-groups"]["dev"] == ["pytest-xdist"]


def test_idempotent_when_already_present_uv(tmp_path: Path) -> None:
    original = '[dependency-groups]\ndev = ["pytest", "pytest-xdist"]\n'
    (tmp_path / "pyproject.toml").write_text(original)
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is False
    assert (tmp_path / "pyproject.toml").read_text() == original


def test_idempotent_recognizes_normalized_name(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest_xdist>=3"]\n')
    result = add_xdist(tmp_path)
    assert result.changed is False


def test_uv_groups_without_dev_array_needs_followup(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\nlint = ["ruff"]\n')
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True


# --- PEP 621 optional-dependencies ---------------------------------------


def test_adds_to_pep621_optional_dev_group(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["pytest"]\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is True
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "pytest-xdist" in data["project"]["optional-dependencies"]["dev"]


def test_pep621_prefers_group_carrying_pytest(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.optional-dependencies]\n'
        'docs = ["sphinx"]\ntest = ["pytest"]\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is True
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "pytest-xdist" in data["project"]["optional-dependencies"]["test"]
    assert "pytest-xdist" not in data["project"]["optional-dependencies"]["docs"]


def test_pep621_selects_dev_group_without_pytest(tmp_path: Path) -> None:
    # No group carries pytest, so the conventional 'dev' group is chosen.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["ruff"]\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is True
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "pytest-xdist" in data["project"]["optional-dependencies"]["dev"]


def test_pep621_idempotent(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.optional-dependencies]\n'
        'dev = ["pytest", "pytest-xdist"]\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is False


def test_pep621_no_dev_or_test_group_needs_followup(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.optional-dependencies]\ndocs = ["sphinx"]\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True


# --- requirements-*.txt ---------------------------------------------------


def test_adds_to_requirements_dev_txt(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text("pytest\nruff\n")
    result = add_xdist(tmp_path)
    assert result.changed is True
    assert "pytest-xdist" in (tmp_path / "requirements-dev.txt").read_text().splitlines()


def test_adds_to_requirements_test_txt_when_dev_absent(tmp_path: Path) -> None:
    (tmp_path / "requirements-test.txt").write_text("pytest")  # no trailing newline
    result = add_xdist(tmp_path)
    assert result.changed is True
    lines = (tmp_path / "requirements-test.txt").read_text().splitlines()
    assert lines == ["pytest", "pytest-xdist"]


def test_requirements_idempotent(tmp_path: Path) -> None:
    original = "pytest\npytest-xdist\n"
    (tmp_path / "requirements-dev.txt").write_text(original)
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert (tmp_path / "requirements-dev.txt").read_text() == original


def test_requirements_ignores_comment_lines(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text(
        "# pytest-xdist is intentionally omitted\npytest\n"
    )
    result = add_xdist(tmp_path)
    assert result.changed is True
    assert "pytest-xdist" in (tmp_path / "requirements-dev.txt").read_text().splitlines()


# --- poetry ---------------------------------------------------------------


def test_poetry_group_dev_refused_loudly_no_edit(tmp_path: Path) -> None:
    # The sweep does not run `poetry lock`, so it refuses rather than commit a
    # stale poetry.lock (#2980).
    original = '[tool.poetry]\nname = "x"\n\n[tool.poetry.group.dev.dependencies]\npytest = "*"\n'
    (tmp_path / "pyproject.toml").write_text(original)
    with patch(f"{_MOD}.subprocess.run") as run:
        result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True
    assert "Poetry" in result.summary
    assert "NOT added" in result.summary
    assert (tmp_path / "pyproject.toml").read_text() == original
    run.assert_not_called()


def test_legacy_poetry_dev_dependencies_refused_loudly_no_edit(tmp_path: Path) -> None:
    original = '[tool.poetry]\nname = "x"\n\n[tool.poetry.dev-dependencies]\npytest = "*"\n'
    (tmp_path / "pyproject.toml").write_text(original)
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True
    assert (tmp_path / "pyproject.toml").read_text() == original


def test_poetry_idempotent(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n\n[tool.poetry.group.dev.dependencies]\n'
        'pytest = "*"\npytest-xdist = "*"\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is False
    assert "already present" in result.summary


def test_poetry_group_without_dependencies_table_needs_followup(tmp_path: Path) -> None:
    # A dev group that declares no `.dependencies` subtable is not a usable target.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n\n[tool.poetry.group.dev]\noptional = true\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True


def test_poetry_without_dev_or_test_group_needs_followup(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n\n[tool.poetry.group.docs.dependencies]\nsphinx = "*"\n',
    )
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True


# --- unknown shape (loud, no edit) ---------------------------------------


def test_unknown_shape_reports_loudly_no_edit(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = weird\n")
    before = sorted(p.name for p in tmp_path.iterdir())
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True
    assert "NOT added" in result.summary
    assert sorted(p.name for p in tmp_path.iterdir()) == before


# --- defensive helper contracts (whitebox) -------------------------------


def test_insert_into_array_returns_none_when_section_absent() -> None:
    assert (
        _insert_into_array("[other]\nx = 1\n", "dependency-groups", "dev", "pytest-xdist") is None
    )


def test_insert_into_array_returns_none_when_array_absent() -> None:
    text = '[dependency-groups]\nlint = ["ruff"]\n'
    assert _insert_into_array(text, "dependency-groups", "dev", "pytest-xdist") is None


def test_select_poetry_table_ignores_non_dict_group_and_falls_back_to_legacy() -> None:
    header, table = _select_poetry_table(
        {"group": "not-a-table", "dev-dependencies": {"pytest": "*"}}
    )
    assert header == "tool.poetry.dev-dependencies"
    assert table == {"pytest": "*"}


# --- uv.lock refresh (#2980) ---------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["uv", "lock"], returncode, stdout, stderr)


def test_uv_groups_refreshes_uv_lock_when_present(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with (
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv"),
        patch(f"{_MOD}.subprocess.run", return_value=_completed(0)) as run,
    ):
        result = add_xdist(tmp_path)
    assert result.changed is True
    assert result.needs_followup is False
    assert "refreshed uv.lock" in result.summary
    run.assert_called_once()
    assert run.call_args.args[0] == ["/usr/bin/uv", "lock"]
    assert run.call_args.kwargs["cwd"] == tmp_path
    # The lock runs against the already-edited pyproject.toml.
    data = tomllib.loads((tmp_path / "pyproject.toml").read_text())
    assert "pytest-xdist" in data["dependency-groups"]["dev"]


def test_pep621_refreshes_uv_lock_when_present(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["pytest"]\n',
    )
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with (
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv"),
        patch(f"{_MOD}.subprocess.run", return_value=_completed(0)) as run,
    ):
        result = add_xdist(tmp_path)
    assert result.changed is True
    assert "refreshed uv.lock" in result.summary
    run.assert_called_once()


def test_no_uv_lock_means_no_lock_refresh(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    with patch(f"{_MOD}.subprocess.run") as run:
        result = add_xdist(tmp_path)
    assert result.changed is True
    assert "uv.lock" not in result.summary
    run.assert_not_called()


def test_already_present_does_not_relock(tmp_path: Path) -> None:
    text = '[dependency-groups]\ndev = ["pytest", "pytest-xdist"]\n'
    (tmp_path / "pyproject.toml").write_text(text)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with patch(f"{_MOD}.subprocess.run") as run:
        result = add_xdist(tmp_path)
    assert result.changed is False
    run.assert_not_called()


def test_requirements_shape_never_relocks(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with patch(f"{_MOD}.subprocess.run") as run:
        result = add_xdist(tmp_path)
    assert result.changed is True
    run.assert_not_called()


def test_missing_uv_raises_loudly(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with (
        patch(f"{_MOD}.shutil.which", return_value=None),
        patch(f"{_MOD}.subprocess.run") as run,
        pytest.raises(UvLockError, match="`uv` is not on PATH"),
    ):
        add_xdist(tmp_path)
    run.assert_not_called()


def test_failed_uv_lock_raises_with_detail(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with (
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv"),
        patch(f"{_MOD}.subprocess.run", return_value=_completed(2, stderr="resolution failed\n")),
        pytest.raises(UvLockError, match=r"`uv lock` failed \(exit 2\).*resolution failed"),
    ):
        add_xdist(tmp_path)


def test_failed_uv_lock_falls_back_to_stdout_detail(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with (
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv"),
        patch(f"{_MOD}.subprocess.run", return_value=_completed(1, stdout="only stdout")),
        pytest.raises(UvLockError, match="only stdout"),
    ):
        add_xdist(tmp_path)


# --- scope: only Python repos (#3549) --------------------------------------


def test_non_python_primary_language_is_out_of_scope(tmp_path: Path) -> None:
    # A declared non-Python repo is skipped even if it carries a pyproject.toml,
    # and never flagged for a follow-up issue.
    original = '[dependency-groups]\ndev = ["pytest"]\n'
    (tmp_path / "pyproject.toml").write_text(original)
    with patch(f"{_MOD}.resolve_language", return_value="go"):
        result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is False
    assert result.summary.startswith("out of scope: primary-language is go;")
    assert (tmp_path / "pyproject.toml").read_text() == original


def test_repo_with_no_python_project_is_out_of_scope(tmp_path: Path) -> None:
    # The docs/containers/actions repos of the 2026-10-14 dry run: no declared
    # language and no Python project files. Before #3549 each became a follow-up.
    (tmp_path / "README.md").write_text("# docs\n")
    result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is False
    assert "no primary-language declared and no Python project files" in result.summary


def test_declared_python_with_unknown_shape_still_needs_followup(tmp_path: Path) -> None:
    with patch(f"{_MOD}.resolve_language", return_value="python"):
        result = add_xdist(tmp_path)
    assert result.changed is False
    assert result.needs_followup is True


def test_undeclared_requirements_only_repo_stays_in_scope(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    result = add_xdist(tmp_path)
    assert result.changed is True


def test_malformed_vergil_toml_raises_not_skips(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("not = [valid toml")
    with pytest.raises(ConfigError):
        add_xdist(tmp_path)


# --- check-only mode (#3549) -----------------------------------------------


def test_check_uv_groups_reports_without_writing_or_locking(tmp_path: Path) -> None:
    original = '[dependency-groups]\ndev = ["pytest"]\n'
    (tmp_path / "pyproject.toml").write_text(original)
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with (
        patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv"),
        patch(f"{_MOD}.subprocess.run") as run,
    ):
        result = add_xdist(tmp_path, check=True)
    assert result.changed is True
    assert (
        result.summary == "would add pytest-xdist to [dependency-groups].dev (and refresh uv.lock)"
    )
    assert (tmp_path / "pyproject.toml").read_text() == original
    run.assert_not_called()


def test_check_without_uv_lock(tmp_path: Path) -> None:
    original = '[project]\nname = "x"\n\n[project.optional-dependencies]\ndev = ["pytest"]\n'
    (tmp_path / "pyproject.toml").write_text(original)
    result = add_xdist(tmp_path, check=True)
    assert result.summary == "would add pytest-xdist to [project.optional-dependencies].dev"
    assert (tmp_path / "pyproject.toml").read_text() == original


def test_check_missing_uv_raises_like_the_write(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    with (
        patch(f"{_MOD}.shutil.which", return_value=None),
        pytest.raises(UvLockError, match="`uv` is not on PATH"),
    ):
        add_xdist(tmp_path, check=True)


def test_check_requirements_reports_without_writing(tmp_path: Path) -> None:
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    result = add_xdist(tmp_path, check=True)
    assert result.changed is True
    assert result.summary == "would add pytest-xdist to requirements-dev.txt"
    assert (tmp_path / "requirements-dev.txt").read_text() == "pytest\n"


def test_check_matches_write_for_skip_and_followup(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest-xdist"]\n')
    assert add_xdist(tmp_path, check=True) == add_xdist(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "x"\n\n[tool.poetry.group.dev.dependencies]\npytest = "*"\n'
    )
    preview = add_xdist(tmp_path, check=True)
    assert preview == add_xdist(tmp_path)
    assert preview.needs_followup is True
