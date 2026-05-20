# VM Image Management Design (vergil-vm)

**Issue:** #894
**Date:** 2026-05-20
**Status:** Draft
**Related:** #892 (identity VM isolation), vergil-docker (analogous
repo for container images), #882 (Docker-based isolation exploration)

## Problem

The identity VM isolation design (#892) established that each agent
identity runs inside a persistent Lima VM. That design assumes a
standardized, versioned VM image — but does not specify how it is
built, maintained, or distributed.

Without a managed image pipeline, VM provisioning becomes ad-hoc:
inconsistent toolchain versions, untested configurations, and
drift between what developers run locally and what the project
expects. This is the same problem vergil-docker solved for
container images, now applied to the VM layer.

Additionally, some tooling requirements straddle the boundary
between the VM and Docker layers. A clear decision framework is
needed to determine where each piece of tooling belongs.

## Three-Layer Tooling Architecture

The combined identity-VM + Docker architecture creates three
distinct layers, each with a different scope and change-control
model:

```text
Layer 1 — VM base image (vergil-vm, standardized)
│   OS (Ubuntu), core utilities, shell environment
│   Vergil tooling (vrg-commit, vrg-git, vrg-gh, etc.)
│   Container runtime (containerd + nerdctl)
│   Git, gh, uv, core dev essentials
│   Same for every Vergil user — no customization
│
Layer 2 — Docker images (vergil-docker, standardized)
│   Language tooling (dev-python, dev-ruby, dev-go, etc.)
│   Infrastructure tooling (future: AWS, GCP, etc.)
│   Pulled into the VM as containers via nerdctl
│   Same images used in CI on GitHub Actions
│   vrg-docker-run / vrg-validate consume these
│
Layer 3 — macOS host (user's environment, unmanaged)
    IDE, browser, Ollama, personal tools
    Vergil does not manage or constrain this layer
    The user's playground — no restrictions
```

### The Decision Boundary

A single test determines where tooling belongs:

**Does CI need this tooling?**

- **Yes → Docker image (Layer 2).** Package it in vergil-docker,
  version it, use the same image locally and in CI. This
  preserves the Tier 1 / Tier 2 validation parity that the
  project depends on.
- **No → VM base image (Layer 1)** if it's core infrastructure
  that every Vergil user needs (git, uv, containerd, Vergil
  tooling itself). These are the foundation that makes the VM
  functional as an agent development environment.
- **Neither → macOS host (Layer 3).** If it's not needed in CI
  and not needed for Vergil's core operation, it doesn't belong
  in the VM. The user's macOS host is the place for personal
  tools, IDE preferences, and anything else outside Vergil's
  scope.

### Applying the Decision Boundary: Examples

| Tooling | CI need? | Decision | Layer |
|---|---|---|---|
| Python 3.14 + ruff + mypy | Yes (lint, typecheck, test) | Docker image | 2 |
| Ruby 3.4 + rubocop | Yes (lint, test) | Docker image | 2 |
| AWS CLI v2 | Yes (if used in deployment/integration tests) | Docker image | 2 |
| AWS CLI v2 | No (interactive exploration only) | Not in VM — use macOS host | 3 |
| Terraform | Yes (if used in CI for infra validation) | Docker image | 2 |
| Git, gh, uv | No (agent workflow essentials) | VM base image | 1 |
| vergil-tooling (vrg-commit, etc.) | No (host/VM workflow tools) | VM base image | 1 |
| containerd + nerdctl | No (container runtime for Layer 2) | VM base image | 1 |
| VS Code | No | Not in VM — use macOS host | 3 |
| Custom shell aliases | No | Not in VM — use macOS host | 3 |

### VM Customization Policy

The VM image is a controlled environment. The design goal is to
allow flexibility wherever possible, provided it does not
negatively affect the behavior of Vergil's tooling.

**The constraint:** User customizations that could alter the
behavior of workflow tools — changing PATH ordering, replacing
core utilities, modifying shell initialization in ways that
affect `vrg-*` commands, installing packages that conflict with
Vergil's dependencies — are not supported. If a customization
causes Vergil's tooling to behave differently from the standard
image, the resulting issues are unsupportable.

**What IS allowed:** Developer convenience tools that have zero
impact on Vergil's behavior. Text editors (nano, vim, emacs),
terminal multiplexers (tmux, screen), shell themes, file
browsers, and similar utilities are fine. These tools don't
touch Vergil's toolchain, don't modify PATH in ways that affect
`vrg-*` commands, and don't alter git, gh, or container runtime
behavior.

**What is NOT allowed:** Anything that modifies the VM's
toolchain, overrides Vergil-managed binaries, alters the
container runtime configuration, or changes the behavior of
git, gh, uv, or any `vrg-*` command. The line is: does this
customization change what happens when Vergil's tools run? If
yes, it's not supported.

**How this is enforced:** The initial implementation does not
include a customization mechanism. The base image ships with a
curated set of developer convenience tools (editors, tmux, etc.)
alongside the Vergil toolchain. If users need additional
safe-category tools, they can be proposed for inclusion in the
base image through the normal change-control process.

A formal extension mechanism (e.g., a constrained package
manifest for safe-category tools only) may be added in a later
version if demand warrants it. The key design constraint for any
such mechanism is that it must not be a vector for changes that
affect Vergil's behavior — no arbitrary shell scripts, no
unconstrained package installation, no PATH or environment
variable overrides.

**Summary of the three environments:**

- **macOS host:** The user's playground. Install whatever you
  want. Vergil doesn't care and doesn't constrain it.
- **Identity VM:** Vergil's controlled environment. Standardized
  base with developer convenience tools. Flexibility where safe;
  locked down where Vergil's behavior could be affected.
- **Docker images:** The portable, CI-parity layer. Same
  everywhere. No customization.

## vergil-vm Repository

### Purpose

Manage Lima VM image definitions with the same rigor that
vergil-docker applies to container images: deterministic builds
from source-controlled definitions, versioned releases, and a
publish pipeline.

### Analogy to vergil-docker

| Aspect | vergil-docker | vergil-vm |
|---|---|---|
| **What it produces** | OCI container images | Lima VM image definitions |
| **Registry** | GHCR (`ghcr.io/vergil-project/`) | TBD (see Distribution below) |
| **Build mechanism** | Dockerfile templates + `build.sh` | Lima YAML templates + provisioning scripts |
| **Versioning** | Semver, `VERSION` file | Semver, `VERSION` file |
| **Image naming** | `{prefix}-{language}:{version}` | `vergil-agent:{version}` (single image) |
| **Multi-arch** | `linux/amd64` + `linux/arm64` | `aarch64` (Apple Silicon) + `x86_64` (Intel Mac) |
| **Change control** | PR review, CI validation, security scanning | PR review, CI validation, image testing |
| **Customization** | None — same image for all consumers | None — same image for all consumers |

### Repository Structure (Proposed)

```text
vergil-vm/
├── templates/
│   └── agent.yaml.tpl          # Lima VM template
├── provisioning/
│   ├── base.sh                 # Core OS setup, user creation
│   ├── tools.sh                # Git, gh, uv, core utilities
│   ├── vergil.sh               # vergil-tooling installation
│   ├── containerd.sh           # Rootless containerd + nerdctl
│   └── hardening.sh            # Security hardening, cleanup
├── config/
│   ├── egress.allow.default    # Baseline egress allowlist
│   └── sshd_config             # SSH server configuration
├── tests/
│   ├── test_tools.sh           # Verify installed tools
│   ├── test_containerd.sh      # Verify container runtime
│   ├── test_vergil.sh          # Verify vergil-tooling
│   └── test_egress.sh          # Verify egress filtering
├── .github/workflows/
│   ├── ci.yml                  # PR validation
│   └── cd.yml                  # Build + publish
├── VERSION                     # Semver version
├── vergil.toml                 # Project metadata
├── CHANGELOG.md
└── docs/
```

### VM Image Contents

The single `vergil-agent` image contains:

**Operating system:**
- Ubuntu LTS (24.04 or current LTS at build time)
- Minimal server install, security updates applied at build

**Core utilities:**
- Git, gh (GitHub CLI)
- uv (Python package manager)
- curl, wget, jq, yq, ripgrep, fzf
- zsh (default shell for the `agent` user)

**Vergil tooling:**
- vergil-tooling installed via `uv tool install` from a pinned
  version tag
- Git hooks path configured
- `vrg-*` commands available on PATH

**Container runtime:**
- Rootless containerd + nerdctl
- Configured to pull from GHCR (vergil-docker images)
- No Docker daemon, no Docker Desktop dependency

**Network configuration:**
- SSH server for host-to-VM access
- Egress filtering support (iptables DNAT rules for HAProxy
  integration)

**Developer convenience tools:**
- Text editors: nano, vim (with vim-plug)
- Terminal multiplexer: tmux
- Shell: zsh with a clean default configuration
- These tools have zero impact on Vergil's behavior and are
  included for developer comfort during interactive sessions

**User environment:**
- `agent` user (uid 1000), passwordless sudo
- Home directory at `/home/agent`
- Workspace mounts at host-path-preserving locations via virtiofs

**What is explicitly NOT included:**
- Language runtimes (Python, Ruby, Go, etc.) — these are in
  Docker images (Layer 2)
- Cloud provider CLIs (AWS, GCP, Azure) — these are either in
  Docker images (if CI needs them) or on the macOS host
- GUI applications or IDEs — use the macOS host for these
- User-specific shell customizations, aliases, dotfiles — the
  VM ships with clean defaults

### Build Pipeline

**Local build:**

```bash
cd vergil-vm
./build.sh
# Produces: vergil-agent Lima template + provisioned VM
```

The build process:
1. Render the Lima YAML template with version placeholders
   resolved
2. Boot a fresh VM from the template
3. Run provisioning scripts in order (base → tools → vergil →
   containerd → hardening)
4. Run test suite inside the VM to verify all tools are
   installed and functional
5. Capture a clean snapshot
6. Export the image artifact

**CI pipeline:**

On PR:
- Lint provisioning scripts (shellcheck)
- Build a test VM
- Run the full test suite inside it
- Verify vergil-tooling works (vrg-validate inside a container
  inside the VM)

On merge to main:
- Build the release image
- Tag with version from `VERSION` file
- Publish (see Distribution below)

### Distribution

Lima VM images are not OCI containers — they can't be pushed to
GHCR the same way Docker images can. Distribution options:

1. **Git-based (simplest):** The vergil-vm repository IS the
   distribution mechanism. Users clone it and run `build.sh` to
   produce their local VM. The Lima YAML template and
   provisioning scripts are the artifact. Version pinning is via
   git tags.

2. **Pre-built disk images:** Build and publish qcow2 or raw
   disk images as GitHub Release artifacts. Users download the
   image and point Lima at it. Faster provisioning (no build
   step), but larger artifacts and architecture-specific.

3. **OCI artifact registry:** Lima supports OCI-based image
   distribution. Publish the VM image to GHCR as an OCI
   artifact (not a container image). Experimental but aligns
   with the existing GHCR infrastructure.

**Recommended starting point:** Option 1 (git-based). It's the
simplest, requires no new infrastructure, and matches how
vergil-tooling itself is distributed (`uv tool install` from a
git URL). Pre-built images (option 2) can be added later when
build time becomes a pain point.

### Versioning

Semver, same as vergil-docker. The version tracks the image
definition, not the tools inside it:

- **Major:** Breaking changes to the VM layout, mount points,
  or user environment that require consumers to re-provision
- **Minor:** New tools added, version bumps of included tools,
  provisioning improvements
- **Patch:** Bug fixes, security updates, documentation

The vergil-tooling version inside the VM is pinned in the
provisioning scripts and updated via the normal dependency-update
workflow. A vergil-tooling version bump is a minor version bump
of vergil-vm.

### Relationship to Corral

Corral (https://gitlab.com/dmorel69/corral) provides its own VM
provisioning via Lima YAML templates and bootstrap scripts. The
vergil-vm approach is architecturally similar but differs in
scope:

- **Corral:** General-purpose agent VM with broad toolchain
  (AWS, Terraform, kubectl, etc.). Per-project scoping.
  User-configurable egress allowlists.
- **vergil-vm:** Minimal agent VM with Vergil-specific tooling.
  Per-identity scoping (#892). No user customization.
  Standardized and versioned.

Corral's provisioning approach (cloud-init + bootstrap scripts)
is a useful reference for vergil-vm's implementation. The
projects may share techniques without sharing code, since their
design philosophies diverge on customization and scoping.

## Containerd as the Container Runtime

The identity VM uses rootless containerd with nerdctl instead of
Docker. This is a deliberate choice:

1. **Open source.** containerd is a CNCF graduated project.
   nerdctl is open source. No licensing concerns, unlike Docker
   Desktop which requires a paid subscription for larger
   organizations.
2. **Docker-compatible.** nerdctl provides a Docker-compatible
   CLI (`nerdctl run` ≈ `docker run`). Most Docker workflows
   translate directly.
3. **Already proven.** Corral uses this exact stack and confirms
   it works for development workflows inside Lima VMs.
4. **No daemon.** Rootless containerd runs without a system
   daemon, reducing the VM's attack surface and resource
   overhead.

### Impact on vrg-docker-run

`vrg-docker-run` currently invokes `docker` as the container
runtime. Inside the identity VM, it needs to invoke `nerdctl`
instead. This requires a small adaptation:

- Detect the available runtime (`docker` or `nerdctl`)
- Use the detected runtime for all container operations
- No behavioral changes — the commands are compatible

This adaptation belongs in vergil-tooling, not vergil-vm. It's
a one-time change to `vrg-docker-run` to support runtime
detection.

### Impact on vergil-docker Images

vergil-docker images are OCI-compliant container images. They
work with any OCI-compatible runtime, including containerd/nerdctl.
No changes to vergil-docker are needed.

The one consideration is image pulling: nerdctl pulls from the
same registries as Docker (GHCR, Docker Hub, etc.) but
authentication configuration is different (`nerdctl login` vs.
`docker login`). The VM provisioning scripts handle this —
GHCR authentication is configured during VM setup.

## Open Questions

1. **Distribution model.** Git-based distribution (clone + build)
   is the recommended starting point. Pre-built images or OCI
   artifacts are future optimizations. Revisit when build time
   or first-setup friction becomes a real pain point.

2. **vergil-tooling version pinning.** The VM pins a specific
   vergil-tooling version. When vergil-tooling releases a new
   version, vergil-vm needs a corresponding update. This is the
   same dependency-update pattern used across all Vergil repos,
   handled by the existing `dependency-update` workflow.

3. **Ubuntu version policy.** Track Ubuntu LTS releases? Pin to
   a specific release and update on a schedule? The simplest
   approach is to track the current LTS and update when a new
   LTS ships (every 2 years), with security patches applied at
   every vergil-vm build.

4. **Egress allowlist management.** The baseline egress allowlist
   ships with vergil-vm. Per-project overlays live in each
   project's repository (`.corral/egress.allow` or a
   Vergil-specific equivalent). The host-side HAProxy/pf setup
   is a separate concern — it may live in vergil-vm's
   documentation or in a dedicated setup script. Details deferred
   to implementation.

5. **Multi-architecture support.** Apple Silicon (aarch64) is the
   primary target. Intel Mac (x86_64) support depends on whether
   any Vergil users are on Intel hardware. Lima supports both via
   Apple Virtualization Framework (ARM) and QEMU (Intel).

## References

- [#892 — Identity VM isolation design](https://github.com/vergil-project/vergil-tooling/issues/892)
- [#882 — Docker-based agent isolation exploration](https://github.com/vergil-project/vergil-tooling/issues/882)
- [vergil-docker — Container image management](https://github.com/vergil-project/vergil-docker)
- [Corral — Lima VM isolation for Claude Code](https://gitlab.com/dmorel69/corral)
- [Lima — Linux virtual machines on macOS](https://lima-vm.io/)
- [containerd — Industry-standard container runtime](https://containerd.io/)
- [nerdctl — Docker-compatible CLI for containerd](https://github.com/containerd/nerdctl)
