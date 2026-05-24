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
│   vrg-container-run / vrg-validate consume these
│
Layer 3 — macOS host (user's environment, unmanaged)
    IDE, browser, Ollama, personal tools
    Vergil does not manage or constrain this layer
    The user's playground — no restrictions
```

### The Decision Boundary

Two questions determine where tooling belongs:

**Question 1: Does the agent identity need this tool during
development?**

If yes, it must be accessible inside the VM — the agent works
there. Proceed to Question 2.

If no, it belongs on the macOS host (Layer 3) or nowhere.

**Question 2: Does CI/CD also need this tool?**

- **Yes → Docker image (Layer 2).** Package it in vergil-docker,
  version it, use the same image locally and in CI. Inside the
  VM, the agent accesses it via `vrg-container-run` / `nerdctl run`
  — never by installing it directly in the VM. This preserves
  Tier 1 / Tier 2 validation parity and avoids duplicating tool
  installations across layers.
- **No → VM base image (Layer 1).** The tool is needed by the
  agent during development but has no CI counterpart. Install it
  in the VM base image under standard change control.

**The no-duplication rule:** If a tool exists in a Docker image
(Layer 2), it is always consumed via the container runtime inside
the VM. It is never also installed directly in the VM base image.
One tool, one layer, one source of truth. The two layers of
virtualization (containers inside a VM) is intentional and
defensible — each layer manages different concerns in different
ways. The VM isolates the agent identity; the containers
standardize the build/test toolchain.

```text
                 Does the agent need this tool?
                      /              \
                    YES               NO
                    /                   \
        Does CI also need it?      macOS host (Layer 3)
            /          \            or not at all
          YES           NO
          /               \
   Docker image (2)    VM base image (1)
   Use via container   Install directly
   inside the VM       in the VM
```

### Applying the Decision Boundary: Examples

| Tooling | Agent needs it? | CI needs it? | Decision | Layer |
|---|---|---|---|---|
| Python 3.14 + ruff + mypy | Yes (validation) | Yes (lint, typecheck, test) | Docker image, used via vrg-container-run | 2 |
| Ruby 3.4 + rubocop | Yes (validation) | Yes (lint, test) | Docker image, used via vrg-container-run | 2 |
| AWS CLI v2 | Yes (integration tests, deployment scripts) | Yes (CD pipelines) | Docker image, used via container | 2 |
| AWS CLI v2 | Yes (agent runs AWS commands during development) | No | VM base image | 1 |
| AWS CLI v2 | No (human explores interactively only) | No | macOS host | 3 |
| Terraform | Yes (infra management during development) | Yes (CI validation) | Docker image, used via container | 2 |
| Git, gh, uv | Yes (agent workflow essentials) | No (not in validation containers) | VM base image | 1 |
| vergil-tooling (vrg-commit, etc.) | Yes (workflow enforcement) | No (host/VM tools) | VM base image | 1 |
| containerd + nerdctl | Yes (container runtime for Layer 2) | No | VM base image | 1 |
| VS Code | No (human uses IDE on host) | No | macOS host | 3 |

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

**The shell customization gray area:** Developers are
understandably protective of their shell environment. A user
who SSHes into the VM for debugging or triage will want their
familiar prompt, aliases, and key bindings. Shell customization
is legitimate — but it's also the single most likely vector for
breaking Vergil's tooling. A `.zshrc` that reorders PATH, shadows
a core utility with an alias, or sets environment variables that
alter git or uv behavior can cause `vrg-*` commands to fail in
ways that are difficult to diagnose.

Mitigating factors: interactive SSH into the VM is expected to be
infrequent. The primary workflow is launching Claude Code
sessions via `vrg-session`, where the agent operates
autonomously. The human SSHes in for debugging, triage, or
tooling development — not for daily coding. The agent itself
does not use shell customizations; it invokes commands directly.

**Provisional approach for shell customization:** The VM ships
with a clean, functional default shell configuration. User shell
customization is acknowledged as a likely demand but is deferred
as an open design question. The risk is real: even experienced
developers can inadvertently break tooling through shell config.
Any future mechanism for shell customization must ensure that
Vergil's tools see a clean, predictable environment regardless
of what the user's interactive shell looks like — for example,
by having `vrg-*` commands source their own environment rather
than inheriting the interactive shell's state. This is a
non-trivial design problem that should be solved deliberately,
not by opening up `.zshrc` and hoping for the best.

**How this is enforced:** The initial implementation does not
include a user customization mechanism. The base image ships
with a curated set of developer convenience tools (editors,
tmux, etc.) alongside the Vergil toolchain. If users need
additional safe-category tools, they can be proposed for
inclusion in the base image through the normal change-control
process.

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

**Vergil tooling (dynamically managed, not baked in):**
- vergil-tooling is NOT pre-installed in the base image
- Installed and updated dynamically at VM startup via
  `uv tool install` (see Dynamic Tooling Management below)
- Git hooks path configured at first-run
- `vrg-*` commands available on PATH after startup

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
3. Run provisioning scripts in order (base → tools →
   containerd → hardening)
4. Run the dynamic tooling installer (to verify it works and
   to exercise the startup hook)
5. Run test suite inside the VM to verify all base tools are
   installed, containerd works, and the dynamic tooling
   installer successfully installs vergil-tooling
6. Capture a clean snapshot (with vergil-tooling uninstalled —
   it will be installed dynamically on first real boot)
7. Export the image artifact

**CI pipeline:**

On PR:
- Lint provisioning scripts (shellcheck)
- Build a test VM
- Run the full test suite inside it
- Verify the dynamic tooling installer works end-to-end
- Verify containerd can pull and run a vergil-docker image

On merge to main:
- Build the release image
- Tag with version from `VERSION` file
- Publish to GHCR (OCI artifact) or GitHub Releases

### Distribution

VM images must be pre-built and published. Requiring users to
build images locally exposes them to the full complexity of the
build environment (Lima configuration, provisioning scripts,
architecture-specific toolchain) and creates a support burden
from build failures across different macOS configurations. The
goal is a low barrier to entry — users install a pre-built image,
not a build toolchain.

Lima VM images are not OCI containers, but there are two viable
distribution mechanisms:

1. **Pre-built disk images via GitHub Releases.** Build and
   publish qcow2 or raw disk images as GitHub Release artifacts,
   one per supported architecture. Users download the image and
   point Lima at it. Simple, well-understood, no experimental
   dependencies.

2. **OCI artifact registry (preferred).** Lima supports
   OCI-based image distribution. Publish the VM image to GHCR
   as an OCI artifact (not a container image, but stored in the
   same registry). This leverages the existing GHCR
   infrastructure that vergil-docker already uses, keeps all
   Vergil artifacts in one place, and aligns with the broader
   OCI ecosystem.

**Recommended approach:** Option 2 (OCI artifacts on GHCR) if
Lima's OCI support is mature enough. Fall back to option 1
(GitHub Releases) if OCI distribution proves unreliable. Both
options deliver pre-built images — the difference is where they
are stored and how they are pulled.

Building locally (`build.sh`) remains available for vergil-vm
contributors developing the image itself, but is not the
consumption path for end users.

### Versioning

Semver, same as vergil-docker. The version tracks the image
definition, not the tools inside it:

- **Major:** Breaking changes to the VM layout, mount points,
  or user environment that require consumers to re-provision
- **Minor:** New tools added, OS version bumps, provisioning
  improvements, new base infrastructure
- **Patch:** Bug fixes, security updates, documentation

Major.minor affinity between vergil-vm and vergil-tooling: the
2.0.x releases of vergil-vm depend on the v2.0 release line of
vergil-tooling. This keeps the pairing predictable without
creating a tight coupling that requires VM rebuilds for every
tooling patch.

### Dynamic Tooling Management

Vergil-tooling is **not baked into the VM image.** It is
installed and updated dynamically, mirroring the approach used
in the Docker layer where vergil-tooling is installed at
container build time rather than pre-installed in the base image.

**Why not pre-install?** Vergil-tooling's rate of change is
high — especially during active development. Pre-installing it
would require rebuilding and republishing the VM image for every
tooling release. The base VM image should be a stable,
slowly-changing foundation (OS, core utilities, container
runtime). Fast-changing dependencies are managed dynamically.

**How it works:**

1. **VM startup hook.** When the VM boots (or when a new session
   is launched via `vrg-session`), a startup script checks the
   installed vergil-tooling version against the configured target
   (e.g., `v2.0`). If the installed version is older than the
   latest available release in the target range, it updates
   automatically via `uv tool install`.

2. **Explicit update command.** A CLI command (e.g.,
   `vrg-vm-update`) can be run inside the VM at any time to pull
   the latest vergil-tooling within the configured version range.
   This supports the mid-development-cycle scenario: you find a
   bug in vergil-tooling, fix and publish it, then run
   `vrg-vm-update` in the VM to pick up the fix immediately
   without restarting anything.

3. **Version range configuration.** The VM is configured with a
   major.minor version affinity (e.g., `v2.0`). The dynamic
   installer resolves this to the latest patch release within
   that range (e.g., `v2.0.24`). This is analogous to how
   `vergil.toml` configures the tooling version for Docker
   cache builds.

**Analogy to the Docker cache system:**

| Aspect | Docker (current) | VM (proposed) |
|---|---|---|
| Base artifact | vergil-docker image (no tooling pre-installed) | vergil-vm image (no tooling pre-installed) |
| Tooling installation | `vrg-container-cache build` installs vergil-tooling into cached image | VM startup hook installs vergil-tooling at boot |
| Version pinning | `vergil.toml` specifies version tag | VM config specifies version range |
| Mid-cycle update | Rebuild Docker cache | Run `vrg-vm-update` |
| CI behavior | Installs tooling dynamically per workflow | N/A (CI uses Docker, not VMs) |

**Consequence for VM image versioning:** Vergil-tooling updates
do NOT trigger VM image rebuilds. The VM image changes only when
the base infrastructure changes (OS updates, new core utilities,
container runtime upgrades, provisioning improvements). This
dramatically reduces the VM image release cadence while keeping
vergil-tooling current.

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

### Impact on vrg-container-run

`vrg-container-run` currently invokes `docker` as the container
runtime. Inside the identity VM, it needs to invoke `nerdctl`
instead. This requires a small adaptation:

- Detect the available runtime (`docker` or `nerdctl`)
- Use the detected runtime for all container operations
- No behavioral changes — the commands are compatible

This adaptation belongs in vergil-tooling, not vergil-vm. It's
a one-time change to `vrg-container-run` to support runtime
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

1. **OCI artifact maturity.** Lima's OCI-based image distribution
   is the preferred approach but needs validation. If OCI
   artifacts on GHCR prove unreliable, fall back to GitHub
   Releases with architecture-specific disk images.

2. **Dynamic tooling installer implementation.** The startup hook
   that installs/updates vergil-tooling needs to handle edge
   cases: network unavailability (use the last-installed version),
   version resolution failures, and concurrent sessions triggering
   simultaneous updates. The `vrg-vm-update` CLI command needs
   to be safe to run while Claude Code sessions are active.

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
