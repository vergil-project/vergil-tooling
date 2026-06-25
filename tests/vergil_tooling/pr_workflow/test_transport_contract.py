"""The Transport ABC contract, trimmed to read/write/git facts (#1872)."""

from __future__ import annotations

import inspect

from vergil_tooling.lib.pr_workflow.transport import Transport


def test_contract_is_read_write_and_git_facts_only() -> None:
    abstract = set(Transport.__abstractmethods__)
    assert abstract == {"read", "write", "head_sha", "merge_base"}
    assert "wait_until_owner" not in dir(Transport)
    assert "wait_until_present" not in dir(Transport)
    # signatures are intact
    assert inspect.isfunction(Transport.read)
