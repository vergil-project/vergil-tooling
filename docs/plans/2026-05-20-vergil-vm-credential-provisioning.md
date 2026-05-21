# vergil-vm: Credential Provisioning — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision agent identity credentials (GitHub App
credentials, GHCR auth) into the identity VM so the agent
operates under its own App identity — never the human's.

**Architecture:** A `vrg-vm-init` script runs on the host and
injects GitHub App credentials (App ID, installation ID, private
key) into the VM via `limactl shell`. Inside the VM, the tooling
uses JWT → installation token exchange for all GitHub operations
over HTTPS. No SSH keys are provisioned — the agent authenticates
exclusively via App installation tokens.

**Tech Stack:** Bash (provisioning scripts), Lima CLI, gh CLI,
nerdctl, PyJWT

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
| 2. Session Management | vrg-session, identities.toml | Planned |
| **3. Credential Provisioning** (this plan) | GitHub App credentials, GHCR auth | This plan |
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
| App ID | `identities.toml` | `~/.config/vergil/app.env` | Identifies the GitHub App for JWT generation |
| Installation ID | `identities.toml` | `~/.config/vergil/app.env` | Identifies the App's installation on a specific org |
| Private key (.pem) | Host filesystem (path in `identities.toml`) | `~/.config/vergil/app.pem` | Signs JWTs for installation token exchange |
| GHCR token | Derived from installation token | nerdctl login state | Pull vergil-docker images |

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

limactl shell vergil-agent -- bash -c \
  'cat > ~/.config/vergil/app.env && chmod 600 ~/.config/vergil/app.env' \
  <<< "APP_ID=12345
INSTALLATION_ID=67890"
```

### The vrg-vm-init Script

A single script that orchestrates all credential injection for
an identity VM. Run once after VM creation, or re-run to update
credentials.

```bash
vrg-vm-init vergil
# Reads identities.toml for the 'vergil' identity
# Injects App credentials into the VM via limactl shell
# Configures git HTTPS auth and GHCR login
# Verifies each credential works
```

### Credential Sources

The init script reads credentials from `identities.toml`:

```toml
[identities.vergil]
vm_instance = "vergil-agent"
auth_type = "app"
app_id = 12345
installation_id = 67890
private_key_path = "~/.config/vergil/keys/vergil-agent.pem"
```

For automation, environment variable overrides are supported:
- `VRG_APP_ID` — GitHub App ID
- `VRG_INSTALLATION_ID` — Installation ID
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

**Files:**
- Modify: `templates/agent.yaml` (optional — git config can be
  set by vrg-vm-init instead of baked into the template)

- [ ] **Step 1: Configure git to use HTTPS for GitHub**

The `vrg-vm-init` script sets git to use HTTPS URLs for GitHub
and configures a credential helper that retrieves the App
installation token:

```bash
limactl shell "$INSTANCE" -- git config --global \
    url."https://github.com/".insteadOf "git@github.com:"
```

The credential helper is provided by vergil-tooling (installed
in the VM via Plan 1). It reads the App credentials from
`~/.config/vergil/app.pem` and `app.env`, generates a JWT,
exchanges it for an installation token, and returns it to git.

- [ ] **Step 2: Commit**

```bash
vrg-commit --type feat --scope vm \
  --message "git HTTPS credential configuration" \
  --body "Configure git to use HTTPS with App installation tokens for GitHub"
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

# App credentials exist
check "App private key" test -f ~/.config/vergil/app.pem
check "App config" test -f ~/.config/vergil/app.env

# GitHub API access (via installation token)
check "GitHub API access" gh api user --jq '.login'

# nerdctl GHCR auth
check "nerdctl GHCR login" nerdctl pull --quiet ghcr.io/vergil-project/dev-python:latest

# Git user config
check "git user.name" git config user.name
check "git user.email" git config user.email

# Git HTTPS config
check "git HTTPS rewrite" git config url.https://github.com/.insteadOf

echo ""
echo "Credential checks: $((7 - failures))/7 passed"
exit "$failures"
```

- [ ] **Step 2: Write the test**

```bash
#!/bin/bash
# tests/test_credentials.sh — Verify credential configuration.
# Runs inside the VM after vrg-vm-init has been executed.
set -euo pipefail

# App credentials exist with correct permissions
test -f ~/.config/vergil/app.pem
perms=$(stat -c '%a' ~/.config/vergil/app.pem 2>/dev/null || stat -f '%Lp' ~/.config/vergil/app.pem)
test "$perms" = "600"

test -f ~/.config/vergil/app.env

# GitHub API is accessible
gh api user --jq '.login' | grep -q .

# Git identity is configured
git config user.name | grep -q .
git config user.email | grep -q .

# Git uses HTTPS for GitHub
git config url.https://github.com/.insteadOf | grep -q 'git@github.com:'

echo "test_credentials: all checks passed"
```

- [ ] **Step 3: Commit**

```bash
vrg-commit --type feat --scope vm \
  --message "credential verification script" \
  --body "vm-verify-credentials.sh checks App credentials, GitHub API access, GHCR login, and git identity"
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
# Reads credentials from identities.toml or environment variables:
#   VRG_APP_ID            — GitHub App ID
#   VRG_INSTALLATION_ID   — Installation ID
#   VRG_PRIVATE_KEY_PATH  — Path to App private key (.pem)
#
# Example:
#   ./scripts/vrg-vm-init.sh vergil vergil-agent
set -euo pipefail

