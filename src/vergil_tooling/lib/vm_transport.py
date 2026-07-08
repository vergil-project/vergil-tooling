"""Run-a-command-in-the-guest transport seam.

A ``Transport`` abstracts *how* a guest-side command is executed so the
provisioning helpers in :mod:`vergil_tooling.lib.vm_guest` are written once and
run unchanged over a local Lima instance (:class:`LimaTransport`) or, later, a
remote cloud host. Only the transport differs between backends; the credential
and tooling logic does not.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
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

# --- Hang prevention (#2202) --------------------------------------------------
# Without any bound, a guest command over a wedged connection — most visibly a new
# multiplexed channel opened over a stale SSH ControlMaster whose peer went away —
# blocks forever with no output, indistinguishable from a dead VM. Two independent
# guards prevent that: SSH connection-liveness options (below), and a wall-clock
# backstop on the subprocess.

# ssh keepalive / connect bounding, shared by both off-platform transports.
#   ConnectTimeout      — cap the initial TCP/tunnel connect.
#   ServerAliveInterval — after auth, probe the peer this often when idle.
#   ServerAliveCountMax — drop the connection (ssh exits 255 -> the transient-retry
#                         path) after this many unanswered probes. This kills a
#                         mid-command *network* wedge WITHOUT capping a healthy
#                         long-running command's runtime.
# ~60s to declare a live-then-dead connection dead — generous enough not to trip on
# a heavily loaded box mid-rebuild.
_SSH_CONNECT_TIMEOUT = 30
_SSH_ALIVE_INTERVAL = 15
_SSH_ALIVE_COUNT_MAX = 4


def _ssh_keepalive_options() -> list[tuple[str, str]]:
    """(key, value) ssh ``-o`` options that bound a wedged/unreachable connection."""
    return [
        ("ConnectTimeout", str(_SSH_CONNECT_TIMEOUT)),
        ("ServerAliveInterval", str(_SSH_ALIVE_INTERVAL)),
        ("ServerAliveCountMax", str(_SSH_ALIVE_COUNT_MAX)),
    ]


# Wall-clock backstop on a single guest command. The keepalive options above catch a
# dead *network* connection, but opening a new multiplexed channel over a wedged
# ControlMaster blocks on the local control socket before keepalives apply, so it
# needs a hard cap. Generous by design: guest provisioning steps legitimately run
# for minutes, so this must never trip a healthy command — it only turns an
# otherwise-infinite hang into a loud, actionable failure. Override via env; 0 or a
# negative value disables it (an explicit opt-out).
_GUEST_CMD_TIMEOUT_ENV = "VERGIL_VM_CMD_TIMEOUT"
_GUEST_CMD_TIMEOUT_DEFAULT_SECS = 600.0


def _guest_cmd_timeout() -> float | None:
    """Per-command wall-clock bound in seconds, or ``None`` when disabled."""
    raw = os.environ.get(_GUEST_CMD_TIMEOUT_ENV, "").strip()
    if not raw:
        return _GUEST_CMD_TIMEOUT_DEFAULT_SECS
    try:
        secs = float(raw)
    except ValueError:
        return _GUEST_CMD_TIMEOUT_DEFAULT_SECS
    return secs if secs > 0 else None


# Opt-in diagnostics: the operator's triage aid when a guest command misbehaves.
_DEBUG_ENV = "VERGIL_VM_DEBUG"
_DEBUG_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _debug_enabled() -> bool:
    return os.environ.get(_DEBUG_ENV, "").strip().lower() in _DEBUG_TRUTHY


def _debug(msg: str) -> None:
    # Callers guard with _debug_enabled() so the (potentially expensive) message is
    # only built when tracing is on.
    print(f"[vm-transport] {msg}", file=sys.stderr)


class TransportTimeoutError(RuntimeError):
    """A guest command exceeded its wall-clock bound (#2202).

    Raised instead of hanging forever so the operator gets an actionable failure
    (which command, on which host, and how to recover) rather than a dead terminal.
    """

    def __init__(self, cmd: list[str], timeout: float) -> None:
        self.cmd = cmd
        self.timeout = timeout
        super().__init__(
            f"guest command timed out after {timeout:.0f}s — the connection may be "
            f"wedged (e.g. a stale SSH ControlMaster whose peer went away):\n"
            f"  {shlex.join(cmd)}\n"
            f"Recover: retry with VERGIL_VM_DISABLE_SSH_MUX=1 to bypass connection "
            f"multiplexing, or reap the master with `ssh -O exit`. Tune the bound "
            f"with {_GUEST_CMD_TIMEOUT_ENV} (0 disables it); set {_DEBUG_ENV}=1 for "
            f"per-command tracing."
        )


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

    A wall-clock timeout (:func:`_guest_cmd_timeout`) is applied so a wedged
    connection fails loudly instead of hanging forever (#2202); it raises
    :class:`TransportTimeoutError` and is *not* retried — a timeout already burned its
    full budget, so another attempt would only burn more.
    """
    if _debug_enabled():
        _debug(f"run (timeout={_guest_cmd_timeout()}): {shlex.join(cmd)}")

    def _call() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                cmd,
                check=True,
                capture_output=True,
                text=True,
                input=input_data,
                timeout=_guest_cmd_timeout(),
            )
        except subprocess.TimeoutExpired as exc:
            raise TransportTimeoutError(cmd, exc.timeout) from exc

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


