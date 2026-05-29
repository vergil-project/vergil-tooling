"""Manage identity VM lifecycle."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from vergil_tooling.bin.vrg_vm_resolve import name_by_session
from vergil_tooling.lib.identity import (
    Identity,
    IdentityConfig,
    default_config_path,
    load_config,
    resolve_identity_by_name,
    resolve_model,
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
    shell_run,
    start_vm,
    stop_vm,
    try_update_tooling,
    update_tooling,
    vm_age_days,
    vm_status,
)
from vergil_tooling.lib.session import list_rows

_default_config_path = default_config_path

_DEFAULT_STALENESS_DAYS = 3

_TERMINAL_ENV_VARS = "COLORTERM,TERM_PROGRAM,TERM_PROGRAM_VERSION"


def _resolve(args: argparse.Namespace) -> tuple[str, Identity, IdentityConfig]:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    name, identity = resolve_identity_by_name(config, args.identity)
    return name, identity, config


def _cmd_create(args: argparse.Namespace) -> int:
    name, identity, config = _resolve(args)
    vergil_version = resolve_vergil_version(config, identity)
    tag = args.tag if args.tag else resolve_vm_tag(config, identity)

    status = vm_status(identity.vm_instance)
    if status:
        print(
            f"ERROR: VM '{identity.vm_instance}' already exists (status: {status})",
            file=sys.stderr,
        )
        return 1

    if not identity.projects_dir:
        print(
            f"ERROR: identity '{name}' has no projects_dir configured",
            file=sys.stderr,
        )
        return 1

    print(f"Creating VM '{identity.vm_instance}' for identity '{name}'...")

    print(f"  Fetching template ({tag})...")
    template = fetch_template(tag)

    try:
        print(f"  Creating VM with projects mount: {identity.projects_dir}")
        create_vm(
            identity.vm_instance,
            template,
            identity.projects_dir,
            cpus=identity.cpus,
            memory=identity.memory,
            disk=identity.disk,
        )

        print("  Starting VM...")
        start_vm(identity.vm_instance)

        print("  Linking Claude config directories...")
        link_claude_dirs(identity.vm_instance, Path.home() / ".claude")

        print("Injecting credentials...")
        inject_credentials(identity.vm_instance, identity)

        install_tooling(identity.vm_instance, vergil_version)
    finally:
        template.unlink(missing_ok=True)

    print(f"\nVM '{identity.vm_instance}' is ready.")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    name, identity, config = _resolve(args)

    status = vm_status(identity.vm_instance)
    if not status:
        print(
            f"ERROR: VM '{identity.vm_instance}' does not exist — run 'vrg-vm create' first",
            file=sys.stderr,
        )
        return 1

    allow_stale = getattr(args, "allow_stale_vm", False)
    if not allow_stale:
        age = vm_age_days(identity.vm_instance)
        if age is not None and age > _DEFAULT_STALENESS_DAYS:
            print(
                f"ERROR: VM '{identity.vm_instance}' is {age:.0f} days old"
                f" (threshold: {_DEFAULT_STALENESS_DAYS} days).\n"
                f"Rebuild with: vrg-vm rebuild --identity {name}\n"
                f"Override with: vrg-vm start --allow-stale-vm --identity {name}",
                file=sys.stderr,
            )
            return 1

    print(f"Starting VM '{identity.vm_instance}' (identity: {name})...")
    start_vm(identity.vm_instance, timeout=args.timeout)

    print("Injecting credentials...")
    inject_credentials(identity.vm_instance, identity)

    claude_dir = Path.home() / ".claude"
    print("Copying Claude Code config...")
    copy_claude_config(identity.vm_instance, claude_dir)
    link_claude_dirs(identity.vm_instance, claude_dir)

    fallback = resolve_vergil_version(config, identity)
    print("Updating vergil-tooling...")
    try_update_tooling(identity.vm_instance, fallback_tag=fallback)

    print(f"VM '{identity.vm_instance}' is running.")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    name, identity, _config = _resolve(args)

    print(f"Stopping VM '{identity.vm_instance}' (identity: {name})...")
    stop_vm(identity.vm_instance)

    print(f"VM '{identity.vm_instance}' stopped.")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    name, identity, _config = _resolve(args)

    print(f"Restarting VM '{identity.vm_instance}' (identity: {name})...")
    stop_vm(identity.vm_instance)
    start_vm(identity.vm_instance)

    print("Injecting credentials...")
    inject_credentials(identity.vm_instance, identity)

    print(f"VM '{identity.vm_instance}' is running.")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    name, identity, config = _resolve(args)

    status = vm_status(identity.vm_instance)
    if status != "Running":
        effective = status or "Not Created"
        print(
            f"ERROR: VM '{identity.vm_instance}' is not running (status: {effective})",
            file=sys.stderr,
        )
        return 1

    tag = args.tag if args.tag else None
    fallback = resolve_vergil_version(config, identity)
    print(f"Updating vergil-tooling in VM '{identity.vm_instance}' (identity: {name})...")

    before = get_tooling_version(identity.vm_instance)
    update_tooling(identity.vm_instance, tag, fallback_tag=fallback)
    after = get_tooling_version(identity.vm_instance)

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
    name, identity, _config = _resolve(args)

    status = vm_status(identity.vm_instance)
    if not status:
        print(
            f"VM '{identity.vm_instance}' does not exist.",
            file=sys.stderr,
        )
        return 1

    print(f"Destroying VM '{identity.vm_instance}' (identity: {name})...")
    delete_vm(identity.vm_instance)

    print(f"VM '{identity.vm_instance}' destroyed.")
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    name, identity, config = _resolve(args)

    status = vm_status(identity.vm_instance)
    if not status:
        print(
            f"ERROR: VM '{identity.vm_instance}' does not exist — run 'vrg-vm create' first",
            file=sys.stderr,
        )
        return 1

    if not identity.projects_dir:
        print(
            f"ERROR: identity '{name}' has no projects_dir configured",
            file=sys.stderr,
        )
        return 1

    vergil_version = resolve_vergil_version(config, identity)
    tag = args.tag if args.tag else resolve_vm_tag(config, identity)

    print(f"Rebuilding VM '{identity.vm_instance}' (identity: {name})...")

    print("  Destroying old VM...")
    delete_vm(identity.vm_instance)

    print(f"  Fetching template ({tag})...")
    template = fetch_template(tag)

    try:
        print(f"  Creating VM with projects mount: {identity.projects_dir}")
        create_vm(
            identity.vm_instance,
            template,
            identity.projects_dir,
            cpus=identity.cpus,
            memory=identity.memory,
            disk=identity.disk,
        )

        print("  Starting VM...")
        start_vm(identity.vm_instance, timeout=args.timeout)

        print("  Injecting credentials...")
        inject_credentials(identity.vm_instance, identity)

        install_tooling(identity.vm_instance, vergil_version)

        claude_dir = Path.home() / ".claude"
        print("  Copying Claude Code config...")
        copy_claude_config(identity.vm_instance, claude_dir)
        link_claude_dirs(identity.vm_instance, claude_dir)
    finally:
        template.unlink(missing_ok=True)

    print(f"\nVM '{identity.vm_instance}' rebuilt and ready.")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)

    if args.sessions:
        return _list_sessions(config)

    vms = list_vms()
    vm_map = {vm["name"]: vm["status"] for vm in vms}

    print(f"{'IDENTITY':<16} {'VM INSTANCE':<24} {'STATUS':<12}")
    print(f"{'─' * 16} {'─' * 24} {'─' * 12}")

    for id_name, identity in config.identities.items():
        status = vm_map.get(identity.vm_instance, "Not Created")
        print(f"{id_name:<16} {identity.vm_instance:<24} {status:<12}")

    return 0


def _vm_active_session_ids(instance: str) -> set[str]:
    """Active session ids reported by a running VM's in-VM resolver."""
    result = shell_run(instance, "vrg-vm-resolve-session", "--list-json")
    rows = json.loads(result.stdout)
    return {row["sessionId"] for row in rows if row.get("active")}


