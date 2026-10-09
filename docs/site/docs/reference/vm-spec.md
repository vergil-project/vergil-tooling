# VM Spec Reference (`[vm]`)

A repo declares the VM it needs in the `[vm]` section of its
`vergil.toml`. `vrg-vm` composes that declaration with the identity's
base footprint and any host override into the effective spec for each
(identity, repo) pair, then builds a **dedicated VM** whenever the repo
customizes anything.

The composition model and rationale live in the per-repo VM profiles
design spec
([vergil-vm `docs/specs/2026-06-04-per-repo-vm-profiles-design.md`](https://github.com/vergil-project/vergil-vm/blob/main/docs/specs/2026-06-04-per-repo-vm-profiles-design.md));
this page is the key-by-key reference.

## Structure

```toml
[vm]                      # applies to every identity
packages = ["qemu-system-x86", "libvirt-clients"]

[[vm.apt_repos]]          # extra apt repositories (list of tables)
name = "hashicorp"
key_url = "https://apt.releases.hashicorp.com/gpg"
uri = "https://apt.releases.hashicorp.com"
suite = "noble"
components = "main"

[vm.vergil-user]          # role overlay: only the vergil-user identity
cpus = 12
memory = "64GiB"
disk = "300GiB"
stale_days = 7
vagrant_plugins = ["vagrant-libvirt"]
port_forwards = ["3000|10.50.0.2:3000"]   # local VM port | nested target
nested = true
```

## Precedence

Five tiers, later wins:

1. Built-in base footprint
2. Identity footprint (`identities.toml`)
3. Repo `[vm]` (all identities)
4. Repo `[vm.<role>]` (one identity)
5. Host override (`identities.toml` per-repo override; scalars pushed
   below the repo-declared floor are flagged loudly)

Scalars are **last-wins**; list keys (`packages`, `apt_repos`,
`vagrant_plugins`, `port_forwards`) **accumulate** across tiers. Declaring
any `[vm]` key marks the spec customized, which gives the repo a dedicated VM.

## Keys

| Key | Type | Default | Semantics |
|---|---|---|---|
| `cpus` | int | identity/base footprint | vCPUs (last-wins scalar) |
| `memory` | string `"<N>GiB"` | identity/base footprint | RAM (last-wins scalar) |
| `disk` | string `"<N>GiB"` | identity/base footprint | Disk size (last-wins scalar) |
| `stale_days` | int | 3 | Age threshold before `vrg-vm` prompts to rebuild |
| `packages` | list of strings | `[]` | Extra apt packages (accumulates) |
| `apt_repos` | list of tables | `[]` | Extra apt repositories — `name`, `key_url`, `uri`, `suite`, `components` (accumulates) |
| `vagrant_plugins` | list of strings | `[]` | Vagrant plugins to install (accumulates) |
| `port_forwards` | list of strings | `[]` | `"<port>\|<host:port>"` relay records — bind `<port>` in the VM and proxy to `<host:port>` (accumulates; see below) |
| `nested` | bool | `false` | Nested virtualization (last-wins scalar; see below) |
| `shared_from` | string `"org/repo"` | *(none)* | Borrow another repo's VM instead of declaring one. Mutually exclusive with every other `[vm]` key (see below) |
| `backend` | string | `"local"` | VM backend: `"local"` (Lima) or `"off-platform"` (remote cloud). Last-wins scalar; selects the driver (see below) |
| `provider` | string | *(none)* | Cloud provider when `backend = "off-platform"` (e.g. `"gcp"`, `"azure"`) — selects the OpenTofu module (see below) |
| `region` | string | *(none)* | Provider-native region when off-platform (e.g. `"us-central1"`) |
| `instance` | string | *(none)* | Provider-native, nested-virt-capable instance type when off-platform (e.g. `"n2-standard-16"`) |
| `volume` | string `"<N>GiB"` | *(none)* | **Persistent** block-volume size when off-platform — created once, reused, outlives the VM. Does **not** fall back to `disk` |
| `boot_disk` | string `"<N>GiB"` | *(module default, ~30 GiB)* | **Ephemeral** boot/root-disk size when off-platform. Optional — unset keeps the image default. Sizes the disk that holds scratch data living outside `volume` |
| `boot_disk_type` | string | *(module default)* | **Ephemeral** boot/root-disk type when off-platform, provider-native (GCP `"pd-ssd"`, Azure `"Premium_LRS"`). Optional; checked against the provider's allowed set (see below) |

The vergil-vm template owns *how* declarative installs happen — repos
never supply scripts.

## Nested virtualization (`nested`)

`nested = true` requests `/dev/kvm` inside the VM
(vergil-project/vergil-vm#131). At create time `vrg-vm` applies both
halves together:

- `--set='.nestedVirtualization = true'` — the Lima config knob
- `--set='.param.NESTED_VIRT = "true"'` — turns on the template's
  in-guest verification, which fails the build loudly when `/dev/kvm`
  did not appear

Three-layer defense, outermost first:

1. **Host preflight** — `vrg-vm create`/`rebuild` abort before any
   build (and before the destroy half of a rebuild) unless the host is
   macOS 15+ on M3-or-later Apple silicon.
2. **Lima** — rejects `nestedVirtualization` on unsupported hosts.
3. **In-guest check** — the template verifies `/dev/kvm` exists and
   fails the build rather than degrading silently to TCG emulation.

## Borrowing a VM (`shared_from`)

A repo with no VM of its own can run its sessions inside another repo's
dedicated VM:

```toml
[vm]
shared_from = "logical-minds-foundry/mq-resiliency-lab"
```

`vrg-vm session <org>/<borrower>` then shells into the **lender's** box
and `cd`s into the borrower's checkout. On a **local (Lima)** box both
checkouts are already present because the whole projects directory is
mounted into every VM. A **cloud (off-platform)** box has no such mount, so
the borrower's checkout does not exist until the session creates it: the
session clones-or-fetches the borrower onto the box's persistent volume
before homing on it (idempotent and self-healing — no rebuild, and the
lender's checkout and running services are untouched).

- The value must be a fully-qualified `org/repo`.
- `shared_from` is the **only** key allowed under `[vm]` — combining it
  with a footprint/package key or a `[vm.<role>]` overlay is a config
  error. A repo either describes a VM or borrows one.
- The borrower may **use** the shared box (`session`, `start`) but not
  **manage** it: `create`, `stop`, `restart`, `update`, `destroy`, and
  `rebuild` on the borrower are refused and point at the lender repo,
  which owns the box.
- One hop only — the lender may not itself declare `shared_from`.

## Off-platform (cloud) backend

By default a repo's VM is a local macOS Lima box (`backend = "local"`).
Setting `backend = "off-platform"` switches it to a **remote
native-x86 cloud host** driven by OpenTofu — for repos that genuinely
need native x86 (nested KVM, not TCG emulation). The backend, modules,
and provisioning live in vergil-vm
([#199](https://github.com/vergil-project/vergil-vm/issues/199)); the
design spec is
[`vergil-vm docs/specs/2026-06-19-off-platform-vm-backend-design.md`](https://github.com/vergil-project/vergil-vm/blob/main/docs/specs/2026-06-19-off-platform-vm-backend-design.md).

```toml
[vm.vergil-user]
backend   = "off-platform"
provider  = "gcp"              # selects the OpenTofu module
region    = "us-central1"      # provider-native region
instance  = "n2-standard-16"   # provider-native, nested-virt-capable
volume    = "300GiB"           # PERSISTENT volume — outlives the VM
boot_disk = "100GiB"           # EPHEMERAL boot disk — optional; dies with the VM
boot_disk_type = "pd-ssd"      # its disk type — optional; provider-native
nested    = true               # /dev/kvm in the cloud box too
cpus      = 12                 # request / under-provision intent (see below)
memory    = "64GiB"
```

- The five keys are **last-wins scalars** that ride the same five-tier
  cascade as the footprint keys. Declaring any of them dedicates the box.
  Because the cascade is resolved before validation, you may split them
  across tiers (e.g. `backend` in `[vm]`, `instance` in `[vm.<role>]`).
- `backend = "off-platform"` **requires** `provider`, `region`,
  `instance`, and `volume`. A missing key is a loud config error — no
  silent default. `volume` must be `"<N>GiB"` and never falls back to
  `disk`.
- `provider`/`region`/`instance` are **opaque provider-native strings**
  — the tooling does not enumerate them, so adding a provider is a
  vergil-vm module change with no tooling change.
- **`disk` is ignored off-platform.** On cloud there are two disks with
  opposite lifecycles: the ephemeral VM boot disk and the persistent
  `volume` declared above. The Lima `disk` knob drives neither — use
  `volume` for the persistent disk and `boot_disk` for the ephemeral one.
- **`boot_disk` sizes the ephemeral boot/root disk** (optional). Unset,
  the boot disk inherits the cloud image's default (~30 GiB); set it
  (`"<N>GiB"`) to grow the disk for workloads whose scratch data lives
  *outside* the persistent `volume` — e.g. a nested-virt image pool and
  qcow2 overlays, which belong on the wipe-on-rebuild boot disk rather
  than the never-wiped `volume`. Unlike `volume` it is **not required**
  off-platform, and it never enters the local (Lima) spec.
- **`boot_disk_type` picks the ephemeral boot disk's type** (optional).
  Unset, the key is not passed and the vergil-vm module default holds:
  on GCP, GCE picks the default type for the machine series; on Azure,
  `StandardSSD_LRS`. Set it when the boot disk
  carries I/O-heavy scratch data (e.g. a nested-virt image pool). Unlike
  `provider`/`region`/`instance`, the value is **checked at
  composition**, before any cloud call, against the provider's allowed
  set. That set mirrors the vergil-vm `vm` module's own validation
  ([vergil-vm#312](https://github.com/vergil-project/vergil-vm/issues/312)):

  | `provider` | Allowed `boot_disk_type` |
  |---|---|
  | `gcp` | `pd-standard`, `pd-balanced`, `pd-ssd`, `hyperdisk-balanced` |
  | `azure` | `Standard_LRS`, `StandardSSD_LRS`, `Premium_LRS`, `StandardSSD_ZRS`, `Premium_ZRS` |

  An unknown value is a loud config error. Values are **never mapped
  across providers**: `pd-ssd` on an Azure profile is rejected, not
  translated to a SKU. Like `boot_disk`, it is ignored on a local
  (Lima) profile, so it can sit on a named cloud instance
  (`[vm.<role>.instances.<name>]`) or on the role with the instance
  inheriting it.
- **`boot_disk_type` needs vergil-vm v2.1.42 or later.** The modules
  are fetched by the host's module tag (`vergil-vm` in
  `identities.toml`, else `vergil`, or `--tag`), not by this
  tooling's version. OpenTofu only *warns* about an undeclared variable
  in a var file, so an older module would silently build the default
  disk type. `vrg-vm create`/`rebuild` therefore check that the
  fetched module declares `boot_disk_type` and fail loudly, naming the
  tag, if it does not. `rebuild` runs this check before it destroys
  the old VM. The moving `v2.1` tag picks up v2.1.42 once it is
  released; a host pinned to an older exact tag must move the pin.
- **`instance` is authoritative over `cpus`/`memory` on cloud.** They
  stay in the spec as human-readable intent; a session-time
  under-provisioning warning (instance smaller than the declared
  `cpus`/`memory`) is part of the backend dispatcher, not this schema
  layer (it needs the provider's instance specs).

### Lifecycle and access (off-platform)

The same `vrg-vm` verbs work, dispatched on the resolved `backend`:

- `create` — `tofu apply`s the persistent `volume` (idempotent — a
  no-op if it already exists), then the ephemeral VM pinned to that
  volume's zone, blocks until cloud-init provisioning is done, injects
  GitHub App + Claude credentials over the tunnel, and clones the repo
  onto the volume (first time) or fetches (reattach). Refuses to stand
  up a second VM for a repo that already has one running.
- `session` — opens the session over the tunnel into
  `/vergil/projects/<org>/<repo>` on the volume.
- `destroy` — tears down the **ephemeral VM only**. The persistent
  volume (the repo checkout and `.claude` session history) survives.
  This is the routine end-of-day teardown.
- `rebuild` — `destroy` + recreate the VM against the **existing**
  volume; the data reattaches intact. This is the path for changing
  `boot_disk` or `boot_disk_type` on an existing box: either change
  replaces the ephemeral boot disk, which means a new instance. `list`
  flags the box `NEEDS-REBUILD` until you do.
- `destroy-volume` — the **only** command that deletes the persistent
  volume. Guarded: retype `org/repo` to confirm, or pass `--yes`.
- `update` — refreshes vergil-tooling (see
  [vergil-tooling inside the VM](#vergil-tooling-inside-the-vm)) and Claude plugins **in place**
  over the IAP tunnel on a running box (seconds, non-disruptive), exactly
  like a Lima box. `rebuild` is reserved for what genuinely needs a fresh
  image (a new base image or changed provision scripts), not a tooling
  bump. `update --all` includes off-platform boxes and updates each running
  one in place; a non-running box is skipped and reported. Two boxes that
  share an `org/repo` (one per identity) stay distinct by their identity.
- `stop` / `start` / `restart` — **not supported**. Off-platform VMs
  are ephemeral; use `destroy` / `create`.
- `list` — gains a `BACKEND` column (`local` for Lima rows, the
  provider for cloud rows). Cloud rows carry their box's `IDENTITY` so
  two boxes sharing an `org/repo` (one per identity) stay distinct.
  Without cloud credentials a cloud row's status degrades to
  `unknown (no <provider> creds)` rather than erroring or hiding the row.
- `volumes` — enumerates the persistent volumes (the long-lived,
  billable, quota-consuming disks that outlive each ephemeral VM) from
  local tofu state: `IDENTITY`, `ORG/REPO`, `DISK NAME`, `SIZE`, `ZONE`,
  `REGION`, all read from each disk's stamped labels/attributes with no
  network call. `--live` adds a `LIVE` column that cross-checks each disk
  against the provider — a disk deleted out of band shows `MISSING`; an
  unauthed/unreachable provider degrades to `unknown`. This is how you
  identify which volume to `destroy-volume` and track SSD quota usage.

Access is via the provider's identity-aware tunnel (GCP IAP) — there is
**no public IP** and no operator-IP allow-list; authentication is the
operator's existing cloud IAM/ADC. Host prerequisites are therefore
OpenTofu (≥ 1.8.0) and the provider CLI (`gcloud`, with ADC); cloud
verbs preflight both and fail with a clear remediation. The in-guest
login user defaults to the cloud image's default user and can be
overridden with `VRG_OFF_PLATFORM_SSH_USER`.

## Port forwards (`port_forwards`)

Each record is `"<port>|<host:port>"`. The vergil-vm template
(vergil-project/vergil-vm#170) provisions a `systemd-socket-proxyd`
relay per record: it binds `0.0.0.0:<port>` inside the VM and proxies
to `<host:port>` — typically a nested libvirt guest. The `0.0.0.0` bind
is auto-forwarded by Lima to the Mac's `localhost:<port>` with no extra
config. The relay is boot-persistent; the build fails loudly if the
port is already bound.

`vrg-vm` joins the accumulated records with `;` and passes them as
`--set='.param.PORT_FORWARDS = "<records>"'` — the template splits on
`;` then `|`. Repos never supply a script; the template owns the relay.

## Fingerprint and `NEEDS-REBUILD`

The composed spec is fingerprinted (SHA-256 over the declaration) and
stamped into the VM at create time. `vrg-vm list` compares the stored
fingerprint against the freshly composed one and shows `NEEDS-REBUILD`
on any drift. Editing any declarative key — including toggling
`nested` — flips the fingerprint. `nested` and `port_forwards` enter
the fingerprint payload only when set (true / non-empty), so profiles
that never declare them kept their fingerprints when the knobs were
introduced.

The off-platform keys (`backend`, `provider`, `region`, `instance`,
`volume`) follow the same rule: they enter the payload **only when
`backend = "off-platform"`**, and on that path `disk` is dropped from
the payload (it is not a cloud knob). A local profile therefore keeps
its byte-for-byte fingerprint from before these keys existed — existing
Lima VMs never falsely read `NEEDS-REBUILD` — while flipping a repo
Lima→cloud, or resizing the `instance`/`volume`, trips `NEEDS-REBUILD`
as expected. `boot_disk` enters the off-platform payload **only when
set**, so cloud VMs created before the knob existed keep their
fingerprints; declaring or resizing it trips `NEEDS-REBUILD` like
`volume`. `boot_disk_type` follows the same rule: it enters the payload
only when set, and declaring or changing it trips `NEEDS-REBUILD`.

## vergil-tooling inside the VM

Lima and cloud VMs install vergil-tooling from the vergil **package
repository** (`https://vergil-project.github.io/packages`) with `apt`,
not with `uv tool install`. The macOS host, the dev container cache and
this repo's dev-tree `.venv` are unchanged and still use `uv`.

**Which version.** The version is the identity's resolved vergil
version: the per-identity `vergil` setting in `identities.toml`, else
the config-level `vergil`. The repo's `vergil.toml` plays no part. A
`vrg-vm update --tag <ref>` overrides it for that one update and is not
remembered.

**Packaged install** (any release version):

- `vX.Y` (a release line) pins `vergil-tooling` to `X.Y.*` in
  `/etc/apt/preferences.d/vergil-tooling` and installs the newest
  package on that line.
- `vX.Y.Z` (an exact release) pins `X.Y.Z-1` and installs
  `vergil-tooling=X.Y.Z-1`.
- Before anything is written to the apt sources, `vrg-vm` downloads
  the org signing key and checks its fingerprint against the one
  pinned in vergil-tooling. It then installs `vergil-archive-keyring`,
  which owns the key and the source entry from then on.
- Every apt call `vrg-vm` makes, both the repository setup and the
  `apt-get update` and `apt-get install` of `vergil-tooling`, runs
  with `Acquire::Retries=3` and 20-second
  `Acquire::http::Timeout`/`Acquire::https::Timeout`, so a dead mirror
  connection fails or retries in seconds rather than hanging. It also
  sets `Acquire::Retries::Delay=false`, because apt 2.4–2.8 can
  deadlock on mirror failover when retries are delayed
  ([Launchpad #2003851](https://bugs.launchpad.net/ubuntu/+source/apt/+bug/2003851)),
  and `DPkg::Lock::Timeout=60`, so a dpkg lock held by
  `unattended-upgrades` or `apt-daily` on a freshly booted VM is waited
  out for up to 60 seconds instead of failing provisioning at once.
- The VM's Ubuntu codename must be a published suite (`noble` or
  `resolute`). Any other codename fails provisioning.
- If the repository has no package matching the version, for example
  a new line before its first packaged release, provisioning **fails**
  and names the version and the repository URL. It never falls back
  to another version or to a `uv` install.
- `vrg-vm update` re-runs the same steps, which upgrades the VM within
  the pin.
- Once the apt install has succeeded, any `uv`-installed copy (a legacy
  install or an earlier dev install) is removed, because the copy in
  `~/.local/bin` would shadow `/usr/bin`. A failed packaged install
  leaves the existing tooling in place.

**Dev install** (explicit, for a git ref that is not a release version,
such as `develop` or a feature branch):

```bash
vrg-vm update --tag develop
```

- This runs `uv tool install` from git into `~/.local/bin`, which
  takes precedence over the packaged `/usr/bin` copy on the VM user's
  `PATH`, and records the ref in `~/.config/vergil/tooling-dev-ref`.
- You only get a dev install by passing a dev ref. A failed packaged
  install never falls back to one.
- While it is in place, every `vrg-vm` command that touches the VM
  (`create`, `rebuild`, `start`, `update`, `session`) prints
  `DEV tooling (ref <ref>) — not the packaged install`.
- A plain `vrg-vm update` (no `--tag`) returns the VM to the packaged
  install and removes the dev copy.
