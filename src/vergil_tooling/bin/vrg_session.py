"""Launch a command inside an identity VM."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from vergil_tooling.lib.identity import (
    default_config_path,
    load_config,
    resolve_identity,
    resolve_workspace,
)

_default_config_path = default_config_path


def build_command(
    *,
    vm_instance: str,
    workspace: str | None,
    command: list[str] | None = None,
) -> list[str]:
    cmd = ["limactl", "shell", "--start", vm_instance]

    if workspace:
        cmd.extend(["--workdir", workspace])

    if command:
        cmd.append("--")
        cmd.extend(command)

    return cmd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="vrg-session",
        description="Launch a command inside an identity VM",
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        help="Workspace path (relative to /projects, or absolute)",
    )
    parser.add_argument(
        "--identity",
        help="Identity name (default: sole configured identity)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to identities.toml",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run inside the VM",
    )

    args = parser.parse_args(argv)

    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    identity = resolve_identity(config, args.identity)

    workspace: str | None = None
    if args.workspace:
        workspace = resolve_workspace(args.workspace)

    cmd = build_command(
        vm_instance=identity.vm_instance,
        workspace=workspace,
        command=args.command or None,
    )

    os.execvp(cmd[0], cmd)  # noqa: S606, S607
