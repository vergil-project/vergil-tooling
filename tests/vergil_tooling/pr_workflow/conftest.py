"""Shared fixtures for the pr_workflow tests.

The dual-agent wait-tuning fixture was removed with the loop (#1872): the
run-and-done CLI never blocks, so there is nothing to fast-forward.
"""

from __future__ import annotations
