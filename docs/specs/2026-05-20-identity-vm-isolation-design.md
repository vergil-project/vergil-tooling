# Identity-Based VM Isolation Design

**Issue:** #892
**Date:** 2026-05-20
**Status:** Draft
**Related:** #882 (Docker-based isolation exploration), #775
(credential management), #805 (Vergil/Mimir identity)

## Problem

The current agent security model uses four layers of defense: Claude
Code permission denylists, `vrg-git`/`vrg-gh` wrapper allowlists,
Vergil plugin hooks, and git-level hooks. This works, but has
structural gaps and a growing maintenance burden:

1. **No filesystem isolation.** Agents run as the human user on the
   host. They can read `~/.aws/credentials`, `~/.ssh/id_rsa`, browser
   cookies, other projects' `.env` files — anything the human can
   read. Security is enforced by constraining tool invocations, not
   access.

2. **No network control.** Agents can make arbitrary HTTP requests,
   `curl` credentials to external endpoints, `pip install` from
   untrusted sources. A prompt injection attack could exfiltrate
   credentials with no defense layer to prevent it.

3. **Credential isolation is policy, not physics.** The two-account
   model (`wphillipmoore` and `wphillipmoore-vergil`) relies on wrapper logic to
   select the right token per operation. Both tokens exist on the
   same filesystem. The wrapper is the only thing preventing the
   agent from using human credentials directly.

4. **Wrapper maintenance scales linearly.** Every new git subcommand,
   every new gh operation, every credential escalation path means
   updating allowlists across multiple files and testing
   interactions across four layers.

