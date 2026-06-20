"""Manage identity VM lifecycle."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

from vergil_tooling.bin.vrg_vm_resolve import (
    _archived_rows,
    _last_activity,
    name_by_session,
    projects_glob,
)
from vergil_tooling.lib import progress
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
    copy_claude_config,
    create_vm,
    delete_vm,
    fetch_template,
    get_tooling_version,
    inject_credentials,
    install_tooling,
    link_claude_dirs,
    list_vms,
    nested_virt_unsupported_reason,
    shell_run,
    start_vm,
    stop_vm,
    try_update_tooling,
    update_plugins,
    update_tooling,
    vm_age_days,
    vm_probe,
    vm_spec_status,
    vm_status,
)
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.session import list_rows, make_name
from vergil_tooling.lib.vm_spec import (
    ComposedSpec,
    SpecError,
    compose_vm_spec,
    instance_name,
    parse_instance_name,
    spec_fingerprint,
)

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
    base = _base_footprint(identity)
    org, repo = _workspace_org_repo(workspace)

    if org is None or repo is None:
        spec = compose_vm_spec(identity=name, base=base, stanza=None, override=None)
        return Target(name, identity, config, None, None, spec, identity.vm_instance, "")

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
    spec = compose_vm_spec(identity=name, base=base, stanza=eff_vm, override=override)

    if not spec.dedicated:
        return Target(name, identity, config, org, repo, spec, identity.vm_instance, "")

    inst = instance_name(name, eff_org, eff_repo)
    return Target(name, identity, config, eff_org, eff_repo, spec, inst, spec_fingerprint(spec))


def _resolve_instance(args: argparse.Namespace) -> tuple[str, Identity, IdentityConfig, str]:
    """Resolve just the instance NAME for lifecycle commands (stop/restart/destroy/update).

    These are all MANAGE commands: if the requested repo borrows a VM via
    ``[vm] shared_from`` they are blocked with ``BorrowError`` pointing at the
    lender. A repo with no readable ``vergil.toml`` (a true orphan) is unaffected
    and resolves to its own instance name, so an orphaned dedicated VM (whose repo
    dropped its `[vm]`) stays reachable; anything else is base.
    """
    name, identity, config = _resolve(args)
    org, repo = _workspace_org_repo(getattr(args, "workspace", None))
    if org is not None and repo is not None:
        requested_vm = _read_repo_vm(identity, org, repo)
        if requested_vm is not None and requested_vm.shared_from is not None:
            lender_org, lender_repo = requested_vm.shared_from
            raise BorrowError(_borrow_block_msg(args.command, org, repo, lender_org, lender_repo))
        instance = instance_name(name, org, repo)
    else:
        instance = identity.vm_instance
    return name, identity, config, instance


def _target_ref(target: Target) -> str:
    """How a user re-addresses this target on the CLI: '<org>/<repo>' or '--identity <name>'."""
    if target.org is not None:
        return f"{target.org}/{target.repo}"
    return f"--identity {target.identity_name}"


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
        spec_status = vm_spec_status(target.instance, target.fingerprint)
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
    spec_status = vm_spec_status(target.instance, target.fingerprint)
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
    link_claude_dirs(state.target.instance, Path.home() / ".claude")


def _st_credentials(state: _LifecycleState) -> None:
    inject_credentials(state.target.instance, state.target.identity)


def _st_install_tooling(state: _LifecycleState) -> None:
    install_tooling(state.target.instance, state.vergil_version)


def _st_copy_config(state: _LifecycleState) -> None:
    claude_dir = Path.home() / ".claude"
    copy_claude_config(state.target.instance, claude_dir)
    link_claude_dirs(state.target.instance, claude_dir)


def _st_update_tooling(state: _LifecycleState) -> None:
    # Runs as a warn-mode stage: a failed update surfaces as ⚠ in the summary
    # and the start continues — the same warn-and-continue contract
    # try_update_tooling provided before the pipeline port.
    fallback = resolve_vergil_version(state.target.config, state.target.identity)
    update_tooling(state.target.instance, fallback_tag=fallback)


def _st_update_plugins(state: _LifecycleState) -> None:
    # Warn-mode stage: a failed plugin refresh surfaces as ⚠ and the lifecycle
    # continues, the same warn-and-continue contract as update-tooling. Plugins
    # are VM-local; this advances them to the latest published versions.
    update_plugins(state.target.instance)


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
    else:
        create_vm(
            target.instance,
            template,
            target.identity.projects_dir,
            cpus=target.identity.cpus,
            memory=target.identity.memory,
            disk=target.identity.disk,
        )


def _cmd_create(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    name, identity, config = target.identity_name, target.identity, target.config
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


def _cmd_stop(args: argparse.Namespace) -> int:
    name, _identity, _config, instance = _resolve_instance(args)

    print(f"Stopping VM '{instance}' (identity: {name})...")
    stop_vm(instance)

    print(f"VM '{instance}' stopped.")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    name, identity, _config, instance = _resolve_instance(args)

    print(f"Restarting VM '{instance}' (identity: {name})...")
    stop_vm(instance)
    start_vm(instance)

    print("Injecting credentials...")
    inject_credentials(instance, identity)

    print(f"VM '{instance}' is running.")
    return 0


def _update_instance(instance: str, name: str, tag: str | None, fallback: str) -> None:
    """Update tooling and plugins in one running VM, printing the version transition."""
    print(f"Updating vergil-tooling in VM '{instance}' (identity: {name})...")

    before = get_tooling_version(instance)
    update_tooling(instance, tag, fallback_tag=fallback)
    after = get_tooling_version(instance)

    if before and after:
        if before == after:
            print(f"  vergil-tooling: {after} (already up to date)")
        else:
            print(f"  vergil-tooling: {before} → {after}")
    elif after:
        print(f"  vergil-tooling: {after}")

    update_plugins(instance)


def _cmd_update(args: argparse.Namespace) -> int:
    if args.all:
        return _cmd_update_all(args)

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
                ident, org, repo = parse_instance_name(inst)
            except ValueError:
                continue
            if ident == id_name and org is not None and repo is not None:
                targets.append((id_name, identity, inst))
    return targets


def _cmd_update_all(args: argparse.Namespace) -> int:
    """Update vergil-tooling in every existing VM owned by a configured identity.

    Fail-deferred by design: every VM in the list is attempted even after one
    fails; non-running VMs are skipped and reported; any failure makes the
    final exit code non-zero — only after all attempts have completed.
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
    if not targets:
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

    total = len(targets)
    print(
        f"Update complete: {len(updated)} updated, {len(skipped)} skipped, "
        f"{len(failed)} failed (of {total} VMs)."
    )
    if failed:
        names = ", ".join(f"'{inst}' ({reason})" for inst, reason in failed)
        print(f"failed to update {len(failed)} of {total}: {names}", file=sys.stderr)
        return 1
    return 0


