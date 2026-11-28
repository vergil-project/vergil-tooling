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
def _no_group_records() -> Iterator[None]:
    """Read no release-group records from GitHub: every issue is in no group.

    The whole-group rule runs at report-ready and in the batch hard rules; its
    refusals have their own tests in ``tests/vergil_tooling/test_release_group_pr_check.py``
    and ``tests/vergil_tooling/pr_workflow/test_batch_rules.py``, which fake the groups
    they need. The single-issue check also names this clone's repo to parse its ``#N``
    against; the clones here have a local bare origin, so the repo is the GitHub one it
    stands for.
    """
    with (
        patch("vergil_tooling.lib.release_groups.record.groups_of", return_value=[]),
        patch("vergil_tooling.bin.vrg_pr_workflow._clone_repo", return_value="acme/tools"),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_open_pr_on_the_branch() -> Iterator[None]:
    """Give report-ready's push a branch with no open PR into ``develop``.

    The lookup reads GitHub; its refusal has its own tests in
    ``tests/vergil_tooling/test_vrg_pr_workflow.py``.
    """
    with patch("vergil_tooling.bin.vrg_pr_workflow.pr_path.open_develop_pr_url", return_value=None):
        yield