# ---------------------------------------------------------------------------
# SSH connection multiplexing (ControlMaster/ControlPath/ControlPersist).
#
# Off-platform pipelines run ~18-20 guest commands back-to-back, each of which
# would otherwise open a fresh IAP tunnel + SSH handshake + auth. That burst is
# the *trigger* behind the IAP "4003: failed to connect to backend" blip (#1992
# retries the residual; this removes most of them) and leaves ~25 half-open
# sessions lingering until sshd's ClientAlive reaps them. Multiplexing makes a
# whole pipeline ride one underlying connection: the first command opens the
# master, the rest reuse it over its control socket, and the master self-reaps
# ``_CONTROL_PERSIST`` after the last client disconnects.
#
# The socket is a filesystem path keyed by (host, workdir), so every per-step
# transport object the pipeline builds for the same box shares one master
# automatically — no need to thread a single transport through the pipeline.
# ---------------------------------------------------------------------------

# Idle lifetime of the background master after the last channel closes. Explicit
# teardown (``close()``) kills it immediately on pipeline exit; this is the
# backstop that reaps a master a crashed run failed to close.
_CONTROL_PERSIST = "60s"

# 16 hex chars of a sha256 keeps the socket filename short so the full path stays
# well under the Unix domain-socket ``sun_path`` cap (104 on macOS, 108 on Linux)
# even for long home directories — a real failure mode for naive temp/state paths.
_SOCKET_HASH_LEN = 16

# Kill-switch: multiplexing is on by default; set this env truthy to disable all
# injection so the transports behave exactly as they did pre-#2088. Cheap insurance
# for the off-platform path, which cannot be validated on a real box pre-merge.
_MUX_DISABLE_ENV = "VERGIL_VM_DISABLE_SSH_MUX"
_MUX_DISABLE_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _mux_disabled() -> bool:
    return os.environ.get(_MUX_DISABLE_ENV, "").strip().lower() in _MUX_DISABLE_TRUTHY


def _cwd() -> str:
    """Current working directory as a string — the worktree discriminator for the
    control socket. Isolated in a helper so callers stay short and testable."""
    return str(Path.cwd())


def _control_dir() -> Path:
    """Directory holding the per-box control sockets.

    Under ``~/.config/vergil`` to match the existing off-platform state (e.g.
    ``SshTransport``'s ``UserKnownHostsFile``) and to stay short enough for the
    socket-path length cap.
    """
    return Path.home() / ".config" / "vergil" / "cm"


def control_socket_path(host: str, workdir: str) -> Path:
    """Deterministic control-socket path for a (host, workdir) pair.

    ``host`` is already globally unique per box (a hash of identity/org/repo/name);
    ``workdir`` adds worktree isolation so two worktrees reaching the same box get
    distinct sockets and never clobber each other's master. Within one pipeline the
    cwd is constant, so every step resolves to the same socket and shares the master.
    """
    digest = hashlib.sha256(f"{host}\0{workdir}".encode()).hexdigest()[:_SOCKET_HASH_LEN]
    return _control_dir() / digest


def ssh_mux_options(host: str, workdir: str) -> list[tuple[str, str]]:
    """SSH multiplexing ``-o`` options for a box, or ``[]`` when disabled.

    Creating the control dir (0700) is a side effect here because ssh requires the
    socket's parent to exist before it opens the master.
    """
    if _mux_disabled():
        return []
    socket = control_socket_path(host, workdir)
    socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return [
        ("ControlMaster", "auto"),
        ("ControlPath", str(socket)),
        ("ControlPersist", _CONTROL_PERSIST),
    ]


def _shutdown_master(dest: str, control_path: Path) -> None:
    """Best-effort teardown of a shared SSH control master.

    ``ssh -O exit`` signals the background master over its control socket to exit
    now; with no socket it fails fast without opening a connection, so the error is
    swallowed. The socket file is then removed. ``ControlPersist`` is the final
    backstop for any master this could not reach.
    """
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603
            ["ssh", "-o", f"ControlPath={control_path}", "-O", "exit", dest],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
        )
    with contextlib.suppress(OSError):
        control_path.unlink(missing_ok=True)


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

    def close(self) -> None:
        """Tear down any shared connection state (no-op for connectionless backends)."""
        ...  # pragma: no cover


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

    def close(self) -> None:
        """No persistent connection to tear down — limactl has no control master."""
        return


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
        # Multiplexing options ride the underlying ssh via ``--ssh-flag``. The
        # glued ``-oKey=Val`` form (no space) is used deliberately: gcloud splits a
        # ``--ssh-flag`` value on spaces, so ``-o ControlPath=…`` would arrive as
        # two mangled tokens — the glued form passes through intact.
        mux = [f"--ssh-flag=-o{key}={value}" for key, value in ssh_mux_options(self.host, _cwd())]
        # Connection-liveness bounding (#2202) rides the same glued --ssh-flag form.
        keepalive = [f"--ssh-flag=-o{key}={value}" for key, value in _ssh_keepalive_options()]
        return [
            "gcloud",
            "compute",
            "ssh",
            f"{self.ssh_user}@{self.host}",
            "--tunnel-through-iap",
            f"--zone={self.zone}",
            f"--project={self.project}",
            *keepalive,
            *mux,
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

    def close(self) -> None:
        """Tear down the shared IAP/SSH control master for this box (best effort)."""
        if _mux_disabled():
            return
        _shutdown_master(f"{self.ssh_user}@{self.host}", control_socket_path(self.host, _cwd()))


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
        # Connection-liveness bounding (#2202) + multiplexing, both as split -o pairs.
        opts: list[str] = []
        for key, value in [*_ssh_keepalive_options(), *ssh_mux_options(self.host, _cwd())]:
            opts += ["-o", f"{key}={value}"]
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
            *opts,
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

    def close(self) -> None:
        """Tear down the shared SSH control master for this box (best effort)."""
        if _mux_disabled():
            return
        _shutdown_master(f"{self.ssh_user}@{self.host}", control_socket_path(self.host, _cwd()))
