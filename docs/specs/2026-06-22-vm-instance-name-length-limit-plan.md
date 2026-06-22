# Bounded Lima Instance Name Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound the Lima instance name so `vrg-vm create` cannot exceed Lima's `UNIX_PATH_MAX` socket-path limit, while keeping a VM's `(identity, org, repo)` recoverable.

**Architecture:** Cap `vm_spec.instance_name` to a home-aware budget with a cloud-style truncate+hash (mirroring `vm_cloud.cloud_resource_name`). When a name is mangled it can no longer be parsed back into tiers, so write a per-instance sidecar (`~/.lima/<instance>/vergil-meta.json`) at create time and recover the triple from it, falling back to `parse_instance_name` for legacy short names.

**Tech Stack:** Python 3.12+, pytest, Lima (`limactl`), `uv` (dev-tree override venv).

## Global Constraints

- **Python floor: 3.12** — CI runs 3.12; all code must run there.
- **Portability:** must work on macOS and Linux.
- **Lima instance-name regex:** `^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$` — single separators only, no leading/trailing separator, no adjacent separators. Every generated name must satisfy this.
- **`UNIX_PATH_MAX = 104`**, checked strict-less-than by Lima.
- **No silent failures** — a corrupt sidecar raises loudly; it is never swallowed into a wrong answer.
- **Git via `vrg-git`; commits via `vrg-commit`** (raw `git`/`gh` are denied). Work happens in worktree `.worktrees/issue-1750-vm-name-length` on branch `feature/1750-vm-name-length`.
- **Validation:** the only full-validation command is `vrg-container-run -- vrg-validate` (run from inside the worktree). Per-step TDD uses `uv run pytest` against the dev-tree `.venv-host`.

---

## File Structure

- `src/vergil_tooling/lib/vm_spec.py` — add `lima_name_budget()` and the length-bounding logic inside `instance_name`. This is the Lima analog of `cloud_resource_name`; the cap belongs here so every caller inherits it.
- `src/vergil_tooling/lib/lima.py` — add `write_instance_meta()` / `read_instance_meta()`; they own the `~/.lima/<instance>/` sidecar (next to the existing `_serial_dir` helper).
- `src/vergil_tooling/bin/vrg_vm.py` — add `recover_triple()`; switch the two recovery sites to it; write the sidecar in `_create_from_target` for dedicated boxes.
- `tests/vergil_tooling/test_vm_spec.py` — budget + naming tests.
- `tests/vergil_tooling/test_vrg_vm.py` — `recover_triple` + sidecar-write wiring tests.

> **Dev-tree setup (once per shell, from the worktree root):**
> ```bash
> cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1750-vm-name-length
> UV_PROJECT_ENVIRONMENT=.venv-host uv sync --group dev
> export PATH="$(pwd)/.venv-host/bin:$PATH"
> ```
> All `uv run pytest` steps below assume this venv.

---

### Task 1: Home-aware name budget

**Files:**
- Modify: `src/vergil_tooling/lib/vm_spec.py` (add import + helper near the `_TIER_SEP` block, ~line 279)
- Test: `tests/vergil_tooling/test_vm_spec.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `lima_name_budget(home: str | None = None) -> int`, and module constant `_UNIX_PATH_MAX = 104`. Budget formula: `(_UNIX_PATH_MAX - 1) - len(home) - (len("/.lima/") + len("/ssh.sock.") + 16)`, i.e. `70 - len(home)`. When `home is None`, uses `str(Path.home())`.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vm_spec.py` (import `lima_name_budget` alongside the existing `instance_name` import at the top):

```python
def test_lima_name_budget_subtracts_home_and_socket_overhead():
    # 104 - 1 - len(home) - (len("/.lima/")=7 + len("/ssh.sock.")=10 + 16) == 70 - len(home)
    assert lima_name_budget("/Users/pmoore") == 57
    assert lima_name_budget("/root") == 65
    assert lima_name_budget("/home/runner") == 58
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vm_spec.py::test_lima_name_budget_subtracts_home_and_socket_overhead -v`
Expected: FAIL — `ImportError: cannot import name 'lima_name_budget'`.

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/lib/vm_spec.py`, add `from pathlib import Path` to the imports (after `import re`), and add below the `_TIER_SEP = "."` line:

```python
_UNIX_PATH_MAX = 104
# Lima validates the longest socket path it might create:
#   <home>/.lima/<instance>/ssh.sock.<16-char worst-case reservation>
# The reservation and "/.lima/"/"/ssh.sock." segments are fixed; only the
# instance name is ours to bound. Strict-less-than, hence the -1.
_SOCK_OVERHEAD = len("/.lima/") + len("/ssh.sock.") + 16  # 7 + 10 + 16 = 33


