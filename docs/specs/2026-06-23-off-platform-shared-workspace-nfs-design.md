# Off-Platform Shared Workspace (vergil-user ↔ vergil-audit) over NFS Design

**Issues:**

- [vergil-tooling #1796 — Design/brainstorm: vergil-user ↔ vergil-audit shared
  worktree off-platform](https://github.com/vergil-project/vergil-tooling/issues/1796)

**Date:** 2026-06-23

**Status:** Design (from brainstorming, 2026-06-23).

**Spans two repositories.** Like the parent
[off-platform VM dispatch design](2026-06-22-off-platform-vm-dispatch-design.md),
this is the `vergil-tooling` companion to a vergil-vm module change. This spec
specifies the `vrg-vm` dispatcher mechanism, the new fleet-scoped lifecycle, and
the per-VM provisioning changes; the `gcp/shared-fs` OpenTofu module that stands up
the NFS server is a **vergil-vm prerequisite** (see "Cross-repo prerequisites").

## Problem

`vergil-user` and `vergil-audit` operate on the **same checkout**: audit reviews
user's work in the same working tree. On the Lima (local) backend this is free —
both identities' VMs mount the **same host filesystem**, so they share both the
checkout and `~/.claude/projects` (transcripts + the per-project memory dir). See
`lib/lima.py:222-229`: `mounts[0]` is the projects dir, `mounts[1]` is
`~/.claude/projects` (writable).

Off-platform there is no shared host filesystem. The parent design gives **each
identity its own per-identity persistent disk** (`cloud_resource_name(identity, org,
repo)` → `vergil-user-…` vs `vergil-audit-…`), each holding that identity's checkout
and Claude history. So the two identities do **not** share a tree — and, less
obviously, they do **not share project memory**, because `~/.claude/projects/<slug>/
memory/` lands on each identity's private disk. This blocks `vergil-audit` going
off-platform for real: there is no tree for it to review and no shared memory.

A standard GCP zonal persistent disk is read-write to exactly one running VM at a
time, so a per-identity block disk cannot be the shared substrate. The shared
substrate must be a **network filesystem** mounted read-write by multiple VMs at once.

## Goals

- **Faithful Lima analog.** Off-platform `vergil-user` and `vergil-audit` share one
  writable tree *and* shared project memory, exactly as they do on the Mac's single
  host filesystem.
- **Credential isolation preserved.** Sharing the filesystem must not weaken the
  per-identity credential sandbox: each VM keeps its own credentials on its own
  ephemeral boot disk; no credential ever lands on the shared filesystem.
- **Affordable, permanent shared substrate.** The shared filesystem is always-on and
  effectively permanent (it holds the only copy of curated project memory). Cost must
  be proportionate to a fleet-of-one with a handful of client VMs.
- **One dispatch decision point.** The shared-filesystem lifecycle is a new
  fleet-scoped resource managed deliberately, distinct from per-VM create/destroy;
  the Lima path is unaffected.

## Non-goals

- **Auto-HA failover for the NFS server.** Out of scope. Zone-loss recovery is
  manual/scripted restore from a snapshot (documented), matching the parent design's
  "fleet-of-one, documented discipline, no auto-reaper" philosophy.
- **Broad cross-zone / multi-region client placement.** Client VMs co-locate in the
  NFS server's zone (cost rationale below). Wider zone flexibility is the separate
  in-progress zone-flexibility work and is deferred to it.
- **Managed Filestore.** Evaluated and rejected on cost at the required scale (see
  "Cost analysis"). Revisitable if the operational surface of a self-managed NFS
  server proves too high.
- **Reworking the dual-agent IPC channel.** The `vrg-pr-workflow` coordination file
  stays in the shared tree; this design accepts the behavioral trust boundary that
  implies (see "Trust boundary").

## Decision summary

| Decision | Choice | Rationale |
|---|---|---|
| What is shared | **Checkout + the whole `~/.claude/projects` tree** (transcripts + memory) | Most faithful Lima analog; preserves shared project memory. The per-identity persistent `/vergil` disk is **eliminated** for off-platform — nothing identity-private needs to survive a rebuild (credentials re-inject each create). |
| Granularity | **One fleet-wide singleton** shared filesystem | The Mac has one shared host FS for all VMs. Amortizes the fixed cost across every repo and both identities. |
| Mechanism | **Self-managed NFS server** (small GCE VM + zonal `pd-balanced` SSD + snapshots), **not** managed Filestore | At the ~1 TiB scale the working trees actually need, roll-our-own is ~2–5× cheaper, and a handful of clients is a trivial NFS load. |
| Lifecycle | **Explicit verb + dedicated fleet-scoped tofu state**; no lazy auto-create | A permanent shared resource should be stood up deliberately, once. |
| Trust boundary | **Both identities mount read-write; behavioral boundary**; credentials isolated on per-VM boot disks | The `.vergil/pr-workflow.json` IPC channel and shared memory both require audit to write, so a read-only audit mount is infeasible. Matches Lima. |
| Disk tier | **~1 TiB zonal `pd-balanced` SSD + scheduled snapshots** | SSD IOPS keep git/build working-tree I/O snappy; the bulk is reconstructible, so snapshots (not regional replication) protect the tiny precious memory slice. |

## Cost analysis

Pricing researched 2026-06-23. Per-GiB dollar figures are **corroborated estimates**
(the official GCP pricing tables render the per-region cells in JavaScript and could
not be quoted verbatim); the capacity/tier **structure** is doc-quoted data. Confirm
the exact per-region rates in the Cloud Console / pricing calculator before
committing a budget line.

**Why managed Filestore was rejected.** GCP Filestore bills **provisioned** capacity,
not consumed — *"if you create a 1 TiB instance and store 100 GiB … you incur charges
for the entire 1 TiB."* The cheapest zonal tiers floor at **1 TiB**; the Regional
(Small) tier floors at **100 GiB** but the working trees (checkout + build cruft)
realistically need ~1 TiB anyway, and pricing is linear, so the 100 GiB floor does
not help in practice:

| Option @ ~1 TiB | Approx monthly (estimate) |
|---|---|
| Filestore Regional (1024 GiB × ~$0.21–0.30) | ~$215–307 |
| **Roll-our-own: e2-small + 1 TiB zonal `pd-balanced` SSD** | VM ~$12 + disk ~$102 = **~$115** |
| Roll-our-own: e2-small + 1 TiB zonal `pd-standard` HDD | VM ~$12 + disk ~$41 = ~$53 (HDD too slow for live git/build I/O) |

**Cross-provider sanity check** (rejected, but recorded): AWS EFS is true
consumption billing (~$3/mo for ~10 GiB, multi-AZ) but is AWS, off the GCP/OpenTofu
backend the whole architecture is built on; Azure Files forces NFS onto a provisioned
SSD floor (32 GiB + 3,000 IOPS + 100 MiB/s) at ~$20–40/mo. gcsfuse is **disqualified**
— Google's docs explicitly say *"you shouldn't store version control system
repositories in Cloud Storage FUSE mount points"* (no file locking, no atomic
renames, multi-writer clobbering).

