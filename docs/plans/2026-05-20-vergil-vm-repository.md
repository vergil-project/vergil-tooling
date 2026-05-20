# vergil-vm: Repository + Working Lima VM — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the vergil-vm repository with a working Lima VM
template that boots an Ubuntu VM with rootless containerd, core
development tools, and dynamically-installed vergil-tooling.

**Architecture:** A new `vergil-vm` repository following
vergil-docker patterns: templated VM definitions, versioned
releases, CI validation. The Lima template uses Apple's
Virtualization.framework (VZ) on macOS with virtiofs mounts and
rootless containerd + nerdctl. Provisioning is inline in the Lima
YAML via cloud-init scripts. Tests run inside the VM via
`limactl shell`.

**Tech Stack:** Lima 2.x (CNCF), Ubuntu 24.04 LTS, rootless
containerd + nerdctl, uv, zsh, Bash

**Specs:**
- `docs/specs/2026-05-20-identity-vm-isolation-design.md` (#892)
- `docs/specs/2026-05-20-vergil-vm-image-management-design.md`
  (#894)

**Decomposition:** This is Plan 1 of 6 for the identity VM
isolation system. Each plan produces independently testable
software:

| Plan | Scope | Deliverable |
|---|---|---|
| **1. Repository + Working VM** (this plan) | vergil-vm repo, Lima template, provisioning, build, test, CI | `limactl start` produces a working identity VM |
| 2. Session Management | vrg-session command, identities.toml, API key forwarding | `vrg-session <project>` launches Claude Code in VM |
| 3. Credential Provisioning | Bootstrap scripts, GitHub PAT/SSH key injection, GHCR auth | VM boots with agent identity credentials |
| ~~4. Egress Filtering~~ | ~~HAProxy, pf, iptables, allowlists~~ | Deferred to v2.2 (#901) |
| 5. vergil-tooling Adaptations | nerdctl in vrg-docker-run, wrapper simplification | vergil-tooling works natively inside VM |
| 6. Distribution + Updates | Pre-built images on GHCR, vrg-vm-update, CD pipeline | Users pull pre-built VM images |

---

## Prerequisites (Manual — Human)

Lima must be installed on the development machine, and the
vergil-vm repository must be bootstrapped on GitHub before the
agent can begin execution. The bootstrap follows the manual
sequence documented in vergil-tooling#807 — a future `vrg-init`
command will automate this, but for now it is a one-time manual
process.

**Wrapper restrictions:** Several bootstrap commands require
raw `gh` or raw `git` because the wrappers block them:

- **`vrg-gh`** restricts `repo` to `view` only. `repo create`,
  `repo edit`, `api`, and `auth` are denied.
- **`vrg-git`** denies `commit` (use vrg-commit — but vrg-commit
  fails on an empty repo with no HEAD), `config` (except the
  exact `config core.hooksPath .githooks`), and does not allow
  `clone` (not in the subcommand allowlist).

These commands must be run by the human directly (via
`! <command>` in Claude Code, or from a separate terminal).
Commands below are annotated with `# human: raw gh` or
`# human: raw git` where this applies. Unmarked commands
work through the normal wrappers.

### 1. Install Lima

```bash
brew install lima
limactl --version   # Confirm >= 2.0.0
```

### 2. Create the GitHub repository

```bash
# human: raw gh — repo create is denied by vrg-gh
gh repo create vergil-project/vergil-vm \
  --public \
  --description "Lima VM image definitions for Vergil identity VMs"
```

### 3. Clone and create the initial commit

The empty repo has no HEAD, so `vrg-commit` cannot be used for
the first commit. Use raw `git` for the bootstrap only.

```bash
# human: raw git — clone is not in the vrg-git allowlist
cd ~/dev/projects/vergil-project
git clone git@github.com:vergil-project/vergil-vm.git
cd vergil-vm

# Create minimal initial files
echo "2.1.0" > VERSION
cat > vergil.toml << 'EOF'
[project]
name = "vergil-vm"
repository-type = "infrastructure"
primary-language = "shell"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[ci]
versions = ["latest"]

[publish]
release = true
docs = false

[dependencies]
vergil = "v2.1"
EOF

cat > LICENSE << 'EOF'
(GPL-3.0-or-later — copy from vergil-tooling/LICENSE)
EOF

# human: raw git — commit is denied by vrg-git, and vrg-commit
# fails on an empty repo with no HEAD (#807)
git add -A
git commit -m "chore: initial repository scaffold"
```

### 4. Create branch structure

GitHub repos default to `main`. Vergil convention uses
`develop` as the default branch with `main` for releases.

```bash
# These work through vrg-git (branch, push are allowed;
# -m and -u are not denied flags)
vrg-git branch -m main develop
vrg-git push -u origin develop

vrg-git branch main
vrg-git push -u origin main
```

### 5. Set default branch to develop

```bash
# human: raw gh — repo edit is denied by vrg-gh
gh repo edit vergil-project/vergil-vm --default-branch develop
```

### 6. Apply GitHub repository configuration

This must happen after both branches exist to avoid the
chicken-and-egg problem (#807): rulesets require branches,
and branches require commits.

```bash
# This is vrg-github-repo-config, not gh — no restriction
vrg-github-repo-config apply --repo vergil-project/vergil-vm
```

If rulesets block the initial push, temporarily disable them
via the GitHub web UI, push, then re-enable. This is the
known bootstrapping workaround until `vrg-init` exists.

### 7. Set up hooks and development environment

```bash
mkdir -p .githooks
# Copy .githooks/pre-commit from any existing Vergil repo
cp ../vergil-tooling/.githooks/pre-commit .githooks/pre-commit

# vrg-git allows the exact command: config core.hooksPath .githooks
vrg-git config core.hooksPath .githooks

# Verify vrg-commit works (normal workflow from here on)
vrg-commit --type chore --scope repo --message "enable pre-commit hook" \
  --body "Copied from vergil-tooling"
vrg-git push
```

### 8. Create CLAUDE.md

The `vrg-github-repo-config audit` command checks that
`CLAUDE.md` exists and contains the **exact** consumer template
from `vergil_tooling/data/claude_md_consumer.md` as a substring
(case-sensitive, whitespace-exact). The template includes four
mandatory sections: Memory management, Parallel AI agent
development (with structure, rules, and agent prompt contract),
Shell command policy, and Validation.

Start with the verbatim template, then add a project-specific
header above it (`# CLAUDE.md`, project overview, etc.).
Additional sections can appear before or after the template,
but the template text must not be modified.

```bash
# Copy the consumer template as the base
cp ../vergil-tooling/src/vergil_tooling/data/claude_md_consumer.md CLAUDE.md

# Prepend the project header (edit the file to add above the template):
#   # CLAUDE.md
#   ## Project Overview
#   vergil-vm provides Lima VM image definitions ...

# Verify audit compliance
vrg-github-repo-config audit --repo vergil-project/vergil-vm
```

### 9. Verify the repo is ready

```bash
vrg-github-repo-config audit --repo vergil-project/vergil-vm
# Should exit 0 (compliant)

# Create a test worktree to confirm normal workflow works
vrg-git worktree add -b feature/test-worktree .worktrees/test develop
vrg-git worktree remove .worktrees/test
vrg-git branch -d feature/test-worktree
```

After these steps, the repo is ready for normal Vergil
development workflow. The agent can create worktrees, use
`vrg-commit`, and submit PRs.

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `VERSION` | Prerequisite | Semver version (2.1.0) — created during bootstrap |
| `vergil.toml` | Prerequisite | Project metadata — created during bootstrap |
| `LICENSE` | Prerequisite | GPL-3.0-or-later — created during bootstrap |
| `.githooks/pre-commit` | Prerequisite | Commit gate — created during bootstrap |
| `CLAUDE.md` | Prerequisite | Agent instructions — created during bootstrap |
| `.gitignore` | Create | Ignore build artifacts |
| `templates/agent.yaml` | Create | Lima VM template with all provisioning |
| `tests/run-tests.sh` | Create | Test runner (shells into VM, runs each test) |
| `tests/test_base.sh` | Create | Verify OS, user, shell, sudo |
| `tests/test_tools.sh` | Create | Verify gh, uv, jq, yq, ripgrep, fzf, git |
| `tests/test_containerd.sh` | Create | Verify containerd running, nerdctl works |
| `tests/test_vergil.sh` | Create | Verify vergil-tooling installable via uv |
| `build.sh` | Create | Create VM, run provisioning, run tests, clean up |
| `.github/workflows/ci.yml` | Create | shellcheck, yamllint, template validation |

---

### Task 1: Directory Structure and Remaining Scaffold

The prerequisite bootstrap created `VERSION`, `vergil.toml`,
`LICENSE`, `.githooks/pre-commit`, and `CLAUDE.md`. This task
creates the remaining directory structure and configuration
that the bootstrap did not cover.

**Files:**
- Create: `.gitignore`

- [ ] **Step 1: Create .gitignore**

```gitignore
# Lima build artifacts
*.qcow2
*.raw
*.img
*.iso

# macOS
.DS_Store

# Editor
*.swp
*.swo
*~

# Build output
/build/
```

- [ ] **Step 2: Create directory structure**

```bash
mkdir -p templates tests .github/workflows
```

- [ ] **Step 3: Commit**

```bash
vrg-commit --type chore --scope vm --message "directory structure and gitignore" \
  --body "templates/, tests/, .github/workflows/, .gitignore"
```

---

### Task 2: Test Suite

Write all tests before the implementation. These define the
acceptance criteria for the VM. Each test is a self-contained
bash script that runs inside the VM via `limactl shell`.

**Files:**
- Create: `tests/run-tests.sh`
- Create: `tests/test_base.sh`
- Create: `tests/test_tools.sh`
- Create: `tests/test_containerd.sh`
- Create: `tests/test_vergil.sh`

- [ ] **Step 1: Write the test runner**

```bash
#!/bin/bash
# tests/run-tests.sh — Run all test scripts inside a Lima VM.
# Usage: ./tests/run-tests.sh [instance-name]
set -euo pipefail

INSTANCE="${1:-vergil-agent}"
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
failures=0
total=0

for test in "${TESTS_DIR}"/test_*.sh; do
    name="$(basename "$test")"
    total=$((total + 1))
    printf "  %-30s " "${name}"
    if limactl shell "$INSTANCE" -- bash -s < "$test" > /dev/null 2>&1; then
        echo "PASS"
    else
        echo "FAIL"
        echo "    Re-running with output:"
        limactl shell "$INSTANCE" -- bash -s < "$test" 2>&1 | sed 's/^/    /'
        failures=$((failures + 1))
    fi
done

echo ""
echo "${total} tests, ${failures} failures"
exit "${failures}"
```

- [ ] **Step 2: Write test_base.sh**

```bash
#!/bin/bash
# tests/test_base.sh — Verify base OS configuration.
set -euo pipefail

# Ubuntu 24.04
grep -q 'Ubuntu' /etc/os-release
grep -q 'VERSION_ID="24.04"' /etc/os-release

# Default shell is zsh
getent passwd "$(whoami)" | grep -q '/bin/zsh'

# Passwordless sudo works
sudo -n true

echo "test_base: all checks passed"
```

- [ ] **Step 3: Write test_tools.sh**

```bash
#!/bin/bash
# tests/test_tools.sh — Verify development tools are installed.
set -euo pipefail

check_command() {
    if ! command -v "$1" > /dev/null 2>&1; then
        echo "MISSING: $1"
        return 1
    fi
}

check_command git
check_command gh
check_command uv
check_command jq
check_command yq
check_command rg
check_command fzf
check_command curl
check_command zsh
check_command vim
check_command tmux
check_command nano

echo "test_tools: all checks passed"
```

- [ ] **Step 4: Write test_containerd.sh**

```bash
#!/bin/bash
# tests/test_containerd.sh — Verify rootless containerd is running
# and nerdctl is functional.
set -euo pipefail

# containerd is running as a user service
systemctl --user is-active containerd

# nerdctl is available
command -v nerdctl

# nerdctl can query the runtime
nerdctl info > /dev/null

# nerdctl can pull and run a minimal container
nerdctl pull --quiet ghcr.io/containerd/alpine:3.14.0
nerdctl run --rm ghcr.io/containerd/alpine:3.14.0 echo "container works"

echo "test_containerd: all checks passed"
```

- [ ] **Step 5: Write test_vergil.sh**

```bash
#!/bin/bash
# tests/test_vergil.sh — Verify vergil-tooling can be installed
# dynamically via uv and that vrg-* commands are available.
set -euo pipefail

export PATH="$HOME/.local/bin:$PATH"

# uv tool install works (install from the configured version)
uv tool install 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'

# Core vrg-* commands are available after install
command -v vrg-commit
command -v vrg-git
command -v vrg-gh
command -v vrg-validate
command -v vrg-docker-run

# Clean up (don't leave tooling installed in the test VM)
uv tool uninstall vergil-tooling

echo "test_vergil: all checks passed"
```

- [ ] **Step 6: Make test runner executable and commit**

```bash
chmod +x tests/run-tests.sh
vrg-commit --type test --scope vm --message "VM acceptance test suite" \
  --body "Test scripts for base OS, dev tools, containerd, and vergil-tooling installation"
```

---

### Task 3: Lima VM Template

The template defines the complete VM: base image, resources,
mounts, containerd configuration, and all provisioning scripts.
Lima handles containerd + nerdctl installation automatically
when `containerd.user: true` (the default). Our provisioning
scripts add the tools Lima does not install.

**Files:**
- Create: `templates/agent.yaml`

- [ ] **Step 1: Write the Lima template header and base
configuration**

Create `templates/agent.yaml`:

```yaml
# vergil-agent — Identity VM for Vergil agent sessions.
#
# Creates an Ubuntu 24.04 VM with rootless containerd, core
# development tools, and dynamic vergil-tooling installation.
#
# Usage:
#   limactl create --name=vergil-agent templates/agent.yaml
#   limactl start vergil-agent
#
# Reference:
#   docs/specs/2026-05-20-identity-vm-isolation-design.md
#   docs/specs/2026-05-20-vergil-vm-image-management-design.md

minimumLimaVersion: "2.0.0"

base:
- template:_images/ubuntu-lts

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
```

- [ ] **Step 2: Add system-level provisioning (core tools)**

Append to `templates/agent.yaml`:

```yaml
provision:
- mode: system
  script: |
    #!/bin/bash
    set -eux -o pipefail
    export DEBIAN_FRONTEND=noninteractive

    apt-get update
    apt-get install -y --no-install-recommends \
      curl wget unzip \
      jq ripgrep fzf \
      zsh vim tmux nano \
      python3 python3-venv

    # GitHub CLI
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg 2>/dev/null
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      | tee /etc/apt/sources.list.d/github-cli.list > /dev/null
    apt-get update
    apt-get install -y gh

    # yq (not in Ubuntu repos — install from GitHub releases)
    ARCH=$(dpkg --print-architecture)
    curl -fsSL "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${ARCH}" \
      -o /usr/local/bin/yq
    chmod +x /usr/local/bin/yq

    # Set zsh as default shell for the Lima user
    chsh -s /bin/zsh "{{.User}}"

    apt-get clean
    rm -rf /var/lib/apt/lists/*
```

- [ ] **Step 3: Add user-level provisioning (uv, zsh config)**

Append to `templates/agent.yaml`:

```yaml
- mode: user
  script: |
    #!/bin/bash
    set -eux -o pipefail

    # Install uv (Python package manager)
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    uv --version

    # Minimal zsh configuration
    cat > "$HOME/.zshrc" << 'ZSHRC'
    export PATH="$HOME/.local/bin:$PATH"

    autoload -Uz compinit && compinit
    setopt HIST_IGNORE_DUPS
    setopt SHARE_HISTORY
    HISTFILE="$HOME/.zsh_history"
    HISTSIZE=10000
    SAVEHIST=10000

    # Prompt: user@hostname:dir$
    PROMPT='%n@%m:%~$ '
    ZSHRC
```

- [ ] **Step 4: Add readiness probe**

Append to `templates/agent.yaml`:

```yaml
probes:
- mode: readiness
  description: "core tools installed and containerd running"
  script: |
    #!/bin/bash
    set -eux -o pipefail
    if ! timeout 120s bash -c "until command -v gh >/dev/null 2>&1; do sleep 3; done"; then
      echo >&2 "gh is not installed yet"
      exit 1
    fi
    if ! timeout 120s bash -c "until command -v uv >/dev/null 2>&1; do sleep 3; done"; then
      echo >&2 "uv is not installed yet"
      exit 1
    fi
    if ! timeout 120s bash -c "until pgrep -f 'containerd' >/dev/null 2>&1; do sleep 3; done"; then
      echo >&2 "containerd is not running yet"
      exit 1
    fi
  hint: |
    Check provisioning logs: limactl shell vergil-agent -- cat /var/log/cloud-init-output.log

message: |
  vergil-agent VM is ready.

  Shell into the VM:
    limactl shell {{.Name}}

  Run nerdctl:
    limactl shell {{.Name}} -- nerdctl run --rm alpine echo hello

  Install vergil-tooling:
    limactl shell {{.Name}} -- bash -c 'uv tool install "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1"'
```

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "Lima VM template for vergil-agent" \
  --body "Ubuntu 24.04 with rootless containerd, gh, uv, jq, yq, zsh, and developer convenience tools"
```

---

### Task 4: Build Script

The build script creates a test VM from the template, waits
for provisioning to complete, runs the test suite, and cleans
up. It is the single entry point for building and validating
the VM image.

**Files:**
- Create: `build.sh`

- [ ] **Step 1: Write build.sh**

```bash
#!/bin/bash
# build.sh — Build and test the vergil-agent VM image.
#
# Creates a temporary Lima VM from the agent template, runs
# the full test suite inside it, and cleans up.
#
# Usage:
#   ./build.sh              # Build, test, clean up
#   ./build.sh --keep       # Build, test, keep the VM running
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTANCE="vergil-agent-test"
TEMPLATE="${SCRIPT_DIR}/templates/agent.yaml"
TESTS="${SCRIPT_DIR}/tests/run-tests.sh"
KEEP=false

for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

cleanup() {
    if [ "$KEEP" = false ]; then
        echo "Cleaning up..."
        limactl stop "$INSTANCE" 2>/dev/null || true
        limactl delete --force "$INSTANCE" 2>/dev/null || true
    else
        echo "VM kept running: limactl shell $INSTANCE"
    fi
}
trap cleanup EXIT

echo "=== Building vergil-agent VM ==="
echo "Instance: $INSTANCE"
echo "Template: $TEMPLATE"
echo ""

# Validate template syntax
echo "Validating template..."
limactl validate "$TEMPLATE"
echo "Template valid."
echo ""

# Delete any previous test instance
limactl stop "$INSTANCE" 2>/dev/null || true
limactl delete --force "$INSTANCE" 2>/dev/null || true

# Create and start the VM (non-interactive)
echo "Creating VM..."
limactl create --name="$INSTANCE" "$TEMPLATE" --tty=false
echo "Starting VM..."
limactl start "$INSTANCE" --tty=false
echo "VM started."
echo ""

# Run tests
echo "=== Running tests ==="
bash "$TESTS" "$INSTANCE"
echo ""

echo "=== Build complete ==="
```

- [ ] **Step 2: Make build.sh executable and commit**

```bash
chmod +x build.sh
vrg-commit --type feat --scope vm --message "build script for VM creation and testing" \
  --body "Creates temporary Lima VM, runs test suite, cleans up"
```

---

### Task 5: CI Workflow

Static analysis only — shellcheck for all bash scripts and
yamllint for the Lima template. Running an actual VM in CI
requires QEMU on the GitHub Actions runner, which is deferred
to Plan 6 (Distribution).

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the CI workflow**

```yaml
# .github/workflows/ci.yml
name: CI

on:
  pull_request:
    branches: [develop, main]

permissions:
  contents: read

jobs:
  lint:
    name: lint
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4

    - name: shellcheck
      run: |
        sudo apt-get update && sudo apt-get install -y shellcheck
        find . -name '*.sh' -not -path './.git/*' \
          -exec shellcheck --severity=warning {} +

    - name: yamllint
      run: |
        pip install yamllint
        yamllint --strict templates/
```

- [ ] **Step 2: Commit**

```bash
vrg-commit --type ci --scope vm --message "CI workflow for static analysis" \
  --body "shellcheck for bash scripts, yamllint for Lima templates"
```

---

### Task 6: Manual Validation

This task is not automated — it requires a human or agent
with Lima installed on macOS to run the build and verify the
VM works end-to-end.

- [ ] **Step 1: Run the build script**

```bash
cd ~/dev/projects/vergil-project/vergil-vm
./build.sh --keep
```

Expected: All tests pass. The VM remains running for manual
inspection.

- [ ] **Step 2: Shell into the VM and verify interactively**

```bash
limactl shell vergil-agent-test

# Inside the VM:
gh --version
uv --version
nerdctl info
echo $SHELL     # Should be /bin/zsh
```

- [ ] **Step 3: Verify workspace mount**

```bash
# Inside the VM:
ls ~/dev/    # Should show host's ~/dev contents
```

- [ ] **Step 4: Verify vergil-tooling dynamic install**

```bash
# Inside the VM:
uv tool install 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'
vrg-git --help
vrg-commit --help
uv tool uninstall vergil-tooling
```

- [ ] **Step 5: Clean up test VM**

```bash
limactl stop vergil-agent-test
limactl delete vergil-agent-test
```

- [ ] **Step 6: Commit any fixes discovered during validation**

If any provisioning or test issues were found and fixed during
manual validation, commit them now.

---

## Self-Review Checklist

- [x] **Spec coverage:** Both specs' Phase 1 deliverables are
  covered — working VM with containerd, core tools,
  vergil-tooling dynamic install, and workspace mounts.
- [x] **Placeholder scan:** No TBD, TODO, or "implement later."
- [x] **Type consistency:** File paths, instance names, and
  command syntax are consistent across all tasks.
- [x] **Scope boundaries:** This plan does NOT include egress
  filtering, credential provisioning, vrg-session, nerdctl
  adaptation in vrg-docker-run, or pre-built image
  distribution — those are Plans 2-6.
