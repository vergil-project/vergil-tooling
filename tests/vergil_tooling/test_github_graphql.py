"""Tests for vergil_tooling.lib.github.graphql."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib import github


def test_graphql_returns_data() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
        return_value='{"data": {"node": {"number": 41}}}',
    ) as mock_read:
        result = github.graphql("query($id:ID!){node(id:$id){number}}", id="ABC")
    assert result == {"node": {"number": 41}}
    args = mock_read.call_args.args
    assert args[0] == "api"
    assert args[1] == "graphql"
    assert any(a.startswith("query=") for a in args)
    assert "id=ABC" in args


def test_graphql_string_var_uses_lowercase_f() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value='{"data": {}}') as mock_read:
        github.graphql("q", node_id="ABC")
    args = list(mock_read.call_args.args)
    assert args[args.index("node_id=ABC") - 1] == "-f"


def test_graphql_int_var_uses_uppercase_f() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value='{"data": {}}') as mock_read:
        github.graphql("q", number=41)
    args = list(mock_read.call_args.args)
    assert args[args.index("number=41") - 1] == "-F"


def test_graphql_raises_on_payload_errors() -> None:
    with (
        patch(
            "vergil_tooling.lib.github.read_output",
            return_value='{"errors": [{"message": "boom"}]}',
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        github.graphql("q")
