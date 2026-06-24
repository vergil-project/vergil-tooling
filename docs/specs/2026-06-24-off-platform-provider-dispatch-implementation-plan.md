# Off-platform Provider-Dispatch Abstraction (Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the minimal provider-dispatch seam (so `select_backend` routes off-platform specs by `spec.provider`) and a dev-fetch override for OpenTofu modules — the GCP-safe foundation that unblocks the Azure and Scaleway backends.

**Architecture:** Two small, independent changes in vergil-tooling: (1) `select_backend` branches on `spec.provider` within the off-platform path, returning the GCP backend for `"gcp"` and raising a clear error for any not-yet-implemented provider — this is the single file Azure/Scaleway later extend; (2) `fetch_modules` gains `VRG_MODULES_PATH` / `VRG_MODULES_REF` dev overrides so a backend can be developed/e2e-tested against an unreleased module before a vergil-vm v-tag exists. No engine/lifecycle refactor here.

**Tech Stack:** Python 3.12, pytest, OpenTofu (modules fetched from vergil-vm v-tag archives).

## Scope note (from the spec, refined)

The design spec (`docs/specs/2026-06-24-off-platform-scaleway-elastic-metal-design.md`, §2/§9/§10) folds an engine extraction into "Phase 1." This plan **deliberately defers the engine/lifecycle-interface extraction (§9) to Phase 3**, where the Scaleway backend provides the second concrete implementation to validate the abstraction against. Abstracting the lifecycle interface from a single (GCP) example would be speculative (YAGNI) and would touch the shipped GCP engine for no present consumer. Phase 1 ships only the dispatch seam (§2, dispatch half) + the dev-fetch override (§10) — both independently valuable, GCP-behavior-preserving, and sufficient to unblock Azure's collision point (`select_backend`).

## Global Constraints

- **Git/commits via wrappers only:** `vrg-git` (not `git`), `vrg-commit` (not `git commit`), `vrg-gh` (not `gh`). Raw `git`/`gh` are denied by the permission model.
- **Work entirely in the worktree:** `/Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1851-scaleway-elastic-metal/` on branch `feature/1851-scaleway-elastic-metal`.
- **GCP behavior unchanged:** the existing off-platform (GCP) path must behave identically; the full existing test suite stays green.
- **No silent failures:** an unknown provider raises; a set-but-invalid `VRG_MODULES_PATH` raises. No fallbacks that hide misconfig.
- **100% test coverage** is enforced by the gate — every new line/branch must be covered.
- **Dev-fetch override is dev-only:** with no override env set, `fetch_modules` resolves to the v-tag exactly as today (production path unchanged).
- **`select_backend` is the shared seam** Azure also edits — keep this diff small and in one clear region.

### Per-task workflow

- **Red/green feedback:** run the single test with `uv run pytest <path>::<TestClass>::<test> -v`.
- **Gate before every commit:** `vrg-container-run -- vrg-validate` (the canonical pipeline — lint, types, full test suite at 100% coverage, audit; it runs in this environment).
- **Commit:** `vrg-git add <files>` then `vrg-commit --type <feat|test> --scope off-platform --message "<desc> (#1851)" --body "<body>"` with the body ending in a real newline then `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## File Structure

- `src/vergil_tooling/lib/vm_backend.py` — **modify** `select_backend` to branch on `spec.provider`.
- `src/vergil_tooling/lib/vm_cloud.py` — **modify** `fetch_modules` (+ add `_MODULES_REF_URL`) for the dev-fetch override.
- `tests/vergil_tooling/test_vm_backend.py` — **modify** (`TestSelectBackend`): unknown-provider raises.
- `tests/vergil_tooling/test_vm_cloud.py` — **modify** (`TestFetchModules`): path/ref overrides.

---

## Task 1: Provider dispatch in `select_backend`

**Files:**
- Modify: `src/vergil_tooling/lib/vm_backend.py` (the `select_backend` body, currently lines 39–44)
- Test: `tests/vergil_tooling/test_vm_backend.py` (`TestSelectBackend`)

**Interfaces:**
- Consumes: `ComposedSpec.off_platform` (bool), `ComposedSpec.provider` (str); `OffPlatformBackend(spec, identity, org, repo, name)`.
- Produces: `select_backend(...)` raises `ValueError` for an off-platform spec whose `provider` is not a supported off-platform provider; `"gcp"` continues to return `OffPlatformBackend`. The provider branch is where Azure/Scaleway add their cases.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vm_backend.py` inside `TestSelectBackend` (the `_spec` helper already exists at module top):

