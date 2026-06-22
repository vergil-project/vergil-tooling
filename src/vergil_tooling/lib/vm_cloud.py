"""Off-platform (cloud) VM backend: tofu two-state lifecycle + IAP transport."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import progress

if TYPE_CHECKING:
    from vergil_tooling.lib.identity import Identity
    from vergil_tooling.lib.vm_transport import Transport

_MAX_NAME = 59  # GCP instance name <=63; the module appends "-ssh" to the firewall name.
_HASH_LEN = 6


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or "x"


def cloud_resource_name(identity: str, org: str, repo: str) -> str:
    """A deterministic RFC1035 name (<=59 chars) for the GCP instance/disk/firewall."""
    base = "-".join(_slug(p) for p in (identity, org, repo))
    if not base[:1].isalpha():
        base = f"v-{base}"
    if len(base) <= _MAX_NAME:
        return base
    digest = hashlib.sha256(f"{identity}/{org}/{repo}".encode()).hexdigest()[:_HASH_LEN]
    keep = _MAX_NAME - _HASH_LEN - 1
    return f"{base[:keep].rstrip('-')}-{digest}"


def cloud_labels(identity: str, org: str, repo: str) -> dict[str, str]:
    """Structured labels for label-based recovery (independent of the mangled name)."""
    return {
        "vergil-identity": _slug(identity),
        "vergil-org": _slug(org),
        "vergil-repo": _slug(repo),
    }


_MODULES_URL = (
    "https://github.com/vergil-project/vergil-vm/releases/download/"
    "{tag}/opentofu-modules-{version}.tar.gz"
)
_TAG_RE = re.compile(r"^v\d+\.\d+(\.\d+)?$")


def fetch_modules(tag: str) -> Path:
    """Download the vergil-vm OpenTofu module tarball at *tag* and return its modules root.

    The release asset keeps ``v<version>`` in the download path segment but the
    filename drops the leading ``v`` (``opentofu-modules-<version>.tar.gz``). The
    tarball roots at ``opentofu/``, so the extracted modules live under
    ``opentofu/modules``.
    """
    if not _TAG_RE.fullmatch(tag):
        print(f"ERROR: invalid module tag '{tag}' (expected vN.N or vN.N.N)", file=sys.stderr)
        raise SystemExit(1)
    url = _MODULES_URL.format(tag=tag, version=tag.lstrip("v"))
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
    modules = tmp / "opentofu" / "modules"
    if not modules.is_dir():
        print(f"ERROR: module archive missing opentofu/modules ({url})", file=sys.stderr)
        raise SystemExit(1)
    return modules


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


def render_provision_env(params: dict[str, str], *, vergil_user: str, home: str) -> str:
    """Render the cloud ``provision.env`` body: the shared params plus VERGIL_USER/HOME."""
    lines = [f"{key}={value}" for key, value in params.items()]
    lines.append(f"VERGIL_USER={vergil_user}")
    lines.append(f"HOME={home}")
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
        transport.run("git", "-C", path, "fetch", "--all")


_FINGERPRINT_PATH = "/etc/vergil/vm-spec.fingerprint"


def await_readiness(transport: Transport, fingerprint: str) -> None:
    """Synthesize a hard-fail readiness gate for a cloud box.

    Waits for cloud-init to finish, then confirms the stamped spec fingerprint
    matches the freshly composed one. Either failure raises ``RuntimeError`` so
    the create pipeline aborts loudly (no half-ready box).
    """
    try:
        transport.run("cloud-init", "status", "--wait")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError("cloud-init did not complete on the cloud box — rebuild the VM") from exc
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


def _tofu_env() -> dict[str, str]:
    """Environment for every tofu invocation: non-interactive + shared plugin cache."""
    return {
        **os.environ,
        "TF_IN_AUTOMATION": "1",
        "TF_PLUGIN_CACHE_DIR": str(_plugin_cache_dir()),
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
) -> tuple[str, str]:
    """Apply the persistent-volume module; return ``(volume_id, zone)``.

    The resolved zone is persisted to ``<state_dir>/zone`` so the VM apply and the
    IAP transport can address the disk's zone without re-querying tofu.
    """
    module_dir = modules_root / "gcp" / "volume"
    state = state_dir / "volume.tfstate"
    _run_tofu(
        module_dir,
        state,
        "apply",
        {"name": name, "region": region, "size_gib": size_gib, "labels": labels},
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
    """Apply the VM module against the existing volume; return its outputs (host, ssh_user)."""
    module_dir = modules_root / "gcp" / "vm"
    state = state_dir / "vm.tfstate"
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

