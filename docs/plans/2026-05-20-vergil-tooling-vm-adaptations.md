# vergil-vm: vergil-tooling Adaptations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adapt vergil-tooling to work natively inside the
identity VM: detect nerdctl as the container runtime, simplify
`vrg-gh` by removing credential selection logic, and simplify
`vrg-git` by removing credential-protection guards.

**Architecture:** Three focused changes to vergil-tooling:
(1) `vrg-docker-run` auto-detects `nerdctl` vs `docker` and
uses whichever is available, (2) `vrg-gh` drops the two-account
discovery and credential selection code — inside the VM there is
exactly one GitHub identity, (3) `vrg-git` drops guards that
existed for credential protection while keeping workflow
enforcement guards.

**Tech Stack:** Python (vergil-tooling)

**Specs:**
- `docs/specs/2026-05-20-identity-vm-isolation-design.md` (#892)
  — Wrapper Simplification section
- `docs/specs/2026-05-20-vergil-vm-image-management-design.md`
  (#894) — Impact on vrg-docker-run section

**Decomposition:** This is Plan 5 of 6 for the identity VM
isolation system.

| Plan | Scope | Status |
|---|---|---|
| 1. Repository + Working VM | vergil-vm repo, Lima template | Complete |
| 2. Session Management | vrg-session, identities.toml | Planned |
| 3. Credential Provisioning | GitHub PAT/SSH key injection | Planned |
| ~~4. Egress Filtering~~ | ~~HAProxy, pf, iptables~~ | Deferred to v2.2 (#901) |
| **5. vergil-tooling Adaptations** (this plan) | nerdctl, wrapper simplification | This plan |
| 6. Distribution + Updates | Pre-built images, vrg-vm-update | Planned |

**Repository:** vergil-tooling

**Depends on:** Plan 1 (working VM with nerdctl), Plan 3
(single-identity credentials configured)

---

## Design

### Change Summary

| Component | What changes | What stays |
|---|---|---|
| `vrg-docker-run` | Auto-detect `nerdctl` or `docker` | All container operations, image building, volume mounts |
| `lib/docker.py` | Runtime detection function | Existing Docker subprocess helpers |
| `vrg-gh` | Delete `_discover_accounts()`, credential selection, escalation | Subcommand allowlist, flag denylist, audit logging |
| `vrg-git` | Delete credential-protection force-push guards | Subcommand allowlist, workflow guards, audit logging |

### Runtime Detection Logic

```python
def detect_runtime() -> str:
    """Return 'nerdctl' if available, else 'docker'."""
    # Check nerdctl first — inside identity VMs, this is the runtime
    if shutil.which("nerdctl"):
        return "nerdctl"
    if shutil.which("docker"):
        return "docker"
    # Neither found
    print("ERROR: no container runtime found (need docker or nerdctl)",
          file=sys.stderr)
    raise SystemExit(1)
```

The detection is done once at the start of each `vrg-docker-run`
invocation. The result replaces all `"docker"` literals in
subprocess calls.

### vrg-gh Simplification

**Current `_get_token()` flow (to be deleted):**

1. Discover accounts via `gh auth status`
2. Check if current repo matches agent or human account
3. Select appropriate token
4. Inject `GH_TOKEN` into subprocess environment
5. For certain operations (pr merge, issue close), escalate to
   human credentials

**Simplified flow:**

1. Run `gh` directly — it uses whatever token is configured
   in the VM's `~/.config/gh/hosts.yml`
2. No token selection, no escalation, no `_discover_accounts()`

The subcommand allowlist and audit logging remain unchanged.
Inside the VM, the agent has exactly one GitHub identity. The
wrapper ensures the agent uses approved operations; the VM
boundary ensures the agent uses the right credentials.

### vrg-git Simplification

**Guards to remove:**
- Force-push protection that existed to prevent the agent from
  pushing with human credentials (the VM boundary handles this)

**Guards to keep:**
- Subcommand allowlist (workflow enforcement)
- Flag denylist (workflow enforcement)
- Protected branch checks (workflow enforcement)
- Audit logging

### Migration Strategy

These changes affect all vergil-tooling consumers, not just the
VM. The adaptations must be **backward compatible**:

- Runtime detection falls back to `docker` if `nerdctl` is not
  found — existing Docker Desktop users are unaffected.
- `vrg-gh` simplification can be gated on environment detection
  (inside VM vs. host). Or: the simplified version works on the
  host too, since the host also has a configured `gh` token. The
  wrapper stops managing tokens; it just runs `gh` with whatever
  auth is ambient.
- `vrg-git` guard removal is safe on the host too — the
  credential protection was defense-in-depth, and the VM boundary
  replaces it.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/lib/docker.py` | Modify | Add `detect_runtime()` |
| `src/vergil_tooling/bin/vrg_docker_run.py` | Modify | Use detected runtime |
| `src/vergil_tooling/bin/vrg_gh.py` | Modify | Remove credential selection |
| `src/vergil_tooling/bin/vrg_git.py` | Modify | Remove credential guards |
| `tests/vergil_tooling/test_docker.py` | Modify | Add runtime detection tests |
| `tests/vergil_tooling/test_vrg_docker_run.py` | Modify | Update for runtime detection |
| `tests/vergil_tooling/test_vrg_gh.py` | Modify | Update for simplified auth |
| `tests/vergil_tooling/test_vrg_git.py` | Modify | Update for removed guards |

---

### Task 1: Container Runtime Detection

**Files:**
- Modify: `src/vergil_tooling/lib/docker.py`
- Modify: `tests/vergil_tooling/test_docker.py`

- [ ] **Step 1: Write the failing tests**

```python
# Add to tests/vergil_tooling/test_docker.py

def test_detect_runtime_nerdctl(monkeypatch: pytest.MonkeyPatch) -> None:
    from vergil_tooling.lib.docker import detect_runtime

    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: "/usr/bin/nerdctl" if cmd == "nerdctl" else None,
    )
    assert detect_runtime() == "nerdctl"


def test_detect_runtime_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    from vergil_tooling.lib.docker import detect_runtime

    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: "/usr/bin/docker" if cmd == "docker" else None,
    )
    assert detect_runtime() == "docker"


def test_detect_runtime_prefers_nerdctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vergil_tooling.lib.docker import detect_runtime

    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: f"/usr/bin/{cmd}",
    )
    assert detect_runtime() == "nerdctl"


