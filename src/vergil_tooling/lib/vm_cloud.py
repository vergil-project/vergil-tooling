"""Off-platform (cloud) VM backend: tofu two-state lifecycle + IAP transport."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import progress
from vergil_tooling.lib.vm_spec import spec_fingerprint, state_slug
from vergil_tooling.lib.vm_transport import IapTransport

if TYPE_CHECKING:
    from vergil_tooling.lib.identity import Identity
    from vergil_tooling.lib.vm_spec import ComposedSpec
    from vergil_tooling.lib.vm_transport import Transport


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or "x"


def cloud_resource_name(slug: str) -> str:
    """Deterministic short RFC1035 name 'vrg-<first 12 hex of sha256(slug)>' (16 chars).

    The four-segment slug overflows GCP's 63-char limit, so the readable slug keys
    the state path / labels while cloud resources take this opaque hash. Identity is
    carried in labels (which `list` and `tofu import` read), never in the name.
    """
    digest = hashlib.sha256(slug.encode()).hexdigest()[:12]
    return f"vrg-{digest}"


def cloud_labels(identity: str, org: str, repo: str, name: str | None = None) -> dict[str, str]:
    """Structured labels for label-based recovery (independent of the hashed name)."""
    labels = {
        "vergil-identity": _slug(identity),
        "vergil-org": _slug(org),
        "vergil-repo": _slug(repo),
    }
    if name:
        labels["vergil-instance"] = _slug(name)
    return labels


# GitHub auto-generates a source archive for any tag at this URL — a single
# unauthenticated GET, no git/clone/checkout (the cloud analog of the raw agent.yaml
# fetch). The version lives ONLY in the tag here: a versioned *release asset* would
# embed the version in both the path segment and the filename, and `v2.1` is a moving
# git tag (not a release), so there is no release coordinate to fetch. The moving tag
# tracks the latest 2.1.x, so the archive always reflects the current opentofu/ tree.
_MODULES_URL = "https://github.com/vergil-project/vergil-vm/archive/refs/tags/{tag}.tar.gz"
_TAG_RE = re.compile(r"^v\d+\.\d+(\.\d+)?$")


def fetch_modules(tag: str) -> Path:
    """Download the vergil-vm OpenTofu modules at *tag* and return their modules root.

    Fetches GitHub's source archive for the tag. The archive roots at
    ``vergil-vm-<ref>/`` (GitHub strips the leading ``v``), so the modules live under
    ``vergil-vm-<ref>/opentofu/modules`` — found via a single-segment glob rather than
    a fixed root.
    """
    if not _TAG_RE.fullmatch(tag):
        print(f"ERROR: invalid module tag '{tag}' (expected vN.N or vN.N.N)", file=sys.stderr)
        raise SystemExit(1)
    url = _MODULES_URL.format(tag=tag)
    tmp = Path(tempfile.mkdtemp(prefix="vergil-modules-"))
    archive = tmp / "modules.tar.gz"
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310
            archive.write_bytes(resp.read())
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")  # noqa: S202
    except (urllib.error.URLError, tarfile.TarError, OSError) as exc:
        print(f"ERROR: failed to fetch modules from {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    matches = sorted(tmp.glob("*/opentofu/modules"))
    if not matches:
        print(f"ERROR: archive missing */opentofu/modules ({url})", file=sys.stderr)
        raise SystemExit(1)
    return matches[0]


def provision_params(
    *,
    packages: list[str] | None = None,
    apt_repos: list[dict[str, str]] | None = None,
    vagrant_plugins: list[str] | None = None,
    port_forwards: list[str] | None = None,
    nested: bool = False,
    fingerprint: str | None = None,
) -> dict[str, str]:
    """Assemble the provisioning ``param`` map shared by Lima and the cloud backend.

    The encodings are byte-identical to the values ``lima.create_vm`` passes via
    ``--set=.param.*`` so the same profile yields the same box on either backend:
    packages and vagrant plugins are space-joined; each apt repo is encoded
    ``name|key_url|uri|suite|components`` with repos joined by ``;``; port forwards
    are ``;``-joined; ``NESTED_VIRT``/``SPEC_FINGERPRINT`` are passthrough strings.
    Keys for unset pieces are omitted entirely (mirroring create_vm's ``if`` guards).
    """
    params: dict[str, str] = {}
    if packages:
        params["EXTRA_PACKAGES"] = " ".join(packages)
    if apt_repos:
        params["APT_REPOS"] = ";".join(
            "|".join((r["name"], r["key_url"], r["uri"], r["suite"], r["components"]))
            for r in apt_repos
        )
    if vagrant_plugins:
        params["VAGRANT_PLUGINS"] = " ".join(vagrant_plugins)
    if port_forwards:
        params["PORT_FORWARDS"] = ";".join(port_forwards)
    if nested:
        params["NESTED_VIRT"] = "true"
    if fingerprint:
        params["SPEC_FINGERPRINT"] = fingerprint
    return params


# Every provision/*.sh sources provision.env under ``set -u`` and reads these keys
# directly (no ``${VAR:-}`` default), so the body must DEFINE all of them even when the
# spec leaves them unset — otherwise the script aborts on an unbound variable. The set and
# its empty defaults mirror Lima's ``agent.yaml.skel`` ``param:`` block byte-for-byte (the
# backend-neutral contract): Lima's template substitutes every ``.Param.*`` to empty, so
# the cloud writer must do the same. ``provision_params`` omits unset keys (correct for
# Lima's ``--set`` path), so we re-establish the full set here.
_PROVISION_ENV_DEFAULTS = {
    "EXTRA_PACKAGES": "",
    "APT_REPOS": "",
    "VAGRANT_PLUGINS": "",
    "SPEC_FINGERPRINT": "",
    "NESTED_VIRT": "",
    "PORT_FORWARDS": "",
}


def render_provision_env(params: dict[str, str], *, vergil_user: str, home: str) -> str:
    """Render the cloud ``provision.env`` body: the full canonical key set plus VERGIL_USER/HOME.

    ``params`` (from ``provision_params``, which omits unset keys) overrides the empty
    ``_PROVISION_ENV_DEFAULTS`` so every key the provision scripts source is always defined
    — provisioning runs under ``set -u`` and aborts on any unbound variable.

    Every value is ``shlex.quote``-escaped: the scripts source this file (``. provision.env``),
    so a multi-token value (EXTRA_PACKAGES' space-joined list, PORT_FORWARDS' ``|``/``;``
    records, APT_REPOS) written raw would be parsed as a ``VAR=x cmd args`` line instead of a
    plain assignment. (#1805)
    """
    merged = {**_PROVISION_ENV_DEFAULTS, **params}
    lines = [f"{key}={shlex.quote(value)}" for key, value in merged.items()]
    lines.append(f"VERGIL_USER={shlex.quote(vergil_user)}")
    lines.append(f"HOME={shlex.quote(home)}")
    return "\n".join(lines)


_TOFU_MIN = (1, 8, 0)


def _tofu_version_ok(stdout: str) -> bool:
    try:
        data = json.loads(stdout)
        raw = str(data["terraform_version"])
        parts = tuple(int(p) for p in raw.split("."))
    except (json.JSONDecodeError, KeyError, ValueError):
        return False
    return parts >= _TOFU_MIN


def preflight() -> None:
    """Verify the cloud host prerequisites: OpenTofu >= 1.8.0, gcloud, and ADC.

    Each missing or unusable piece aborts with its own specific remediation
    rather than letting an opaque ``tofu``/``gcloud`` error surface later.
    """
    if shutil.which("tofu") is None:
        print("ERROR: OpenTofu not found — install OpenTofu >= 1.8.0", file=sys.stderr)
        raise SystemExit(1)
    try:
        result = subprocess.run(  # noqa: S603
            ["tofu", "version", "-json"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(
            "ERROR: could not query OpenTofu version — install OpenTofu >= 1.8.0",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    if not _tofu_version_ok(result.stdout):
        print("ERROR: OpenTofu too old — install OpenTofu >= 1.8.0", file=sys.stderr)
        raise SystemExit(1)

    if shutil.which("gcloud") is None:
        print("ERROR: gcloud not found — install the gcloud CLI", file=sys.stderr)
        raise SystemExit(1)

    try:
        subprocess.run(  # noqa: S603
            ["gcloud", "auth", "application-default", "print-access-token"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print(
            "ERROR: no application-default credentials — run: "
            "gcloud auth application-default login",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def bootstrap_volume(transport: Transport, identity: Identity, org: str, repo: str) -> None:
    """Clone the repo onto the persistent volume, reattach (fetch), or skip.

    - ``auth_type == "none"``: credential-less identity — skip checkout, logged.
    - existing ``/vergil/projects/<org>/<repo>``: reattached volume — fetch only.
    - absent: fresh volume — in-guest ``vrg-git clone`` + seed ``/vergil/claude``.
    """
    if identity.auth_type == "none":
        print("  Skipping checkout (credential-less identity)")
        return
    path = f"/vergil/projects/{org}/{repo}"
    try:
        transport.run("test", "-d", path)
    except subprocess.CalledProcessError:
        print(f"  Cloning {org}/{repo} onto the volume...")
        transport.run("vrg-git", "clone", f"https://github.com/{org}/{repo}.git", path)
        transport.run("mkdir", "-p", "/vergil/claude")
    else:
        print(f"  Reattaching existing checkout for {org}/{repo}...")
        # vrg-git (not raw git) so the installation token is injected, run inside the
        # repo so `fetch` resolves the org from its own remote (#1790).
        transport.run("vrg-git", "fetch", "--all", workdir=path)


_FINGERPRINT_PATH = "/etc/vergil/vm-spec.fingerprint"
_CLOUD_INIT_LOG = "/var/log/cloud-init-output.log"

# How often the readiness gate re-queries cloud-init and emits an elapsed
# heartbeat. cloud-init provisioning on a fresh box runs many minutes, so a
# ~20s cadence gives the operator a steady "still alive" signal without
# hammering the IAP tunnel.
_READINESS_POLL_SECS = 20.0

# `ssh` (and `gcloud compute ssh`'s tunnel) exits 255 when the transport itself
# fails to connect — on a fresh box that is the IAP "4003: failed to connect to
# port 22" boot race, where the instance is RUNNING but sshd is not accepting
# yet. Keying off this code tells a transient connect failure (retry) apart from
# cloud-init's own error(1)/degraded(2) exits (terminal).
_CONNECT_FAILURE_RETURNCODE = 255

# The SSH-readiness wait probes a trivial command on this cadence, bounded by an
# overall timeout. The cadence is brisk because the boot race usually clears in
# seconds; the timeout is generous because a box not answering SSH after several
# minutes has genuinely failed to boot — distinct from cloud-init merely taking
# a long time, which the (unbounded) cloud-init poll handles separately.
_SSH_PROBE_INTERVAL_SECS = 5.0
_SSH_READY_TIMEOUT_SECS = 300.0

# cloud-init's terminal states (``cloud-init status`` output). "done" is the
# only clean success; "error" and "degraded done" mean provisioning finished
# with faults — both of which the old blocking ``status --wait`` surfaced as a
# nonzero exit, so we preserve that by treating them as hard failures.
_CLOUD_INIT_DONE = "done"
_CLOUD_INIT_FAILED = frozenset({"error", "degraded done"})


def _parse_cloud_init_status(stdout: str) -> tuple[str, str]:
    """Pull ``(status, detail)`` from ``cloud-init status --long`` output.

    Returns empty strings for any field not present — e.g. a transient SSH
    failure yields no ``status:`` line, which the caller reads as "not ready
    yet" and keeps polling.
    """
    status = ""
    detail = ""
    for raw in stdout.splitlines():
        line = raw.strip()
        if line.startswith("status:"):
            status = line.split(":", 1)[1].strip()
        elif line.startswith("detail:"):
            detail = line.split(":", 1)[1].strip()
    return status, detail


def _is_connection_failure(exc: subprocess.CalledProcessError) -> bool:
    """True when *exc* is the transport failing to connect (ssh/IAP exit 255).

    Distinguishes "cannot reach the box yet" (transient — retry) from a command
    that ran and exited nonzero (e.g. cloud-init's error/degraded states), so the
    readiness gate never mistakes a boot race for a provisioning fault.
    """
    return exc.returncode == _CONNECT_FAILURE_RETURNCODE


def _wait_for_ssh(
    transport: Transport,
    *,
    timeout_secs: float = _SSH_READY_TIMEOUT_SECS,
    poll_secs: float = _SSH_PROBE_INTERVAL_SECS,
) -> None:
    """Block until the guest accepts a trivial SSH command, or raise on timeout.

    A fresh box's sshd is not listening the instant the instance reports
    RUNNING, so the first probes race guest boot and the IAP tunnel fails to
    connect (ssh exit 255 / IAP 4003). Those are "not ready yet", not failures:
    we retry on a bounded cadence — emitting a heartbeat so the wait is visible —
    until a trivial command succeeds. The probe runs ``quiet`` so the expected
    connect errors do not spam the operator with misleading 4003 noise. Only an
    exhausted timeout is terminal, and its message says the box "never became
    reachable" (distinct from a cloud-init fault).
    """
    started = time.monotonic()
    deadline = started + timeout_secs
    while True:
        try:
            transport.run("true", quiet=True)
            return
        except subprocess.CalledProcessError as exc:
            now = time.monotonic()
            if now >= deadline:
                raise RuntimeError(
                    f"SSH never became reachable on the cloud box within "
                    f"{int(timeout_secs)}s (last exit {exc.returncode}) — "
                    "the box may have failed to boot"
                ) from exc
            beat = _readiness_heartbeat(now - started, "", "waiting for SSH (still booting)")
            progress.emit(beat)
            time.sleep(poll_secs)


def _poll_cloud_init_status(transport: Transport) -> tuple[str, str]:
    """Query cloud-init once and return its ``(status, detail)``.

    ``cloud-init status`` exits nonzero for the error(1)/degraded(2) states, but
    the status line still rides on stdout — so we parse the captured output of a
    failed call too. A transport-connect failure (ssh exit 255: SSH dropped /
    not up yet) is told apart from those by its return code and yields an empty
    status the poll loop treats as non-terminal. The probe runs ``quiet`` so a
    transient connect drop mid-provision does not spam a raw IAP error; a real
    cloud-init fault still surfaces via the parsed status the loop raises on.
    """
    try:
        result = transport.run("cloud-init", "status", "--long", quiet=True)
    except subprocess.CalledProcessError as exc:
        if _is_connection_failure(exc):
            return "", ""
        return _parse_cloud_init_status(exc.stdout or "")
    return _parse_cloud_init_status(result.stdout)


def _readiness_heartbeat(elapsed: float, status: str, detail: str) -> str:
    """One elapsed-vs-stage line, mirroring the Lima backend's ``[elapsed]`` beat."""
    stage = detail or status or "connecting"
    return f"[cloud-init] {progress.format_elapsed(elapsed)} elapsed — {stage}"


class _CloudInitTail:
    """Background ``tail -f`` of the guest cloud-init log, relayed to the renderer.

    Opt-in (``--verbose`` / ``VERGIL_VM_VERBOSE``) live provisioning output. The
    poll loop owns readiness; this is display-only, so a tail that never starts
    (older box, missing log) degrades to heartbeats rather than failing the gate.
    """

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._proc: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # Spawn synchronously so ``_proc`` is set before the poll loop runs —
        # the thread only drains stdout, keeping ``stop()`` race-free.
        try:
            # ``-n +1`` replays the log from the top so output already written
            # before we attached is not lost.
            self._proc = self._transport.popen("sudo", "tail", "-n", "+1", "-f", _CLOUD_INIT_LOG)
        except OSError as exc:  # spawning the tunnel failed — degrade to heartbeats
            progress.emit(f"[cloud-init] live tail unavailable: {exc}")
            return
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        assert self._proc is not None  # noqa: S101 — start() set it before spawning the thread
        assert self._proc.stdout is not None  # noqa: S101 — Popen(stdout=PIPE) guarantees it
        for raw in self._proc.stdout:
            line = raw.rstrip("\n")
            if line:
                progress.emit(f"[cloud-init] {line}")

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._proc.wait(timeout=5)
        if self._thread is not None:
            self._thread.join(timeout=5)


def _wait_for_cloud_init(
    transport: Transport, *, verbose: bool, poll_secs: float = _READINESS_POLL_SECS
) -> None:
    """Poll cloud-init to completion, emitting an elapsed heartbeat each cycle.

    Replaces a single blocking ``cloud-init status --wait`` (silent for the whole
    provisioning window) with a poll loop so the create pipeline shows steady
    progress. There is deliberately no timeout — matching ``--wait``'s
    wait-forever semantics — because the heartbeat itself is the signal the
    operator uses to decide a box is wedged and abort by hand. Raises
    ``RuntimeError`` on any cloud-init failure state so the gate still hard-fails.

    First waits for SSH reachability, so the cloud-init probe (and the verbose
    log tail, which also tunnels in) never races sshd startup and mistakes the
    boot-time IAP 4003 connect error for a provisioning failure.
    """
    _wait_for_ssh(transport)
    tail = _CloudInitTail(transport) if verbose else None
    if tail is not None:
        tail.start()
    started = time.monotonic()
    try:
        while True:
            status, detail = _poll_cloud_init_status(transport)
            if status == _CLOUD_INIT_DONE:
                return
            if status in _CLOUD_INIT_FAILED:
                raise RuntimeError(
                    f"cloud-init reported '{status}' on the cloud box — rebuild the VM"
                )
            progress.emit(_readiness_heartbeat(time.monotonic() - started, status, detail))
            time.sleep(poll_secs)
    finally:
        if tail is not None:
            tail.stop()


def await_readiness(transport: Transport, fingerprint: str, *, verbose: bool = False) -> None:
    """Synthesize a hard-fail readiness gate for a cloud box.

    Waits for cloud-init to finish — emitting an elapsed/stage heartbeat each
    poll, plus a live log tail when ``verbose`` — then confirms the stamped spec
    fingerprint matches the freshly composed one. Either failure raises
    ``RuntimeError`` so the create pipeline aborts loudly (no half-ready box).
    """
    _wait_for_cloud_init(transport, verbose=verbose)
    try:
        result = transport.run("cat", _FINGERPRINT_PATH)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"could not read the spec fingerprint marker ({_FINGERPRINT_PATH}) "
            "on the cloud box — rebuild the VM"
        ) from exc
    if result.stdout.strip() != fingerprint:
        raise RuntimeError("spec fingerprint mismatch on the cloud box — rebuild the VM")


_CLOUD_CLAUDE_LINK_DIRS = ("projects", "todos")
_CLOUD_CLAUDE_VOLUME = "/vergil/claude"


def link_cloud_claude_dirs(transport: Transport) -> None:
    """Link only the ~/.claude history subdirs onto the persistent volume.

    ``projects`` and ``todos`` are symlinked onto ``/vergil/claude`` so session
    history survives teardown, while injected credentials (``.credentials.json``,
    ``.claude.json``) stay on the ephemeral boot disk and die with the VM
    (acceptance: no injected credential on the detachable volume).
    """
    volume_dirs = " ".join(f"{_CLOUD_CLAUDE_VOLUME}/{sub}" for sub in _CLOUD_CLAUDE_LINK_DIRS)
    transport.run("bash", "-c", f"mkdir -p ~/.claude {volume_dirs}")
    for sub in _CLOUD_CLAUDE_LINK_DIRS:
        transport.run(
            "bash",
            "-c",
            f"ln -sfn {_CLOUD_CLAUDE_VOLUME}/{sub} ~/.claude/{sub}",
        )


# --- OpenTofu two-state runner -----------------------------------------------
#
# The cloud backend keeps the volume and the VM in *separate* tofu states under
# one per-identity state dir, so the disposable VM can be destroyed and rebuilt
# without ever touching the persistent volume. State, plugin cache, and the
# rendered tfvars all live under ~/.config/vergil/tofu so nothing is left in the
# (read-only, fetched-and-discarded) module tree.


def tofu_state_dir(state_key: str, provider: str) -> Path:
    """Per-(identity, provider) tofu state directory, created on demand."""
    path = Path.home() / ".config" / "vergil" / "tofu" / state_key / provider
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plugin_cache_dir() -> Path:
    """Shared OpenTofu provider plugin cache, created on demand."""
    path = Path.home() / ".config" / "vergil" / "tofu" / "plugin-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_project() -> str:
    """The GCP project for the off-platform backend: ``GOOGLE_CLOUD_PROJECT`` if set,
    else ``gcloud config get-value project``. The google OpenTofu provider does NOT read
    gcloud config, so we resolve it here and inject it into the tofu environment.
    """
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        result = subprocess.run(  # noqa: S603
            ["gcloud", "config", "get-value", "project"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        project = result.stdout.strip()
    if not project:
        print(
            "ERROR: no GCP project — set GOOGLE_CLOUD_PROJECT or run: "
            "gcloud config set project <project>",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return project


def _tofu_env() -> dict[str, str]:
    """Environment for every tofu invocation: non-interactive, shared plugin cache, and
    the GCP project. The google provider reads ``GOOGLE_CLOUD_PROJECT`` (not gcloud
    config), so without this every apply fails with ``project: required field is not set``.
    """
    return {
        **os.environ,
        "TF_IN_AUTOMATION": "1",
        "TF_PLUGIN_CACHE_DIR": str(_plugin_cache_dir()),
        "GOOGLE_CLOUD_PROJECT": _resolve_project(),
    }


def _run_tofu(module_dir: Path, state: Path, action: str, tofu_vars: dict[str, object]) -> None:
    """Run ``tofu init`` then ``tofu <action>`` against a single state file.

    The var values are written next to the state as ``<state>.tfvars.json`` so a
    later ``destroy`` can reuse them verbatim without re-deriving the inputs. When
    ``tofu_vars`` is empty (the destroy case) the existing tfvars file is reused;
    if neither vars nor a stored file exist we fail loudly rather than run an
    under-specified destroy.
    """
    var_file = Path(f"{state}.tfvars.json")
    if tofu_vars:
        var_file.write_text(json.dumps(tofu_vars), encoding="utf-8")
    elif not var_file.exists():
        msg = f"no tofu vars supplied and no stored {var_file} to reuse"
        raise RuntimeError(msg)

    progress.run(["tofu", f"-chdir={module_dir}", "init", "-input=false"], env=_tofu_env())

    args = [
        "tofu",
        f"-chdir={module_dir}",
        action,
        "-input=false",
        f"-state={state}",
        f"-var-file={var_file}",
    ]
    if action in {"apply", "destroy"}:
        args.append("-auto-approve")
    progress.run(args, env=_tofu_env())


def _tofu_output(module_dir: Path, state: Path) -> dict[str, str]:
    """Return ``tofu output -json`` for a state file flattened to ``{name: str(value)}``."""
    result = subprocess.run(  # noqa: S603
        ["tofu", f"-chdir={module_dir}", "output", "-json", f"-state={state}"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
        env=_tofu_env(),
    )
    data = json.loads(result.stdout)
    return {key: str(entry["value"]) for key, entry in data.items()}


def apply_volume(
    modules_root: Path,
    state_dir: Path,
    *,
    name: str,
    region: str,
    size_gib: int,
    labels: dict[str, str],
    zone: str = "",
) -> tuple[str, str]:
    """Apply the persistent-volume module; return ``(volume_id, zone)``.

    The resolved zone is persisted to ``<state_dir>/zone`` so the VM apply and the
    IAP transport can address the disk's zone without re-querying tofu. ``zone`` is an
    optional explicit GCP zone; empty falls back to the module's ``${region}-b`` default
    (the module coalesces it away), so existing region-b volumes are unaffected (#1797).
    """
    module_dir = modules_root / "gcp" / "volume"
    state = state_dir / "volume.tfstate"
    _run_tofu(
        module_dir,
        state,
        "apply",
        {
            "name": name,
            "region": region,
            "size_gib": size_gib,
            "labels": labels,
            "zone": zone,
        },
    )
    out = _tofu_output(module_dir, state)
    (state_dir / "zone").write_text(out["zone"], encoding="utf-8")
    return out["volume_id"], out["zone"]


def apply_vm(
    modules_root: Path,
    state_dir: Path,
    *,
    name: str,
    zone: str,
    instance_type: str,
    nested: bool,
    volume_id: str,
    ssh_user: str,
    provision_env: str,
    labels: dict[str, str],
) -> dict[str, str]:
    """Apply the VM module against the existing volume; return its outputs (host, ssh_user).

    A VM apply creates the global ``google_compute_firewall.ssh`` before the zonal
    instance. When the instance fails — most often a capacity stockout (#1797) — tofu
    persists the already-created firewall to vm.tfstate but the apply errors out, so the
    next create tries to re-create the global firewall and fails with a 409
    ``already exists`` that blocks every retry (#1804). To keep a failed create cleanly
    retryable we roll the partial apply back with a ``tofu destroy`` against the VM state
    before re-raising. The VM state holds only the firewall and instance — the persistent
    volume lives in its own state — so the rollback never touches the reusable disk.
    """
    module_dir = modules_root / "gcp" / "vm"
    state = state_dir / "vm.tfstate"
    try:
        _run_tofu(
            module_dir,
            state,
            "apply",
            {
                "name": name,
                "zone": zone,
                "instance_type": instance_type,
                "nested": nested,
                "volume_id": volume_id,
                "ssh_user": ssh_user,
                "provision_env": provision_env,
                "labels": labels,
            },
        )
    except subprocess.CalledProcessError:
        # Best-effort rollback: tear down the partial state (the orphan firewall) so the
        # retry starts clean. A rollback failure must never mask the real apply error, so
        # swallow it and re-raise the original — the operator still sees why the create
        # failed, and a stubborn orphan can be cleared with `vrg-vm destroy`.
        print("VM apply failed — rolling back the partial state...", file=sys.stderr)
        with contextlib.suppress(subprocess.CalledProcessError):
            _run_tofu(module_dir, state, "destroy", {})
        raise
    return _tofu_output(module_dir, state)


def destroy_vm(modules_root: Path, state_dir: Path) -> None:
    """Destroy the disposable VM, reusing the stored tfvars; the volume is untouched."""
    _run_tofu(modules_root / "gcp" / "vm", state_dir / "vm.tfstate", "destroy", {})


def destroy_volume(modules_root: Path, state_dir: Path) -> None:
    """Destroy the persistent volume, then remove the whole state dir for a clean rebuild."""
    _run_tofu(modules_root / "gcp" / "volume", state_dir / "volume.tfstate", "destroy", {})
    shutil.rmtree(state_dir)


def read_zone(state_dir: Path) -> str:
    """Read the zone persisted by ``apply_volume``; raise if no volume has been applied."""
    zone_file = state_dir / "zone"
    if not zone_file.is_file():
        msg = f"no persisted zone at {zone_file} — apply the volume first"
        raise RuntimeError(msg)
    return zone_file.read_text(encoding="utf-8").strip()


# A GCP zone-capacity stockout ("the zone does not have enough resources" /
# ZONE_RESOURCE_POOL_EXHAUSTED) is transient and zone-specific, so it is worth retrying in
# another zone — unlike a real config/quota error, which must abort. (#1813)
_ZONE_CAPACITY_RE = re.compile(
    r"does not have enough resources available|ZONE_RESOURCE_POOL_EXHAUSTED",
    re.IGNORECASE,
)


def is_zone_capacity_error(exc: subprocess.CalledProcessError) -> bool:
    """True when a tofu apply failed purely because the zone is out of capacity."""
    blob = f"{exc.stderr or ''}{exc.stdout or ''}"
    return bool(_ZONE_CAPACITY_RE.search(blob))


def region_zones(region: str) -> list[str]:
    """The UP zones of a GCP region, sorted (e.g. us-central1 -> -a/-b/-c/-f).

    Used to sweep zones for capacity when an instance create is stocked out.
    """
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gcloud",
            "compute",
            "zones",
            "list",
            f"--filter=name~^{region}- AND status=UP",
            "--format=value(name)",
            f"--project={_resolve_project()}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(result.stdout.split())


# --- Instance-family fallback ladder (#1836) ---------------------------------
#
# A capacity stockout is specific to a (zone, machine-family) pair: a different
# family in the same zone often has capacity when the requested one does not. On a
# reattach the zonal data disk pins the zone, so swapping the family is the only
# recovery (see apply_vm_with_zone_fallback). The ladder may contain ONLY families
# that support GCP nested virtualization — nested KVM is the point of these VMs, and
# a family without it (e2, Tau) would boot a box with no /dev/kvm and fail the
# provision. Membership/order is verified BY HAND against GCP's nested-virt
# supported-machine-types doc before merge; the unit test is only a change-detector.
NESTED_VIRT_FAMILIES = ("n2", "n2d", "c2", "c2d")

# Shapes verified to exist for EVERY family in the ladder and actually run
# off-platform. Family-fallback engages only for these, so we never synthesize an
# invalid machine type. Adding a size is one line here.
FALLBACK_SHAPES = frozenset({"standard-8", "standard-16"})


def instance_fallback_candidates(requested: str) -> list[str]:
    """Ordered machine types to try for ``requested``, the requested type first.

    Splits ``requested`` into ``(family, shape)`` (``n2-standard-8`` ->
    ``("n2", "standard-8")``). When the shape is in ``FALLBACK_SHAPES`` the result is
    the requested type, then every other ``NESTED_VIRT_FAMILIES`` member at the same
    shape, deduped. When the shape is unsupported the result is just ``[requested]``
    (no fallback). If the requested family is not in the ladder (e.g. a misconfigured
    ``e2`` declared with nested virt) the requested type still leads and the full
    ladder follows, so fallback still reaches the nested-virt-safe families.
    """
    _family, _, shape = requested.partition("-")
    if not shape or shape not in FALLBACK_SHAPES:
        return [requested]
    candidates = [requested]
    for family in NESTED_VIRT_FAMILIES:
        candidate = f"{family}-{shape}"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def apply_vm_with_zone_fallback(
    modules_root: Path,
    state_dir: Path,
    backend: OffPlatformBackend,
    *,
    zone: str,
    volume_id: str,
    fallback_zones: list[str],
) -> tuple[str, str, dict[str, str]]:
    """Apply the VM in ``zone``; on a capacity stockout, recreate the (empty) volume + VM
    in each ``fallback_zones`` entry until one lands. Returns ``(volume_id, zone, outputs)``.

    Only the fresh-volume create path supplies fallback zones — a reattach (existing volume
    with data) passes ``[]``, since a zonal disk cannot move zones without losing its data.
    ``apply_vm`` already rolls back its own partial state (the firewall) on failure; between
    fallback zones we additionally destroy the just-created *empty* volume. (#1813)
    """
    tried = [zone]
    vm_vars = cast("dict[str, Any]", backend.vm_vars(zone=zone, volume_id=volume_id))
    try:
        return volume_id, zone, apply_vm(modules_root, state_dir, **vm_vars)
    except subprocess.CalledProcessError as exc:
        if not (fallback_zones and is_zone_capacity_error(exc)):
            raise
        print(f"  zone {zone}: no capacity — trying another...", file=sys.stderr)

    for next_zone in fallback_zones:
        destroy_volume(modules_root, state_dir)  # the empty disk; rmtrees the state dir
        state_dir.mkdir(parents=True, exist_ok=True)
        volume_vars = cast("dict[str, Any]", {**backend.volume_vars(), "zone": next_zone})
        volume_id, zone = apply_volume(modules_root, state_dir, **volume_vars)
        tried.append(zone)
        vm_vars = cast("dict[str, Any]", backend.vm_vars(zone=zone, volume_id=volume_id))
        try:
            return volume_id, zone, apply_vm(modules_root, state_dir, **vm_vars)
        except subprocess.CalledProcessError as exc:
            if not is_zone_capacity_error(exc):
                raise
            print(f"  zone {zone}: no capacity — trying another...", file=sys.stderr)

    msg = (
        f"no zone in {backend.spec.region} has capacity for {backend.spec.instance} "
        f"(tried: {', '.join(tried)}). Try a different instance family (e.g. n2d-*), "
        "another region, or wait for capacity."
    )
    raise RuntimeError(msg)


# --- Volume state parsing ----------------------------------------------------
#
# Each off-platform volume's tofu state carries the one resource that matters
# for an inventory: ``google_compute_disk.data``, stamped at apply time with the
# disk's name, size, zone, and the vergil-{identity,org,repo} labels. Parsing it
# lets ``vrg-vm volumes`` build a full listing from purely local state — no
# gcloud, no network — reflecting exactly what vrg-vm manages.


@dataclass(frozen=True)
class VolumeState:
    """The ``google_compute_disk.data`` attributes parsed from a volume.tfstate."""

    name: str
    size_gib: int | None
    zone: str
    labels: dict[str, str]


def _coerce_int(value: object) -> int | None:
    """Best-effort int from a tofu attribute, ``None`` when it is absent/non-numeric."""
    if isinstance(value, bool):  # bool is an int subclass; a label-like bool is not a size
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _zone_name(zone: str) -> str:
    """Normalize a zone that may be a full selfLink URL down to its bare name."""
    return zone.rsplit("/", 1)[-1] if zone else ""


def zone_to_region(zone: str) -> str:
    """Derive a GCP region from a zone name (``us-central1-a`` -> ``us-central1``).

    Returns the empty string for a zone with no trailing ``-<suffix>`` (or none at
    all), so the caller can show a placeholder rather than a malformed region.
    """
    if not zone or "-" not in zone:
        return ""
    return zone.rsplit("-", 1)[0]


def parse_volume_state(state_file: Path) -> VolumeState | None:
    """Parse a ``volume.tfstate``, returning the persistent disk's attributes.

    Returns ``None`` when the file is absent, unreadable, malformed, or carries
    no applied ``google_compute_disk`` resource (e.g. the ``{}`` placeholder of a
    never-applied state). A bad state degrades to a placeholder volume row in the
    caller rather than erroring the whole listing — there is no network call and
    no gcloud here, only the local state file.
    """
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for resource in data.get("resources", []):
        if not isinstance(resource, dict) or resource.get("type") != "google_compute_disk":
            continue
        instances = resource.get("instances") or []
        if not instances or not isinstance(instances[0], dict):
            continue
        attrs = instances[0].get("attributes")
        if not isinstance(attrs, dict):
            continue
        raw_labels = attrs.get("labels") or {}
        labels = (
            {str(k): str(v) for k, v in raw_labels.items()} if isinstance(raw_labels, dict) else {}
        )
        return VolumeState(
            name=str(attrs.get("name", "")),
            size_gib=_coerce_int(attrs.get("size")),
            zone=_zone_name(str(attrs.get("zone", ""))),
            labels=labels,
        )
    return None


# --- Off-platform backend ----------------------------------------------------

# ASSUMPTION pending real-cloud e2e (vergil-vm gated test): the GCE image's
# default user that cloud-init's VERGIL_USER provisioning runs as. Overridable
# via VRG_OFF_PLATFORM_SSH_USER.
_DEFAULT_SSH_USER = "ubuntu"


def _effective_ssh_user() -> str:
    return os.environ.get("VRG_OFF_PLATFORM_SSH_USER", _DEFAULT_SSH_USER)


def off_platform_transport(name: str, state_dir: Path) -> IapTransport:
    """Build an IAP transport for an already-applied off-platform box from local state.

    Mirrors :meth:`OffPlatformBackend.transport` but sourced purely from the
    persisted tofu state (the resource ``name`` plus the apply ``zone`` file), so a
    fan-out enumerator can reach a running box without composing its full spec.
    Raises ``RuntimeError`` (via :func:`read_zone`) when no zone has been persisted
    — i.e. the volume was never applied and there is nothing to reach.
    """
    zone = read_zone(state_dir)
    return IapTransport(name, zone, _resolve_project(), _effective_ssh_user())


class OffPlatformBackend:
    """Cloud (OpenTofu + GCP) backend behind the ``Backend`` protocol.

    The cloud host is always addressed by its deterministic resource name, so the
    ``instance`` arg on ``transport``/``status`` exists only for protocol parity
    with ``LimaBackend`` and is ignored.
    """

    def __init__(
        self,
        spec: ComposedSpec,
        identity: str,
        org: str,
        repo: str,
        name: str | None = None,
    ) -> None:
        self.spec = spec
        self.identity = identity
        self.org = org
        self.repo = repo
        self.instance_name = name
        # Readable slug keys the state path; the cloud resource name is its hash.
        self.slug = state_slug(identity, org, repo, name)
        self.name = cloud_resource_name(self.slug)
        self.labels = cloud_labels(identity, org, repo, name)
        self.state_key = self.slug
        self.ssh_user = _effective_ssh_user()
        # Plain attribute (not a property): the Backend protocol declares
        # provider_label as a settable variable, which a read-only property fails.
        self.provider_label = spec.provider

    def _project(self) -> str:
        return _resolve_project()

    def state_dir(self) -> Path:
        return tofu_state_dir(self.state_key, self.spec.provider)

    def transport(self, instance: str | None = None) -> IapTransport:  # noqa: ARG002
        zone = read_zone(self.state_dir())
        return IapTransport(self.name, zone, self._project(), self.ssh_user)

    def status(self, instance: str | None = None) -> str:  # noqa: ARG002
        try:
            zone = read_zone(self.state_dir())
        except RuntimeError:
            return ""
        try:
            result = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "gcloud",
                    "compute",
                    "instances",
                    "describe",
                    self.name,
                    f"--zone={zone}",
                    f"--project={self._project()}",
                    "--format=value(status)",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return ""
        raw = result.stdout.strip()
        if raw == "RUNNING":
            return "Running"
        if raw in {"TERMINATED", "STOPPED"}:
            return "Stopped"
        return ""

    def volume_vars(self) -> dict[str, object]:
        return {
            "name": self.name,
            "region": self.spec.region,
            "size_gib": int(self.spec.volume.removesuffix("GiB")),
            "labels": self.labels,
            "zone": self.spec.zone,
        }

    def vm_vars(self, *, zone: str, volume_id: str) -> dict[str, object]:
        provision_env = render_provision_env(
            provision_params(
                packages=list(self.spec.packages),
                apt_repos=list(self.spec.apt_repos),
                vagrant_plugins=list(self.spec.vagrant_plugins),
                port_forwards=list(self.spec.port_forwards),
                nested=self.spec.nested,
                fingerprint=spec_fingerprint(self.spec),
            ),
            vergil_user=self.ssh_user,
            home=f"/home/{self.ssh_user}",
        )
        return {
            "name": self.name,
            "zone": zone,
            "instance_type": self.spec.instance,
            "nested": self.spec.nested,
            "volume_id": volume_id,
            "ssh_user": self.ssh_user,
            "provision_env": provision_env,
            "labels": self.labels,
        }
