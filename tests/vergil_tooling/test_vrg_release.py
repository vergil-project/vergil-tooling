from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_release import main, parse_args
from vergil_tooling.lib.release.context import ReleaseContext

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

    from vergil_tooling.lib.release.orchestrator import ReleaseState

_MOD = "vergil_tooling.bin.vrg_release"


def test_parse_args_default() -> None:
    args = parse_args([])
    assert args.version_override is None


def test_parse_args_minor() -> None:
    args = parse_args(["minor"])
    assert args.version_override == "minor"


def test_parse_args_major() -> None:
    args = parse_args(["major"])
    assert args.version_override == "major"


def test_parse_args_no_promote() -> None:
    args = parse_args(["--no-promote"])
    assert args.no_promote is True
    assert args.version_override is None


def test_parse_args_default_promote() -> None:
    args = parse_args([])
    assert args.no_promote is False


def test_parse_args_no_promote_with_minor() -> None:
    args = parse_args(["--no-promote", "minor"])
    assert args.no_promote is True
    assert args.version_override == "minor"


def test_parse_args_has_progress_flags() -> None:
    args = parse_args(["--skip-audit", "--output-format", "plain"])
    assert args.skip_audit is True
    assert args.output_format == "plain"


def test_parse_args_default_skip_audit() -> None:
    args = parse_args([])
    assert args.skip_audit is False
    assert args.output_window is None  # auto-size to terminal height
    assert args.output_format is None


def test_main_runs_pipeline() -> None:
    with (
        patch(_MOD + ".git") as m_git,
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--no-promote", "--output-format", "plain"]) == 0
    state = m_pipeline.call_args.args[0]
    assert state.promote is False
    assert state.repo_root is m_git.repo_root.return_value


def test_main_returns_pipeline_exit_code() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=1),
    ):
        assert main([]) == 1


def test_main_resume_with_bump_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_MOD + ".git"):
        assert main(["minor", "--resume"]) == 1
    assert "cannot be combined" in capsys.readouterr().err


def test_main_resume_finds_target_and_runs_pipeline() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".find_resume_target", return_value=("2.1.0", 42)),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--resume", "--output-format", "plain"]) == 0
    state = m_pipeline.call_args.args[0]
    assert state.resume is True
    assert state.resume_version == "2.1.0"
    assert state.resume_issue_number == 42


def test_main_resume_no_target_returns_1(capsys: pytest.CaptureFixture[str]) -> None:
    from vergil_tooling.lib.release.context import ReleaseError

    err = ReleaseError(phase="resume", command="x", message="No in-flight release to resume.")
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".find_resume_target", side_effect=err),
    ):
        assert main(["--resume"]) == 1
    assert "No in-flight release" in capsys.readouterr().err


def test_main_prints_consumer_refresh_message_after_pipeline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = "Consumer refresh commands:\n\nuv tool install pkg@v2.1.0"

    def fake_pipeline(state: ReleaseState, *args: object, **kwargs: object) -> int:
        state.ctx = ReleaseContext(
            repo="owner/repo",
            version="2.1.0",
            repo_root=Path("/tmp/repo"),  # noqa: S108
            version_override=None,
            consumer_refresh_message=message,
        )
        return 1

    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", side_effect=fake_pipeline),
    ):
        assert main([]) == 1
    captured = capsys.readouterr()
    assert "Consumer refresh commands:" in captured.out
    assert "uv tool install pkg@v2.1.0" in captured.out


def test_main_prints_nothing_when_no_consumer_refresh_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=0),
    ):
        assert main([]) == 0
    assert capsys.readouterr().out == ""


def test_main_passes_version_override() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        main(["minor"])
    state = m_pipeline.call_args.args[0]
    assert state.version_override == "minor"
    assert state.promote is True
    kwargs = m_pipeline.call_args.kwargs
    assert kwargs["command"] == "vrg-release"
    assert kwargs["label"] == "vrg-release"


# -- --install: run the consumer-refresh commands after release (issue #1643) --


def test_parse_args_install_defaults_false() -> None:
    assert parse_args([]).install is False


def test_parse_args_install_flag() -> None:
    assert parse_args(["--install"]).install is True


def _pipeline_with_commands(commands: str | None, rc: int) -> Callable[..., int]:
    """A fake run_pipeline that hydrates ctx with consumer-refresh state."""

    def fake_pipeline(state: ReleaseState, *args: object, **kwargs: object) -> int:
        state.ctx = ReleaseContext(
            repo="owner/repo",
            version="2.1.0",
            repo_root=Path("/tmp/repo"),  # noqa: S108
            version_override=None,
            consumer_refresh_message="Consumer refresh commands:\n\n<cmds>",
            consumer_refresh_commands=commands,
        )
        return rc

    return fake_pipeline


def test_main_install_runs_consumer_refresh_on_success() -> None:
    with (
        patch(_MOD + ".git"),
        patch(
            _MOD + ".progress.run_pipeline",
            side_effect=_pipeline_with_commands("uv tool install pkg\nvrg-vm update --all", 0),
        ),
        patch(_MOD + ".run_consumer_refresh", return_value=0) as m_run,
    ):
        assert main(["--install"]) == 0
    m_run.assert_called_once_with("uv tool install pkg\nvrg-vm update --all")


def test_main_install_returns_consumer_refresh_exit_code() -> None:
    """A failed install propagates its exit code (the release already
    succeeded; only the local refresh failed)."""
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", side_effect=_pipeline_with_commands("cmd", 0)),
        patch(_MOD + ".run_consumer_refresh", return_value=5) as m_run,
    ):
        assert main(["--install"]) == 5
    m_run.assert_called_once()


def test_main_install_skipped_when_release_fails() -> None:
    """A non-zero pipeline must never trigger the install step."""
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", side_effect=_pipeline_with_commands("cmd", 1)),
        patch(_MOD + ".run_consumer_refresh") as m_run,
    ):
        assert main(["--install"]) == 1
    m_run.assert_not_called()


def test_main_install_without_configured_commands_errors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--install with no [publish].consumer-refresh is a clear error, not a
    silent no-op."""
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", side_effect=_pipeline_with_commands(None, 0)),
        patch(_MOD + ".run_consumer_refresh") as m_run,
    ):
        assert main(["--install"]) == 1
    m_run.assert_not_called()
    assert "--install" in capsys.readouterr().err


def test_main_without_install_never_runs_consumer_refresh() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", side_effect=_pipeline_with_commands("cmd", 0)),
        patch(_MOD + ".run_consumer_refresh") as m_run,
    ):
        assert main([]) == 0
    m_run.assert_not_called()