def lima_name_budget(home: str | None = None) -> int:
    """Max instance-name length that keeps Lima's socket path under UNIX_PATH_MAX."""
    home = home if home is not None else str(Path.home())
    return (_UNIX_PATH_MAX - 1) - len(home) - _SOCK_OVERHEAD
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_vm_spec.py::test_lima_name_budget_subtracts_home_and_socket_overhead -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_spec.py tests/vergil_tooling/test_vm_spec.py
vrg-commit --type feat --scope vm \
  --message "add home-aware Lima instance-name budget" \
  --body "Compute the max instance-name length that keeps Lima's worst-case ssh.sock path under UNIX_PATH_MAX. Ref #1750"
```

---

### Task 2: Length-bounded `instance_name`

**Files:**
- Modify: `src/vergil_tooling/lib/vm_spec.py` (the `instance_name` function, ~line 282)
- Test: `tests/vergil_tooling/test_vm_spec.py`

**Interfaces:**
- Consumes: `lima_name_budget` (Task 1); existing `SpecError`, `hashlib`, `_TIER_SEP`.
- Produces: updated `instance_name(identity: str, org: str | None, repo: str | None, *, home: str | None = None) -> str`. Unchanged for base boxes and for dotted names within budget; over budget returns `f"{full[:budget-7].rstrip('._-')}-{sha256(f'{identity}/{org}/{repo}')[:6]}"`. Raises `SpecError` when `budget < len(identity) + 7`.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vm_spec.py` (import `lima_name_budget` already added in Task 1; add `import re` at top if absent):

```python
_LIMA_NAME_RE = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")


def test_instance_name_unchanged_when_within_budget():
    # 52 chars, fits the 57 budget for /Users/pmoore -> returned verbatim.
    name = instance_name(
        "vergil-user", "logical-minds-foundry", "mq-cluster-tooling", home="/Users/pmoore"
    )
    assert name == "vergil-user.logical-minds-foundry.mq-cluster-tooling"


def test_instance_name_truncates_and_hashes_when_over_budget():
    # The reported failure: 61-char full name, 57 budget.
    name = instance_name(
        "vergil-user", "logical-minds-foundry", "mq-resiliency-lab-for-linux",
        home="/Users/pmoore",
    )
    assert len(name) <= lima_name_budget("/Users/pmoore")
    assert _LIMA_NAME_RE.fullmatch(name)            # valid Lima instance name
    assert name.startswith("vergil-user.logical")   # readable prefix retained
    assert re.search(r"-[0-9a-f]{6}$", name)         # 6-char hash suffix


def test_instance_name_truncation_is_deterministic():
    args = ("vergil-user", "logical-minds-foundry", "mq-resiliency-lab-for-linux")
    assert instance_name(*args, home="/Users/pmoore") == instance_name(*args, home="/Users/pmoore")


def test_instance_name_raises_when_budget_cannot_fit_identity():
    with pytest.raises(SpecError):
        instance_name("vergil-user", "o", "r", home="/" + "x" * 70)
```

Ensure `import pytest` and `from vergil_tooling.lib.vm_spec import SpecError` are present in the test file (add if missing).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vm_spec.py -k instance_name -v`
Expected: FAIL — `instance_name` got an unexpected keyword `home` / over-budget case returns the too-long dotted name.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `instance_name` in `src/vergil_tooling/lib/vm_spec.py`:

```python
def instance_name(
    identity: str, org: str | None, repo: str | None, *, home: str | None = None
) -> str:
    """Derive the Lima instance name. Bare identity = base box; ``.``-joined = dedicated.

    Dedicated names are returned verbatim when they fit ``lima_name_budget``; over
    budget they are truncated and hashed (mirroring ``vm_cloud.cloud_resource_name``)
    so Lima's worst-case socket path stays under UNIX_PATH_MAX. ``recover_triple``
    (vrg_vm) reverses a mangled name via the per-instance sidecar.
    """
    if org is None or repo is None:
        return identity
    for tier, value in (("identity", identity), ("org", org)):
        if _TIER_SEP in value:
            msg = f"{tier} name {value!r} must not contain '{_TIER_SEP}'"
            raise ValueError(msg)
    full = _TIER_SEP.join((identity, org, repo))
    budget = lima_name_budget(home)
    if len(full) <= budget:
        return full
    if budget < len(identity) + 7:  # 6 hash chars + 1 separator
        msg = (
            f"home directory too long to fit a bounded VM name for identity "
            f"{identity!r}: budget {budget} < {len(identity) + 7}"
        )
        raise SpecError(msg)
    digest = hashlib.sha256(f"{identity}/{org}/{repo}".encode()).hexdigest()[:6]
    keep = budget - 7
    return f"{full[:keep].rstrip('._-')}-{digest}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_vm_spec.py -k instance_name -v`
