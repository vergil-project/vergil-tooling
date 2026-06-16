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
and `cd`s into the borrower's checkout (the whole projects directory is
mounted into every VM, so both checkouts are present).

- The value must be a fully-qualified `org/repo`.
- `shared_from` is the **only** key allowed under `[vm]` — combining it
  with a footprint/package key or a `[vm.<role>]` overlay is a config
  error. A repo either describes a VM or borrows one.
- The borrower may **use** the shared box (`session`, `start`) but not
  **manage** it: `create`, `stop`, `restart`, `update`, `destroy`, and
  `rebuild` on the borrower are refused and point at the lender repo,
  which owns the box.
- One hop only — the lender may not itself declare `shared_from`.

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
