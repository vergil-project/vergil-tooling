# vergil-vm: Egress Filtering — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement three-layer egress filtering that prevents the
agent from making network connections to unauthorized hosts,
defending against prompt injection data exfiltration.

**Architecture:** Three layers, each serving a distinct purpose:
(1) iptables inside the VM redirects HTTPS to a host-side proxy
(convenience, subvertable by root), (2) HAProxy on the host
inspects TLS SNI against an allowlist (enforcement, VM cannot
modify), (3) pf on the host drops all non-443 traffic from the
VM subnet (perimeter, kernel-level). This is adopted from
corral's proven design.

**Tech Stack:** HAProxy, pf (macOS packet filter), iptables,
Bash

**Specs:**
- `docs/specs/2026-05-20-identity-vm-isolation-design.md` (#892)
  — Egress Filtering section

**Decomposition:** This is Plan 4 of 6 for the identity VM
isolation system.

| Plan | Scope | Status |
|---|---|---|
| 1. Repository + Working VM | vergil-vm repo, Lima template | Complete |
| 2. Session Management | vrg-session, identities.toml | Planned |
| 3. Credential Provisioning | GitHub PAT/SSH key injection | Planned |
| **4. Egress Filtering** (this plan) | HAProxy, pf, iptables | This plan |
| 5. vergil-tooling Adaptations | nerdctl, wrapper simplification | Planned |
| 6. Distribution + Updates | Pre-built images, vrg-vm-update | Planned |

**Repository:** vergil-vm (config, scripts, VM template changes)
+ macOS host configuration

**Depends on:** Plan 1 (working VM with known IP/subnet)

**Deferral candidate:** This plan can be deferred to a later
release if the complexity does not justify the security benefit
for the initial rollout. The identity VM already provides
filesystem and credential isolation without egress filtering.
Egress filtering adds network isolation — important for defense
against prompt injection exfiltration, but not required for a
functional v1.

---

## Design

### Traffic Flow

```text
Agent process (inside VM)
  │
  ├── HTTPS request to api.github.com:443
  │     ↓
  │   iptables DNAT → redirects to host HAProxy (Layer 1)
  │     ↓
  │   HAProxy inspects TLS ClientHello SNI (Layer 2)
  │     ├── SNI matches allowlist → forward to real destination
  │     └── SNI not in allowlist → TCP RST (connection killed)
  │
  ├── HTTP request to evil.com:80
  │     ↓
  │   pf drops the packet (Layer 3 — only 443 allowed)
  │
  └── DNS request to evil.com:53
        ↓
      pf drops the packet (Layer 3 — only 443 allowed)
```

### Allowlist Model

**Baseline allowlist** (ships with vergil-vm):

```text
# config/egress.allow.default
# Hosts required for standard Vergil agent workflows.
# One hostname per line. Comments start with #.

# Anthropic API
api.anthropic.com

# GitHub
github.com
api.github.com
uploads.github.com
objects.githubusercontent.com
raw.githubusercontent.com
ghcr.io
pkg-containers.githubusercontent.com

# Python packaging
pypi.org
files.pythonhosted.org

# Node packaging (if needed by project)
registry.npmjs.org

# Ruby packaging (if needed by project)
rubygems.org

# Container registries
registry-1.docker.io
production.cloudflare.docker.com

# uv installer
astral.sh
```

**Per-project overlays** (stored in each project repo):

```text
# <project>/.vergil/egress.allow
# Additional hosts needed by this project.
staging-api.example.com
```

The HAProxy config is built by merging the baseline with all
per-project overlays for mounted workspaces.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `config/egress.allow.default` | Create | Baseline egress allowlist |
| `scripts/host-egress-setup.sh` | Create | Install/configure HAProxy + pf on macOS |
| `scripts/host-egress-teardown.sh` | Create | Remove HAProxy + pf rules |
| `scripts/build-haproxy-cfg.sh` | Create | Merge baseline + project allowlists into HAProxy config |
| `templates/agent.yaml` | Modify | Add iptables DNAT provisioning |
| `tests/test_egress.sh` | Create | Verify egress filtering works |

---

### Task 1: Baseline Allowlist

**Files:**
- Create: `config/egress.allow.default`

- [ ] **Step 1: Create the allowlist file**

```text
# config/egress.allow.default
# Hosts required for standard Vergil agent workflows.
# One hostname per line. Comments and blank lines are ignored.

# Anthropic API
api.anthropic.com

# GitHub
github.com
api.github.com
uploads.github.com
objects.githubusercontent.com
raw.githubusercontent.com
ghcr.io
pkg-containers.githubusercontent.com

# Python packaging
pypi.org
files.pythonhosted.org

# Node packaging
registry.npmjs.org

# Ruby packaging
rubygems.org

# Container registries
registry-1.docker.io
production.cloudflare.docker.com

# uv installer
astral.sh
```

- [ ] **Step 2: Commit**

```bash
vrg-commit --type feat --scope egress \
  --message "baseline egress allowlist" \
  --body "Default hostnames required for Vergil agent workflows"
```

---

### Task 2: HAProxy Configuration Builder

A script that reads the baseline allowlist and any per-project
overlays, then generates the HAProxy configuration.

**Files:**
- Create: `scripts/build-haproxy-cfg.sh`

- [ ] **Step 1: Write the HAProxy config builder**

```bash
#!/bin/bash
# scripts/build-haproxy-cfg.sh
# Build HAProxy configuration from baseline + project allowlists.
#
# Usage:
#   ./scripts/build-haproxy-cfg.sh [workspace-root...]
#
# Outputs HAProxy config to stdout. Redirect to a file:
#   ./scripts/build-haproxy-cfg.sh ~/dev > /usr/local/etc/haproxy/haproxy.cfg
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASELINE="${SCRIPT_DIR}/../config/egress.allow.default"

# Collect all hostnames from baseline and project overlays
collect_hosts() {
    # Baseline
    if [ -f "$BASELINE" ]; then
        grep -v '^\s*#' "$BASELINE" | grep -v '^\s*$'
    fi

    # Per-project overlays
    for workspace_root in "$@"; do
        find "$workspace_root" -maxdepth 3 \
            -path '*/.vergil/egress.allow' \
            -type f 2>/dev/null | while read -r overlay; do
            grep -v '^\s*#' "$overlay" | grep -v '^\s*$'
        done
    done
}

# Deduplicate and sort
HOSTS=$(collect_hosts "$@" | sort -u)

# Generate HAProxy config
cat << 'HEADER'
global
    log stdout format raw local0
    maxconn 1024

defaults
    mode tcp
    timeout connect 10s
    timeout client 30s
    timeout server 30s

frontend https_in
    bind *:8443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req_ssl_hello_type 1 }

HEADER

# ACL rules for each allowed host
i=0
while IFS= read -r host; do
    [ -z "$host" ] && continue
    echo "    acl allowed_sni_${i} req.ssl_sni -i ${host}"
    i=$((i + 1))
done <<< "$HOSTS"

echo ""

# OR all ACLs together
if [ "$i" -gt 0 ]; then
    acl_list=""
    for j in $(seq 0 $((i - 1))); do
        acl_list="${acl_list} allowed_sni_${j}"
    done
    echo "    tcp-request content reject unless${acl_list}"
fi

cat << 'BACKEND'

    default_backend passthrough

backend passthrough
    server upstream 0.0.0.0:0
    # HAProxy will use the original destination via
    # the TPROXY/SO_ORIGINAL_DST mechanism
BACKEND
```

- [ ] **Step 2: Commit**

```bash
chmod +x scripts/build-haproxy-cfg.sh
vrg-commit --type feat --scope egress \
  --message "HAProxy config builder from allowlists" \
  --body "Merges baseline + per-project .vergil/egress.allow into HAProxy SNI filter config"
```

---

### Task 3: Host Egress Setup Script

Installs and configures HAProxy and pf on the macOS host.

**Files:**
- Create: `scripts/host-egress-setup.sh`
- Create: `scripts/host-egress-teardown.sh`

- [ ] **Step 1: Write the setup script**

```bash
#!/bin/bash
# scripts/host-egress-setup.sh
# Configure egress filtering on the macOS host.
#
# Requires: HAProxy (brew install haproxy), sudo for pf.
#
# Usage:
#   sudo ./scripts/host-egress-setup.sh <vm-subnet> [workspace-root...]
#
# Example:
#   sudo ./scripts/host-egress-setup.sh 192.168.105.0/24 ~/dev
set -euo pipefail

VM_SUBNET="${1:?Usage: host-egress-setup.sh <vm-subnet> [workspace-root...]}"
shift
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HAPROXY_CFG="/usr/local/etc/haproxy/haproxy.cfg"
PF_ANCHOR="com.vergil.egress"

echo "=== Egress filtering setup ==="
echo "VM subnet: $VM_SUBNET"
echo ""

# --- HAProxy ---

echo "Generating HAProxy configuration..."
mkdir -p "$(dirname "$HAPROXY_CFG")"
"$SCRIPT_DIR/build-haproxy-cfg.sh" "$@" > "$HAPROXY_CFG"
echo "HAProxy config written to $HAPROXY_CFG"

# Start or reload HAProxy
if pgrep -x haproxy > /dev/null; then
    echo "Reloading HAProxy..."
    brew services restart haproxy
else
    echo "Starting HAProxy..."
    brew services start haproxy
fi
echo ""

# --- pf (packet filter) ---

echo "Configuring pf rules..."

PF_RULES=$(cat << PFRULES
# Vergil egress filtering: only allow HTTPS from VM subnet
# to the HAProxy proxy port. Drop everything else.
block drop quick on lo0 from $VM_SUBNET to any
pass quick on lo0 from $VM_SUBNET to any port 8443
PFRULES
)

# Write anchor rules
echo "$PF_RULES" | pfctl -a "$PF_ANCHOR" -f -

# Ensure the anchor is loaded in the main ruleset
if ! pfctl -s rules 2>/dev/null | grep -q "$PF_ANCHOR"; then
    echo "anchor \"$PF_ANCHOR\"" | pfctl -f -
fi

pfctl -e 2>/dev/null || true
echo "pf rules loaded."
echo ""

echo "=== Egress filtering active ==="
```

- [ ] **Step 2: Write the teardown script**

```bash
#!/bin/bash
# scripts/host-egress-teardown.sh
# Remove egress filtering from the macOS host.
set -euo pipefail

PF_ANCHOR="com.vergil.egress"

echo "=== Removing egress filtering ==="

# Stop HAProxy
if pgrep -x haproxy > /dev/null; then
    echo "Stopping HAProxy..."
    brew services stop haproxy
fi

# Flush pf anchor
echo "Flushing pf rules..."
pfctl -a "$PF_ANCHOR" -F all 2>/dev/null || true

echo "=== Egress filtering removed ==="
```

- [ ] **Step 3: Commit**

```bash
chmod +x scripts/host-egress-setup.sh scripts/host-egress-teardown.sh
vrg-commit --type feat --scope egress \
  --message "host egress setup and teardown scripts" \
  --body "HAProxy SNI filtering + pf perimeter rules for VM subnet"
```

---

### Task 4: VM-Side iptables DNAT

Add iptables rules to the VM template that redirect outbound
HTTPS traffic to the host-side HAProxy.

**Files:**
- Modify: `templates/agent.yaml`

- [ ] **Step 1: Add iptables provisioning to the VM template**

Append a new system-mode provision block to `templates/agent.yaml`:

```yaml
- mode: system
  script: |
    #!/bin/bash
    set -eux -o pipefail

    # Egress filtering: redirect outbound HTTPS to host HAProxy.
    # The host IP is the default gateway from the VM's perspective.
    HOST_IP=$(ip route show default | awk '{print $3}')
    HAPROXY_PORT=8443

    # Redirect all outbound 443 traffic to host HAProxy
    iptables -t nat -A OUTPUT -p tcp --dport 443 \
      -j DNAT --to-destination "${HOST_IP}:${HAPROXY_PORT}"

    # Persist the rule across reboots
    mkdir -p /etc/iptables
    iptables-save > /etc/iptables/rules.v4
```

- [ ] **Step 2: Commit**

```bash
vrg-commit --type feat --scope egress \
  --message "VM-side iptables DNAT for egress filtering" \
  --body "Redirects outbound HTTPS to host HAProxy for SNI inspection"
```

---

### Task 5: Egress Test

**Files:**
- Create: `tests/test_egress.sh`

- [ ] **Step 1: Write the egress test**

```bash
#!/bin/bash
# tests/test_egress.sh — Verify egress filtering is working.
# Requires egress filtering to be configured on the host.
set -euo pipefail

# Allowed: GitHub API should work
echo "Testing allowed host (api.github.com)..."
if curl -sS --max-time 10 https://api.github.com/zen > /dev/null; then
    echo "  PASS: api.github.com reachable"
else
    echo "  FAIL: api.github.com not reachable"
    exit 1
fi

# Blocked: arbitrary host should fail
echo "Testing blocked host (example.com)..."
if curl -sS --max-time 5 https://example.com > /dev/null 2>&1; then
    echo "  FAIL: example.com should be blocked but is reachable"
    exit 1
else
    echo "  PASS: example.com correctly blocked"
fi

echo "test_egress: all checks passed"
```

- [ ] **Step 2: Commit**

```bash
vrg-commit --type test --scope egress \
  --message "egress filtering test" \
  --body "Verifies allowed hosts work and blocked hosts are rejected"
```

---

### Task 6: Manual Validation

- [ ] **Step 1: Set up egress filtering on the host**

```bash
brew install haproxy

cd ~/dev/projects/vergil-project/vergil-vm
sudo ./scripts/host-egress-setup.sh 192.168.105.0/24 ~/dev
```

- [ ] **Step 2: Test from inside the VM**

```bash
limactl shell vergil-agent

# Inside the VM:
curl https://api.github.com/zen           # Should work
curl https://pypi.org                     # Should work
curl https://example.com                  # Should fail (blocked)
curl https://evil-exfiltration-site.com   # Should fail (blocked)
```

- [ ] **Step 3: Verify teardown works**

```bash
sudo ./scripts/host-egress-teardown.sh

limactl shell vergil-agent
curl https://example.com    # Should work again (filtering removed)
```

- [ ] **Step 4: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** All three egress layers from the spec
  are implemented — iptables DNAT, HAProxy SNI allowlist, pf
  perimeter.
- [x] **Placeholder scan:** No TBD, TODO, or "implement later."
- [x] **Type consistency:** Subnet notation, port numbers, and
  script names are consistent across all tasks.
- [x] **Scope boundaries:** This plan does NOT include
  per-project overlay UI or automated allowlist discovery —
  overlays are simple files in `.vergil/egress.allow`.
- [x] **Deferral note:** This entire plan can be deferred without
  blocking Plans 1-3, 5, or 6. The VM works without egress
  filtering; it just lacks network isolation.
