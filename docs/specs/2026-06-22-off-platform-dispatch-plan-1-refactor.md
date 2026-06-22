# Off-Platform Dispatch — Plan 1: Backend + Transport Refactor

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a `Backend` + `Transport` abstraction and route `vrg-vm` through a dispatched backend, with the Lima path's behavior **byte-for-byte unchanged** — the de-risking foundation the cloud backend (Plan 2) builds on.

**Architecture:** Extract the "run a command in the guest" seam into a `Transport` protocol (`LimaTransport` wraps `limactl shell`). Move the guest-side helpers (credential injection, tooling install, claude-config, probes) out of `lima.py` into `vm_guest.py`, reshaped to take a `Transport` instead of an instance name. Wrap the `limactl` lifecycle in a `LimaBackend` behind a `Backend` protocol, selected by `select_backend(spec)`. `vrg_vm.py` calls `backend`/transport methods instead of bare `lima` functions.

**Tech Stack:** Python 3.12+, `argparse`, `subprocess`, `unittest.mock`, `pytest`. No new runtime dependencies.

## Global Constraints

- **Zero behavior change on the Lima path.** This is a pure refactor. The existing `tests/vergil_tooling/test_lima.py` and `test_vrg_vm.py` are the regression gate; they must stay green (updated only for moved import paths / call shapes, never for changed behavior).
- **100% test coverage** is enforced by `vrg-validate`. Every new line needs a test.
- **No raw `git`/`gh`.** Use `vrg-git` and `vrg-commit` (`--type/--scope/--message/--body`). Raw `git`/`gh` are denied.
- **Validation is one command:** `vrg-container-run -- vrg-validate` (transparently expands to `uv run vrg-validate` here via the `[validation]` override). Do not run individual linters.
- **Portability:** code must work on macOS and Linux; bash must pass shellcheck (no new bash here).
- **Work location:** worktree `.worktrees/issue-1706-off-platform-dispatch`, branch `feature/1706-off-platform-dispatch`. All edits and commands run from inside the worktree (absolute paths or `cd` first).
- **Commit style:** conventional commits via `vrg-commit`; scope `off-platform` (or `vm`); reference `#1706` in the body.

---

## File Structure

- **Create** `src/vergil_tooling/lib/vm_transport.py` — `Transport` protocol; `LimaTransport`.
- **Create** `src/vergil_tooling/lib/vm_guest.py` — guest-side helpers, each taking a `Transport`: `inject_credentials`, `install_tooling`, `update_tooling`, `get_tooling_version`, `copy_claude_config`, `link_claude_dirs`, `try_update_tooling`, `vm_probe`, `read_fingerprint`, `vm_spec_status`, plus their private helpers.
- **Create** `src/vergil_tooling/lib/vm_backend.py` — `Backend` protocol; `select_backend(spec)`.
- **Create** `src/vergil_tooling/lib/vm_lima.py` — `LimaBackend` (limactl lifecycle: create/start/stop/destroy/list/status), wraps `LimaTransport`.
- **Modify** `src/vergil_tooling/lib/lima.py` — keep only the `limactl` primitives (`_limactl`, `_limactl_stream`, `shell_run`, `shell_pipe`, `vm_status`, `list_vms`, `fetch_template`, `create_vm`, `start_vm`, `stop_vm`, `delete_vm`, `vm_age_days`, the nested-virt + serial-log helpers). Guest helpers move out.
- **Modify** `src/vergil_tooling/bin/vrg_vm.py` — import guest helpers from `vm_guest`, drive lifecycle via `select_backend(...)`/`LimaBackend`, pass `backend.transport(target)` to guest stages.
- **Create tests** `tests/vergil_tooling/test_vm_transport.py`, `test_vm_guest.py`, `test_vm_backend.py`.
- **Modify tests** `tests/vergil_tooling/test_lima.py` (drop tests for moved fns; keep primitives), `test_vrg_vm.py` (update import/patch paths).

