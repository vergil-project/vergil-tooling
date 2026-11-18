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
