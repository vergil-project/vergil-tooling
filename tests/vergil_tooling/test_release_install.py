"""The vrg-release ``install`` stage (spec §4.4 and §4.6 of epic vergil-project/.github#1440)."""

from __future__ import annotations

import argparse
import io
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import defer_publish
from vergil_tooling.lib import progress
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.install import (
    check_line_on_release,
    expand,
    install,
    minor,
    run_install,
)
from vergil_tooling.lib.release.state import initial_state

if TYPE_CHECKING:
    from collections.abc import Iterator


_MOD = "vergil_tooling.lib.release.install"
_TAG_COMMIT = "a" * 40
_TAG_OBJECT = "b" * 40
_OTHER = "c" * 40


def _ctx(tmp_path: Path | None = None, *, promoted: bool = True) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path or Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.tag = "v2.1.0"
    ctx.tag_commit = _TAG_COMMIT
    initial = initial_state(version="2.1.0", cut="c" * 40, issues=(), without_deps=False)
    ctx.state = replace(initial, done=("promote",) if promoted else ())
    return ctx


def _line(commit: str = _TAG_COMMIT, *, annotated: bool = True) -> str:
    """``git ls-remote origin refs/tags/v2.1 refs/tags/v2.1^{}``'s output for *commit*."""
    if annotated:
        return f"{_TAG_OBJECT}\trefs/tags/v2.1\n{commit}\trefs/tags/v2.1^{{}}"
    return f"{commit}\trefs/tags/v2.1"


@pytest.fixture
def template() -> Iterator[MagicMock]:
    """Patch read_config; set ``.return_value.publish.install``."""
    with patch(_MOD + ".config.read_config") as mock_config:
        yield mock_config


@pytest.fixture
def ls_remote() -> Iterator[MagicMock]:
    """Stand in for the vX.Y check's one ``git ls-remote``: vX.Y names the release."""
    with patch(_MOD + ".git.read_output", return_value=_line()) as m_read:
        yield m_read


def _set(mock_config: MagicMock, value: str | None) -> None:
    mock_config.return_value.publish.install = value


# -- the placeholders --------------------------------------------------------------


def test_minor_is_the_release_line() -> None:
    assert minor("2.1.340") == "2.1"


def test_install_expands_minor(template: MagicMock, ls_remote: MagicMock) -> None:
    """<MINOR> is the release's X.Y, so an install follows the line (spec §4.6)."""
    _set(template, "vrg-tooling-install v<MINOR>\necho <VERSION>")
    ctx = _ctx()
    with patch(_MOD + ".run_install", return_value=(0, "")) as m_run:
        install(ctx)
    m_run.assert_called_once_with("vrg-tooling-install v2.1\necho 2.1.0", cwd=ctx.repo_root)
    assert ctx.install_commands == "vrg-tooling-install v2.1\necho 2.1.0"


def test_expand_replaces_every_placeholder() -> None:
    assert expand("a <VERSION> <MINOR> <VERSION>", "3.4.5") == "a 3.4.5 3.4 3.4.5"


# -- the commands run, version-expanded (issues #1643, #1184) ----------------------


def test_install_runs_the_expanded_commands(template: MagicMock, ls_remote: MagicMock) -> None:
    _set(template, "uv tool install pkg@v<VERSION>\nvrg-vm update --all")
    ctx = _ctx()
    with patch(_MOD + ".run_install", return_value=(0, "")) as m_run:
        install(ctx)
    m_run.assert_called_once_with(
        "uv tool install pkg@v2.1.0\nvrg-vm update --all", cwd=ctx.repo_root
    )
    ls_remote.assert_called_once_with("ls-remote", "origin", "refs/tags/v2.1", "refs/tags/v2.1^{}")


def test_install_runs_with_index_deferred(template: MagicMock, ls_remote: MagicMock) -> None:
    """A deferred publish never holds the install back (spec §4.4)."""
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    defer_publish(ctx, "package-index", "docker-publish")
    with patch(_MOD + ".run_install", return_value=(0, "")) as m_run:
        install(ctx)
    m_run.assert_called_once()