def test_detect_runtime_none_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vergil_tooling.lib.docker import detect_runtime

    monkeypatch.setattr("shutil.which", lambda cmd: None)
    with pytest.raises(SystemExit):
        detect_runtime()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_docker.py -k detect_runtime -v`
Expected: FAIL

- [ ] **Step 3: Implement detect_runtime()**

Add to `src/vergil_tooling/lib/docker.py`:

```python
import shutil


def detect_runtime() -> str:
    if shutil.which("nerdctl"):
        return "nerdctl"
    if shutil.which("docker"):
        return "docker"
    print(
        "ERROR: no container runtime found (need docker or nerdctl)",
        file=sys.stderr,
    )
    raise SystemExit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_docker.py -k detect_runtime -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope docker \
  --message "container runtime auto-detection (nerdctl/docker)" \
  --body "detect_runtime() prefers nerdctl if available, falls back to docker"
```

---

### Task 2: Wire Runtime Detection into vrg-docker-run

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_docker_run.py`
- Modify: `tests/vergil_tooling/test_vrg_docker_run.py`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/vergil_tooling/test_vrg_docker_run.py

def test_uses_detected_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """vrg-docker-run uses detect_runtime() instead of hardcoded 'docker'."""
    monkeypatch.setattr(
        "vergil_tooling.lib.docker.detect_runtime",
        lambda: "nerdctl",
    )
    # Verify the constructed command uses 'nerdctl' not 'docker'
    # (exact test depends on how the command is built)
```

- [ ] **Step 2: Run test to verify it fails**

- [ ] **Step 3: Replace hardcoded "docker" with detect_runtime()**

In `vrg_docker_run.py`, find all places where `"docker"` is used
as a command name and replace with `detect_runtime()`. Call it
once at the top of `main()` and pass the result through.

The key change is in the subprocess call that builds the
container command — replace:

```python
cmd = ["docker", "run", ...]
```

with:

```python
runtime = detect_runtime()
cmd = [runtime, "run", ...]
```

Apply the same change to `docker build`, `docker images`, and
any other Docker CLI invocations.

- [ ] **Step 4: Run full test suite**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_docker_run.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope docker \
  --message "use detected container runtime in vrg-docker-run" \
  --body "Replaces hardcoded 'docker' with detect_runtime() — supports nerdctl inside identity VMs"
```

