from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.updaters.vergil_eco import (
    format_version,
    normalize_refs,
    read_source_version,
    set_source_version,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_format_version_normalizes() -> None:
    assert format_version("2.2") == "v2.2"
    assert format_version("v2.3") == "v2.3"
    assert format_version(" 2.4 ") == "v2.4"


def test_format_version_rejects_invalid() -> None:
    with pytest.raises(UpdateDepsError, match="invalid vergil version"):
        format_version("2")


def test_read_source_version(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    assert read_source_version(tmp_path) == "v2.1"


def test_read_source_version_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[dependencies]\n")
    with pytest.raises(UpdateDepsError, match="dependencies..vergil"):
        read_source_version(tmp_path)


def test_set_source_version_rewrites_and_is_idempotent(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text('[dependencies]\nvergil = "v2.1"\n')
    assert set_source_version(tmp_path, "v2.2") is True
    assert 'vergil = "v2.2"' in (tmp_path / "vergil.toml").read_text()
    assert set_source_version(tmp_path, "v2.2") is False


def test_normalize_refs_rewrites_drifting_only(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        "jobs:\n"
        "  a:\n"
        "    uses: vergil-project/vergil-actions/.github/workflows/ci.yml@v2.0\n"
        "  b:\n"
        "    uses: vergil-project/vergil-actions/.github/workflows/cd.yml@v2.1\n"
        "  c:\n"
        "    uses: actions/checkout@v4\n"
    )
    changed = normalize_refs(tmp_path, "v2.1")
    assert changed == [wf / "ci.yml"]
    text = (wf / "ci.yml").read_text()
    assert "@v2.0" not in text
    assert text.count("@v2.1") == 2
    assert "actions/checkout@v4" in text  # third-party untouched
    assert normalize_refs(tmp_path, "v2.1") == []  # idempotent


def test_normalize_refs_no_workflows_dir(tmp_path: Path) -> None:
    assert normalize_refs(tmp_path, "v2.1") == []