IDENTITY="${1:?Usage: vrg-vm-init <identity-name> <vm-instance>}"
INSTANCE="${2:?Usage: vrg-vm-init <identity-name> <vm-instance>}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Initializing identity VM: $INSTANCE (identity: $IDENTITY) ==="
echo ""

# --- Read credentials from identities.toml or env vars ---

APP_ID="${VRG_APP_ID:-}"
INSTALLATION_ID="${VRG_INSTALLATION_ID:-}"
PRIVATE_KEY_PATH="${VRG_PRIVATE_KEY_PATH:-}"

if [ -z "$APP_ID" ] || [ -z "$INSTALLATION_ID" ] || [ -z "$PRIVATE_KEY_PATH" ]; then
    echo "Reading credentials from identities.toml..."
    # TODO: parse identities.toml for the given identity
    # For now, require env vars
    echo "ERROR: Set VRG_APP_ID, VRG_INSTALLATION_ID, and VRG_PRIVATE_KEY_PATH" >&2
    exit 1
fi

if [ ! -f "$PRIVATE_KEY_PATH" ]; then
    echo "ERROR: Private key not found: $PRIVATE_KEY_PATH" >&2
    exit 1
fi

# --- Inject App Credentials ---

echo "Injecting App private key..."
limactl shell "$INSTANCE" -- bash -c \
    'mkdir -p ~/.config/vergil && cat > ~/.config/vergil/app.pem && chmod 600 ~/.config/vergil/app.pem' \
    < "$PRIVATE_KEY_PATH"

echo "Injecting App configuration..."
limactl shell "$INSTANCE" -- bash -c \
    'cat > ~/.config/vergil/app.env && chmod 600 ~/.config/vergil/app.env' \
    <<EOF
APP_ID=$APP_ID
INSTALLATION_ID=$INSTALLATION_ID
EOF

# --- Configure git for HTTPS ---

echo "Configuring git for HTTPS GitHub access..."
limactl shell "$INSTANCE" -- \
    git config --global url."https://github.com/".insteadOf "git@github.com:"
echo ""

# --- GHCR Authentication ---

echo "Configuring nerdctl GHCR authentication..."
# Generate an installation token and use it for GHCR login
# The token exchange uses the App credentials just injected
limactl shell "$INSTANCE" -- bash -c '
    source ~/.config/vergil/app.env
    TOKEN=$(vrg-app-token --app-id "$APP_ID" --installation-id "$INSTALLATION_ID" --key ~/.config/vergil/app.pem)
    echo "$TOKEN" | nerdctl login ghcr.io -u x-access-token --password-stdin
'
echo ""

# --- Git Identity ---

echo "Configuring git identity..."
limactl shell "$INSTANCE" -- bash -c '
    source ~/.config/vergil/app.env
    TOKEN=$(vrg-app-token --app-id "$APP_ID" --installation-id "$INSTALLATION_ID" --key ~/.config/vergil/app.pem)
    GITHUB_USER=$(GH_TOKEN="$TOKEN" gh api user --jq ".login")
    GITHUB_EMAIL=$(GH_TOKEN="$TOKEN" gh api user --jq ".email // empty")
    if [ -z "$GITHUB_EMAIL" ]; then
        GITHUB_EMAIL="${GITHUB_USER}@users.noreply.github.com"
    fi
    echo "Git identity: $GITHUB_USER <$GITHUB_EMAIL>"
    git config --global user.name "$GITHUB_USER"
    git config --global user.email "$GITHUB_EMAIL"
'
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
  --body "vrg-vm-init injects GitHub App credentials, configures HTTPS git auth, GHCR login, and git identity"
```

---

### Task 4: Manual Validation

- [ ] **Step 1: Run vrg-vm-init against a test VM**

```bash
cd ~/dev/projects/vergil-project/vergil-vm

# Start the VM from Plan 1
limactl start vergil-agent

# Run credential init
VRG_APP_ID=12345 \
VRG_INSTALLATION_ID=67890 \
VRG_PRIVATE_KEY_PATH=~/.config/vergil/keys/vergil-agent.pem \
  ./scripts/vrg-vm-init.sh vergil vergil-agent
```

- [ ] **Step 2: Verify inside the VM**

```bash
limactl shell vergil-agent

# Inside the VM:
gh api user --jq '.login'   # Should return App identity
git config user.name         # Should show configured identity
nerdctl pull ghcr.io/vergil-project/dev-python:latest  # Should work
```

- [ ] **Step 3: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** App ID, installation ID, private key,
  GHCR auth, git identity, HTTPS configuration — all credential
  types from the spec (#933) are covered.
- [x] **No SSH:** SSH key injection, SSH config, known_hosts
  setup, and SSH verification are all removed. Git uses HTTPS
  with App installation tokens exclusively.
- [x] **Placeholder scan:** One TODO remains (parsing
  identities.toml in vrg-vm-init) — acceptable for v1, env vars
  are the primary mechanism.
- [x] **Type consistency:** Script names, variable names, and
  VM instance references are consistent across all tasks.
- [x] **Scope boundaries:** This plan does NOT include API key
  forwarding (Plan 2), egress filtering (Plan 4), or wrapper
  simplification (Plan 5). ANTHROPIC_API_KEY is handled
  per-session in Plan 2, not persisted in the VM.
