# vergil-vm: Credential Provisioning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision agent identity credentials (GitHub PAT, SSH
key, GHCR auth) into the identity VM so the agent operates
exclusively under its own identity — never the human's.

**Architecture:** A `vrg-vm-init` script runs inside the VM on
first boot (or on-demand) to configure GitHub CLI auth, install
the agent's SSH key, and authenticate nerdctl to GHCR. Credentials
are injected from the host via `limactl shell` and stored inside
the VM's filesystem. The human's credentials never enter the VM.

**Tech Stack:** Bash (provisioning scripts), Lima CLI, gh CLI,
nerdctl

**Specs:**
- `docs/specs/2026-05-20-identity-vm-isolation-design.md` (#892)
  — Credential Provisioning section

**Decomposition:** This is Plan 3 of 6 for the identity VM
isolation system.

| Plan | Scope | Status |
|---|---|---|
| 1. Repository + Working VM | vergil-vm repo, Lima template | Complete |
| 2. Session Management | vrg-session, identities.toml | Planned |
| **3. Credential Provisioning** (this plan) | GitHub PAT/SSH key injection | This plan |
| ~~4. Egress Filtering~~ | ~~HAProxy, pf, iptables~~ | Deferred to v2.2 (#901) |
| 5. vergil-tooling Adaptations | nerdctl, wrapper simplification | Planned |
| 6. Distribution + Updates | Pre-built images, vrg-vm-update | Planned |

**Repository:** vergil-vm

**Depends on:** Plan 1 (working VM), Plan 2 (identities.toml
defines which credentials to provision)

---

## Design

### Credential Inventory

| Credential | Source | Storage inside VM | Purpose |
|---|---|---|---|
| GitHub PAT | Host keychain or env var | `~/.config/gh/hosts.yml` | gh CLI auth (`repo`, `read:org` scope) |
| SSH key | Host `~/.ssh/` (agent identity key) | `~/.ssh/id_ed25519` | Git SSH operations |
| GHCR token | Derived from GitHub PAT | nerdctl login state | Pull vergil-docker images |

### Injection Model

Credentials are injected from the host into the VM via
`limactl shell` — the host reads the credential and pipes it
into a script running inside the VM. No credentials are stored
in the VM template, provisioning scripts, or version-controlled
files.

```bash
# Host-side: inject GitHub PAT
limactl shell vergil-agent -- bash -s <<'SCRIPT'
  gh auth login --with-token <<< "$1"
SCRIPT

# Host-side: inject SSH key
limactl shell vergil-agent -- bash -c \
  'mkdir -p ~/.ssh && cat > ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519' \
  < ~/.ssh/id_ed25519_vergil
```

### The vrg-vm-init Script

A single script that orchestrates all credential injection for
an identity VM. Run once after VM creation, or re-run to update
credentials.

```bash
vrg-vm-init vergil
# Reads identities.toml for the 'vergil' identity
# Prompts for or reads credentials from the environment
# Injects them into the VM via limactl shell
# Verifies each credential works
```

### Credential Sources

The init script reads credentials from these sources, in order:

1. **Environment variables** (for automation):
   - `VRG_GITHUB_PAT_VERGIL` — GitHub PAT for the vergil identity
   - `VRG_SSH_KEY_VERGIL` — path to SSH private key

2. **Interactive prompt** (for manual setup):
   - If env vars are not set, prompt the user to paste/provide
     each credential

The source mechanism is intentionally simple for v1. A future
version could integrate with macOS Keychain or 1Password CLI.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/vrg-vm-init.sh` | Create | Host-side credential injection orchestrator |
| `scripts/vm-verify-credentials.sh` | Create | VM-side credential verification |
| `tests/test_credentials.sh` | Create | Verify credentials are properly configured |
| `templates/agent.yaml` | Modify | Add SSH config provisioning block |

---

### Task 1: SSH Configuration Provisioning

Add an SSH configuration block to the VM template that sets up
the `~/.ssh` directory structure and known_hosts for GitHub.

**Files:**
- Modify: `templates/agent.yaml`

- [ ] **Step 1: Add SSH provisioning to the VM template**

Append a new user-mode provision block to `templates/agent.yaml`:

```yaml
- mode: user
  script: |
    #!/bin/bash
    set -eux -o pipefail

    mkdir -p ~/.ssh
    chmod 700 ~/.ssh

    # Pre-populate GitHub's SSH host keys
    ssh-keyscan -t ed25519,rsa github.com >> ~/.ssh/known_hosts 2>/dev/null
    chmod 644 ~/.ssh/known_hosts

    # SSH config: use the identity key for GitHub
    cat > ~/.ssh/config << 'SSHCONFIG'
    Host github.com
      IdentityFile ~/.ssh/id_ed25519
      IdentitiesOnly yes
    SSHCONFIG
    chmod 600 ~/.ssh/config
```

- [ ] **Step 2: Commit**

```bash
vrg-commit --type feat --scope vm \
  --message "SSH directory provisioning in VM template" \
  --body "Pre-configure ~/.ssh with GitHub known_hosts and identity key config"
```

---

### Task 2: Credential Verification Script

A script that runs inside the VM and verifies all credentials
are properly configured.

**Files:**
- Create: `scripts/vm-verify-credentials.sh`
- Create: `tests/test_credentials.sh`

- [ ] **Step 1: Write the credential verification script**

```bash
#!/bin/bash
# scripts/vm-verify-credentials.sh
# Run inside the VM to verify all credentials are configured.
set -euo pipefail

failures=0

check() {
    local name="$1"
    shift
    printf "  %-30s " "$name"
    if "$@" > /dev/null 2>&1; then
        echo "OK"
    else
        echo "FAIL"
        failures=$((failures + 1))
    fi
}

echo "Verifying credentials..."

# GitHub CLI authentication
check "gh auth status" gh auth status

# SSH key exists
check "SSH key present" test -f ~/.ssh/id_ed25519

# SSH to GitHub works
check "SSH to GitHub" ssh -T git@github.com -o BatchMode=yes -o ConnectTimeout=5

# nerdctl GHCR auth
check "nerdctl GHCR login" nerdctl pull --quiet ghcr.io/vergil-project/dev-python:latest

# Git user config
check "git user.name" git config user.name
check "git user.email" git config user.email

echo ""
echo "Credential checks: $((6 - failures))/6 passed"
exit "$failures"
```

- [ ] **Step 2: Write the test**

```bash
#!/bin/bash
# tests/test_credentials.sh — Verify credential configuration.
# Runs inside the VM after vrg-vm-init has been executed.
set -euo pipefail

# gh is authenticated
gh auth status

# SSH key exists and has correct permissions
test -f ~/.ssh/id_ed25519
perms=$(stat -c '%a' ~/.ssh/id_ed25519 2>/dev/null || stat -f '%Lp' ~/.ssh/id_ed25519)
test "$perms" = "600"

# known_hosts contains GitHub
grep -q 'github.com' ~/.ssh/known_hosts

# Git identity is configured
git config user.name | grep -q .
git config user.email | grep -q .

echo "test_credentials: all checks passed"
```

- [ ] **Step 3: Commit**

```bash
vrg-commit --type feat --scope vm \
  --message "credential verification script" \
  --body "vm-verify-credentials.sh checks gh auth, SSH key, GHCR login, and git identity"
```

---

### Task 3: Host-Side Init Script

The `vrg-vm-init` script runs on the host and injects all
credentials into the VM.

**Files:**
- Create: `scripts/vrg-vm-init.sh`

- [ ] **Step 1: Write vrg-vm-init.sh**

```bash
#!/bin/bash
# scripts/vrg-vm-init.sh
# Initialize an identity VM with agent credentials.
#
# Usage:
#   ./scripts/vrg-vm-init.sh <identity-name> <vm-instance>
#
# Environment variables (optional — prompts if not set):
#   VRG_GITHUB_PAT  — GitHub Personal Access Token
#   VRG_SSH_KEY     — Path to SSH private key file
#
# Example:
#   VRG_GITHUB_PAT="ghp_..." VRG_SSH_KEY=~/.ssh/id_ed25519_vergil \
#     ./scripts/vrg-vm-init.sh vergil vergil-agent
set -euo pipefail

IDENTITY="${1:?Usage: vrg-vm-init <identity-name> <vm-instance>}"
INSTANCE="${2:?Usage: vrg-vm-init <identity-name> <vm-instance>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Initializing identity VM: $INSTANCE (identity: $IDENTITY) ==="
echo ""

# --- GitHub PAT ---

if [ -n "${VRG_GITHUB_PAT:-}" ]; then
    echo "Using GitHub PAT from VRG_GITHUB_PAT environment variable"
else
    echo "Enter GitHub PAT for the '$IDENTITY' identity:"
    echo "(needs repo, read:org scopes)"
    read -rs VRG_GITHUB_PAT
    echo ""
fi

echo "Configuring gh CLI authentication..."
echo "$VRG_GITHUB_PAT" | limactl shell "$INSTANCE" -- \
    gh auth login --hostname github.com --with-token

# Verify
limactl shell "$INSTANCE" -- gh auth status
echo ""

# --- SSH Key ---

if [ -n "${VRG_SSH_KEY:-}" ]; then
    echo "Using SSH key from VRG_SSH_KEY: $VRG_SSH_KEY"
else
    echo "Enter path to SSH private key for the '$IDENTITY' identity:"
    read -r VRG_SSH_KEY
fi

if [ ! -f "$VRG_SSH_KEY" ]; then
    echo "ERROR: SSH key not found: $VRG_SSH_KEY" >&2
    exit 1
fi

echo "Injecting SSH key..."
limactl shell "$INSTANCE" -- bash -c \
    'cat > ~/.ssh/id_ed25519 && chmod 600 ~/.ssh/id_ed25519' \
    < "$VRG_SSH_KEY"

# Also inject the public key if it exists
if [ -f "${VRG_SSH_KEY}.pub" ]; then
    limactl shell "$INSTANCE" -- bash -c \
        'cat > ~/.ssh/id_ed25519.pub && chmod 644 ~/.ssh/id_ed25519.pub' \
        < "${VRG_SSH_KEY}.pub"
fi

# Verify SSH to GitHub
echo "Verifying SSH to GitHub..."
limactl shell "$INSTANCE" -- \
    ssh -T git@github.com -o BatchMode=yes -o ConnectTimeout=10 \
    2>&1 || true
echo ""

# --- GHCR Authentication ---

echo "Configuring nerdctl GHCR authentication..."
echo "$VRG_GITHUB_PAT" | limactl shell "$INSTANCE" -- \
    nerdctl login ghcr.io -u "$IDENTITY" --password-stdin
echo ""

# --- Git Identity ---

GITHUB_USER=$(limactl shell "$INSTANCE" -- gh api user --jq '.login')
GITHUB_EMAIL=$(limactl shell "$INSTANCE" -- gh api user --jq '.email // empty')

if [ -z "$GITHUB_EMAIL" ]; then
    GITHUB_EMAIL="${GITHUB_USER}@users.noreply.github.com"
fi

echo "Configuring git identity: $GITHUB_USER <$GITHUB_EMAIL>"
limactl shell "$INSTANCE" -- git config --global user.name "$GITHUB_USER"
limactl shell "$INSTANCE" -- git config --global user.email "$GITHUB_EMAIL"
echo ""

# --- Verify All ---

echo "=== Running credential verification ==="
limactl shell "$INSTANCE" -- bash -s < "$SCRIPT_DIR/vm-verify-credentials.sh"

echo ""
echo "=== VM initialization complete ==="
```

- [ ] **Step 2: Make it executable and commit**

```bash
chmod +x scripts/vrg-vm-init.sh scripts/vm-verify-credentials.sh
vrg-commit --type feat --scope vm \
  --message "host-side credential injection script" \
  --body "vrg-vm-init injects GitHub PAT, SSH key, GHCR auth, and git identity into the VM"
```

---

### Task 4: Manual Validation

- [ ] **Step 1: Run vrg-vm-init against a test VM**

```bash
cd ~/dev/projects/vergil-project/vergil-vm

# Start the VM from Plan 1
limactl start vergil-agent

# Run credential init
VRG_SSH_KEY=~/.ssh/id_ed25519_vergil \
  ./scripts/vrg-vm-init.sh vergil vergil-agent
```

- [ ] **Step 2: Verify inside the VM**

```bash
limactl shell vergil-agent

# Inside the VM:
gh auth status              # Should show agent identity
ssh -T git@github.com       # Should greet agent identity
git config user.name        # Should show agent username
nerdctl pull ghcr.io/vergil-project/dev-python:latest  # Should work
```

- [ ] **Step 3: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** GitHub PAT, SSH key, GHCR auth, git
  identity — all credential types from the spec are covered.
- [x] **Placeholder scan:** No TBD, TODO, or "implement later."
- [x] **Type consistency:** Script names, variable names, and
  VM instance references are consistent across all tasks.
- [x] **Scope boundaries:** This plan does NOT include API key
  forwarding (Plan 2), egress filtering (Plan 4), or wrapper
  simplification (Plan 5). ANTHROPIC_API_KEY is handled
  per-session in Plan 2, not persisted in the VM.