**Chosen cost envelope: ~$115–125/month** for the whole fleet (one NFS server VM +
~1 TiB zonal `pd-balanced` SSD + daily snapshots of the tiny precious slice).

### Sources

- Filestore tiers/minimums & provisioned-billing rule:
  `https://cloud.google.com/filestore/docs/service-tiers`,
  `https://cloud.google.com/filestore/docs/overview`,
  `https://cloud.google.com/filestore/pricing`
- AWS EFS: `https://aws.amazon.com/efs/pricing/`
- Azure Files: `https://learn.microsoft.com/en-us/azure/storage/files/understanding-billing`
- gcsfuse semantics (VCS prohibition):
  `https://github.com/GoogleCloudPlatform/gcsfuse/blob/master/docs/semantics.md`,
  `https://cloud.google.com/storage/docs/cloud-storage-fuse/overview`
- Compute / disk pricing: `https://cloud.google.com/compute/all-pricing`,
  `https://cloud.google.com/compute/disks-image-pricing`

## Architecture

Replace the off-platform per-identity persistent `/vergil` disk with **one fleet-wide
self-managed NFS server** providing a shared tree mounted read-write by every
off-platform VM — the faithful cloud analog of the Mac's single shared host
filesystem.

- **Shared NFS server (new fleet singleton).** A small always-on GCE VM (`e2-small`)
  running `nfs-kernel-server`, backed by a ~1 TiB zonal `pd-balanced` SSD, exporting a
  shared tree to the fleet's client VMs over the VPC. A snapshot schedule protects the
  precious (tiny) memory slice.
