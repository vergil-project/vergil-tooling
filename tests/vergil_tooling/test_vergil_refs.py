from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.vergil_refs import (
    EXPECTED_MARKETPLACE_REF,
    MARKETPLACE_NAME,
    format_version,
    is_deprecated_marketplace_ref,
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


def test_expected_marketplace_ref_constant() -> None:
    assert EXPECTED_MARKETPLACE_REF == "main"


def test_is_deprecated_marketplace_ref_develop() -> None:
    assert is_deprecated_marketplace_ref("develop") is True


@pytest.mark.parametrize("ref", ["2.1", "v2.1", "2.0.7", "v2.0.7"])
def test_is_deprecated_marketplace_ref_versions(ref: str) -> None:
    assert is_deprecated_marketplace_ref(ref) is True


@pytest.mark.parametrize("ref", ["main", "feature/x", "", "v2", "latest"])
def test_is_deprecated_marketplace_ref_other(ref: str) -> None:
    assert is_deprecated_marketplace_ref(ref) is False


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