```python
    def test_off_platform_unsupported_provider_raises(self) -> None:
        spec = _spec(
            "off-platform",
            provider="scaleway",  # not implemented yet in Phase 1
            region="fr-par-2",
            instance="EM-B230E-NVMe-128G",
            volume="0GiB",
        )
        with pytest.raises(ValueError, match="unsupported off-platform provider"):
            select_backend(spec, identity="vergil-user", org="o", repo="r")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vm_backend.py::TestSelectBackend::test_off_platform_unsupported_provider_raises -v`
Expected: FAIL — today `select_backend` ignores `provider` and returns an `OffPlatformBackend`, so no `ValueError` is raised.

- [ ] **Step 3: Add the provider branch**

In `src/vergil_tooling/lib/vm_backend.py`, replace the off-platform block in `select_backend`:

```python
    if spec.off_platform:
        if identity is None or org is None or repo is None:
            msg = "off-platform backend requires identity, org, and repo"
            raise ValueError(msg)
        return OffPlatformBackend(spec, identity, org, repo, name)
    return LimaBackend()
```

with:

```python
    if spec.off_platform:
        if identity is None or org is None or repo is None:
            msg = "off-platform backend requires identity, org, and repo"
            raise ValueError(msg)
        # Dispatch by provider. This branch is the seam additional off-platform
        # providers (azure, scaleway) extend with their own backend. (#1851)
        if spec.provider == "gcp":
            return OffPlatformBackend(spec, identity, org, repo, name)
        msg = f"unsupported off-platform provider {spec.provider!r} (supported: gcp)"
        raise ValueError(msg)
    return LimaBackend()
```

- [ ] **Step 4: Run the new test + the existing dispatch tests to verify all pass**

Run: `uv run pytest tests/vergil_tooling/test_vm_backend.py::TestSelectBackend -v`
Expected: PASS — the new unsupported-provider test plus the pre-existing `test_off_platform_returns_off_platform_backend` (provider `"gcp"`), `test_local_returns_lima_backend`, and `test_off_platform_requires_identity_org_repo`.

- [ ] **Step 5: Gate and commit**

```bash
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/lib/vm_backend.py tests/vergil_tooling/test_vm_backend.py
vrg-commit --type feat --scope off-platform \
  --message "dispatch off-platform backends by spec.provider (#1851)" \
  --body "select_backend branches on spec.provider: gcp returns the existing backend; any other provider raises a clear error. This is the seam the azure and scaleway backends extend.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Dev-fetch override in `fetch_modules`

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py` (`fetch_modules`, currently lines 72–98; add `_MODULES_REF_URL` near `_MODULES_URL` ~line 68)
- Test: `tests/vergil_tooling/test_vm_cloud.py` (`TestFetchModules`)

