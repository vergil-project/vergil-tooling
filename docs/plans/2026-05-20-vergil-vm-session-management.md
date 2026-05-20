# vergil-vm: Session Management — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a `vrg-session` CLI command in vergil-tooling that
launches Claude Code inside an identity VM via Lima, with
ANTHROPIC_API_KEY forwarding and workspace directory resolution.

**Architecture:** A new `vrg-session` console script in
vergil-tooling reads identity configuration from
`~/.config/vergil/identities.toml`, resolves the target project
to a workspace path inside the VM, and launches an interactive
SSH session via `limactl shell`. The API key is passed per-session
as an environment variable — it is the only credential that
crosses the VM boundary at launch time.

**Tech Stack:** Python (vergil-tooling), TOML configuration, Lima
CLI (`limactl shell`)

**Specs:**
- `docs/specs/2026-05-20-identity-vm-isolation-design.md` (#892)

**Decomposition:** This is Plan 2 of 6 for the identity VM
isolation system.

| Plan | Scope | Status |
|---|---|---|
| 1. Repository + Working VM | vergil-vm repo, Lima template | Complete |
| **2. Session Management** (this plan) | vrg-session, identities.toml | This plan |
| 3. Credential Provisioning | GitHub PAT/SSH key injection | Planned |
| 4. Egress Filtering | HAProxy, pf, iptables | Planned |
| 5. vergil-tooling Adaptations | nerdctl, wrapper simplification | Planned |
| 6. Distribution + Updates | Pre-built images, vrg-vm-update | Planned |

**Repository:** vergil-tooling

**Depends on:** Plan 1 (working VM exists and boots successfully)

---

## Design

### The `vrg-session` Command

```bash
# Launch Claude Code on vergil-tooling in the Vergil identity VM
vrg-session vergil-tooling

# Launch a plain shell (no Claude Code)
vrg-session --shell vergil-tooling

# Ad-hoc SSH into the VM (no project context)
vrg-session --shell
```

Under the hood:

```bash
limactl shell vergil-agent -- \
  env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  bash -lc 'cd ~/dev/projects/vergil-project/vergil-tooling && claude'
```

### Identity Configuration

```toml
# ~/.config/vergil/identities.toml

[identities.vergil]
vm_instance = "vergil-agent"
github_user = "wphillipmoore-vergil"

# Project workspace mappings — maps short names to paths
# inside the VM. Paths mirror host paths under the mount.
[identities.vergil.workspaces]
vergil-tooling = "~/dev/projects/vergil-project/vergil-tooling"
vergil-actions = "~/dev/projects/vergil-project/vergil-actions"
vergil-docker  = "~/dev/projects/vergil-project/vergil-docker"
vergil-vm      = "~/dev/projects/vergil-project/vergil-vm"
diogenes-core  = "~/dev/projects/diogenes-project/diogenes-core"

# Future: second identity
# [identities.mimir]
# vm_instance = "mimir-agent"
# github_user = "wphillipmoore-mimir"
```

### Session Flow

1. Read `identities.toml`
2. Resolve the project name to an identity and workspace path
3. Verify the VM is running (`limactl list --json`)
4. If not running, start it (`limactl start`)
5. Launch `limactl shell` with env vars and the workspace `cd`

### ANTHROPIC_API_KEY Handling

The API key is read from the host environment at session launch
time. It is passed to the VM process as an environment variable
via `limactl shell -- env`. It is NOT stored inside the VM, NOT
written to disk inside the VM, and NOT baked into any
configuration. Each session gets it fresh from the host.

If `ANTHROPIC_API_KEY` is not set in the host environment,
`vrg-session` exits with a clear error message.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/bin/vrg_session.py` | Create | CLI entry point |
| `src/vergil_tooling/lib/identity.py` | Create | Identity config parser |
| `tests/vergil_tooling/test_vrg_session.py` | Create | CLI tests |
| `tests/vergil_tooling/test_identity.py` | Create | Config parser tests |
| `pyproject.toml` | Modify | Add `vrg-session` console script entry |

---

### Task 1: Identity Configuration Parser

**Files:**
- Create: `src/vergil_tooling/lib/identity.py`
- Create: `tests/vergil_tooling/test_identity.py`

- [ ] **Step 1: Write the failing tests for config loading**

```python
# tests/vergil_tooling/test_identity.py
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from vergil_tooling.lib.identity import (
    Identity,
    IdentityConfig,
    load_config,
    resolve_project,
)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        github_user = "wphillipmoore-vergil"

        [identities.vergil.workspaces]
        vergil-tooling = "~/dev/projects/vergil-project/vergil-tooling"
        diogenes-core = "~/dev/projects/diogenes-project/diogenes-core"
    """))
    return p


def test_load_config(config_file: Path) -> None:
    cfg = load_config(config_file)
    assert isinstance(cfg, IdentityConfig)
    assert "vergil" in cfg.identities


def test_identity_fields(config_file: Path) -> None:
    cfg = load_config(config_file)
    ident = cfg.identities["vergil"]
    assert isinstance(ident, Identity)
    assert ident.vm_instance == "vergil-agent"
    assert ident.github_user == "wphillipmoore-vergil"
    assert ident.workspaces["vergil-tooling"] == "~/dev/projects/vergil-project/vergil-tooling"


def test_resolve_project_found(config_file: Path) -> None:
    cfg = load_config(config_file)
    ident, workspace = resolve_project(cfg, "vergil-tooling")
    assert ident.vm_instance == "vergil-agent"
    assert workspace == "~/dev/projects/vergil-project/vergil-tooling"


def test_resolve_project_not_found(config_file: Path) -> None:
    cfg = load_config(config_file)
    with pytest.raises(SystemExit):
        resolve_project(cfg, "nonexistent-project")


def test_resolve_project_ambiguous(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        github_user = "user-vergil"
        [identities.vergil.workspaces]
        shared-repo = "~/dev/shared-repo"

        [identities.mimir]
        vm_instance = "mimir-agent"
        github_user = "user-mimir"
        [identities.mimir.workspaces]
        shared-repo = "~/dev/shared-repo"
    """))
    cfg = load_config(p)
    with pytest.raises(SystemExit):
        resolve_project(cfg, "shared-repo")


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_config(tmp_path / "nonexistent.toml")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_identity.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement the identity config parser**

