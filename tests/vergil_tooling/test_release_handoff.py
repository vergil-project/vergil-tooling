from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib.release.context import ReleaseContext
from vergil_tooling.lib.release.handoff import consumer_refresh, run_consumer_refresh

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.lib.release.handoff"


def _ctx() -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )


def test_consumer_refresh_templates_version(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = "uv tool install pkg@v<VERSION>"
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


def test_consumer_refresh_stores_message_on_ctx() -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = "uv tool install pkg@v<VERSION>"
        consumer_refresh(ctx)
    assert ctx.consumer_refresh_message is not None
    assert "Consumer refresh commands:" in ctx.consumer_refresh_message
    assert "uv tool install pkg@v2.1.0" in ctx.consumer_refresh_message


def test_consumer_refresh_none_stores_notice_on_ctx() -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = None
        consumer_refresh(ctx)
    assert ctx.consumer_refresh_message is not None
    assert "no consumer-refresh" in ctx.consumer_refresh_message.lower()


# -- consumer_refresh stores the raw command block for --install (issue #1643) --


def test_consumer_refresh_stores_expanded_commands_on_ctx() -> None:
    ctx = _ctx()
    template = "uv tool install pkg@v<VERSION>\nvrg-vm update --all"
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = template
        consumer_refresh(ctx)
    # The version macro is expanded and the display wrapper is stripped, so
    # --install runs exactly the command lines.
    assert ctx.consumer_refresh_commands == "uv tool install pkg@v2.1.0\nvrg-vm update --all"


def test_consumer_refresh_none_stores_no_commands() -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = None
        consumer_refresh(ctx)
    assert ctx.consumer_refresh_commands is None


# -- run_consumer_refresh: execute the command block fail-fast (issue #1643) ----


def test_run_consumer_refresh_executes_via_bash_failfast() -> None:
    with patch(_MOD + ".subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = run_consumer_refresh("uv tool install pkg\nvrg-vm update --all")
    assert rc == 0
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "bash"
    assert cmd[1] == "-c"
    # fail-fast wrapper, then the verbatim command block.
    assert cmd[2].startswith("set -eo pipefail\n")
    assert "uv tool install pkg" in cmd[2]
    assert "vrg-vm update --all" in cmd[2]


def test_run_consumer_refresh_returns_failure_code(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(_MOD + ".subprocess.run") as mock_run:
        mock_run.return_value.returncode = 3
        rc = run_consumer_refresh("false")
    assert rc == 3
    err = capsys.readouterr().err
    assert "install commands failed" in err
    # The release itself is not implicated by a failed local refresh.
    assert "release itself completed" in err
