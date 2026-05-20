# Multi-Platform Host Support Design

**Issue:** #909
**Date:** 2026-05-20
**Status:** Draft

## Problem

Vergil's development infrastructure targets Linux as the development
and CI/CD platform, supporting five programming languages inside
Docker containers. The host developer platform is currently macOS
(Apple Silicon). A natural question from prospective users: can I
run this on a Linux desktop or a Windows machine?

The host-OS surface area in `vergil-tooling` is small, but without
an explicit design, platform support remains accidental rather than
intentional.

## Governing Principles

1. **Prefer POSIX/portable standards over platform-specific idioms.**
   If a portable solution exists and the functionality loss is
   negligible, use it. Don't reach for platform-conditional code
   until a universal path has been ruled out.

2. **The VM boundary is the platform boundary.** With the
   identity-based VM isolation model (see
   `docs/specs/2026-05-20-identity-vm-isolation-design.md`), agents
   run inside a Linux VM. All Docker/container work happens there.
   The host is a desktop client that manages VM sessions and runs
   human-facing CLI tools.

3. **WSL2 is the Windows strategy.** Native PowerShell/CMD support
   is out of scope. WSL2 provides a real Linux environment where
   POSIX assumptions hold. This matches the convergence of serious
   developer tooling on Windows (VS Code Remote, Docker Desktop WSL
   backend).

4. **Linux is the development platform.** macOS, Linux desktop, and
   Windows are host platforms for hardware you sit in front of. The
   tooling produces Linux software, runs Linux CI, and deploys to
   Linux. No other development target platform is supported.

## Current State

An audit of the `vergil-tooling` codebase found the host-OS surface
area is already nearly fully portable:

**Already cross-platform (no changes needed):**

- All 22 `vrg-*` commands are Python entry points using `pathlib.Path`
  for path handling and `subprocess.run()` with explicit command lists
- No hardcoded absolute paths (`/Users/`, `/home/`)
- No `shell=True` subprocess calls
- No platform detection or conditional branching (except Docker
  architecture detection via `platform.machine()`)
- Install mechanism (`uv tool install`) is identical on all platforms
- Docker architecture detection already maps `arm64`, `aarch64`,
  `x86_64`, and `AMD64` to correct Docker platform flags

**Requires changes:**

- `.githooks/pre-commit` uses bash-specific `[[ ]]` syntax (should
  be POSIX `sh`)
- Flat-file audit logs at `~/.local/share/vergil/` are the only
  host-specific data directory (tracked in #902 for removal)

## Design

### Scope

This design covers `vergil-tooling` only. Host-side CLI tools, git
hooks, and documentation. CI stays Ubuntu. Dev containers stay
Linux.

Multi-platform VM lifecycle (hypervisor selection, provisioning,
platform-specific templates) is the concern of `vergil-vm` and is
specced separately.

### What Changes in `vergil-tooling`

**1. Pre-commit hook POSIX rewrite**

Replace bash-specific syntax in `.githooks/pre-commit` with POSIX
`sh` equivalents. Change shebang from `#!/usr/bin/env bash` to
`#!/usr/bin/env sh`. Replace `[[ ]]` conditionals with `[ ]`. The
file is 36 lines; the logic (env var checks and a case statement) is
fully expressible in POSIX `sh`.

**2. Remove flat-file audit logging (#902)**

`vrg-git` and `vrg-gh` append JSON-lines audit entries to
`~/.local/share/vergil/vrg-git.log` and `vrg-gh.log`. These files
grow without bound. Issue #902 tracks their removal and replacement
with a scalable approach. Once resolved, `vergil-tooling` has zero
host-specific data directories.

**3. Documentation: Supported Host Platforms**

Add a section to the docs explaining the platform support model:

- Linux is the development and CI/CD platform
- macOS, Linux desktop, and Windows (via WSL2) are supported host
  platforms for running the VM and human-facing CLI tools
- Host prerequisites: Python 3.12+ and `uv` (for `vrg-*` tool
  installation), POSIX `sh` (for git hooks), a hypervisor
  (platform-specific, managed by `vergil-vm`)
- Link to `vergil-vm` for platform-specific VM setup and
  provisioning

**4. No platform abstraction module**

A centralized `lib/platform.py` was considered and rejected. The
architecture detection code in `docker.py` will be reworked during
Phase 5 of the VM isolation plan (nerdctl adaptation). There is no
remaining platform-specific logic in `vergil-tooling` that warrants
centralization.

### What Does NOT Change

- All 22 `vrg-*` Python entry points (already portable)
- Docker integration code (moves inside the VM, reworked in VM
  plan Phase 5)
- CI workflows (stay Ubuntu)
- `vergil.toml` format (no platform fields needed)

### Contract with `vergil-vm`

The two repositories share a clear boundary:

**`vergil-tooling` assumes:**

- The host has Python 3.12+ and `uv`
- The host has POSIX `sh`
- A Linux VM is reachable when agent sessions run (provisioned by
  `vergil-vm`)
- `vrg-*` tools behave identically on the host and inside the VM
  (pure Python, no platform-conditional code)

**`vergil-vm` assumes:**

- `vergil-tooling` is installable inside the VM via `uv tool install`
- The pre-commit hook works with POSIX `sh`
- No host-specific data directories need to be mounted into the VM

### Per-Platform VM Strategy (owned by `vergil-vm`)

For reference, not specced here:

| Host OS | Hypervisor | Notes |
|---------|-----------|-------|
| macOS | Lima | Apple Virtualization.framework, virtiofs mounts, already specced |
| Linux desktop | QEMU/KVM or Lima | Lima also runs on Linux with the same YAML-driven interface |
| Windows | WSL2 | May suffice if isolation/egress requirements are met; Lima on Windows is experimental |

### Out of Scope

- Native Windows PowerShell/CMD support
- macOS or Windows as development target platforms
- Credential management tooling (each platform's secret store is the
  user's concern; `gh auth` and SSH keys handle the Vergil-relevant
  credentials)
- CI runner diversification
- VM lifecycle and provisioning (owned by `vergil-vm`)

## Testing Strategy

- Unit tests with mocked platform detection for any new
  platform-aware code (minimal — likely just the POSIX hook rewrite
  needs manual verification)
- Manual validation on actual Linux desktop and Windows/WSL2 hardware
  when available
- No CI matrix expansion (host-side tools are pure Python; the
  pre-commit hook is too simple to warrant cross-platform CI)

## Dependencies

- #902 — Remove flat-file audit logs (unblocks removal of
  `~/.local/share/vergil/`)
- `vergil-vm` Phase 5 — vergil-tooling adaptations for nerdctl and
  in-VM execution (reworks Docker integration, may affect
  architecture detection code)

## Summary

`vergil-tooling` is already 99% portable. The pre-commit hook gets
a POSIX rewrite, the flat-file logs go away via #902, the docs get a
platform support section, and all real multi-platform engineering
lives in `vergil-vm`.
