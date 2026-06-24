# Off-platform: instance-family fallback on reattach

- **Issue:** vergil-tooling#1836
- **Status:** Design (approved, pushback-reviewed)
- **Related:** #1797 / PR #1799 (explicit zone knob), #1804 / PR #1807 (orphan-firewall rollback), #1813 / PR #1816 (zone fallback on fresh create)
- **Coordination:** #1831 (named instances) — in flight, reshaping the meta sidecar; see Design §4.

## Problem

An off-platform VM apply that **reattaches an existing data disk** is pinned to
that disk's zone — a zonal `pd-ssd` can only attach to a VM in the same zone.
When the pinned zone is out of capacity for the requested machine type, the apply
fails with a transient capacity stockout:

```
Error waiting for instance to create: The zone '…' does not have enough
resources available to fulfill the request.  (ZONE_RESOURCE_POOL_EXHAUSTED)
```

`apply_vm_with_zone_fallback` already recovers from this on the **fresh-volume
create** path by sweeping zones (#1813). But on the **reattach** path it passes
`fallback_zones=[]` on purpose — a zonal disk cannot move zones without losing
its data — so a reattach has no recovery at all and hard-fails.

In practice a *different machine family* in the **same** zone very often has
capacity when the requested family does not (a stockout is specific to a
`(zone, family)` pair). The tool never tries one. An operator whose data disk
already exists is therefore stuck until the exact pinned `(zone, family)` combo
frees up — which can be an open-ended wait.

## Goal

On a reattach capacity stockout, sweep a curated set of machine **families** in
the **same pinned zone** before giving up. Preserve the data disk untouched.
Do not introduce a spec-drift rebuild loop. Keep the change minimal and
reattach-only for v1.

## Non-goals (v1)

- Changing the fresh-create path (it keeps the existing zone fallback, #1813).
- Snapshot/restore-based zone migration of a disk that already holds data.
- Regional persistent disks.
- A runtime nested-virtualization guard (the curated static ladder is the
  control; see "Alternatives considered").
- Dynamically querying GCP for capacity or supported machine types.
- `highmem` / other shapes beyond those actually in use.

## Constraint that shapes everything: nested virtualization

Nested KVM is the entire point of these VMs — `templates/provision/70-nested-virt.sh`
**fails the provision loudly** if `/dev/kvm` never appears, and the GCP VM module
sets `enable_nested_virtualization`. Therefore the fallback ladder may contain
**only machine families that support GCP nested virtualization.** `e2` and the
Tau families (`t2d`/`t2a`) do not, so a fallback onto them would produce a VM
with no `/dev/kvm` and a failed provision — strictly worse than the stockout.

## Design

### 1. The ladder — one family list, size preserved

The family and the size are independent axes; the fallback moves only along the
**family** axis and carries the requested **size** unchanged. So a single
ordered family list, not a per-shape table:

```python
# Ordered nested-virt-capable families, by preference. Size-independent —
# a fallback swaps the family and keeps the requested shape untouched.
# Intel-only: GCE nested virt excludes AMD/Arm, so n2d/c2d are NOT eligible
# (verified against GCP docs — see "Verify before shipping").
NESTED_VIRT_FAMILIES = ["n2", "c2"]

# Shapes verified to exist across the whole ladder and actually run off-platform.
# Family-fallback engages only for these; adding a size later is one line.
FALLBACK_SHAPES = {"standard-8", "standard-16"}
```

**Candidate derivation.** Split the requested instance into `(family, shape)`,
e.g. `n2-standard-8 → ("n2", "standard-8")`. If `shape ∈ FALLBACK_SHAPES`, the
candidate list is the requested family first, then the remaining
`NESTED_VIRT_FAMILIES`, each combined with that same shape:

```
n2-standard-8  →  [n2-standard-8, c2-standard-8]
```

**Edge case — requested family not in the ladder.** If the requested instance's
family is absent from `NESTED_VIRT_FAMILIES` (e.g. a misconfigured
`e2-standard-8` with `nested=true`), derive candidates as *requested-instance
first, then the full ladder, deduped*. The original is still attempted first
(respecting the operator's declaration), and fallback still reaches the
nested-virt-safe families rather than dead-ending.

**Why gate on `FALLBACK_SHAPES` instead of blind family-swapping.** Not every
family offers every shape (e.g. `c2` only exists in fixed shapes). Restricting
fallback to shapes we have verified exist for *every* family in the ladder
guarantees we never synthesize an invalid machine type that we'd then have to
silently skip — which would violate the no-silent-failures rule. If the
requested shape is not in the set, we log that fallback is unavailable for it and
re-raise the original capacity error: explicit, not silent. Resizing the
supported envelope (shrink to `standard-4`, grow past `standard-16` once quota
lands) is editing one set and never touches the ladder.

### 2. Hook point — reattach only

There is no separate reattach code path: `_cs_tofu_vm` serves both create and
reattach, distinguished only by whether `volume.tfstate` exists. `_cs_tofu_volume`
already encodes that distinction — it populates `state.fallback_zones` **only when
`volume.tfstate` does not exist** (fresh create). Family-fallback mirrors this
with a new field, gated on the opposite condition:

- Add `fallback_instances: list[str]` to `_CloudState` (alongside
  `fallback_zones`). In `_cs_tofu_volume`, populate it **only when
  `volume.tfstate` exists** (reattach) — the candidate list from §1 minus the
  requested instance. On fresh create it stays `[]`, so the create path is
  unchanged and keeps its zone sweep. This symmetric gate is what makes the
  feature reattach-only; widening to create later is a one-line change to the
  condition.
- Add an `instance_override: str | None = None` parameter to
  `OffPlatformBackend.vm_vars` (see §3 for why this — not spec mutation — is
  load-bearing). `_cs_tofu_vm` passes `state.fallback_instances` into the apply
  engine.

The sweep itself (in `apply_vm_with_zone_fallback`, or a sibling it delegates to):

1. Attempt `apply_vm` with the requested instance in the pinned zone.
2. On failure, if it is **not** `is_zone_capacity_error(exc)`, re-raise (a real
   config/quota error must abort). `is_zone_capacity_error` already matches the
   family-stockout message — a family stockout produces the same
   `ZONE_RESOURCE_POOL_EXHAUSTED` — so it needs no change.
3. Otherwise iterate `fallback_instances`, re-running `apply_vm` with
   `vm_vars(zone=…, volume_id=…, instance_override=candidate)` — **same pinned
   zone, same `volume_id`**. `apply_vm`'s existing partial-state rollback (the
   orphan `google_compute_firewall.ssh`) runs between attempts so each retry
   starts clean. The **volume is never touched** (no `destroy_volume`, unlike the
   zone-fallback path — that loop destroys the *empty* disk to relocate it; here
   the disk holds data and stays put).
4. On exhaustion, raise a `RuntimeError` naming the families tried and the zone,
   pointing the operator at waiting, a larger quota, or a different region.

The fresh-create path is unchanged: `fallback_instances` stays `[]` and it still
sweeps `fallback_zones` as today.

### 3. Ladder-aware conformance — no rebuild loop

`spec_fingerprint` includes `instance={spec.instance}` on the off-platform path
(`vm_spec.py`), and the guest-stamped fingerprint is compared against the
composed spec by the drift check at `vrg_vm.py:357`, which on mismatch reports
**"VM no longer meets spec — rebuild it."** A naive fallback that lands `n2d`
while the spec says `n2` would trip this and steer the operator straight back
into the stockout.

The fingerprint is, by its own docstring, a hash **over the declaration, not the
realized resource.** A family fallback does not change the declaration — the
operator still asked for `n2-standard-8`. So:

> **The fallback overrides only the tofu `instance_type` variable passed to the
> apply. It does NOT mutate `spec.instance`.**

Consequently `spec_fingerprint(spec)` still hashes the *declared* instance, the
declared hash is what gets stamped in the guest, and the drift check compares
declared-vs-declared → it passes. No spurious `NEEDS-REBUILD`, and **no
fingerprint-format change** (so no existing VM's fingerprint flips on upgrade,
consistent with the established "don't flip fingerprints on upgrade" pattern).

**The mechanism is load-bearing and must be implemented exactly.** `vm_vars`
derives *both* the tofu `instance_type` *and* the stamped `SPEC_FINGERPRINT`
(via `render_provision_env(provision_params(..., fingerprint=spec_fingerprint(self.spec)))`)
from `self.spec`. The two are independent computations off the same object — so:

- The fallback passes the candidate family through the new `instance_override`
  parameter; `vm_vars` returns `"instance_type": instance_override or self.spec.instance`.
- `vm_vars` keeps computing `fingerprint=spec_fingerprint(self.spec)` from the
  **unmutated** declared spec.

The natural-looking shortcut — mutating `self.spec.instance` before the apply —
would flip the stamped fingerprint along with the machine type, reintroducing the
exact drift-rebuild loop this section exists to prevent. **Do not mutate the
spec.** A test asserts that after a fallback the stamped fingerprint equals the
*declared* spec's fingerprint, not the landed family's.

The **actually landed family** is read from the realized resource, not recorded
by hand: `vm.tfstate`'s `google_compute_instance.vm` carries `machine_type`.
`vrg-vm list` / `volumes` surface it by parsing `vm.tfstate` exactly as `volumes`
already parses `volume.tfstate` (the `VolumeState` precedent). This is the single
source of truth — it can't drift from reality — and it deliberately avoids the
per-instance meta sidecar, which #1831 is concurrently reshaping (see §4).

### 4. Transparency — no silent fallback

Every attempt and the outcome are logged loudly to stderr, e.g.:

```
  zone us-central1-f: n2-standard-8 out of capacity — trying n2d-standard-8...
  landed on n2d-standard-8
```

Durable visibility comes from `vm.tfstate` (§3), not a hand-written record: a
later `list`/`volumes` reads the actual `machine_type` so it reflects reality
rather than the declared type.

**Coordination with #1831 (named instances).** That in-flight work reshapes the
per-instance meta sidecar (`write_instance_meta`/`read_instance_meta`). This
design deliberately records nothing there — landed family comes from `vm.tfstate`
— so the two features touch disjoint surfaces and won't collide on the meta
schema. The only shared file is `bin/vrg_vm.py` (#1831 edits instance
resolution/meta; this edits the `_cs_*` stages and `_CloudState`); keep the
diffs in separate regions and rebase whichever lands second.

## Testing

- **Candidate derivation:** requested-family-first ordering; dedup when the
  requested family is in the ladder; the requested-family-not-in-ladder edge case
  (original first, then full ladder); only the requested shape is used; a shape
  outside `FALLBACK_SHAPES` yields no candidates (fallback disabled).
- **Ladder change-detector (NOT a validity proof):** a unit test pins
  `NESTED_VIRT_FAMILIES` and `FALLBACK_SHAPES` to their expected values so the
  ladder cannot be edited *accidentally* — any change forces a deliberate test
  update. This asserts intent, **not** GCP reality; it would happily pass on a
  family that doesn't actually support nested virt. Real validity is the manual
  gate in "Verify before shipping," optionally reinforced by the per-family e2e
  below.
- **Fallback loop:** tries families in order, stops on first success; a
  non-capacity error aborts immediately; ladder exhaustion raises the helpful
  error naming families tried; the volume is never destroyed on this path.
- **Conformance:** after a fallback, the stamped fingerprint equals the
  *declared* spec's fingerprint, **not** the landed family's — and `self.spec` is
  unmutated after `vm_vars(instance_override=…)`.
- **Landed-family surfacing:** `vm.tfstate` parsing returns the realized
  `machine_type`, and `list`/`volumes` report the landed family (not the declared
  one) after a fallback.
- **(Optional, not a CI gate) per-family e2e:** boot each ladder family and assert
  `/dev/kvm` is present — the only test that proves nested virt for real. Run
  on-demand, not per-PR (real GCP spend; subject to the very stockouts at issue).

## Verify before shipping

This is a **manual, human-performed gate** — the unit test (a change-detector)
cannot do it.

**Verified 2026-06-24.** GCE nested virtualization requires an Intel (VT-x)
processor; the docs list **AMD and Arm processors, E2, memory-optimized, and H4D**
as unsupported. So the AMD families **n2d and c2d are NOT eligible** and were
dropped — the original draft ladder (`n2/n2d/c2/c2d`) would have booted VMs with no
`/dev/kvm`. The shipped ladder is Intel-only: **`["n2", "c2"]`**, and both offer
`standard-8` and `standard-16`. Sources:
- nested-virt overview — `docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview`
  ("L1 VMs cannot use ... AMD and Arm processors").
- general-purpose machine docs — corroborating "N2D VMs don't support ... nested
  virtualization".

Re-run this manual check (and update the change-detector test) whenever
`NESTED_VIRT_FAMILIES` or `FALLBACK_SHAPES` changes.

## Alternatives considered

- **Config-driven ladder per spec** (e.g. a `fallback_families` key in
  `vergil.toml`): rejected for v1 — every repo must opt in and get it right, and a
  wrong entry could silently break nested virt.
- **Dynamically derived from GCP:** rejected for v1 — adds API calls, a live
  metadata dependency, and complexity for a list that changes rarely.
- **Static ladder + runtime nested-virt guard** (belt-and-suspenders): the
  curated static ladder plus the change-detector test and the manual GCP-doc
  verification gate are judged sufficient; a runtime guard can be added later if
  the ladder ever grows risky to edit.
- **Both create and reattach** / **family-first on create:** deferred — the
  reattach gap is the live pain; fresh-create can already escape via zone sweep.
