"""Manage identity VM lifecycle."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from vergil_tooling.lib.identity import (
    Identity,
    default_config_path,
    load_config,
    resolve_identity,
    resolve_identity_by_name,
    resolve_workspace,
)
from vergil_tooling.lib.lima import (
    create_vm,
    delete_vm,
    fetch_template,
    inject_credentials,
    install_tooling,
    list_vms,
    start_vm,
    stop_vm,
    vm_status,
)

_DEFAULT_TAG = "v2.0"

_default_config_path = default_config_path


def _resolve(args: argparse.Namespace) -> tuple[str, Identity]:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    name, identity = resolve_identity_by_name(config, args.identity)
    return name, identity


def _cmd_create(args: argparse.Namespace) -> int:
    name, identity = _resolve(args)
    tag = args.tag

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
        create_vm(identity.vm_instance, template, identity.projects_dir)

        print("  Starting VM...")
        start_vm(identity.vm_instance)

        print("Injecting credentials...")
        inject_credentials(identity.vm_instance, identity)

        install_tooling(identity.vm_instance, tag)
    finally:
        template.unlink(missing_ok=True)

    print(f"\nVM '{identity.vm_instance}' is ready.")
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    name, identity = _resolve(args)

    status = vm_status(identity.vm_instance)
    if not status:
        print(
            f"ERROR: VM '{identity.vm_instance}' does not exist — run 'vrg-vm create' first",
            file=sys.stderr,
        )
        return 1

    print(f"Starting VM '{identity.vm_instance}' (identity: {name})...")
    start_vm(identity.vm_instance)

    print("Injecting credentials...")
    inject_credentials(identity.vm_instance, identity)

    print(f"VM '{identity.vm_instance}' is running.")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    name, identity = _resolve(args)

    print(f"Stopping VM '{identity.vm_instance}' (identity: {name})...")
    stop_vm(identity.vm_instance)

    print(f"VM '{identity.vm_instance}' stopped.")
    return 0


def _cmd_restart(args: argparse.Namespace) -> int:
    name, identity = _resolve(args)

    print(f"Restarting VM '{identity.vm_instance}' (identity: {name})...")
    stop_vm(identity.vm_instance)
    start_vm(identity.vm_instance)

    print("Injecting credentials...")
    inject_credentials(identity.vm_instance, identity)

    print(f"VM '{identity.vm_instance}' is running.")
    return 0


def _cmd_destroy(args: argparse.Namespace) -> int:
    name, identity = _resolve(args)

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


def _cmd_list(args: argparse.Namespace) -> int:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)

    vms = list_vms()
    vm_map = {vm["name"]: vm["status"] for vm in vms}

    print(f"{'IDENTITY':<16} {'VM INSTANCE':<24} {'STATUS':<12}")
    print(f"{'─' * 16} {'─' * 24} {'─' * 12}")

    for id_name, identity in config.identities.items():
        status = vm_map.get(identity.vm_instance, "Not Created")
        print(f"{id_name:<16} {identity.vm_instance:<24} {status:<12}")

    return 0


def _cmd_session(args: argparse.Namespace) -> int:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    identity = resolve_identity(config, args.identity)

    workspace: str | None = None
    if args.workspace:
        workspace = resolve_workspace(args.workspace)

    cmd = ["limactl", "shell", "--start", identity.vm_instance]

    if workspace:
        cmd.extend(["--workdir", workspace])

    if args.cmd:
        cmd.append("--")
        cmd.extend(args.cmd)

    os.execvp(cmd[0], cmd)  # noqa: S606, S607
    return 0  # unreachable, keeps mypy happy


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
        "--tag", default=_DEFAULT_TAG, help="Template version tag (default: v2.0)"
    )

    p_start = sub.add_parser("start", help="Start VM and inject credentials")
    _add_identity_args(p_start)

    p_stop = sub.add_parser("stop", help="Stop VM")
    _add_identity_args(p_stop)

    p_restart = sub.add_parser("restart", help="Restart VM and re-inject credentials")
    _add_identity_args(p_restart)

    p_destroy = sub.add_parser("destroy", help="Destroy VM entirely")
    _add_identity_args(p_destroy)

    p_list = sub.add_parser("list", help="List all identity VMs and their status")
    p_list.add_argument("--config", type=Path, help="Path to identities.toml")

    p_session = sub.add_parser("session", help="Shell into a VM")
    _add_identity_args(p_session)
    p_session.add_argument(
        "workspace", nargs="?", help="Workspace path (relative to /projects or absolute)"
    )
    p_session.add_argument("cmd", nargs=argparse.REMAINDER, help="Command to run inside the VM")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1

    dispatch = {
        "create": _cmd_create,
        "start": _cmd_start,
        "stop": _cmd_stop,
        "restart": _cmd_restart,
        "destroy": _cmd_destroy,
        "list": _cmd_list,
        "session": _cmd_session,
    }
    return dispatch[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
