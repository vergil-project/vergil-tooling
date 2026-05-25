# Stateless VM Lifecycle — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make identity VMs dataless and stateless — selective
host mounts for Claude Code data, staleness enforcement with hard
block, auto-update on every entry point, and a `rebuild` command.

**Architecture:** All persistent data lives on the macOS host via
virtiofs mounts (`~/.claude/projects/`, `~/.claude/skills/`) and
file copies (`CLAUDE.md`, `settings.json`). VM staleness is
checked against Lima directory metadata on the host. vergil-tooling
is auto-updated on `start` and `session` with graceful fallback
on network failure.

**Tech Stack:** Python, Lima, argparse, pytest

**Specs:**
- `docs/specs/2026-05-24-stateless-vm-lifecycle-design.md` (#907)

**Repository:** vergil-tooling (all changes are host-side
orchestration in `vrg-vm`)

**Note on Lima template mounts:** The `~/.claude/projects/` and
`~/.claude/skills/` mounts need to be added to the Lima template
in the vergil-vm repository. That template does not live in this
repo. This plan covers the vergil-tooling side (host-side
orchestration). The vergil-vm template changes are tracked
separately.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/lib/lima.py` | Modify | Add `vm_age_days()`, `copy_claude_config()`, graceful `try_update_tooling()` |
| `src/vergil_tooling/bin/vrg_vm.py` | Modify | Add `rebuild` subcommand, staleness check, `--allow-stale-vm`, auto-update in `start` |
| `tests/vergil_tooling/test_lima.py` | Modify | Tests for `vm_age_days()`, `copy_claude_config()`, `try_update_tooling()` |
| `tests/vergil_tooling/test_vrg_vm.py` | Modify | Tests for `rebuild`, staleness enforcement, `--allow-stale-vm` |

---

### Task 1: VM Age Detection

Read the VM creation timestamp from Lima's instance directory
metadata on the host. No data is stored inside the VM.

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py`
- Modify: `tests/vergil_tooling/test_lima.py`

- [ ] **Step 1: Write the failing test for `vm_age_days`**

```python
class TestVmAgeDays:
    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_age_in_days(self, mock: MagicMock, tmp_path: Path) -> None:
        vm_dir = tmp_path / "vergil-agent"
        vm_dir.mkdir()

        mock.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"name": "vergil-agent", "dir": str(vm_dir)}) + "\n"
        )

        age = vm_age_days("vergil-agent")
        assert age is not None
        assert age >= 0

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_none_when_vm_not_found(self, mock: MagicMock) -> None:
        mock.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"name": "other-vm", "dir": "/tmp/other"}) + "\n"
        )
        assert vm_age_days("vergil-agent") is None

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_none_on_error(self, mock: MagicMock) -> None:
        mock.side_effect = subprocess.CalledProcessError(1, "limactl")
        assert vm_age_days("vergil-agent") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_lima.py::TestVmAgeDays -v`
Expected: FAIL with `ImportError` (function not defined)

- [ ] **Step 3: Implement `vm_age_days`**

Add to `src/vergil_tooling/lib/lima.py`:

```python
import time

def vm_age_days(instance: str) -> float | None:
    """Return VM age in fractional days, or None if not found."""
    try:
        result = _limactl("list", "--json")
    except subprocess.CalledProcessError:
        return None
    for line in result.stdout.strip().splitlines():
        entry = json.loads(line)
        if entry.get("name") == instance:
            vm_dir = entry.get("dir", "")
            if not vm_dir:
                return None
            dir_path = Path(vm_dir)
            if not dir_path.exists():
                return None
            created = dir_path.stat().st_birthtime
            return (time.time() - created) / 86400
    return None
```

Update imports: add `time` to the import list.

- [ ] **Step 4: Export `vm_age_days` from the module**

Add `vm_age_days` to the imports in
`src/vergil_tooling/bin/vrg_vm.py` and to the test import in
`tests/vergil_tooling/test_lima.py`.

- [ ] **Step 5: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_lima.py::TestVmAgeDays -v`
Expected: PASS

- [ ] **Step 6: Commit**

```
vrg-commit --type feat --scope vm \
  --message "vm_age_days reads VM creation time from Lima metadata" \
  --body "Returns fractional days since VM creation using the Lima instance directory birth time. Ref #907"
```

---

### Task 2: Copy Claude Config Files

Copy `~/.claude/CLAUDE.md` and `~/.claude/settings.json` from the
host into the VM. Lima mounts directories, not files, so these
individual files must be copied.

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py`
- Modify: `tests/vergil_tooling/test_lima.py`

- [ ] **Step 1: Write the failing test for `copy_claude_config`**

```python
class TestCopyClaudeConfig:
    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_copies_existing_files(
        self, mock_run: MagicMock, mock_pipe: MagicMock, tmp_path: Path
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# My prefs\n")
        (claude_dir / "settings.json").write_text('{"key": "val"}\n')

        copy_claude_config("vergil-agent", claude_dir)

        assert mock_run.call_count == 1
        mkdir_call = mock_run.call_args_list[0]
        assert "mkdir" in " ".join(str(a) for a in mkdir_call[0])
        assert ".claude" in " ".join(str(a) for a in mkdir_call[0])

        assert mock_pipe.call_count == 2
        md_call = mock_pipe.call_args_list[0]
        assert "CLAUDE.md" in md_call[0][1]
        assert "# My prefs" in md_call[0][2]
        settings_call = mock_pipe.call_args_list[1]
        assert "settings.json" in settings_call[0][1]
        assert '"key"' in settings_call[0][2]

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_skips_missing_files(
        self, mock_run: MagicMock, mock_pipe: MagicMock, tmp_path: Path
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        copy_claude_config("vergil-agent", claude_dir)

        mock_run.assert_called_once()
        mock_pipe.assert_not_called()

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_skips_if_claude_dir_missing(
        self, mock_run: MagicMock, mock_pipe: MagicMock, tmp_path: Path
    ) -> None:
        claude_dir = tmp_path / ".claude"

        copy_claude_config("vergil-agent", claude_dir)

        mock_run.assert_not_called()
        mock_pipe.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_lima.py::TestCopyClaudeConfig -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement `copy_claude_config`**

Add to `src/vergil_tooling/lib/lima.py`:

```python
_CLAUDE_CONFIG_FILES = ("CLAUDE.md", "settings.json")


def copy_claude_config(instance: str, claude_dir: Path) -> None:
    """Copy CLAUDE.md and settings.json from host into the VM."""
    if not claude_dir.is_dir():
        return
    shell_run(instance, "bash", "-c", "mkdir -p ~/.claude")
    for filename in _CLAUDE_CONFIG_FILES:
        source = claude_dir / filename
        if source.exists():
            content = source.read_text()
            shell_pipe(
                instance,
                f"cat > ~/.claude/{filename}",
                content,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_lima.py::TestCopyClaudeConfig -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope vm \
  --message "copy_claude_config copies CLAUDE.md and settings.json into VM" \
  --body "One-way host-to-VM copy for files that cannot be mounted (Lima mounts directories only). Ref #907"
```

---

### Task 3: Graceful Tooling Update

Wrap `update_tooling` with a version that catches network failures
and warns instead of aborting. Used by `start` and `session`.

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py`
- Modify: `tests/vergil_tooling/test_lima.py`

- [ ] **Step 1: Write the failing test for `try_update_tooling`**

```python
class TestTryUpdateTooling:
    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_returns_true_on_success(self, mock_update: MagicMock) -> None:
        result = try_update_tooling("vergil-agent", fallback_tag="v2.0")
        assert result is True
        mock_update.assert_called_once_with("vergil-agent", None, fallback_tag="v2.0")

    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_returns_false_on_subprocess_error(
        self, mock_update: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_update.side_effect = subprocess.CalledProcessError(1, "uv")
        result = try_update_tooling("vergil-agent", fallback_tag="v2.0")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_returns_false_on_system_exit(
        self, mock_update: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_update.side_effect = SystemExit(1)
        result = try_update_tooling("vergil-agent", fallback_tag="v2.0")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_passes_explicit_tag(self, mock_update: MagicMock) -> None:
        try_update_tooling("vergil-agent", tag="v2.1", fallback_tag="v2.0")
        mock_update.assert_called_once_with("vergil-agent", "v2.1", fallback_tag="v2.0")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_lima.py::TestTryUpdateTooling -v`
Expected: FAIL with `ImportError`

- [ ] **Step 3: Implement `try_update_tooling`**

Add to `src/vergil_tooling/lib/lima.py`:

```python
def try_update_tooling(
    instance: str,
    tag: str | None = None,
    *,
    fallback_tag: str = "",
) -> bool:
    """Update vergil-tooling, returning False on failure instead of aborting."""
    try:
        update_tooling(instance, tag, fallback_tag=fallback_tag)
        return True
    except (subprocess.CalledProcessError, SystemExit):
        print(
            "WARNING: vergil-tooling update failed — continuing with installed version",
            file=sys.stderr,
        )
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_lima.py::TestTryUpdateTooling -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope vm \
  --message "try_update_tooling wraps update with graceful fallback" \
  --body "Returns False and warns on failure instead of aborting. Used by start and session to avoid blocking on network errors. Ref #907"
```

---

### Task 4: Staleness Enforcement in `vrg-vm start`

Add staleness check and `--allow-stale-vm` flag to `start`.
Also add auto-update and Claude config copy.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py`
- Modify: `tests/vergil_tooling/test_vrg_vm.py`

- [ ] **Step 1: Write the failing tests**

```python
_DEFAULT_STALENESS_DAYS = 3


class TestStartStaleness:
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_rejects_stale_vm(
        self,
        _status: MagicMock,
        _age: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 1
        captured = capsys.readouterr()
        assert "5 days old" in captured.err or "5.0" in captured.err
        assert "--allow-stale-vm" in captured.err

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_allows_stale_with_override(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["start", "--config", str(config_file), "--allow-stale-vm"])
        assert result == 0
        mock_start.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_passes_fresh_vm(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 0
        mock_start.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_calls_auto_update(
        self,
        _status: MagicMock,
        _age: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        mock_update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
    ) -> None:
        main(["start", "--config", str(config_file)])
        mock_update.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_copies_claude_config(
        self,
        _status: MagicMock,
        _age: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        mock_copy: MagicMock,
        config_file: Path,
    ) -> None:
        main(["start", "--config", str(config_file)])
        mock_copy.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestStartStaleness -v`
Expected: FAIL

- [ ] **Step 3: Update imports in `vrg_vm.py`**

Add to the `lima` import block:

```python
from vergil_tooling.lib.lima import (
    copy_claude_config,
    create_vm,
    delete_vm,
    fetch_template,
    inject_credentials,
    install_tooling,
    list_vms,
    start_vm,
    stop_vm,
    try_update_tooling,
    update_tooling,
    vm_age_days,
    vm_status,
)
```

- [ ] **Step 4: Add `--allow-stale-vm` to `start` parser**

In the `main` function, after the `p_start` parser is created:

```python
p_start = sub.add_parser("start", help="Start VM and inject credentials")
_add_identity_args(p_start)
p_start.add_argument(
    "--allow-stale-vm",
    action="store_true",
    help="Start even if the VM exceeds the staleness threshold",
)
```

- [ ] **Step 5: Add staleness constant**

At the top of `vrg_vm.py`, after imports:

```python
_DEFAULT_STALENESS_DAYS = 3
```

- [ ] **Step 6: Rewrite `_cmd_start`**

```python
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
    start_vm(identity.vm_instance)

    print("Injecting credentials...")
    inject_credentials(identity.vm_instance, identity)

    claude_dir = Path.home() / ".claude"
    print("Copying Claude Code config...")
    copy_claude_config(identity.vm_instance, claude_dir)

    fallback = resolve_vergil_version(config, identity)
    print("Updating vergil-tooling...")
    try_update_tooling(identity.vm_instance, fallback_tag=fallback)

    print(f"VM '{identity.vm_instance}' is running.")
    return 0
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestStartStaleness -v`
Expected: PASS

- [ ] **Step 8: Run full test suite**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -v`
Expected: ALL PASS (existing `TestStart` tests may need the new
mocked dependencies added — update them to patch the new functions)

- [ ] **Step 9: Commit**

```
vrg-commit --type feat --scope vm \
  --message "staleness enforcement and auto-update in vrg-vm start" \
  --body "Refuses to start VMs older than 3 days unless --allow-stale-vm is passed. Auto-updates vergil-tooling and copies Claude config on start. Ref #907"
```

---

### Task 5: Staleness Enforcement in `vrg-vm session`

Add staleness check and `--allow-stale-vm` to `session`.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py`
- Modify: `tests/vergil_tooling/test_vrg_vm.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestSessionStaleness:
    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    def test_session_rejects_stale_vm(
        self,
        _age: MagicMock,
        _copy: MagicMock,
        _update: MagicMock,
        _exec: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = main(["session", "--config", str(config_file)])
        assert result == 1
        captured = capsys.readouterr()
        assert "--allow-stale-vm" in captured.err

    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    def test_session_allows_stale_with_override(
        self,
        _age: MagicMock,
        _copy: MagicMock,
        _update: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "--allow-stale-vm"])
        mock_exec.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    def test_session_passes_fresh_vm(
        self,
        _age: MagicMock,
        _copy: MagicMock,
        _update: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file)])
        mock_exec.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestSessionStaleness -v`
Expected: FAIL

- [ ] **Step 3: Add `--allow-stale-vm` to `session` parser**

```python
p_session = sub.add_parser("session", help="Shell into a VM")
_add_identity_args(p_session)
p_session.add_argument(
    "--allow-stale-vm",
    action="store_true",
    help="Connect even if the VM exceeds the staleness threshold",
)
```

- [ ] **Step 4: Add staleness check to `_cmd_session`**

Insert at the beginning of `_cmd_session`, after resolving
the identity:

```python
def _cmd_session(args: argparse.Namespace) -> int:
    config_path = args.config if args.config else _default_config_path()
    config = load_config(config_path)
    identity = resolve_identity(config, args.identity)

    allow_stale = getattr(args, "allow_stale_vm", False)
    if not allow_stale:
        age = vm_age_days(identity.vm_instance)
        if age is not None and age > _DEFAULT_STALENESS_DAYS:
            name = args.identity or config.default_identity or "default"
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

    # ... rest of session logic unchanged
```

Note: replace the existing `update_tooling` call with
`try_update_tooling` and add the `copy_claude_config` call.

- [ ] **Step 5: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestSessionStaleness -v`
Expected: PASS

- [ ] **Step 6: Update existing session tests**

The existing `TestSession` tests need to mock the new
dependencies (`vm_age_days`, `copy_claude_config`,
`try_update_tooling`). Update each test to add these patches.
Replace `update_tooling` patches with `try_update_tooling`.

- [ ] **Step 7: Run full test suite**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py -v`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```
vrg-commit --type feat --scope vm \
  --message "staleness enforcement in vrg-vm session" \
  --body "Session refuses to connect to VMs older than 3 days unless --allow-stale-vm is passed. Also copies Claude config and uses graceful tooling update. Ref #907"
```

---

### Task 6: Rebuild Command

Add `vrg-vm rebuild` as a single command that destroys and
recreates a VM.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py`
- Modify: `tests/vergil_tooling/test_vrg_vm.py`

- [ ] **Step 1: Write the failing tests**

```python
class TestRebuild:
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_rebuild_destroys_and_creates(
        self,
        _status: MagicMock,
        mock_delete: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        mock_install: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["rebuild", "--config", str(config_file)])
        assert result == 0
        mock_delete.assert_called_once_with("vergil-agent")
        mock_create.assert_called_once()
        mock_start.assert_called_once()
        mock_inject.assert_called_once()
        mock_install.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_rebuild_fails_if_not_created(
        self, _status: MagicMock, config_file: Path
    ) -> None:
        result = main(["rebuild", "--config", str(config_file)])
        assert result == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestRebuild -v`
Expected: FAIL

- [ ] **Step 3: Add `rebuild` subcommand parser**

In `main`, add after the `destroy` parser:

```python
p_rebuild = sub.add_parser("rebuild", help="Destroy and recreate VM (stateless rebuild)")
_add_identity_args(p_rebuild)
p_rebuild.add_argument(
    "--tag", default="", help="VM template version tag (default: vergil version from config)"
)
```

Add to the dispatch dict:

```python
"rebuild": _cmd_rebuild,
```

- [ ] **Step 4: Implement `_cmd_rebuild`**

```python
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
        create_vm(identity.vm_instance, template, identity.projects_dir)

        print("  Starting VM...")
        start_vm(identity.vm_instance)

        print("  Injecting credentials...")
        inject_credentials(identity.vm_instance, identity)

        install_tooling(identity.vm_instance, vergil_version)

        claude_dir = Path.home() / ".claude"
        print("  Copying Claude Code config...")
        copy_claude_config(identity.vm_instance, claude_dir)
    finally:
        template.unlink(missing_ok=True)

    print(f"\nVM '{identity.vm_instance}' rebuilt and ready.")
    return 0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestRebuild -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```
vrg-commit --type feat --scope vm \
  --message "vrg-vm rebuild command for stateless VM lifecycle" \
  --body "Destroys and recreates the VM in one step. Safe because VMs are stateless — all persistent data lives on host mounts. Ref #907"
```

---

### Task 7: Update Existing Tests

The existing `TestStart` and `TestSession` tests need to be
updated to account for the new dependencies (`vm_age_days`,
`copy_claude_config`, `try_update_tooling`).

**Files:**
- Modify: `tests/vergil_tooling/test_vrg_vm.py`

- [ ] **Step 1: Update `TestStart` tests**

Add patches for `vm_age_days` (returning `1.0`),
`copy_claude_config`, and `try_update_tooling` to the existing
start tests. The existing `test_start_and_inject` test should
verify that all three new functions are called.

- [ ] **Step 2: Update `TestSession` tests**

Replace `update_tooling` patches with `try_update_tooling`.
Add `vm_age_days` (returning `1.0`) and `copy_claude_config`
patches to all session tests.

- [ ] **Step 3: Run full test suite**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```
vrg-commit --type test --scope vm \
  --message "update existing VM tests for stateless lifecycle changes" \
  --body "Adds mocks for vm_age_days, copy_claude_config, and try_update_tooling to existing start and session tests. Ref #907"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** All spec requirements are covered:
  - Selective mounts → noted as vergil-vm template work (out of scope for this plan)
  - Copied files (CLAUDE.md, settings.json) → Task 2
  - Auto-update on start and session → Tasks 4, 5
  - Graceful fallback → Task 3
  - Staleness enforcement → Tasks 4, 5
  - `--allow-stale-vm` override → Tasks 4, 5
  - `vrg-vm rebuild` → Task 6
  - VM age from Lima metadata → Task 1
- [x] **Placeholder scan:** No TBDs, TODOs, or vague steps. All code shown.
- [x] **Type consistency:** `vm_age_days` returns `float | None`,
  `try_update_tooling` returns `bool`, `copy_claude_config` takes
  `Path` — consistent across all tasks.
- [x] **Scope boundaries:** Lima template changes (mounts) are
  explicitly noted as vergil-vm work, not included here.
