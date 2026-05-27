"""Tests for vergil_tooling.bin.vrg_gh."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_gh import main


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# -- no arguments / missing subcommand ----------------------------------------


def test_no_args_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_none_argv_reads_sys_argv(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_gh.sys.argv", ["vrg-gh"]):
        assert main(None) != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_top_level_only_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["issue"]) != 0


# -- allowed subcommand pairs ------------------------------------------------

_ALLOWED_PAIRS: list[tuple[str, str]] = [
    ("issue", "view"),
    ("issue", "create"),
    ("issue", "close"),
    ("issue", "reopen"),
    ("issue", "edit"),
    ("issue", "list"),
    ("issue", "comment"),
    ("pr", "view"),
    ("pr", "checks"),
    ("pr", "list"),
    ("pr", "diff"),
    ("pr", "comment"),
    ("pr", "edit"),
    ("run", "list"),
    ("run", "view"),
    ("run", "watch"),
    ("repo", "view"),
    ("repo", "list"),
    ("label", "list"),
    ("label", "create"),
]


@pytest.mark.parametrize(("top", "sub"), _ALLOWED_PAIRS)
def test_allowed_pair_passes(top: str, sub: str) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main([top, sub])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[0] == "gh"
    assert args[1] == top
    assert args[2] == sub


# -- unrecognized subcommands ------------------------------------------------


def test_unrecognized_top_level(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["codespace", "list"]) != 0
    err = capsys.readouterr().err
    assert "codespace" in err


def test_unrecognized_second_level(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["issue", "pin"]) != 0
    err = capsys.readouterr().err
    assert "pin" in err


# -- denied subcommand pairs -------------------------------------------------

_DENIED_PAIRS: list[tuple[str, str]] = [
    ("repo", "edit"),
    ("repo", "create"),
    ("repo", "delete"),
]


@pytest.mark.parametrize(("top", "sub"), _DENIED_PAIRS)
def test_denied_pair(top: str, sub: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([top, sub]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


def test_pr_create_denied_suggests_vrg_submit_pr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["pr", "create"])
    err = capsys.readouterr().err
    assert "vrg-submit-pr" in err


def test_pr_close_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pr", "close"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


# -- top-level denials -------------------------------------------------------


def test_api_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api", "repos/owner/repo"]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


def test_auth_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["auth", "login"]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


# -- pr review flag gating ---------------------------------------------------


def test_pr_review_comment_allowed() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(["pr", "review", "--comment", "-b", "looks good"])
    assert rc == 0


def test_pr_review_no_flags_allowed() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(["pr", "review"])
    assert rc == 0


def test_pr_review_approve_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pr", "review", "--approve"]) != 0
    err = capsys.readouterr().err
    assert "approve" in err.lower()


# -- pr merge ----------------------------------------------------------------


def test_pr_merge_allowed_with_valid_context() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        rc = main(["pr", "merge", "42"])
    assert rc == 0


def test_pr_merge_denied_without_args(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["pr", "merge"]) != 0
    err = capsys.readouterr().err
    assert "denied" in err.lower()


# -- token injection --------------------------------------------------------


def test_injects_app_token_when_available() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value="ghs_app_token",
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs["env"]["GH_TOKEN"] == "ghs_app_token"  # noqa: S105


def test_no_env_injection_when_no_app_token() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert "env" not in kwargs or kwargs.get("env") is None


# -- subprocess passthrough ---------------------------------------------------


def test_subprocess_uses_shell_false() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed()
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs.get("shell") is not True


def test_returns_subprocess_exit_code() -> None:
    err = subprocess.CalledProcessError(128, ["gh", "issue", "list"])
    err.stdout = ""
    err.stderr = "fatal\n"
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
    ):
        rc = main(["issue", "list"])
    assert rc == 128


def test_stdout_and_stderr_replayed_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _completed(stdout="output\n", stderr="warning\n")
        main(["issue", "list"])
    captured = capsys.readouterr()
    assert "output" in captured.out
    assert "warning" in captured.err


def test_stdout_and_stderr_replayed_on_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, ["gh"])
    err.stdout = "partial\n"
    err.stderr = "HTTP 404 Not Found\n"
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
    ):
        rc = main(["issue", "view", "42"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "partial" in captured.out
    assert "404" in captured.err


def test_failure_with_no_output(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, ["gh"])
    err.stdout = ""
    err.stderr = ""
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
    ):
        rc = main(["issue", "view", "42"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


# -- retry behaviour ----------------------------------------------------------


def _api_error(
    returncode: int = 1, stderr: str = "", stdout: str = ""
) -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(returncode=returncode, cmd=["gh"])
    exc.stderr = stderr
    exc.stdout = stdout
    return exc


class TestVrgGhRetry:
    def test_retries_on_502_then_succeeds(self) -> None:
        err = _api_error(stderr="HTTP 502 Bad Gateway")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, _completed(stdout="ok\n")],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            rc = main(["issue", "list"])
        assert rc == 0
        assert mock_run.call_count == 2

    def test_gives_up_after_max_retries(self) -> None:
        err = _api_error(stderr="HTTP 503 Service Unavailable")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            rc = main(["issue", "list"])
        assert rc == 1

    def test_no_retry_on_non_transient_error(self) -> None:
        err = _api_error(stderr="HTTP 404 Not Found")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
        ):
            rc = main(["issue", "view", "42"])
        assert rc == 1
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()
