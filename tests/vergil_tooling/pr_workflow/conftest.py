"""Shared fixtures for the pr_workflow tests.

The dual-agent wait-tuning fixture was removed with the loop (#1872): the
run-and-done CLI never blocks, so there is nothing to fast-forward.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _no_planning_close_preview() -> Iterator[None]:
    """Skip report-ready's planning-close preview, which reads GitHub.

    The preview has its own tests in ``tests/vergil_tooling/test_planning_close.py``.
    """
    with patch("vergil_tooling.bin.vrg_pr_workflow._planning_close_preview", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _no_open_pr_on_the_branch() -> Iterator[None]:
    """Give report-ready's push a branch with no open PR into ``develop``.

    The lookup reads GitHub; its refusal has its own tests in
    ``tests/vergil_tooling/test_vrg_pr_workflow.py``.
    """
    with patch("vergil_tooling.bin.vrg_pr_workflow.pr_path.open_develop_pr_url", return_value=None):
        yield
