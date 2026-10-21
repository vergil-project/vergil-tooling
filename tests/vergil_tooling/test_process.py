"""Tests for vergil_tooling.lib.process."""

from __future__ import annotations

import io
import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib.process import exec_replace

if TYPE_CHECKING:
    import pytest


def test_exec_replace_flushes_both_streams_before_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    out_raw, err_raw = io.BytesIO(), io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out_raw, encoding="utf-8"))
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(err_raw, encoding="utf-8"))
    seen: list[tuple[str, str]] = []

    def _exec(_file: str, _args: list[str]) -> None:
        seen.append((out_raw.getvalue().decode(), err_raw.getvalue().decode()))

    print("banner")
    print("warning", file=sys.stderr)
    with patch("vergil_tooling.lib.process.os.execvp", side_effect=_exec) as mock_exec:
        exec_replace("docker", ("docker", "run"))

    mock_exec.assert_called_once_with("docker", ["docker", "run"])
    assert seen == [("banner\n", "warning\n")]


def test_exec_replace_keeps_output_written_to_a_pipe() -> None:
    # End to end: a real exec with stdout as a pipe, the non-TTY case that lost
    # the banner. The printed line must precede the exec'd program's output.
    script = (
        "import sys\n"
        "from vergil_tooling.lib.process import exec_replace\n"
        "print('banner')\n"
        "exec_replace(sys.executable, [sys.executable, '-c', 'print(\"after\")'])\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "banner\nafter\n"
