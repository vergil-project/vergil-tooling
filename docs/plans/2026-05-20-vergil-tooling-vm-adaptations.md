# vergil-vm: vergil-tooling Adaptations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adapt vergil-tooling to detect nerdctl as the
container runtime inside identity VMs, falling back to docker
for host-side development.

**Architecture:** One focused change to vergil-tooling:
`vrg-docker-run` auto-detects `nerdctl` vs `docker` and uses
whichever is available. This is the only VM-specific adaptation
needed in vergil-tooling itself.

**Wrapper simplification (vrg-gh, vrg-git) has moved:** The
credential selection removal from `vrg-gh` and credential-guard
removal from `vrg-git` are now covered by the single-account
identity plan
(`docs/plans/2026-05-20-single-account-identity-tooling.md`,
#933). That plan implements GitHub App token exchange and
rewrites both wrappers as part of a broader identity model
change that applies on both the host and in the VM. This plan
originally included those changes as Tasks 3-4, but #933
supersedes them with a more comprehensive approach.

**Tech Stack:** Python (vergil-tooling)

**Specs:**
- `docs/specs/2026-05-20-vergil-vm-image-management-design.md`
  (#894) — Impact on vrg-docker-run section

**Decomposition:** This is Plan 5 of 6 for the identity VM
isolation system.

| Plan | Scope | Status |
|---|---|---|
| 1. Repository + Working VM | vergil-vm repo, Lima template | Planned |
| 2. Session Management | vrg-session, identities.toml | Planned |
| 3. Credential Provisioning | GitHub App credentials, GHCR auth | Planned |
| ~~4. Egress Filtering~~ | ~~HAProxy, pf, iptables~~ | Deferred to v2.2 (#901) |
| **5. vergil-tooling Adaptations** (this plan) | nerdctl runtime detection | This plan |
| 6. Distribution + Updates | Pre-built images, vrg-vm-update | Planned |
| — | Wrapper simplification (vrg-gh, vrg-git) | Moved to #933 identity plan |

**Repository:** vergil-tooling

**Depends on:** Plan 1 (working VM with nerdctl)

---

## Design

### Change Summary

| Component | What changes | What stays |
|---|---|---|
| `vrg-docker-run` | Auto-detect `nerdctl` or `docker` | All container operations, image building, volume mounts |
| `lib/docker.py` | Runtime detection function | Existing Docker subprocess helpers |

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

### Migration Strategy

Runtime detection falls back to `docker` if `nerdctl` is not
found — existing Docker Desktop users are unaffected. No
breaking changes for non-VM users.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/lib/docker.py` | Modify | Add `detect_runtime()` |
| `src/vergil_tooling/bin/vrg_docker_run.py` | Modify | Use detected runtime |
| `tests/vergil_tooling/test_docker.py` | Modify | Add runtime detection tests |
| `tests/vergil_tooling/test_vrg_docker_run.py` | Modify | Update for runtime detection |

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

### Task 3: Full Validation and Manual Testing

- [ ] **Step 1: Run full validation**

```bash
vrg-docker-run -- uv run vrg-validate
```

- [ ] **Step 2: Test inside the VM (requires working VM from Plan 1)**

```bash
limactl shell vergil-agent

# Install vergil-tooling
uv tool install 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'

# Verify nerdctl detection
vrg-docker-run -- echo "runtime works"
```

- [ ] **Step 3: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** Runtime detection — the only remaining
  spec item for this plan. Wrapper simplification (vrg-gh, vrg-git)
  is now tracked by the single-account identity plan (#933).
- [x] **Placeholder scan:** No TBD, TODO, or "implement later."
- [x] **Type consistency:** Function names, import paths, and
  test patterns are consistent with the existing codebase.
- [x] **Scope boundaries:** This plan does NOT include nerdctl
  image authentication (Plan 3), egress rules (Plan 4), VM
  distribution (Plan 6), or wrapper simplification (#933).
- [x] **Backward compatibility:** Runtime detection falls back
  to `docker`. No breaking changes for non-VM users.