```python
# src/vergil_tooling/lib/identity.py
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


@dataclass
class Identity:
    vm_instance: str
    github_user: str
    workspaces: dict[str, str] = field(default_factory=dict)


@dataclass
class IdentityConfig:
    identities: dict[str, Identity] = field(default_factory=dict)


def load_config(path: Path) -> IdentityConfig:
    if not path.exists():
        print(f"ERROR: identity config not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    identities: dict[str, Identity] = {}
    for name, data in raw.get("identities", {}).items():
        identities[name] = Identity(
            vm_instance=data["vm_instance"],
            github_user=data["github_user"],
            workspaces=data.get("workspaces", {}),
        )
    return IdentityConfig(identities=identities)


def default_config_path() -> Path:
    return Path.home() / ".config" / "vergil" / "identities.toml"


def resolve_project(
    config: IdentityConfig, project: str
) -> tuple[Identity, str]:
    matches: list[tuple[Identity, str]] = []
    for ident in config.identities.values():
        if project in ident.workspaces:
            matches.append((ident, ident.workspaces[project]))

    if not matches:
        print(
            f"ERROR: project '{project}' not found in any identity",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if len(matches) > 1:
        print(
            f"ERROR: project '{project}' found in multiple identities"
            " — ambiguous",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return matches[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_identity.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope session \
  --message "identity config parser for VM session management" \
  --body "Loads ~/.config/vergil/identities.toml, resolves project names to identity VMs and workspace paths"
```

---

### Task 2: vrg-session CLI

**Files:**
- Create: `src/vergil_tooling/bin/vrg_session.py`
- Create: `tests/vergil_tooling/test_vrg_session.py`
- Modify: `pyproject.toml` (add entry point)

- [ ] **Step 1: Write the failing tests**

```python
# tests/vergil_tooling/test_vrg_session.py
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_session import build_command, main


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / ".config" / "vergil"
    cfg.mkdir(parents=True)
    (cfg / "identities.toml").write_text(textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        github_user = "wphillipmoore-vergil"

        [identities.vergil.workspaces]
        vergil-tooling = "~/dev/projects/vergil-project/vergil-tooling"
    """))
    return tmp_path


def test_build_command_claude_session() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="~/dev/projects/vergil-project/vergil-tooling",
        api_key="sk-test-key",
        shell_only=False,
    )
    assert cmd[0] == "limactl"
    assert cmd[1] == "shell"
    assert cmd[2] == "vergil-agent"
    assert "ANTHROPIC_API_KEY=sk-test-key" in " ".join(cmd)
    assert "claude" in " ".join(cmd)


def test_build_command_shell_only() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="~/dev/projects/vergil-project/vergil-tooling",
        api_key="sk-test-key",
        shell_only=True,
    )
    assert "claude" not in " ".join(cmd)
    assert "cd" in " ".join(cmd)


def test_build_command_no_workspace() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace=None,
        api_key="sk-test-key",
        shell_only=True,
    )
    assert "cd" not in " ".join(cmd)


def test_missing_api_key(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "vergil_tooling.lib.identity.default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    with pytest.raises(SystemExit):
        main(["vergil-tooling"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_session.py -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement vrg-session**

```python
# src/vergil_tooling/bin/vrg_session.py
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from vergil_tooling.lib.identity import (
    default_config_path,
    load_config,
    resolve_project,
)


