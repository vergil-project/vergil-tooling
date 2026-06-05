"""Manage identity VM lifecycle."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
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
    update_tooling,
    vm_age_days,
    vm_probe,
    vm_spec_status,
    vm_status,
)
from vergil_tooling.lib.session import list_rows, make_name
from vergil_tooling.lib.vm_spec import (
    ComposedSpec,
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


def _resolve_target(args: argparse.Namespace) -> Target:
    """Resolve (identity, optional org/repo) to a base or dedicated VM target."""
    name, identity, config = _resolve(args)
    workspace = getattr(args, "workspace", None)
    base = _base_footprint(identity)
    org, repo = _workspace_org_repo(workspace)

    if org is None or repo is None:
        spec = compose_vm_spec(identity=name, base=base, stanza=None, override=None)
        return Target(name, identity, config, None, None, spec, identity.vm_instance, "")

    repo_dir = Path(resolve_workspace(f"{org}/{repo}", identity.projects_dir))
    try:
        stanza = read_config(repo_dir).vm
    except FileNotFoundError:
        stanza = None  # a repo with no vergil.toml needs no dedicated VM
    override = identity.overrides.get((org, repo))
    spec = compose_vm_spec(identity=name, base=base, stanza=stanza, override=override)

    if not spec.dedicated:
        return Target(name, identity, config, org, repo, spec, identity.vm_instance, "")

    inst = instance_name(name, org, repo)
    return Target(name, identity, config, org, repo, spec, inst, spec_fingerprint(spec))


def _resolve_instance(args: argparse.Namespace) -> tuple[str, Identity, IdentityConfig, str]:
    """Resolve just the instance NAME for lifecycle commands (stop/restart/destroy/update).

    Unlike `_resolve_target`, this reads no repo `vergil.toml` and composes no spec — it only
    needs the instance to act on. A 2-level `org/repo` names the dedicated instance directly,
    so an orphaned VM (whose repo dropped its `[vm]`) is still reachable; anything else is base.
    """
    name, identity, config = _resolve(args)
    org, repo = _workspace_org_repo(getattr(args, "workspace", None))
    if org is not None and repo is not None:
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
    if vm_spec_status(target.instance, target.fingerprint) == "needs-rebuild":
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

    print(f"  Fetching template ({tag})...")
    template = fetch_template(tag)

    try:
        print(f"  Creating VM with projects mount: {identity.projects_dir}")
        _create_from_target(target, template)

        print("  Starting VM...")
        start_vm(target.instance)

        print("  Linking Claude config directories...")
        link_claude_dirs(target.instance, Path.home() / ".claude")

        print("Injecting credentials...")
        inject_credentials(target.instance, identity)

        install_tooling(target.instance, vergil_version)
    finally:
        template.unlink(missing_ok=True)

    print(f"\nVM '{target.instance}' is ready.")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    target = _resolve_target(args)
    name, identity, config = target.identity_name, target.identity, target.config

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
    start_vm(target.instance, timeout=args.timeout)

    print("Injecting credentials...")
    inject_credentials(target.instance, identity)

    claude_dir = Path.home() / ".claude"
    print("Copying Claude Code config...")
    copy_claude_config(target.instance, claude_dir)
    link_claude_dirs(target.instance, claude_dir)

    fallback = resolve_vergil_version(config, identity)
    print("Updating vergil-tooling...")
    try_update_tooling(target.instance, fallback_tag=fallback)

    print(f"VM '{target.instance}' is running.")
    return 0


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


def _cmd_update(args: argparse.Namespace) -> int:
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

    print("Update complete.")
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
        print(
            f"ERROR: VM '{target.instance}' does not exist — run 'vrg-vm create' first",
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

    vergil_version = resolve_vergil_version(config, identity)
    tag = args.tag if args.tag else resolve_vm_tag(config, identity)

    print(f"Rebuilding VM '{target.instance}' (identity: {name})...")

    print("  Destroying old VM...")
    delete_vm(target.instance)

    print(f"  Fetching template ({tag})...")
    template = fetch_template(tag)

    try:
        print(f"  Creating VM with projects mount: {identity.projects_dir}")
        _create_from_target(target, template)

        print("  Starting VM...")
        start_vm(target.instance, timeout=args.timeout)

        print("  Injecting credentials...")
        inject_credentials(target.instance, identity)

        install_tooling(target.instance, vergil_version)

        claude_dir = Path.home() / ".claude"
        print("  Copying Claude Code config...")
        copy_claude_config(target.instance, claude_dir)
        link_claude_dirs(target.instance, claude_dir)
    finally:
        template.unlink(missing_ok=True)

    print(f"\nVM '{target.instance}' rebuilt and ready.")
    return 0


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
    rows.sort(key=lambda r: (str(r["identity"]), cast("int", r["slot"]), str(r["path"])))

    now = datetime.datetime.now(tz=datetime.UTC).timestamp()
    print(f"{'IDENTITY':<16} {'SLOT':<6} {'WORKSPACE':<36} {'STATE':<9} {'LAST ACTIVE':<12}")
    print(f"{'─' * 16} {'─' * 6} {'─' * 36} {'─' * 9} {'─' * 12}")
    for r in rows:
        slot = f"{cast('int', r['slot']):02d}"
        age = _format_age(cast("float | None", r.get("lastActive")), now)
        print(f"{r['identity']!s:<16} {slot:<6} {r['path']!s:<36} {r['state']!s:<9} {age:<12}")

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
    target = _resolve_target(args)
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
    parser.add_argument("--identity", help="Identity name (default: default_identity)")
    parser.add_argument("--config", type=Path, help="Path to identities.toml")


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
    sub = parser.add_subparsers(dest="command")

    p_create = sub.add_parser("create", help="Create and provision a new VM")
    _add_identity_args(p_create)
    _add_workspace_arg(p_create)
    p_create.add_argument(
        "--tag", default="", help="VM template version tag (default: vergil version from config)"
    )

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
    p_list.add_argument("--config", type=Path, help="Path to identities.toml")
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
        nargs=argparse.REMAINDER,
        help="Optional command override after '--' (e.g. -- bash)",
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
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