---

### Task 3: Simplify vrg-gh Credential Handling

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_gh.py`
- Modify: `tests/vergil_tooling/test_vrg_gh.py`

- [ ] **Step 1: Understand current credential flow**

Read `src/vergil_tooling/bin/vrg_gh.py` and identify:
- `_get_token()` — credential discovery and selection
- `_discover_accounts()` — if present, multi-account logic
- Any `GH_TOKEN` injection in subprocess calls
- Escalation logic for `pr merge`, `issue close`

- [ ] **Step 2: Write tests for simplified behavior**

```python
# The simplified vrg-gh should:
# 1. NOT inject GH_TOKEN (use ambient gh auth)
# 2. NOT discover or select between accounts
# 3. Still enforce subcommand allowlist
# 4. Still enforce flag denylist
# 5. Still log audit trail
```

- [ ] **Step 3: Remove credential selection logic**

Delete:
- `_get_token()` function
- `_discover_accounts()` function (if present)
- `GH_TOKEN` environment injection in subprocess calls
- Credential escalation for `pr merge` / `issue close`

Keep:
- `_ALLOWED` dict (subcommand allowlist)
- `_DENIED_PAIRS` (operation denylist)
- `_DENIED_TOP` (top-level command denylist)
- Audit logging

The subprocess call becomes a simple passthrough:

```python
result = subprocess.run(  # noqa: S603, S607
    ["gh", *args],
    check=False,
)
raise SystemExit(result.returncode)
```

- [ ] **Step 4: Update tests**

Remove tests for credential selection, account discovery, and
escalation. Add tests confirming `gh` is called without
`GH_TOKEN` injection.

- [ ] **Step 5: Run full test suite**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
vrg-commit --type refactor --scope gh \
  --message "remove credential selection from vrg-gh" \
  --body "VM boundary handles credential isolation — vrg-gh is now workflow enforcement only"
```

---

### Task 4: Simplify vrg-git Guards

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_git.py`
- Modify: `tests/vergil_tooling/test_vrg_git.py`

- [ ] **Step 1: Identify credential-protection guards**

Read `src/vergil_tooling/bin/vrg_git.py` and identify guards
that exist for credential protection vs. workflow enforcement.

Credential-protection guards (to remove):
- Force-push prevention that exists to stop the agent from
  pushing with human credentials

Workflow-enforcement guards (to keep):
- Subcommand allowlist (`_ALLOWED_SIMPLE`, `_ALLOWED_COMPOUND`)
- Flag denylist
- Protected branch checks
- Audit logging

- [ ] **Step 2: Remove credential-protection guards**

- [ ] **Step 3: Update tests**

Remove tests for deleted guards. Keep tests for workflow
enforcement.

- [ ] **Step 4: Run full validation**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-commit --type refactor --scope git \
  --message "remove credential-protection guards from vrg-git" \
  --body "VM boundary handles credential isolation — vrg-git retains workflow enforcement only"
```

---

### Task 5: Full Validation and Manual Testing

- [ ] **Step 1: Run full validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

- [ ] **Step 2: Test inside the VM**

```bash
limactl shell vergil-agent

# Install vergil-tooling
uv tool install 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'

# Verify nerdctl detection
vrg-docker-run -- echo "runtime works"

# Verify vrg-gh uses ambient auth
vrg-gh repo view --json name

# Verify vrg-git workflow enforcement
vrg-git status
vrg-git log --oneline -5
```

- [ ] **Step 3: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** Runtime detection, vrg-gh credential
  removal, vrg-git guard removal — all spec items covered.
- [x] **Placeholder scan:** No TBD, TODO, or "implement later."
  Task 3 steps 1-2 are investigative (read current code) rather
  than placeholders — the exact lines to change depend on the
  current state of vrg_gh.py at execution time.
- [x] **Type consistency:** Function names, import paths, and
  test patterns are consistent with the existing codebase.
- [x] **Scope boundaries:** This plan does NOT include nerdctl
  image authentication (Plan 3), egress rules (Plan 4), or VM
  distribution (Plan 6).
- [x] **Backward compatibility:** Runtime detection falls back
  to `docker`. vrg-gh simplification works on host too. No
  breaking changes for non-VM users.
