# Off-platform: instance-family fallback on reattach

- **Issue:** vergil-tooling#1836
- **Status:** Design (approved)
- **Related:** #1797 (explicit zone knob), #1804 (orphan-firewall rollback), #1813 (zone fallback on fresh create)

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
# MUST match GCP's nested-virtualization supported-machine-types doc.
NESTED_VIRT_FAMILIES = ["n2", "n2d", "c2", "c2d"]

# Shapes verified to exist across the whole ladder and actually run off-platform.
# Family-fallback engages only for these; adding a size later is one line.
FALLBACK_SHAPES = {"standard-8", "standard-16"}
```

**Candidate derivation.** Split the requested instance into `(family, shape)`,
e.g. `n2-standard-8 → ("n2", "standard-8")`. If `shape ∈ FALLBACK_SHAPES`, the
candidate list is the requested family first, then the remaining
`NESTED_VIRT_FAMILIES`, each combined with that same shape:

```
n2-standard-8  →  [n2-standard-8, n2d-standard-8, c2-standard-8, c2d-standard-8]
```

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

In the reattach call path (where `fallback_zones=[]` today), add a family sweep:

1. Attempt `apply_vm` with the requested instance in the pinned zone.
2. On failure, if it is **not** `is_zone_capacity_error(exc)`, re-raise (real
   config/quota error must abort). `is_zone_capacity_error` already matches the
   family-stockout message — a family stockout produces the same
   `ZONE_RESOURCE_POOL_EXHAUSTED` — so it needs no change.
3. Otherwise iterate the remaining ladder candidates, re-running `apply_vm` with
   the candidate as the tofu `instance_type`, **same pinned zone, same
   `volume_id`**. `apply_vm`'s existing partial-state rollback (the orphan
   `google_compute_firewall.ssh`) runs between attempts so each retry starts
   clean. The **volume is never touched** (no `destroy_volume`, unlike the
   zone-fallback path).
4. On exhaustion, raise a `RuntimeError` naming the families tried and the zone,
   pointing the operator at waiting, a larger quota, or a different region.

The fresh-create path is unchanged: it still uses `apply_vm_with_zone_fallback`
with real `fallback_zones`.

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

The **actually landed family** is operational state, recorded separately in the
per-instance meta sidecar via `write_instance_meta`, and surfaced in
`vrg-vm list` / `vrg-vm volumes` for transparency and inventory.

### 4. Transparency — no silent fallback

Every attempt and the outcome are logged loudly to stderr, e.g.:

```
  zone us-central1-f: n2-standard-8 out of capacity — trying n2d-standard-8...
  landed on n2d-standard-8
```

The landed family is persisted to the meta sidecar (point 3) so a later
`list`/`volumes` reflects reality rather than only the declared type.

## Testing

- **Candidate derivation:** requested-family-first ordering; dedup when the
  requested family is in the ladder; only the requested shape is used; a shape
  outside `FALLBACK_SHAPES` yields no candidates (fallback disabled).
- **Ladder validity:** the `FALLBACK_SHAPES × NESTED_VIRT_FAMILIES` cross-product
  is all valid machine types, and the ladder matches the documented
  nested-virt-supported set (a pinned list the test asserts against).
- **Fallback loop:** tries families in order, stops on first success; a
  non-capacity error aborts immediately; ladder exhaustion raises the helpful
  error naming families tried; the volume is never destroyed on this path.
- **Conformance:** after a fallback, the stamped fingerprint equals the
  *declared* spec's fingerprint (not the landed family's), and the meta sidecar
  records the landed family.

## Verify before shipping

The ladder's membership and order **must** be checked against GCP's current
"nested virtualization — supported machine types" documentation. Confident:
N1/N2/N2D/C2 support nested virtualization; E2 and the Tau families do not.
**To confirm against the doc before inclusion: C2D (AMD) nested-virtualization
support.** If C2D is not supported, drop it from `NESTED_VIRT_FAMILIES`; the
ladder-validity test encodes whatever the doc says.

## Alternatives considered

- **Config-driven ladder per spec** (`fallback_instances` in `vergil.toml`):
  rejected for v1 — every repo must opt in and get it right, and a wrong entry
  could silently break nested virt.
- **Dynamically derived from GCP:** rejected for v1 — adds API calls, a live
  metadata dependency, and complexity for a list that changes rarely.
- **Static ladder + runtime nested-virt guard** (belt-and-suspenders): the
  curated static ladder plus the ladder-validity unit test is judged sufficient;
  a runtime guard can be added later if the ladder ever grows risky to edit.
- **Both create and reattach** / **family-first on create:** deferred — the
  reattach gap is the live pain; fresh-create can already escape via zone sweep.