def _list_sessions(config: IdentityConfig) -> int:
    """List named Claude sessions across all identity VMs.

    Transcripts are shared (host-backed), so names come from the host store;
    liveness is queried per running VM, since each VM owns its own roster.
    """
    vm_map = {vm["name"]: vm["status"] for vm in list_vms()}
    active: set[str] = set()
    for identity in config.identities.values():
        if vm_map.get(identity.vm_instance) == "Running":
            active |= _vm_active_session_ids(identity.vm_instance)

    names = name_by_session(Path.home() / ".claude" / "projects")
    rows = list_rows(names, active)

    print(f"{'IDENTITY':<16} {'SLOT':<6} {'WORKSPACE':<36} {'STATE':<8}")
    print(f"{'─' * 16} {'─' * 6} {'─' * 36} {'─' * 8}")
    for row in rows:
        slot = f"{row.slot:02d}"
        state = "active" if row.active else "idle"
        print(f"{row.identity:<16} {slot:<6} {row.path:<36} {state:<8}")

    return 0


def _session_inner(args: argparse.Namespace, identity_name: str, rel_path: str, model: str) -> str:
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
    if extra:
        resolve_cmd += ["--", *extra]
    return f"{source} exec {shlex.join(resolve_cmd)}"


def _cmd_session(args: argparse.Namespace) -> int:
    name, identity, config = _resolve(args)

    if not args.allow_stale_vm:
        age = vm_age_days(identity.vm_instance)
        if age is not None and age > _DEFAULT_STALENESS_DAYS:
            print(
                f"ERROR: VM '{identity.vm_instance}' is {age:.0f} days old"
                f" (threshold: {_DEFAULT_STALENESS_DAYS} days).\n"
                f"Rebuild with: vrg-vm rebuild --identity {name}\n"
                f"Override with: vrg-vm session --allow-stale-vm --identity {name}",
                file=sys.stderr,
            )
            return 1

    fallback = resolve_vergil_version(config, identity)
    try_update_tooling(identity.vm_instance, fallback_tag=fallback)

    claude_dir = Path.home() / ".claude"
    copy_claude_config(identity.vm_instance, claude_dir)
    link_claude_dirs(identity.vm_instance, claude_dir)

    workspace_abs = os.path.normpath(resolve_workspace(args.workspace, identity.projects_dir))
    rel_path = os.path.relpath(workspace_abs, identity.projects_dir)

    os.environ["LIMA_SHELLENV_ALLOW"] = _TERMINAL_ENV_VARS

    cmd = [
        "limactl",
        "shell",
        "--start",
        "--preserve-env",
        f"--workdir={workspace_abs}",
        identity.vm_instance,
        "bash",
        "-c",
        _session_inner(args, name, rel_path, resolve_model(config, identity, args.model)),
    ]
    os.execvp(cmd[0], cmd)  # noqa: S606, S607
    return 0  # unreachable, keeps the type checker happy