def build_command(
    *,
    vm_instance: str,
    workspace: str | None,
    api_key: str,
    shell_only: bool,
) -> list[str]:
    cmd = ["limactl", "shell", vm_instance, "--"]

    env_prefix = f"ANTHROPIC_API_KEY={api_key}"

    if workspace and not shell_only:
        cmd.extend([
            "env", env_prefix,
            "bash", "-lc",
            f"cd {workspace} && claude",
        ])
    elif workspace:
        cmd.extend([
            "env", env_prefix,
            "bash", "-lc",
            f"cd {workspace} && exec zsh",
        ])
    else:
        cmd.extend(["env", env_prefix, "zsh", "-l"])

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
        type=str,
        help="Path to identities.toml",
    )

    args = parser.parse_args(argv)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "ERROR: ANTHROPIC_API_KEY not set in environment",
            file=sys.stderr,
        )
        raise SystemExit(1)

    config_path = (
        args.config
        if args.config
        else default_config_path()
    )
    config = load_config(config_path)

    if args.project:
        identity, workspace = resolve_project(config, args.project)
    elif args.identity:
        if args.identity not in config.identities:
            print(
                f"ERROR: identity '{args.identity}' not found",
                file=sys.stderr,
            )
            raise SystemExit(1)
        identity = config.identities[args.identity]
        workspace = None
    else:
        print(
            "ERROR: provide a project name or --identity",
            file=sys.stderr,
        )
        raise SystemExit(1)

    cmd = build_command(
        vm_instance=identity.vm_instance,
        workspace=workspace,
        api_key=api_key,
        shell_only=args.shell or not args.project,
    )

    result = subprocess.call(cmd)  # noqa: S603
    raise SystemExit(result)
```

- [ ] **Step 4: Add entry point to pyproject.toml**

Add to the `[project.scripts]` section:

```toml
vrg-session = "vergil_tooling.bin.vrg_session:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_session.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
vrg-commit --type feat --scope session \
  --message "vrg-session CLI for launching Claude Code in identity VMs" \
  --body "SSH into identity VM, forward ANTHROPIC_API_KEY, cd to workspace, launch claude"
```

---

### Task 3: VM Readiness Check

Before launching a session, `vrg-session` should verify the VM is
running and start it if needed.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_session.py`
- Modify: `tests/vergil_tooling/test_vrg_session.py`

- [ ] **Step 1: Write the failing test for VM readiness**

```python
# Add to tests/vergil_tooling/test_vrg_session.py

def test_check_vm_running_true(monkeypatch: pytest.MonkeyPatch) -> None:
    from vergil_tooling.bin.vrg_session import check_vm_running

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        result = subprocess.CompletedProcess(args=[], returncode=0)
        result.stdout = json.dumps([
            {"name": "vergil-agent", "status": "Running"},
        ])
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert check_vm_running("vergil-agent") is True


def test_check_vm_running_false(monkeypatch: pytest.MonkeyPatch) -> None:
    from vergil_tooling.bin.vrg_session import check_vm_running

    def fake_run(*args, **kwargs):  # noqa: ANN002, ANN003, ARG001
        result = subprocess.CompletedProcess(args=[], returncode=0)
        result.stdout = json.dumps([
            {"name": "vergil-agent", "status": "Stopped"},
        ])
        return result

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert check_vm_running("vergil-agent") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_session.py::test_check_vm_running_true -v`
Expected: FAIL

- [ ] **Step 3: Implement check_vm_running and auto-start**

Add to `vrg_session.py`:

```python
def check_vm_running(instance: str) -> bool:
    result = subprocess.run(  # noqa: S603, S607
        ["limactl", "list", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    vms = json.loads(result.stdout)
    return any(
        vm["name"] == instance and vm["status"] == "Running"
        for vm in vms
    )


def ensure_vm_running(instance: str) -> None:
    if check_vm_running(instance):
        return
    print(f"Starting VM '{instance}'...")
    subprocess.run(  # noqa: S603, S607
        ["limactl", "start", instance],
        check=True,
    )
```

Call `ensure_vm_running(identity.vm_instance)` in `main()` before
building the command.

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_session.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope session \
  --message "auto-start VM if not running" \
  --body "vrg-session checks limactl list and starts the VM if needed"
```

---

### Task 4: Full Validation

Run the full test suite and validate manually.

- [ ] **Step 1: Run full validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

- [ ] **Step 2: Manual validation (requires working VM from Plan 1)**

```bash
# On the host, with ANTHROPIC_API_KEY set:
vrg-session vergil-tooling
# Should SSH into the VM, cd to the project, launch claude

vrg-session --shell vergil-tooling
# Should open a zsh shell in the project directory

vrg-session --shell --identity vergil
# Should open a zsh shell in the VM home directory
```

- [ ] **Step 3: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** `vrg-session` command, identities.toml,
  API key forwarding, workspace resolution — all covered.
- [x] **Placeholder scan:** No TBD, TODO, or "implement later."
- [x] **Type consistency:** Function signatures, config field
  names, and CLI argument names are consistent across all tasks.
- [x] **Scope boundaries:** This plan does NOT include credential
  provisioning (Plan 3), egress filtering (Plan 4), or wrapper
  simplification (Plan 5).
