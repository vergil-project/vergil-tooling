"""Tests for vergil_tooling.lib.process_errors."""

from __future__ import annotations

import subprocess

import pytest

from vergil_tooling.lib import process_errors


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [("uv lock", "uv lock"), (["uv", "lock"], "uv lock"), (("a", 1), "a 1"), (7, "7")],
)
def test_command_text(cmd: object, expected: str) -> None:
    assert process_errors.command_text(cmd) == expected


def test_failure_message_argv_list() -> None:
    exc = subprocess.CalledProcessError(100, ["apt-get", "update"])
    assert (
        process_errors.failure_message(exc)
        == "Command 'apt-get update' returned non-zero exit status 100."
    )


def test_failure_message_string_command() -> None:
    exc = subprocess.CalledProcessError(2, "make check")
    assert process_errors.failure_message(exc) == (
        "Command 'make check' returned non-zero exit status 2."
    )


def test_failure_message_signal() -> None:
    exc = subprocess.CalledProcessError(-9, ["sleep", "60"])
    assert process_errors.failure_message(exc) == "Command 'sleep 60' died with signal 9."


def test_failure_message_ignores_output() -> None:
    exc = subprocess.CalledProcessError(1, ["git"], output="out", stderr="err")
    assert process_errors.failure_message(exc) == "Command 'git' returned non-zero exit status 1."


def test_failure_detail_empty_output_is_the_message() -> None:
    exc = subprocess.CalledProcessError(1, ["gh", "api"], output="", stderr="  \n")
    assert process_errors.failure_detail(exc) == (
        "Command 'gh api' returned non-zero exit status 1."
    )


def test_failure_detail_no_output() -> None:
    exc = subprocess.CalledProcessError(1, ["gh", "api"])
    assert process_errors.failure_detail(exc) == (
        "Command 'gh api' returned non-zero exit status 1."
    )


def test_failure_detail_labels_stderr_then_stdout() -> None:
    exc = subprocess.CalledProcessError(1, ["gh", "api"], output="{}\n", stderr="HTTP 404\n")
    assert process_errors.failure_detail(exc) == (
        "Command 'gh api' returned non-zero exit status 1.\nstderr: HTTP 404\nstdout: {}"
    )


def test_failure_detail_decodes_bytes() -> None:
    exc = subprocess.CalledProcessError(1, ["git"], output=b"", stderr=b"fatal: \xff bad\n")
    assert process_errors.failure_detail(exc) == (
        "Command 'git' returned non-zero exit status 1.\nstderr: fatal: � bad"
    )


def test_error_text_plain_called_process_error_uses_detail() -> None:
    exc = subprocess.CalledProcessError(1, ["git", "fetch"], stderr="fatal: no remote")
    assert process_errors.error_text(exc) == (
        "Command 'git fetch' returned non-zero exit status 1.\nstderr: fatal: no remote"
    )


def test_error_text_without_output_is_the_message() -> None:
    exc = subprocess.CalledProcessError(1, ["git", "fetch"], stderr="fatal: no remote")
    assert process_errors.error_text(exc, output=False) == (
        "Command 'git fetch' returned non-zero exit status 1."
    )


def test_error_text_without_output_keeps_an_owned_str() -> None:
    class _Owned(subprocess.CalledProcessError):
        def __str__(self) -> str:
            return "owned message"

    assert process_errors.error_text(_Owned(1, ["x"]), output=False) == "owned message"


def test_error_text_subclass_without_str_uses_detail() -> None:
    class _Sub(subprocess.CalledProcessError):
        pass

    exc = _Sub(3, ["x"])
    assert process_errors.error_text(exc) == "Command 'x' returned non-zero exit status 3."


def test_error_text_subclass_with_own_str_keeps_it() -> None:
    class _Owned(subprocess.CalledProcessError):
        def __str__(self) -> str:
            return "owned message"

    assert process_errors.error_text(_Owned(1, ["x"])) == "owned message"


def test_error_text_other_exception_is_str() -> None:
    assert process_errors.error_text(ValueError("bad value")) == "bad value"