def _add_identity_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--identity", help="Identity name (default: default_identity)")
    parser.add_argument("--config", type=Path, help="Path to identities.toml")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-vm",
        description="Manage identity VM lifecycle",
    )
    sub = parser.add_subparsers(dest="command")

    p_create = sub.add_parser("create", help="Create and provision a new VM")
    _add_identity_args(p_create)
    p_create.add_argument(
        "--tag", default="", help="VM template version tag (default: vergil version from config)"
    )

    p_start = sub.add_parser("start", help="Start VM and inject credentials")
    _add_identity_args(p_start)
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

    p_restart = sub.add_parser("restart", help="Restart VM and re-inject credentials")
    _add_identity_args(p_restart)

    p_update = sub.add_parser("update", help="Reinstall vergil-tooling inside a running VM")
    _add_identity_args(p_update)
    p_update.add_argument(
        "--tag", default="", help="Override version tag (default: tag from initial install)"
    )

    p_destroy = sub.add_parser("destroy", help="Destroy VM entirely")
    _add_identity_args(p_destroy)

    p_rebuild = sub.add_parser("rebuild", help="Destroy and recreate VM (stateless rebuild)")
    _add_identity_args(p_rebuild)
    p_rebuild.add_argument(
        "--tag", default="", help="VM template version tag (default: vergil version from config)"
    )
    p_rebuild.add_argument(
        "--timeout",
        default="30m",
        help="How long to wait for VM to reach running status (default: 30m)",
    )

    p_list = sub.add_parser("list", help="List all identity VMs and their status")
    p_list.add_argument("--config", type=Path, help="Path to identities.toml")
    p_list.add_argument(
        "--sessions",
        action="store_true",
        help="List named Claude sessions across identity VMs instead of VMs",
    )

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
