"""Tests for the ``secrets: inherit`` allow-list (epic vergil-project/.github#1384).

Covers :mod:`vergil_tooling.lib.secrets_inherit` and the ``secrets-inherit``
common check of ``vrg-validate``
(:func:`vergil_tooling.bin.validate_common._check_secrets_inherit`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import yaml

from vergil_tooling.bin.validate_common import _check_secrets_inherit
from vergil_tooling.lib.secrets_inherit import (
    INHERIT_ALLOWED,
    InheritViolation,
    inherit_allowed,
    inherit_violations,
)
from vergil_tooling.lib.semgrep import DEFAULT_EXCLUDED_RULES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_ACTIONS = "vergil-project/vergil-actions/.github/workflows"


def _caller(uses: str, secrets: str = "    secrets: inherit\n", job: str = "release") -> str:
    return f"name: CD\non: push\njobs:\n  {job}:\n    uses: {uses}\n{secrets}"


# -- the allow-list -----------------------------------------------------------


@pytest.mark.parametrize("name", INHERIT_ALLOWED)
def test_inherit_on_listed_workflow_passes(name: str) -> None:
    assert inherit_violations(_caller(f"{_ACTIONS}/{name}@v2.1")) == []


def test_inherit_on_unlisted_workflow_fails() -> None:
    text = _caller(f"{_ACTIONS}/ci-quality.yml@v2.1")
    assert inherit_violations(text) == [
        InheritViolation(line=6, job="release", uses=f"{_ACTIONS}/ci-quality.yml@v2.1")
    ]


def test_explicit_map_passes() -> None:
    secrets = "    secrets:\n      APP_CLIENT_ID: ${{ secrets.APP_CLIENT_ID }}\n"
    assert inherit_violations(_caller(f"{_ACTIONS}/ci-quality.yml@v2.1", secrets)) == []


def test_no_secrets_passes() -> None:
    assert inherit_violations(_caller(f"{_ACTIONS}/ci-quality.yml@v2.1", "")) == []


def test_listed_name_in_another_repo_fails() -> None:
    uses = "someone/vergil-actions-fork/.github/workflows/cd-release.yml@v2.1"
    assert [v.uses for v in inherit_violations(_caller(uses))] == [uses]


def test_listed_name_in_another_owner_fails() -> None:
    uses = "someone/vergil-actions/.github/workflows/cd-release.yml@main"
    assert inherit_violations(_caller(uses)) != []


@pytest.mark.parametrize(
    "uses",
    [
        "Vergil-Project/Vergil-Actions/.github/workflows/cd-release.yml@v2.1",
        f"{_ACTIONS}/cd-docs.yml@develop",
        f"{_ACTIONS}/publish-index.yml@0123456789abcdef0123456789abcdef01234567",
        "./.github/workflows/cd-docs.yml",
    ],
)
def test_allowed_forms(uses: str) -> None:
    assert inherit_allowed(uses)


@pytest.mark.parametrize(
    "uses",
    [
        f"{_ACTIONS}/cd-release.yml",  # no ref
        f"{_ACTIONS}/cd-release.yml@",  # empty ref
        f"{_ACTIONS}/sub/cd-release.yml@v2.1",
        f"{_ACTIONS}/../workflows/cd-release.yml@v2.1",
        "vergil-project/vergil-actions/cd-release.yml@v2.1",
        "./.github/workflows/ci-quality.yml",
        "./.github/workflows/../../evil/cd-release.yml",
        "./cd-release.yml",
        "actions/checkout@v6",
        "docker://alpine:3",
    ],
)
def test_refused_forms(uses: str) -> None:
    assert not inherit_allowed(uses)


def test_inherit_without_uses_fails() -> None:
    text = "on: push\njobs:\n  build:\n    runs-on: ubuntu-24.04\n    secrets: inherit\n"
    assert inherit_violations(text) == [InheritViolation(line=5, job="build", uses=None)]


def test_every_violation_in_line_order() -> None:
    text = (
        "on: push\njobs:\n"
        f"  a:\n    uses: {_ACTIONS}/ci-test.yml@v2.1\n    secrets: inherit\n"
        f"  b:\n    uses: {_ACTIONS}/cd-release.yml@v2.1\n    secrets: inherit\n"
        "  c:\n    uses: other/repo/.github/workflows/x.yml@v1\n    secrets: inherit\n"
    )
    assert [(v.line, v.job) for v in inherit_violations(text)] == [(5, "a"), (11, "c")]


def test_aliased_inherit_is_checked() -> None:
    text = (
        "on: push\njobs:\n"
        f"  a:\n    uses: {_ACTIONS}/cd-release.yml@v2.1\n    secrets: &s inherit\n"
        f"  b:\n    uses: {_ACTIONS}/ci-test.yml@v2.1\n    secrets: *s\n"
    )
    assert [v.job for v in inherit_violations(text)] == ["b"]


def test_no_jobs_has_nothing_to_check() -> None:
    assert inherit_violations("on: push\n") == []
    assert inherit_violations("") == []
    assert inherit_violations("just a string\n") == []
    assert inherit_violations("on: push\njobs:\n  a:\n  b: [1]\n") == []


def test_unparseable_raises() -> None:
    with pytest.raises(yaml.YAMLError):
        inherit_violations("jobs: [unclosed\n")


def test_semgrep_rule_is_replaced_not_doubled() -> None:
    # The check replaces semgrep's registry rule, so the rule has one definition.
    assert "yaml.github-actions.security.secrets-inherit.secrets-inherit" in DEFAULT_EXCLUDED_RULES


# -- the vrg-validate common check ----------------------------------------------


@pytest.fixture(autouse=True)
def _no_workflow_listing() -> Iterator[None]:
    """List workflow files from the directory: ``git ls-files`` needs a real
    repository, which ``tmp_path`` is not."""

    def _listing(repo_root: Path) -> list[str]:
        workflows = repo_root / ".github" / "workflows"
        if not workflows.is_dir():
            return []
        return sorted(str(path) for path in workflows.iterdir())

    with patch("vergil_tooling.bin.validate_common._find_workflow_files", side_effect=_listing):
        yield


def _workflow(repo_root: Path, text: str, name: str = "cd.yml") -> None:
    workflows = repo_root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(text)


def test_check_refuses_unlisted_inherit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workflow(tmp_path, _caller(f"{_ACTIONS}/ci-quality.yml@v2.1"))
    assert _check_secrets_inherit(tmp_path) == 1
    err = capsys.readouterr().err
    assert (
        f".github/workflows/cd.yml:6: job release passes secrets: inherit to "
        f"{_ACTIONS}/ci-quality.yml@v2.1; inherit is allowed only for the vergil-actions "
        "workflows "
    ) in err
    assert "Pass an explicit secrets map" in err


def test_check_names_a_job_with_no_uses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workflow(tmp_path, "on: push\njobs:\n  build:\n    secrets: inherit\n")
    assert _check_secrets_inherit(tmp_path) == 1
    assert "job build passes secrets: inherit to no reusable workflow" in capsys.readouterr().err


def test_check_passes_listed_inherit_and_maps(tmp_path: Path) -> None:
    _workflow(tmp_path, _caller(f"{_ACTIONS}/cd-release.yml@v2.1"))
    _workflow(tmp_path, _caller(f"{_ACTIONS}/ci-quality.yml@v2.1", ""), name="ci.yml")
    assert _check_secrets_inherit(tmp_path) == 0


def test_check_reports_every_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workflow(tmp_path, _caller(f"{_ACTIONS}/ci-test.yml@v2.1"), name="a.yml")
    _workflow(tmp_path, _caller(f"{_ACTIONS}/ci-audit.yml@v2.1"), name="b.yml")
    assert _check_secrets_inherit(tmp_path) == 1
    err = capsys.readouterr().err
    assert ".github/workflows/a.yml:6:" in err
    assert ".github/workflows/b.yml:6:" in err


def test_check_unparseable_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workflow(tmp_path, "jobs: [unclosed\n")
    assert _check_secrets_inherit(tmp_path) == 1
    assert ".github/workflows/cd.yml: unparseable:" in capsys.readouterr().err


def test_check_no_workflows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _check_secrets_inherit(tmp_path) == 0
    assert "secrets-inherit" not in capsys.readouterr().out
