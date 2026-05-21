"""Launch a Claude Code session inside an identity VM."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
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
    api_key: str,
    shell_only: bool,
) -> list[str]:
    cmd = ["limactl", "shell", vm_instance, "--"]
    env_prefix = f"ANTHROPIC_API_KEY={api_key}"
    safe_workspace = shlex.quote(workspace) if workspace else None

    if safe_workspace and not shell_only:
        cmd.extend(
            [
                "env",
                env_prefix,
                "bash",
                "-lc",
                f"cd {safe_workspace} && claude",
            ]
        )
    elif safe_workspace:
        cmd.extend(
            [
                "env",
                env_prefix,
                "bash",
                "-lc",
                f"cd {safe_workspace} && exec zsh",
            ]
        )
    else:
        cmd.extend(["env", env_prefix, "zsh", "-l"])

    return cmd


def check_vm_running(instance: str) -> bool:
    result = subprocess.run(
        ["limactl", "list", "--json"],  # noqa: S603, S607
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    vms = json.loads(result.stdout)
    return any(vm["name"] == instance and vm["status"] == "Running" for vm in vms)


def ensure_vm_running(instance: str) -> None:
    if check_vm_running(instance):
        return
    print(f"Starting VM '{instance}'...")
    subprocess.run(  # noqa: S603
        ["limactl", "start", instance],  # noqa: S607
        check=True,
    )


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

    args = parser.parse_args(argv)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in environment", file=sys.stderr)
        raise SystemExit(1)

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

    ensure_vm_running(identity.vm_instance)

    cmd = build_command(
        vm_instance=identity.vm_instance,
        workspace=workspace,
        api_key=api_key,
        shell_only=args.shell or not args.project,
    )

    os.execvp(cmd[0], cmd)  # noqa: S606, S607
