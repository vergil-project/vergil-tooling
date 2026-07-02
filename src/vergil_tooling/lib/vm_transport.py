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
import time
from typing import TYPE_CHECKING, NoReturn, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable

_DEFAULT_WORKDIR = "/tmp"  # noqa: S108
_TERMINAL_ENV_VARS = "COLORTERM,TERM_PROGRAM,TERM_PROGRAM_VERSION"

# Transient-transport retry for the off-platform transports (IAP tunnel, plain
# ssh). Both ``gcloud compute ssh`` and ``ssh`` exit 255 when the *transport
# itself* fails to connect — the IAP "4003: failed to connect to backend" /
# "Failed to connect to port 22" blip, or ssh's "Connection refused" /
# "Connection closed" — as opposed to a remote command that ran and returned its
# own nonzero exit. A burst of short-lived IAP tunnels (readiness + credentials +
# tooling) can trip a Google-side backend blip that clears in seconds, and a
# single such blip must not discard a multi-minute rebuild (#1992).
#
# Keying the retry on exit 255 alone is safe here: none of the guest-side
# commands the off-platform pipeline runs return 255, so a 255 is unambiguously a
# transport failure, never a real remote-command result. Any other nonzero exit
# is a genuine command failure and is re-raised immediately — retrying it would
# mask a real fault (no-silent-failures).
_CONNECT_FAILURE_RETURNCODE = 255
_CONNECT_RETRIES = 3  # extra attempts after the first (4 total)
_CONNECT_BACKOFF_BASE_SECS = 2.0  # 2s, 4s, 8s — bounded, ~14s worst case


def _run_checked(
    cmd: list[str],
    *,
    quiet: bool,
    input_data: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* under the shared off-platform retry/reporting policy.

    A transient transport-connect failure (exit 255) is retried with bounded
    exponential backoff; every other nonzero exit is a real remote-command
    failure and is raised on the first attempt. On the final failure the child's
    stderr is echoed unless *quiet*.

    ``quiet`` probe callers (the readiness gate's ``_wait_for_ssh`` /
    ``_poll_cloud_init_status``) own their own poll loop and expect the connect
    failure back, so they are never retried here — an internal retry would blow
    out their cadence during the boot race. They also suppress the stderr echo,
    for which raw IAP 4003 noise would be misleading.
    """

    def _call() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            cmd,
            check=True,
            capture_output=True,
            text=True,
            input=input_data,
        )

    return _with_connect_retry(_call, quiet=quiet)


def _with_connect_retry(
    call: Callable[[], subprocess.CompletedProcess[str]],
    *,
    quiet: bool,
) -> subprocess.CompletedProcess[str]:
    """Invoke *call*, retrying only a transport-connect failure (exit 255)."""
    for attempt in range(_CONNECT_RETRIES + 1):
        try:
            return call()
        except subprocess.CalledProcessError as exc:
            transient = exc.returncode == _CONNECT_FAILURE_RETURNCODE
            last = attempt == _CONNECT_RETRIES
            if quiet or not transient or last:
                if exc.stderr and not quiet:
                    print(exc.stderr, end="", file=sys.stderr)
                raise
            delay = _CONNECT_BACKOFF_BASE_SECS * (2**attempt)
            print(
                f"  transient transport failure (exit {exc.returncode}); retrying "
                f"in {delay:.0f}s ({attempt + 1}/{_CONNECT_RETRIES})...",
                file=sys.stderr,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


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
        return _run_checked([*self._base(), f"--command={remote}"], quiet=quiet)

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None:
        remote = f"cd {shlex.quote(workdir)} && {cmd}"
        _run_checked([*self._base(), f"--command={remote}"], quiet=False, input_data=input_data)

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


class SshTransport:
    """Transport over plain ``ssh`` to a public-IP host (Azure off-platform).

    The vm module exposes a routable public IP (``host``) and the box is reached as
    ``ssh -i <key> <ssh_user>@<host>``. The NSG that fronts port 22 is locked to the
    operator's current /32, refreshed at session start (see vm_cloud.nsg_refresh).
    Same run/pipe/popen/exec_session surface as the other transports.
    """

    def __init__(self, host: str, ssh_user: str, key_path: str) -> None:
        self.host = host
        self.ssh_user = ssh_user
        self.key_path = key_path

    def _base(self, *, pty: bool = False) -> list[str]:
        return [
            "ssh",
            *(["-t"] if pty else []),
            "-i",
            self.key_path,
            # accept-new: trust the key on first contact (the box is freshly created and
            # its host key is unknown), but still detect a changed key thereafter.
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "UserKnownHostsFile=~/.config/vergil/known_hosts",
            f"{self.ssh_user}@{self.host}",
        ]

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        remote = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"
        return _run_checked([*self._base(), remote], quiet=quiet)

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None:
        remote = f"cd {shlex.quote(workdir)} && {cmd}"
        _run_checked([*self._base(), remote], quiet=False, input_data=input_data)

    def popen(self, *args: str, workdir: str = _DEFAULT_WORKDIR) -> subprocess.Popen[str]:
        remote = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"
        return subprocess.Popen(  # noqa: S603
            [*self._base(), remote],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def exec_session(self, workdir: str, inner: str) -> NoReturn:
        remote = f"cd {shlex.quote(workdir)} && {inner}"
        cmd = [*self._base(pty=True), remote]
        os.execvp(cmd[0], cmd)  # noqa: S606, S607
