"""Run-a-command-in-the-guest transport seam.

A ``Transport`` abstracts *how* a guest-side command is executed so the
provisioning helpers in :mod:`vergil_tooling.lib.vm_guest` are written once and
run unchanged over a local Lima instance (:class:`LimaTransport`) or, later, a
remote cloud host. Only the transport differs between backends; the credential
and tooling logic does not.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import NoReturn, Protocol, runtime_checkable

_DEFAULT_WORKDIR = "/tmp"  # noqa: S108
_TERMINAL_ENV_VARS = "COLORTERM,TERM_PROGRAM,TERM_PROGRAM_VERSION"


@runtime_checkable
class Transport(Protocol):
    """Execute commands inside a guest, regardless of how we reach it."""

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR
    ) -> subprocess.CompletedProcess[str]: ...  # pragma: no cover

    def pipe(
        self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR
    ) -> None: ...  # pragma: no cover

    def exec_session(self, workdir: str, inner: str) -> NoReturn: ...  # pragma: no cover


class LimaTransport:
    """Transport over ``limactl shell`` for a local Lima instance."""

    def __init__(self, instance: str) -> None:
        self.instance = instance

    def run(self, *args: str, workdir: str = _DEFAULT_WORKDIR) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                ["limactl", "shell", "--workdir", workdir, self.instance, "--", *args],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None:
        try:
            subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "limactl",
                    "shell",
                    "--workdir",
                    workdir,
                    self.instance,
                    "--",
                    "bash",
                    "-c",
                    cmd,
                ],
                check=True,
                input=input_data,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def exec_session(self, workdir: str, inner: str) -> NoReturn:
        os.environ["LIMA_SHELLENV_ALLOW"] = _TERMINAL_ENV_VARS
        cmd = [
            "limactl",
            "shell",
            "--start",
            "--preserve-env",
            f"--workdir={workdir}",
            self.instance,
            "bash",
            "-c",
            inner,
        ]
        os.execvp(cmd[0], cmd)  # noqa: S606, S607