**Transformation rule for moved guest helpers (applied throughout Tasks 2–3):**
A function `def f(instance: str, ...)` that calls `shell_run(instance, *args, workdir=w)` / `shell_pipe(instance, cmd, data, workdir=w)` becomes `def f(transport: Transport, ...)` calling `transport.run(*args, workdir=w)` / `transport.pipe(cmd, data, workdir=w)`. No other logic changes. Callers in `vrg_vm.py` pass a `LimaTransport(instance)`.

---

## Task 1: `Transport` protocol + `LimaTransport`

**Files:**
- Create: `src/vergil_tooling/lib/vm_transport.py`
- Test: `tests/vergil_tooling/test_vm_transport.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Transport(Protocol)` with `run(self, *args: str, workdir: str = "/tmp") -> subprocess.CompletedProcess[str]`, `pipe(self, cmd: str, input_data: str, *, workdir: str = "/tmp") -> None`, `exec_session(self, workdir: str, inner: str) -> NoReturn`.
  - `class LimaTransport` with `__init__(self, instance: str)` implementing `Transport`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_transport.py
import subprocess
from unittest.mock import MagicMock, patch

from vergil_tooling.lib.vm_transport import LimaTransport


class TestLimaTransport:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_constructs_limactl_shell(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        t = LimaTransport("vm-x")
        result = t.run("echo", "hi", workdir="/work")
        assert result.stdout == "ok"
        args = mock_run.call_args[0][0]
        assert args == ["limactl", "shell", "--workdir", "/work", "vm-x", "--", "echo", "hi"]

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_sends_input(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        LimaTransport("vm-x").pipe("cat > f", "payload", workdir="/work")
        assert mock_run.call_args[1]["input"] == "payload"
        args = mock_run.call_args[0][0]
        assert args == ["limactl", "shell", "--workdir", "/work", "vm-x", "--", "bash", "-c", "cat > f"]

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_default_workdir_is_tmp(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        LimaTransport("vm-x").run("ls")
        assert mock_run.call_args[0][0][:5] == ["limactl", "shell", "--workdir", "/tmp", "vm-x"]

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_execs_limactl_start(self, mock_execvp: MagicMock) -> None:
        LimaTransport("vm-x").exec_session("/work", "exec bash")
        cmd = mock_execvp.call_args[0][1]
        assert cmd[:4] == ["limactl", "shell", "--start", "--preserve-env"]
        assert "--workdir=/work" in cmd
        assert cmd[-3:] == ["bash", "-c", "exec bash"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .worktrees/issue-1706-off-platform-dispatch && uv run --project . pytest tests/vergil_tooling/test_vm_transport.py -v`
Expected: FAIL with `ModuleNotFoundError: vergil_tooling.lib.vm_transport`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/vergil_tooling/lib/vm_transport.py
"""Run-a-command-in-the-guest transport seam (limactl today; gcloud/IAP in Plan 2)."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING, NoReturn, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass

_DEFAULT_WORKDIR = "/tmp"  # noqa: S108
_TERMINAL_ENV_VARS = "COLORTERM,TERM_PROGRAM,TERM_PROGRAM_VERSION"


@runtime_checkable
class Transport(Protocol):
    """Execute commands inside a guest, regardless of how we reach it."""

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR
    ) -> subprocess.CompletedProcess[str]: ...

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None: ...

    def exec_session(self, workdir: str, inner: str) -> NoReturn: ...


class LimaTransport:
    """Transport over ``limactl shell`` for a local Lima instance."""

    def __init__(self, instance: str) -> None:
        self.instance = instance

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                ["limactl", "shell", "--workdir", workdir, self.instance, "--", *args],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None:
        try:
            subprocess.run(  # noqa: S603
                ["limactl", "shell", "--workdir", workdir, self.instance, "--", "bash", "-c", cmd],  # noqa: S607
                check=True,
                input=input_data,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def exec_session(self, workdir: str, inner: str) -> NoReturn:
        os.environ["LIMA_SHELLENV_ALLOW"] = _TERMINAL_ENV_VARS
        cmd = [
            "limactl",
            "shell",
            "--start",
            "--preserve-env",
            f"--workdir={workdir}",
            self.instance,
            "bash",
            "-c",
            inner,
        ]
        os.execvp(cmd[0], cmd)  # noqa: S606, S607
        raise AssertionError("unreachable")  # pragma: no cover
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_transport.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_transport.py tests/vergil_tooling/test_vm_transport.py
vrg-commit --type feat --scope off-platform --message "add Transport protocol + LimaTransport (#1706)" --body "First seam of the backend refactor: a Transport that runs commands in the guest. LimaTransport wraps limactl shell, preserving the exact command shape of the existing shell_run/shell_pipe/session exec.\n\nRef #1706"
```

---

## Task 2: Move guest credential/tooling helpers to `vm_guest.py`

This task moves the credential + tooling functions from `lima.py` to `vm_guest.py`, reshaped to take a `Transport`. The behavior is identical; only the first parameter changes (`instance: str` → `transport: Transport`) and the internal `shell_run(instance, ...)`/`shell_pipe(instance, ...)` calls become `transport.run(...)`/`transport.pipe(...)`.

**Files:**
- Create: `src/vergil_tooling/lib/vm_guest.py`
- Modify: `src/vergil_tooling/lib/lima.py` (remove the moved functions)
- Test: `tests/vergil_tooling/test_vm_guest.py`
- Modify: `tests/vergil_tooling/test_lima.py` (remove tests for the moved functions)

**Functions to move (exhaustive), applying the transformation rule:**
`inject_credentials`, `_inject_host_git_identity`, `_read_host_git_config`, `_inject_identity_mode`, `_inject_claude_token`, `install_tooling`, `update_tooling`, `try_update_tooling`, `get_tooling_version`, `_uv_tool_install`, `copy_claude_config`, `link_claude_dirs`, plus module constants they use (`_TOOLING_TAG_FILE`, `_TOOLING_INSTALL`, `_BASHRC_MODE_LINE`, `_BASHRC_SOURCE_LINE`). `_read_host_git_config` reads the **host** git config (not the guest), so it keeps its plain `subprocess.run` and gains no `Transport` param; `_inject_host_git_identity(transport)` calls it and then `transport.run(...)`.

**Interfaces:**
- Consumes: `Transport` (Task 1); `Identity` (from `lib.identity`).
- Produces (new signatures, used by `vrg_vm.py` and Plan 2):
  - `inject_credentials(transport: Transport, identity: Identity) -> None`
  - `install_tooling(transport: Transport, tag: str) -> None`
  - `update_tooling(transport: Transport, tag: str | None = None, *, fallback_tag: str = "") -> None`
  - `try_update_tooling(transport: Transport, *, fallback_tag: str = "") -> None`
  - `get_tooling_version(transport: Transport) -> str | None`
  - `copy_claude_config(transport: Transport, claude_dir: Path) -> None`
  - `link_claude_dirs(transport: Transport, claude_dir: Path) -> None`

- [ ] **Step 1: Write the failing test for the reshaped surface**

```python
# tests/vergil_tooling/test_vm_guest.py
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from vergil_tooling.lib.vm_guest import get_tooling_version, install_tooling


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class TestGuestUsesTransport:
    def test_install_tooling_calls_transport_run(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _ok()
        install_tooling(transport, "v2.1")
        # The install spec must carry the tag and go through transport.run, not limactl.
        joined = " ".join(c for call in transport.run.call_args_list for c in call.args)
        assert "vergil-tooling" in joined
        assert "v2.1" in joined

    def test_get_tooling_version_parses_uv_tool_list(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _ok("vergil-tooling v2.1.3\nother 1.0\n")
        assert get_tooling_version(transport) == "v2.1.3"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_guest.py -v`
Expected: FAIL with `ModuleNotFoundError: vergil_tooling.lib.vm_guest`.

- [ ] **Step 3: Create `vm_guest.py` by moving the functions**

Cut the listed functions/constants from `lima.py` into a new `vm_guest.py`. Apply the transformation rule. Example — the full reshaped `install_tooling` + `_uv_tool_install` + `get_tooling_version` (the rest follow the identical pattern):

```python
# src/vergil_tooling/lib/vm_guest.py (excerpt — apply the same rule to every moved fn)
"""Guest-side provisioning steps, transport-agnostic (limactl or IAP)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vergil_tooling.lib.identity import Identity
    from vergil_tooling.lib.vm_transport import Transport

_TOOLING_INSTALL = "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@{tag}"
_TOOLING_TAG_FILE = "~/.config/vergil/tooling-tag"


def _uv_tool_install(transport: Transport, install_spec: str, *, reinstall: bool) -> None:
    flag = "--reinstall " if reinstall else ""
    install_cmd = f'export PATH="$HOME/.local/bin:$PATH" && uv tool install {flag}"{install_spec}"'
    retry_cmd = (
        'export PATH="$HOME/.local/bin:$PATH" && '
        f'uv tool install --force --reinstall "{install_spec}"'
    )
    try:
        transport.run("bash", "-c", install_cmd)
        return
    except subprocess.CalledProcessError:
        print(
            "  uv tool install failed — clearing the VM uv cache and retrying once...",
            file=sys.stderr,
        )
        transport.run("bash", "-c", 'export PATH="$HOME/.local/bin:$PATH" && uv cache clean')
        transport.run("bash", "-c", retry_cmd)


def install_tooling(transport: Transport, tag: str) -> None:
    install_spec = _TOOLING_INSTALL.format(tag=tag)
    print(f"  Installing vergil-tooling ({tag})...")
    _uv_tool_install(transport, install_spec, reinstall=False)
    transport.run("bash", "-c", f"mkdir -p $(dirname {_TOOLING_TAG_FILE})")
    transport.pipe(f"cat > {_TOOLING_TAG_FILE}", f"{tag}\n")


def get_tooling_version(transport: Transport) -> str | None:
    try:
        result = transport.run(
            "bash", "-c", 'export PATH="$HOME/.local/bin:$PATH" && uv tool list 2>/dev/null'
        )
        for line in result.stdout.splitlines():
            if line.startswith("vergil-tooling "):
                return line.split()[1]
    except subprocess.CalledProcessError:
        pass
    return None
```

Apply the identical transformation to `inject_credentials`, `_inject_host_git_identity`, `_inject_identity_mode`, `_inject_claude_token`, `update_tooling`, `try_update_tooling`, `copy_claude_config`, `link_claude_dirs` (and move `_read_host_git_config` unchanged). Remove all of these from `lima.py`. Remove now-unused imports from `lima.py` (`json` may still be used by `vm_status`/`list_vms` — keep what remains in use).

- [ ] **Step 4: Remove the moved-function tests from `test_lima.py`**

Delete the `test_lima.py` test methods that exercise the moved functions (inject/install/update/copy/link/probe-not-yet). Leave the primitive tests (`_limactl`, `shell_run`, `shell_pipe`, `vm_status`, `list_vms`, `fetch_template`, `create_vm`, `start_vm`, `stop_vm`, `delete_vm`, nested-virt) intact.

- [ ] **Step 5: Run the new + existing tests**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_guest.py tests/vergil_tooling/test_lima.py -v`
Expected: PASS (new guest tests pass; remaining lima primitive tests pass).

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_guest.py src/vergil_tooling/lib/lima.py tests/vergil_tooling/test_vm_guest.py tests/vergil_tooling/test_lima.py
vrg-commit --type refactor --scope off-platform --message "move guest cred/tooling helpers to vm_guest over a Transport (#1706)" --body "Cuts inject_credentials/install_tooling/update_tooling/copy/link_claude_config and friends out of lima.py into vm_guest.py, reshaped to take a Transport instead of an instance name. Behavior unchanged; only the transport seam differs.\n\nRef #1706"
```

---

## Task 3: Move probe/fingerprint helpers to `vm_guest.py`

**Files:**
- Modify: `src/vergil_tooling/lib/vm_guest.py` (add the probe helpers)
- Modify: `src/vergil_tooling/lib/lima.py` (remove them)
- Modify: `tests/vergil_tooling/test_vm_guest.py`, `tests/vergil_tooling/test_lima.py`

**Functions to move:** `vm_probe`, `read_fingerprint`, `vm_spec_status`, `vm_occupancy`, and `VmUnreachableError`. Apply the transformation rule (these call `shell_run(instance, ...)` → `transport.run(...)`).

**Interfaces:**
- Produces:
  - `vm_probe(transport: Transport, *, fingerprint: bool = False) -> tuple[int, int, str | None]`
  - `read_fingerprint(transport: Transport) -> str | None`
  - `vm_spec_status(transport: Transport, expected_fingerprint: str) -> str` (returns `"ok"|"needs-rebuild"|"unreachable"`)

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_guest.py (append)
from vergil_tooling.lib.vm_guest import vm_spec_status


class TestSpecStatus:
    def test_ok_when_fingerprint_matches(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _ok("abc123\n")
        assert vm_spec_status(transport, "abc123") == "ok"

    def test_needs_rebuild_when_fingerprint_differs(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _ok("different\n")
        assert vm_spec_status(transport, "abc123") == "needs-rebuild"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_guest.py::TestSpecStatus -v`
Expected: FAIL with `ImportError: cannot import name 'vm_spec_status'`.

- [ ] **Step 3: Move the probe helpers** applying the transformation rule; remove from `lima.py`. Preserve the `unreachable` semantics exactly (an SSH/shell failure → `"unreachable"`, never `"needs-rebuild"`).

- [ ] **Step 4: Move the corresponding tests** out of `test_lima.py` (any probe/fingerprint tests) and adapt them to pass a `MagicMock` transport.

- [ ] **Step 5: Run tests**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_guest.py tests/vergil_tooling/test_lima.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_guest.py src/vergil_tooling/lib/lima.py tests/vergil_tooling/test_vm_guest.py tests/vergil_tooling/test_lima.py
vrg-commit --type refactor --scope off-platform --message "move probe/fingerprint helpers to vm_guest over a Transport (#1706)" --body "vm_probe/read_fingerprint/vm_spec_status now take a Transport. unreachable-vs-needs-rebuild semantics preserved.\n\nRef #1706"
```

---

## Task 4: `Backend` protocol + `LimaBackend` + `select_backend`

**Files:**
- Create: `src/vergil_tooling/lib/vm_backend.py`
- Create: `src/vergil_tooling/lib/vm_lima.py`
- Test: `tests/vergil_tooling/test_vm_backend.py`

**Interfaces:**
- Consumes: `ComposedSpec` (from `lib.vm_spec`), `LimaTransport`, the `lima` primitives, the `vm_guest` helpers.
- Produces:
  - `class Backend(Protocol)`: `provider_label: str`; `transport(self, instance: str) -> Transport`; `status(self, instance: str) -> str`.
  - `class LimaBackend` implementing it: `provider_label = "local"`; `transport(instance)` returns `LimaTransport(instance)`; `status(instance)` delegates to `lima.vm_status`.
  - `select_backend(spec: ComposedSpec) -> Backend` — returns `LimaBackend()` for `local`; raises `NotImplementedError("off-platform backend not yet available")` for `off-platform` (Plan 2 replaces this).

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_backend.py
import pytest

from vergil_tooling.lib.vm_backend import LimaBackend, select_backend
from vergil_tooling.lib.vm_spec import ComposedSpec
from vergil_tooling.lib.vm_transport import LimaTransport


def _spec(backend: str = "local", **kw: object) -> ComposedSpec:
    base = dict(
        cpus=4, memory="4GiB", disk="50GiB", stale_days=3, packages=(), apt_repos=(),
        vagrant_plugins=(), port_forwards=(), dedicated=False, under=(), nested=False,
        backend=backend,
    )
    base.update(kw)
    return ComposedSpec(**base)  # type: ignore[arg-type]


class TestSelectBackend:
    def test_local_returns_lima_backend(self) -> None:
        backend = select_backend(_spec("local"))
        assert isinstance(backend, LimaBackend)
        assert backend.provider_label == "local"
        assert isinstance(backend.transport("vm-x"), LimaTransport)

    def test_off_platform_not_yet_available(self) -> None:
        with pytest.raises(NotImplementedError):
            select_backend(_spec("off-platform", provider="gcp", region="us-central1",
                                 instance="n2-standard-16", volume="300GiB"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: vergil_tooling.lib.vm_backend`.

- [ ] **Step 3: Implement `vm_lima.py` then `vm_backend.py`**

```python
# src/vergil_tooling/lib/vm_lima.py
"""LimaBackend — the local limactl lifecycle behind the Backend protocol."""

from __future__ import annotations

from vergil_tooling.lib import lima
from vergil_tooling.lib.vm_transport import LimaTransport, Transport


class LimaBackend:
    provider_label = "local"

    def transport(self, instance: str) -> Transport:
        return LimaTransport(instance)

    def status(self, instance: str) -> str:
        return lima.vm_status(instance)
```

```python
# src/vergil_tooling/lib/vm_backend.py
"""Backend selection: route a composed spec to its lifecycle backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from vergil_tooling.lib.vm_lima import LimaBackend

if TYPE_CHECKING:
    from vergil_tooling.lib.vm_spec import ComposedSpec
    from vergil_tooling.lib.vm_transport import Transport


@runtime_checkable
class Backend(Protocol):
    provider_label: str

    def transport(self, instance: str) -> Transport: ...

    def status(self, instance: str) -> str: ...


def select_backend(spec: ComposedSpec) -> Backend:
    if spec.off_platform:
        msg = "off-platform backend not yet available"
        raise NotImplementedError(msg)
    return LimaBackend()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_backend.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_backend.py src/vergil_tooling/lib/vm_lima.py tests/vergil_tooling/test_vm_backend.py
vrg-commit --type feat --scope off-platform --message "add Backend protocol, LimaBackend, select_backend (#1706)" --body "One dispatch point: select_backend(spec) returns LimaBackend for local and raises NotImplementedError for off-platform (Plan 2 replaces).\n\nRef #1706"
```

---

## Task 5: Route `vrg_vm.py` through the backend + transport

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py`
- Modify: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Consumes: `select_backend`, `LimaTransport`, the relocated `vm_guest` helpers.
- Produces: no new public surface; `vrg_vm.py`'s lifecycle stages now obtain a `Transport` via `backend.transport(target.instance)` and call `vm_guest` functions with it.

**Changes:**
1. Replace the `from vergil_tooling.lib.lima import (…)` block. Keep only the lifecycle/query primitives imported from `lima` (`create_vm`, `start_vm`, `stop_vm`, `delete_vm`, `fetch_template`, `vm_status`, `list_vms`, `vm_age_days`, `nested_virt_unsupported_reason`, `shell_run`, `update_plugins`). Import the relocated guest helpers from `vm_guest` (`inject_credentials`, `install_tooling`, `update_tooling`, `try_update_tooling`, `get_tooling_version`, `copy_claude_config`, `link_claude_dirs`, `vm_probe`, `vm_spec_status`). `copy_claude_config` and `get_tooling_version` were moved to `vm_guest` in Task 2 — they must NOT remain in the `lima` import list.
2. Add `from vergil_tooling.lib.vm_backend import select_backend`.
3. In each `_st_*` guest stage and the inline `_cmd_session`/`_cmd_restart` credential calls, build `transport = LimaTransport(state.target.instance)` (or `LimaTransport(instance)`) and pass it to the `vm_guest` functions instead of the instance name. Example transformation:

```python
# before
def _st_credentials(state: _LifecycleState) -> None:
    inject_credentials(state.target.instance, state.target.identity)

# after
def _st_credentials(state: _LifecycleState) -> None:
    transport = LimaTransport(state.target.instance)
    inject_credentials(transport, state.target.identity)
```

Apply to `_st_link_config`, `_st_credentials`, `_st_install_tooling`, `_st_copy_config`, `_st_update_tooling`, `_st_spec_check` (which calls `vm_spec_status`), `_probe_running` (calls `vm_probe`), and the inline calls in `_cmd_session` (`try_update_tooling`, `copy_claude_config`, `link_claude_dirs`) and `_cmd_restart`/`_cmd_update`/`_update_instance` (`inject_credentials`, `get_tooling_version`, `update_tooling`, `update_plugins`).
4. `_vm_active_sessions` uses `shell_run(instance, …)` — leave it on `lima.shell_run` for now (a query primitive) OR route via `LimaTransport(instance).run(...)`. Prefer the transport for consistency: `LimaTransport(instance).run("vrg-vm-resolve-session", "--list-json")`.
5. The `select_backend` call: in `_resolve_target`/`_cmd_*`, after composing `spec`, call `select_backend(spec)` once. For Plan 1 the only effect is that an off-platform repo raises `NotImplementedError` — catch it in `main()` alongside the existing `(BorrowError, SpecError)` handler and print a clean message + exit 1.

- [ ] **Step 1: Write/adjust the failing test**

```python
# tests/vergil_tooling/test_vrg_vm.py (add)
from unittest.mock import MagicMock, patch

from vergil_tooling.bin import vrg_vm


class TestBackendRouting:
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    def test_credentials_stage_passes_a_transport(self, mock_inject: MagicMock) -> None:
        target = MagicMock()
        target.instance = "vm-x"
        state = vrg_vm._LifecycleState(target=target)
        vrg_vm._st_credentials(state)
        passed = mock_inject.call_args[0][0]
        # First arg is now a Transport, not the bare instance string.
        assert not isinstance(passed, str)
        assert getattr(passed, "instance", None) == "vm-x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py::TestBackendRouting -v`
Expected: FAIL (currently `inject_credentials` receives the instance string).

- [ ] **Step 3: Apply the import + call-site changes** described above.

- [ ] **Step 4: Run the full vm test set**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py tests/vergil_tooling/test_vm_guest.py tests/vergil_tooling/test_vm_backend.py tests/vergil_tooling/test_lima.py -v`
Expected: PASS. Fix any `test_vrg_vm.py` cases that patched `vergil_tooling.bin.vrg_vm.inject_credentials` etc. — the patch targets stay valid (still imported into `vrg_vm`), but assertions on the first positional arg change from the instance string to a transport.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type refactor --scope off-platform --message "route vrg-vm lifecycle through Backend + Transport (#1706)" --body "vrg_vm stages obtain a Transport via the backend and call vm_guest helpers with it; select_backend gates off-platform with a clean NotImplementedError. Lima behavior unchanged.\n\nRef #1706"
```

---

## Task 6: Full validation gate

**Files:** none (verification only).

- [ ] **Step 1: Run the full validation pipeline**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — lint, typecheck, **100% coverage**, tests, audit, common checks all green. If coverage dips, add focused tests for any new untested line in `vm_transport.py`/`vm_backend.py`/`vm_lima.py` (e.g. the `LimaTransport.pipe` stderr-on-error branch, `LimaBackend.status`).

- [ ] **Step 2: Confirm zero behavior change**

Manually confirm no Lima-path test asserted a *different* result than before — only import paths and the first-arg type (string → transport) changed. The behavioral assertions (command strings, file contents, error messages) are identical.

- [ ] **Step 3: Commit any coverage top-ups**

```bash
vrg-git add tests/vergil_tooling/
vrg-commit --type test --scope off-platform --message "cover new backend/transport seams to 100% (#1706)" --body "Ref #1706"
```

---

## Self-Review (completed during authoring)

- **Spec coverage:** This plan implements the spec's "Architecture / The two interfaces" and "How stages compose" for the Lima path, and the "Delivery → Refactor PR" item. Cloud-specific sections are Plan 2.
- **Placeholders:** none — every new file and test shows complete code; moves use one fully-worked example plus an exhaustive function list and a single transformation rule (DRY for mechanical edits).
- **Type consistency:** `Transport.run/pipe/exec_session`, `Backend.transport/status/provider_label`, and the reshaped `vm_guest` signatures are used identically across Tasks 1–5.
