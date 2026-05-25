# Stateless VM Lifecycle Design

**Issue:** #907
**Date:** 2026-05-24
**Status:** Draft
**Supersedes:** Distribution and Dynamic Tooling Management sections
of #894 (`2026-05-20-vergil-vm-image-management-design.md`)
**Related:** #892 (identity VM isolation), #894 (VM image
management), mempalace (transcript mining)

## Problem

The identity VM isolation design (#892) and the VM image management
design (#894) assumed that VMs would be long-lived and that keeping
them current required two mechanisms: centrally published pre-built
images for onboarding, and a dynamic in-place update system for OS
packages and tooling.

Both assumptions are wrong for this system:

1. **Published images are over-engineering.** Docker images already
   solve the "works on my machine" problem for development — the
   entire build/test/validate toolchain lives in vergil-docker
   containers consumed via `vrg-container-run`. The VM's role is
   narrower: an isolation boundary that runs a container runtime
   and a handful of CLI tools. A provisioning script that installs
   five tools on a stock Ubuntu image does not produce meaningful
   variance across machines. Publishing pre-built images adds a
   CD pipeline, registry management, and multi-arch build
   complexity for a problem that doesn't exist.

2. **In-place OS updates are the wrong model.** VMs should be
   rebuilt frequently, not patched in place. Frequent rebuilds
   keep the environment fresh (latest OS packages, latest tool
   versions), keep agent contexts from accumulating drift, and
   align with the principle that VMs are disposable compute —
   not persistent workstations.

The key insight: **VMs must be dataless and stateless.** All
persistent data lives on the macOS host, accessed via mounts.
The VM is a disposable shell. Rebuilding it loses nothing.

This design replaces the distribution and dynamic tooling
management sections of #894 with a simpler model: local builds,
frequent rebuilds, and targeted in-place updates for
vergil-tooling only.

## Core Principle: Dataless, Stateless VMs

The identity VM contains no state that survives a rebuild. All
persistent data lives on the macOS host, accessed via virtiofs
mounts and file copies at VM startup.

### Mounts

| Mount | Default host path | VM path | Configurable? |
|---|---|---|---|
| Workspace | `~/dev` | Host-path-preserving | Yes |
| Claude projects | `~/.claude/projects` | `~/.claude/projects` (native) | No |
| Claude skills | `~/.claude/skills` | `~/.claude/skills` (native) | No |

**Workspace mount:** Host-path-preserving — if the host path is
`/Users/pmoore/dev`, the VM mount point is `/Users/pmoore/dev`.
This ensures Claude Code derives identical project memory keys
regardless of whether a session runs on the host or in the VM.
The workspace mount path is configurable per identity (via
`identities.toml`), since developers organize their source trees
differently. One developer may mount `~/dev`, another
`~/dev/projects`, another `~` entirely. The mount must be
host-path-preserving regardless of which directory is chosen.

**Claude projects mount:** The host's `~/.claude/projects/`
directory is mounted at the VM's native `~/.claude/projects/`.
This contains session transcripts and memory files — the data
that mempalace mines and that provides continuity across
sessions.

**Claude skills mount:** The host's `~/.claude/skills/` directory
is mounted at the VM's native `~/.claude/skills/`. User-defined
skills are shared across host and VM sessions.

Only specific subdirectories are mounted, not all of `~/.claude/`.
This is deliberate: Claude Code credentials (`.credentials.json`)
are injected per-identity by `vrg-vm` and must remain VM-local.
Mounting all of `~/.claude/` would cause per-identity credential
files to bleed onto the host or collide between VMs.

### Copied on VM Start

Individual files that cannot be mounted (Lima mounts directories,
not files) are copied from the host into the VM at startup,
during the credential injection phase:

| File | Purpose |
|---|---|
| `~/.claude/CLAUDE.md` | User's global preferences and behavioral guidance |
| `~/.claude/settings.json` | User's global Claude Code settings |

These are one-way copies (host → VM). Changes made inside the
VM are not persisted — the host copy is authoritative.

### VM-Local (Not Shared)

Everything else in the VM's `~/.claude/` directory is local to
that VM instance and destroyed on rebuild:

| Path | Why VM-local |
|---|---|
| `.credentials.json` | Per-identity Claude Code OAuth token, injected by `vrg-vm` |
| `plugins/` | Plugin cache, re-fetched automatically |
| `cache/`, `debug/`, `downloads/` | Ephemeral working state |
| `history.jsonl` | Per-environment command history |
| `sessions/`, `session-env/` | Per-instance session state |
| `statsig/`, `telemetry/` | Analytics, per-instance |

### Why This Works

Claude Code derives project memory keys from the working
directory path, not from where `~/.claude` lives. Because repos
are mounted at host-preserving paths, the derived key (e.g.,
`-Users-pmoore-dev-projects-vergil-project-vergil-tooling`) is
identical whether the session runs on the host or in the VM.
Both environments read and write the same physical files in
`~/.claude/projects/` through different mount points.

**Mempalace integration:** Mempalace mines `~/.claude/projects/`
on the host. It is unaware of VMs. Sessions that ran inside VMs
produce transcripts in the same location as host sessions —
mempalace sees them all. No special configuration, no
VM-awareness in mempalace.

**Concurrent access:** Running Claude Code on the host and inside
a VM simultaneously is safe — the same pattern as running
multiple Claude Code instances in separate terminal windows
today.

### Consequence

Rebuilding a VM loses nothing of value. Session transcripts and
memory files persist on the host via the `projects/` mount.
User preferences are re-copied on next start. Credentials are
re-injected. No backup, no migration, no data export. Destroy
and recreate.

## Update Strategy: Two Tiers

The VM has exactly two categories of software with different
update mechanisms.

### Tier 1: vergil-tooling (In-Place, Aggressive)

vergil-tooling is the sole component updated in place. It
releases frequently and a bug fix mid-session should not require
a full VM rebuild.

**Mechanism:**

All update orchestration runs on the host via `vrg-vm`. Nothing
inside the VM drives updates — the VM is a passive target.

- `vrg-vm update` reinstalls vergil-tooling inside the VM via
  `uv tool install --reinstall`, executed through `limactl shell`.
- The version target is read from `identities.toml` on the host
  (the `vergil` key at identity or config level). No version
  configuration is stored inside the VM.
- Both `vrg-vm start` and `vrg-vm session` auto-update
  vergil-tooling — ensures the VM is current on every entry
  point. The cost is low and eliminates stale-tooling scenarios.
  If the update fails (network unavailable, GitHub unreachable),
  the command warns and continues with the installed version.
- The user can run `vrg-vm update` explicitly at any time to
  pick up a new release mid-session.
- A marker file inside the VM (`~/.config/vergil/tooling-tag`)
  records which tag was last installed. This is ephemeral state —
  destroyed on rebuild, used only by the update mechanism to
  detect the current version.

**Scope constraint:** `vrg-vm update` updates vergil-tooling and
nothing else. It does not touch OS packages, git, gh, uv,
containerd, nerdctl, or any other software in the VM.

### Tier 2: Everything Else (Rebuild From Scratch)

OS packages, git, gh, uv, containerd, nerdctl, developer
convenience tools — all installed by the provisioning script at
VM creation time. No in-place update machinery. When the VM is
stale, destroy it and rebuild. The provisioning script pulls
the latest versions of everything at build time.

### Summary

| Component | Update mechanism | Cadence |
|---|---|---|
| vergil-tooling | `vrg-vm-update` (in-place) | Aggressive, per-release |
| OS + base tools | Rebuild VM from scratch | When stale (every few days) |
| Persistent state | Host-mounted, survives rebuild | N/A |

## Staleness Enforcement

`vrg-vm start` checks the VM creation date against a configurable
threshold. If the VM is older than the threshold, the command
**fails with an error** and refuses to start.

`vrg-session` similarly refuses to connect to a stale VM.

The VM creation timestamp is read from Lima's own VM metadata
(external to the VM). Nothing is stored inside the VM for this
purpose.

**Default threshold:** 3 days. Configurable. This cadence may be
tuned based on experience.

**Error message:**

```
ERROR: VM 'vergil-agent' is 5 days old (threshold: 3 days).
Rebuild with: vrg-vm rebuild vergil-agent
Override with: vrg-vm start --allow-stale-vm vergil-agent
```

**Override:** The `--allow-stale-vm` flag bypasses the staleness
check. The flag name is deliberately verbose — a friction
mechanism, not a convenience. It is available on both
`vrg-vm start` and `vrg-session`.

The staleness check is a hard block by default, not a warning.
The user must consciously choose to proceed with a stale VM.

## VM Lifecycle: Build, Use, Rebuild

The VM lifecycle is deliberately simple. No migration, no
snapshots, no in-place OS upgrades.

### Build

```bash
vrg-vm create [--workspace ~/dev] [--name vergil-agent]
```

The provisioning script runs once at creation time and installs
everything: Ubuntu LTS with latest packages, git, gh, uv,
containerd, nerdctl, developer convenience tools. vergil-tooling
is installed last via `vrg-vm update` (host-side, through
`limactl shell`). The workspace mount path is read from
`identities.toml` by default; the `--workspace` flag overrides
it for a single invocation.

Build takes a few minutes. It is a local operation — no images
to download, no registry to authenticate against.

### Use

```bash
vrg-vm start vergil-agent
vrg-session <project-name>
```

On start, the staleness check runs first. If the VM passes,
vergil-tooling is auto-updated. The workspace and
`~/.claude/projects/` mounts are established by Lima
automatically.

Day-to-day, the VM is an environment that Claude Code sessions
run inside. The user interacts via `vrg-session`, not by SSHing
into the VM directly (though SSH is available for debugging and
triage).

### Rebuild

```bash
vrg-vm rebuild vergil-agent
```

Equivalent to destroy + create. Because the VM is stateless,
this is safe at any time — all persistent data lives on the host
mounts. The rebuild pulls the latest OS packages, latest tool
versions, and installs the current vergil-tooling release. Fresh
start in a few minutes.

The staleness error message directs users here when the threshold
is exceeded.

### No Publish, No Pull, No Registry

VM images are not distributed. Every developer builds locally
from the same provisioning scripts in the vergil-vm repository.
The provisioning scripts are the source of truth — versioned,
reviewed, and tested like any other code.

Publishing pre-built images may be revisited when a team is
actively onboarding onto Vergil. The local-build approach does
not preclude adding a publish pipeline later — it simply defers
the complexity until there is a consumer base to justify it.

## Impact on Existing Design

This design supersedes the Distribution and Dynamic Tooling
Management sections of the VM image management design (#894).

### Eliminated

| Original scope | Reason |
|---|---|
| Published pre-built images (GitHub Releases / OCI) | Over-engineering for current consumer base; Docker images already solve reproducibility |
| CD workflow for VM image publishing | Nothing to publish |
| Published Lima template (`agent-published.yaml`) | No registry to reference |
| In-place OS / general tool updates | Rebuild from scratch handles this |
| `/etc/vergil/vm.conf` | Version config lives on the host in `identities.toml`, not inside the VM |
| In-VM startup hooks | Updates orchestrated by host-side `vrg-vm`, not by scripts inside the VM |

### Retained (Reduced Scope)

| Component | Original purpose | Revised purpose |
|---|---|---|
| `vrg-vm update` | General dynamic tooling management | vergil-tooling updates only |
| `identities.toml` `vergil` key | Version config for dynamic tooling | vergil-tooling version target (host-side, no in-VM config) |
| `vrg-vm start`/`session` auto-update | Install/update all tooling at startup | Auto-update vergil-tooling on every entry point |

### Added

| Component | Purpose |
|---|---|
| `~/.claude/projects/` and `~/.claude/skills/` host mounts | Persist Claude Code memory, transcripts, and skills across VM rebuilds |
| Configurable workspace mount | Support user-specific source tree layouts via `identities.toml` |
| `vrg-vm rebuild` command | Destroy + create as a single operation |
| `--allow-stale-vm` override | Deliberate friction for bypassing staleness enforcement |
| Staleness enforcement | Hard block in `vrg-vm start` and `vrg-session` |

## Plan 7: Site Documentation

The identity VM system, stateless design, mempalace integration,
and three-layer architecture require comprehensive site
documentation. This is a significant writing effort — the design
is worth explaining well. A separate plan (Plan 7) will cover
the documentation review and update.

## References

- [#892 — Identity VM isolation design](https://github.com/vergil-project/vergil-tooling/issues/892)
- [#894 — VM image management design](https://github.com/vergil-project/vergil-tooling/issues/894)
- [#907 — VM image distribution and dynamic updates](https://github.com/vergil-project/vergil-tooling/issues/907)
- [mempalace — AI memory system](https://github.com/mempalace/mempalace)
- [Lima — Linux virtual machines on macOS](https://lima-vm.io/)
