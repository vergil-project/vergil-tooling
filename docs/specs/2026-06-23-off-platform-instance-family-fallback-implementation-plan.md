# Instance-Family Fallback on Reattach — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On a reattach capacity stockout, recover by sweeping a curated, nested-virt-safe machine-family ladder within the pinned zone — without losing the data disk or tripping a spec-drift rebuild loop.

**Architecture:** A pure ladder function in `vm_cloud.py` derives same-shape family candidates. `OffPlatformBackend.vm_vars` gains an `instance_override` that swaps the tofu machine type *without* touching `self.spec` (so the stamped `SPEC_FINGERPRINT` stays the declared one). `apply_vm_with_zone_fallback` gains a family sweep that runs in the pinned zone before the existing zone sweep. The call site in `vrg_vm.py` populates the family ladder only on reattach (mirroring how `fallback_zones` is populated only on fresh create). The landed family is read back from `vm.tfstate` for `vrg-vm volumes`.

**Tech Stack:** Python 3.12, pytest, OpenTofu (GCP modules), `gcloud`.

## Global Constraints

- **Git/GitHub via wrappers only.** Use `vrg-git` (not `git`), `vrg-gh` (not `gh`), and `vrg-commit` (not `git commit`). Raw `git`/`gh` are denied by the permission model.
- **Work entirely in the worktree:** `/Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1836-instance-family-fallback/` on branch `feature/1836-instance-family-fallback`.
- **Conformance invariant (load-bearing):** never mutate `spec.instance`; the stamped fingerprint must always be `spec_fingerprint(self.spec)` of the *declared* spec. Violating this reintroduces the drift-rebuild loop the feature exists to prevent.
- **No silent failures:** a non-capacity error must abort; every fallback attempt logs to stderr.
- **Ladder validity is a human gate:** before merge, verify `NESTED_VIRT_FAMILIES` against GCP's current "nested virtualization — supported machine types" doc (confirm C2D/AMD; confirm every `FALLBACK_SHAPES × NESTED_VIRT_FAMILIES` type exists). Record the doc URL + date in the PR. The unit test is only a change-detector.
- **Scope:** reattach-only. Fresh-create keeps its existing zone sweep untouched.

### Per-task workflow