Issue #882 explored Docker-based containerization as a solution but
deferred it due to UX concerns and prerequisites. A comparative
evaluation of the [corral project](https://gitlab.com/dmorel69/corral)
— a Lima VM-based isolation tool designed specifically for running
Claude Code in sandboxed environments — revealed a complementary
approach that addresses all four gaps while preserving everything
Vergil does well.

## External Reference: Corral

[Corral](https://gitlab.com/dmorel69/corral) is an MIT-licensed CLI
tool by David Morel that runs Claude Code inside per-project Lima
VMs. Key characteristics relevant to this design:

- **Lima VM isolation:** Real kernel boundary via Apple's
  Virtualization Framework (ARM) or QEMU (Intel). Host home
  directory, SSH keys, credentials — all invisible to the VM.
- **Egress filtering:** Three-layer network control (iptables inside
  VM for convenience, HAProxy SNI allowlist on host for enforcement,
  pf firewall on host for perimeter). The VM cannot subvert host-side
  rules even with root access.
- **Rootless containerd:** Docker-compatible container workflows
  inside the VM via nerdctl. No Docker Desktop required.
- **Workspace mounting:** Project directories mounted into the VM
  via virtiofs.
- **Snapshot/reset:** Clean-state snapshots captured post-provision;
  `corral reset` restores them instantly.
- **Minimal credential crossing:** Only `ANTHROPIC_API_KEY` crosses
  the VM boundary by default. Everything else must be explicitly
  provisioned.
- **macOS-only:** Depends on Lima, which uses Apple's Virtualization
  Framework or QEMU. No Linux or Windows support.

Corral's design is per-project: one VM per workspace. This spec
proposes a different scoping that better fits Vergil's operational
model.

## Core Insight: VM Per Identity, Not Per Project

Corral scopes isolation to the **project**. Vergil's concern is the
**agent identity**. These are different isolation units, and choosing
the wrong one creates a scaling problem.

A developer working on 5-10 repositories simultaneously cannot
sustain 5-10 Lima VMs on a laptop — especially one also running
local LLMs via Ollama. But the same developer has only 1-2 agent
identities (`wphillipmoore-vergil`, and potentially
`wphillipmoore-mimir` in future).

**The identity VM model:** Each agent identity gets a single
persistent Lima VM provisioned with that identity's credentials and
toolchain. The VM mounts all workspaces the identity needs access to.
Inside the VM, git provides project-level separation — each
repository is its own workspace with its own `.git`, branches, and
worktrees. No additional isolation is needed between projects within
the VM.

This scales to 1-2 VMs regardless of project count. Resource cost
is bounded and predictable.

## Architecture

### Stack Overview

```text
macOS Host
├── Human's environment
│   ├── iTerm (SSH sessions into identity VMs)
│   ├── IDE (optional, for human-side editing)
│   ├── Ollama (local LLMs, priority on RAM)
│   └── Browser, tools, etc.
│
├── Network control (host-enforced, VM cannot subvert)
│   ├── pf firewall (per-VM egress rules)
│   └── HAProxy (SNI allowlist enforcement)
│
├── wphillipmoore-vergil VM (Lima, persistent, ~1-2 GB RAM)
│   ├── Identity
│   │   ├── GitHub PAT (wphillipmoore-vergil: repo, read:org)
│   │   ├── SSH key (wphillipmoore-vergil)
│   │   └── User: agent (uid 1000)
│   ├── Toolchain
│   │   ├── vergil-tooling (vrg-commit, vrg-git, etc.)
│   │   ├── uv, python, node, gh, git
│   │   └── rootless containerd + nerdctl
│   ├── Workspace mounts (virtiofs, user-configurable)
│   │   ├── ~/dev/projects/vergil-project/*
│   │   ├── ~/dev/projects/diogenes-project/*
│   │   └── ~/dev/github/*
│   ├── Agent memory
│   │   └── /var/lib/claude-mem (per-identity disk)
│   └── Concurrent sessions
│       ├── claude (vergil-tooling, issue #900)
│       ├── claude (vergil-actions, issue #180)
│       └── claude (diogenes-core, issue #42)
│
└── wphillipmoore-mimir VM (future, same architecture)
    └── Different credentials, different capabilities
```

### Isolation Boundaries

Three boundaries, each serving a distinct purpose:

| Boundary | Enforces | Mechanism |
|---|---|---|
| VM (Lima) | Access control: what the agent can see and reach | Kernel-level isolation via hypervisor |
| Egress filter (HAProxy + pf) | Network control: where the agent can connect | Host-side firewall rules, immune to in-VM tampering |
| Vergil wrappers (vrg-git, vrg-gh) | Workflow standards: how the agent operates | Subcommand allowlists, audit logging |

The first two boundaries are infrastructure. The third is policy.
This separation means the infrastructure handles security (credential
isolation, filesystem containment, network control) while the
wrappers focus exclusively on workflow enforcement (commit standards,
branch naming, PR structure).

### Human Interaction Model

The human interacts with agent sessions via SSH into the identity
VM, running Claude Code inside the VM's terminal environment:

**Today:**
```bash
cd ~/dev/projects/vergil-project/vergil-tooling
claude
```

**With identity VM:**
```bash
vrg-session vergil-tooling
# Wraps: ssh agent@vergil-vm -t \
#   'cd /workspace/vergil-tooling && claude'
```

Each iTerm tab is a separate SSH session into the same VM, running
an independent Claude Code instance on a different project or issue.
Claude Code is a terminal application; SSH is a terminal. The
experience is functionally identical.

For ad-hoc access:
```bash
ssh agent@vergil-vm
```

The `vrg-session` wrapper is a thin convenience script (~20 lines)
that handles SSH connection, directory navigation, and `claude`
launch. It is not a security boundary.

### Workspace Mounting

Workspace mounts are user-configurable. The VM template accepts a
list of host directories to mount via virtiofs. A reasonable default
for most users is their entire development tree:

```toml
# ~/.config/vergil/identities.toml (conceptual)
[vergil]
mounts = ["~/dev"]
github_user = "wphillipmoore-vergil"
```

Inside the VM, mounted paths preserve their host-side absolute
paths for portability. The agent `cd`s into whichever project it's
assigned to work on. Git provides project-level separation — each
repository is its own `.git` with its own branches and worktrees.

**Design decision:** Mount broadly rather than narrowly. The VM
boundary already isolates the agent from host secrets. Within the
VM, the agent should have access to everything it might need to
work on. Restricting per-project visibility within the VM adds
complexity without meaningful security benefit — the credentials
are identity-scoped, not project-scoped.

### Credential Provisioning

The VM is provisioned with exactly the credentials the identity
needs. No more, no less.

**Design principle:** Every interaction the agent has with an
external system should appear under the agent's own identity, not
the human's. The VM is the enforcement boundary for this — the
agent's credentials live inside the VM, the human's credentials
stay on the host, and the two never mix. This principle extends to
any external service the agent interacts with, not just GitHub.

**Current credentials (Vergil workflow):**

| Credential | How it enters the VM | Scope |
|---|---|---|
| GitHub PAT | Provisioned at VM creation via bootstrap | `repo`, `read:org` (agent-level scope per #775) |
| SSH key | Provisioned at VM creation via bootstrap | wphillipmoore-vergil's key |

**Extensible by design:** The credential provisioning model is not
limited to GitHub. As agent workflows expand to interact with
additional external systems, each gets its own agent-scoped
credentials provisioned into the VM:

| Future credential | Use case | Identity model |
|---|---|---|
| AWS IAM credentials | Cloud infrastructure, deployments | Dedicated IAM user or role per agent identity |
| Container registry tokens | Pushing images to GHCR, ECR, etc. | Agent-scoped token |
| Additional SaaS API keys | CI services, monitoring, etc. | Per-agent credentials as needed |

The pattern is consistent: the agent identity has its own account
or role in each external system, and only those credentials are
provisioned into the VM. The human's credentials for the same
systems remain on the host.

**Exception — `ANTHROPIC_API_KEY`:** The one credential that
crosses the VM boundary at session launch time is the Anthropic API
key, because the agent runs under the human's Anthropic account.
This is passed per-session (not baked into the VM) and is the only
shared credential in the model. This exception may disappear when
local LLM usage via Ollama replaces or supplements the Anthropic
API.

Human credentials (`wphillipmoore`'s PAT, SSH key, AWS credentials,
etc.) never enter the VM. They exist only on the host. The VM
boundary is the credential isolation mechanism — no wrapper logic
required.

**Consequence for vrg-gh:** The `_discover_accounts()` function,
credential selection logic, and escalation dance for `pr merge` /
`issue close` are all deleted. Inside the VM, there is exactly one
GitHub identity. `vrg-gh` becomes a pure workflow enforcement tool:
subcommand allowlist + audit logging.

**Consequence for operations requiring human credentials:** If an
operation requires human-level access (e.g., merging a PR with
branch protection requiring specific reviewers), the agent cannot
perform it. It asks the human, who acts from the host using their
own credentials. This is cleaner than the current escalation model
— the boundary between "agent can do this" and "human must do this"
is the VM wall, not wrapper logic.

### Egress Filtering

Adopted from corral's three-layer model, adapted for identity VMs:

**Layer 1 — iptables inside VM (convenience):** DNAT redirects port
443 to the host-side HAProxy. This is convenience only — a root
attacker inside the VM can flush these rules.

**Layer 2 — HAProxy on host (enforcement):** Inspects TLS
ClientHello SNI against an allowlist of permitted hostnames.
Connections to non-allowlisted hosts are reset before the TLS
handshake completes. The VM cannot modify HAProxy configuration.

**Layer 3 — pf on host (perimeter):** Kernel-level packet filter.
All non-port-443 traffic from the VM subnet is dropped. The VM
cannot access pf rules.

**Allowlist composition:**

1. **Identity baseline:** Hosts that any agent session needs
   (`api.anthropic.com`, `github.com`, `api.github.com`, `ghcr.io`,
   `pypi.org`, `registry.npmjs.org`, etc.). Shipped with the
   tooling.
2. **Per-project overlay:** Additional hosts a specific project
   needs (e.g., a staging API for integration tests). Stored in
   `<project>/.corral/egress.allow`, version-controllable,
   travels with the repo.

**What this prevents:** Prompt injection attacks that attempt to
exfiltrate credentials via `curl` to external endpoints. The
connection is killed at the HAProxy layer before the TLS handshake
completes. This is the network security layer Vergil currently
lacks entirely.

### Validation Pipeline (Unchanged)

The Docker-based validation pipeline runs inside the VM without
modification:

```text
Human launches: vrg-session vergil-tooling
└── SSH into wphillipmoore-vergil VM
    └── Claude Code runs
        └── Agent works on vergil-tooling
            └── vrg-commit triggers pre-commit hook
                └── vrg-docker-run -- vrg-validate
                    └── nerdctl run (rootless container)
                        └── Same dev image as CI
                            └── vrg-validate runs all checks
```

The validation container inside the VM uses the same images that CI
uses on GitHub Actions. Tier 1 / Tier 2 parity is preserved. The
agent's commits pass through identical checks locally and in CI.

Corral's VM image includes rootless containerd with nerdctl, which
provides Docker-compatible container operations. `vrg-docker-run`
would use `nerdctl` instead of `docker` as the container runtime
inside the VM. This may require a small adaptation in
`vrg-docker-run` to detect the available runtime.

### Agent Memory

Corral provisions a dedicated ext4 block device per VM for Claude
Code's persistent memory (`claude-mem`). In the identity VM model,
this becomes per-identity memory:

- The `wphillipmoore-vergil` identity has one memory store shared across
  all projects it works on.
- Memory persists across VM restarts, resets, and even VM deletion
  (the disk is retained separately).
- Multiple concurrent Claude Code sessions in the same VM share
  the memory store (Claude Code handles concurrent access via
  SQLite).

**Per-identity vs. per-project memory:** Per-identity is the
initial design choice. The agent builds cross-project context
(e.g., understanding how vergil-tooling and vergil-actions relate).
This may prove more valuable than isolated per-project memory. If
separation is needed later, it can be implemented via Claude Code
configuration rather than VM-level disk isolation.

**Open question:** How does Claude Code's `claude-mem` behave with
multiple concurrent sessions? This needs empirical testing during
the proof-of-concept phase.

## Wrapper Simplification

### Before (Current Model)

| Layer | Purpose | Complexity |
|---|---|---|
| Claude Code `settings.json` | Deny raw git/gh, allow vrg-* | Credential + workflow |
| `vrg-git` | Subcommand allowlist, flag denylist, protected branches, audit | Credential + workflow |
| `vrg-gh` | Subcommand allowlist, two-account discovery, credential selection, escalation | Credential + workflow |
| Plugin hooks | Additional enforcement | Credential + workflow |
| Git hooks | Gate commits through vrg-commit | Workflow |

### After (Identity VM Model)

| Layer | Purpose | Complexity |
|---|---|---|
| VM boundary | Credential isolation, filesystem isolation, network control | Infrastructure |
| `vrg-git` | Subcommand allowlist, flag denylist, audit | Workflow only |
| `vrg-gh` | Subcommand allowlist, audit | Workflow only |
| Git hooks | Gate commits through vrg-commit | Workflow (unchanged) |
| Claude Code `settings.json` | Deny raw git/gh, allow vrg-* | Defense in depth |

### What Gets Deleted

- `vrg-gh`: `_discover_accounts()`, credential selection logic,
  `GH_TOKEN` injection per subprocess, escalation for `pr merge` /
  `issue close`
- `vrg-git`: Protected-branch force-push guards that exist for
  credential protection (workflow guards remain)
- Plugin hooks: Credential-related enforcement (workflow enforcement
  may remain)

### What Stays Unchanged

- `vrg-commit`: Conventional commits, branch naming, issue linking,
  co-author headers
- `vrg-submit-pr`: PR structure and standards
- `vrg-validate`: Full validation pipeline via `vrg-docker-run`
- Git hooks: Pre-commit gate requiring `vrg-commit`
- `vrg-git` subcommand allowlist and audit logging
- `vrg-gh` subcommand allowlist and audit logging
- Claude Code `settings.json` denylists (defense in depth — the
  wrappers remain useful even inside the VM because they enforce
  workflow standards, not just credentials)

## Comparative Evaluation

### Security Model

| Dimension | Current Vergil | Identity VM |
|---|---|---|
| Isolation primitive | Software-layer allowlists | Kernel boundary |
| Credential isolation | Two-account wrapper logic | VM wall |
| Filesystem isolation | None | Workspace mount only |
| Network control | None | SNI-allowlisted HTTPS + pf |
| Attack surface if agent is compromised | Full host access minus denied commands | Workspace contents only |
| Defense against prompt injection exfiltration | None | Egress filter blocks unauthorized connections |

### Operational Comparison

| Dimension | Current Vergil | Identity VM |
|---|---|---|
| Maintenance burden | Every new workflow = allowlist updates across 4 layers | VM image updates (less frequent) + workflow-only wrapper updates |
| Credential management | Two accounts, token selection, escalation | One account per VM, no selection logic |
| Resource cost | Negligible (Python scripts) | ~1-2 GB RAM per identity VM |
| Setup complexity | `uv tool install` + hooks | Lima + VM provision + tooling install |
| Cross-platform | macOS + Linux | macOS only (local), Linux (CI unchanged) |
| Parallel agents | Worktrees + prompt contracts | Multiple SSH sessions into same VM |
| Recovery from agent errors | `git checkout` / recreate worktree | `git checkout` per project, VM reset as escape valve |

### What Corral Provides That Vergil Lacks

1. **Filesystem isolation.** Host secrets are invisible to the agent.
2. **Network egress control.** Agents cannot exfiltrate data to
   arbitrary endpoints.
3. **Snapshot/reset.** Clean-state restoration without re-provisioning.
4. **Minimal credential crossing.** Explicit provisioning model
   instead of ambient host credentials.

### What Vergil Provides That Corral Lacks

1. **Workflow enforcement.** Commit standards, branch naming, PR
   structure — enforced by tooling, not left to agent judgment.
2. **Two-identity audit trail.** Distinct GitHub accounts for agent
   vs. human work, visible in git log and PR history.
3. **CI parity.** Same validation locally and in CI via shared
   container images.
4. **Cross-platform support.** Docker-first model works on Linux
   and in CI natively.

### The Complementarity

These two systems solve non-overlapping halves of the problem:

| Problem | Vergil | Corral |
|---|---|---|
| Enforce commit standards | Yes | No |
| Enforce branch conventions | Yes | No |
| Enforce PR structure | Yes | No |
| Isolate host credentials | No | Yes |
| Isolate host filesystem | No | Yes |
| Control network egress | No | Yes |
| Provide clean-room resets | No | Yes |
| CI integration | Yes | No |
| Cross-platform | Yes | No |

The combined system is stronger than either alone: corral provides
the isolation boundary, Vergil provides the workflow enforcement.

## Implementation Strategy

### Phase 1: Evaluation (2-4 weeks)

Install corral alongside the existing Vergil stack and run real
agent work through it to validate assumptions:

1. Install corral on the development machine.
2. Provision a VM for one active project using `wphillipmoore-vergil`
   credentials (corral's per-project model, not yet identity-scoped).
3. Install vergil-tooling inside the VM via `uv tool install`.
4. Run real agent work sessions: feature development, bug fixes,
   parallel agents.
5. Evaluate:
   - UX friction of SSH-based Claude Code sessions
   - Resource consumption (RAM, CPU, disk) under real workloads
   - virtiofs performance with workspace mounts
   - `vrg-docker-run` behavior inside the VM (nerdctl compatibility)
   - Egress filter false positives
   - Snapshot/reset utility in practice
   - `claude-mem` behavior with concurrent sessions

### Phase 2: Identity VM Adaptation

Based on Phase 1 findings, adapt corral's model for identity-scoped
VMs. This is either:

- **Upstream contribution:** If corral's architecture supports the
  identity VM concept without major changes, contribute the
  multi-mount and identity-naming features upstream.
- **Fork or extension:** If the changes are too divergent from
  corral's per-project design philosophy, fork or build a
  Vergil-native Lima wrapper using corral as a reference
  architecture.
- **Vergil-native implementation:** If Lima integration is
  straightforward enough, build the identity VM tooling directly in
  vergil-tooling using Lima as a library dependency.

The choice depends on Phase 1 findings and on alignment with
corral's maintainer on the identity VM concept.

### Phase 3: Wrapper Simplification

Once identity VMs are operational:

1. Remove credential selection logic from `vrg-gh`.
2. Remove credential-protection guards from `vrg-git` (keep
   workflow guards).
3. Simplify or remove plugin hooks related to credential
   enforcement.
4. Update documentation and specs to reflect the new architecture.
5. Update the credential management design (#775) to reflect VM-based
   isolation.

### Phase 4: Second Identity (Future)

When the Mimir identity (#805) is implemented:

1. Provision a second identity VM (`wphillipmoore-mimir`) with Mimir's
   credentials and capabilities.
2. Configure distinct egress allowlists reflecting Mimir's different
   operational profile.
3. Validate that two concurrent identity VMs work within resource
   constraints alongside Ollama.

## Risk Assessment

### Low Risk

- **Vergil tooling inside the VM.** Python, installed via uv, no
  macOS-specific dependencies.
- **SSH-based Claude Code.** Terminal application over terminal
  protocol. Functionally identical UX.
- **Parallel sessions in one VM.** Separate SSH connections,
  separate processes, separate project directories.
- **Egress filtering.** Corral has this solved; adopting it is
  configuration, not development.

### Medium Risk

- **virtiofs performance.** Large workspace mounts (entire `~/dev`
  tree) need real-world benchmarking. File-heavy operations
  (git status on large repos, npm install, compilation) may show
  latency differences.
- **Concurrent validation containers.** Multiple `vrg-docker-run`
  invocations inside one VM create memory pressure from overlapping
  containers. May need container-level memory limits.
- **VM stability under sustained load.** Lima VMs under continuous
  agent workloads for hours/days need endurance testing.
- **nerdctl compatibility.** `vrg-docker-run` currently targets
  Docker. Adapting to nerdctl may surface minor incompatibilities
  in volume mounting, networking, or image pulling.

### Unknown Risk (Resolved by Phase 1)

- Actual resource consumption of one Lima VM under typical agent
  load.
- Whether corral's architecture can be adapted to multi-mount
  identity VMs or needs significant rework.
- How `claude-mem` behaves with multiple concurrent Claude Code
  sessions in one VM.
- Whether corral's maintainer is interested in the identity VM
  concept for upstream collaboration.

## Open Questions

1. **Corral integration model.** Adopt as dependency, fork, or use
   as reference architecture? Depends on Phase 1 findings and
   upstream alignment.

2. **IDE support.** Terminal-first (SSH) is sufficient for the
   current user. For broader adoption, options include VS Code
   Remote-SSH (connects to the VM as a remote host) or running a
   full IDE inside the VM with display forwarding. Not a Phase 1
   concern.

3. **Reset granularity.** With multiple projects in one VM, VM-level
   reset affects all projects. Per-project reset is git-level
   (`git checkout`, worktree cleanup). VM-level reset becomes a
   "nuke the agent, start fresh" escape valve. Whether this is
   sufficient depends on operational experience.

4. **Memory model.** Per-identity memory (one store across all
   projects) vs. per-project memory (isolated stores). Initial
   design is per-identity. Revisit based on empirical claude-mem
   behavior during Phase 1.

5. **Worktree convention interaction.** The current worktree
   convention (`.worktrees/` under project root) works inside the
   VM unchanged. But with the VM providing identity isolation, the
   worktree convention's role shifts from "isolate parallel agents"
   to "isolate parallel work on the same project." The convention
   may simplify.

## References

- [#882 — Docker-based agent isolation exploration](https://github.com/vergil-project/vergil-tooling/issues/882)
- [Corral — Lima VM isolation for Claude Code](https://gitlab.com/dmorel69/corral)
- [#775 — Credential management design](https://github.com/vergil-project/vergil-tooling/issues/775)
- [#805 — Vergil/Mimir identity design](https://github.com/vergil-project/vergil-tooling/issues/805)
- [Worktree convention spec](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md)
- [Lima — Linux virtual machines on macOS](https://lima-vm.io/)
