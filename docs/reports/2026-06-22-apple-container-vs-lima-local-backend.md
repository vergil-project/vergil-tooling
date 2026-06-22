# Apple `container` / container-machine as a local VM backend — research report

- **Date:** 2026-06-22
- **Issue:** [vergil-tooling#1740](https://github.com/vergil-project/vergil-tooling/issues/1740)
- **Status:** Deferred research — captured for later revisit
- **Scope:** Local macOS (Apple Silicon) backend only. The off-platform
  GCloud/Azure backend is explicitly out of scope.

> **Convention note on data vs. judgment.** Throughout this report, claims
> drawn directly from a source are marked **[data]** with a citation; claims
> that are the author's reasoning on top of those facts are marked
> **[judgment]**. This separation is deliberate: the recommendation hinges on
> a number nobody has measured yet, and the reader should be able to tell
> exactly where the facts stop and the inference begins.

## 1. The question

Apple shipped a first-party containerization stack for macOS (`container` CLI
and the `container machine` persistent-environment mode, introduced at WWDC25
and maturing through macOS 26 "Tahoe"). The question raised: could this native
tool **replace Lima** as the local backend behind the unified Vergil VM session
interface, and does it bring **functional** advantages beyond the presumed
performance wins of being Apple's own stack?

This is purely about the **local** path. Vergil exposes one "VM session"
interface with two backends:

- **Local** (the majority of development): lightweight, concurrent VMs on Apple
  Silicon, **currently implemented with Lima** (`vergil-vm`). Apple `container`
  is a candidate *only* here.
- **Off-platform** (in progress, `off-platform-dispatch`): full VMs on Google
  Cloud / Azure, used specifically to build **x86-native** software stacks
  without paying the ARM emulation penalty. Apple `container` is irrelevant to
  this path and it stays as-is.

## 2. Today's architecture (grounded in the repos)

- **`vergil-vm`** — Lima VM image definitions for "Vergil identity VMs." The
  Lima VM is the **sandbox boundary** for running Claude Code: it scopes host
  filesystem access to a single project directory and authenticates to GitHub
  through a dedicated, scoped GitHub App. **[data — `vergil-vm/README.md`]**
- **`vergil-docker`** — the "Virtual Docker Repo." Language toolchain images
  (`dev-ruby`, `dev-python`, `dev-java`, `dev-go`, `dev-rust`, `dev-base`) at
  multiple versions, published to
  `ghcr.io/vergil-project/dev-{language}:{version}`. The build tooling prefers
  `nerdctl` (Lima + containerd) and falls back to `docker`.
  **[data — `vergil-docker/README.md`]**

**Resulting topology [judgment, from the two facts above]:**

```text
macOS host (Apple Silicon)
└── Lima VM  (Virtualization.framework / vz)         ← agent sandbox boundary
    └── containerd / nerdctl
        └── dev-{lang}:{ver} containers              ← pulled from GHCR ("VDR")
```

The inner language containers are **ordinary Linux containers running inside a
Linux VM** — a single guest kernel shared by all of them. This is *not* nested
virtualization. **The "can a container run containers?" worry does not apply to
the current design**, because the inner layer never needed nesting in the first
place: Docker/containerd inside a Linux VM is the ordinary case. **[judgment]**

## 3. What Apple actually shipped

Two distinct things, which map very differently onto the topology above:

### 3.1 `container` (the runtime) — VM-per-container

- Each Linux container runs inside **its own lightweight VM** via the macOS
  Virtualization framework, giving hardware-level isolation per container.
  **[data — apple/container technical overview; WWDC25 "Meet Containerization"]**
- A custom init written in Swift, **`vminitd`**, runs as PID 1 inside each VM,
  managing network interfaces, mounting filesystems, and supervising processes.
  **[data — WWDC25 / technical overview]**
- The guest root filesystem is **stripped** (no core utilities, no dynamic
  `libc`); `vminitd` is a static executable built with Swift's Static Linux
  SDK. Combined with a minimal kernel, this yields **sub-second startup**.
  **[data — WWDC25 / technical overview]**

These are *ephemeral, minimal* VMs. They are **not** general-purpose
environments you would install Docker into and run nested containers within.
**[judgment]**

### 3.2 `container machine` — persistent full Linux environment

- A **persistent** Linux environment with a real init system — you can run
  `systemctl start postgresql` and similar. **[data — apple/container
  `container-machine.md`]**
- Maps the macOS username and home directory into the guest (home mounted at
  `/Users/<username>`), enabling edit-on-Mac / build-in-Linux. Mounts can be
  read-write, read-only, or omitted. CPU/memory are adjustable. **[data —
  `container-machine.md`]**
- Supports **nested virtualization** only on M3+ chips with macOS 15+, and only
  with a custom kernel built with `CONFIG_KVM=y`. **[data —
  `container-machine.md`]**

`container machine` is the near-drop-in analogue of a Lima VM: a persistent
Linux box you can install containerd/Docker into and run the VDR images inside,
exactly as today. **[judgment]**

### 3.3 Networking

- Every container gets its **own dedicated IP** via the `vmnet` framework — no
  port-forwarding juggling. **[data — Apple container networking guide]**
- **Caveat:** on macOS 15, containers on the same network are **isolated from
  one another** (cannot interact). Full inter-container networking — each
  container with its own network stack — arrives in **macOS 26 (Tahoe)**.
  **[data — Apple container networking guide]**

## 4. The load-bearing correction: Lima is *already* on Apple's hypervisor

The intuitive premise — "it's Apple's native stack, so it must be faster" — is
largely **already true inside Lima**:

- Lima's `vz` driver runs guests on Apple's **Virtualization.framework**, and
  `vz` has been the **default** driver on macOS since **Lima v1.0**. It includes
  **Rosetta 2** integration (x86_64 binaries on ARM) and **VSOCK** support.
  **[data — Lima `vz` docs; DeepWiki Lima VZ driver]**

**Implication [judgment]:** switching the backend from Lima to Apple
`container`/`container machine` does **not** buy a faster hypervisor — both sit
on the same Apple hypervisor. Any real performance difference comes from
*elsewhere* (VM minimalism, cold-start time, the networking model), not from
"finally using Apple's stack." Performance expectations should be set
accordingly, and validated by measurement rather than assumed.

## 5. Functional advantages that are genuinely new

Beyond raw performance, Apple's stack offers differences that Lima does not
provide out of the box:

1. **OCI-native session boundary [judgment, on data that it is an OCI runtime].**
   The sandbox itself becomes a versioned, registry-distributable OCI image.
   The "Virtual Docker Repo" could graduate from a registry of *inner
   toolchains* to a registry of *whole session images*. Strong conceptual
   alignment with how the ecosystem already thinks about images.
2. **Per-container dedicated IP / network stack [data].** Cleaner multi-service
   networking than Lima's single-VM port-forwarding — gated on macOS 26 for
   inter-container communication (§3.3).
3. **First-party, OS-bundled, fewer moving parts [judgment].** No
   Lima/Colima/QEMU layer to maintain; updates arrive with the OS. This is the
   most direct hit on the "fewer moving parts / native stack" motivation.
4. **Per-unit lightness for concurrency [data: minimal rootfs + static
   `vminitd` + sub-second start].** Genuinely lighter *per container* than a
   full Lima VM — **but only realized if work is decomposed into many small
   containers** (see the tension in §6).
5. **Optional hardware isolation per toolchain [data].** Available "for free"
   even though stronger per-toolchain isolation was not a stated driver.

## 6. The central tension

The two stated motivations **pull in opposite directions**:

- **Per-session overhead / concurrency** is best served by the
  **VM-per-container** model — many minimal VMs.
- But the **current topology** (the agent spins up toolchain containers
  *inside* its sandbox) maps cleanly onto a **`container machine` with Docker
  inside** — i.e. **one heavier VM**. That path gives up most of the lightness
  win, and since Lima already uses `vz`, the net gain over today is small. It is
  essentially "Lima with an Apple badge." **[judgment]**

To actually cash the concurrency win, the toolchains would stop being
nested-inside and become **sibling** Apple containers orchestrated by the
Vergil session control plane on the host — a real re-architecture, and one
gated on **macOS 26** for the agent↔toolchain wiring (§3.3). **[judgment]**

## 7. Approaches considered

### A — `container machine` drop-in (low risk, low payoff)

Local session = one Apple `container machine`; containerd/Docker + the VDR
images run inside, unchanged. Delivers the "native stack / fewer moving parts"
motivation and pleasant home-mounting DX. **Barely moves the concurrency
needle**, because it is one full VM and Lima is already on the same hypervisor.

### B — VM-per-container re-architecture (high payoff, high effort)

The Vergil session control plane orchestrates each toolchain as its own minimal
Apple container, wired together by dedicated IPs; the agent sandbox is itself a
container. Cashes **both** motivations. Costs: rethinks the "agent runs Docker
inside its sandbox" assumption, requires macOS 26, and bets a core substrate on
roughly one-year-old technology.

### C — Keep Lima; add Apple as a pluggable third backend; let numbers decide (recommended)

Vergil **already** abstracts local-vs-off-platform behind one session
interface, so adding `apple-container` as a third backend is natural and cheap.
Keep Lima as the shipping local backend; prototype B's primitive (run one
toolchain as a standalone Apple container) and **measure cold-start and memory
against an equivalent Lima session** on a real workload. Let the measured delta
decide whether B's re-architecture is justified.