- **Red/green feedback:** run the single test with `uv run pytest <path>::<TestClass>::<test> -v`.
- **Gate before every commit:** `vrg-container-run -- vrg-validate` (the repo's only sanctioned validation pipeline — lint, types, full test suite).
- **Commit** with: `vrg-commit --type <feat|test|docs> --scope off-platform --message "<desc> (#1836)" --body "<body>\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"` after `vrg-git add <files>`.

---

## File Structure

- `src/vergil_tooling/lib/vm_cloud.py` — **modify.** Add the ladder (`NESTED_VIRT_FAMILIES`, `FALLBACK_SHAPES`, `instance_fallback_candidates`), the `instance_override` param on `vm_vars`, the family sweep in `apply_vm_with_zone_fallback`, and `parse_vm_machine_type`.
- `src/vergil_tooling/bin/vrg_vm.py` — **modify.** Add `_CloudState.fallback_instances`, populate it on reattach in `_cs_tofu_volume`, pass it through `_cs_tofu_vm`, and surface the landed family in `_volume_rows`/`_cmd_volumes`.
- `tests/vergil_tooling/test_vm_cloud.py` — **modify.** Ladder, `vm_vars` conformance, family-sweep, and `parse_vm_machine_type` tests.
- `tests/vergil_tooling/test_vrg_vm.py` — **modify.** Reattach-wiring and `_volume_rows` tests.

---

## Task 1: Family-fallback ladder (constants + candidate derivation)

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py` (add after `region_zones`, near the other capacity helpers)
- Test: `tests/vergil_tooling/test_vm_cloud.py`

**Interfaces:**
- Produces: `NESTED_VIRT_FAMILIES: tuple[str, ...]`, `FALLBACK_SHAPES: frozenset[str]`, `instance_fallback_candidates(requested: str) -> list[str]` (requested type first, then same-shape ladder siblings, deduped; `[requested]` only when the shape is unsupported).

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vm_cloud.py`. Add `instance_fallback_candidates`, `NESTED_VIRT_FAMILIES`, `FALLBACK_SHAPES` to the existing `from vergil_tooling.lib.vm_cloud import (...)` block first.

```python
class TestInstanceFallbackLadder:
    def test_requested_first_then_same_shape_siblings(self) -> None:
        assert instance_fallback_candidates("n2-standard-8") == [
            "n2-standard-8",
            "n2d-standard-8",
            "c2-standard-8",
            "c2d-standard-8",
        ]

    def test_dedups_when_requested_family_in_ladder(self) -> None:
        # n2d is in the ladder; it must appear once, still requested-first.
        result = instance_fallback_candidates("n2d-standard-16")
        assert result[0] == "n2d-standard-16"
        assert result.count("n2d-standard-16") == 1
        assert set(result) == {
            "n2d-standard-16",
            "n2-standard-16",
            "c2-standard-16",
            "c2d-standard-16",
        }

    def test_unsupported_shape_yields_no_fallback(self) -> None:
        assert instance_fallback_candidates("n2-highmem-8") == ["n2-highmem-8"]
        assert instance_fallback_candidates("n2-standard-4") == ["n2-standard-4"]

    def test_requested_family_not_in_ladder_still_leads(self) -> None:
        # A misconfigured non-nested-virt family: original first, then full ladder.
        assert instance_fallback_candidates("e2-standard-8") == [
            "e2-standard-8",
            "n2-standard-8",
            "n2d-standard-8",
            "c2-standard-8",
            "c2d-standard-8",
        ]

    def test_ladder_change_detector(self) -> None:
        # NOT a validity proof — pins the curated values so an edit is deliberate.
        # Real nested-virt validity is verified by hand against GCP docs (#1836).
        assert NESTED_VIRT_FAMILIES == ("n2", "n2d", "c2", "c2d")
        assert FALLBACK_SHAPES == frozenset({"standard-8", "standard-16"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestInstanceFallbackLadder -v`
Expected: FAIL — `ImportError` / `cannot import name 'instance_fallback_candidates'`.

- [ ] **Step 3: Implement the ladder**

Add to `src/vergil_tooling/lib/vm_cloud.py` immediately after the `region_zones` function:

```python
# --- Instance-family fallback ladder (#1836) ---------------------------------
#
# A capacity stockout is specific to a (zone, machine-family) pair: a different
# family in the same zone often has capacity when the requested one does not. On a
# reattach the zonal data disk pins the zone, so swapping the family is the only
# recovery (see apply_vm_with_zone_fallback). The ladder may contain ONLY families
# that support GCP nested virtualization — nested KVM is the point of these VMs, and
# a family without it (e2, Tau) would boot a box with no /dev/kvm and fail the
# provision. Membership/order is verified BY HAND against GCP's nested-virt
# supported-machine-types doc before merge; the unit test is only a change-detector.
NESTED_VIRT_FAMILIES = ("n2", "n2d", "c2", "c2d")

# Shapes verified to exist for EVERY family in the ladder and actually run
# off-platform. Family-fallback engages only for these, so we never synthesize an
# invalid machine type. Adding a size is one line here.
FALLBACK_SHAPES = frozenset({"standard-8", "standard-16"})


def instance_fallback_candidates(requested: str) -> list[str]:
    """Ordered machine types to try for ``requested``, the requested type first.

    Splits ``requested`` into ``(family, shape)`` (``n2-standard-8`` ->
    ``("n2", "standard-8")``). When the shape is in ``FALLBACK_SHAPES`` the result is
    the requested type, then every other ``NESTED_VIRT_FAMILIES`` member at the same
    shape, deduped. When the shape is unsupported the result is just ``[requested]``
    (no fallback). If the requested family is not in the ladder (e.g. a misconfigured
    ``e2`` declared with nested virt) the requested type still leads and the full
    ladder follows, so fallback still reaches the nested-virt-safe families.
    """
    _family, _, shape = requested.partition("-")
    if not shape or shape not in FALLBACK_SHAPES:
        return [requested]
    candidates = [requested]
    for family in NESTED_VIRT_FAMILIES:
        candidate = f"{family}-{shape}"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestInstanceFallbackLadder -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Gate and commit**

```bash
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform \
  --message "add nested-virt-safe instance-family fallback ladder (#1836)" \
  --body "instance_fallback_candidates derives same-shape family siblings for a requested machine type, gated on FALLBACK_SHAPES so no invalid type is ever synthesized. The change-detector test pins the curated values; real nested-virt validity is a manual GCP-doc gate.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `instance_override` on `vm_vars` (conformance-preserving)

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py` — `OffPlatformBackend.vm_vars` (currently `def vm_vars(self, *, zone: str, volume_id: str)`)
- Test: `tests/vergil_tooling/test_vm_cloud.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `OffPlatformBackend.vm_vars(self, *, zone: str, volume_id: str, instance_override: str | None = None) -> dict[str, object]` — sets `"instance_type": instance_override or self.spec.instance`; `provision_env`'s `SPEC_FINGERPRINT` is always `spec_fingerprint(self.spec)` of the unmutated declared spec.

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_vm_cloud.py`. Ensure `from vergil_tooling.lib.vm_spec import spec_fingerprint` is imported at the top (add if missing); `dataclasses` is already imported.

```python
class TestVmVarsInstanceOverride:
    def test_override_swaps_machine_type_but_keeps_declared_fingerprint(self) -> None:
        spec = _off_spec(instance="n2-standard-8")
        b = OffPlatformBackend(spec, "vergil-user", "o", "r")
        declared_fp = spec_fingerprint(spec)
        would_be_landed_fp = spec_fingerprint(
            dataclasses.replace(spec, instance="n2d-standard-8")
        )

        v = b.vm_vars(zone="us-central1-f", volume_id="v1", instance_override="n2d-standard-8")

        # The tofu machine type is the fallback family...
        assert v["instance_type"] == "n2d-standard-8"
        # ...but the stamped fingerprint is the DECLARED one, never the landed family's.
        assert declared_fp in str(v["provision_env"])
        assert would_be_landed_fp not in str(v["provision_env"])
        # ...and the spec object is never mutated.
        assert b.spec.instance == "n2-standard-8"

    def test_default_uses_declared_instance(self) -> None:
        b = OffPlatformBackend(_off_spec(instance="n2-standard-16"), "vergil-user", "o", "r")
        v = b.vm_vars(zone="us-central1-b", volume_id="v1")
        assert v["instance_type"] == "n2-standard-16"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestVmVarsInstanceOverride -v`
Expected: FAIL — `TypeError: vm_vars() got an unexpected keyword argument 'instance_override'`.

- [ ] **Step 3: Add the parameter**

In `src/vergil_tooling/lib/vm_cloud.py`, change `OffPlatformBackend.vm_vars`. Replace:

```python
    def vm_vars(self, *, zone: str, volume_id: str) -> dict[str, object]:
        provision_env = render_provision_env(
```

with:

```python
    def vm_vars(
        self, *, zone: str, volume_id: str, instance_override: str | None = None
    ) -> dict[str, object]:
        provision_env = render_provision_env(
```

Then, in the same method's returned dict, replace:

```python
            "instance_type": self.spec.instance,
```

with:

```python
            # A family fallback swaps the machine type here ONLY; self.spec is never
            # mutated, so fingerprint=spec_fingerprint(self.spec) above stays the
            # declared hash and the drift check never reads a fallback as drift. (#1836)
            "instance_type": instance_override or self.spec.instance,
```

(Leave the `fingerprint=spec_fingerprint(self.spec)` line above exactly as-is — that is the conformance invariant.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestVmVarsInstanceOverride -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Gate and commit**

```bash
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform \
  --message "let vm_vars override the machine type without changing the fingerprint (#1836)" \
  --body "instance_override swaps only the tofu instance_type; SPEC_FINGERPRINT stays computed from the unmutated declared spec, so a family fallback is conformant by construction.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Family sweep in `apply_vm_with_zone_fallback`

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py` — `apply_vm_with_zone_fallback`
- Test: `tests/vergil_tooling/test_vm_cloud.py`

**Interfaces:**
- Consumes: `instance_fallback_candidates` (Task 1) is *not* called here — the caller passes the ready list; `vm_vars(..., instance_override=...)` (Task 2).
- Produces: `apply_vm_with_zone_fallback(..., *, zone, volume_id, fallback_zones, fallback_instances: list[str] | None = None)`. On a capacity error it sweeps `fallback_instances` in the pinned zone (same `volume_id`, no `destroy_volume`), then the existing `fallback_zones` sweep. With neither list it re-raises the original error (unchanged reattach-with-nothing behavior).

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vm_cloud.py` (the existing `TestZoneFallback`, `_capacity_exc` helper, and imports are already present):

```python
class TestFamilyFallback:
    @staticmethod
    def _backend() -> MagicMock:
        backend = MagicMock()
        backend.vm_vars.return_value = {}
        backend.spec.region = "us-central1"
        backend.spec.instance = "n2-standard-8"
        return backend

    def test_swaps_family_in_same_zone_without_touching_volume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requested family stocked, first fallback family lands — same zone, same disk.
        av = MagicMock(side_effect=[_capacity_exc(), {"host": "h"}])
        dv = MagicMock()
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.apply_vm", av)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.destroy_volume", dv)
        backend = self._backend()

        result = apply_vm_with_zone_fallback(
            tmp_path / "m",
            tmp_path / "s",
            backend,
            zone="us-central1-f",
            volume_id="v1",
            fallback_zones=[],
            fallback_instances=["n2d-standard-8", "c2-standard-8"],
        )

        assert result == ("v1", "us-central1-f", {"host": "h"})
        assert av.call_count == 2
        dv.assert_not_called()  # the data disk is never destroyed on this path
        backend.vm_vars.assert_any_call(
            zone="us-central1-f", volume_id="v1", instance_override="n2d-standard-8"
        )

    def test_all_families_stocked_raises_naming_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=_capacity_exc())
        )
        with pytest.raises(RuntimeError, match="no nested-virt machine family has capacity"):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-f",
                volume_id="v1",
                fallback_zones=[],
                fallback_instances=["n2d-standard-8", "c2-standard-8"],
            )

    def test_non_capacity_error_during_family_sweep_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boom = subprocess.CalledProcessError(1, [], stderr="Error: bad config")
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm",
            MagicMock(side_effect=[_capacity_exc(), boom]),
        )
        with pytest.raises(subprocess.CalledProcessError):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-f",
                volume_id="v1",
                fallback_zones=[],
                fallback_instances=["n2d-standard-8", "c2-standard-8"],
            )

    def test_capacity_with_no_fallbacks_at_all_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reattach of an unsupported shape: no families, no zones -> original error.
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=_capacity_exc())
        )
        with pytest.raises(subprocess.CalledProcessError):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-f",
                volume_id="v1",
                fallback_zones=[],
                fallback_instances=[],
            )
```

- [ ] **Step 2: Run the new tests AND the existing ones to confirm they fail/still pass**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestFamilyFallback -v`
Expected: FAIL — `TypeError: apply_vm_with_zone_fallback() got an unexpected keyword argument 'fallback_instances'`.

- [ ] **Step 3: Add the family sweep**

In `src/vergil_tooling/lib/vm_cloud.py`, replace the entire body of `apply_vm_with_zone_fallback` (from its `def` through its final `raise RuntimeError(msg)`) with:

```python
def apply_vm_with_zone_fallback(
    modules_root: Path,
    state_dir: Path,
    backend: OffPlatformBackend,
    *,
    zone: str,
    volume_id: str,
    fallback_zones: list[str],
    fallback_instances: list[str] | None = None,
) -> tuple[str, str, dict[str, str]]:
    """Apply the VM in ``zone``; on a capacity stockout, recover by either swapping the
    machine family in the SAME zone (reattach — the zonal data disk pins the zone) or,
    on a fresh create, recreating the empty volume in each ``fallback_zones`` entry.

    ``fallback_instances`` (the ladder minus the requested type, from
    ``instance_fallback_candidates``) is supplied on a reattach; ``fallback_zones`` on a
    fresh create. The two are mutually exclusive today. Family fallback keeps
    ``volume_id`` untouched — the disk holds data and never moves — whereas the zone
    sweep destroys the *empty* disk to relocate it. With neither list a capacity error
    is fatal. (#1813, #1836)
    """
    instances = list(fallback_instances or [])
    tried_zones = [zone]
    tried_instances = [backend.spec.instance]
    vm_vars = cast("dict[str, Any]", backend.vm_vars(zone=zone, volume_id=volume_id))
    try:
        return volume_id, zone, apply_vm(modules_root, state_dir, **vm_vars)
    except subprocess.CalledProcessError as exc:
        if not is_zone_capacity_error(exc):
            raise
        if not instances and not fallback_zones:
            raise  # reattach with no ladder (unsupported shape) — original behavior

    # Family fallback — same zone, same volume (a zonal disk with data cannot move).
    for instance in instances:
        print(
            f"  zone {zone}: {tried_instances[-1]} out of capacity — trying {instance}...",
            file=sys.stderr,
        )
        tried_instances.append(instance)
        vm_vars = cast(
            "dict[str, Any]",
            backend.vm_vars(zone=zone, volume_id=volume_id, instance_override=instance),
        )
        try:
            return volume_id, zone, apply_vm(modules_root, state_dir, **vm_vars)
        except subprocess.CalledProcessError as exc:
            if not is_zone_capacity_error(exc):
                raise

    # Zone fallback — fresh create only; recreate the empty disk in each next zone.
    for next_zone in fallback_zones:
        print(f"  zone {zone}: no capacity — trying {next_zone}...", file=sys.stderr)
        destroy_volume(modules_root, state_dir)  # the empty disk; rmtrees the state dir
        state_dir.mkdir(parents=True, exist_ok=True)
        volume_vars = cast("dict[str, Any]", {**backend.volume_vars(), "zone": next_zone})
        volume_id, zone = apply_volume(modules_root, state_dir, **volume_vars)
        tried_zones.append(zone)
        vm_vars = cast("dict[str, Any]", backend.vm_vars(zone=zone, volume_id=volume_id))
        try:
            return volume_id, zone, apply_vm(modules_root, state_dir, **vm_vars)
        except subprocess.CalledProcessError as exc:
            if not is_zone_capacity_error(exc):
                raise

    if len(tried_instances) > 1:
        msg = (
            f"no nested-virt machine family has capacity in {zone} "
            f"(tried: {', '.join(tried_instances)}). Wait for capacity, raise quota, "
            "or try another region."
        )
    else:
        msg = (
            f"no zone in {backend.spec.region} has capacity for {backend.spec.instance} "
            f"(tried: {', '.join(tried_zones)}). Try a different instance family "
            "(e.g. n2d-*), another region, or wait for capacity."
        )
    raise RuntimeError(msg)
```

- [ ] **Step 4: Run the new and existing fallback tests to verify all pass**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestFamilyFallback tests/vergil_tooling/test_vm_cloud.py::TestZoneFallback -v`
Expected: PASS — all `TestFamilyFallback` (4) and all pre-existing `TestZoneFallback` tests (the zone message and `test_capacity_with_no_fallback_reraises` behavior are preserved).

- [ ] **Step 5: Gate and commit**

```bash
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform \
  --message "sweep nested-virt machine families in the pinned zone on a reattach stockout (#1836)" \
  --body "apply_vm_with_zone_fallback tries each fallback_instances candidate in the same zone with the same volume before the existing zone sweep. The data disk is never destroyed on this path; a non-capacity error still aborts; with no ladder and no zones the original error re-raises.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the ladder at the call site (reattach-only)

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm.py` — `_CloudState` dataclass, `_cs_tofu_volume`, `_cs_tofu_vm`
- Test: `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Consumes: `vm_cloud.instance_fallback_candidates` (Task 1); `apply_vm_with_zone_fallback(..., fallback_instances=...)` (Task 3).
- Produces: `_CloudState.fallback_instances: list[str]`, populated only when `volume.tfstate` exists (reattach).

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_vrg_vm.py`, inside the existing `TestCloudStageGuards` class (it already has `_state`, and `_cs_tofu_volume` is imported):

```python
    def test_tofu_volume_reattach_populates_family_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reattach: zone is pinned, so the recovery is a machine-family sweep (#1836).
        state = self._state(tmp_path)
        state.modules_root = tmp_path / "modules"
        state.state_dir.mkdir(parents=True, exist_ok=True)
        (state.state_dir / "volume.tfstate").write_text("{}")
        monkeypatch.setattr(
            "vergil_tooling.bin.vrg_vm.vm_cloud.apply_volume",
            MagicMock(return_value=("vol-1", "us-central1-b")),
        )
        _cs_tofu_volume(state)
        expected = vm_cloud.instance_fallback_candidates(state.backend.spec.instance)[1:]
        assert state.fallback_instances == expected
        assert state.fallback_zones == []

    def test_tofu_volume_fresh_create_leaves_family_fallback_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fresh create keeps the zone sweep and never engages family fallback.
        state = self._state(tmp_path)
        state.modules_root = tmp_path / "modules"
        monkeypatch.setattr(
            "vergil_tooling.bin.vrg_vm.vm_cloud.apply_volume",
            MagicMock(return_value=("vol-1", "us-central1-b")),
        )
        monkeypatch.setattr(
            "vergil_tooling.bin.vrg_vm.vm_cloud.region_zones",
            MagicMock(return_value=["us-central1-a", "us-central1-b"]),
        )
        _cs_tofu_volume(state)
        assert state.fallback_instances == []
```

Confirm `vm_cloud` is imported in the test module (it is used elsewhere); if not, add `from vergil_tooling.lib import vm_cloud`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestCloudStageGuards::test_tofu_volume_reattach_populates_family_fallback tests/vergil_tooling/test_vrg_vm.py::TestCloudStageGuards::test_tofu_volume_fresh_create_leaves_family_fallback_empty -v`
Expected: FAIL — `AttributeError: '_CloudState' object has no attribute 'fallback_instances'`.

- [ ] **Step 3a: Add the `_CloudState` field**

In `src/vergil_tooling/bin/vrg_vm.py`, in the `_CloudState` dataclass, immediately after the `fallback_zones` field, add:

```python
    # Machine families to try (same pinned zone) if a reattach VM apply hits a capacity
    # stockout — the ladder minus the requested type. Populated only on a reattach; empty
    # on a fresh create, which sweeps fallback_zones instead. (#1836)
    fallback_instances: list[str] = field(default_factory=list)
```

- [ ] **Step 3b: Populate it on reattach**

In `_cs_tofu_volume`, the current fresh-create block reads:

```python
    if not (state.state_dir / "volume.tfstate").exists():
        candidates = _candidate_zones(state.backend)
        if candidates:
            volume_vars["zone"] = candidates[0]
            state.fallback_zones = candidates[1:]
    volume_id, zone = vm_cloud.apply_volume(modules_root, state.state_dir, **volume_vars)
```

Replace it with:

```python
    if not (state.state_dir / "volume.tfstate").exists():
        candidates = _candidate_zones(state.backend)
        if candidates:
            volume_vars["zone"] = candidates[0]
            state.fallback_zones = candidates[1:]
    else:
        # Reattach: the zonal disk pins the zone, so recovery is a machine-family
        # sweep in that zone rather than a zone sweep. (#1836)
        state.fallback_instances = vm_cloud.instance_fallback_candidates(
            state.backend.spec.instance
        )[1:]
    volume_id, zone = vm_cloud.apply_volume(modules_root, state.state_dir, **volume_vars)
```

- [ ] **Step 3c: Pass it through `_cs_tofu_vm`**

In `_cs_tofu_vm`, the call currently reads:

```python
    volume_id, zone, out = vm_cloud.apply_vm_with_zone_fallback(
        modules_root,
        state.state_dir,
        state.backend,
        zone=state.zone,
        volume_id=state.volume_id,
        fallback_zones=state.fallback_zones,
    )
```

Add the `fallback_instances` argument:

```python
    volume_id, zone, out = vm_cloud.apply_vm_with_zone_fallback(
        modules_root,
        state.state_dir,
        state.backend,
        zone=state.zone,
        volume_id=state.volume_id,
        fallback_zones=state.fallback_zones,
        fallback_instances=state.fallback_instances,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestCloudStageGuards -v`
Expected: PASS — the two new tests plus the pre-existing stage-guard tests.

- [ ] **Step 5: Gate and commit**

```bash
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope off-platform \
  --message "engage instance-family fallback on reattach VM applies (#1836)" \
  --body "_CloudState gains fallback_instances, populated only when volume.tfstate exists (reattach) and threaded into apply_vm_with_zone_fallback. Fresh create is unchanged and keeps its zone sweep.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Surface the landed family from `vm.tfstate` in `vrg-vm volumes`

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py` — add `parse_vm_machine_type`
- Modify: `src/vergil_tooling/bin/vrg_vm.py` — `_volume_rows`, `_cmd_volumes`
- Test: `tests/vergil_tooling/test_vm_cloud.py`, `tests/vergil_tooling/test_vrg_vm.py`

**Interfaces:**
- Produces: `parse_vm_machine_type(state_file: Path) -> str | None` — the bare machine type from a `vm.tfstate`'s `google_compute_instance.vm`, or `None`. `_volume_rows` rows gain a `"vm_type"` key.

- [ ] **Step 1a: Write the failing parser test**

Add to `tests/vergil_tooling/test_vm_cloud.py` (add `parse_vm_machine_type` to the import block):

```python
class TestParseVmMachineType:
    def _state(self, machine_type: str) -> str:
        return json.dumps(
            {
                "resources": [
                    {
                        "type": "google_compute_instance",
                        "instances": [{"attributes": {"machine_type": machine_type}}],
                    }
                ]
            }
        )

    def test_returns_bare_type_from_selflink(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(self._state("projects/p/zones/us-central1-f/machineTypes/n2d-standard-8"))
        assert parse_vm_machine_type(f) == "n2d-standard-8"

    def test_returns_bare_type_when_already_bare(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(self._state("n2-standard-8"))
        assert parse_vm_machine_type(f) == "n2-standard-8"

    def test_none_when_absent_or_empty(self, tmp_path: Path) -> None:
        assert parse_vm_machine_type(tmp_path / "missing.tfstate") is None
        empty = tmp_path / "vm.tfstate"
        empty.write_text("{}")
        assert parse_vm_machine_type(empty) is None
```

`json` is already imported in `test_vm_cloud.py` (used elsewhere); confirm and add if missing.

- [ ] **Step 1b: Run it to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestParseVmMachineType -v`
Expected: FAIL — `cannot import name 'parse_vm_machine_type'`.

- [ ] **Step 2: Implement the parser**

Add to `src/vergil_tooling/lib/vm_cloud.py`, immediately after `parse_volume_state`:

```python
def parse_vm_machine_type(state_file: Path) -> str | None:
    """Return the bare machine type from a ``vm.tfstate``'s instance, or ``None``.

    Mirrors ``parse_volume_state``: ``None`` when the file is absent, unreadable,
    malformed, or carries no applied ``google_compute_instance``. ``machine_type`` may
    be a bare type or a full selfLink — normalize to the bare name. This is the single
    source of truth for the family a reattach fallback actually landed on. (#1836)
    """
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for resource in data.get("resources", []):
        if not isinstance(resource, dict) or resource.get("type") != "google_compute_instance":
            continue
        instances = resource.get("instances") or []
        if not instances or not isinstance(instances[0], dict):
            continue
        attrs = instances[0].get("attributes")
        if not isinstance(attrs, dict):
            continue
        machine_type = attrs.get("machine_type")
        if not machine_type:
            return None
        return str(machine_type).rsplit("/", 1)[-1]
    return None
```

- [ ] **Step 3: Run the parser test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_vm_cloud.py::TestParseVmMachineType -v`
Expected: PASS (3 tests).

- [ ] **Step 4: Write the failing `_volume_rows` test**

Add to `tests/vergil_tooling/test_vrg_vm.py`, inside the existing `TestListRows` class (or near `_volume_rows` tests). It writes a `volume.tfstate` and a sibling `vm.tfstate` and asserts the row carries the realized machine type:

```python
    def test_volume_rows_include_landed_machine_type(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        provider = tmp_path / ".config" / "vergil" / "tofu" / "vergil-lmf-cloud" / "gcp"
        provider.mkdir(parents=True)
        (provider / "volume.tfstate").write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "type": "google_compute_disk",
                            "instances": [
                                {"attributes": {"name": "vergil-lmf-cloud-data", "size": 300,
                                                "zone": "us-central1-f", "labels": {}}}
                            ],
                        }
                    ]
                }
            )
        )
        (provider / "vm.tfstate").write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "type": "google_compute_instance",
                            "instances": [{"attributes": {"machine_type": "n2d-standard-8"}}],
                        }
                    ]
                }
            )
        )
        rows = _volume_rows()
        assert rows
        assert rows[0]["vm_type"] == "n2d-standard-8"