- **Per-identity client VMs (existing, modified).** Each off-platform agent VM keeps
  its **ephemeral boot disk** (credentials) but **loses its per-identity persistent
  volume**; instead it mounts the shared NFS at `/vergil`. The per-identity
  `volume.tfstate` machinery is removed from the off-platform path.

### Filesystem layout (mirrors the Mac)

```
<nfs-export>/                   (e.g. exported as /srv/vergil)
  projects/<org>/<repo>/        ← shared checkouts (working trees, incl. build cruft)
  claude/projects/<slug>/       ← shared ~/.claude/projects (transcripts + MEMORY)
```

Each client VM:

- mounts `<nfs-export>` at `/vergil`,
- resolves its session workdir to `/vergil/projects/<org>/<repo>`,
- symlinks `~/.claude/projects → /vergil/claude/projects`,
- keeps credentials (`app.pem`, `.credentials.json`) on the boot disk (unchanged).

Because the mountpoint and workdir paths are **identical across identities**, both
`vergil-user` and `vergil-audit` derive the same `<slug>` (the slug is a function of
the workdir path) and resolve to the same `claude/projects/<slug>/memory/` dir — that
is exactly what preserves shared project memory.

`~/.claude/todos` stays **local/ephemeral** on the boot disk (not shared), matching
Lima — todos are not mounted there, and each agent's todo list is its own. This is a
small fix to the current off-platform behavior, which symlinks `todos/` onto the
persistent disk too.

### Trust boundary

Both identities mount the NFS **read-write**; the trust boundary is **behavioral**,
enforced by the agent role contracts (audit never edits source per its skill), the
identity-mode credential split, and the PR-workflow protocol (audit posts verdicts
but holds no merge or credential power).

A read-only audit mount is **infeasible**: the dual-agent coordination protocol
(`lib/pr_workflow/local_transport.py`) uses `.vergil/pr-workflow.json` **inside the
shared tree** as its message bus — *both* agents read and write it — and the shared
`~/.claude/projects/<slug>/memory/` is co-written too. The boundary that does hold is
the one that matters: **credentials never touch the shared filesystem**; they live on
each VM's ephemeral boot disk. NFS compromise loses work product (tree, memory,
transcripts, IPC state), never identities. NFS export is firewalled to the VPC
(`AUTH_SYS` over a private network is adequate for a fleet-of-one; Kerberos is overkill).

### NFS consistency for the dual-agent IPC

The `vrg-pr-workflow` protocol already uses **SHA-256-of-content change detection
(never mtime)** and atomic temp+rename writes, precisely because the two agents poll a
file across a shared mount — *"mtime semantics vary across the host mount that the two
agents share."* This maps cleanly onto NFS close-to-open consistency (each poll
re-opens and re-reads). The spec pins NFS mount options for prompt cross-client
visibility of an atomic rename — tight attribute caching (e.g. low `actimeo`, or
`lookupcache=none` if needed) — validated against the real protocol during the gated
e2e.

## Lifecycle

### Fleet singleton (decoupled from per-VM create/destroy)

- **`vrg-vm shared-fs create`** → `tofu apply` the `gcp/shared-fs` module against a
  **fleet-scoped state** at `~/.config/vergil/tofu/_fleet/<provider>/shared-fs.tfstate`.
  Stands up the NFS VM + data disk + firewall + snapshot schedule. Idempotent,
  deliberate, one-time. Populates structured labels so a lost fleet state is
  re-importable by label match (mirrors the volume pattern).
