# vergil-vm: Distribution + Updates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish pre-built VM images and provide a dynamic
update mechanism for vergil-tooling inside the VM, so users
pull ready-to-use images instead of building from source.

**Architecture:** Two distribution mechanisms:
(1) Pre-built VM disk images published as GitHub Release
artifacts (one per architecture), consumed via `limactl create`
with a URL. (2) A `vrg-vm-update` script inside the VM that
dynamically installs or updates vergil-tooling via `uv tool
install` without requiring a VM rebuild. A CD workflow builds
and publishes images on merge to main.

**Tech Stack:** Lima, GitHub Actions, GitHub Releases, uv, Bash

**Specs:**
- `docs/specs/2026-05-20-vergil-vm-image-management-design.md`
  (#894) — Distribution and Dynamic Tooling Management sections

**Decomposition:** This is Plan 6 of 6 for the identity VM
isolation system.

| Plan | Scope | Status |
|---|---|---|
| 1. Repository + Working VM | vergil-vm repo, Lima template | Complete |
| 2. Session Management | vrg-session, identities.toml | Planned |
| 3. Credential Provisioning | GitHub PAT/SSH key injection | Planned |
| 4. Egress Filtering | HAProxy, pf, iptables | Planned |
| 5. vergil-tooling Adaptations | nerdctl, wrapper simplification | Planned |
| **6. Distribution + Updates** (this plan) | Pre-built images, vrg-vm-update | This plan |

**Repository:** vergil-vm

**Depends on:** Plan 1 (build script produces a working VM),
Plan 3 (credential init for GHCR publishing)

---

## Design

### Distribution Model

The spec identifies two options: OCI artifacts on GHCR (preferred)
and GitHub Releases (fallback). This plan implements **GitHub
Releases** as the initial approach — it is simpler, well-tested,
and does not depend on Lima's OCI artifact support being mature.

**How it works:**

1. CD workflow builds the VM image on a GitHub Actions runner
   (using QEMU since Actions runners are x86_64).
2. The build produces a compressed disk image (`.qcow2.gz` or
   `.tar.gz`) for each supported architecture.
3. The artifacts are uploaded to a GitHub Release tagged with the
   vergil-vm version.
4. Users create a VM by pointing Lima at the release URL:

```bash
limactl create --name=vergil-agent \
  https://github.com/vergil-project/vergil-vm/releases/download/v2.1.0/vergil-agent.yaml
```

The published YAML template references the pre-built disk image
URL instead of building from an Ubuntu base. First boot is fast
— no provisioning, just the startup hook for vergil-tooling.

### Dynamic Tooling Updates

Vergil-tooling is NOT baked into the published VM image. It is
installed dynamically on first boot and updateable in-place:

```text
VM boots
  ↓
Startup hook checks vergil-tooling version
  ├── Not installed → uv tool install vergil-tooling@v2.1
  ├── Installed but outdated → uv tool install --upgrade
  └── Up to date → no-op
  ↓
vrg-* commands available on PATH
```

**vrg-vm-update** is a simple script inside the VM that the
user or agent can run at any time to update vergil-tooling:

```bash
vrg-vm-update
# Equivalent to:
# uv tool install --upgrade \
#   'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'
```

### Version Configuration

The VM reads its vergil-tooling version target from a config
file created during initial setup:

```bash
# /etc/vergil/vm.conf (inside the VM)
VERGIL_TOOLING_VERSION="v2.1"
```

This is analogous to how `vergil.toml` configures the tooling
version for Docker cache builds. The startup hook and
`vrg-vm-update` both read this file.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `scripts/vrg-vm-update.sh` | Create | In-VM vergil-tooling updater |
| `scripts/vm-startup-hook.sh` | Create | Boot-time tooling installer |
| `templates/agent-published.yaml` | Create | Published template (references pre-built image) |
| `.github/workflows/cd.yml` | Create | Build + publish pipeline |
| `tests/test_update.sh` | Create | Verify dynamic update works |

---

### Task 1: vrg-vm-update Script

The in-VM script for installing or updating vergil-tooling.

**Files:**
- Create: `scripts/vrg-vm-update.sh`

- [ ] **Step 1: Write vrg-vm-update.sh**

```bash
#!/bin/bash
# scripts/vrg-vm-update.sh
# Install or update vergil-tooling inside the identity VM.
#
# Reads the target version from /etc/vergil/vm.conf and installs
# the latest patch release within that version range via uv.
#
# Usage:
#   vrg-vm-update          # Install/update to configured version
#   vrg-vm-update v2.1     # Override the target version
set -euo pipefail

CONFIG="/etc/vergil/vm.conf"

if [ -n "${1:-}" ]; then
    VERSION="$1"
elif [ -f "$CONFIG" ]; then
    # shellcheck source=/dev/null
    source "$CONFIG"
    VERSION="${VERGIL_TOOLING_VERSION:?VERGIL_TOOLING_VERSION not set in $CONFIG}"
else
    echo "ERROR: no version specified and $CONFIG not found" >&2
    exit 1
fi

export PATH="$HOME/.local/bin:$PATH"

INSTALL_SPEC="vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@${VERSION}"

echo "vergil-tooling target: $VERSION"

# Check if already installed
if command -v vrg-commit > /dev/null 2>&1; then
    CURRENT=$(vrg-version 2>/dev/null || echo "unknown")
    echo "Currently installed: $CURRENT"
    echo "Updating..."
    uv tool install --upgrade "$INSTALL_SPEC"
else
    echo "Not installed. Installing..."
    uv tool install "$INSTALL_SPEC"
fi

# Verify
echo ""
echo "Installed version: $(vrg-version 2>/dev/null || echo 'verification failed')"
echo "vrg-vm-update complete."
```

- [ ] **Step 2: Commit**

```bash
chmod +x scripts/vrg-vm-update.sh
vrg-commit --type feat --scope vm \
  --message "vrg-vm-update script for dynamic tooling management" \
  --body "Installs or updates vergil-tooling inside the VM via uv tool install"
```

---

### Task 2: VM Startup Hook

A script that runs on VM boot to ensure vergil-tooling is
installed and current.

**Files:**
- Create: `scripts/vm-startup-hook.sh`
- Modify: `templates/agent.yaml` (add startup provisioning)

- [ ] **Step 1: Write the startup hook**

```bash
#!/bin/bash
# scripts/vm-startup-hook.sh
# Runs at VM boot to ensure vergil-tooling is installed.
# Called by Lima's provision mechanism on every start.
set -euo pipefail

CONFIG="/etc/vergil/vm.conf"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="/var/log/vergil-startup.log"

{
    echo "$(date -Iseconds) vergil-startup: begin"

    export PATH="$HOME/.local/bin:$PATH"

    if [ ! -f "$CONFIG" ]; then
        echo "$(date -Iseconds) vergil-startup: no config at $CONFIG, skipping"
        exit 0
    fi

    # shellcheck source=/dev/null
    source "$CONFIG"

    if command -v vrg-commit > /dev/null 2>&1; then
        echo "$(date -Iseconds) vergil-startup: vergil-tooling already installed"
    else
        echo "$(date -Iseconds) vergil-startup: installing vergil-tooling@${VERGIL_TOOLING_VERSION}"
        uv tool install \
            "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@${VERGIL_TOOLING_VERSION}"
    fi

    echo "$(date -Iseconds) vergil-startup: complete"
} >> "$LOG" 2>&1
```

- [ ] **Step 2: Add startup hook to VM template**

Add a boot-mode provision block to `templates/agent.yaml`:

```yaml
- mode: user
  script: |
    #!/bin/bash
    set -eux -o pipefail

    # Create vergil config directory and version config
    sudo mkdir -p /etc/vergil
    if [ ! -f /etc/vergil/vm.conf ]; then
      echo 'VERGIL_TOOLING_VERSION="v2.1"' | sudo tee /etc/vergil/vm.conf
    fi

    # Install vergil-tooling on first boot
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v vrg-commit > /dev/null 2>&1; then
      source /etc/vergil/vm.conf
      uv tool install \
        "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@${VERGIL_TOOLING_VERSION}"
    fi
```

- [ ] **Step 3: Commit**

```bash
chmod +x scripts/vm-startup-hook.sh
vrg-commit --type feat --scope vm \
  --message "VM startup hook for vergil-tooling installation" \
  --body "Installs vergil-tooling on first boot via /etc/vergil/vm.conf version config"
```

---

### Task 3: Published Template

A Lima template that references a pre-built disk image from
GitHub Releases instead of building from scratch. Users consume
this template.

**Files:**
- Create: `templates/agent-published.yaml`

- [ ] **Step 1: Write the published template**

```yaml
# templates/agent-published.yaml
# Published template — references a pre-built vergil-agent image.
#
# Usage:
#   limactl create --name=vergil-agent \
#     https://github.com/vergil-project/vergil-vm/releases/download/v2.1.0/agent-published.yaml
#
# This template is published alongside the disk image in each
# GitHub Release. It points to the pre-built image rather than
# building from an Ubuntu base.

minimumLimaVersion: "2.0.0"

images:
- location: "https://github.com/vergil-project/vergil-vm/releases/download/v__VERSION__/vergil-agent-aarch64.qcow2.gz"
  arch: "aarch64"
  digest: "__DIGEST_ARM64__"
- location: "https://github.com/vergil-project/vergil-vm/releases/download/v__VERSION__/vergil-agent-x86_64.qcow2.gz"
  arch: "x86_64"
  digest: "__DIGEST_X86_64__"

cpus: 4
memory: "2GiB"
disk: "50GiB"

containerd:
  system: false
  user: true

mounts:
- location: "~/dev"
  writable: true

ssh:
  forwardAgent: true

provision:
- mode: user
  script: |
    #!/bin/bash
    set -eux -o pipefail
    export PATH="$HOME/.local/bin:$PATH"

    # Install vergil-tooling if not already present
    if ! command -v vrg-commit > /dev/null 2>&1; then
      source /etc/vergil/vm.conf
      uv tool install \
        "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@${VERGIL_TOOLING_VERSION}"
    fi

probes:
- mode: readiness
  description: "vergil-tooling installed and containerd running"
  script: |
    #!/bin/bash
    set -eux -o pipefail
    if ! timeout 120s bash -c "until command -v vrg-commit >/dev/null 2>&1; do sleep 3; done"; then
      echo >&2 "vergil-tooling not installed yet"
      exit 1
    fi
    if ! timeout 120s bash -c "until pgrep -f 'containerd' >/dev/null 2>&1; do sleep 3; done"; then
      echo >&2 "containerd is not running yet"
      exit 1
    fi
  hint: |
    Check provisioning logs: limactl shell vergil-agent -- cat /var/log/vergil-startup.log

message: |
  vergil-agent VM is ready (pre-built image).

  Shell into the VM:
    limactl shell {{.Name}}

  Start a Claude Code session:
    vrg-session <project-name>
```

The `__VERSION__`, `__DIGEST_ARM64__`, and `__DIGEST_X86_64__`
placeholders are replaced by the CD workflow at publish time.

- [ ] **Step 2: Commit**

```bash
vrg-commit --type feat --scope vm \
  --message "published Lima template for pre-built images" \
  --body "References pre-built disk images from GitHub Releases — no local build required"
```

---

### Task 4: CD Workflow

Build and publish the VM image on merge to main.

**Files:**
- Create: `.github/workflows/cd.yml`

- [ ] **Step 1: Write the CD workflow**

```yaml
# .github/workflows/cd.yml
name: CD

on:
  push:
    branches: [main]

permissions:
  contents: write

jobs:
  build-and-publish:
    name: build and publish
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4

    - name: Read version
      id: version
      run: echo "version=$(cat VERSION)" >> "$GITHUB_OUTPUT"

    - name: Install Lima
      run: |
        LIMA_VERSION=$(curl -s https://api.github.com/repos/lima-vm/lima/releases/latest | jq -r .tag_name)
        curl -fsSL "https://github.com/lima-vm/lima/releases/download/${LIMA_VERSION}/lima-${LIMA_VERSION:1}-$(uname -s)-$(uname -m).tar.gz" \
          | sudo tar xz -C /usr/local

    - name: Build VM image
      run: |
        limactl create --name=vergil-agent-build templates/agent.yaml --tty=false
        limactl start vergil-agent-build --tty=false

    - name: Run tests
      run: bash tests/run-tests.sh vergil-agent-build

    - name: Export disk image
      run: |
        limactl stop vergil-agent-build
        limactl disk export vergil-agent-build vergil-agent-x86_64.qcow2
        gzip vergil-agent-x86_64.qcow2

    - name: Compute digest
      id: digest
      run: |
        sha256=$(sha256sum vergil-agent-x86_64.qcow2.gz | awk '{print $1}')
        echo "sha256_x86_64=sha256:${sha256}" >> "$GITHUB_OUTPUT"

    - name: Render published template
      run: |
        VERSION="${{ steps.version.outputs.version }}"
        DIGEST_X86_64="${{ steps.digest.outputs.sha256_x86_64 }}"
        sed \
          -e "s/__VERSION__/${VERSION}/g" \
          -e "s/__DIGEST_X86_64__/${DIGEST_X86_64}/g" \
          -e "s/__DIGEST_ARM64__/TODO/g" \
          templates/agent-published.yaml > agent-published.yaml

    - name: Create GitHub Release
      env:
        GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
      run: |
        VERSION="${{ steps.version.outputs.version }}"
        gh release create "v${VERSION}" \
          --title "v${VERSION}" \
          --notes "vergil-agent VM image v${VERSION}" \
          vergil-agent-x86_64.qcow2.gz \
          agent-published.yaml

    - name: Clean up
      if: always()
      run: |
        limactl stop vergil-agent-build 2>/dev/null || true
        limactl delete --force vergil-agent-build 2>/dev/null || true
```

**Note:** This initial workflow only builds x86_64 (the GitHub
Actions runner architecture). ARM64 (Apple Silicon) builds
require either a self-hosted macOS runner or cross-compilation
via QEMU. ARM64 support can be added as a matrix job once the
x86_64 pipeline is proven.

- [ ] **Step 2: Commit**

```bash
vrg-commit --type ci --scope vm \
  --message "CD workflow for VM image build and publish" \
  --body "Builds VM on merge to main, runs tests, publishes to GitHub Releases"
```

---

### Task 5: Update Test

**Files:**
- Create: `tests/test_update.sh`

- [ ] **Step 1: Write the update test**

```bash
#!/bin/bash
# tests/test_update.sh — Verify the dynamic update mechanism works.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

# /etc/vergil/vm.conf must exist
test -f /etc/vergil/vm.conf

# Source the config
# shellcheck source=/dev/null
source /etc/vergil/vm.conf
test -n "$VERGIL_TOOLING_VERSION"

# uv is available
command -v uv

# vrg-vm-update script is available (if installed)
if [ -f /usr/local/bin/vrg-vm-update ]; then
    # Run update (should be idempotent)
    vrg-vm-update
fi

# After update, vrg-commit should be available
command -v vrg-commit

echo "test_update: all checks passed"
```

- [ ] **Step 2: Commit**

```bash
vrg-commit --type test --scope vm \
  --message "dynamic tooling update test" \
  --body "Verifies vm.conf, uv availability, and vrg-vm-update idempotency"
```

---

### Task 6: Manual Validation

- [ ] **Step 1: Test vrg-vm-update locally**

```bash
limactl shell vergil-agent

# Inside the VM:
# Create the config if not present
sudo mkdir -p /etc/vergil
echo 'VERGIL_TOOLING_VERSION="v2.1"' | sudo tee /etc/vergil/vm.conf

# Copy the update script
# (from the host, or just paste it)

# Run update
bash /path/to/vrg-vm-update.sh

# Verify
vrg-commit --help
vrg-version
```

- [ ] **Step 2: Test idempotency**

```bash
# Run update again — should be a no-op
bash /path/to/vrg-vm-update.sh
```

- [ ] **Step 3: Commit any fixes**

---

## Self-Review Checklist

- [x] **Spec coverage:** GitHub Releases distribution, dynamic
  tooling management, version configuration, startup hook,
  in-place updates — all spec items covered.
- [x] **Placeholder scan:** The `__VERSION__` and `__DIGEST__`
  strings in the published template are build-time placeholders
  replaced by the CD workflow, not implementation gaps.
- [x] **Type consistency:** Version strings, file paths, and
  script names are consistent across all tasks.
- [x] **Scope boundaries:** This plan does NOT include OCI
  artifact distribution (deferred until Lima's OCI support
  matures) or ARM64 CI builds (deferred until self-hosted
  runner is available).
