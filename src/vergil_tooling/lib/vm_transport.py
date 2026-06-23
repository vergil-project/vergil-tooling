"""Run-a-command-in-the-guest transport seam.

A ``Transport`` abstracts *how* a guest-side command is executed so the
provisioning helpers in :mod:`vergil_tooling.lib.vm_guest` are written once and
run unchanged over a local Lima instance (:class:`LimaTransport`) or, later, a
remote cloud host. Only the transport differs between backends; the credential
and tooling logic does not.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from typing import NoReturn, Protocol, runtime_checkable

_DEFAULT_WORKDIR = "/tmp"  # noqa: S108
_TERMINAL_ENV_VARS = "COLORTERM,TERM_PROGRAM,TERM_PROGRAM_VERSION"


@runtime_checkable
class Transport(Protocol):
    """Execute commands inside a guest, regardless of how we reach it."""

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run a command in the guest, raising on a nonzero exit.

        ``quiet`` suppresses echoing the child's stderr on failure — for probe
        callers (readiness polling) where a connect failure is expected and the
        raw transport error would be misleading noise. The exception still
        carries ``returncode``/``stderr`` for the caller to inspect.
        """
        ...  # pragma: no cover

    def pipe(
        self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR
    ) -> None: ...  # pragma: no cover

    def popen(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR
    ) -> subprocess.Popen[str]: ...  # pragma: no cover

    def exec_session(self, workdir: str, inner: str) -> NoReturn: ...  # pragma: no cover


class LimaTransport:
    """Transport over ``limactl shell`` for a local Lima instance."""

    def __init__(self, instance: str) -> None:
        self.instance = instance

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                ["limactl", "shell", "--workdir", workdir, self.instance, "--", *args],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr and not quiet:
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

    def popen(self, *args: str, workdir: str = _DEFAULT_WORKDIR) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603
            ["limactl", "shell", "--workdir", workdir, self.instance, "--", *args],  # noqa: S607
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

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


class IapTransport:
    """Transport over a GCP IAP SSH tunnel (no public IP, IAM-authed).

    The vm module exposes the box by its *instance name* (``host``, no public IP)
    and a separate ``ssh_user``, so the tunnel addresses it as
    ``gcloud compute ssh <ssh_user>@<host> --tunnel-through-iap``. Same
    run/pipe/exec_session surface as :class:`LimaTransport`.
    """

    def __init__(self, host: str, zone: str, project: str, ssh_user: str) -> None:
        self.host = host
        self.zone = zone
        self.project = project
        self.ssh_user = ssh_user

    def _base(self) -> list[str]:
        return [
            "gcloud",
            "compute",
            "ssh",
            f"{self.ssh_user}@{self.host}",
            "--tunnel-through-iap",
            f"--zone={self.zone}",
            f"--project={self.project}",
        ]

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        remote = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"
        try:
            return subprocess.run(  # noqa: S603
                [*self._base(), f"--command={remote}"],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr and not quiet:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None:
        remote = f"cd {shlex.quote(workdir)} && {cmd}"
        try:
            subprocess.run(  # noqa: S603
                [*self._base(), f"--command={remote}"],  # noqa: S607
                check=True,
                input=input_data,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def popen(self, *args: str, workdir: str = _DEFAULT_WORKDIR) -> subprocess.Popen[str]:
        remote = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"
        # Long-running streaming child (e.g. ``tail -f``): the caller drains
        # stdout line-by-line and terminates it. stderr is folded into stdout so
        # a guest-side error (missing log, permission denied) surfaces in the
        # stream rather than vanishing.
        return subprocess.Popen(  # noqa: S603
            [*self._base(), f"--command={remote}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def exec_session(self, workdir: str, inner: str) -> NoReturn:
        remote = f"cd {workdir} && {inner}"
        cmd = [*self._base(), "--", "-t", remote]
        os.execvp(cmd[0], cmd)  # noqa: S606, S607