- **`vrg-vm shared-fs destroy`** → confirmation-gated (like `destroy-volume`), rarely
  used, loud data-loss warning. Removes the fleet state on success.
- **`vrg-vm shared-fs status`** / surfaced in `vrg-vm list` → shows shared-fs presence
  and the NFS endpoint.

No lazy auto-create: standing up a permanent shared resource is always explicit.

### Per-VM (modified)

- **`create` preflight.** The shared-fs must exist (fleet-state read / label query).
  Absent → hard fail with remediation: *"run `vrg-vm shared-fs create` first."*
- **`create` provisioning.** cloud-init mounts the NFS endpoint at `/vergil`, then the
  existing bootstrap step runs **per-subdir on the shared mount**:
  `/vergil/projects/<org>/<repo>` exists → `vrg-git fetch` (reattach); absent →
  `vrg-git clone` (fresh); credential-less identity (`auth_type="none"`) → skip,
  logged. This is today's `bootstrap_volume` logic, repointed at the shared mount.
- **`destroy` / `rebuild`.** Only the ephemeral VM; the shared NFS is **never in
  scope** (exactly as the per-identity volume was never in `destroy`'s scope).
  Reattach = remount + per-subdir fetch.
- **`destroy-volume` (off-platform).** There is no per-identity volume anymore →
  returns a clear message redirecting to `vrg-vm shared-fs destroy` for the fleet
  resource.

### Reaching the NFS server, and the zone choice

The dispatcher reads `nfs_endpoint` (internal IP) and `zone` from the fleet-state
output and injects them into the VM's `provision_env`; cloud-init mounts the export.
The firewall opens NFS (2049) only within the VPC.

Client VMs are **pinned to the NFS server's zone**, read from the fleet state.
Rationale: same-zone VM-to-VM traffic is free, whereas cross-zone in-region NFS
traffic bills ~$0.01/GiB and builds push many bytes over NFS. For a few-VM fleet,
co-locating is the cheap, faithful ("it's all local") choice. Tradeoff: this
reintroduces a single-zone pin — now the *NFS* zone, fleet-wide, rather than a
per-identity disk's zone. Broader zone flexibility is deferred to the in-progress
zone-flexibility work. This replaces the parent design's "zone = where the volume
landed" derivation with "zone = the NFS server's zone."

### Snapshots (precious-slice protection)

A GCP snapshot **resource policy** on the NFS data disk (daily, ~7-day retention),
declared in the `gcp/shared-fs` module. Zone-loss recovery = restore snapshot → new
disk → new NFS VM in a surviving zone (manual/scripted, documented). The
reconstructible bulk (checkouts re-clone from GitHub; build artifacts regenerate)
rides along for free; the point is the tiny non-reconstructible memory slice.

## Code shape

### vergil-tooling

- New `lib/vm_shared_fs.py` (or extend `lib/vm_cloud.py`): `shared_fs_create` /
  `shared_fs_destroy` / `shared_fs_status` driving `tofu` against the fleet state;
  fleet-state path helper (`_fleet/<provider>`), label-based recovery;
  `resolve_nfs_endpoint()` reading the fleet-state output.
- `OffPlatformBackend`: drop `apply_volume` / `destroy_volume` / volume-zone logic
  (the VM no longer owns a persistent disk); gain NFS-endpoint injection and zone-pin
  (zone read from fleet state).
- `bootstrap_volume` → `bootstrap_workspace(transport, mount, identity, org, repo)`
  operating on the shared mount subdir; same clone / fetch / skip logic.
- `link_claude_dirs`: symlink only `~/.claude/projects → /vergil/claude/projects`;
  leave `~/.claude/todos` local on the boot disk.
- `bin/vrg_vm.py`: new `shared-fs` verb group; `create` preflight; off-platform
  `destroy-volume` redirect; `list` BACKEND/shared-fs surfacing.

### vergil-vm (cross-repo prerequisite)

New `gcp/shared-fs` OpenTofu module:

- **inputs:** `name`, `region`, `zone` (or pick), `disk_size_gib`, `machine_type`,
  `labels`, network/subnet, `snapshot_schedule` params, allowed-client CIDR (VPC range).
- **resources:** GCE instance (NFS server) + zonal `pd-balanced` data disk +
  `nfs-kernel-server` cloud-init + firewall (2049 from VPC) + snapshot resource policy.
- **outputs:** `nfs_endpoint` (internal IP/hostname), `zone`, `disk_id`.

The `vm` module gains `nfs_endpoint` mount wiring (mount at `/vergil` in cloud-init)
and drops the per-identity data-disk attach. Published via the existing
module-tarball/tag fetch mechanism (`fetch_modules`), version-locked to the same tag.

## Testing (vergil-tooling side)

`tofu`/`gcloud` mocked at the subprocess boundary:

- `shared-fs` create / destroy / status drive `tofu` against the fleet state; correct
  fleet-state path (`_fleet/<provider>`) and label population.
- `create` preflight: shared-fs absent → clear remediation; present → proceeds.
- VM provisioning passes `nfs_endpoint` and pins the VM zone to the NFS zone.
- `bootstrap_workspace`: clone / fetch / skip (credential-less) per-subdir on the
  shared mount.
- `link_claude_dirs` symlinks only `projects/` to the shared mount; `todos/` stays
  local.
- `destroy` / `rebuild` never touch the shared-fs state; off-platform
  `destroy-volume` returns the redirect message.
- NFS mount options present in the provisioning (consistency for the IPC poll).
- **Lima regression:** the existing `vrg-vm` / `lima` tests stay green; Lima behavior
  is unchanged.

No real cloud in CI. The gated, money-spending e2e (NFS mounts read-write from two
VMs; the `vrg-pr-workflow` poll round-trips across the mount; snapshot/restore
recovers the memory slice) lives in vergil-vm.

## Acceptance criteria

1. `vrg-vm shared-fs create` stands up the fleet NFS server (VM + zonal `pd-balanced`
   disk + firewall + snapshot schedule) via the `gcp/shared-fs` module against a
   fleet-scoped tofu state; `shared-fs destroy` is the only path that tears it down and
   is confirmation-gated.
2. An off-platform `create` with no shared-fs present fails with a clear remediation
   pointing at `vrg-vm shared-fs create` (no lazy auto-create).
3. Two identities (`vergil-user`, `vergil-audit`) on the same `(org, repo)` mount the
   shared NFS read-write and operate on the **same working tree**; project memory under
   `~/.claude/projects/<slug>/memory/` is **shared** between them.
4. No credential (App key or Claude token) ever lands on the shared filesystem; each
   VM's credentials live on its ephemeral boot disk and die with the VM.
5. The per-identity persistent `/vergil` volume is removed from the off-platform path;
   `destroy` / `rebuild` never touch the shared NFS; reattach remounts and fetches
   per-subdir.
6. The `vrg-pr-workflow` dual-agent poll round-trips correctly across the NFS mount
   (content-hash change detection + pinned mount options).
7. `~/.claude/projects` is shared on the NFS; `~/.claude/todos` stays local.
8. The Lima default is unaffected — a repo with no off-platform backend behaves
   identically.

## Cross-repo prerequisites (vergil-vm, land + release first)

1. **`gcp/shared-fs` module** — NFS-server instance + zonal `pd-balanced` disk + nfsd
   cloud-init + 2049 firewall + snapshot resource policy; outputs `nfs_endpoint` /
   `zone` / `disk_id`.
2. **`vm` module change** — accept `nfs_endpoint`, mount it at `/vergil` in cloud-init,
   drop the per-identity data-disk attach.
3. **Publish in the module tarball** at the resolved tag (existing `fetch_modules` path).

## Related

- **Parent design:** [off-platform VM dispatch](2026-06-22-off-platform-vm-dispatch-design.md);
  cross-repo contract in vergil-vm #199.
- **Epic:** vergil-vm #199 · dispatch #1706 · volume visibility #1795 ·
  zone-flexibility (in progress).
- **Security register:** vergil-tooling #1369.
- **Adjacent:** #1705 (credential-less VM identity), #1707 (`auth_type="none"`).
