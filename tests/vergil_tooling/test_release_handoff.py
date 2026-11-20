from __future__ import annotations

import argparse
import io
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import progress
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.handoff import consumer_refresh, run_consumer_refresh

if TYPE_CHECKING:
    from collections.abc import Iterator


_MOD = "vergil_tooling.lib.release.handoff"


def _ctx(tmp_path: Path | None = None) -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path or Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )


@pytest.fixture
def template() -> Iterator[MagicMock]:
    """Patch read_config; set ``.return_value.publish.consumer_refresh``."""
    with patch(_MOD + ".config.read_config") as mock_config:
        yield mock_config


def _set(mock_config: MagicMock, value: str | None) -> None:
    mock_config.return_value.publish.consumer_refresh = value


# -- the commands run, version-expanded (issues #1643, #1184) ---------------------


def test_consumer_refresh_runs_the_expanded_commands(template: MagicMock) -> None:
    _set(template, "uv tool install pkg@v<VERSION>\nvrg-vm update --all")
    ctx = _ctx()
    with patch(_MOD + ".run_consumer_refresh", return_value=(0, "")) as m_run:
        consumer_refresh(ctx)
    m_run.assert_called_once_with(
        "uv tool install pkg@v2.1.0\nvrg-vm update --all", cwd=ctx.repo_root
    )
    assert ctx.consumer_refresh_commands == "uv tool install pkg@v2.1.0\nvrg-vm update --all"