## 8. Recommendation

**Approach C. [judgment]** The entire premise hinges on a number nobody has
measured yet: *how much lighter, concurrently, is a minimal Apple container than
a Lima session on our real workload?*

- If the delta is **dramatic**, approach B earns its re-architecture.
- If the delta is **marginal** — plausible, since both share the Apple
  hypervisor — then a migration onto young technology buys mainly a cosmetic
  "native stack" win, and Lima should stay.

Adding Apple as a pluggable backend keeps the bet cheap and reversible, and
defers the irreversible commitment until evidence exists.

## 9. Open questions / what a future spike should measure

1. **Concurrency benchmark:** cold-start latency and steady-state memory for N
   concurrent minimal Apple containers vs. N Lima sessions, on a representative
   multi-language workload.
2. **macOS version floor:** inter-container networking needs macOS 26 (§3.3).
   What is the actual macOS distribution across developer machines?
3. **Agent↔toolchain wiring under VM-per-container:** how does the sandboxed
   agent reach sibling toolchain containers (exec, network, shared mounts)
   without reintroducing the complexity Lima hides today?
4. **Security boundary parity:** can Apple `container` reproduce `vergil-vm`'s
   guarantees — filesystem scoping to one project dir, scoped GitHub App
   credential handling — at least as strongly?
5. **VDR consumption:** do the `ghcr.io/vergil-project/dev-*` images run
   unmodified under Apple `container`, including the `nerdctl`/`containerd`
   build path, or only under `docker`?
6. **x86 path:** confirm the off-platform GCloud/Azure backend remains the
   answer for native x86 builds (Apple `container` can run amd64 images via
   Rosetta emulation, but that reintroduces exactly the emulation cost the
   off-platform path exists to avoid).

## 10. Sources

- Apple — `container machine` docs:
  <https://github.com/apple/container/blob/main/docs/container-machine.md>
- Apple — `container` technical overview:
  <https://github.com/apple/container/blob/main/docs/technical-overview.md>
- Apple — container networking guide:
  <https://www.mintlify.com/apple/container/guides/networking>
- WWDC25 — "Meet Containerization":
  <https://developer.apple.com/videos/play/wwdc2025/346/>
- Lima — VZ driver (Virtualization.framework) docs:
  <https://lima-vm.io/docs/config/vmtype/vz/>
- DeepWiki — Lima VZ driver:
  <https://deepwiki.com/lima-vm/lima/10.2-vz-driver-(macos-virtualization.framework)>
