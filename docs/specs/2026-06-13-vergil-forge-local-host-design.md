# vergil-forge: Local Forgejo Host Design

**Issue:** #1653
**Date:** 2026-06-13
**Status:** Draft — Design
**Related:** #1521 (forge abstraction strategy — this is its Phase 0
prerequisite, made concrete)

## Purpose

`vergil-forge` is a new, standalone repository that stands up and
operates a persistent, **local-first Forgejo instance** on owned
hardware (the MacBook Pro today, a home cluster later). It is the
self-hosted Forgejo home that the forge-abstraction strategy (#1521)
names as its **Phase 0 prerequisite**: the `ForgejoForge` adapter and
the per-repo migration both depend on a real instance to target.

The executive decision driving this work: move the forge — the git
hosting layer — local, onto owned hardware, and aggressively reduce
dependence on GitHub. This repo is the hosting half of that move; the
adapter half lives in `vergil-tooling` (#1521).

## Separation of Concerns

`vergil-forge` owns the **forge-host domain**. It is deliberately a
separate repo from `vergil-tooling`, managed independently:

| Repo | Owns | Examples |
|---|---|---|
| `vergil-tooling` | Cross-repo dev tooling and forge **adapters** | `vrg-git`, `vrg-gh`, `vrg-validate`, `GitHubForge`, `ForgejoForge` |
| `vergil-forge` | The forge-**host** runtime and its operations | Lima VM, Forgejo + Postgres stack, `vrg-forge` lifecycle CLI |

`vergil-forge` is itself a **Python repo** and a managed repo like any
other: it consumes `vergil-tooling` (host-installed `vrg-*`, a
`vergil.toml`, container-based `vrg-validate`, the worktree
convention). Its *domain* tooling (`vrg-forge`) lives in it, not in
`vergil-tooling`, because that tooling is localized to operating this
one service. Not every tool belongs in `vergil-tooling`; placement
follows where the tool is used.

## Foundational Principles

1. **Independence via ownership.** The forge runs on hardware we own,
   with no cloud service on its critical path. "Self-hosted" means not
   critically dependent on a provider we do not control.

2. **The forge duality.** **Local Forgejo serves the repos worked on
   solo; Codeberg serves shared/community repos.** The laptop is never
   a remote dependency for other people — collaboration migrates *to*
   Codeberg, it does not reach *into* the local machine. The same
   `ForgejoForge` adapter serves both (they differ only by base URL and
   auth), so `vergil-forge`'s scope is specifically the **local solo
   instance**; Codeberg is a separate deployment of the same adapter,
   not something `vergil-forge` manages.

3. **Repos have roles.** Each repo is either:
   - **Solo** (the majority): the local instance is canonical/primary;
     an off-box **push-mirror** provides backup. One-directional and
     robust because nobody else writes.
   - **Shared**: Codeberg is the collaboration surface where the
     community opens PRs; integration flows back into local. This
     needs fetch-and-integrate mechanics, *not* a blind push-mirror
     (see Deferred Problems).

4. **Off-box backup target is config-driven and transitions.** During
   the migration window the backup target is **GitHub** (the canonical
   mirror / safety net). In the endgame, GitHub retires and the target
   becomes **Codeberg**. The design treats "off-box mirror target" as
   configuration, not a hardwired provider.

5. **Disposable VM, durable data.** The service VM is long-lived but
   rebuildable: all persistent state lives on the host. Rebuilding the
   VM loses nothing. This preserves the project's existing
   "data-on-host, VM stateless" principle even for a permanent service.

## Architecture

A dedicated, long-lived **Lima VM** named `vergil-forge` — a new
*service* archetype alongside the existing disposable *agent-identity*
VMs, but using the same Lima mechanism already standardized in the
tooling.

Inside the VM, a container runtime runs two containers:

- **Forgejo** — the official Forgejo image.
- **Postgres** — the Forgejo database.

```text
macOS host (MacBook Pro)
│
├─ host-mounted volume (virtiofs)        ← all durable state lives here
│    forgejo-data/   (git repos, app.ini, attachments)
│    postgres-data/  (database files)
│
└─ Lima VM: vergil-forge (long-lived, disposable)
     └─ container runtime
          ├─ container: forgejo   (binds host forgejo-data)
          └─ container: postgres  (binds host postgres-data)
```

**Upgrades** are an image-tag bump (`forgejo:X.Y` → `forgejo:X.Z`),
followed by a container restart. A full VM rebuild re-provisions the
shell and re-attaches the same host data — no data migration.

## Database: Postgres

**Decision:** Postgres, not SQLite.

**(Data.)** SQLite uses a single-writer model: concurrent writers
serialize, and under contention they surface `SQLITE_BUSY` /
"database is locked". Forgejo mitigates this with WAL mode and a
busy-timeout (concurrent readers, one writer), but writers still
serialize. Forgejo's database documentation recommends SQLite only for
minimal/personal instances and a server database (Postgres/MySQL) for
deployments with real concurrency.

**(Judgment.)** "Single user" understates the concurrency here. The
human is one operator, but the workload runs **many agents in
parallel**, each performing git pushes, PR creation, label/status
writes, and CI webhook updates — distinct concurrent *writers*. That
is precisely the axis where SQLite is weakest and this workload is
heaviest. For a host built as the durable home, Postgres removes that
ceiling for a small, bounded cost (one container, one backup target),
and there is no risk of growing into a *remote multi-user team*
(Principle 2 caps that — teams go to Codeberg). So Postgres here is
about **parallel-agent write concurrency**, not user-count scale.

## Persistence & Backup

**Host volume layout** (the durable state):

- `forgejo-data/` — git repositories, `app.ini`, attachments, LFS.
- `postgres-data/` — the Postgres data directory.

**Backup (v1).** Two complementary mechanisms, deliberately light
because GitHub remains the canonical safety net during migration:

1. **Per-repo push-mirror** to the configured off-box target
   (`mirror.target`, GitHub today → Codeberg in the endgame). Forgejo's
   built-in push mirroring handles the solo-repo case directly.
2. **Periodic local snapshot** — a tarball of `forgejo-data` plus a
   `pg_dump` of the database, written to the host.

The backup bar is intentionally modest for v1 (GitHub is the canonical
mirror per #1521's migration model). It hardens as GitHub retires and
Codeberg becomes the backup of record.

## Networking & Identity

**Reachability (v1): host + its VMs only.** Forgejo is bound to a
Lima-forwarded port on the host. The disposable agent-identity Lima VMs
reach it via the host gateway (or a shared Lima network). Plain HTTP,
no TLS/DNS in v1 — consistent with a strictly local, single-operator
deployment.

**Identity bootstrap** provisions:

- An **admin** account for instance administration.
- The **`vergil-audit` bot** account plus a PAT — the audit-gate
  identity from #1521 Section 5. The bot posts the
  `vergil-audit/approved` commit status; branch protection requires
  that status context. The security property of the GitHub App model is
  preserved (only the bot can post the named status; merge is refused
  without it).
- The operator's **user** account plus a PAT.

Tokens are placed where `vrg-whoami` and the `ForgejoForge` adapter
expect to resolve them.

## Repository Layout

```text
vergil-forge/
├─ vergil.toml                  # managed-repo config (Python primary)
├─ pyproject.toml               # vrg-forge console script, deps
├─ src/vergil_forge/
│    ├─ cli.py                  # vrg-forge entry point
│    └─ ...                     # lifecycle, bootstrap, backup logic
├─ lima/
│    └─ vergil-forge.yaml       # Lima VM definition + provisioning
├─ compose/
│    └─ forgejo-stack.yml       # Forgejo + Postgres containers
├─ config/
│    └─ app.ini.template        # Forgejo config (branch-protection
│                               #   friendly defaults)
└─ docs/                        # operational docs
```

## The `vrg-forge` CLI

A single domain CLI, shipped from this repo:

| Command | Purpose |
|---|---|
| `vrg-forge bootstrap` | First-run: create VM, start stack, provision admin/bot/user accounts + tokens, configure mirrors |
| `vrg-forge up` | Start the VM and the Forgejo + Postgres stack |
| `vrg-forge down` | Stop the stack and the VM (data persists on host) |
| `vrg-forge status` | Report VM, container, and service health |
| `vrg-forge backup` | Snapshot `forgejo-data` + `pg_dump` to host |
| `vrg-forge restore` | Restore from a snapshot |
| `vrg-forge upgrade` | Bump the Forgejo image tag and restart |

## Scope

**In scope (v1):**

- The Lima service VM definition and provisioning.
- The Forgejo + Postgres container stack with host-mounted data.
- `app.ini` template with branch-protection-friendly defaults.
- Bootstrap of admin, `vergil-audit` bot, and user accounts + tokens.
- The `vrg-forge` lifecycle CLI.
- Off-box push-mirror backup to the configured target (GitHub in v1)
  plus local snapshots.
- Host-local networking (host + its VMs, plain HTTP).

**Later — named, not built now:**

- **Codeberg as backup target** (the GitHub → Codeberg transition).
- **Shared-repo sync** (the fetch-and-integrate flow; see Deferred
  Problems).
- **TLS / LAN / remote reach** and a stable hostname.
- **CI runners** (Forgejo Actions; #1521 Phase 4 — separable and
  deferrable).

## Deferred Problems (Recorded, Not Solved)

**Push-mirror directionality vs. shared repos.** A Forgejo push-mirror
is **one-directional and force-pushing** — it will clobber anything
pushed directly to the mirror, including community PR branches. The
same repo therefore cannot be both a naive push-mirror *and* a place
the community pushes to, on the same branches. The two repo roles
(Principle 3) need genuinely different sync mechanics:

- **Solo** → push-mirror is correct (no other writer).
- **Shared** → a fetch-and-integrate flow where the community owns PR
  branches on Codeberg and the operator owns integration; a blind
  push-mirror is wrong here.

This is an **endgame problem**, not a v1 problem — most repos are solo,
so the simple path covers the common case. It is recorded here so the
shared-repo phase does not assume the sync is trivial.

## Phased Path

- **Phase 0 — Bootstrap the host.** `vrg-forge bootstrap` stands up the
  VM, the Forgejo + Postgres stack on host-mounted data, and the
  accounts/tokens. Goal: a running, reachable local Forgejo.
- **Phase 1 — Prove the audit-gate loop.** With the `ForgejoForge`
  adapter (#1521), validate the full slice end-to-end: clone, branch,
  open a PR, post `vergil-audit/approved`, merge under required-context
  branch protection. This is #1521's Phase 0 spike, now running on a
  real host.
- **Phase 2 — Solo-repo migration.** Move pilot solo repos; enable
  push-mirror backup; validate the everyday loop and backups.
- **Phase 3+ — Deferred items** as they become necessary: Codeberg
  backup transition, shared-repo sync, TLS/remote, CI runners.

## Risks

| Risk | Mitigation |
|---|---|
| Service VM diverges from the disposable-VM model and becomes a pet | All state on host; the VM is rebuildable and re-attaches host data — disposable despite being long-lived |
| Postgres adds operational surface | One container + one `pg_dump` target; bounded because the instance never serves a remote team (Principle 2) |
| Backup too light during migration | GitHub remains the canonical mirror (#1521); backup hardens as GitHub retires |
| Shared-repo sync assumed trivial | Recorded as a deferred problem with explicit different mechanics; solo path unaffected |
| Inter-VM networking (agent VMs → forge VM) is fiddly | Host-local scope only in v1; reach via host gateway / shared Lima network, validated in Phase 0 |
| Forgejo upgrade breaks the stack | Image-tag bump is reversible; host data is snapshotted before upgrade |

## Dependencies

- A Lima-capable host (the MacBook Pro).
- `vergil-tooling` host install (consumed like any managed repo).
- The `ForgejoForge` adapter (#1521) for Phase 1 onward — the host can
  be bootstrapped (Phase 0) before the adapter is complete.
- `vrg-whoami` (#1520) for identity resolution as auth generalizes
  beyond GitHub.

## Open Questions

- Exact Lima networking choice for agent-VM → forge-VM reachability
  (host gateway vs. shared Lima network) — to be settled in Phase 0.
- Whether `app.ini` is fully templated or partly bootstrapped via the
  Forgejo admin API.
- Snapshot retention/rotation policy for local backups.