def test_install_success_leaves_nothing_to_read(
    template: MagicMock, ls_remote: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    with patch(_MOD + ".run_install", return_value=(0, "Installed 40 packages\n")):
        install(ctx)
    assert ctx.install_message is None
    assert capsys.readouterr().out == ""


def test_install_failure_raises_and_keeps_full_output(
    template: MagicMock, ls_remote: MagicMock
) -> None:
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    output = "Resolving...\nerror: no such tag v2.1.0\n"
    with (
        patch(_MOD + ".run_install", return_value=(2, output)),
        pytest.raises(ReleaseError) as info,
    ):
        install(ctx)
    assert info.value.phase == "install"
    assert "exit 2" in str(info.value)
    # The failure is finished by a resume, never by running the commands by hand.
    assert "vrg-release --resume 2.1.0" in str(info.value)
    assert "by hand" not in str(info.value)
    # The local install log never rides on the error, which is posted on the
    # public tracking issue.
    assert info.value.detail is None
    assert "no such tag" not in str(info.value)
    message = ctx.install_message or ""
    assert "failed (exit 2)" in message
    assert "uv tool install pkg@v2.1.0" in message
    assert "Resolving...\nerror: no such tag v2.1.0" in message


def test_install_none_configured_is_not_a_warning(
    template: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    """No install is the normal case: ``none configured``, no warning (spec §4.6)."""
    _set(template, None)
    ctx = _ctx(promoted=False)
    with (
        patch(_MOD + ".run_install") as m_run,
        patch(_MOD + ".git.read_output") as m_read,
        patch(_MOD + ".progress.note") as m_note,
    ):
        install(ctx)
    m_run.assert_not_called()
    m_read.assert_not_called()
    m_note.assert_called_once_with("none configured")
    assert ctx.install_commands is None
    assert ctx.install_message is None
    assert capsys.readouterr().out == ""


# -- the gate: promote done, and vX.Y names this release -----------------------------


def test_install_refuses_when_promote_failed(template: MagicMock) -> None:
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx(promoted=False)
    ctx.deferred_failures = ["promote"]
    with (
        patch(_MOD + ".run_install") as m_run,
        patch(_MOD + ".git.read_output") as m_read,
        pytest.raises(ReleaseError, match="install needs promote done") as info,
    ):
        install(ctx)
    m_run.assert_not_called()
    m_read.assert_not_called()
    assert info.value.phase == "install"


@pytest.mark.parametrize("annotated", [True, False])
def test_install_refuses_when_line_not_on_release(template: MagicMock, annotated: bool) -> None:
    """vX.Y not on this release when install runs (Review Focus 4): it refuses."""
    _set(template, "uv tool install pkg@v<VERSION>")
    ctx = _ctx()
    with (
        patch(_MOD + ".git.read_output", return_value=_line(_OTHER, annotated=annotated)),
        patch(_MOD + ".run_install") as m_run,
        pytest.raises(ReleaseError, match=r"v2\.1 names cccccccccccc, not v2\.1\.0's") as info,
    ):
        install(ctx)
    m_run.assert_not_called()
    assert info.value.command == "git ls-remote origin refs/tags/v2.1 refs/tags/v2.1^{}"


def test_check_line_passes_a_lightweight_line_tag() -> None:
    ctx = _ctx()
    with patch(_MOD + ".git.read_output", return_value=_line(annotated=False)):
        check_line_on_release(ctx)


def test_check_line_reads_only_the_line_tag_rows() -> None:
    """A row that is not the line tag, or not a ``<sha> <ref>`` row, is not read."""
    ctx = _ctx()
    listing = f"malformed\n{_OTHER}\trefs/tags/v2.10\n" + _line()
    with patch(_MOD + ".git.read_output", return_value=listing):
        check_line_on_release(ctx)


def test_check_line_peeled_commit_wins_over_the_tag_object() -> None:
    """An annotated vX.Y lists its tag object first: only the peeled commit counts."""
    ctx = _ctx()
    ctx.tag_commit = _TAG_OBJECT
    with (
        patch(_MOD + ".git.read_output", return_value=_line()),
        pytest.raises(ReleaseError, match="not v2.1.0's"),
    ):
        check_line_on_release(ctx)


def test_check_line_refuses_a_missing_line_tag() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        pytest.raises(ReleaseError, match=r"v2\.1 does not exist on origin"),
    ):
        check_line_on_release(ctx)


def test_check_line_refuses_without_a_recorded_tag_commit() -> None:
    ctx = _ctx()
    ctx.tag_commit = None
    with (
        patch(_MOD + ".git.read_output") as m_read,
        pytest.raises(ReleaseError, match="no commit is recorded"),
    ):
        check_line_on_release(ctx)
    m_read.assert_not_called()


def test_check_line_read_failure_propagates() -> None:
    """A failed ls-remote is a failure, never read as "no line tag"."""
    ctx = _ctx()
    with (
        patch(_MOD + ".git.read_output", side_effect=OSError("network down")),
        pytest.raises(OSError, match="network down"),
    ):
        check_line_on_release(ctx)


# -- through the pipeline: one check line, or the full output and a failure ------


def _run_stage(ctx: ReleaseContext, tmp_path: Path) -> int:
    args = argparse.Namespace(output_format="plain", output_window=None)
    return progress.run_pipeline(
        ctx,
        [Stage("install", install, mode="fail_defer")],
        command="vrg-release",
        label="vrg-release",
        args=args,
        repo_root=tmp_path,
    )


def test_install_success_is_one_check_line(
    template: MagicMock,
    ls_remote: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set(template, "echo installing vergil-tooling <VERSION>\necho refreshing VMs")
    ctx = _ctx(tmp_path)
    rc = _run_stage(ctx, tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    stage_lines = out.split("\n\n", 1)[0].splitlines()
    assert stage_lines[0] == "→ install  starting..."
    assert len(stage_lines) == 2
    assert stage_lines[1].startswith("✓ install  ")
    assert "installing vergil-tooling" not in out
    assert "refreshing VMs" not in out


def _log_text(tmp_path: Path) -> str:
    return next((tmp_path / ".vergil").glob("vrg-release-*.log")).read_text()


def test_install_success_writes_the_full_run_to_the_log(
    template: MagicMock, ls_remote: MagicMock, tmp_path: Path
) -> None:
    """The terminal is one check line, but the run log holds the commands,
    their output (standard error included) and the exit status (#4812)."""
    _set(template, "echo installing vergil-tooling <VERSION>\necho refreshing VMs >&2")
    ctx = _ctx(tmp_path)
    assert _run_stage(ctx, tmp_path) == 0
    log = _log_text(tmp_path)
    started = log.index("=== install: started ===")
    ended = log.index("=== install: ok (")
    stage = log[started:ended]
    assert (
        "install: running the commands:\n"
        "  echo installing vergil-tooling 2.1.0\n"
        "  echo refreshing VMs >&2\n"
        "installing vergil-tooling 2.1.0\n"
        "refreshing VMs\n"
        "install: the commands exited 0\n"
    ) in stage


def test_install_failure_writes_the_full_run_to_the_log(
    template: MagicMock, ls_remote: MagicMock, tmp_path: Path
) -> None:
    _set(template, "echo step one <VERSION>\necho to-stderr >&2\nexit 7")
    ctx = _ctx(tmp_path)
    assert _run_stage(ctx, tmp_path) == 1
    log = _log_text(tmp_path)
    assert "step one 2.1.0\nto-stderr\ninstall: the commands exited 7\n" in log


def test_install_failure_shows_full_output_and_fails(
    template: MagicMock,
    ls_remote: MagicMock,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set(template, "echo step one <VERSION>\necho to-stderr >&2\nexit 7\necho never-reached")
    ctx = _ctx(tmp_path)
    rc = _run_stage(ctx, tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert any(line.startswith("✗ install  ") and "exit 7" in line for line in out.splitlines())
    # The full output, standard error included, is shown; set -e stopped the block.
    assert "step one 2.1.0\nto-stderr" in out
    message = ctx.install_message or ""
    shown = message.split("Their full output:\n\n", 1)[1]
    assert shown == "step one 2.1.0\nto-stderr"


# -- run_install: one bash -c, fail-fast, output captured ---------------------------


def test_run_install_runs_bash_failfast_and_captures(tmp_path: Path) -> None:
    rc, output = run_install("echo one\nfalse\necho two", cwd=tmp_path)
    assert rc == 1
    assert output == "one\n"


def test_run_install_pipefail(tmp_path: Path) -> None:
    rc, _ = run_install("false | cat", cwd=tmp_path)
    assert rc == 1


def test_run_install_merges_stderr_in_order(tmp_path: Path) -> None:
    rc, output = run_install("echo out1\necho err1 >&2\necho out2", cwd=tmp_path)
    assert rc == 0
    assert output == "out1\nerr1\nout2\n"


def test_run_install_terminates_the_commands_on_interrupt(tmp_path: Path) -> None:
    fake_proc = MagicMock()
    fake_proc.stdout = io.StringIO("")
    fake_proc.wait.side_effect = [KeyboardInterrupt, -15]
    with (
        patch(_MOD + ".subprocess.Popen", return_value=fake_proc),
        pytest.raises(KeyboardInterrupt),
    ):
        run_install("sleep 60", cwd=tmp_path)
    fake_proc.terminate.assert_called_once_with()
    assert fake_proc.wait.call_count == 2


def test_run_install_runs_in_cwd_with_no_stdin(tmp_path: Path) -> None:
    rc, output = run_install("pwd\ncat", cwd=tmp_path)
    assert rc == 0
    assert output == f"{tmp_path.resolve()}\n"
