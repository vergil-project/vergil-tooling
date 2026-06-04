"""Tests for vergil_tooling.bin.vrg_pr_await."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_pr_await import main, parse_args
from vergil_tooling.lib.pr_await import PrState

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_pr_await"
_PR = "https://github.com/pr/1"


def test_parse_args_defaults() -> None:
    args = parse_args([_PR])
    assert args.pr == _PR
    assert args.since_sha is None
    assert args.since_reviews is None


def test_parse_args_with_baselines() -> None:
    args = parse_args([_PR, "--since-sha", "abc", "--since-reviews", "2"])
    assert args.since_sha == "abc"
    assert args.since_reviews == 2


def test_main_prints_settled_state_json(capsys: pytest.CaptureFixture[str]) -> None:
    state = PrState("abc123", [{"name": "build", "bucket": "pass", "state": "SUCCESS"}], [])
    with patch(f"{_MOD}.pr_await.wait_for_settle", return_value=(state, "checks_terminal")):
        result = main([_PR])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "checks_terminal"
    assert payload["head_sha"] == "abc123"
    assert payload["all_checks_passed"] is True


def test_main_passes_baselines_through() -> None:
    state = PrState("sha", [], [])
    with patch(f"{_MOD}.pr_await.wait_for_settle", return_value=(state, "new_commit")) as waiter:
        main([_PR, "--since-sha", "old", "--since-reviews", "3"])
    _, kwargs = waiter.call_args
    assert kwargs["since_sha"] == "old"
    assert kwargs["since_reviews"] == 3