Expected: PASS (including the pre-existing `instance_name`/`parse_instance_name` tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_spec.py tests/vergil_tooling/test_vm_spec.py
vrg-commit --type fix --scope vm \
  --message "bound Lima instance name with truncate+hash over budget" \
  --body "Over-budget dedicated names are truncated and hashed like the cloud backend, satisfying Lima's name regex and keeping the socket path under UNIX_PATH_MAX. Ref #1750"
```

---

### Task 3: Per-instance sidecar read/write

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py` (add after `_serial_dir`, ~line 280; add `import json` is already present)
- Test: `tests/vergil_tooling/test_vm_guest.py` *(new functions; or create `tests/vergil_tooling/test_lima_meta.py`)* — use `test_lima_meta.py`.

**Interfaces:**
- Consumes: existing `_serial_dir(instance) -> Path`, `json`, `Path`.
- Produces:
  - `write_instance_meta(instance: str, identity: str, org: str, repo: str) -> None` — writes `_serial_dir(instance)/"vergil-meta.json"` = `{"schema": 1, "identity": ..., "org": ..., "repo": ...}` (creates the dir if absent).
  - `read_instance_meta(instance: str) -> dict[str, str] | None` — returns the parsed dict, or `None` if the file is absent. Raises on malformed JSON or missing keys.

- [ ] **Step 1: Write the failing test**

Create `tests/vergil_tooling/test_lima_meta.py`:

```python
import json

import pytest

from vergil_tooling.lib import lima


def test_write_then_read_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(lima.Path, "home", classmethod(lambda cls: tmp_path))
    lima.write_instance_meta("u.org.repo", "vergil-user", "org", "repo")
    assert lima.read_instance_meta("u.org.repo") == {
        "schema": 1,
        "identity": "vergil-user",
        "org": "org",
        "repo": "repo",
    }


def test_read_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(lima.Path, "home", classmethod(lambda cls: tmp_path))
    assert lima.read_instance_meta("missing") is None


def test_read_raises_on_corrupt_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(lima.Path, "home", classmethod(lambda cls: tmp_path))
    d = tmp_path / ".lima" / "u.org.repo"
    d.mkdir(parents=True)
    (d / "vergil-meta.json").write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        lima.read_instance_meta("u.org.repo")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_lima_meta.py -v`
Expected: FAIL — `AttributeError: module 'vergil_tooling.lib.lima' has no attribute 'write_instance_meta'`.

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/lib/lima.py`, add after `_serial_dir`:

```python
_META_FILE = "vergil-meta.json"


def write_instance_meta(instance: str, identity: str, org: str, repo: str) -> None:
    """Record (identity, org, repo) beside the instance so a mangled name stays reversible.

    Lives in the instance's own ``~/.lima/<instance>/`` dir, so it is removed when
    ``limactl delete --force`` deletes that dir — no separate cleanup, no drift.
    """
    meta_dir = _serial_dir(instance)
    meta_dir.mkdir(parents=True, exist_ok=True)
    payload = {"schema": 1, "identity": identity, "org": org, "repo": repo}
    (meta_dir / _META_FILE).write_text(json.dumps(payload))


def read_instance_meta(instance: str) -> dict[str, str] | None:
    """Return the instance's recorded triple, or None if no sidecar exists.

    Raises on a malformed sidecar rather than silently falling back — a corrupt
    file is a real fault, not a missing one.
    """
    path = _serial_dir(instance) / _META_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # Touch the required keys so a truncated/garbled file fails loudly here.
    _ = (data["identity"], data["org"], data["repo"])
    return data
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_lima_meta.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/lima.py tests/vergil_tooling/test_lima_meta.py
vrg-commit --type feat --scope vm \
  --message "add per-instance Lima metadata sidecar read/write" \
  --body "Store (identity, org, repo) in ~/.lima/<instance>/vergil-meta.json so a truncated instance name stays reversible; self-cleaning with the instance dir. Ref #1750"
```

---

### Task 4: `recover_triple` and switch the recovery sites

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (imports near line 46; add helper; change lines 991 and 1222)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Consumes: `read_instance_meta` (Task 3, import from `vergil_tooling.lib.lima`); `parse_instance_name` (already imported).
- Produces: `recover_triple(instance: str) -> tuple[str, str | None, str | None]` — sidecar triple when present, else `parse_instance_name(instance)`.

- [ ] **Step 1: Write the failing test**

`test_vrg_vm.py` imports symbols directly from `vergil_tooling.bin.vrg_vm` (see the `from ... import (` block at the top) and patches collaborators via string paths. Add `recover_triple` to that import block, then add these tests (patch `read_instance_meta` — which Task 4 imports into the `vrg_vm` namespace — by its dotted path):

```python
def test_recover_triple_prefers_sidecar(monkeypatch):
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_vm.read_instance_meta",
        lambda inst: {"schema": 1, "identity": "vergil-user", "org": "o", "repo": "r"},
    )
    assert recover_triple("mangled-abc123") == ("vergil-user", "o", "r")


def test_recover_triple_falls_back_to_parse_for_legacy_name(monkeypatch):
    monkeypatch.setattr("vergil_tooling.bin.vrg_vm.read_instance_meta", lambda inst: None)
    assert recover_triple("vergil-user.acme.widgets") == ("vergil-user", "acme", "widgets")


def test_recover_triple_falls_back_to_parse_for_base_box(monkeypatch):
    monkeypatch.setattr("vergil_tooling.bin.vrg_vm.read_instance_meta", lambda inst: None)
    assert recover_triple("vergil-user") == ("vergil-user", None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py -k recover_triple -v`
Expected: FAIL — `module 'vergil_tooling.bin.vrg_vm' has no attribute 'recover_triple'`.

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/bin/vrg_vm.py`, add `read_instance_meta` and `write_instance_meta` to the existing `from vergil_tooling.lib.lima import (` block (near line 46–48). Add the helper near the other instance-name helpers (e.g. just after the imports / before `_all_update_targets`):

```python
def recover_triple(instance: str) -> tuple[str, str | None, str | None]:
    """Reverse an instance name into (identity, org, repo).

    Prefers the per-instance sidecar (the only reliable source once a long name
    has been truncated+hashed); falls back to parsing the name for legacy short
    names and base boxes that predate the sidecar.
    """
    meta = read_instance_meta(instance)
    if meta is not None:
        return meta["identity"], meta["org"], meta["repo"]
    return parse_instance_name(instance)
```

Then change the two recovery sites (the `except ValueError: continue` stays — `parse_instance_name` still raises `ValueError` for genuinely unparseable legacy names):

- Line ~991 in `_all_update_targets`:
  ```python
              try:
                  ident, org, repo = recover_triple(inst)
              except ValueError:
                  continue
  ```
- Line ~1222 in `discover_dedicated`:
  ```python
          try:
              ident, org, repo = recover_triple(name)
          except ValueError:
              continue
  ```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py -k recover_triple -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type fix --scope vm \
  --message "recover VM triple via sidecar with parse fallback" \
  --body "discover_dedicated and _all_update_targets now resolve (identity, org, repo) through recover_triple, so truncated-name VMs are no longer silently skipped. Ref #1750"
```

---

### Task 5: Write the sidecar at create time

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` (`_create_from_target`, ~line 573)
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Consumes: `write_instance_meta` (imported in Task 4); `Target.identity_name`, `Target.org`, `Target.repo`, `Target.instance`.
- Produces: side effect — after a dedicated box is created, its sidecar exists. Base boxes write no sidecar.

- [ ] **Step 1: Write the failing test**

`_create_from_target` only reads attributes off `target` (and `create_vm`/`write_instance_meta` are patched), so a `types.SimpleNamespace` stub is sufficient — no need to build a real `Target`/`ComposedSpec`. Add `_create_from_target` to the `from vergil_tooling.bin.vrg_vm import (...)` block and add `import types` at the top, then:

```python
def _stub_target(*, dedicated, identity_name, org, repo, instance):
    spec = types.SimpleNamespace(
        dedicated=dedicated, cpus=4, memory="8GiB", disk="100GiB",
        packages=[], apt_repos=[], vagrant_plugins=[], port_forwards=[], nested=False,
    )
    identity = types.SimpleNamespace(
        projects_dir="/home/user/projects", cpus=4, memory="8GiB", disk="100GiB",
    )
    return types.SimpleNamespace(
        spec=spec, identity=identity, identity_name=identity_name,
        org=org, repo=repo, instance=instance, fingerprint="fp",
    )


def test_create_from_target_writes_sidecar_for_dedicated(monkeypatch):
    calls = []
    monkeypatch.setattr("vergil_tooling.bin.vrg_vm.create_vm", lambda *a, **k: None)
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_vm.write_instance_meta", lambda *a: calls.append(a)
    )
    target = _stub_target(
        dedicated=True, identity_name="vergil-user",
        org="acme", repo="widgets", instance="vergil-user.acme.widgets",
    )
    _create_from_target(target, Path("/tmp/t.yaml"))
    assert calls == [("vergil-user.acme.widgets", "vergil-user", "acme", "widgets")]


def test_create_from_target_skips_sidecar_for_base(monkeypatch):
    calls = []
    monkeypatch.setattr("vergil_tooling.bin.vrg_vm.create_vm", lambda *a, **k: None)
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_vm.write_instance_meta", lambda *a: calls.append(a)
    )
    target = _stub_target(
        dedicated=False, identity_name="vergil-user",
        org=None, repo=None, instance="vergil-agent",
    )
    _create_from_target(target, Path("/tmp/t.yaml"))
    assert calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py -k create_from_target -v`
Expected: FAIL — `write_instance_meta` never called for the dedicated case.

- [ ] **Step 3: Write minimal implementation**

In `_create_from_target`, in the `if target.spec.dedicated:` branch, after the `create_vm(...)` call returns, add:

```python
        assert target.org is not None and target.repo is not None  # dedicated invariant
        write_instance_meta(target.instance, target.identity_name, target.org, target.repo)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py -k create_from_target -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope vm \
  --message "write VM metadata sidecar on dedicated create" \
  --body "After a dedicated box is created, persist its (identity, org, repo) sidecar so a truncated instance name remains reversible by discovery. Ref #1750"
```

---

### Task 6: Full validation and PR handoff prep

**Files:** none (verification only).

- [ ] **Step 1: Run the full validation pipeline**

Run (from the worktree root): `vrg-container-run -- vrg-validate`
Expected: PASS (lint, typecheck, full test suite, audit, common checks).

- [ ] **Step 2: Fix anything validation surfaces, re-run until green.** Commit fixes with `vrg-commit` using the matching type/scope.

- [ ] **Step 3: Record PR metadata for the human handoff** (agents must not submit the PR):

```bash
vrg-pr-workflow report-ready \
  --title "fix(vm): bound Lima instance name under UNIX_PATH_MAX" \
  --summary "Cap instance_name via a home-aware budget with cloud-style truncate+hash; recover (identity, org, repo) from a per-instance sidecar with parse fallback." \
  --notes "Fixes dedicated VM create failure for long org/repo names (e.g. logical-minds-foundry/mq-resiliency-lab-for-linux). No migration: existing short names are byte-identical. Ref #1750" \
  --linkage Ref
```

Then stop — the human runs `vrg-submit-pr`.

---

## Self-Review

**1. Spec coverage:**
- Name generation (base unchanged / dotted-when-fits / truncate+hash) → Task 2. ✓
- Computed budget (`70 - len(home)`, injectable, guard) → Tasks 1 & 2. ✓
- Sidecar write/read → Task 3; written at create → Task 5. ✓
- `recover_triple` + both call sites + latent-bug fix → Task 4. ✓
- Testing (budget math, unchanged-when-fits, regex-valid mangle, determinism, guard, sidecar round-trip, fallback) → Tasks 1–5. ✓
- Back-compat (legacy parse fallback; base box skip) → Tasks 2, 4, 5. ✓
- Edge case (home-length change) → documented in spec; no code (YAGNI). ✓

**2. Placeholder scan:** No TBD/TODO. All test sketches use concrete imports/patches matched to the existing `test_vrg_vm.py` conventions (direct symbol imports, string-path `monkeypatch.setattr`, `SimpleNamespace` target stub) and `test_vm_spec.py` (which already imports `re`/`pytest`/`SpecError`).

**3. Type consistency:** `lima_name_budget(home: str | None)` returns `int`, consumed by `instance_name` (Task 2). `write_instance_meta(instance, identity, org, repo)` arg order matches the Task 5 call and the Task 4 `recover_triple` read of `meta["identity"|"org"|"repo"]`. Sidecar schema (`schema/identity/org/repo`) is identical across Tasks 3, 4, 5.