```

Confirm `_volume_rows` and `json` are imported in `test_vrg_vm.py`; add `from vergil_tooling.bin.vrg_vm import _volume_rows` / `import json` if missing.

- [ ] **Step 5: Run it to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestListRows::test_volume_rows_include_landed_machine_type -v`
Expected: FAIL — `KeyError: 'vm_type'`.

- [ ] **Step 6: Wire `vm_type` into `_volume_rows`**

In `src/vergil_tooling/bin/vrg_vm.py`, in `_volume_rows`, inside the `for volume_state in sorted(...)` loop, after `provider = provider_dir.name`, add:

```python
        vm_type = vm_cloud.parse_vm_machine_type(provider_dir / "vm.tfstate") or "—"
```

Then add `"vm_type": vm_type,` to **both** dicts appended to `rows` (the `parsed is None` placeholder dict and the populated dict).

- [ ] **Step 7: Run the `_volume_rows` test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_vrg_vm.py::TestListRows::test_volume_rows_include_landed_machine_type -v`
Expected: PASS.

- [ ] **Step 8: Add the display column**

In `_cmd_volumes`, update the header and line to add a `VM TYPE` column. Replace:

```python
    header = (
        f"{'IDENTITY':<14} {'ORG/REPO':<{scope_w}} {'DISK NAME':<{name_w}} "
        f"{'SIZE':<8} {'ZONE':<16} {'REGION':<14}"
    )