def test_consumer_refresh_success_leaves_nothing_to_read(
    template: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    with patch(_MOD + ".run_consumer_refresh", return_value=(0, "Installed 40 packages\n")):
        consumer_refresh(ctx)
    assert ctx.consumer_refresh_message is None
    assert capsys.readouterr().out == ""


def test_consumer_refresh_failure_raises_and_keeps_full_output(template: MagicMock) -> None:
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    output = "Resolving...\nerror: no such tag v2.1.0\n"
    with (
        patch(_MOD + ".run_consumer_refresh", return_value=(2, output)),
        pytest.raises(ReleaseError) as info,
    ):
        consumer_refresh(ctx)
    assert info.value.phase == "consumer-refresh"
    assert "exit 2" in str(info.value)
    # The local install log never rides on the error, which is posted on the
    # public tracking issue.
    assert info.value.detail is None
    assert "no such tag" not in str(info.value)
    message = ctx.consumer_refresh_message or ""
    assert "failed (exit 2)" in message
    assert "uv tool install pkg@v2.1.0" in message
    assert "Resolving...\nerror: no such tag v2.1.0" in message


def test_consumer_refresh_no_install_shows_the_commands_without_running(
    template: MagicMock,
) -> None:
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    ctx.install = False
    with patch(_MOD + ".run_consumer_refresh") as m_run:
        consumer_refresh(ctx)
    m_run.assert_not_called()
    message = ctx.consumer_refresh_message or ""
    assert "Consumer refresh commands for v2.1.0" in message
    assert "--no-install" in message
    assert "uv tool install pkg@v2.1.0" in message


def test_consumer_refresh_none_configured_is_not_an_error(template: MagicMock) -> None:
    _set(template, None)
    ctx = _ctx()
    with patch(_MOD + ".run_consumer_refresh") as m_run:
        consumer_refresh(ctx)
    m_run.assert_not_called()
    assert ctx.consumer_refresh_commands is None
    assert "no consumer-refresh" in (ctx.consumer_refresh_message or "").lower()


# -- holds when publish is deferred (issue #1853), or after a failed stage --------


def test_consumer_refresh_holds_when_publish_deferred(template: MagicMock) -> None:
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    ctx.deferred_publish_failures = ["docker-publish"]
    with patch(_MOD + ".run_consumer_refresh") as m_run:
        consumer_refresh(ctx)
    m_run.assert_not_called()
    template.assert_not_called()
    assert ctx.consumer_refresh_commands is None
    message = ctx.consumer_refresh_message or ""
    assert "held" in message.lower()
    assert "2.1.0" in message  # version named
    assert "re-run the CD publish" in message


def test_consumer_refresh_hold_gives_per_kind_advice_for_an_index_deferral(
    template: MagicMock,
) -> None:
    ctx = _ctx()
    ctx.deferred_publish_failures = ["package-index"]
    with (
        patch(_MOD + ".publish_deferred_remedy", return_value="re-run publish-index") as remedy,
        patch(_MOD + ".run_consumer_refresh") as m_run,
    ):
        consumer_refresh(ctx)
    m_run.assert_not_called()
    remedy.assert_called_once_with(ctx, ["package-index"])
    message = ctx.consumer_refresh_message or ""
    assert "re-run publish-index" in message
    assert "CD publish" not in message


def test_consumer_refresh_not_run_after_a_failed_stage(template: MagicMock) -> None:
    """A failed earlier stage never triggers the install, as the post-pipeline
    install never ran after a failed pipeline: the commands are shown."""
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    ctx.deferred_failures = ["promote", "update-deps"]
    with patch(_MOD + ".run_consumer_refresh") as m_run:
        consumer_refresh(ctx)
    m_run.assert_not_called()
    assert ctx.consumer_refresh_commands == "uv tool install pkg@v2.1.0"
    message = ctx.consumer_refresh_message or ""
    assert "not run: earlier stages failed: promote, update-deps" in message
    assert "uv tool install pkg@v2.1.0" in message


# -- through the pipeline: one check line, or the full output and a failure ------


def _run_stage(ctx: ReleaseContext, tmp_path: Path) -> int:
    args = argparse.Namespace(output_format="plain", output_window=None)
    return progress.run_pipeline(
        ctx,
        [Stage("consumer-refresh", consumer_refresh, mode="fail_defer")],
        command="vrg-release",
        label="vrg-release",
        args=args,
        repo_root=tmp_path,
    )


def test_consumer_refresh_success_is_one_check_line(
    template: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(template, "echo installing vergil-tooling <VERSION>\necho refreshing VMs")
    ctx = _ctx(tmp_path)
    rc = _run_stage(ctx, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    stage_lines = out.split("\n\n", 1)[0].splitlines()
    assert stage_lines[0] == "→ consumer-refresh  starting..."
    assert len(stage_lines) == 2
    assert stage_lines[1].startswith("✓ consumer-refresh  ")
    assert "installing vergil-tooling" not in out
    assert "refreshing VMs" not in out


def _log_text(tmp_path: Path) -> str:
    return next((tmp_path / ".vergil").glob("vrg-release-*.log")).read_text()


def test_consumer_refresh_success_writes_the_full_run_to_the_log(
    template: MagicMock, tmp_path: Path
) -> None:
    """The terminal is one check line, but the run log holds the commands,
    their output (standard error included) and the exit status (#4812)."""
    _set(template, "echo installing vergil-tooling <VERSION>\necho refreshing VMs >&2")
    ctx = _ctx(tmp_path)
    assert _run_stage(ctx, tmp_path) == 0
    log = _log_text(tmp_path)
    started = log.index("=== consumer-refresh: started ===")
    ended = log.index("=== consumer-refresh: ok (")
    stage = log[started:ended]
    assert (
        "consumer-refresh: running the commands:\n"
        "  echo installing vergil-tooling 2.1.0\n"
        "  echo refreshing VMs >&2\n"
        "installing vergil-tooling 2.1.0\n"
        "refreshing VMs\n"
        "consumer-refresh: the commands exited 0\n"
    ) in stage


def test_consumer_refresh_failure_writes_the_full_run_to_the_log(
    template: MagicMock, tmp_path: Path
) -> None:
    _set(template, "echo step one <VERSION>\necho to-stderr >&2\nexit 7")
    ctx = _ctx(tmp_path)
    assert _run_stage(ctx, tmp_path) == 1
    log = _log_text(tmp_path)
    assert "step one 2.1.0\nto-stderr\nconsumer-refresh: the commands exited 7\n" in log


def test_consumer_refresh_failure_shows_full_output_and_fails(
    template: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(template, "echo step one <VERSION>\necho to-stderr >&2\nexit 7\necho never-reached")
    ctx = _ctx(tmp_path)
    rc = _run_stage(ctx, tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert any(
        line.startswith("✗ consumer-refresh  ") and "exit 7" in line for line in out.splitlines()
    )
    # The full output, standard error included, is shown; set -e stopped the block.
    assert "step one 2.1.0\nto-stderr" in out
    message = ctx.consumer_refresh_message or ""
    shown = message.split("Their full output:\n\n", 1)[1]
    assert shown == "step one 2.1.0\nto-stderr"


# -- run_consumer_refresh: one bash -c, fail-fast, output captured ---------------


def test_run_consumer_refresh_runs_bash_failfast_and_captures(tmp_path: Path) -> None:
    rc, output = run_consumer_refresh("echo one\nfalse\necho two", cwd=tmp_path)
    assert rc == 1
    assert output == "one\n"


def test_run_consumer_refresh_pipefail(tmp_path: Path) -> None:
    rc, _ = run_consumer_refresh("false | cat", cwd=tmp_path)
    assert rc == 1


def test_run_consumer_refresh_merges_stderr_in_order(tmp_path: Path) -> None:
    rc, output = run_consumer_refresh("echo out1\necho err1 >&2\necho out2", cwd=tmp_path)
    assert rc == 0
    assert output == "out1\nerr1\nout2\n"


def test_run_consumer_refresh_terminates_the_commands_on_interrupt(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO("")
    fake_proc.wait.side_effect = [KeyboardInterrupt, -15]
    with (
        patch(_MOD + ".subprocess.Popen", return_value=fake_proc),
        pytest.raises(KeyboardInterrupt),
    ):
        run_consumer_refresh("sleep 60", cwd=tmp_path)
    fake_proc.terminate.assert_called_once_with()
    assert fake_proc.wait.call_count == 2


def test_run_consumer_refresh_runs_in_cwd_with_no_stdin(tmp_path: Path) -> None:
    rc, output = run_consumer_refresh("pwd\ncat", cwd=tmp_path)
    assert rc == 0
    assert output == f"{tmp_path.resolve()}\n"
