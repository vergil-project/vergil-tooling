# Off-Platform VM Backend Dispatch (vergil-tooling slice) Design

**Issues:**

- [vergil-tooling #1706 — `vrg-*` wrapper + credential/profile conventions for remote cloud session hosts](https://github.com/vergil-project/vergil-tooling/issues/1706)

**Date:** 2026-06-22

**Status:** Design (from brainstorming, 2026-06-22).

**Spans two repositories.** This is the `vergil-tooling` companion to
[vergil-vm #199](https://github.com/vergil-project/vergil-vm/issues/199). The
authoritative cross-repo contract — OpenTofu module interface, two-state lifecycle,
provisioning model, credential flows, security boundary — lives in vergil-vm's
[`docs/specs/2026-06-19-off-platform-vm-backend-design.md`](https://github.com/vergil-project/vergil-vm/blob/develop/docs/specs/2026-06-19-off-platform-vm-backend-design.md).
That spec specifies *what* the tooling does at the interface level and explicitly
defers *how* — "the mechanism is the companion's" — to this document. This spec does
not re-litigate decided behavior; it specifies the `vrg-vm` dispatcher mechanism.

## Background: what is already done

- **vergil-vm side (merged, released in 2.1.27, commit `b75610b`).** Backend-neutral
  provisioning scripts (`templates/provision/*.sh`), the GCP `volume` + `vm` OpenTofu
  modules implementing the provider-agnostic `opentofu/interface.json` contract,
  cloud-init that synthesizes readiness, and the module tests. Azure (phase 3) is not
  built; that is a vergil-vm concern, not this slice's.
- **vergil-tooling side (phase 1, merged in PR #1715).** The profile *schema*:
  `backend`/`provider`/`region`/`instance`/`volume` parsing in `lib/config.py`,
  composition through the full five-tier cascade in `lib/vm_spec.py`, the closed
  backend enum and off-platform required-key validation (`SpecError`), and the
  fingerprint fold that leaves local Lima profiles byte-for-byte unchanged. This
  layer is executable but inert — nothing reads `ComposedSpec.off_platform` yet to
  actually build a cloud VM.

## Problem

`vrg-vm` can read an off-platform profile but cannot stand up a cloud VM. The CLI
(`bin/vrg_vm.py`) imports ~25 functions directly from `lib/lima.py` and drives every
verb through `limactl`. Issue #1706's remaining scope — backend dispatch, `tofu`
invocation and state management, SSH session transport, credential injection on a
remote peer, and the `list`/`destroy-volume`/`update` verb changes — has no
implementing code.

## Goals

- **Same verbs, second backend.** `vrg-vm create/start/stop/destroy/rebuild/session/
  list` work for an off-platform VM exactly as for Lima, dispatched on the composed
  `backend`.
- **One provisioning truth, no credential drift.** The guest-side credential and
  tooling logic is written once and runs over either transport (`limactl` or `ssh`),
  realizing the vergil-vm spec's "cred injection logic is unchanged — only the
  transport changes" as a structural property rather than copy-paste.
- **Local Lima default unaffected.** A repo with no `backend` key behaves identically;
  the Lima code path is unchanged in behavior.
- **No credential ever persists on the detachable volume.** Both the GitHub App
  private key and the Claude OAuth token live on the ephemeral boot disk and die with
  the VM; only the repo checkout and session history persist on the volume.

## Non-goals

- The OpenTofu modules, provisioning scripts, cloud-init, and the gated real-cloud
  e2e test — all owned by vergil-vm.
- Azure — a vergil-vm phase-3 module add; this dispatcher is already provider-blind,
  so it requires no tooling change when Azure lands.
- An auto-reaper or billing surface in `list` — rejected as over-machinery in the
  vergil-vm spec (fleet-of-one); the cost guard is documented teardown discipline.
- An in-place tooling-refresh path for off-platform — `update` maps to `rebuild`.

## Delivery

One comprehensive spec (this document) and one implementation plan, landed as **one
large vergil-tooling PR**. Because the module tarball must exist before `vrg-vm` can
fetch it, the work necessarily spans two repos: a **small vergil-vm publish PR lands
and cuts a release first** (see "Cross-repo dependency"), then the vergil-tooling
dispatch PR consumes it.

## Architecture

`bin/vrg_vm.py` stops importing `lib/lima.py` directly. It obtains a `Backend` from a
dispatcher keyed on the composed spec, and every verb and pipeline stage calls
`backend`/transport methods. There is exactly one dispatch decision point — no
`if off_platform` scattered through the verbs.

### File layout

```
src/vergil_tooling/lib/
  vm_backend.py    Backend protocol + select_backend(spec) -> Backend
  vm_transport.py  Transport protocol: run() / pipe() / exec_session()
                     LimaTransport (wraps limactl shell)
                     SshTransport  (wraps ssh/scp)
  vm_guest.py      guest-side steps written ONCE, each takes a Transport:
                     inject_credentials, install/update_tooling,
                     copy/link_claude_config, get_tooling_version,
                     vm_probe, read_fingerprint, vm_spec_status
  vm_lima.py       LimaBackend  — limactl lifecycle, uses LimaTransport
  vm_cloud.py      OffPlatformBackend — tofu+ssh lifecycle, uses SshTransport
  lima.py          trimmed: keeps the limactl primitives LimaBackend needs
```

`lib/vm_spec.py` and `lib/config.py` are untouched (phase 1 delivered them). The
`Target`/`ComposedSpec` resolution, borrow logic, staleness gates, and the
progress-pipeline framework in `vrg_vm.py` stay as-is — they are already
backend-neutral.

### The two interfaces

**`Transport`** — the seam between `limactl` and `ssh`. Three operations, because
that is all the guest-side code needs:

```
Transport:
  run(*args, workdir=...) -> CompletedProcess   # capture output (queries, cat-into-file writes)
  pipe(cmd, input_data, workdir=...) -> None     # stream a payload into the guest (cred files)
  exec_session(workdir, inner) -> NoReturn       # os.execvp into an interactive session
```

- `LimaTransport(instance)` → `limactl shell --workdir … <instance> -- …`, and
  `limactl shell --start …` for the session exec.
- `SshTransport(host, user, key_path, known_hosts_path)` → `ssh -i key … user@host …`,
  and `ssh -t …` for the session exec.

Everything in `vm_guest.py` takes a `Transport` and never names `limactl` or `ssh`.
This is the no-drift guarantee for credential and tooling injection.

**`Backend`** — owns the lifecycle, which genuinely differs:

```
Backend:
  provider_label -> str                  # "local" | "gcp" | "azure"  (list BACKEND column)
  transport(target) -> Transport
  status(target) -> str                  # "Running" | "Stopped" | ""
  create_stages(state) -> list[Stage]    # backend-specific build pipeline
  start_stages(state) -> list[Stage]
  rebuild_stages(state) -> list[Stage]
  stop(target) / destroy(target) -> int
  list_contribution(...) -> rows         # for vrg-vm list
```

### How stages compose

The pipeline framework (`progress.run_pipeline`, `Stage`) stays. The *lifecycle*
stages are backend-owned; the *guest* stages are shared:

- Lima create lifecycle: `fetch-template → create → start`.
- Cloud create lifecycle: `fetch-modules → tofu-volume → tofu-vm → await-readiness`.
- Shared guest stages (both paths): `credentials`, `tooling`, `copy-config`,
  `spec-check` — the **same `vm_guest` functions** run over `backend.transport(target)`.

So a cloud create is `[cloud lifecycle stages] + [shared guest stages over
SshTransport]`; a Lima create is `[lima lifecycle stages] + [the same shared guest
stages over LimaTransport]`. Create/destroy diverge where they must; credential and
tooling code is provably identical.

## `tofu` invocation and two-state management

The `OffPlatformBackend` lifecycle core.

### Module fetch

`fetch_modules(tag)` downloads `opentofu-modules-<tag>.tar.gz` — a single
unauthenticated HTTP GET of a vergil-vm **release asset** — and extracts it to a temp
dir, cleaned up after the run. This mirrors `fetch_template` (which GETs a single
`agent.yaml`); the only difference is a tarball (a directory tree) instead of one
file. The `<tag>` is the **same vergil-vm tag `vrg-vm` already resolves** for the
Lima template (`resolve_vm_tag`), keeping the Lima template and cloud modules
version-locked.

### State location (on the Mac, per repo)

State is keyed by the instance name (the existing `<identity>.<org>.<repo>` name,
which doubles as the state key per the vergil-vm spec):

```
~/.config/vergil/tofu/<instance-name>/<provider>/
  volume.tfstate   # precious — proves the disk exists; recoverable via tofu import from labels
  vm.tfstate       # disposable — the VM is ephemeral
~/.config/vergil/tofu/plugin-cache/   # TF_PLUGIN_CACHE_DIR — providers download once, not per-create
```

A lost `vm.tfstate` is harmless (just `create` again); a lost `volume.tfstate` is
recoverable by `tofu import` against the label-matched volume. No remote-bucket
bootstrap, no always-on state cost — matches the fleet-of-one deployment.

### Two states, two invocations

Each module is a valid `tofu` root; we run `tofu` against the extracted module dir
with an explicit `-state`, using the module's committed `.terraform.lock.hcl` for
provider pinning:

1. **`tofu-volume`** — `tofu -chdir=<modules>/<provider>/volume init` then `apply
   -state=…/volume.tfstate` with `name`, `region`, `size_gib` (= `_gib(spec.volume)`).
   Idempotent — a no-op if the disk exists. `tofu output -json` → read `volume_id`
   and **`zone`** (the zone the disk actually landed in).
2. **`tofu-vm`** — `tofu -chdir=<modules>/<provider>/vm init` then `apply
   -state=…/vm.tfstate` pinned to that `zone` and `volume_id`, plus
   `instance_type=spec.instance`, `nested=spec.nested`, `ssh_user`, `ssh_public_key`,
   `ssh_source_ranges`, `provision_env`, `labels`. `tofu output -json` → read `host`
   and `ssh_user`.

A zonal block volume only attaches to an instance in its zone, so the volume owns its
zone and the VM follows it (the vergil-vm contract). `region` stays the human-facing
knob.

### `provision.env` rendered from the shared param set

Today `create_vm` passes `EXTRA_PACKAGES`/`APT_REPOS`/`VAGRANT_PLUGINS`/`NESTED_VIRT`/
`PORT_FORWARDS`/`SPEC_FINGERPRINT` as `--set=.param.*` to Lima. That assembly is
extracted into one shared function: Lima keeps feeding it to `--set`; cloud renders it
as the `provision.env` body passed as the `provision_env` module variable. Same inputs
→ same box on either backend (the vergil-vm "one provisioning truth").

### Readiness, and verb mechanics

- **`await-readiness`** — `cloud-init status --wait` over SSH, then confirm the
  fingerprint marker exists. A non-zero status or missing marker is a **hard `create`
  failure** (no half-ready box), mirroring Lima refusing to mark a half-provisioned
  box ready.
- **`destroy`** → `tofu -chdir=…/vm destroy -state=…/vm.tfstate`. Volume state is
  structurally never in scope.
- **`rebuild`** → destroy vm + apply vm against the existing volume (data survives).
- **`destroy-volume`** → the only path touching `volume.tfstate`; guarded by explicit
  confirmation; never implied by `destroy`.
- **`update`** → maps to `rebuild`.
- **Preflight** (before any cloud verb): `tofu` present and at least the version the
  modules declare (`required_version >= 1.8.0` today), else a clear "install OpenTofu
  >= 1.8.0" remediation — never an opaque stack trace.

## SSH transport, keypair, and ingress

### Managed SSH keypair (per repo, alongside the tofu state)

```
~/.config/vergil/tofu/<instance-name>/<provider>/ssh/{id_ed25519, id_ed25519.pub}   # mode 600
```

Generated on first `create` if absent; the public key feeds the vm module's
`ssh_public_key`; the private key is what `SshTransport` connects with. Reused across
`stop`/`start`/`session`; never written to the persistent volume. This transport key
is distinct from the GitHub App key (Credentials).

### Ingress allow-list = the operator's current public IP

The vm module rejects an empty list and `0.0.0.0/0` (enforced in its variable
validation). At `create` time `vrg-vm` resolves the Mac's current public address via
an IP-echo HTTP GET, passes it as a `/32` (IPv4, plus IPv6 if present), and
**re-applies it on every `create`** so the allow-list tracks wherever the operator is
working from — no hand-maintained CIDR. If the address cannot be resolved, `create`
**fails loudly**; there is never a wildcard fallback. sshd is key-only (enforced by
the module / provisioning).

### Host-key handling

`SshTransport` uses a per-instance `UserKnownHostsFile` under the state dir with
`StrictHostKeyChecking=accept-new` — trust-on-first-use, acceptable because the box
was just stood up by us, behind an IP allow-list, with key-only auth. The residual
first-connect MITM seam routes to the strategic security-boundary register
([#1369](https://github.com/vergil-project/vergil-tooling/issues/1369)); a
provider-native bastion/tunnel (GCP IAP, Azure Bastion) is the logged hardening path,
future work because it cuts against the provider-agnostic interface.

### `session`: same inner command, ssh outer

`_cmd_session` builds an in-guest `inner` resolver command (`vrg-vm-resolve-session
…`) and execs it. The `inner` string is byte-identical for cloud (it runs in-guest);
only the outer wrapper changes from `limactl shell --start …` to `ssh -t -i key -o …
user@host "cd <workdir> && bash -c inner"`. Terminal env (`TERM`, `COLORTERM`, …)
rides `ssh SendEnv` into the sshd `AcceptEnv` the provisioning configures, replacing
the `LIMA_SHELLENV_ALLOW` path.

## Credentials

Two flows, neither ever on the persistent volume.

### Flow A — cloud provider creds (for `tofu`), on the Mac only

`tofu` sources GCP creds from the SDK default chain (ADC /
`GOOGLE_APPLICATION_CREDENTIALS`); `vrg-vm` passes the ambient environment through to
the `tofu` subprocess. Never written to the repo, profile, or a tfvars file. A cloud
verb preflights that ADC is present and fails with a clear remediation (`gcloud auth
application-default login`) rather than an opaque `tofu` error. This parallels the
existing per-identity GitHub App cred selection.

### Flow B — GitHub App + Claude creds, into the VM over SSH

`vm_guest.inject_credentials` run over `SshTransport` instead of `LimaTransport` —
identical logic (app.pem, app.env, identity-mode, git HTTPS rewrite, Claude token),
re-run on every `create`.

### Placement rule (closes a seam the vergil-vm spec only spelled out for the App key)

Every injected secret lands on the **ephemeral boot disk** (the home dir,
`~/.config/vergil/…` and `~/.claude/.credentials.json`), so `destroy` — which deletes
the boot disk — destroys them. The persistent **volume** carries only what must
survive teardown: the repo checkout and Claude **session history**. Mirroring the
existing `link_claude_dirs` pattern, `~/.claude` is a real directory on the boot disk,
and only the history subdirs (`projects/`, `todos/`) are symlinked onto
`/vergil/claude/`. Result:

- App private key → boot disk → dies with VM. (vergil-vm spec requirement.)
- Claude OAuth token (`.credentials.json`) → boot disk → dies with VM, re-injected
  next `create`.
- Session transcripts/history → volume → survive teardown.

No injected credential — App key *or* Claude token — ever persists on the detachable
volume, while session continuity is preserved.

## Persistent volume: bootstrap vs reattach

A backend-layer step that runs after provisioning + cred injection, only on the cloud
path. The vergil-vm module owns format-on-first-use + mount at `/vergil`; the tooling
owns the checkout. The signal is whether `/vergil/projects/<org>/<repo>` already holds
a checkout:

- **Fresh volume** → `vrg-git clone` in-guest over SSH (using the just-injected App
  creds) into `/vergil/projects/<org>/<repo>`; seed an empty `/vergil/claude/`.
- **Reattach** → detect the checkout, `git fetch` to surface drift, **do not clone**,
  leave working state intact.

### Session workspace differs from Lima

Lima resolves the workdir against the Mac's `projects_dir` (the shared mount). Cloud
has no mount, so the cloud `session` workdir is the on-volume path
`/vergil/projects/<org>/<repo>`; the in-guest resolver command is otherwise unchanged.

## `vrg-vm list` and the remaining verbs

- **`list`** gains a **BACKEND** column (`local`/`gcp`). Off-platform rows render from
  local state + profile; with ADC present, status/occupancy is queried (occupancy via
  the in-guest probe over SSH). Without creds, the row shows `unknown (no <provider>
  creds)` — `list` never errors and never hides a possibly-running row.
- **`stop`/`start`** → provider deallocate/start (pause billing, keep the box) — the
  "overnight long test" affordance. `destroy` remains the default daily cadence.
- **`destroy-volume`** → new guarded verb, the only path touching `volume.tfstate`;
  explicit confirmation required.
- **`update`** → `rebuild`.
- **Concurrency** stays "multiple sessions on one VM." The dispatcher refuses to stand
  up a second instance for an `(identity, org/repo)` that already has a running one.
- **Under-provisioning warning at `session`/`start`** (deferred to this slice by PR
  #1715's reviewer notes). On cloud, the chosen `instance` type is authoritative over
  `cpus`/`memory` — you cannot ask a cloud for "12.5 vCPU". Those scalars stay in the
  spec as human-readable intent: if the resolved `instance` is smaller than the
  declared `cpus`/`memory`, `session`/`start` warns loudly (the #99 override-floor
  pattern, alongside the existing host-override `_warn_under`) rather than silently
  running undersized. Mapping `instance` → real vCPU/memory needs the provider's
  catalog; the first cut warns from a small built-in table of known nested-virt
  instance types and stays silent (never falsely warns) for an unknown type.

## Cross-repo dependency (lands first)

A small **vergil-vm PR**:

1. A release build step that tars the `opentofu/` subtree (modules + `interface.json`)
   into `opentofu-modules-$VERSION.tar.gz` — hung on vergil-vm's existing
   `scripts/build*.sh` patterns.
2. One line in vergil-vm's `cd.yml`: pass `release-artifacts:
   opentofu-modules-$VERSION.tar.gz` to the existing `cd-release` call (the shared
   `cd-release.yml` already supports `release-artifacts`, and `tag-and-release`
   already runs `gh release create` with attached assets — verified).

vergil-vm then cuts a release so the asset URL exists. The big vergil-tooling dispatch
PR consumes it at that tag.

## Testing (vergil-tooling side)

Unit tests with `tofu` and `ssh` mocked at the subprocess boundary:

- `select_backend` dispatch by composed `backend`.
- State-path derivation and the two-state call ordering (volume before vm; vm pinned
  to the volume's zone/`volume_id`).
- `provision.env` rendering parity with the Lima `--set=.param.*` set (same inputs →
  same body).
- Ingress fail-closed (no resolvable IP → `create` aborts; never `0.0.0.0/0`).
- Reattach-vs-clone branch on the checkout-present signal.
- Credential placement: injected secrets target boot-disk paths; only history subdirs
  are linked to the volume.
- `list` graceful degradation when creds are absent.
- `tofu` preflight (absent / below floor → clear remediation).
- Lima regression: the existing `vrg-vm`/`lima` tests stay green; the Lima behavior is
  unchanged after the `Transport` refactor.

No real cloud in CI. The gated, money-spending e2e (`driver=kvm` resolves, volume
survives `destroy`+`create`) lives in vergil-vm, already specified there.

## Acceptance criteria

1. `vrg-vm create/session/stop/start/destroy/rebuild` work against a GCP nested-virt
   instance via the OpenTofu modules, dispatched on the composed `backend`.
2. The persistent volume survives `destroy`; a later `create`/`rebuild` reattaches it
   with the repo checkout + Claude history intact; `volume.tfstate` is never in
   `destroy`'s scope.
3. The local Lima default is unaffected — a repo with no `backend` key behaves
   identically; the Lima path's behavior is unchanged after the `Transport` refactor.
4. No injected credential (App key or Claude token) persists on the volume.
5. `vrg-vm list` shows a BACKEND column and degrades visibly (never errors, never
   hides a row) when provider creds are absent.
6. `destroy-volume` is the only path that deletes the persistent disk, and requires
   explicit confirmation.
7. The vergil-vm module tarball is published as a release asset and `vrg-vm` fetches
   it at the resolved tag with a single unauthenticated GET.

## Related

- **Cross-repo contract:** vergil-vm #199 and its design spec
  (`docs/specs/2026-06-19-off-platform-vm-backend-design.md`).
- **Phase 1 (schema):** vergil-tooling PR #1715.
- **Evidence:** mq-cluster-tooling #289 (TCG Emulation Tax), #276, #283.
- **Security register:** vergil-tooling #1369.
- **Adjacent:** vergil-tooling #1705 (credential-less VM identity).