```

with:

```python
    header = (
        f"{'IDENTITY':<14} {'ORG/REPO':<{scope_w}} {'DISK NAME':<{name_w}} "
        f"{'SIZE':<8} {'ZONE':<16} {'REGION':<14} {'VM TYPE':<16}"
    )
```

and replace:

```python
        line = (
            f"{r['identity']!s:<14} {r['scope']!s:<{scope_w}} {r['name']!s:<{name_w}} "
            f"{r['size']!s:<8} {r['zone']!s:<16} {r['region']!s:<14}"
        )
```

with:

```python
        line = (
            f"{r['identity']!s:<14} {r['scope']!s:<{scope_w}} {r['name']!s:<{name_w}} "
            f"{r['size']!s:<8} {r['zone']!s:<16} {r['region']!s:<14} {r['vm_type']!s:<16}"
        )
```

- [ ] **Step 9: Gate and commit**

```bash
vrg-container-run -- vrg-validate
vrg-git add src/vergil_tooling/lib/vm_cloud.py src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vm_cloud.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope off-platform \
  --message "surface the landed machine family in 'vrg-vm volumes' from vm.tfstate (#1836)" \
  --body "parse_vm_machine_type reads the realized machine type from vm.tfstate; vrg-vm volumes shows it in a VM TYPE column so a reattach that fell back to another family is visible. Single source of truth, no sidecar, no drift.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final: ladder verification + PR

