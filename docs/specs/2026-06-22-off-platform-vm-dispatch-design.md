# Off-Platform VM Backend Dispatch (vergil-tooling slice) Design

**Issues:**

- [vergil-tooling #1706 — `vrg-*` wrapper + credential/profile conventions for remote cloud session hosts](https://github.com/vergil-project/vergil-tooling/issues/1706)

**Date:** 2026-06-22

**Status:** Design (from brainstorming, 2026-06-22; revised after a paad:pushback review
the same day — see "Pushback resolutions").

**Spans two repositories.** This is the `vergil-tooling` companion to
[vergil-vm #199](https://github.com/vergil-project/vergil-vm/issues/199). The
authoritative cross-repo contract — OpenTofu module interface, two-state lifecycle,
provisioning model, credential flows, security boundary — lives in vergil-vm's
[`docs/specs/2026-06-19-off-platform-vm-backend-design.md`](https://github.com/vergil-project/vergil-vm/blob/develop/docs/specs/2026-06-19-off-platform-vm-backend-design.md).
That spec specifies *what* the tooling does at the interface level and explicitly
defers *how* — "the mechanism is the companion's" — to this document. This spec does
not re-litigate decided behavior; it specifies the `vrg-vm` dispatcher mechanism. The
pushback review (below) surfaced two areas where the mechanism the spec settled on
requires a **vergil-vm module change** (IAP-based access; removing the volume's
`prevent_destroy`); those are called out as cross-repo prerequisites.

## Background: what is already done

- **vergil-vm side (merged, released in 2.1.27, commit `b75610b`).** Backend-neutral
  provisioning scripts (`templates/provision/*.sh`), the GCP `volume` + `vm` OpenTofu
  modules implementing the provider-agnostic `opentofu/interface.json` contract,
  cloud-init that synthesizes readiness, and the module tests. Azure (phase 3) is not
  built; that is a vergil-vm concern, not this slice's. **Note:** the merged GCP `vm`
  module exposes SSH over a public IP behind an operator-IP allow-list; the IAP
  decision below supersedes that surface and is a vergil-vm prerequisite (see
  "Connectivity & access" and "Cross-repo prerequisites").
- **vergil-tooling side (phase 1, merged in PR #1715).** The profile *schema*:
  `backend`/`provider`/`region`/`instance`/`volume` parsing in `lib/config.py`,
  composition through the full five-tier cascade in `lib/vm_spec.py`, the closed
  backend enum and off-platform required-key validation (`SpecError`), and the
  fingerprint fold that leaves local Lima profiles byte-for-byte unchanged. This
  layer is executable but inert — nothing reads `ComposedSpec.off_platform` yet.

## Problem

`vrg-vm` can read an off-platform profile but cannot stand up a cloud VM. The CLI
(`bin/vrg_vm.py`) imports ~25 functions directly from `lib/lima.py` and drives every
verb through `limactl`. Issue #1706's remaining scope — backend dispatch, `tofu`
invocation and state management, the in-VM session transport, credential injection on
a remote peer, and the `list`/`destroy-volume`/`update` verb changes — has no
implementing code.

## Goals

- **Same verbs, second backend.** `vrg-vm create/destroy/rebuild/session/list` work
  for an off-platform VM exactly as for Lima, dispatched on the composed `backend`.
- **One provisioning truth, no credential drift.** The guest-side credential and
  tooling logic is written once and runs over either transport (`limactl` or the IAP
  tunnel), realizing the vergil-vm spec's "cred injection logic is unchanged — only
  the transport changes" as a structural property rather than copy-paste.
- **Local Lima default unaffected.** A repo with no `backend` key behaves identically;
  the Lima code path is unchanged in behavior.
- **No credential ever persists on the detachable volume.** Both the GitHub App
  private key and the Claude OAuth token live on the ephemeral boot disk and die with
  the VM; only the repo checkout and session history persist on the volume.
- **No public attack surface on the box.** Access is via GCP IAP — no public IP, no
  operator-IP allow-list, no third-party IP discovery (see "Connectivity & access").

## Non-goals

- The OpenTofu modules, provisioning scripts, cloud-init, and the gated real-cloud
  e2e test — all owned by vergil-vm.
- Azure — a vergil-vm phase-3 module add; the lifecycle/module interface is
  provider-blind, so it requires no tooling dispatch change. Azure's *transport* (a
  Bastion analog of IAP) is a tracked follow-up.
- An auto-reaper or billing surface in `list` — rejected as over-machinery in the
  vergil-vm spec (fleet-of-one); the cost guard is documented teardown discipline.
- An in-place tooling-refresh path for off-platform — `update` maps to `rebuild`.
- **`stop`/`start` for off-platform v1** — dropped (see "Pushback resolutions"); power
  state has no OpenTofu mechanism today. Tracked as a follow-up.

## Delivery

Two vergil-tooling PRs under #1706, both implemented now:

1. **Refactor PR (de-risking, lands first).** Introduce `Backend` + `Transport`,
   move `vrg_vm.py` onto a dispatched `Backend`, and reshape the lima guest helpers to
   take a `Transport`. **Lima-only, zero behavior change**, guarded by the existing
   test suite — the direct analog of vergil-vm's phase-1 provisioning extraction,
   shipped separately to de-risk and keep the cloud diff reviewable.
2. **Cloud dispatch PR.** `OffPlatformBackend`: tofu two-state lifecycle, IAP
   transport, credential injection, volume bootstrap, and the `list`/`destroy-volume`/
   `update` verb changes.

Both depend on the **vergil-vm prerequisites** (below) landing and cutting a release
first, because the module tarball and the IAP module changes must exist before the
cloud PR can consume them.

## Cross-repo prerequisites (vergil-vm, land + release first)

1. **Publish the module tree as a release asset.** A release build step tars the
   `opentofu/` subtree into `opentofu-modules-$VERSION.tar.gz`, and one line in
   vergil-vm's `cd.yml` passes `release-artifacts: opentofu-modules-$VERSION.tar.gz`
   to the existing `cd-release` call (the shared `cd-release.yml` already supports
   `release-artifacts`; `tag-and-release` already runs `gh release create` with
   attachments — verified).
2. **IAP module redesign (GCP `vm` module).** Drop the ephemeral public IP
   (`access_config`), set the SSH firewall to Google's fixed IAP range
   (`35.235.240.0/20`) instead of an operator IP, and retire `ssh_public_key` /
   `ssh_source_ranges` (and rework `ssh_user` / the `host` output) — an
   `interface.json` contract change Azure's future module mirrors via Bastion.
   Precondition: the IAP API enabled and `roles/iap.tunnelResourceAccessor` granted
   (verify against the #204 account setup).
3. **Remove `prevent_destroy` from the volume disk.** It blocks the legitimate
   `destroy-volume` verb and cannot be conditionalized; the two-state separation plus
   the confirmation-gated verb are the real guard (see "Verb mechanics").

Items 2 and 3 are sizeable enough to warrant their own vergil-vm issues (filed as
follow-ups).

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
                     IapTransport  (wraps gcloud compute ssh --tunnel-through-iap)
  vm_guest.py      guest-side steps written ONCE, each takes a Transport:
                     inject_credentials, install/update_tooling,
                     copy/link_claude_config, get_tooling_version,
                     vm_probe, read_fingerprint, vm_spec_status
  vm_lima.py       LimaBackend  — limactl lifecycle, uses LimaTransport
  vm_cloud.py      OffPlatformBackend — tofu+IAP lifecycle, uses IapTransport
  lima.py          trimmed: keeps the limactl primitives LimaBackend needs
```

`lib/vm_spec.py` and `lib/config.py` are untouched (phase 1 delivered them). The
`Target`/`ComposedSpec` resolution, borrow logic, staleness gates, and the
progress-pipeline framework in `vrg_vm.py` stay as-is — they are already
backend-neutral.

### The two interfaces

**`Transport`** — the seam between `limactl` and the IAP tunnel. Three operations,
because that is all the guest-side code needs:

```
Transport:
  run(*args, workdir=...) -> CompletedProcess   # capture output (queries, cat-into-file writes)
  pipe(cmd, input_data, workdir=...) -> None     # stream a payload into the guest (cred files)
  exec_session(workdir, inner) -> NoReturn       # os.execvp into an interactive session
```

- `LimaTransport(instance)` → `limactl shell --workdir … <instance> -- …`, and
  `limactl shell --start …` for the session exec.
- `IapTransport(instance, zone, project)` → `gcloud compute ssh <instance>
  --tunnel-through-iap --zone … -- …`, and the `-t` form for the session exec. gcloud
  provisions ephemeral SSH keys and manages host keys, so the tooling manages no
  keypair or `known_hosts` of its own.

Everything in `vm_guest.py` takes a `Transport` and never names `limactl` or gcloud.
This is the no-drift guarantee for credential and tooling injection.

**`Backend`** — owns the lifecycle, which genuinely differs:

```
Backend:
  provider_label -> str                  # "local" | "gcp"  (list BACKEND column)
  transport(target) -> Transport
  status(target) -> str                  # "Running" | "Stopped" | ""
  create_stages(state) -> list[Stage]    # backend-specific build pipeline
  rebuild_stages(state) -> list[Stage]
  destroy(target) -> int
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
IapTransport]`; a Lima create is `[lima lifecycle stages] + [the same shared guest
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

### Cloud resource naming

The canonical instance name (`<identity>.<org>.<repo>`, dot-joined because Lima
requires it) is **not** a valid GCP resource name (GCP names are RFC1035: lowercase,
hyphen-only, ≤63 chars — no dots). So the two uses are separated:

- The **local state-dir key** uses the canonical dotted name (dots are fine in a
  directory name).
- The module's `name` variable is a **derived cloud-safe name**: lowercase the
  identity/org/repo, map non-alphanumerics to `-`, ensure a leading letter, and when
  it would exceed the limit (the firewall appends `-ssh`, so budget ≤59), truncate
  with a short deterministic hash suffix for uniqueness.
- Recovery does **not** depend on the mangled name: the dispatcher populates the
  module's `labels` map with structured `vergil-identity` / `vergil-org` /
  `vergil-repo`, so a lost `volume.tfstate` is re-importable by label match (the
  vergil-vm spec's intent).

### State location (on the Mac, per repo)

State is keyed by the canonical instance name:

```
~/.config/vergil/tofu/<instance-name>/<provider>/
  volume.tfstate   # precious — proves the disk exists; recoverable via tofu import from labels
  vm.tfstate       # disposable — the VM is ephemeral
~/.config/vergil/tofu/plugin-cache/   # TF_PLUGIN_CACHE_DIR — providers download once, not per-create
```

A lost `vm.tfstate` is harmless (just `create` again); a lost `volume.tfstate` is
recoverable by `tofu import` against the label-matched volume. No remote-bucket
bootstrap, no always-on state cost — matches the fleet-of-one deployment. Concurrent
`vrg-vm` runs on the same state are serialized by `tofu`'s own local-state lock
(`.tflock`); the dispatcher surfaces a lock error clearly rather than adding its own.

### Two states, two invocations

Each module is a valid `tofu` root; we run `tofu` against the extracted module dir
with an explicit `-state`, using the module's committed `.terraform.lock.hcl` for
provider pinning. All runs are **non-interactive and streamed**: `-input=false` and
`TF_IN_AUTOMATION=1` on every run, `-auto-approve` on `apply`/`destroy`, stdout/stderr
streamed through the existing progress framework (as `limactl` output is), and
`TF_PLUGIN_CACHE_DIR` set so providers download once. Any non-zero `tofu` exit fails
the stage loudly with `tofu`'s stderr (no-silent-failures); a partial apply is
reconciled by the next run since `apply` is idempotent. `create` carries a `--timeout`
(like Lima's) bounding the apply + readiness poll.

1. **`tofu-volume`** — `tofu -chdir=<modules>/<provider>/volume init` then `apply
   -state=…/volume.tfstate` with `name`, `region`, `size_gib` (= `_gib(spec.volume)`),
   `labels`. Idempotent — a no-op if the disk exists. `tofu output -json` → read
   `volume_id` and **`zone`** (the zone the disk actually landed in).
2. **`tofu-vm`** — `tofu -chdir=<modules>/<provider>/vm init` then `apply
   -state=…/vm.tfstate` pinned to that `zone` and `volume_id`, plus
   `instance_type=spec.instance`, `nested=spec.nested`, `provision_env`, `labels`.
   `tofu output -json` → read the instance identifier the IAP transport connects to.

A zonal block volume only attaches to an instance in its zone, so the volume owns its
zone and the VM follows it (the vergil-vm contract). `region` stays the human-facing
knob.

### `provision.env` rendered from the shared param set

Today `create_vm` passes `EXTRA_PACKAGES`/`APT_REPOS`/`VAGRANT_PLUGINS`/`NESTED_VIRT`/
`PORT_FORWARDS`/`SPEC_FINGERPRINT` as `--set=.param.*` to Lima. That assembly is
extracted into one shared function: Lima keeps feeding it to `--set`; cloud renders it
as the `provision.env` body passed as the `provision_env` module variable. Same inputs
→ same box on either backend (the vergil-vm "one provisioning truth").

### Readiness and verb mechanics

- **`await-readiness`** — `cloud-init status --wait` over the IAP transport, then
  confirm the fingerprint marker exists. A non-zero status or missing marker is a
  **hard `create` failure** (no half-ready box), mirroring Lima.
- **`destroy`** → `tofu -chdir=…/vm destroy -state=…/vm.tfstate`. Volume state is
  structurally never in scope.
- **`rebuild`** → destroy vm + apply vm against the existing volume (data survives).
- **`destroy-volume`** → the only path touching `volume.tfstate`; guarded by explicit
  confirmation; never implied by `destroy`. After the disk is destroyed, removes the
  local `volume.tfstate` and the per-repo state dir (once empty) so a later `create`
  starts clean. Depends on the vergil-vm `prevent_destroy` removal (prerequisite #3).
- **`update`** → maps to `rebuild`.
- **`stop`/`start`** → **not supported in v1** (no OpenTofu power-state mechanism). The
  verbs return a clear "off-platform VMs are ephemeral — use `destroy`/`create`;
  pause-overnight isn't supported yet" message. Tracked as a follow-up (a
  `desired_status` module variable).
- **Preflight** (before any cloud verb): `tofu` present and at least the modules'
  declared `required_version` (`>= 1.8.0` today), and `gcloud` present with ADC
  available — else a clear remediation (`install OpenTofu >= 1.8.0`; `gcloud auth
  application-default login`) rather than an opaque stack trace.

## Connectivity & access (IAP)

Access to the box is via **GCP Identity-Aware Proxy (IAP) TCP tunneling**, not a
public SSH port. This is the mechanism the vergil-vm spec named as the hardening path;
the pushback review promoted it to the primary because the alternative (bake the
operator's public IP into a firewall rule) is unsound behind NAT — a host cannot
self-detect its public IP without an external oracle, and pulling in a third-party
IP-echo service is an unacceptable dependency in a security-critical, create-blocking
path.

- **No public IP on the instance.** IAP tunnels to the instance's internal IP through
  Google's backbone; the module drops `access_config`.
- **Firewall allows SSH only from `35.235.240.0/20`** (Google's fixed IAP range) — a
  module constant, not an operator address. There is no operator-IP allow-list and
  nothing to refresh when the operator roams.
- **Auth is the operator's existing GCP IAM / ADC** — the same credentials `tofu`
  uses; `roles/iap.tunnelResourceAccessor` gates tunnel access. No Vergil-managed SSH
  keypair (gcloud provisions ephemeral keys).
- **Transport is GCP-specific, by design absorbed in the `Transport` layer.** The
  lifecycle/module interface stays provider-blind; only the connection method differs.
  Azure's symmetric answer (Bastion) is a tracked follow-up when the Azure module
  lands.
- **Residual seam:** a credentialed VM reachable by anyone holding the IAM role — a
  tighter boundary than the public-IP design it replaces. Logged in the strategic
  security-boundary register
  ([#1369](https://github.com/vergil-project/vergil-tooling/issues/1369)).

### `session`: same inner command, IAP outer

`_cmd_session` builds an in-guest `inner` resolver command (`vrg-vm-resolve-session
…`) and execs it. The `inner` string is byte-identical for cloud (it runs in-guest);
only the outer wrapper changes from `limactl shell --start …` to `gcloud compute ssh
<instance> --tunnel-through-iap --zone … -- "cd <workdir> && bash -c inner"`. Terminal
env rides into the sshd `AcceptEnv` the provisioning configures.

## Credentials

Two flows, neither ever on the persistent volume.

### Flow A — cloud provider creds (for `tofu` and IAP), on the Mac only

`tofu` and `gcloud`/IAP source GCP creds from the SDK default chain (ADC /
`GOOGLE_APPLICATION_CREDENTIALS`); `vrg-vm` passes the ambient environment through to
the subprocesses. Never written to the repo, profile, or a tfvars file. A cloud verb
preflights that ADC is present and fails with a clear remediation (`gcloud auth
application-default login`).

### Flow B — GitHub App + Claude creds, into the VM over the transport

`vm_guest.inject_credentials` run over `IapTransport` instead of `LimaTransport` —
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

- **Fresh volume** → `vrg-git clone` in-guest over the transport (using the
  just-injected App creds) into `/vergil/projects/<org>/<repo>`; seed an empty
  `/vergil/claude/`.
- **Reattach** → detect the checkout, `git fetch` to surface drift, **do not clone**,
  leave working state intact.
- **Credential-less identity (`auth_type="none"`, #1707)** → **skip the checkout**,
  logging "skipping checkout (credential-less identity)" — mirroring how
  `inject_credentials` already skips. The box still stands up with an empty volume
  (the legitimate no-git bootstrap-test case from #1705); no doomed clone, no silent
  failure.

### Session workspace differs from Lima

Lima resolves the workdir against the Mac's `projects_dir` (the shared mount). Cloud
has no mount, so the cloud `session` workdir is the on-volume path
`/vergil/projects/<org>/<repo>`; the in-guest resolver command is otherwise unchanged.

## `vrg-vm list` and the remaining verbs

- **`list`** gains a **BACKEND** column (`local`/`gcp`). Off-platform rows render from
  local state + profile; with ADC present, status/occupancy is queried (occupancy via
  the in-guest probe over the transport). Without creds, the row shows `unknown (no
  <provider> creds)` — `list` never errors and never hides a possibly-running row.
- **`destroy-volume`** → new guarded verb (see "Verb mechanics").
- **`update`** → `rebuild`.
- **`stop`/`start`** → not supported in v1 (clear message; follow-up tracked).
- **Concurrency** stays "multiple sessions on one VM." The dispatcher refuses to stand
  up a second instance for an `(identity, org/repo)` that already has a running one.
- **Under-provisioning warning at `session`** (deferred to this slice by PR #1715's
  reviewer notes). On cloud the chosen `instance` type is authoritative over
  `cpus`/`memory`; those scalars stay as human-readable intent. If the resolved
  `instance` is smaller than the declared `cpus`/`memory`, `session` warns loudly
  (the #99 override-floor pattern, alongside the existing `_warn_under`). Mapping
  `instance` → real vCPU/memory uses a small built-in table of known nested-virt
  instance types and stays **silent on an unknown type** (never a false warning);
  the table's drift is accepted as a cheap, advisory-only cost.

## Testing (vergil-tooling side)

Unit tests with `tofu` and `gcloud` mocked at the subprocess boundary:

- `select_backend` dispatch by composed `backend`.
- Cloud-safe name derivation (dots → hyphens, lowercase, length cap + hash suffix) and
  structured-label population.
- State-path derivation and the two-state call ordering (volume before vm; vm pinned
  to the volume's zone/`volume_id`).
- `provision.env` rendering parity with the Lima `--set=.param.*` set (same inputs →
  same body).
- Reattach-vs-clone branch, including the credential-less skip.
- Credential placement: injected secrets target boot-disk paths; only history subdirs
  are linked to the volume.
- `list` graceful degradation when creds are absent.
- `tofu`/`gcloud` preflight (absent / below floor → clear remediation) and the
  non-interactive flag set.
- `stop`/`start` return the clear unsupported message.
- Lima regression: the existing `vrg-vm`/`lima` tests stay green; the Lima behavior is
  unchanged after the `Transport` refactor (the refactor PR's acceptance bar).

No real cloud in CI. The gated, money-spending e2e (`driver=kvm` resolves, volume
survives `destroy`+`create`) lives in vergil-vm, already specified there.

## Acceptance criteria

1. `vrg-vm create/session/destroy/rebuild` work against a GCP nested-virt instance via
   the OpenTofu modules, dispatched on the composed `backend`, with access via IAP (no
   public IP).
2. The persistent volume survives `destroy`; a later `create`/`rebuild` reattaches it
   with the repo checkout + Claude history intact; `volume.tfstate` is never in
   `destroy`'s scope.
3. The local Lima default is unaffected — a repo with no `backend` key behaves
   identically; the Lima path's behavior is unchanged after the `Transport` refactor.
4. No injected credential (App key or Claude token) persists on the volume.
5. `vrg-vm list` shows a BACKEND column and degrades visibly (never errors, never
   hides a row) when provider creds are absent.
6. `destroy-volume` is the only path that deletes the persistent disk, requires
   explicit confirmation, and cleans up local state.
7. The vergil-vm module tarball is published as a release asset and `vrg-vm` fetches
   it at the resolved tag with a single unauthenticated GET.
8. `stop`/`start` on an off-platform repo return a clear unsupported message (not a
   crash or a silent no-op).

## Pushback resolutions (2026-06-22)

A paad:pushback review ran against the first draft. No source-control conflicts. One
structural finding and seven issues, all folded into the body above:

1. **Scope (size).** The `Backend`+`Transport` refactor is independently shippable and
   de-risking → split into a first Lima-only refactor PR (see "Delivery").
2. **GCP resource naming (serious, feasibility).** The dotted instance name is not a
   valid GCP name → derive a cloud-safe `name` + structured labels; canonical name is
   the local state key only (see "Cloud resource naming").
3. **`stop`/`start` (serious, feasibility).** No OpenTofu power-state mechanism →
   dropped from v1 with a clear message; `desired_status` module variable tracked as a
   follow-up.
4. **`destroy-volume` vs `prevent_destroy` (serious, feasibility).** The guard blocks
   the legitimate verb and can't be conditionalized → vergil-vm removes it; the
   two-state separation + confirmation-gated verb are the guard (prerequisite #3).
5. **Roaming-IP lockout (moderate).** Mooted by the IAP decision (no operator-IP
   allow-list) — the planned `session` self-heal is not needed.
6. **Credential-less identity (moderate, omission).** Off-platform + `auth_type="none"`
   → skip the checkout, logged (see "Persistent volume").
7. **`tofu` non-interactive/streaming (moderate, omission).** Specified the flag set,
   streaming, timeout, and loud-failure behavior (see "Two states, two invocations").
8. **Origin-IP via third-party echo (moderate, security).** Rejected as an unsound
   NAT-bound third-party dependency → replaced wholesale by IAP (see "Connectivity &
   access"), which also removes the public IP and the managed keypair.

Minor items folded in: under-provisioning table (silent on unknown), `tofu` local
state lock for concurrency, local state cleanup on `destroy-volume`.

## Follow-ups (to be filed)

- **vergil-vm: IAP module redesign** — drop public IP, firewall → IAP range, retire
  `ssh_public_key`/`ssh_source_ranges`, rework `ssh_user`/`host` output;
  `interface.json` change. Prerequisite for the cloud PR.
- **vergil-vm: remove `prevent_destroy`** from the volume disk. Prerequisite for
  `destroy-volume`.
- **vergil-vm + vergil-tooling: off-platform `stop`/`start`** via a `desired_status`
  module variable (overnight pause).
- **vergil-tooling: Azure Bastion transport** when the Azure module lands (symmetric
  to IAP).

## Related

- **Cross-repo contract:** vergil-vm #199 and its design spec
  (`docs/specs/2026-06-19-off-platform-vm-backend-design.md`).
- **Phase 1 (schema):** vergil-tooling PR #1715.
- **Evidence:** mq-cluster-tooling #289 (TCG Emulation Tax), #276, #283.
- **Security register:** vergil-tooling #1369.
- **Adjacent:** vergil-tooling #1705 (credential-less VM identity), #1707
  (`auth_type="none"`).
