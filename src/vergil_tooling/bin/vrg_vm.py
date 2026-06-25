"""Manage identity VM lifecycle."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vergil_tooling.lib.vm_backend import Backend
    from vergil_tooling.lib.vm_cloud import OffPlatformBackend
    from vergil_tooling.lib.vm_transport import Transport

from vergil_tooling.bin.vrg_vm_resolve import (
    _archived_rows,
    _last_activity,
    name_by_session,
    projects_glob,
)
from vergil_tooling.lib import progress, vm_cloud
from vergil_tooling.lib.config import ConfigError, VmStanza, read_config
from vergil_tooling.lib.identity import (
    Identity,
    IdentityConfig,
    default_config_path,
    load_config,
    resolve_identity_by_name,
    resolve_model,
    resolve_session_archive_days,
    resolve_session_stale_days,
    resolve_vergil_version,
    resolve_vm_tag,
    resolve_workspace,
)
from vergil_tooling.lib.lima import (
    create_vm,
    delete_vm,
    fetch_template,
    list_vms,
    nested_virt_unsupported_reason,
    read_instance_meta,
    shell_run,
    start_vm,
    stop_vm,
    vm_age_days,
    vm_status,
    write_instance_meta,
)
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.session import list_rows, make_name
from vergil_tooling.lib.vm_backend import select_backend
from vergil_tooling.lib.vm_guest import (
    copy_claude_config,
    get_tooling_version,
    inject_credentials,
    install_tooling,
    link_claude_dirs,
    try_update_tooling,
    update_plugins,
    update_tooling,
    vm_probe,
    vm_spec_status,
)
from vergil_tooling.lib.vm_spec import (
    ComposedSpec,
    SpecError,
    compose_vm_spec,
    instance_name,
    parse_instance_name,
    spec_fingerprint,
    split_state_slug,
    state_slug,
    validate_instance_name,
    validate_repo_segment,
)
from vergil_tooling.lib.vm_transport import LimaTransport

_default_config_path = default_config_path

_TERMINAL_ENV_VARS = "COLORTERM,TERM_PROGRAM,TERM_PROGRAM_VERSION"


def _resolve(args: argparse.Namespace) -> tuple[str, Identity, IdentityConfig]:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    name, identity = resolve_identity_by_name(config, args.identity)
    return name, identity, config


_BASE_CPUS = 4
_BASE_MEMORY = "4GiB"
_BASE_DISK = "50GiB"


@dataclass
class Target:
    identity_name: str
    identity: Identity
    config: IdentityConfig
    org: str | None
    repo: str | None
    spec: ComposedSpec
    instance: str
    fingerprint: str
    backend: Backend
    instance_name_arg: str | None = None


class BorrowError(Exception):
    """A vrg-vm command cannot proceed because of a [vm] shared_from redirect.

    Raised for an invalid borrow (self-reference, chain, or a lender that declares
    no VM) and for a MANAGE command invoked against a borrowing repo. Caught in
    main(), which prints the message and returns 1.
    """


@dataclass
class Borrow:
    """A resolved redirect: the lender repo and its [vm] stanza."""

    org: str
    repo: str
    stanza: VmStanza


def _read_repo_vm(identity: Identity, org: str, repo: str) -> VmStanza | None:
    """Return the [vm] stanza of projects_dir/<org>/<repo>, or None if no vergil.toml."""
    repo_dir = Path(resolve_workspace(f"{org}/{repo}", identity.projects_dir))
    try:
        return read_config(repo_dir).vm
    except FileNotFoundError:
        return None


def resolve_borrow(
    identity: Identity,
    req_org: str,
    req_repo: str,
    requested_vm: VmStanza | None,
) -> Borrow | None:
    """Resolve a [vm] shared_from redirect on the requested repo to its lender.

    Returns None when the requested repo does not borrow. Raises BorrowError on a
    self-reference, a borrow chain, or a lender that declares no VM.
    """
    if requested_vm is None or requested_vm.shared_from is None:
        return None
    lender_org, lender_repo = requested_vm.shared_from
    if (lender_org, lender_repo) == (req_org, req_repo):
        msg = f"{req_org}/{req_repo} cannot borrow its own VM (shared_from points at itself)"
        raise BorrowError(msg)
    lender_vm = _read_repo_vm(identity, lender_org, lender_repo)
    if lender_vm is None:
        msg = (
            f"{req_org}/{req_repo} borrows the VM of {lender_org}/{lender_repo}, "
            f"but that repo declares no [vm] stanza"
        )
        raise BorrowError(msg)
    if lender_vm.shared_from is not None:
        msg = (
            f"{req_org}/{req_repo} borrows {lender_org}/{lender_repo}, which itself "
            f"borrows another VM; shared_from chains are not allowed"
        )
        raise BorrowError(msg)
    return Borrow(lender_org, lender_repo, lender_vm)


def _borrow_block_msg(
    command: str, req_org: str, req_repo: str, lender_org: str, lender_repo: str
) -> str:
    """Message for a MANAGE command blocked because the repo borrows a VM."""
    return (
        f"{req_org}/{req_repo} borrows the VM of {lender_org}/{lender_repo}.\n"
        f"Manage that box via the lender:\n"
        f"  vrg-vm {command} {lender_org}/{lender_repo}"
    )


def _base_footprint(identity: Identity) -> dict[str, object]:
    return {
        "cpus": identity.cpus if identity.cpus is not None else _BASE_CPUS,
        "memory": identity.memory if identity.memory is not None else _BASE_MEMORY,
        "disk": identity.disk if identity.disk is not None else _BASE_DISK,
    }


def _requested_name(args: argparse.Namespace) -> str | None:
    """Return the --name value from args, or None if the flag is absent."""
    return getattr(args, "name", None)


def _workspace_org_repo(workspace: str | None) -> tuple[str | None, str | None]:
    """Derive (org, repo) for VM selection.

    Only an exact relative 'org/repo' path maps to a dedicated VM. None, '.', a bare
    repo name, a deeper path, or an absolute path all mean the base VM — this keeps the
    pre-existing 1-level / '.' session convention working while the spec's 2-level
    'org/repo' convention drives dedicated boxes.
    """
    if not workspace or workspace.startswith("/"):
        return None, None
    parts = workspace.strip("/").split("/")
    expected = 2
    if len(parts) != expected:
        return None, None
    return parts[0], parts[1]


def _resolve_target(args: argparse.Namespace, *, borrow_allowed: bool = False) -> Target:
    """Resolve (identity, optional org/repo) to a base or dedicated VM target.

    When the requested repo declares ``[vm] shared_from`` and ``borrow_allowed`` is
    True (USE commands: session, start), the instance and spec redirect to the
    lender. With ``borrow_allowed`` False (MANAGE commands: create, rebuild) a
    borrow raises ``BorrowError``. The session working directory is unaffected —
    it is always derived from ``args.workspace`` by the caller.
    """
    name, identity, config = _resolve(args)
    workspace = getattr(args, "workspace", None)
    inst_name = _requested_name(args)
    base = _base_footprint(identity)
    org, repo = _workspace_org_repo(workspace)

    if org is None or repo is None:
        if inst_name is not None:
            msg = "--name requires an <org>/<repo> workspace (named instances are per-repo)"
            raise SpecError(msg)
        spec = compose_vm_spec(identity=name, base=base, stanza=None, override=None)
        backend = select_backend(spec, identity=name, org=None, repo=None)
        return Target(name, identity, config, None, None, spec, identity.vm_instance, "", backend)

    validate_repo_segment(repo)

    requested_vm = _read_repo_vm(identity, org, repo)
    borrow = resolve_borrow(identity, org, repo, requested_vm)
    eff_vm: VmStanza | None
    if borrow is not None:
        if not borrow_allowed:
            raise BorrowError(_borrow_block_msg(args.command, org, repo, borrow.org, borrow.repo))
        eff_org, eff_repo, eff_vm = borrow.org, borrow.repo, borrow.stanza
    else:
        eff_org, eff_repo, eff_vm = org, repo, requested_vm

    override = identity.overrides.get((eff_org, eff_repo))
    spec = compose_vm_spec(
        identity=name, base=base, stanza=eff_vm, override=override, instance=inst_name
    )
    backend = select_backend(spec, identity=name, org=eff_org, repo=eff_repo, name=inst_name)

    if not spec.dedicated:
        return Target(name, identity, config, org, repo, spec, identity.vm_instance, "", backend)

    inst = instance_name(name, eff_org, eff_repo, inst_name)
    return Target(
        name,
        identity,
        config,
        eff_org,
        eff_repo,
        spec,
        inst,
        spec_fingerprint(spec),
        backend,
        inst_name,
    )


def _resolve_instance(
    args: argparse.Namespace,
) -> tuple[str, Identity, IdentityConfig, str]:
    """Resolve the Lima instance NAME for lifecycle commands (stop/restart).

    Maps an ``org/repo`` (or its absence) plus an optional ``--name`` to a Lima
    instance name. A repo with no readable ``vergil.toml`` still resolves to its own
    instance name so an orphaned dedicated VM stays reachable.
    """
    name, identity, config = _resolve(args)
    org, repo = _workspace_org_repo(getattr(args, "workspace", None))
    inst_name = _requested_name(args)
    if org is not None and repo is not None:
        validate_repo_segment(repo)
        instance = instance_name(name, org, repo, inst_name)
    else:
        instance = identity.vm_instance
    return name, identity, config, instance


def _target_ref(target: Target) -> str:
    """How a user re-addresses this target on the CLI: '<org>/<repo>' or '--identity <name>'."""
    if target.org is not None:
        return f"{target.org}/{target.repo}"
    return f"--identity {target.identity_name}"


def recover_handle(instance: str) -> tuple[str, str | None, str | None, str | None]:
    """Reverse an instance name into the four-part handle (identity, org, repo, name).

    Prefers the per-instance sidecar (reliable once a long name is truncated+hashed);
    falls back to parsing for legacy short names and base boxes that predate the
    sidecar, where ``name`` is None.
    """
    meta = read_instance_meta(instance)
    if meta is not None:
        name = str(meta.get("name") or "") or None
        return str(meta["identity"]), str(meta["org"]), str(meta["repo"]), name
    ident, org, repo = parse_instance_name(instance)
    return ident, org, repo, None


def _warn_under(target: Target) -> None:
    """Loudly warn when a host override sized a scalar below the repo's declared value."""
    if not target.spec.under:
        return
    fields = ", ".join(target.spec.under)
    print(
        f"WARNING: VM '{target.instance}' is under-provisioned for "
        f"{target.org}/{target.repo} (below declared: {fields}). "
        f"This probably will not work — the repo asked for more than this box has.",
        file=sys.stderr,
    )


def _preflight_target(target: Target) -> int:
    """Validate a dedicated target before session/start. Base targets pass through.

    Returns 0 to proceed, 1 to abort (after printing the remediation command).
    """
    if not target.spec.dedicated:
        return 0

    workspace = f"{target.org}/{target.repo}"
    status = vm_status(target.instance)
    if not status:
        print(
            f"ERROR: VM '{target.instance}' does not exist — this repo requires a "
            f"dedicated VM.\nBuild it: vrg-vm create {workspace} --identity {target.identity_name}",
            file=sys.stderr,
        )
        return 1
    # The spec fingerprint is stamped inside the guest (/etc/vergil/vm-spec.fingerprint)
    # and is only readable over `limactl shell` while the VM runs. A stopped VM cannot
    # be fingerprinted, so the drift gate can only run when already Running — checking it
    # against a stopped VM always reads needs-rebuild, which made every stopped dedicated
    # VM un-startable and falsely demanded a rebuild. For start (VM stopped) the check is
    # deferred to the post-start `spec-check` stage, once the guest is up. Mirrors list's
    # "drift only while running" contract.
    if status == "Running":
        transport = target.backend.transport(target.instance)
        spec_status = vm_spec_status(transport, target.fingerprint)
        # 'unreachable' is not drift: Lima reports the box Running but the shell
        # transport (SSH) refused, so the spec was never read. Telling the user to
        # rebuild would be wrong — the VM may be mid-boot or wedged. Surface the
        # reachability problem with a restart remediation instead.
        if spec_status == "unreachable":
            print(
                f"ERROR: VM '{target.instance}' is reported Running but is not reachable "
                f"over SSH — it may be mid-boot or wedged.\nTry: vrg-vm restart {workspace} "
                f"--identity {target.identity_name} (or inspect with `limactl list`).",
                file=sys.stderr,
            )
            return 1
        if spec_status == "needs-rebuild":
            print(
                f"ERROR: VM '{target.instance}' no longer meets {workspace}'s spec.\n"
                f"Rebuild it: vrg-vm rebuild {workspace} --identity {target.identity_name}",
                file=sys.stderr,
            )
            return 1
    _warn_under(target)
    return 0