- [ ] **Manual GCP-doc verification (the real ladder gate).** Open GCP's current "nested virtualization — supported machine types" doc. Confirm each of `n2`, `n2d`, `c2`, `c2d` supports nested virtualization, and that `n2-/n2d-/c2-/c2d-standard-8` and `-standard-16` all exist. If `c2d` (AMD) is not supported, remove it from `NESTED_VIRT_FAMILIES` and update the `test_ladder_change_detector` expectation. Record the doc URL + date in the PR description.
- [ ] **Open the PR** via the repo's submission workflow (e.g. `vrg-submit-pr`), referencing `#1836`, linking the design spec, and noting the manual verification result.

---

## Self-Review

**1. Spec coverage:**
- Ladder (one family list + `FALLBACK_SHAPES`, candidate derivation, edge case) → Task 1.
- `FALLBACK_SHAPES` gate against invalid types → Task 1 (`test_unsupported_shape_yields_no_fallback`).
- Ladder-aware conformance / no rebuild loop (instance_override; fingerprint from declared spec; no spec mutation) → Task 2.
- Reattach-only hook via `_CloudState.fallback_instances` gated on `volume.tfstate` existing → Task 4.
- Family sweep in pinned zone, volume never destroyed, helpful exhaustion error, non-capacity aborts → Task 3.
- No silent fallback / loud logging → Task 3 (stderr prints).
- Landed family surfaced from `vm.tfstate` (no meta sidecar; avoids #1831 collision) → Task 5.
- Change-detector test + manual GCP-doc gate (not a tautological validity test) → Task 1 + Final.
- Fresh-create path unchanged → Task 3 (backward-compat tests) + Task 4 (`test_tofu_volume_fresh_create_leaves_family_fallback_empty`).

**2. Placeholder scan:** No TBD/TODO; every code step shows complete code; the only deferred item is the *intentional* manual GCP-doc verification, called out as a human gate.

**3. Type consistency:** `instance_fallback_candidates(requested: str) -> list[str]` produced in Task 1, consumed in Task 4. `vm_vars(..., instance_override=...)` produced in Task 2, consumed in Task 3. `apply_vm_with_zone_fallback(..., fallback_instances=...)` produced in Task 3, consumed in Task 4. `_CloudState.fallback_instances: list[str]` defined in Task 4. `parse_vm_machine_type` produced in Task 5, consumed in `_volume_rows` same task. Names and signatures match across tasks.
