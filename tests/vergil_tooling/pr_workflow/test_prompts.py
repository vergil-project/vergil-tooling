"""Structural conformance tests for the judgment-check prompts.

These do NOT judge prompt quality (that is eval-style and deferred); they assert
each prompt names its check, the three statuses, and a check.v1 JSON output
instruction, so the audit agent is always told how to shape its result.
"""

from __future__ import annotations

from importlib.resources import files

import pytest

from vergil_tooling.lib.pr_workflow.registry import check_ids

_PROMPTS = files("vergil_tooling.lib.pr_workflow.prompts")


@pytest.mark.parametrize("check_id", check_ids())
def test_prompt_file_exists_and_is_nonempty(check_id: str) -> None:
    text = (_PROMPTS / f"{check_id}.md").read_text(encoding="utf-8")
    assert text.strip(), f"{check_id}.md is empty"


@pytest.mark.parametrize("check_id", check_ids())
def test_prompt_is_structurally_conformant(check_id: str) -> None:
    text = (_PROMPTS / f"{check_id}.md").read_text(encoding="utf-8")
    assert check_id in text  # names its own check id
    for status in ("pass", "fail", "escalate"):
        assert status in text, f"{check_id}.md does not mention status '{status}'"
    assert "check.v1" in text  # tells the agent the output schema
    assert "JSON" in text or "json" in text


def test_every_check_id_has_a_prompt_file() -> None:
    for check_id in check_ids():
        assert (_PROMPTS / f"{check_id}.md").is_file()
