from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.vergil_refs import (
    MARKETPLACE_NAME,
    expected_claude_ref,
    format_version,
    is_marketplace_source_repo,
    iter_workflow_refs,
    read_source_version,
)

if TYPE_CHECKING:
    from pathlib import Path


def _seed_toml(base: Path, version: str) -> None:
    (base / "vergil.toml").write_text(f'[dependencies]\nvergil = "{version}"\n')


def test_marketplace_name_constant() -> None:
    assert MARKETPLACE_NAME == "vergil-marketplace"


def test_format_version_normalizes() -> None:
    assert format_version("2.2") == "v2.2"
    assert format_version("v2.3") == "v2.3"


def test_format_version_rejects_invalid() -> None:
    with pytest.raises(UpdateDepsError, match="invalid vergil version"):
        format_version("2")


def test_read_source_version(tmp_path: Path) -> None:
    _seed_toml(tmp_path, "v2.1")
    assert read_source_version(tmp_path) == "v2.1"


def test_read_source_version_missing_key(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[project]\n")
    with pytest.raises(UpdateDepsError, match="not found"):
        read_source_version(tmp_path)


def test_is_marketplace_source_repo_true(tmp_path: Path) -> None:
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text("{}")
    assert is_marketplace_source_repo(tmp_path) is True


def test_is_marketplace_source_repo_false(tmp_path: Path) -> None:
    assert is_marketplace_source_repo(tmp_path) is False


def test_expected_claude_ref_consumer(tmp_path: Path) -> None:
    _seed_toml(tmp_path, "v2.0")
    assert expected_claude_ref(tmp_path) == "v2.0"


def test_expected_claude_ref_self_repo(tmp_path: Path) -> None:
    _seed_toml(tmp_path, "v2.1")
    (tmp_path / ".claude-plugin").mkdir()
    (tmp_path / ".claude-plugin" / "marketplace.json").write_text("{}")
    assert expected_claude_ref(tmp_path) == "develop"


def test_iter_workflow_refs(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n"
        "  a:\n"
        "    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@v2.0\n"
        "  b:\n"
        "    uses: actions/checkout@v6\n"
    )
    refs = list(iter_workflow_refs(tmp_path))
    assert refs == [(wf / "ci.yml", "v2.0")]
