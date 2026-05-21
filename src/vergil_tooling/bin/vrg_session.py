"""Launch a Claude Code session inside an identity VM."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from vergil_tooling.lib.identity import (
    default_config_path,
    load_config,
    resolve_project,
)

_default_config_path = default_config_path


def build_command(
    *,
    vm_instance: str,
    workspace: str | None,
    shell_only: bool,
    claude_args: list[str] | None = None,
) -> list[str]:
    cmd = ["limactl", "shell", "--start", vm_instance]

    if workspace:
        cmd.extend(["--workdir", workspace])

    if not shell_only:
        cmd.append("--")
        cmd.append("claude")
        if claude_args:
            cmd.extend(claude_args)

    return cmd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="vrg-session",
        description="Launch a Claude Code session inside an identity VM",
    )
    parser.add_argument(
        "project",
        nargs="?",
        help="Project short name (from identities.toml workspaces)",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Open a shell instead of launching Claude Code",
    )
    parser.add_argument(
        "--identity",
        help="Identity name (default: resolved from project)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to identities.toml",
    )
    parser.add_argument(
        "claude_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to Claude Code (after --)",
    )

    args = parser.parse_args(argv)

    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)

    if args.project:
        identity, workspace = resolve_project(config, args.project)
    elif args.identity:
        if args.identity not in config.identities:
            print(f"ERROR: identity '{args.identity}' not found", file=sys.stderr)
            raise SystemExit(1)
        identity = config.identities[args.identity]
        workspace = None
    else:
        print("ERROR: provide a project name or --identity", file=sys.stderr)
        raise SystemExit(1)

    cmd = build_command(
        vm_instance=identity.vm_instance,
        workspace=workspace,
        shell_only=args.shell or not args.project,
        claude_args=args.claude_args or None,
    )

    os.execvp(cmd[0], cmd)  # noqa: S606, S607