**Interfaces:**
- Consumes: `os.environ` (`VRG_MODULES_PATH`, `VRG_MODULES_REF`); existing `_MODULES_URL`, `_TAG_RE`.
- Produces: `fetch_modules(tag)` resolves, in order — `VRG_MODULES_PATH` (a local vergil-vm checkout → its `opentofu/modules`), `VRG_MODULES_REF` (a git ref/branch → `archive/{ref}.tar.gz`, tag validation skipped), else the existing v-tag path unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vm_cloud.py` inside `TestFetchModules`. Add an autouse fixture first so existing tests stay deterministic if these env vars leak from the shell, then the override tests:

```python
    @pytest.fixture(autouse=True)
    def _clear_module_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_MODULES_PATH", raising=False)
        monkeypatch.delenv("VRG_MODULES_REF", raising=False)

    def test_local_path_override_returns_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        modules = tmp_path / "checkout" / "opentofu" / "modules"
        modules.mkdir(parents=True)
        monkeypatch.setenv("VRG_MODULES_PATH", str(tmp_path / "checkout"))
        # No network: a bogus tag must be ignored when the path override wins.
        assert fetch_modules("not-a-tag") == modules

    def test_local_path_override_missing_dir_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VRG_MODULES_PATH", str(tmp_path / "checkout"))  # no opentofu/modules
        with pytest.raises(SystemExit):
            fetch_modules("v2.1.50")

    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_ref_override_builds_ref_url_and_skips_tag_check(
        self, mock_urlopen: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VRG_MODULES_REF", "feature/1851-scaleway-elastic-metal")
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"not-a-tarball"
        with pytest.raises(SystemExit):  # tar extraction of fake bytes fails loudly
            fetch_modules("not-a-tag")  # bogus tag proves the ref path skips _TAG_RE
        url = mock_urlopen.call_args[0][0]
        assert url == (
            "https://github.com/vergil-project/vergil-vm/archive/"
            "feature/1851-scaleway-elastic-metal.tar.gz"
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestFetchModules -v`
Expected: FAIL — `test_local_path_override_returns_modules` raises `SystemExit` (bogus tag rejected; override not implemented); the ref test builds the v-tag/`refs/tags` URL, not the `archive/<ref>` URL.

- [ ] **Step 3: Add the `_MODULES_REF_URL` constant**

In `src/vergil_tooling/lib/vm_cloud.py`, immediately after the `_MODULES_URL` definition (~line 68), add:

```python
# Dev-only override target: an arbitrary git ref (branch/tag/SHA) rather than a release
# v-tag, so an unreleased module can be fetched during development. GitHub's archive
# endpoint resolves branches/tags/SHAs here; the */opentofu/modules glob handles the
# ref-derived root dir name. (#1851)
_MODULES_REF_URL = "https://github.com/vergil-project/vergil-vm/archive/{ref}.tar.gz"
```

- [ ] **Step 4: Implement the override in `fetch_modules`**

In `src/vergil_tooling/lib/vm_cloud.py`, replace the start of `fetch_modules` (the docstring stays; replace from the `if not _TAG_RE...` validation down to the `url = _MODULES_URL.format(tag=tag)` line):

```python
    if not _TAG_RE.fullmatch(tag):
        print(f"ERROR: invalid module tag '{tag}' (expected vN.N or vN.N.N)", file=sys.stderr)
        raise SystemExit(1)
    url = _MODULES_URL.format(tag=tag)
```

with:

```python
    # Dev-only overrides (production passes neither and resolves the v-tag below):
    #   VRG_MODULES_PATH — a local vergil-vm checkout; use its opentofu/modules directly.
    #   VRG_MODULES_REF  — a git ref/branch; fetch its archive, skipping v-tag validation.
    # Lets a backend be developed against an unreleased module before a release tag. (#1851)
    local = os.environ.get("VRG_MODULES_PATH")
    if local:
        modules = Path(local) / "opentofu" / "modules"
        if not modules.is_dir():
            print(
                f"ERROR: VRG_MODULES_PATH set but {modules} is not a directory",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return modules
    ref = os.environ.get("VRG_MODULES_REF")
    if ref:
        url = _MODULES_REF_URL.format(ref=ref)
    else:
        if not _TAG_RE.fullmatch(tag):
            print(f"ERROR: invalid module tag '{tag}' (expected vN.N or vN.N.N)", file=sys.stderr)
            raise SystemExit(1)
        url = _MODULES_URL.format(tag=tag)
```

(`os` and `Path` are already imported in `vm_cloud.py`.)

- [ ] **Step 5: Run the override tests + the existing fetch tests to verify all pass**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestFetchModules -v`
Expected: PASS — the three new tests plus the pre-existing `test_rejects_bad_tag`, `test_builds_archive_url`, `test_builds_archive_url_two_segment_tag`, `test_returns_modules_path_when_present`, `test_missing_modules_dir_aborts` (no override env → v-tag path unchanged).

- [ ] **Step 6: Gate and commit**

```bash
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform \
  --message "add VRG_MODULES_PATH/VRG_MODULES_REF dev-fetch overrides (#1851)" \
  --body "fetch_modules can resolve modules from a local checkout or an arbitrary git ref, so a backend is testable against an unreleased module before a vergil-vm release tag exists. Production (no override) resolves the v-tag unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage (this phase only):**
- §2 dispatch half — provider branch in `select_backend` → Task 1.
- §10 dev-fetch override (`VRG_MODULES_PATH`/`VRG_MODULES_REF`) → Task 2.
- §9 engine extraction — **explicitly deferred to Phase 3** (scope note), validated there by the Scaleway backend. Azure's collision point (`select_backend`) is covered by Task 1.
- Provider-neutral phasing (§"Phased implementation", phase 1) — this plan is exactly that phase.

**2. Placeholder scan:** No TBD/TODO; every code step shows the full before/after; commands have expected output.

**3. Type consistency:** `select_backend(spec, *, identity, org, repo, name)` unchanged signature; `OffPlatformBackend(spec, identity, org, repo, name)` call unchanged. `fetch_modules(tag: str) -> Path` signature unchanged; new `_MODULES_REF_URL` formatted with `{ref}`. Env var names `VRG_MODULES_PATH` / `VRG_MODULES_REF` used identically in code and tests. No cross-task type references (the two tasks are independent).
