from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_release import main, parse_args
from vergil_tooling.lib.release.context import ReleaseContext
from vergil_tooling.lib.release.gate import unreleased_refusal as _real_unreleased_refusal

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vergil_tooling.lib.release.orchestrator import ReleaseState

_MOD = "vergil_tooling.bin.vrg_release"


@pytest.fixture(autouse=True)
def _released_repo() -> Iterator[None]:
    """Default every test to a repo that declares [publish].release = true.

    The refusal tests override it — the innermost patch wins.
    """
    with patch(_MOD + ".gate.unreleased_refusal", return_value=None):
        yield


_UNRELEASED_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]

[publish]
release = false
"""


@pytest.mark.parametrize("argv", [[], ["--no-install"], ["--resume"], ["minor"]])
def test_main_refuses_repo_not_released(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """[publish].release = false: refuse before the resume lookup or any stage
    (issue #4175)."""
    (tmp_path / "vergil.toml").write_text(_UNRELEASED_TOML)
    with (
        patch(_MOD + ".gate.unreleased_refusal", side_effect=_real_unreleased_refusal),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".find_resume_target") as m_resume,
        patch(_MOD + ".progress.run_pipeline") as m_pipeline,
    ):
        assert main(argv) == 1
    m_resume.assert_not_called()
    m_pipeline.assert_not_called()
    err = capsys.readouterr().err
    assert "vrg-release: this repo is not released ([publish].release" in err


def test_main_released_repo_runs_pipeline(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        _UNRELEASED_TOML.replace("release = false", "release = true")
    )
    with (
        patch(_MOD + ".gate.unreleased_refusal", side_effect=_real_unreleased_refusal),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--output-format", "plain"]) == 0
    m_pipeline.assert_called_once()


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


def test_parse_args_default_with_deps() -> None:
    assert parse_args([]).without_deps is False


def test_main_without_deps_reaches_state() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--without-deps", "--output-format", "plain"]) == 0
    state = m_pipeline.call_args.args[0]
    assert state.without_deps is True
    assert state.promote is True


def test_main_defaults_to_running_deps() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--output-format", "plain"]) == 0
    assert m_pipeline.call_args.args[0].without_deps is False
    assert m_pipeline.call_args.args[0].allow_pending_deps is False


def test_parse_args_default_gates_pending_deps() -> None:
    assert parse_args([]).allow_pending_deps is False


def test_main_allow_pending_deps_reaches_state() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--allow-pending-deps", "--output-format", "plain"]) == 0
    state = m_pipeline.call_args.args[0]
    assert state.allow_pending_deps is True
    assert state.without_deps is False


def test_main_returns_pipeline_exit_code() -> None:
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=1),
    ):
        assert main([]) == 1


def test_main_aborts_under_agent_identity(capsys: pytest.CaptureFixture[str]) -> None:
    """vrg-release is human-only (release/merge/finalize are human actions).
    An agent identity (user/audit) must hard-abort before any side effect —
    no repo resolution, no pipeline — mirroring vrg-submit-pr (issue #1694)."""
    with (
        patch(_MOD + ".identity_mode.is_agent", return_value=True),
        patch(_MOD + ".git") as m_git,
        patch(_MOD + ".progress.run_pipeline") as m_pipeline,
    ):
        rc = main(["--output-format", "plain"])
    assert rc == 1
    m_pipeline.assert_not_called()
    m_git.repo_root.assert_not_called()
    assert "human" in capsys.readouterr().err.lower()


def test_release_refuses_mixed_signals(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human env value cannot override another agent signal (fail closed)."""
    monkeypatch.setenv("VRG_IDENTITY_MODE", "human")
    with (
        patch(_MOD + ".identity_mode.any_agent_signal", return_value=True),
        patch(_MOD + ".git") as m_git,
        patch(_MOD + ".progress.run_pipeline") as m_pipeline,
    ):
        rc = main(["--output-format", "plain"])
    assert rc == 1
    m_pipeline.assert_not_called()
    assert m_git.mock_calls == []
    assert "human" in capsys.readouterr().err.lower()


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
    assert state.without_deps is False


def test_main_resume_allows_without_deps() -> None:
    """--without-deps on a resume reaches the pipeline, which records it (spec §4.5)."""
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch(_MOD + ".find_resume_target", return_value=("2.1.0", 42)),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--resume", "--without-deps", "--output-format", "plain"]) == 0
    state = m_pipeline.call_args.args[0]
    assert state.resume is True
    assert state.without_deps is True


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
    message = "Consumer refresh commands for v2.1.0:\n\nuv tool install pkg@v2.1.0"

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
    assert "Consumer refresh commands for v2.1.0:" in captured.out
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


def test_main_success_label_names_released_tag() -> None:
    """The completion line names the cut tag, read from the release context."""
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        main([])
    state = m_pipeline.call_args.args[0]
    success_label = m_pipeline.call_args.kwargs["success_label"]
    # Before preflight has built the context there is no version to name.
    assert success_label() is None
    state.ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.44",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert success_label() == "vrg-release v2.1.44"


# -- install by default; --no-install skips it (issues #1643, #1184) --


def test_parse_args_no_install_defaults_false() -> None:
    assert parse_args([]).no_install is False


def test_parse_args_no_install_flag() -> None:
    assert parse_args(["--no-install"]).no_install is True


def test_help_lists_no_install_and_hides_install(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--help"])
    out = capsys.readouterr().out
    assert "--no-install" in out
    assert "--install" not in out.replace("--no-install", "")


@pytest.mark.parametrize(("argv", "install"), [([], True), (["--no-install"], False)])
def test_install_flag_reaches_the_stage(argv: list[str], install: bool) -> None:
    """Installing is the default; --no-install tells the consumer-refresh stage
    to show the commands instead of running them."""
    with (
        patch(_MOD + ".git"),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(argv) == 0
    assert m_pipeline.call_args.args[0].install is install


def test_install_flag_is_fatal(capsys: pytest.CaptureFixture[str]) -> None:
    """--install was removed: exit 1 with the exact message, before any stage."""
    with (
        patch(_MOD + ".git") as m_git,
        patch(_MOD + ".progress.run_pipeline") as m_pipeline,
    ):
        assert main(["--install"]) == 1
    m_git.repo_root.assert_not_called()
    m_pipeline.assert_not_called()
    assert capsys.readouterr().err == (
        "vrg-release: --install was removed; installing is now the default "
        "(use --no-install to skip it)\n"
    )
