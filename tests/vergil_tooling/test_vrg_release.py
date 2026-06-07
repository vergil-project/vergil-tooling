from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_release import main, parse_args
from vergil_tooling.lib.release.context import ReleaseContext

if TYPE_CHECKING:
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
