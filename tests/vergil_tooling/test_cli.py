"""Tests for the ``cli_main`` GitHub auth-error boundary (#3216)."""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.cli import cli_main

if TYPE_CHECKING:
    from collections.abc import Callable

# Functions decorated in this module get their program name from this file.
_PROG = "test-cli"


def _raising(exc: BaseException) -> Callable[[list[str] | None], int]:
    @cli_main
    def main(argv: list[str] | None = None) -> int:
        raise exc

    return main


def _exit_message(exc: Exception, capsys: pytest.CaptureFixture[str]) -> str:
    with pytest.raises(SystemExit) as excinfo:
        _raising(exc)([])
    assert excinfo.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Traceback" not in captured.err
    return captured.err


class TestCliMain:
    def test_passes_arguments_and_return_value_through(self) -> None:
        @cli_main
        def main(argv: list[str] | None = None, *, flag: bool = False) -> int:
            return len(argv or []) + (10 if flag else 0)

        assert main(["a", "b"], flag=True) == 12

    def test_preserves_the_wrapped_function_identity(self) -> None:
        def main(argv: list[str] | None = None) -> int:
            """Docstring."""
            return 0

        wrapped = cli_main(main)
        assert wrapped.__doc__ == "Docstring."
        assert inspect.unwrap(wrapped) is main

    def test_app_auth_error_is_a_clean_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        exc = github.AppAuthError("test-org", "12345", Path("/keys/app.pem"), "HTTP Error 401")
        err = _exit_message(exc, capsys)
        assert err == f"{_PROG}: {github.app_auth_message(exc)}\n"

    def test_no_installation_error_is_a_clean_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exc = github.NoInstallationError("test-org", ["other-org"])
        err = _exit_message(exc, capsys)
        assert err == f"{_PROG}: {github.no_installation_message(exc)}\n"

    def test_missing_token_error_is_a_clean_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exc = github.MissingGitHubTokenError(4, ["gh", "api"], "", "set GH_TOKEN")
        err = _exit_message(exc, capsys)
        assert err == f"{github.missing_token_message(_PROG)}\n"

    def test_epic_home_error_is_a_clean_message(self, capsys: pytest.CaptureFixture[str]) -> None:
        exc = epics.EpicHomeError("cannot resolve the epic home of org/repo: HTTP 502")
        err = _exit_message(exc, capsys)
        assert err == f"{_PROG}: cannot resolve the epic home of org/repo: HTTP 502\n"

    def test_other_exceptions_propagate_unchanged(self, capsys: pytest.CaptureFixture[str]) -> None:
        exc = RuntimeError("boom")
        with pytest.raises(RuntimeError) as excinfo:
            _raising(exc)([])
        assert excinfo.value is exc
        assert capsys.readouterr().err == ""

    def test_other_gh_failures_propagate_unchanged(self) -> None:
        exc = github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 500")
        with pytest.raises(github.GitHubAPIError) as excinfo:
            _raising(exc)([])
        assert excinfo.value is exc

    def test_system_exit_from_main_is_untouched(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            _raising(SystemExit(2))([])
        assert excinfo.value.code == 2


class TestEndToEnd:
    """Real interpreters, as an installed console script sees them."""

    def test_importing_github_leaves_the_excepthook_alone(self) -> None:
        code = (
            "import sys\n"
            "hook = sys.excepthook\n"
            "import vergil_tooling.lib.github\n"
            "assert sys.excepthook is hook\n"
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_app_auth_failure_exits_nonzero_without_traceback(self, tmp_path: Path) -> None:
        # A configured App whose private key is missing: minting the token
        # fails locally (no network), so the command must print the
        # AppAuthError cleanly instead of falling back or tracing back.
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "VRG_APP_ID": "12345",
            "VRG_PRIVATE_KEY_PATH": str(tmp_path / "missing.pem"),
        }
        env.pop("GH_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "vergil_tooling.bin.vrg_roadmap", "--org", "test-org"],
            capture_output=True,
            text=True,
            check=False,
            cwd=tmp_path,
            env=env,
        )
        assert result.returncode == 1
        assert "Traceback" not in result.stderr
        assert result.stderr.startswith(
            "vrg-roadmap: GitHub App authentication failed for owner 'test-org'"
        )
        assert str(tmp_path / "missing.pem") in result.stderr
