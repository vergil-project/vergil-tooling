# vergil-vm: Credential Provisioning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision agent identity credentials (GitHub App
credentials) into the identity VM so the agent operates under
its own App identity — never the human's.

**Architecture:** A `vrg-vm-init` script runs on the host and
injects GitHub App credentials (App ID, private key) into the VM
via `limactl shell`. Inside the VM, all token acquisition happens
dynamically at runtime through `vrg-git` / `vrg-gh` — the
wrappers call `get_installation_token()` in `github.py`, which
generates a JWT, discovers installation IDs via
`GET /app/installations`, and exchanges for per-org installation
tokens cached with 55-minute TTL. No standalone token tool
(`vrg-app-token`) is needed.

**Tech Stack:** Bash (provisioning scripts), Lima CLI, yq (TOML
parsing)

**Specs:**
- `docs/specs/2026-05-20-identity-vm-isolation-design.md` (#892)
  — Credential Provisioning section
- `docs/specs/2026-05-20-single-account-identity-design.md` (#933)
  — GitHub App identity model

**Decomposition:** This is Plan 3 of 6 for the identity VM
isolation system.

| Plan | Scope | Status |
|---|---|---|
| 1. Repository + Working VM | vergil-vm repo, Lima template | Complete |
| 2. Session Management | vrg-session, identities.toml | Complete |
| **3. Credential Provisioning** (this plan) | GitHub App credentials | **Complete** |
| ~~4. Egress Filtering~~ | ~~HAProxy, pf, iptables~~ | Deferred to v2.2 (#901) |
| 5. vergil-tooling Adaptations | nerdctl runtime detection | Planned |
| 6. Distribution + Updates | Pre-built images, vrg-vm-update | Planned |

**Repository:** vergil-vm

**Depends on:** Plan 1 (working VM), Plan 2 (identities.toml
defines which credentials to provision)

**Prerequisite implemented in vergil-tooling:**
vergil-tooling PR #1008 (issue #1006) added dynamic per-org
installation token resolution to `github.py`. This is the runtime
mechanism that makes static token bootstrapping unnecessary.

---

## Architecture Decision: Dynamic-Only Token Acquisition

All GitHub API access in the VM goes through `vrg-git` or
`vrg-gh`, which call `get_installation_token()` in
`vergil_tooling.lib.github`. That function:

1. Detects the org from the current repo's git remote URL
2. Generates a JWT from the App ID + private key
3. Discovers installation IDs via `GET /app/installations`
4. Exchanges for a per-org installation token
5. Caches the token for 55 minutes

Because every authenticated operation routes through the
wrappers, there is no need for:

- A standalone `vrg-app-token` CLI tool
- Init-time GHCR login (nerdctl pulls go through wrappers)
- Init-time git identity configuration (commits go through
  `vrg-commit`)
- A git credential helper for raw `git` commands

Raw `git` / `gh` commands that require auth will fail in the VM
because no ambient token exists. This is self-correcting — the
agent learns to use the wrappers. Read-only commands
(`git status`, `git log`) work without tokens.

---

## Design

### Credential Inventory

| Credential | Source | Storage inside VM | Purpose |
|---|---|---|---|
| App ID | `identities.toml` | `~/.config/vergil/app.env` | Identifies the GitHub App for JWT generation |
| Private key (.pem) | Host filesystem (path in `identities.toml`) | `~/.config/vergil/app.pem` | Signs JWTs for installation token exchange |

Installation IDs are **not** stored in the VM or in
`identities.toml`. The wrapper scripts (`vrg-git`, `vrg-gh`)
resolve installation IDs dynamically at runtime via
`GET /app/installations` using the App JWT, then cache the
org → installation ID mapping for the session. This enables
multi-org access from a single VM without per-org configuration.

### Injection Model

Credentials are injected from the host into the VM via
`limactl shell` — the host reads the credential and pipes it
into a script running inside the VM. No credentials are stored
in the VM template, provisioning scripts, or version-controlled
files.

```bash
# Host-side: inject App credentials
limactl shell vergil-agent -- bash -c \
  'mkdir -p ~/.config/vergil && cat > ~/.config/vergil/app.pem && chmod 600 ~/.config/vergil/app.pem' \
  < ~/.config/vergil/keys/vergil-agent.pem

printf 'APP_ID=3809631\n' | limactl shell vergil-agent -- bash -c \
  'cat > ~/.config/vergil/app.env && chmod 600 ~/.config/vergil/app.env'
```

### The vrg-vm-init Script

A single script that orchestrates all credential injection for
an identity VM. Run once after VM creation, or re-run to update
credentials.

```bash
vrg-vm-init vergil vergil-agent
# Reads identities.toml for the 'vergil' identity
# Injects App credentials into the VM via limactl shell
# Configures git HTTPS URL rewriting
# Installs vergil-tooling (provides vrg-git, vrg-gh)
# Runs credential verification
```

### Credential Sources

The init script reads credentials from `identities.toml`:

```toml
[identities.vergil]
vm_instance = "vergil-agent"
auth_type = "app"
app_id = 3809631
private_key_path = "~/.config/vergil/keys/wphillipmoore-vergil-agent.2026-05-22.private-key.pem"
```

For automation, environment variable overrides are supported:
- `VRG_APP_ID` — GitHub App ID
- `VRG_PRIVATE_KEY_PATH` — Path to private key file

### PAT Fallback Mode

When `auth_type = "pat"` in `identities.toml`, the init script
falls back to PAT injection:

```bash
# Host-side: inject PAT
echo "$VRG_GITHUB_PAT" | limactl shell vergil-agent -- \
    gh auth login --hostname github.com --with-token
```

This mode does not provide server-side merge control (see #933).

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/vrg-vm-init.sh` | Create | Host-side credential injection orchestrator |
| `scripts/vm-verify-credentials.sh` | Create | VM-side credential verification |
| `tests/test_credentials.sh` | Create | Verify credentials are properly configured |

---

### Task 1: Git HTTPS Credential Configuration

Configure git inside the VM to authenticate to GitHub via
installation tokens over HTTPS. No SSH configuration is needed.

- [x] **Step 1: Configure git to use HTTPS for GitHub**

The `vrg-vm-init` script sets git to use HTTPS URLs for GitHub:

```bash
limactl shell "$INSTANCE" -- git config --global \
    url."https://github.com/".insteadOf "git@github.com:"
```

No credential helper is needed — all authenticated git operations
go through `vrg-git`, which acquires tokens dynamically.

- [x] **Step 2: Commit**

Committed as part of the combined `vrg-vm-init` implementation.

---

### Task 2: Credential Verification Script

A script that runs inside the VM and verifies the credential
file layout is correct.

**Files:**
- Create: `scripts/vm-verify-credentials.sh`
- Create: `tests/test_credentials.sh`

- [x] **Step 1: Write the credential verification script**

Checks: app.pem exists with 600 permissions, app.env exists with
600 permissions, git HTTPS rewrite is configured.

- [x] **Step 2: Write the test**

Same checks as verification script, wired into the
`run-tests.sh` harness.

- [x] **Step 3: Commit**

Committed as part of the combined implementation
(vergil-vm PR #24).

---

### Task 3: Host-Side Init Script

The `vrg-vm-init` script runs on the host and injects all
credentials into the VM.

**Files:**
- Create: `scripts/vrg-vm-init.sh`

- [x] **Step 1: Write vrg-vm-init.sh**

Implemented with `identities.toml` parsing via `yq`, env var
overrides, credential injection, git HTTPS configuration,
vergil-tooling installation, and verification.

- [x] **Step 2: Make it executable and commit**

Committed and merged as vergil-vm PR #24.

---

### Task 4: Manual Validation

- [ ] **Step 1: Run vrg-vm-init against a test VM**

```bash
cd ~/dev/projects/vergil-project/vergil-vm

# Start the VM from Plan 1
limactl start vergil-agent

# Run credential init
./scripts/vrg-vm-init.sh vergil vergil-agent
```

- [ ] **Step 2: Verify inside the VM**

```bash
limactl shell vergil-agent

# Inside the VM — all authenticated commands use wrappers:
vrg-git clone https://github.com/vergil-project/vergil-tooling.git
vrg-gh api user --jq '.login'
```

- [ ] **Step 3: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** App ID, private key, HTTPS
  configuration — all credential types from the spec (#933) are
  covered. GHCR auth and git identity are handled dynamically
  at runtime by the wrappers, not bootstrapped during init.
- [x] **No SSH:** SSH key injection, SSH config, known_hosts
  setup, and SSH verification are all removed. Git uses HTTPS
  with App installation tokens exclusively.
- [x] **No standalone token tool:** `vrg-app-token` is not
  needed. All token acquisition is dynamic via `vrg-git` /
  `vrg-gh` calling `get_installation_token()`.
- [x] **identities.toml parsing:** vrg-vm-init reads
  `identities.toml` directly via `yq`, with env var overrides
  for automation.
- [x] **Type consistency:** Script names, variable names, and
  VM instance references are consistent across all tasks.
- [x] **Scope boundaries:** This plan does NOT include API key
  forwarding (Plan 2), egress filtering (Plan 4), or wrapper
  simplification (Plan 5). ANTHROPIC_API_KEY is handled
  per-session in Plan 2, not persisted in the VM.
