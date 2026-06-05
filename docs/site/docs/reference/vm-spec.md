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
`vagrant_plugins`) **accumulate** across tiers. Declaring any `[vm]`
key marks the spec customized, which gives the repo a dedicated VM.

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
| `nested` | bool | `false` | Nested virtualization (last-wins scalar; see below) |

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

## Fingerprint and `NEEDS-REBUILD`

The composed spec is fingerprinted (SHA-256 over the declaration) and
stamped into the VM at create time. `vrg-vm list` compares the stored
fingerprint against the freshly composed one and shows `NEEDS-REBUILD`
on any drift. Editing any declarative key — including toggling
`nested` — flips the fingerprint. `nested` enters the fingerprint
payload only when true, so profiles that never set it kept their
fingerprints when the knob was introduced.