def _cmd_destroy(args: argparse.Namespace) -> int:
    name, _identity, _config, instance = _resolve_instance(args)

    status = vm_status(instance)
    if not status:
        print(
            f"VM '{instance}' does not exist.",
            file=sys.stderr,
        )
        return 1

    print(f"Destroying VM '{instance}' (identity: {name})...")
    delete_vm(instance)

    print(f"VM '{instance}' destroyed.")
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    name, identity, config = target.identity_name, target.identity, target.config

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
            ident, org, repo = parse_instance_name(name)
        except ValueError:
            continue
        if ident != identity_name or org is None or repo is None:
            continue
        state, stanza = _classify_instance(projects_dir, org, repo, name)
        rows.append(DedicatedRow(org, repo, name, state, stanza))
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
            instance: pool.submit(vm_probe, instance, fingerprint=want_fp)
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

    header = (
        f"{'IDENTITY':<14} {'SCOPE':<40} {'STATUS':<11} {'CPUS':<5} {'MEM':<7} "
        f"{'DISK':<7} {'AGENTS':<7} {'HUMANS':<7} {'SPEC':<22}"
    )
    print(header)
    print("─" * len(header))

    for id_name, identity in config.identities.items():
        for r in _list_rows(id_name, identity, discovered[id_name], status, probes):
            print(
                f"{id_name:<14} {r['scope']!s:<40} {r['status']!s:<11} "
                f"{r['cpus']!s:<5} {r['memory']!s:<7} {r['disk']!s:<7} "
                f"{r['agents']!s:<7} {r['humans']!s:<7} {r['spec']!s:<22}"
            )

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


def _cmd_session(args: argparse.Namespace) -> int:
    target = _resolve_target(args, borrow_allowed=True)
    name, identity, config = target.identity_name, target.identity, target.config

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
    try_update_tooling(target.instance, fallback_tag=fallback)

    claude_dir = Path.home() / ".claude"
    copy_claude_config(target.instance, claude_dir)
    link_claude_dirs(target.instance, claude_dir)

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
    p_create.add_argument(
        "--tag", default="", help="VM template version tag (default: vergil version from config)"
    )
    progress.add_progress_args(p_create, ())

    p_start = sub.add_parser("start", help="Start VM and inject credentials")
    _add_identity_args(p_start)
    _add_workspace_arg(p_start)
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

    p_restart = sub.add_parser("restart", help="Restart VM and re-inject credentials")
    _add_identity_args(p_restart)
    _add_workspace_arg(p_restart)

    p_update = sub.add_parser("update", help="Reinstall vergil-tooling inside a running VM")
    _add_identity_args(p_update)
    _add_workspace_arg(p_update)
    p_update.add_argument(
        "--tag", default="", help="Override version tag (default: tag from initial install)"
    )
    p_update.add_argument(
        "--all",
        action="store_true",
        help=(
            "Update every VM owned by a configured identity (fail-deferred: all VMs "
            "are attempted even if one fails; non-running VMs are skipped and reported)"
        ),
    )

    p_destroy = sub.add_parser("destroy", help="Destroy VM entirely")
    _add_identity_args(p_destroy)
    _add_workspace_arg(p_destroy)

    p_rebuild = sub.add_parser("rebuild", help="Destroy and recreate VM (stateless rebuild)")
    _add_identity_args(p_rebuild)
    _add_workspace_arg(p_rebuild)
    p_rebuild.add_argument(
        "--tag", default="", help="VM template version tag (default: vergil version from config)"
    )
    p_rebuild.add_argument(
        "--timeout",
        default="30m",
        help="How long to wait for VM to reach running status (default: 30m)",
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

    p_session = sub.add_parser("session", help="Launch a Claude session in a VM")
    _add_identity_args(p_session)
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
        "rebuild": _cmd_rebuild,
        "list": _cmd_list,
        "session": _cmd_session,
    }
    try:
        return dispatch[args.command](args)
    except (BorrowError, SpecError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
