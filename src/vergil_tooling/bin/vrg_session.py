"""Launch a Claude Code session inside an identity VM."""

from __future__ import annotations

import argparse
import os
import sys
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
        "workspace",
        nargs="?",
        help="Workspace path (relative to /projects, or absolute)",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Open a shell instead of launching Claude Code",
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
        "claude_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to Claude Code (after --)",
    )

    args = parser.parse_args(argv)

    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    identity = resolve_identity(config, args.identity)

    workspace: str | None = None
    if args.workspace:
        workspace = resolve_workspace(args.workspace)

    if not args.workspace and not args.identity and not args.shell:
        print("ERROR: provide a workspace path or --identity", file=sys.stderr)
        raise SystemExit(1)

    cmd = build_command(
        vm_instance=identity.vm_instance,
        workspace=workspace,
        shell_only=args.shell,
        claude_args=args.claude_args or None,
    )

    os.execvp(cmd[0], cmd)  # noqa: S606, S607
