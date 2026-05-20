from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext
from vergil_tooling.lib.release.handoff import consumer_refresh

_MOD = "vergil_tooling.lib.release.handoff"


def _ctx() -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )


def test_consumer_refresh_templates_version(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = (
            "uv tool install pkg@v<VERSION>"
        )
        consumer_refresh(ctx)
    captured = capsys.readouterr()
    assert "uv tool install pkg@v2.1.0" in captured.out


def test_consumer_refresh_none(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = None
        consumer_refresh(ctx)
    captured = capsys.readouterr()
    assert "no consumer-refresh" in captured.out.lower()