def _nested_preflight(target: Target) -> int:
    """Abort (nonzero) when the target wants nested virt the host cannot provide.

    Runs before any build step — and before the destroy half of a rebuild —
    so an unsupported host never eats a VM it cannot recreate. Lima's own
    rejection is the backstop; the template's in-guest /dev/kvm check is the
    last line (no-silent-failures).
    """
    if not target.spec.nested:
        return 0
    reason = nested_virt_unsupported_reason()
    if reason is None:
        return 0
    print(
        f"ERROR: cannot build VM '{target.instance}': {reason}",
        file=sys.stderr,
    )
    return 1


@dataclass
class _LifecycleState:
    """Pipeline context for the long-running lifecycle commands.

    ``template`` is populated by the fetch-template stage and cleaned up by
    ``_run_lifecycle`` after the pipeline finishes, success or failure.
    """

    target: Target
    tag: str = ""
    vergil_version: str = ""
    timeout: str = "30m"
    template: Path | None = None


def _log_root() -> Path:
    """Where the .vergil run log lives: the enclosing repo, else the home dir.

    vrg-vm is a host command runnable from anywhere; when invoked outside a
    git checkout there is no project-local scratch dir to use.
    """
    result = subprocess.run(  # noqa: S603
        ("git", "rev-parse", "--show-toplevel"),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path.home()


def _st_destroy(state: _LifecycleState) -> None:
    delete_vm(state.target.instance)


def _st_fetch_template(state: _LifecycleState) -> None:
    print(f"Fetching template ({state.tag})...")
    state.template = fetch_template(state.tag)


def _st_create(state: _LifecycleState) -> None:
    if state.template is None:
        msg = "template missing — fetch-template did not run"
        raise RuntimeError(msg)
    print(f"Creating VM with projects mount: {state.target.identity.projects_dir}")
    _create_from_target(state.target, state.template)


def _st_start(state: _LifecycleState) -> None:
    start_vm(state.target.instance, timeout=state.timeout)


class SpecDriftError(RuntimeError):
    """Raised by the post-start spec-check stage when a freshly-started VM's
    stamped fingerprint no longer matches its composed spec. Carried as a warn:
    surfaced as a non-fatal ⚠ in the pipeline summary, never aborts the start.
    """


class SpecCheckUnreachableError(RuntimeError):
    """Raised by the post-start spec-check stage when a freshly-started VM cannot
    be reached over SSH to read its fingerprint. Surfaced as a non-fatal ⚠ like
    SpecDriftError, but explicitly *not* drift: the spec was never read, so the
    warning must never tell the user to rebuild.
    """


def _st_spec_check(state: _LifecycleState) -> None:
    # Post-start drift check. The fingerprint lives inside the guest and is only
    # readable now that it is up — the check _preflight_target cannot perform
    # against a stopped VM. Warn-mode: a drifted VM is already running and usable,
    # so we tell the user to rebuild rather than refusing. Base boxes carry no
    # per-repo spec and are skipped.
    target = state.target
    if not target.spec.dedicated:
        return
    transport = target.backend.transport(target.instance)
    spec_status = vm_spec_status(transport, target.fingerprint)
    if spec_status == "ok":
        return
    workspace = f"{target.org}/{target.repo}"
    # 'unreachable' is not drift: the VM was just started but the shell transport
    # could not read the fingerprint. Warn about reachability — never rebuild.
    if spec_status == "unreachable":
        msg = (
            f"VM '{target.instance}' was started but could not be reached over SSH to "
            f"verify its spec — it may still be settling. Re-run if the session fails to connect."
        )
        raise SpecCheckUnreachableError(msg)
    msg = (
        f"VM '{target.instance}' no longer meets {workspace}'s spec — "
        f"rebuild it: vrg-vm rebuild {workspace} --identity {target.identity_name}"
    )
    raise SpecDriftError(msg)


def _st_link_config(state: _LifecycleState) -> None:
    transport = state.target.backend.transport(state.target.instance)
    link_claude_dirs(transport, Path.home() / ".claude")


def _st_credentials(state: _LifecycleState) -> None:
    transport = state.target.backend.transport(state.target.instance)
    inject_credentials(transport, state.target.identity)


def _st_install_tooling(state: _LifecycleState) -> None:
    transport = state.target.backend.transport(state.target.instance)
    install_tooling(transport, state.vergil_version)


def _st_copy_config(state: _LifecycleState) -> None:
    claude_dir = Path.home() / ".claude"
    transport = state.target.backend.transport(state.target.instance)
    copy_claude_config(transport, claude_dir)
    link_claude_dirs(transport, claude_dir)


def _st_update_tooling(state: _LifecycleState) -> None:
    # Runs as a warn-mode stage: a failed update surfaces as ⚠ in the summary
    # and the start continues — the same warn-and-continue contract
    # try_update_tooling provided before the pipeline port.
    fallback = resolve_vergil_version(state.target.config, state.target.identity)
    transport = state.target.backend.transport(state.target.instance)
    update_tooling(transport, fallback_tag=fallback)


def _st_update_plugins(state: _LifecycleState) -> None:
    # Warn-mode stage: a failed plugin refresh surfaces as ⚠ and the lifecycle
    # continues, the same warn-and-continue contract as update-tooling. Plugins
    # are guest-local; this advances them to the latest published versions over
    # whichever transport the backend provides (Lima or off-platform IAP).
    transport = state.target.backend.transport(state.target.instance)
    update_plugins(transport)


def _st_cycle_ssh(state: _LifecycleState) -> None:
    # Lima establishes its multiplexed SSH ControlMaster as soon as the
    # guest's sshd answers — before cloud-init group provisioning
    # (usermod -aG) has run. sshd resolves supplementary groups once, at
    # session establishment, so every later `limactl shell` rides that stale
    # session and never sees provisioned groups (#1463). Cycle the VM as the
    # final build step so the master is re-established against the fully
    # provisioned guest; the second boot re-runs no provisioning and is fast.
    stop_vm(state.target.instance)
    start_vm(state.target.instance, timeout=state.timeout)


def _create_stages() -> list[Stage]:
    return [
        Stage("fetch-template", _st_fetch_template, mode="fail_fast"),
        Stage("create", _st_create, mode="fail_fast"),
        Stage("start", _st_start, mode="fail_fast"),
        Stage("link-config", _st_link_config, mode="fail_fast"),
        Stage("credentials", _st_credentials, mode="fail_fast"),
        Stage("tooling", _st_install_tooling, mode="fail_fast"),
        Stage("cycle-ssh", _st_cycle_ssh, mode="fail_fast"),
    ]


def _start_stages() -> list[Stage]:
    return [
        Stage("start", _st_start, mode="fail_fast"),
        # Verify the just-booted guest against its composed spec. Non-fatal and
        # placed immediately after start so a drift warning surfaces before the
        # credential/config work, without blocking a usable VM from coming up.
        Stage("spec-check", _st_spec_check, mode="warn"),
        Stage("credentials", _st_credentials, mode="fail_fast"),
        Stage("copy-config", _st_copy_config, mode="fail_fast"),
        Stage("update-tooling", _st_update_tooling, mode="warn"),
        Stage("update-plugins", _st_update_plugins, mode="warn"),
    ]


def _rebuild_stages() -> list[Stage]:
    return [
        Stage("destroy", _st_destroy, mode="fail_fast"),
        Stage("fetch-template", _st_fetch_template, mode="fail_fast"),
        Stage("create", _st_create, mode="fail_fast"),
        Stage("start", _st_start, mode="fail_fast"),
        Stage("credentials", _st_credentials, mode="fail_fast"),
        Stage("tooling", _st_install_tooling, mode="fail_fast"),
        Stage("copy-config", _st_copy_config, mode="fail_fast"),
        Stage("update-plugins", _st_update_plugins, mode="warn"),
        Stage("cycle-ssh", _st_cycle_ssh, mode="fail_fast"),
    ]


def _run_lifecycle(
    verb: str, state: _LifecycleState, stages: list[Stage], args: argparse.Namespace
) -> int:
    """Run a lifecycle pipeline, always cleaning up the fetched template."""
    try:
        return progress.run_pipeline(
            state,
            stages,
            command="vrg-vm",
            label=f"vrg-vm {verb} '{state.target.instance}'",
            args=args,
            repo_root=_log_root(),
        )
    finally:
        if state.template is not None:
            state.template.unlink(missing_ok=True)


def _create_from_target(target: Target, template: Path) -> None:
    """Build the VM for a target: dedicated boxes carry the composed spec, base is unchanged."""
    if target.spec.dedicated:
        create_vm(
            target.instance,
            template,
            target.identity.projects_dir,
            cpus=target.spec.cpus,
            memory=target.spec.memory,
            disk=target.spec.disk,
            packages=list(target.spec.packages),
            apt_repos=list(target.spec.apt_repos),
            vagrant_plugins=list(target.spec.vagrant_plugins),
            port_forwards=list(target.spec.port_forwards),
            fingerprint=target.fingerprint,
            nested=target.spec.nested,
        )
        # Persist (identity, org, repo) so recover_handle can reverse the name
        # even after it has been truncated+hashed to fit UNIX_PATH_MAX.
        # org/repo are guaranteed non-None for dedicated targets.
        assert target.org is not None and target.repo is not None  # noqa: S101
        write_instance_meta(
            target.instance,
            target.identity_name,
            target.org,
            target.repo,
            target.instance_name_arg,
        )
    else:
        create_vm(
            target.instance,
            template,
            target.identity.projects_dir,
            cpus=target.identity.cpus,
            memory=target.identity.memory,
            disk=target.identity.disk,
        )


# --- Off-platform (cloud) lifecycle ------------------------------------------
#
# The cloud path mirrors the Lima Stage/run_pipeline framework with its own
# small state object. Where the Lima lifecycle threads a fetched template, the
# cloud lifecycle threads a fetched OpenTofu modules root (cleaned up in a
# finally) plus the volume/VM coordinates that flow stage-to-stage: the resolved
# zone and volume_id from the volume apply, the host from the VM apply, and the
# IAP transport built once the box exists.

# Known nested-virt instance types → (vcpus, mem_gib), for the under-provision
# warning. Unknown types are silent (no false warning) — the table is a small,
# best-effort allowlist, not an exhaustive provider catalogue.
_CLOUD_INSTANCE_SIZES: dict[str, tuple[int, int]] = {
    "n2-standard-16": (16, 64),
    "n2-standard-8": (8, 32),
    "c3-standard-22": (22, 88),
}


def _warn_cloud_under(target: Target) -> None:
    """Loudly warn when a known cloud instance type is smaller than the declared spec.

    Mirrors ``_warn_under``: the box will probably not satisfy the repo's footprint.
    Unknown instance types are silent — we never guess a size we don't know.
    """
    size = _CLOUD_INSTANCE_SIZES.get(target.spec.instance)
    if size is None:
        return
    vcpus, mem_gib = size
    declared_mem = int(str(target.spec.memory).removesuffix("GiB"))
    if vcpus >= target.spec.cpus and mem_gib >= declared_mem:
        return
    print(
        f"WARNING: cloud instance type '{target.spec.instance}' "
        f"({vcpus} vCPU, {mem_gib}GiB) is under-provisioned for "
        f"{target.org}/{target.repo} (declared: {target.spec.cpus} CPU, "
        f"{target.spec.memory}). This probably will not work — the repo asked "
        f"for more than this instance type provides.",
        file=sys.stderr,
    )


@dataclass
class _CloudState:
    """Pipeline context for the cloud (off-platform) lifecycle commands.

    ``modules_root`` is populated by the fetch-modules stage and cleaned up by
    ``_run_cloud_lifecycle`` after the pipeline finishes, success or failure.
    """

    target: Target
    backend: OffPlatformBackend
    state_dir: Path
    tag: str = ""
    vergil_version: str = ""
    verbose: bool = False
    modules_root: Path | None = None
    zone: str = ""
    volume_id: str = ""
    host: str = ""
    transport: Transport | None = None
    # Remaining zones to try if the VM apply hits a capacity stockout (#1813). Populated
    # by tofu-volume on a fresh create; empty on a reattach (a zonal disk cannot move).
    fallback_zones: list[str] = field(default_factory=list)
    # Machine families to try (same pinned zone) if a reattach VM apply hits a capacity
    # stockout — the ladder minus the requested type. Populated only on a reattach; empty
    # on a fresh create, which sweeps fallback_zones instead. (#1836)
    fallback_instances: list[str] = field(default_factory=list)


def _require_modules(state: _CloudState) -> Path:
    """Return the fetched modules root, failing loudly if fetch-modules never ran."""
    if state.modules_root is None:
        msg = "modules missing — fetch-modules did not run"
        raise RuntimeError(msg)
    return state.modules_root


def _require_transport(state: _CloudState) -> Transport:
    """Return the live transport, failing loudly if tofu-vm never built it."""
    if state.transport is None:
        msg = "transport missing — tofu-vm did not run"
        raise RuntimeError(msg)
    return state.transport


def _cs_fetch_modules(state: _CloudState) -> None:
    print(f"Fetching OpenTofu modules ({state.tag})...")
    state.modules_root = vm_cloud.fetch_modules(state.tag)


def _candidate_zones(backend: OffPlatformBackend) -> list[str]:
    """Zone order to try for a fresh create: an explicit ``zone`` first (operator
    preference), otherwise the region's zones shuffled to spread load. (#1813)
    """
    zones = vm_cloud.region_zones(backend.spec.region)
    configured = backend.spec.zone
    if configured:
        return [configured, *(z for z in zones if z != configured)]
    shuffled = list(zones)
    random.shuffle(shuffled)
    return shuffled


def _cs_tofu_volume(state: _CloudState) -> None:
    modules_root = _require_modules(state)
    print("Applying persistent volume...")
    # The backend returns the var map as dict[str, object]; the engine's apply_*
    # signatures are precisely typed. The dict is the backend's own contract, so the
    # spread is the right call shape — cast to Any to bridge the object→typed kwargs gap.
    volume_vars = cast("dict[str, Any]", state.backend.volume_vars())
    # Fresh create -> sweep zones on a capacity stockout. A reattach (existing volume
    # state) is pinned to its zone — a zonal disk holds data and cannot move (#1813).
    if not (state.state_dir / "volume.tfstate").exists():
        candidates = _candidate_zones(state.backend)
        if candidates:
            volume_vars["zone"] = candidates[0]
            state.fallback_zones = candidates[1:]
    else:
        # Reattach: the zonal disk pins the zone, so recovery is a machine-family
        # sweep in that zone rather than a zone sweep. (#1836)
        state.fallback_instances = vm_cloud.instance_fallback_candidates(
            state.backend.spec.instance
        )[1:]
    volume_id, zone = vm_cloud.apply_volume(
        modules_root, state.state_dir, **volume_vars, provider=state.backend.provider_label
    )
    state.volume_id = volume_id
    state.zone = zone


def _cs_tofu_vm(state: _CloudState) -> None:
    modules_root = _require_modules(state)
    print("Applying VM...")
    volume_id, zone, out = vm_cloud.apply_vm_with_zone_fallback(
        modules_root,
        state.state_dir,
        state.backend,
        zone=state.zone,
        volume_id=state.volume_id,
        fallback_zones=state.fallback_zones,
        fallback_instances=state.fallback_instances,
    )
    state.volume_id = volume_id
    state.zone = zone
    state.host = out["host"]
    state.transport = state.backend.transport()


def _cs_await_readiness(state: _CloudState) -> None:
    print("Waiting for the cloud box to be ready...")
    vm_cloud.await_readiness(
        _require_transport(state),
        spec_fingerprint(state.target.spec),
        verbose=state.verbose,
    )


def _cs_credentials(state: _CloudState) -> None:
    inject_credentials(_require_transport(state), state.target.identity)


def _cs_tooling(state: _CloudState) -> None:
    install_tooling(_require_transport(state), state.vergil_version)


def _cs_bootstrap_volume(state: _CloudState) -> None:
    transport = _require_transport(state)
    assert state.target.org is not None  # noqa: S101 — dedicated cloud target always carries org/repo
    assert state.target.repo is not None  # noqa: S101
    vm_cloud.bootstrap_volume(transport, state.target.identity, state.target.org, state.target.repo)


def _cs_link_claude(state: _CloudState) -> None:
    # Parity with the Lima path's _st_copy_config (#1825): seed the host's
    # ~/.claude config (CLAUDE.md + settings.json) onto the cloud VM, THEN link
    # the durable history subdirs onto the persistent volume. settings.json
    # carries the operator's permissions.defaultMode; without this copy an
    # off-platform VM never receives it, so bypassPermissions is unreachable
    # (it is a launch-time/default mode, not a Shift+Tab cycle). The VM sandbox
    # is the security boundary that makes bypass-in-guest the standard.
    transport = _require_transport(state)
    copy_claude_config(transport, Path.home() / ".claude")
    vm_cloud.link_cloud_claude_dirs(transport)


def _cloud_create_stages() -> list[Stage]:
    return [
        Stage("fetch-modules", _cs_fetch_modules, mode="fail_fast"),
        Stage("tofu-volume", _cs_tofu_volume, mode="fail_fast"),
        Stage("tofu-vm", _cs_tofu_vm, mode="fail_fast"),
        Stage("await-readiness", _cs_await_readiness, mode="fail_fast"),
        Stage("credentials", _cs_credentials, mode="fail_fast"),
        Stage("tooling", _cs_tooling, mode="fail_fast"),
        Stage("bootstrap-volume", _cs_bootstrap_volume, mode="fail_fast"),
        Stage("link-claude", _cs_link_claude, mode="fail_fast"),
    ]


def _run_cloud_lifecycle(
    verb: str, state: _CloudState, stages: list[Stage], args: argparse.Namespace
) -> int:
    """Run a cloud lifecycle pipeline, always cleaning up the fetched modules dir."""
    try:
        return progress.run_pipeline(
            state,
            stages,
            command="vrg-vm",
            label=f"vrg-vm {verb} '{state.backend.name}'",
            args=args,
            repo_root=_log_root(),
        )
    finally:
        if state.modules_root is not None:
            shutil.rmtree(state.modules_root.parent, ignore_errors=True)


def _cloud_backend(target: Target) -> OffPlatformBackend:
    """Narrow ``target.backend`` to the cloud backend for an off-platform target."""
    return cast("OffPlatformBackend", target.backend)


_VERBOSE_ENV_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _resolve_vm_verbose(args: argparse.Namespace) -> bool:
    """Opt into live provisioning output via ``--verbose`` or ``VERGIL_VM_VERBOSE``."""
    if getattr(args, "verbose", False):
        return True
    return os.environ.get("VERGIL_VM_VERBOSE", "").strip().lower() in _VERBOSE_ENV_TRUTHY


def _cloud_create(
    verb: str, target: Target, args: argparse.Namespace, *, destroy_first: bool
) -> int:
    """Run the cloud build pipeline for create/rebuild.

    With ``destroy_first`` the disposable VM is torn down before the (idempotent)
    volume + VM apply rebuilds it; the persistent volume is reattached, not recreated.
    """
    name, identity, config = target.identity_name, target.identity, target.config
    backend = _cloud_backend(target)
    vm_cloud.preflight()

    # Concurrency guard (create only): refuse to clobber a live box. Rebuild
    # destroys the disposable VM first, so a Running box is expected there.
    if not destroy_first and backend.status() == "Running":
        print(
            f"ERROR: VM '{backend.name}' already exists (status: Running)",
            file=sys.stderr,
        )
        return 1

    vergil_version = resolve_vergil_version(config, identity)
    tag = args.tag if getattr(args, "tag", "") else resolve_vm_tag(config, identity)

    if destroy_first:
        modules_root = vm_cloud.fetch_modules(tag)
        try:
            print(f"Destroying disposable VM '{backend.name}' before rebuild...")
            vm_cloud.destroy_vm(modules_root, backend.state_dir(), provider=backend.provider_label)
        finally:
            shutil.rmtree(modules_root.parent, ignore_errors=True)

    print(f"Building cloud VM '{backend.name}' for identity '{name}'...")
    state = _CloudState(
        target=target,
        backend=backend,
        state_dir=backend.state_dir(),
        tag=tag,
        vergil_version=vergil_version,
        verbose=_resolve_vm_verbose(args),
    )
    return _run_cloud_lifecycle(verb, state, _cloud_create_stages(), args)


def _cmd_create(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    name, identity, config = target.identity_name, target.identity, target.config

    if target.spec.off_platform:
        return _cloud_create("create", target, args, destroy_first=False)

    vergil_version = resolve_vergil_version(config, identity)
    tag = args.tag if args.tag else resolve_vm_tag(config, identity)

    status = vm_status(target.instance)
    if status:
        print(
            f"ERROR: VM '{target.instance}' already exists (status: {status})",
            file=sys.stderr,
        )
        return 1

    if not identity.projects_dir:
        print(
            f"ERROR: identity '{name}' has no projects_dir configured",
            file=sys.stderr,
        )
        return 1

    if _nested_preflight(target) != 0:
        return 1

    print(f"Creating VM '{target.instance}' for identity '{name}'...")
    state = _LifecycleState(target=target, tag=tag, vergil_version=vergil_version)
    return _run_lifecycle("create", state, _create_stages(), args)


def _cmd_start(args: argparse.Namespace) -> int:
    target = _resolve_target(args, borrow_allowed=True)
    name = target.identity_name

    if target.spec.off_platform:
        print(_EPHEMERAL_MSG, file=sys.stderr)
        return 1

    if _preflight_target(target) != 0:
        return 1

    status = vm_status(target.instance)
    if not status:
        print(
            f"ERROR: VM '{target.instance}' does not exist — run 'vrg-vm create' first",
            file=sys.stderr,
        )
        return 1

    allow_stale = getattr(args, "allow_stale_vm", False)
    if not allow_stale:
        age = vm_age_days(target.instance)
        if age is not None and age > target.spec.stale_days:
            ref = _target_ref(target)
            print(
                f"ERROR: VM '{target.instance}' is {age:.0f} days old"
                f" (threshold: {target.spec.stale_days} days).\n"
                f"Rebuild with: vrg-vm rebuild {ref}\n"
                f"Override with: vrg-vm start --allow-stale-vm {ref}",
                file=sys.stderr,
            )
            return 1

    print(f"Starting VM '{target.instance}' (identity: {name})...")
    state = _LifecycleState(target=target, timeout=args.timeout)
    return _run_lifecycle("start", state, _start_stages(), args)


_EPHEMERAL_MSG = (
    "ERROR: off-platform VMs are ephemeral — use 'vrg-vm destroy'/'vrg-vm create' "
    "(stop/start/restart not supported)"
)


def _reject_if_off_platform(args: argparse.Namespace) -> bool:
    """Print the ephemeral-VM error and return True when the target is off-platform.

    Resolves the full target (not just the instance name) so the spec's backend is
    known — ``_resolve_instance`` alone cannot tell Lima from cloud.
    """
    if _resolve_target(args).spec.off_platform:
        print(_EPHEMERAL_MSG, file=sys.stderr)
        return True
    return False


def _cmd_stop(args: argparse.Namespace) -> int:
    if _reject_if_off_platform(args):
        return 1
    name, _identity, _config, instance = _resolve_instance(args)

    print(f"Stopping VM '{instance}' (identity: {name})...")
    stop_vm(instance)

    print(f"VM '{instance}' stopped.")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    if _reject_if_off_platform(args):
        return 1
    name, identity, _config, instance = _resolve_instance(args)

    print(f"Restarting VM '{instance}' (identity: {name})...")
    stop_vm(instance)
    start_vm(instance)

    print("Injecting credentials...")
    inject_credentials(LimaTransport(instance), identity)

    print(f"VM '{instance}' is running.")
    return 0


def _update_over_transport(
    transport: Transport, label: str, tag: str | None, fallback: str
) -> None:
    """Update tooling and plugins in one running box, printing the version transition.

    Transport-generic core shared by the Lima and off-platform update paths
    (#1812): an off-platform box updates in place over its IAP transport exactly
    as a Lima box does over limactl — no rebuild.
    """
    print(f"Updating vergil-tooling in {label}...")

    before = get_tooling_version(transport)
    update_tooling(transport, tag, fallback_tag=fallback)
    after = get_tooling_version(transport)

    if before and after:
        if before == after:
            print(f"  vergil-tooling: {after} (already up to date)")
        else:
            print(f"  vergil-tooling: {before} → {after}")
    elif after:
        print(f"  vergil-tooling: {after}")

    update_plugins(transport)


def _update_instance(instance: str, name: str, tag: str | None, fallback: str) -> None:
    """Update tooling and plugins in one running Lima VM over its limactl transport."""
    _update_over_transport(
        LimaTransport(instance), f"VM '{instance}' (identity: {name})", tag, fallback
    )


def _cmd_update_off_platform(target: Target, args: argparse.Namespace) -> int:
    """Update tooling and plugins in a running off-platform box in place over IAP.

    In-place is correct for a *running* off-platform box (#1812): the tooling
    update is transport-generic, so it runs over the box's IAP transport exactly
    as Lima runs over limactl — seconds, non-disruptive, no capacity/quota risk.
    Rebuild (``vrg-vm rebuild``) stays reserved for what genuinely needs a fresh
    image (a new base image or changed provision scripts), not a tooling bump.
    """
    backend = _cloud_backend(target)
    status = backend.status()
    if status != "Running":
        effective = status or "Not Created"
        # Identity-qualified hint: two off-platform boxes can share an org/repo
        # (one per identity), so the start command must carry --identity (#1812).
        start_ref = f"{target.org}/{target.repo} --identity {target.identity_name}"
        print(
            f"ERROR: off-platform VM '{backend.name}' is not running (status: {effective}).\n"
            f"Start it first: vrg-vm start {start_ref}",
            file=sys.stderr,
        )
        return 1

    tag = args.tag if args.tag else None
    fallback = resolve_vergil_version(target.config, target.identity)
    label = f"off-platform box '{backend.name}' (identity: {target.identity_name})"
    _update_over_transport(backend.transport(), label, tag, fallback)

    print("Update complete.")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    if args.all:
        return _cmd_update_all(args)

    # Off-platform boxes update IN PLACE over IAP — same transport-generic tooling
    # update as Lima, no rebuild (#1812, correcting #1803's stateless premise).
    target = _resolve_target(args)
    if target.spec.off_platform:
        return _cmd_update_off_platform(target, args)

    name, identity, config, instance = _resolve_instance(args)

    status = vm_status(instance)
    if status != "Running":
        effective = status or "Not Created"
        print(
            f"ERROR: VM '{instance}' is not running (status: {effective})",
            file=sys.stderr,
        )
        return 1

    tag = args.tag if args.tag else None
    fallback = resolve_vergil_version(config, identity)
    _update_instance(instance, name, tag, fallback)

    print("Update complete.")
    return 0


def _all_update_targets(
    config: IdentityConfig, status: dict[str, str]
) -> list[tuple[str, Identity, str]]:
    """Enumerate (identity_name, identity, instance) for every existing owned VM.

    Base VMs are matched by exact ``vm_instance``; dedicated VMs by the identity
    tier of the instance name (orphaned ones included — they still run agents
    that need current tooling). Lima instances owned by no configured identity
    are ignored.
    """
    targets: list[tuple[str, Identity, str]] = []
    for id_name, identity in config.identities.items():
        if identity.vm_instance in status:
            targets.append((id_name, identity, identity.vm_instance))
        for inst in sorted(status):
            try:
                ident, org, repo, _inst_name = recover_handle(inst)
            except ValueError:
                continue
            if ident == id_name and org is not None and repo is not None:
                targets.append((id_name, identity, inst))
    return targets


def _off_platform_fallback(config: IdentityConfig, vm: OffPlatformVm) -> str:
    """Resolve the fallback vergil tag for an off-platform box from its labeled identity.

    The box's own tooling-tag marker is authoritative — ``update_tooling`` reads it
    and this fallback applies only to a box with no marker. Resolve it from the
    box's labeled identity when that identity is still configured, otherwise the
    config-level default; raise (deferred by the caller) when neither is set.
    """
    identity = config.identities.get(vm.identity) if vm.identity else None
    if identity is not None:
        return resolve_vergil_version(config, identity)
    if config.vergil:
        return config.vergil
    print("ERROR: no 'vergil' version configured in identities.toml", file=sys.stderr)
    raise SystemExit(1)


def _cmd_update_all(args: argparse.Namespace) -> int:
    """Update vergil-tooling in every existing VM owned by a configured identity.

    Fail-deferred by design: every box in the list is attempted even after one
    fails; non-running boxes are skipped and reported; any failure makes the
    final exit code non-zero — only after all attempts have completed. Off-platform
    boxes are enumerated across backends and updated IN PLACE over IAP, exactly
    like Lima boxes (#1812, correcting #1803 which skipped-and-reported them on the
    false premise that off-platform update means a rebuild). Two off-platform boxes
    that share an org/repo (one per identity) stay distinct via their identity label.
    """
    if args.workspace or args.identity:
        print(
            "ERROR: --all cannot be combined with a workspace or --identity",
            file=sys.stderr,
        )
        return 2

    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    status = {vm["name"]: vm["status"] for vm in list_vms()}

    targets = _all_update_targets(config, status)
    off_platform = _off_platform_vms()
    if not targets and not off_platform:
        print("No VMs found.")
        return 0

    tag = args.tag if args.tag else None
    updated: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    for id_name, identity, instance in targets:
        vm_state = status[instance]
        if vm_state != "Running":
            print(f"Skipping VM '{instance}' (status: {vm_state or 'Not Created'})")
            skipped.append(instance)
            continue
        try:
            fallback = resolve_vergil_version(config, identity)
            _update_instance(instance, id_name, tag, fallback)
            updated.append(instance)
        except subprocess.CalledProcessError as exc:
            reason = f"exit status {exc.returncode}"
            print(f"ERROR: failed to update VM '{instance}': {reason}", file=sys.stderr)
            failed.append((instance, reason))
        except SystemExit as exc:
            # update_tooling/resolve_vergil_version abort via SystemExit after
            # printing their own ERROR line; defer the failure like any other.
            reason = f"aborted (exit {exc.code})"
            print(f"ERROR: failed to update VM '{instance}': {reason}", file=sys.stderr)
            failed.append((instance, reason))

    for vm in off_platform:
        box = f"off-platform box '{vm.label}'"
        if not vm.is_running:
            print(f"Skipping {box} (status: {vm.status or 'Not Created'})")
            skipped.append(vm.label)
            continue
        try:
            fallback = _off_platform_fallback(config, vm)
            transport = vm_cloud.off_platform_transport(vm.cloud_name, vm.state_dir)
            _update_over_transport(transport, box, tag, fallback)
            updated.append(vm.label)
        except subprocess.CalledProcessError as exc:
            reason = f"exit status {exc.returncode}"
            print(f"ERROR: failed to update {box}: {reason}", file=sys.stderr)
            failed.append((vm.label, reason))
        except SystemExit as exc:
            reason = f"aborted (exit {exc.code})"
            print(f"ERROR: failed to update {box}: {reason}", file=sys.stderr)
            failed.append((vm.label, reason))
        except RuntimeError as exc:
            # off_platform_transport raises RuntimeError when no zone is persisted
            # (volume never applied), and update_plugins raises it on a plugin
            # failure. Both are deferred like any other box failure.
            reason = str(exc)
            print(f"ERROR: failed to update {box}: {reason}", file=sys.stderr)
            failed.append((vm.label, reason))

    # total is always > 0 here: the empty case returned "No VMs found." above.
    total = len(targets) + len(off_platform)
    print(
        f"Update complete: {len(updated)} updated, {len(skipped)} skipped, "
        f"{len(failed)} failed (of {total} VMs)."
    )

    if failed:
        names = ", ".join(f"'{inst}' ({reason})" for inst, reason in failed)
        print(f"failed to update {len(failed)} of {total}: {names}", file=sys.stderr)
        return 1
    return 0


def _destroy_recorded(
    rs: RecordedState,
    args: argparse.Namespace,
    config: IdentityConfig,
    identity: Identity,
) -> int:
    """Tear down the Lima box and every recorded provider state for a handle."""
    if rs.lima_instance is not None:
        print(f"Destroying Lima VM '{rs.lima_instance}'...")
        delete_vm(rs.lima_instance)
    if rs.tofu_dirs:
        tag = args.tag if getattr(args, "tag", "") else resolve_vm_tag(config, identity)
        modules_root = vm_cloud.fetch_modules(tag)
        try:
            for provider, state_dir in rs.tofu_dirs:
                print(f"Destroying cloud VM under {provider} (volume preserved): {state_dir}")
                vm_cloud.destroy_vm(modules_root, state_dir, provider=provider)
        finally:
            shutil.rmtree(modules_root.parent, ignore_errors=True)
    print("Destroyed (persistent volumes preserved — use destroy-volume to remove them).")
    return 0


def _cmd_destroy(args: argparse.Namespace) -> int:
    name, identity, config = _resolve(args)
    org, repo = _workspace_org_repo(getattr(args, "workspace", None))
    inst_name = getattr(args, "name", None)
    if org is None or repo is None:
        rs = RecordedState(
            lima_instance=(
                identity.vm_instance
                if identity.vm_instance in {vm["name"] for vm in list_vms()}
                else None
            ),
            tofu_dirs=[],
        )
    else:
        validate_repo_segment(repo)
        if inst_name is not None:
            try:
                validate_instance_name(inst_name)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
        requested_vm = _read_repo_vm(identity, org, repo)
        borrow = resolve_borrow(identity, org, repo, requested_vm)
        if borrow is not None:
            raise BorrowError(_borrow_block_msg(args.command, org, repo, borrow.org, borrow.repo))
        rs = _recorded_state_for_handle(name, org, repo, inst_name)

    if rs.lima_instance is None and not rs.tofu_dirs:
        print("No recorded state to destroy for this handle.", file=sys.stderr)
        return 1

    # Confirmation contract.
    box_count = (1 if rs.lima_instance else 0) + len(rs.tofu_dirs)
    box_word = "box" if box_count == 1 else "boxes"
    print(f"Will destroy the following recorded {box_word}:")
    if rs.lima_instance:
        print(f"  - Lima: {rs.lima_instance}")
    for provider, state_dir in rs.tofu_dirs:
        print(f"  - {provider}: {state_dir}")
    if not getattr(args, "yes", False):
        if not sys.stdin.isatty():
            print(
                f"Refusing to destroy {box_count} recorded {box_word} non-interactively"
                " — re-run with --yes to confirm.",
                file=sys.stderr,
            )
            return 1
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted.", file=sys.stderr)
            return 1

    return _destroy_recorded(rs, args, config, identity)


def _cmd_destroy_volume(args: argparse.Namespace) -> int:
    """Destroy an off-platform VM's PERSISTENT volume (irreversible).

    Requires an off-platform target and an explicit confirmation: the user must
    retype ``org/repo`` (or pass ``--yes``) before the volume — and its local tofu
    state dir — are torn down.
    """
    target = _resolve_target(args)
    if not target.spec.off_platform:
        print("ERROR: destroy-volume is only for off-platform VMs", file=sys.stderr)
        return 1

    backend = _cloud_backend(target)
    ref = f"{target.org}/{target.repo}"
    if not args.yes:
        answer = input(
            f"Type the repo '{ref}' to confirm destroying the PERSISTENT volume: "
        ).strip()
        if answer != ref:
            print("Aborted — confirmation did not match.", file=sys.stderr)
            return 1

    tag = args.tag if args.tag else resolve_vm_tag(target.config, target.identity)
    modules_root = vm_cloud.fetch_modules(tag)
    try:
        print(f"Destroying the persistent volume for {ref} (state: {backend.state_dir()})...")
        destroyed = vm_cloud.destroy_volume(
            modules_root, backend.state_dir(), provider=backend.provider_label
        )
    finally:
        shutil.rmtree(modules_root.parent, ignore_errors=True)
    if destroyed:
        print(f"Persistent volume for {ref} destroyed (local tofu state removed).")
    else:
        # The state held no disk — tofu destroyed nothing. Say so loudly rather than
        # report a phantom success: a cloud disk created under a different/legacy
        # state dir may still exist (and keep pinning quota). (#1846)
        print(
            f"WARNING: no disk was present in the tofu state for {ref} — nothing was "
            f"destroyed. The local state has been cleared, but a cloud disk under a "
            f"different/legacy state may still exist; check `vrg-vm volumes` and your "
            f"provider console.",
            file=sys.stderr,
        )
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    name, identity, config = target.identity_name, target.identity, target.config

    if target.spec.off_platform:
        # The volume apply is idempotent, so the rebuild destroys only the
        # disposable VM and re-applies; the persistent volume is reattached.
        return _cloud_create("rebuild", target, args, destroy_first=True)

    status = vm_status(target.instance)
    if not status:
        # Rebuild is idempotent: with nothing to rebuild, create the VM rather
        # than abort. An absent VM has nothing to destroy, so the create
        # lifecycle — not a destroy-skipped rebuild — is the right pipeline.
        # Delegate to _cmd_create, which re-confirms absence and runs the
        # create stages (and accurately labels the run as a create).
        print(f"VM '{target.instance}' does not exist yet — creating it (nothing to rebuild).")
        return _cmd_create(args)

    if not identity.projects_dir:
        print(
            f"ERROR: identity '{name}' has no projects_dir configured",
            file=sys.stderr,
        )
        return 1

    if _nested_preflight(target) != 0:
        return 1

    vergil_version = resolve_vergil_version(config, identity)
    tag = args.tag if args.tag else resolve_vm_tag(config, identity)

    print(f"Rebuilding VM '{target.instance}' (identity: {name})...")
    state = _LifecycleState(
        target=target, tag=tag, vergil_version=vergil_version, timeout=args.timeout
    )
    return _run_lifecycle("rebuild", state, _rebuild_stages(), args)


@dataclass
class DedicatedRow:
    org: str
    repo: str
    instance: str
    state: str  # "present" | "orphaned"
    stanza: VmStanza | None = None
    instance_name: str | None = None


def _classify_instance(
    projects_dir: str, org: str, repo: str, instance: str
) -> tuple[str, VmStanza | None]:
    """Classify one existing instance against its repo's [vm] stanza.

    Returns ``(state, stanza)``: ``("present", stanza)`` when the repo declares
    a spec, ``("orphaned", None)`` when it provably lacks one. A repo whose
    ``vergil.toml`` fails to parse is classified ``present`` conservatively —
    flagging it orphaned would invite destroying a VM whose spec may still be
    declared — and the failure is warned loudly with the config path.
    """
    repo_dir = Path(projects_dir) / org / repo
    config_path = repo_dir / "vergil.toml"
    if not config_path.exists():
        return "orphaned", None
    try:
        stanza = read_config(repo_dir).vm
    except ConfigError as exc:
        print(
            f"WARNING: cannot parse {config_path}: {exc} — "
            f"listing '{instance}' as present (unverified)",
            file=sys.stderr,
        )
        return "present", None
    if stanza is None:
        return "orphaned", None
    return "present", stanza


def discover_dedicated(
    identity_name: str, instances: list[str], projects_dir: str
) -> list[DedicatedRow]:
    """Classify existing <identity>.<org>.<repo> instances against local repos.

    Enumeration is from instances only — O(instances), one targeted
    ``read_config()`` each:

    - instance + spec   -> present (carrying the parsed stanza)
    - instance, no spec -> orphaned

    A spec-bearing repo with no instance is not listed here; the session/start
    preflight gate surfaces it loudly when the repo is actually used.
    """
    rows: list[DedicatedRow] = []
    for name in instances:
        try:
            ident, org, repo, inst_name = recover_handle(name)
        except ValueError:
            continue
        if ident != identity_name or org is None or repo is None:
            continue
        state, stanza = _classify_instance(projects_dir, org, repo, name)
        rows.append(DedicatedRow(org, repo, name, state, stanza, inst_name))
    return rows


# (agents, humans, fingerprint) from one combined vm_probe round-trip.
ProbeResult = tuple[int, int, str | None]


def _probe_running(
    identities: dict[str, Identity],
    discovered: dict[str, list[DedicatedRow]],
    status: dict[str, str],
) -> dict[str, ProbeResult]:
    """Probe every running VM in parallel, one shell round-trip each.

    The fingerprint is requested only where a present dedicated row will
    compare it; base and orphaned VMs get the occupancy-only probe. The
    probes are subprocess-bound (one SSH session each), so a thread pool
    makes wall-clock ≈ one round-trip regardless of running-VM count.
    Failures beyond vm_probe's documented (0, 0, None) contract propagate
    out of Future.result() rather than being swallowed.
    """
    wants: dict[str, bool] = {}
    for id_name, identity in identities.items():
        if status.get(identity.vm_instance) == "Running":
            wants[identity.vm_instance] = False
        for d in discovered[id_name]:
            if status.get(d.instance) == "Running":
                wants[d.instance] = d.state == "present"
    if not wants:
        return {}
    with ThreadPoolExecutor(max_workers=len(wants)) as pool:
        futures = {
            instance: pool.submit(vm_probe, LimaTransport(instance), fingerprint=want_fp)
            for instance, want_fp in wants.items()
        }
        return {instance: future.result() for instance, future in futures.items()}


def _occupancy(instance: str, probes: Mapping[str, ProbeResult]) -> tuple[str, str]:
    probe = probes.get(instance)
    if probe is None:  # not running -> not probed
        return "—", "—"
    return str(probe[0]), str(probe[1])


def _present_spec_state(
    instance: str, spec: ComposedSpec, probes: Mapping[str, ProbeResult]
) -> str:
    """SPEC column for a present dedicated VM: drift + under flag, only while running."""
    probe = probes.get(instance)
    if probe is None:  # not running -> not probed
        return "ok"
    state = "ok" if probe[2] == spec_fingerprint(spec) else "NEEDS-REBUILD"
    if spec.under:
        state = f"{state} ⚠ under ({','.join(spec.under)})"
    return state


def _list_rows(
    identity_name: str,
    identity: Identity,
    dedicated: list[DedicatedRow],
    status: dict[str, str],
    probes: Mapping[str, ProbeResult],
) -> list[dict[str, object]]:
    """Build display rows for one identity: the base VM plus each dedicated row."""
    base = _base_footprint(identity)
    rows: list[dict[str, object]] = []

    b_agents, b_humans = _occupancy(identity.vm_instance, probes)
    rows.append(
        {
            "scope": "base",
            "instance": "—",
            "backend": "local",
            "status": status.get(identity.vm_instance, "Not Created"),
            "cpus": cast("int", base["cpus"]),
            "memory": str(base["memory"]),
            "disk": str(base["disk"]),
            "agents": b_agents,
            "humans": b_humans,
            "spec": "ok",
        }
    )

    for d in dedicated:
        scope = f"{d.org}/{d.repo}"
        agents, humans = _occupancy(d.instance, probes)
        st = status.get(d.instance, "Not Created")
        if d.state == "orphaned":
            rows.append(
                {
                    "scope": scope,
                    "instance": d.instance_name or "—",
                    "backend": "local",
                    "status": st,
                    "cpus": "—",
                    "memory": "—",
                    "disk": "—",
                    "agents": agents,
                    "humans": humans,
                    "spec": "orphaned",
                }
            )
            continue
        override = identity.overrides.get((d.org, d.repo))
        spec = compose_vm_spec(
            identity=identity_name, base=base, stanza=d.stanza, override=override
        )
        rows.append(
            {
                "scope": scope,
                "instance": d.instance_name or "—",
                "backend": "local",
                "status": st,
                "cpus": spec.cpus,
                "memory": spec.memory,
                "disk": spec.disk,
                "agents": agents,
                "humans": humans,
                "spec": _present_spec_state(d.instance, spec, probes),
            }
        )
    return rows


def _cloud_instance_info(state_dir: Path, instance_name: str) -> tuple[str, str]:
    """Best-effort ``(status, external_ip)`` for one tofu state dir, never raising.

    ``instance_name`` is the gcloud instance name (``cloud_resource_name(slug)``,
    i.e. ``vrg-<hash>``) — NOT the readable state key, which names no GCP resource
    and would 404 (#1866). Queries gcloud directly with the persisted zone rather
    than going through ``OffPlatformBackend.status``. A single ``describe`` pulls
    both the run status and the ephemeral external IP (``natIP``) — separator-joined
    so one round-trip serves both columns. The external IP is ``""`` for an
    internal-only box (no ``access_config``) or one that isn't running (#1855).

    Failures degrade rather than raising, so ``list`` never errors on one box:
    ``("MISSING", "")`` when the provider reports the instance absent (drift — torn
    down out of band), and ``("", "")`` for any other failure (no zone, no creds,
    gcloud absent) so the caller shows the credential-less placeholder. The two are
    kept distinct so a real ``no creds`` is never confused with a vanished VM
    (#1866).
    """
    try:
        zone = vm_cloud.read_zone(state_dir)
    except RuntimeError:
        return "", ""
    try:
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "gcloud",
                "compute",
                "instances",
                "describe",
                instance_name,
                f"--zone={zone}",
                "--format=value[separator='|'](status,networkInterfaces[0].accessConfigs[0].natIP)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        return "", ""
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        if "not found" in stderr or "was not found" in stderr:
            return "MISSING", ""
        return "", ""
    status, _, external_ip = result.stdout.strip().partition("|")
    return status, external_ip


@dataclass(frozen=True)
class OffPlatformVm:
    """One off-platform VM reconstructed from local tofu state — no network.

    ``identity`` / ``org`` / ``repo`` come from the persistent disk's ``vergil-*``
    labels (``None`` for a never-applied or unlabeled state). ``status`` is the raw
    best-effort cloud status (``"RUNNING"`` for a live box; ``""`` when the provider
    cannot be reached). ``external_ip`` is the box's ephemeral public IP (``natIP``),
    or ``""`` for an internal-only box or when the provider cannot be reached.
    ``state_dir`` is the ``<state_key>/<provider>`` tofu directory; ``name`` is the
    readable state-key slug — it keys the local state path / labels and the display
    fallback, but it is **not** what GCP calls the VM. The gcloud instance name is
    the deterministic hash of the slug — use ``cloud_name`` for every gcloud / IAP
    call, never ``name`` (#1866).
    """

    name: str
    provider: str
    state_dir: Path
    identity: str | None
    org: str | None
    repo: str | None
    status: str
    instance: str | None = None
    volume_size: str | None = None
    vm_present: bool = True
    external_ip: str = ""

    @property
    def cloud_name(self) -> str:
        """The gcloud instance name for this box — ``cloud_resource_name(slug)``.

        The four-segment state-key slug overflows GCP's 63-char name limit, so the
        backend names the live resource ``vrg-<hash>`` at create time
        (``vm_cloud.cloud_resource_name``); identity/org/repo ride in labels, never
        in the name. ``name`` keys local state and display, so every call that
        addresses the box in GCP — ``describe`` for ``list`` status, the IAP
        transport for ``update`` / session listing — must use this, not ``name``,
        or it queries an instance that does not exist (#1866).
        """
        return vm_cloud.cloud_resource_name(self.name)

    @property
    def is_running(self) -> bool:
        return self.status == "RUNNING"

    @property
    def scope(self) -> str:
        """Human label for the box: ``org/repo`` when labeled, else the resource name."""
        if self.org and self.repo:
            return f"{self.org}/{self.repo}"
        return self.name

    @property
    def label(self) -> str:
        """Identity-qualified human label, so two boxes for the same org/repo stay distinct.

        A repo with both a ``vergil-user`` and ``vergil-audit`` off-platform box has
        two state dirs sharing one org/repo; ``scope`` alone collapses them into
        identical lines (#1812). Carrying the identity keeps every box addressable.
        """
        if self.identity:
            return f"{self.scope} [{self.identity}]"
        return self.scope

    @property
    def update_ref(self) -> str:
        """How an operator re-addresses this box for a targeted ``vrg-vm update``.

        Identity-qualified: a labeled box is reached by ``<org>/<repo> --identity
        <identity>`` (two identities can share one org/repo), an unlabeled one by
        ``--identity <identity>`` alone, falling back to the resource name only
        when even the identity is unknown (#1812).
        """
        if self.org and self.repo:
            base = f"{self.org}/{self.repo}"
            if self.identity:
                return f"{base} --identity {self.identity}"
            return base
        if self.identity:
            return f"--identity {self.identity}"
        return self.name


@dataclass
class RecordedState:
    """Everything actually built under one handle, discovered from disk + Lima."""

    lima_instance: str | None
    tofu_dirs: list[tuple[str, Path]]  # (provider, provider_state_dir)


def _recorded_state_for_handle(
    identity: str, org: str, repo: str, name: str | None
) -> RecordedState:
    """Enumerate the Lima box and every tofu provider state recorded for a handle.

    Acts on reality, not the live profile: the Lima instance named for the handle
    (if it exists) plus every ``~/.config/vergil/tofu/<slug>/<provider>/`` directory
    carrying recorded state. The slug is deterministic, so this is a direct glob of
    the handle's own subtree.
    """
    lima = instance_name(identity, org, repo, name)
    existing = {vm["name"] for vm in list_vms()}
    lima_instance = lima if lima in existing else None

    slug = state_slug(identity, org, repo, name)
    handle_root = Path.home() / ".config" / "vergil" / "tofu" / slug
    tofu_dirs: list[tuple[str, Path]] = []
    if handle_root.is_dir():
        for provider_dir in sorted(p for p in handle_root.iterdir() if p.is_dir()):
            has_state = (provider_dir / "volume.tfstate").exists() or (
                provider_dir / "vm.tfstate"
            ).exists()
            if has_state:
                tofu_dirs.append((provider_dir.name, provider_dir))
    return RecordedState(lima_instance, tofu_dirs)


def _off_platform_vms() -> list[OffPlatformVm]:
    """Enumerate off-platform VMs from local tofu state under ~/.config/vergil/tofu.

    The cross-backend companion to ``list_vms()`` (which sees only Lima): every
    ``<state_key>/<provider>/volume.tfstate`` is one off-platform box. Identity /
    org / repo are recovered from the disk's ``vergil-*`` labels; STATUS is a
    best-effort cloud query that degrades to ``""`` rather than raising. Shared by
    every fan-out-over-all-VMs caller (``list``, ``update --all``, session listing)
    so none of them silently sees only Lima boxes (issue #1803).
    """
    vms: list[OffPlatformVm] = []
    tofu_root = Path.home() / ".config" / "vergil" / "tofu"
    if not tofu_root.is_dir():
        return vms
    for volume_state in sorted(tofu_root.glob("*/*/volume.tfstate")):
        provider_dir = volume_state.parent
        provider = provider_dir.name
        state_key = provider_dir.parent.name
        parsed = vm_cloud.parse_volume_state(volume_state)
        labels = parsed.labels if parsed else {}
        size = f"{parsed.size_gib}GiB" if parsed and parsed.size_gib else None
        vm_present = (provider_dir / "vm.tfstate").exists()
        # GCP names the instance vrg-<hash>, not the readable state key, so the live
        # describe must target the deterministic cloud resource name (#1866).
        status, external_ip = (
            _cloud_instance_info(provider_dir, vm_cloud.cloud_resource_name(state_key))
            if vm_present
            else ("", "")
        )
        vms.append(
            OffPlatformVm(
                name=state_key,
                provider=provider,
                state_dir=provider_dir,
                identity=labels.get("vergil-identity"),
                org=labels.get("vergil-org"),
                repo=labels.get("vergil-repo"),
                instance=labels.get("vergil-instance"),
                status=status,
                volume_size=size,
                vm_present=vm_present,
                external_ip=external_ip,
            )
        )
    return vms


def _classify_off_platform(vm: OffPlatformVm, config: IdentityConfig) -> str:
    """Classify a recorded off-platform box against the repo's current profile.

    'orphaned' when the repo dropped its [vm], no longer declares this instance, or
    no longer composes this (off-platform, provider); 'ok' when it still matches.
    The handle is recovered exactly from the readable slug — no lossy label round-trip.

    A repo whose vergil.toml fails to parse is classified 'ok' conservatively —
    flagging it orphaned would invite destroying a VM whose spec may still be
    declared — and the failure is warned loudly with the config path (mirrors the
    Lima-path ``_classify_instance`` treatment of ConfigError).
    """
    identity_name, org, repo, inst_name = split_state_slug(vm.name)
    if org is None or repo is None:
        return "ok"  # a base box carries no per-repo spec
    identity = config.identities.get(identity_name)
    if identity is None:
        return "orphaned"
    repo_dir = Path(identity.projects_dir) / org / repo
    config_path = repo_dir / "vergil.toml"
    if not config_path.exists():
        return "orphaned"
    try:
        stanza = read_config(repo_dir).vm
        spec = compose_vm_spec(
            identity=identity_name,
            base=_base_footprint(identity),
            stanza=stanza,
            override=identity.overrides.get((org, repo)),
            instance=inst_name,
        )
    except ConfigError as exc:
        print(
            f"WARNING: cannot parse {config_path}: {exc} — "
            f"listing '{vm.name}' as present (unverified)",
            file=sys.stderr,
        )
        return "ok"
    except SpecError:
        return "orphaned"
    if not spec.off_platform or spec.provider != vm.provider:
        return "orphaned"
    return "ok"


def _cloud_list_rows(config: IdentityConfig) -> list[dict[str, object]]:
    """Display rows for off-platform VMs (the cloud half of ``vrg-vm list``).

    Reuses the shared ``_off_platform_vms()`` enumeration; an unauthed/unreachable
    provider yields a degraded ``unknown (no <provider> creds)`` placeholder rather
    than dropping the row or erroring the list. Each row carries the box's identity
    so two boxes sharing an org/repo (one per identity) stay distinct in the listing
    rather than collapsing into identical lines (#1812).
    """
    rows: list[dict[str, object]] = []
    for vm in _off_platform_vms():
        spec = _classify_off_platform(vm, config)
        if not vm.vm_present:
            status = "no-vm"
            disk = vm.volume_size or "—"
        else:
            status = vm.status or f"unknown (no {vm.provider} creds)"
            disk = "—"
        rows.append(
            {
                "identity": vm.identity or "—",
                "scope": vm.scope,
                "instance": vm.instance or "—",
                "backend": vm.provider,
                "status": status,
                "external_ip": vm.external_ip or "—",
                "disk": disk,
                "spec": spec,
            }
        )
    return rows


def _cmd_list(args: argparse.Namespace) -> int:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)

    if args.sessions:
        return _list_sessions(config, args)

    status = {vm["name"]: vm["status"] for vm in list_vms()}
    instances = list(status)

    discovered = {
        id_name: discover_dedicated(id_name, instances, identity.projects_dir)
        for id_name, identity in config.identities.items()
    }
    probes = _probe_running(config.identities, discovered, status)

    local_rows = [
        (id_name, r)
        for id_name, identity in config.identities.items()
        for r in _list_rows(id_name, identity, discovered[id_name], status, probes)
    ]
    cloud_rows = _cloud_list_rows(config)

    # SCOPE and STATUS carry the only variable-length values — long org/repo handles
    # and the wide 'unknown (no gcp creds)' placeholder — so size each to its widest
    # value with the prior fixed widths as floors. A value that overruns a fixed
    # field shoves every later column out of alignment, which is exactly what a long
    # handle did to SCOPE (#1866).
    scopes = [str(r["scope"]) for _id, r in local_rows] + [str(r["scope"]) for r in cloud_rows]
    statuses = [str(r["status"]) for _id, r in local_rows] + [str(r["status"]) for r in cloud_rows]
    scope_w = max(40, *(len(s) for s in scopes)) if scopes else 40
    status_w = max(11, *(len(s) for s in statuses)) if statuses else 11

    header = (
        f"{'IDENTITY':<14} {'SCOPE':<{scope_w}} {'INSTANCE':<11} {'BACKEND':<13} "
        f"{'STATUS':<{status_w}} {'EXTERNAL IP':<15} {'CPUS':<5} {'MEM':<7} {'DISK':<7} "
        f"{'AGENTS':<7} {'HUMANS':<7} {'SPEC':<22}"
    )
    print(header)
    print("─" * len(header))

    for id_name, r in local_rows:
        print(
            f"{id_name:<14} {r['scope']!s:<{scope_w}} {r.get('instance', '—')!s:<11} "
            f"{r['backend']!s:<13} {r['status']!s:<{status_w}} {'—':<15} "
            f"{r['cpus']!s:<5} {r['memory']!s:<7} {r['disk']!s:<7} "
            f"{r['agents']!s:<7} {r['humans']!s:<7} {r['spec']!s:<22}"
        )

    for r in cloud_rows:
        print(
            f"{r['identity']!s:<14} {r['scope']!s:<{scope_w}} {r['instance']!s:<11} "
            f"{r['backend']!s:<13} {r['status']!s:<{status_w}} {r['external_ip']!s:<15} "
            f"{'—':<5} {'—':<7} {r['disk']!s:<7} {'—':<7} {'—':<7} {r['spec']!s:<22}"
        )

    return 0


def _volume_rows() -> list[dict[str, object]]:
    """Enumerate off-platform persistent volumes from local tofu state.

    Globs ``~/.config/vergil/tofu/<state_key>/<provider>/volume.tfstate`` and
    parses each into a display row: identity, org/repo, disk name, size, zone,
    and region. Every field is sourced from the disk's stamped attributes and
    ``vergil-*`` labels — no network, no gcloud — so the listing reflects exactly
    what vrg-vm manages. A state that parses to no disk (a never-applied
    placeholder) degrades to a row keyed by its state dir rather than dropping
    out, so an operator still sees that the state exists.
    """
    rows: list[dict[str, object]] = []
    tofu_root = Path.home() / ".config" / "vergil" / "tofu"
    if not tofu_root.is_dir():
        return rows
    for volume_state in sorted(tofu_root.glob("*/*/volume.tfstate")):
        provider_dir = volume_state.parent
        provider = provider_dir.name
        state_key = provider_dir.parent.name
        vm_type = vm_cloud.parse_vm_machine_type(provider_dir / "vm.tfstate") or "—"
        parsed = vm_cloud.parse_volume_state(volume_state)
        if parsed is None:
            rows.append(
                {
                    "identity": "—",
                    "scope": state_key,
                    "instance": "—",
                    "name": "—",
                    "size": "—",
                    "zone": "—",
                    "region": "—",
                    "provider": provider,
                    "vm_type": vm_type,
                }
            )
            continue
        org = parsed.labels.get("vergil-org")
        repo = parsed.labels.get("vergil-repo")
        scope = f"{org}/{repo}" if org and repo else state_key
        region = vm_cloud.zone_to_region(parsed.zone)
        rows.append(
            {
                "identity": parsed.labels.get("vergil-identity") or "—",
                "scope": scope,
                "instance": parsed.labels.get("vergil-instance") or "—",
                "name": parsed.name or "—",
                "size": f"{parsed.size_gib}GiB" if parsed.size_gib is not None else "—",
                "zone": parsed.zone or "—",
                "region": region or "—",
                "provider": provider,
                "vm_type": vm_type,
            }
        )
    return rows


def _volume_live_status(name: str, zone: str, provider: str) -> str:
    """Best-effort live check of one volume against the cloud, never raising.

    Returns the disk's live status (``READY``/``CREATING``/…) when it exists,
    ``MISSING`` when the provider reports it absent (drift — deleted out of
    band), or a degraded ``unknown (…)`` placeholder when the provider cannot be
    queried (no gcloud, no creds, unknown provider). Only GCP is wired today; any
    other provider yields the degraded placeholder rather than a false reading.
    """
    if provider != "gcp" or name in {"", "—"} or zone in {"", "—"}:
        return f"unknown (no {provider} live check)"
    try:
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "gcloud",
                "compute",
                "disks",
                "describe",
                name,
                f"--zone={zone}",
                "--format=value(status)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        return "unknown (no gcloud)"
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").lower()
        if "not found" in stderr or "was not found" in stderr:
            return "MISSING"
        return f"unknown (no {provider} creds)"
    return result.stdout.strip() or "MISSING"


def _cmd_volumes(args: argparse.Namespace) -> int:
    """List off-platform persistent volumes from local tofu state.

    The persistent volume is the long-lived, billable, quota-consuming object
    that outlives every ephemeral cloud VM, so it is the one off-platform object
    an operator most needs to enumerate and identify — for cleanup (which volume
    to ``destroy-volume``) and for cost/quota management. ``--live`` adds a column
    that cross-checks each disk against the provider to spot drift (a disk
    deleted out of band, or one stuck mid-create).
    """
    rows = _volume_rows()
    live = getattr(args, "live", False)
    if live:
        for r in rows:
            r["live"] = _volume_live_status(str(r["name"]), str(r["zone"]), str(r["provider"]))

    scope_w = max([24, *(len(str(r["scope"])) for r in rows)])
    name_w = max([20, *(len(str(r["name"])) for r in rows)])
    inst_w = max([10, *(len(str(r.get("instance", "—"))) for r in rows)])
    header = (
        f"{'IDENTITY':<14} {'ORG/REPO':<{scope_w}} {'INSTANCE':<{inst_w}} "
        f"{'DISK NAME':<{name_w}} {'SIZE':<8} {'ZONE':<16} {'REGION':<14} {'VM TYPE':<16}"
    )
    if live:
        header += f" {'LIVE':<22}"
    print(header)
    print("─" * len(header))
    for r in rows:
        line = (
            f"{r['identity']!s:<14} {r['scope']!s:<{scope_w}} "
            f"{r.get('instance', '—')!s:<{inst_w}} {r['name']!s:<{name_w}} "
            f"{r['size']!s:<8} {r['zone']!s:<16} {r['region']!s:<14} {r.get('vm_type', '—')!s:<16}"
        )
        if live:
            line += f" {r['live']!s:<22}"
        print(line)
    if not rows:
        print("(no off-platform volumes found)")
    return 0


def _vm_active_sessions(instance: str) -> dict[str, dict[str, object]]:
    """Map active session id -> its row from a running VM's resolver.

    A running VM owns its roster, so it is the only place a live session's name
    is known — including a freshly created session that has no host transcript
    yet. The full row (identity, slot, path, lastActive) is returned so the host
    can both name and age such sessions.
    """
    result = shell_run(instance, "vrg-vm-resolve-session", "--list-json")
    rows = json.loads(result.stdout)
    return {row["sessionId"]: row for row in rows if row.get("state") == "active"}


def _off_platform_active_sessions(vm: OffPlatformVm) -> dict[str, dict[str, object]]:
    """Map active session id -> row from a running off-platform box over IAP.

    The cloud analog of ``_vm_active_sessions``: the same
    ``vrg-vm-resolve-session --list-json`` query, but carried over the IAP SSH
    transport instead of limactl. The box owns its own roster, so it is the only
    source for a live session's name — exactly as for a Lima box.
    """
    transport = vm_cloud.off_platform_transport(vm.cloud_name, vm.state_dir)
    result = transport.run("vrg-vm-resolve-session", "--list-json")
    rows = json.loads(result.stdout)
    return {row["sessionId"]: row for row in rows if row.get("state") == "active"}


def _format_age(last_active: float | None, now: float) -> str:
    if last_active is None:
        return "unknown"
    days = (now - last_active) / 86400.0
    if days < 1:
        return f"{int(days * 24)}h"
    return f"{int(days)}d"


def _selected_states(args: argparse.Namespace) -> set[str]:
    if args.all:
        return {"active", "idle", "archived"}
    states = {s for s in ("active", "idle", "archived") if getattr(args, s)}
    return states or {"active", "idle"}


def _list_sessions(config: IdentityConfig, args: argparse.Namespace) -> int:
    """List named Claude sessions across all identity VMs.

    Transcripts are shared (host-backed), so idle ages and archived rows come
    from the host store; active liveness, names, and updatedAt are queried per
    running VM, since each VM owns its own roster. A live session is named from
    that roster, so the VM is the only source for one with no host transcript.
    """
    vm_map = {vm["name"]: vm["status"] for vm in list_vms()}
    active_rows: dict[str, dict[str, object]] = {}
    for identity in config.identities.values():
        if vm_map.get(identity.vm_instance) == "Running":
            active_rows.update(_vm_active_sessions(identity.vm_instance))

    # Off-platform boxes live in tofu state, not Lima, so list_vms() never sees
    # them; query each running one's roster over IAP the same way (issue #1803). A
    # query failure (no creds, unreachable, malformed) degrades to a warning rather
    # than erroring the whole listing.
    for vm in _off_platform_vms():
        if not vm.is_running:
            continue
        try:
            active_rows.update(_off_platform_active_sessions(vm))
        except (subprocess.CalledProcessError, OSError, json.JSONDecodeError, RuntimeError) as exc:
            print(
                f"WARNING: could not query sessions on off-platform box '{vm.scope}': {exc}",
                file=sys.stderr,
            )

    projects = Path.home() / ".claude" / "projects"
    names = name_by_session(projects)
    # Adopt the VM-reported name for any active session the host has no
    # transcript for (e.g. a brand-new session with zero turns); the host store
    # alone would drop it entirely.
    for sid, row in active_rows.items():
        names.setdefault(
            sid, make_name(str(row["identity"]), cast("int", row["slot"]), str(row["path"]))
        )
    last_active: dict[str, float] = {
        sid: cast("float", row.get("lastActive") or 0.0) for sid, row in active_rows.items()
    }
    for sid in names:
        if sid not in last_active:
            ts = _last_activity(projects_glob(projects, sid))
            if ts is not None:
                last_active[sid] = ts

    active = set(active_rows)
    rows: list[dict[str, object]] = [
        {
            "identity": row.identity,
            "slot": row.slot,
            "path": row.path,
            "state": "active" if row.active else "idle",
            "lastActive": row.last_active,
        }
        for row in list_rows(names, active, last_active)
    ]
    rows.extend(_archived_rows(names, last_active))

    wanted = _selected_states(args)
    rows = [r for r in rows if r["state"] in wanted]
    # Group by workspace first, then slot within a workspace; identity is the
    # final tiebreaker so same-workspace/same-slot rows stay stably ordered.
    rows.sort(key=lambda r: (str(r["path"]), cast("int", r["slot"]), str(r["identity"])))

    now = datetime.datetime.now(tz=datetime.UTC).timestamp()
    # Size the WORKSPACE column to the longest path present (36 floor), so a
    # path wider than the historical fixed width keeps SLOT/STATE/LAST ACTIVE
    # aligned.
    ws = max([36, *(len(str(r["path"])) for r in rows)])
    print(f"{'IDENTITY':<16} {'WORKSPACE':<{ws}} {'SLOT':<6} {'STATE':<9} {'LAST ACTIVE':<12}")
    print(f"{'─' * 16} {'─' * ws} {'─' * 6} {'─' * 9} {'─' * 12}")
    for r in rows:
        slot = f"{cast('int', r['slot']):02d}"
        age = _format_age(cast("float | None", r.get("lastActive")), now)
        print(f"{r['identity']!s:<16} {r['path']!s:<{ws}} {slot:<6} {r['state']!s:<9} {age:<12}")

    return 0


def _session_inner(
    args: argparse.Namespace,
    identity_name: str,
    rel_path: str,
    model: str,
    stale_days: int,
    archive_days: int,
) -> str:
    """Build the in-VM command: a raw override, or the session resolver."""
    source = ". ~/.config/vergil/claude.env 2>/dev/null;"

    override = list(args.cmd)
    if override and override[0] == "--":
        override = override[1:]

    # Any non-claude command runs raw (the `-- bash` escape hatch). A bare
    # `claude` (or no command) goes through the resolver for naming/resume.
    if override and override[0] != "claude":
        return f"{source} exec {shlex.join(override)}"

    passthrough = override[1:] if override[:1] == ["claude"] else []
    # The resolved model is applied first; an explicit `-- claude --model X`
    # passthrough comes after, so Claude's last --model wins.
    extra = (["--model", model] if model else []) + passthrough
    resolve_cmd = [
        "vrg-vm-resolve-session",
        "--identity",
        identity_name,
        "--path",
        rel_path,
    ]
    if args.slot is not None:
        resolve_cmd += ["--slot", str(args.slot)]
    if args.fork:
        resolve_cmd += ["--fork"]
    if args.fresh:
        resolve_cmd += ["--fresh"]
    resolve_cmd += ["--stale-days", str(stale_days), "--archive-days", str(archive_days)]
    if extra:
        resolve_cmd += ["--", *extra]
    return f"{source} exec {shlex.join(resolve_cmd)}"


def _cloud_session(target: Target, args: argparse.Namespace) -> int:
    """Launch a Claude session on the off-platform (cloud) box over the IAP transport.

    Reuses the exact inner resolver/override command the Lima path builds; only the
    transport differs. Cloud boxes are ephemeral, so the Lima preflight and the
    staleness gate (both Lima-specific) do not apply here.
    """
    name, identity, config = target.identity_name, target.identity, target.config
    backend = _cloud_backend(target)
    vm_cloud.preflight()
    _warn_cloud_under(target)

    transport = backend.transport()
    workspace_abs = os.path.normpath(resolve_workspace(args.workspace, identity.projects_dir))
    rel_path = os.path.relpath(workspace_abs, identity.projects_dir)
    inner = _session_inner(
        args,
        name,
        rel_path,
        resolve_model(config, identity, args.model),
        resolve_session_stale_days(config, identity),
        resolve_session_archive_days(config, identity),
    )
    workdir = f"/vergil/projects/{target.org}/{target.repo}"
    transport.exec_session(workdir=workdir, inner=inner)
    return 0  # unreachable, keeps the type checker happy


def _cmd_session(args: argparse.Namespace) -> int:
    target = _resolve_target(args, borrow_allowed=True)
    name, identity, config = target.identity_name, target.identity, target.config

    if target.spec.off_platform:
        return _cloud_session(target, args)

    if _preflight_target(target) != 0:
        return 1

    if not args.allow_stale_vm:
        age = vm_age_days(target.instance)
        if age is not None and age > target.spec.stale_days:
            ref = _target_ref(target)
            print(
                f"ERROR: VM '{target.instance}' is {age:.0f} days old"
                f" (threshold: {target.spec.stale_days} days).\n"
                f"Rebuild with: vrg-vm rebuild {ref}\n"
                f"Override with: vrg-vm session --allow-stale-vm {ref}",
                file=sys.stderr,
            )
            return 1

    fallback = resolve_vergil_version(config, identity)
    transport = target.backend.transport(target.instance)
    try_update_tooling(transport, fallback_tag=fallback)

    claude_dir = Path.home() / ".claude"
    copy_claude_config(transport, claude_dir)
    link_claude_dirs(transport, claude_dir)

    workspace_abs = os.path.normpath(resolve_workspace(args.workspace, identity.projects_dir))
    rel_path = os.path.relpath(workspace_abs, identity.projects_dir)

    os.environ["LIMA_SHELLENV_ALLOW"] = _TERMINAL_ENV_VARS

    inner = _session_inner(
        args,
        name,
        rel_path,
        resolve_model(config, identity, args.model),
        resolve_session_stale_days(config, identity),
        resolve_session_archive_days(config, identity),
    )
    cmd = [
        "limactl",
        "shell",
        "--start",
        "--preserve-env",
        f"--workdir={workspace_abs}",
        target.instance,
        "bash",
        "-c",
        inner,
    ]
    os.execvp(cmd[0], cmd)  # noqa: S606, S607
    return 0  # unreachable, keeps the type checker happy


def _add_identity_args(parser: argparse.ArgumentParser) -> None:
    """Add per-subcommand ``--identity``/``--config`` (placed after the subcommand).

    Their defaults are ``SUPPRESS`` so that omitting them after the subcommand does
    not overwrite a value supplied *before* it via the matching top-level options
    (``_add_global_identity_args``). That is the standard argparse parent/subparser
    default-clobber gotcha: without SUPPRESS, the subparser's own default would
    silently reset ``args.identity``/``args.config`` whenever the option appeared
    only before the subcommand. The top-level parser owns the real defaults.
    """
    parser.add_argument(
        "--identity",
        default=argparse.SUPPRESS,
        help="Identity name (default: default_identity)",
    )
    parser.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help="Path to identities.toml"
    )


