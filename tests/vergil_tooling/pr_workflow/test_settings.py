"""Tests for vergil_tooling.lib.pr_workflow.settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.pr_workflow import settings
from vergil_tooling.lib.pr_workflow.errors import WorkflowError

if TYPE_CHECKING:
    from pathlib import Path


def test_default_when_no_vergil_toml(tmp_path: Path) -> None:
    assert settings.max_rounds(tmp_path) == 10


def test_default_when_key_absent(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[project]\nname = 'x'\n")
    assert settings.max_rounds(tmp_path) == 10


def test_reads_configured_cap(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[pr-workflow]\nmax-rounds = 3\n")
    assert settings.max_rounds(tmp_path) == 3


def test_rejects_non_positive_cap(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[pr-workflow]\nmax-rounds = 0\n")
    with pytest.raises(WorkflowError, match="max-rounds"):
        settings.max_rounds(tmp_path)
