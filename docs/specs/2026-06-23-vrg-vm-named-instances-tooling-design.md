# `vrg-vm` Named Instances — Tooling-Side Design

**Issue:** [vergil-tooling #1831 — feat: multiple VM instances per repo (tooling
side)](https://github.com/vergil-project/vergil-tooling/issues/1831)

**Date:** 2026-06-23

**Status:** Design (from brainstorming, 2026-06-23). Tooling companion to the
authoritative cross-repo design.

## Relationship to the authoritative design

The authoritative design for multiple VM instances per repo lives in **vergil-vm**
at `docs/specs/2026-06-23-multiple-vm-instances-per-repo-design.md`
([vergil-vm #242](https://github.com/vergil-project/vergil-vm/issues/242)). That
document is authoritative for the cross-repo contract and the `vergil-vm`-owned
module guard (the `var.name` validation + tests). This document is the
**tooling-side implementation design** for [#1831](https://github.com/vergil-project/vergil-tooling/issues/1831):
it grounds the authoritative model in the actual `vrg-vm` code and records the
design decisions that grounding surfaced.

The split mirrors the prior cross-repo features — per-repo profiles
(vergil-vm #99 / vergil-tooling #1412) and the off-platform backend
(vergil-vm #199 / vergil-tooling #1706).

**Where this document diverges from the authoritative spec it says so explicitly**
(see [Reconciliations](#reconciliations-deltas-from-the-authoritative-spec)). The
divergences are naming/representation details that the authoritative spec described
at an idealized level; the observable behavior (named instances, `--name`,
recorded-state lifecycle, the `list` surface, the cloud length guard) is unchanged.

## Goal

Let one `(identity, org/repo)` own several **named instances**, each with its own
composed profile, backend, lifecycle, recorded state, and persistent volume. An
absent name is the default instance — exactly today's behavior, fully backward
compatible. Dissolve the backend-switch destroy-orphan edge (Problem 1) by making
the destructive lifecycle verbs act on recorded state rather than the live profile.

## Scope boundary

- **vergil-vm** owns the GCP modules' defense-in-depth `var.name` validation
  (RFC1035 charset + length ≤ 58) and its tests. The modules are otherwise
  instance-agnostic (`name`/`labels` are opaque; `interface.json` unchanged).
- **This issue (vergil-tooling)** owns everything below: profile parsing &
  composition, the handle and its derived names, `--name` across every verb,
  recorded-state lifecycle dispatch, the cloud-name hash derivation + labels, and
  the `list` surface.

## Design decisions (from brainstorming)

Three keystone decisions, settled before this design was written, shape the rest:

1. **Keep the existing per-target delimiters; add the fourth segment to each.**
   The authoritative spec describes a single `--`-delimited slug that doubles as
   the Lima instance name and is reversed with `split('--')`. That is not
   implementable as written: Lima's instance-name regex
   (`^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$`) rejects consecutive dashes, which is
   precisely why the #99 implementation already uses `.` for Lima names and a
   separate `-`/hashed scheme for cloud names, reversing via a sidecar file (Lima)
   and resource labels (cloud) rather than by splitting. We extend each existing
   scheme with the fourth segment and keep the proven reversal machinery. The
   `--` slug survives as the **readable handle** used for the tofu state path and
   in documentation, not as the literal Lima name.

2. **Decouple the tofu state-path key from the cloud resource name; key the state
   path on the readable `--` slug.** Today `state_key == cloud_resource_name`.
   Once the cloud resource name becomes an opaque hash (decision forced by the
   63-char limit, below), the state directory is keyed on the readable
   `identity--org--repo--name` slug instead, matching the authoritative spec's
   "readable slug for the state path" intent and keeping
   `~/.config/vergil/tofu/` human-debuggable.

3. **Greenfield — no migration path.** There are no live off-platform deployments
   whose cloud resources or state directories must be preserved across the naming
   change. The new scheme applies to newly created instances. Existing **local**
   dedicated VMs are default instances (three-segment `.` Lima names, unchanged),
   so the Lima side is backward compatible without migration; the cloud-side scheme
   change affects nothing that exists.

## The handle and its derived names

The authoritative key is the four-part handle `(identity, org, repo, name)`, where
an absent `name` selects the default instance. From the handle, three deterministic
names are derived, each with its own delimiter:

| Derived name | Form | Used for | Reversal |
|---|---|---|---|
| **Lima instance name** | `identity.org.repo.name` (dots; bare `identity` for base, three-segment for default dedicated) | the `limactl` instance | sidecar metadata (`write_instance_meta` / `recover_triple`), extended to carry `name` |
| **State slug** (readable) | `identity--org--repo--name` (`--`-joined; three-segment for default dedicated, bare `identity` for base) | tofu state dir `~/.config/vergil/tofu/<slug>/<provider>/`; the SHA-256 input for the cloud name | resource labels (and human-readable in the path) |
| **Cloud resource name** | `vrg-<first 12 hex of sha256(state-slug)>` (≤ 16 chars, RFC1035: leading letter, lowercase alnum + hyphen) | GCP instance/disk/firewall name | never reversed from the name — `list` and `tofu import` read the labels |

### Why the cloud name is hashed

GCP resource names are capped at 63 chars (RFC1035); the derived `<name>-data`
disk and `<name>-ssh` firewall add suffixes. The four-segment slug for the
motivating repo
(`vergil-user--logical-minds-foundry--mq-cluster-tooling--cloud-x86`, 65 chars)
already overflows. So the readable slug and the cloud resource name are split: the
dispatcher passes the GCP modules the short deterministic `vrg-<hash>` name and the
identity labels. The hash is deterministic (same slug → same name, so `create` is
idempotent and re-import is stable) and collision-free at fleet-of-one scale.

**The hash is applied uniformly to all off-platform instances** (three- and
four-segment), not only when the slug would overflow. This is deterministic and
uniform, and greenfield makes it free. (The prior behavior — derive a readable
dashed name and hash only on overflow — is dropped; nothing deployed depends on
it.)

### Identity lives in labels

`vergil-identity` / `vergil-org` / `vergil-repo`, plus the new **`vergil-instance`**
label, carry the human-readable handle on every cloud resource. `vergil-instance`
is a value inside the existing opaque `labels` map — **no module variable or
`interface.json` change**. `vrg-vm list` and `tofu import` read the labels, never
the opaque resource name.

### Validation (loud, at parse time)

- An instance name must match `[a-z0-9-]+` and must not contain `--`. A single
  dash inside a name is fine; a double dash would break the readable slug.
- A **repo name containing `--` is rejected**, keeping the readable `--` state
  slug unambiguous. (Identities are a closed vergil-prefixed set and GitHub
  org/user logins cannot contain consecutive hyphens, so only the repo segment
  needs the guard.)

Both checks fail loudly before any name is constructed — no malformed slug is ever
produced (no-silent-failures).

## Configuration parsing & composition (`lib/vm_spec.py`)

### The `instances` namespace

A named instance is declared under an explicit `instances` table inside the role
overlay (**per-identity by design — there is no all-identity
`[vm.instances.<name>]` tier**):

```toml
[vm.vergil-user]                         # the default (unnamed) instance
cpus   = 12
memory = "64GiB"

[vm.vergil-user.instances.cloud-x86]     # named: "cloud-x86"
backend  = "off-platform"
provider = "gcp"
region   = "us-central1"
instance = "n2-standard-16"
volume   = "300GiB"
```

The parser reads `[vm.<identity>.instances.<name>]` into a new
`instances: Mapping[str, RoleOverlay]` field on the per-identity overlay. The
explicit namespace avoids any clash with array-of-table keys (`port_forwards`,
`apt_repos`).

### The composition cascade

`compose_vm_spec` gains an optional `instance: str | None` parameter and composes:

1. Built-in base footprint.
2. `identities.toml [<identity>]` — credentials + base footprint.
3. Repo `[vm]` — all-identity requirements.
4. Repo `[vm.<identity>]` — role overlay. **Defines the default instance.**
5. Repo `[vm.<identity>.instances.<name>]` — named-instance overlay (only when
   `instance` is given).
6. `identities.toml [<identity>.<org>.<repo>]` — host override, wins. The per-name
   slot `[<identity>.<org>.<repo>.<name>]` is **reserved** (parsed-but-unused),
   non-breaking to activate later.

- **Default instance** composes tiers 1–4 (+6) — today's path, byte-for-byte
  unchanged when no `instances` are declared.
- **Named instance** composes tiers 1–5 (+6): inherits the role overlay (tier 4),
  then the named overlay (tier 5).

Merge rules are unchanged (`_apply_overlay`): `packages` accumulate (union),
scalars are last-wins, credentials come solely from tier 2. The off-platform
required-key contract (`_validate_backend`: `off-platform` requires
`provider`/`region`/`instance`/`volume`) runs per composed instance.

`--name X` against an identity that declares no instance `X` raises a `SpecError`
listing that identity's available instance names — **no silent fallback to the
default**.

### Fingerprint

`spec_fingerprint` inputs are unchanged. The **name is part of the handle, not
fingerprint content**, so adding or renaming an instance never trips drift on its
siblings. The preflight gate (`session`/`start`) is the #99 gate, now keyed on the
four-part handle.

## CLI (`bin/vrg_vm.py`)

- Add a `--name <name>` flag to
  `create`/`session`/`rebuild`/`destroy`/`stop`/`start`/`update`/`destroy-volume`,
  threaded through `_resolve_target` and `_resolve_instance` into the handle.
- Absent `--name` resolves the **default instance**, exactly as today. There is
  **no error path** when only named instances exist — "bare verb hits the default
  box" is the consistent #99 rule (a "forbid the bare default" mode is a deferred
  non-goal).

## Recorded-state lifecycle dispatch (the Problem-1 fix)

Resolution is split by verb intent:

- **`create` / `rebuild` / `session`-preflight** resolve from the **composed
  profile + fingerprint** — you are asserting intent. Unchanged shape.
- **`destroy` / `stop` / `start`** resolve from **recorded state for the handle**,
  not the live profile. A new enumerator takes `(identity, org, repo, name)` and
  finds every recorded box under the handle:
  - the Lima instance `identity.org.repo.name`, if it exists; and
  - every `~/.config/vergil/tofu/<state-slug>/<provider>/` directory carrying
    recorded state.

  Because the state slug is now readable and deterministic, this is a direct glob
  of the handle's own subtree — no scan of unrelated handles.

A bare `destroy --name X` tears down **every** recorded backend/provider under the
handle (a Lima box *and* a `gcp` box, or `gcp` *and* `azure` state side by side,
left by an in-place `backend`/`provider` edit) — after printing a confirmation
listing of exactly what it will remove. `stop`/`start` act on each recorded running
box. This replaces `_resolve_instance`'s spec-derivation for these three verbs.

**How this dissolves Problem 1.** A profile edit can no longer aim `destroy` at a
box that was never built, nor leave a sibling-backend box behind: nothing built
under a handle survives a `destroy` of that handle. The orphan-prone in-place
backend flip is also no longer the natural operation — "move the lab to GCP"
becomes "declare a `cloud-x86` instance, destroy `local` by name when ready," two
independent handles. Any leftover whose composed spec no longer backs it surfaces
as `orphaned` in `list` and is removable by `destroy --name X`.

## `vrg-vm list`

- Add an **INSTANCE** column (between SCOPE and BACKEND); `—` for the default
  instance.
- Enumerate from **VM/instance state and volume state**: the Lima `<identity>.*`
  prefix scan (now including four-segment slugs, reversed via the sidecar) plus the
  tofu state dirs (reversed via labels, now including `vergil-instance`). One row
  per recorded `(slug, provider)`, so a handle carrying more than one recorded
  backend/provider (from an in-place edit) is never hidden; the stale sibling reads
  `orphaned` against the current composed spec.
- A handle whose `volume.tfstate` exists but whose VM has been destroyed shows a
  `STATUS = no-vm` row carrying the **persistent volume size in the DISK column**,
  so every paid volume stays visible with its VM down. No reaper, no billing math —
  just no hidden paid volume. `destroy-volume --name X` removes one.
- CPUS/MEM/DISK, AGENTS/HUMANS (process-tree classification), BACKEND, and SPEC
  (`ok` / `NEEDS-REBUILD` / `orphaned` / `under`) semantics are unchanged, reported
  per instance. Enumeration stays **O(instances)**. `list` degrades visibly without
  cloud creds (`unknown (no <provider> creds)`), per instance.

## State paths, labels, and volumes

```
~/.config/vergil/tofu/<identity>--<org>--<repo>--<name>/<provider>/
  volume.tfstate   # precious; re-importable from labels
  vm.tfstate       # ephemeral
```

Each named instance owns its **own** persistent volume, keyed by the four-part
handle, disk-named `<cloud-name>-data` (per vergil-vm #221). The mapping stays
1:1 instance→volume, preserving #199's no-concurrent-attach property (no
multi-attach, no shared FS). The bootstrap-vs-reattach logic, the fixed-path mount,
the format-only-if-blank guard, and the guarded `destroy-volume --name <name>` verb
all behave per #199, per instance. With `prevent_destroy` no longer set on the
modules (#212), the guarded explicit `destroy-volume` is the only path that removes
a volume.

## Backward compatibility

Fully backward compatible:

- A repo that declares no `instances` resolves exactly as today (default instance =
  tiers 1–4, base box if empty).
- Existing local dedicated VMs are default instances with unchanged three-segment
  `.` Lima names.
- `--name` is optional on every verb; omitting it preserves current behavior.
- No migration of existing VMs or `identities.toml` / `vergil.toml` config is
  required (greenfield on the cloud side; see decision 3).

## Security boundaries

The #99 repo-code-runs-as-root-in-a-credentialed-VM boundary applies **per
instance** — each instance composes its own profile and provisions independently,
so a permissive config on one does not widen another. The off-platform access model
(IAP + private VM, Cloud NAT egress; vergil-vm #207/#211/#228) is per instance and
unchanged: each instance is its own IAP target. Letting a named collaborator into a
specific instance is an out-of-band IAM grant
(`roles/iap.tunnelResourceAccessor` + OS Login), **not** a `vergil.toml` knob — the
named-instance model contributes the dedicated, independently-disposable box, not
the access grant. No new security-register entry is required; vergil-tooling #1369
covers the seams at instance granularity.

## Testing

- **Composition / handle.** A named instance composes tiers 1–5 and the default
  composes 1–4; a repo with an empty `instances` namespace behaves identically to
  today; `--name X` against an identity declaring no instance `X` errors with the
  available names; the four-part handle round-trips through the sidecar (Lima) and
  labels (cloud), including four-segment slugs; an invalid instance name (`--` or
  illegal chars) **and a repo name containing `--`** are rejected loudly at parse
  time.
- **Cloud naming.** The dispatcher derives the deterministic `vrg-<hash>` name
  (≤ 63) and composes the `vergil-identity`/`vergil-org`/`vergil-repo`/
  `vergil-instance` labels; `list` and re-import read the labels, not the name.
- **Lifecycle / Problem 1.** `destroy` enumerates and reconciles all recorded
  state, not the live profile: build a local instance, edit its overlay `backend`
  to off-platform, `destroy --name X` removes the **local** box (no orphan); build
  under `gcp`, edit `provider` to `azure` and rebuild, assert `destroy --name X`
  tears down **both** the `gcp` and `azure` recorded states (no sibling orphan); a
  leftover from a dropped stanza shows `orphaned` in `list` and `destroy --name X`
  removes it.
- **Volume visibility.** A handle whose VM is destroyed but whose volume state
  remains shows a `no-vm` row in `list` carrying the volume size.
- **Regression.** The full existing `tests/` suite stays green — the Lima
  default path is unchanged.
- **Gated real two-instance cloud e2e — deferred.** `tests/e2e-off-platform.sh`
  does not yet exist (#199 left the paid cloud e2e unbuilt); standing it up is a
  separate follow-up.

## Acceptance criteria (tooling subset)

1. `create/session/destroy/stop/start/rebuild <repo> --name X` operate on the named
   instance `X` independently of the default and of other named instances.
2. Two named instances for one `(identity, repo)` co-exist and run simultaneously,
   each with its own volume, state, and fingerprint.
3. A bare verb resolves to the default instance exactly as today; a repo that names
   nothing is behaviorally identical to pre-change.
4. Editing a built instance's `backend`/`provider` in place and running
   `destroy --name X` tears down **every recorded backend/provider** under the
   handle — never orphaning a sibling — and a dropped stanza surfaces as `orphaned`
   in `list`, removable by `destroy --name X`.
5. An invalid instance name **or a repo name containing `--`** is rejected at parse
   time with a clear error; no malformed slug is produced.
6. `vrg-vm list` shows the INSTANCE column with correct per-instance
   STATUS/footprint/AGENTS/HUMANS/BACKEND/SPEC; a surviving volume with a destroyed
   VM shows a `no-vm` row carrying the volume size; enumeration stays O(instances).
7. `--name X` against an identity that declares no instance `X` errors loudly with
   that identity's available named instances — no silent fallback.
8. The dispatcher derives the deterministic `vrg-<hash>` cloud name and composes
   the identity labels (`list`/import read the labels, not the name).
9. The full existing `tests/` suite is green.

## Reconciliations (deltas from the authoritative spec)

Recorded so the divergence from the authoritative `vergil-vm` document is explicit
and auditable:

1. **Lima name stays `.`-delimited; reversal is not `split('--')`.** The
   authoritative spec presents one `--` slug that is also the Lima instance name,
   reversed by splitting. Lima rejects `--`, so the Lima name keeps the existing
   `.` delimiter (fourth segment appended) and is reversed via the existing sidecar
   metadata. The `--` slug remains the readable handle for the state path and docs.
   The repo-name-`--` rejection and instance-name validation are still implemented
   (they keep the readable state slug unambiguous), satisfying the spec's
   acceptance criteria.
2. **`state_key` decoupled from `cloud_resource_name`.** The state directory is
   keyed on the readable `--` slug; the cloud resource name is the `vrg-<hash>`.
   (Today they are equal.)
3. **The cloud hash is applied uniformly** to all off-platform instances, not only
   when the slug would overflow 63 chars. Deterministic and uniform; greenfield
   makes it free.

## Implementation touch-points

- `lib/vm_spec.py` — parse `[vm.<identity>.instances.<name>]`; `compose_vm_spec`
  gains `instance`; tier-5 composition; instance-name + repo-`--` validation;
  derive the three names from the handle; reserve the tier-6 per-name slot.
- `lib/vm_cloud.py` — `cloud_resource_name` → `vrg-<hash>`; decouple `state_key`
  (readable slug) from the cloud name; add `vergil-instance` to `cloud_labels`.
- `lib/lima.py` — extend the sidecar (`write_instance_meta`/`recover_triple`) to
  carry `name`.
- `bin/vrg_vm.py` — `--name` across the verbs; the recorded-state enumerator for
  `destroy`/`stop`/`start`; `list` INSTANCE column + `no-vm` rows + per-`(slug,
  provider)` rows.

## Related

- **Authoritative design:** vergil-vm #242 +
  `docs/specs/2026-06-23-multiple-vm-instances-per-repo-design.md`.
- **Parent models:** vergil-vm #99 / vergil-tooling #1412 (per-repo profiles);
  vergil-vm #199 / vergil-tooling #1706 (off-platform backend).
- **Security register:** vergil-tooling #1369.