def _add_global_identity_args(parser: argparse.ArgumentParser) -> None:
    """Add ``--identity``/``--config`` on the top-level parser, owning the real defaults.

    Defining them here lets both options appear *before* the subcommand
    (``vrg-vm --identity X session repo``); the per-subcommand copies let them
    appear after it too. These defaults guarantee the attributes always exist, so
    the subcommand copies can safely use SUPPRESS.
    """
    parser.add_argument(
        "--identity", default=None, help="Identity name (default: default_identity)"
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to identities.toml")


def _add_workspace_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "workspace",
        nargs="?",
        default=None,
        help="Optional <org>/<repo> to target a dedicated VM (default: the base VM)",
    )


def _add_name_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--name",
        default=None,
        help=(
            "Named VM instance for this repo (default: the unnamed default instance). "
            "Must be declared under [vm.<identity>.instances.<name>]."
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-vm",
        description="Manage identity VM lifecycle",
    )
    _add_global_identity_args(parser)
    sub = parser.add_subparsers(dest="command")

    p_create = sub.add_parser("create", help="Create and provision a new VM")
    _add_identity_args(p_create)
    _add_workspace_arg(p_create)
    _add_name_arg(p_create)
    p_create.add_argument(
        "--tag", default="", help="VM template version tag (default: vergil version from config)"
    )
    p_create.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Stream live cloud-init provisioning output during await-readiness "
            "(off-platform only; also enabled by VERGIL_VM_VERBOSE)"
        ),
    )
    progress.add_progress_args(p_create, ())

    p_start = sub.add_parser("start", help="Start VM and inject credentials")
    _add_identity_args(p_start)
    _add_workspace_arg(p_start)
    _add_name_arg(p_start)
    p_start.add_argument(
        "--allow-stale-vm",
        action="store_true",
        help="Start even if the VM exceeds the staleness threshold",
    )
    p_start.add_argument(
        "--timeout",
        default="30m",
        help="How long to wait for VM to reach running status (default: 30m)",
    )
    progress.add_progress_args(p_start, ())

    p_stop = sub.add_parser("stop", help="Stop VM")
    _add_identity_args(p_stop)
    _add_workspace_arg(p_stop)
    _add_name_arg(p_stop)

    p_restart = sub.add_parser("restart", help="Restart VM and re-inject credentials")
    _add_identity_args(p_restart)
    _add_workspace_arg(p_restart)
    _add_name_arg(p_restart)

    p_update = sub.add_parser(
        "update", help="Reinstall vergil-tooling and refresh plugins in a running box (in place)"
    )
    _add_identity_args(p_update)
    _add_workspace_arg(p_update)
    _add_name_arg(p_update)
    p_update.add_argument(
        "--tag", default="", help="Override version tag (default: tag from initial install)"
    )
    p_update.add_argument(
        "--all",
        action="store_true",
        help=(
            "Update every box owned by a configured identity (fail-deferred: all "
            "boxes are attempted even if one fails; non-running boxes are skipped and "
            "reported). Off-platform boxes are updated in place over IAP, exactly "
            "like Lima boxes — no rebuild."
        ),
    )
    progress.add_progress_args(p_update, ())

    p_destroy = sub.add_parser("destroy", help="Destroy VM entirely")
    _add_identity_args(p_destroy)
    _add_workspace_arg(p_destroy)
    _add_name_arg(p_destroy)
    p_destroy.add_argument(
        "--tag",
        default="",
        help="OpenTofu module version tag for off-platform destroy (default: from config)",
    )
    p_destroy.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the confirmation prompt (required for non-interactive destroy)",
    )

    p_destroy_volume = sub.add_parser(
        "destroy-volume",
        help="Destroy an off-platform VM's PERSISTENT volume (irreversible)",
    )
    _add_identity_args(p_destroy_volume)
    _add_workspace_arg(p_destroy_volume)
    _add_name_arg(p_destroy_volume)
    p_destroy_volume.add_argument(
        "--tag",
        default="",
        help="OpenTofu module version tag (default: from config)",
    )
    p_destroy_volume.add_argument(
        "--yes",
        action="store_true",
        help="Skip the org/repo confirmation prompt",
    )

    p_rebuild = sub.add_parser("rebuild", help="Destroy and recreate VM (stateless rebuild)")
    _add_identity_args(p_rebuild)
    _add_workspace_arg(p_rebuild)
    _add_name_arg(p_rebuild)
    p_rebuild.add_argument(
        "--tag", default="", help="VM template version tag (default: vergil version from config)"
    )
    p_rebuild.add_argument(
        "--timeout",
        default="30m",
        help="How long to wait for VM to reach running status (default: 30m)",
    )
    p_rebuild.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Stream live cloud-init provisioning output during await-readiness "
            "(off-platform only; also enabled by VERGIL_VM_VERBOSE)"
        ),
    )
    progress.add_progress_args(p_rebuild, ())

    p_list = sub.add_parser(
        "list",
        help="List all identity VMs and their status",
        description=(
            "List each identity's base and dedicated VMs with configured footprint "
            "(CPUS/MEM/DISK), live occupancy, and SPEC health. Dedicated rows enumerate "
            "existing VM instances only; a spec'd repo whose VM was never built is "
            "surfaced by the session/start preflight gate, not here. AGENTS counts "
            "harness instances (Claude Code sessions); HUMANS counts open human-held "
            "interactive shells (a tally of shells, not distinct people). SPEC is one "
            "of: ok, NEEDS-REBUILD (drift — rebuild it), orphaned (a VM whose repo "
            "dropped its [vm]), or an 'under' flag when a host override sized the box "
            "below the repo's declared footprint."
        ),
    )
    # SUPPRESS so a global `--config` (before the subcommand) is not clobbered;
    # `list` carries no `--identity` (identity does not scope a list of all VMs).
    p_list.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help="Path to identities.toml"
    )
    p_list.add_argument(
        "--sessions",
        action="store_true",
        help="List named Claude sessions across identity VMs instead of VMs",
    )
    p_list.add_argument("--active", action="store_true", help="With --sessions: only active")
    p_list.add_argument("--idle", action="store_true", help="With --sessions: only idle")
    p_list.add_argument("--archived", action="store_true", help="With --sessions: only archived")
    p_list.add_argument("--all", action="store_true", help="With --sessions: include archived too")

    p_volumes = sub.add_parser(
        "volumes",
        help="List off-platform persistent volumes",
        description=(
            "List every off-platform persistent volume from local tofu state — the "
            "long-lived, billable, quota-consuming disks that outlive each ephemeral "
            "cloud VM. Columns: IDENTITY, ORG/REPO, DISK NAME, SIZE, ZONE, REGION, all "
            "read from the disk's stamped labels/attributes with no network call, so the "
            "listing reflects exactly what vrg-vm manages. --live cross-checks each disk "
            "against the provider (a LIVE column) to spot drift: a disk deleted out of "
            "band shows MISSING; an unauthed/unreachable provider degrades to 'unknown'."
        ),
    )
    p_volumes.add_argument(
        "--live",
        action="store_true",
        help="Cross-check each volume against the cloud provider (adds a LIVE column)",
    )

    p_session = sub.add_parser("session", help="Launch a Claude session in a VM")
    _add_identity_args(p_session)
    p_session.add_argument(
        "--name",
        default=None,
        help=(
            "Named VM instance for this repo (default: the unnamed default instance). "
            "Must be declared under [vm.<identity>.instances.<name>]."
        ),
    )
    p_session.add_argument(
        "--allow-stale-vm",
        action="store_true",
        help="Connect even if the VM exceeds the staleness threshold",
    )
    p_session.add_argument(
        "--slot",
        type=int,
        help="Session slot number (1-99); default picks the lowest idle/free slot",
    )
    p_session.add_argument(
        "--fork",
        action="store_true",
        help="Fork the targeted --slot into a new session instead of resuming it",
    )
    p_session.add_argument(
        "--fresh",
        action="store_true",
        help="Start a brand-new session, archiving the old one and reclaiming its name",
    )
    p_session.add_argument(
        "--model",
        default="",
        help="Claude model to launch (overrides the identity's 'model' default)",
    )
    p_session.add_argument(
        "workspace", help="Workspace path relative to projects_dir (use '.' for the root)"
    )
    p_session.add_argument(
        "cmd",
        nargs="*",
        help=(
            "Optional command override. A bare 'claude' (or nothing) goes through the "
            "session resolver; anything else runs raw. Pass flags after '--' so they are "
            "not parsed as session options (e.g. '-- bash', '-- claude --model X')."
        ),
    )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    dispatch = {
        "create": _cmd_create,
        "start": _cmd_start,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "update": _cmd_update,
        "destroy": _cmd_destroy,
        "destroy-volume": _cmd_destroy_volume,
        "rebuild": _cmd_rebuild,
        "list": _cmd_list,
        "volumes": _cmd_volumes,
        "session": _cmd_session,
    }
    try:
        return dispatch[args.command](args)
    except (BorrowError, SpecError, NotImplementedError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
