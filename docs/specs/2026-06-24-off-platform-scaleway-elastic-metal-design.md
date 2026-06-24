# Off-platform: Scaleway Elastic Metal backend + provider-dispatch abstraction

- **Issue:** vergil-tooling#1851
- **Status:** Design (brainstormed, pushback-reviewed, alignment-checked)
- **Related:** #1836 / PR #1841 (nested-virt family fallback on GCP reattach)
- **Coordination:** in-flight **Azure** backend — this spec defines the provider-dispatch seam Azure conforms to (see §2 and §8).

## Problem

GCP on-demand capacity for nested-virtualization-capable machines is structurally
unreliable. Nested virt on GCE is **Intel-only** (n2/c2; AMD excluded — see #1836),
and those families stock out (`ZONE_RESOURCE_POOL_EXHAUSTED`) in popular zones. #1836
added family fallback, but it only spreads the bet across a small, contended pool;
when every Intel family is exhausted there is no recovery, and releasing a box to
rebuild it routinely loses the slot to another tenant.

We need a **reliable, cheap, IaC-provisioned** alternative that does not compete for
scarce cloud VM SKUs.

## Goal

Add a Scaleway **Elastic Metal** off-platform backend:

- **Bare metal ⇒ native KVM.** Nested virt is a non-issue — the agent's KVM guests
  are first-level, no per-SKU policy roulette, `70-nested-virt.sh` passes trivially.
- **The box is assigned to us.** No capacity loss on rebuild; the GCP "release it and
  someone grabs it" failure cannot happen.
- **Cheap and IaC-native.** First-class Terraform provider; ~€150/mo for an
  EM-B230E (AMD EPYC 8C/16T, 128 GB) — roughly 4× cheaper than a GCP reservation.

Introduce the minimal **provider-dispatch abstraction** required to host a second
off-platform provider cleanly, and define it so the concurrent Azure backend
conforms to the same seam rather than inventing a parallel one.

## Non-goals (v1)

- Any change to the GCP backend's behavior (beyond extracting the shared seam).
- The Azure backend itself (parallel work; it consumes the seam defined here).
- Detachable / persistent storage on metal (no two-state model — see §3).
- Other bare-metal providers (Hetzner, Latitude, Vultr) — future, behind the same seam.
- Multi-box fleets / autoscaling on Scaleway.

## Decisions (from brainstorm)

| Axis | Decision |
| --- | --- |
| Integration model | Cloud-IaC (OpenTofu / first-class TF provider), not a hand-managed host. |
| Provider | Scaleway Elastic Metal (EU; geography-flexible, cost/reliability prioritized). |
| Footprint | ~16 threads / ≥64 GB, nested-virt, kept ~24/7. EM-B230E (8C/16T, 128 GB). |
| Lifecycle | **Single-state** — server only; rebuild = OS reinstall on the same box. |
| Transport | **Tailscale/WireGuard overlay** — no public SSH; survives operator roaming. |
| Azure coordination | **Define the provider-dispatch seam here; Azure conforms.** |

## Design

### 1. Lifecycle — single-state (not GCP's two-state)

GCP splits a disposable VM from a surviving persistent disk so the VM can be rebuilt
without data loss. Bare metal cannot: the disk belongs to the server and dies when the
server is released. Since this box runs ~24/7 and its data is reproducible
(blow-away-and-recreate), the Scaleway backend uses a **single resource**:

- **create** → provision one `scaleway_baremetal_server` (offer, zone, OS image, SSH
  key, cloud-init user-data). Provisioning runs `templates/provision/*.sh` unchanged.
- **rebuild** → **OS reinstall on the same allocated server**, driven via the Scaleway
  **install/reinstall API** against the stored server ID — **not** via a Terraform
  `os`/`user_data` change. The platform supports in-place reinstall (a dedicated
  Elastic Metal operation that keeps the allocation), but the `scaleway_baremetal_server`
  Terraform resource's `ForceNew` behavior on `os`/`user_data` is unverified — if those
  are `ForceNew`, a plain `terraform apply` would destroy+recreate and **lose the box**,
  reintroducing the GCP capacity-loss failure. Driving reinstall through the API makes the
  "keep the box, no capacity loss" guarantee independent of provider-version semantics.
  Terraform owns create/destroy; the server ID is persisted in state so rebuild targets it.
  Keeps the box, wipes the disk. *(Build task: confirm the TF `ForceNew` flags and, if
  needed, ensure the resource isn't replaced when the API-driven reinstall changes the OS.)*
- **destroy** → release the server; capacity returns to Scaleway's pool, all state gone.
- Native KVM: the `nested` flag and `70-nested-virt.sh` are satisfied with no config.

### 2. Provider-dispatch abstraction (the seam Azure conforms to)

Today `off-platform` resolves directly to a GCP-specific `OffPlatformBackend`. The seam
lands in two steps across phases (don't abstract a lifecycle interface from a single GCP
example — let the second consumer shape it):

- **Phase 1 — dispatch only.** `spec.provider` already exists; `select_backend` branches on
  it when `spec.off_platform` is true: `"gcp"` → the GCP backend, anything else → an
  explicit "unsupported provider" error. This branch is the one shared edit; Azure/Scaleway
  add their own `provider` case here. (No interface change, no engine change — GCP-safe.)
- **Phase 3 — provider-neutral interface + fingerprint.** When the Scaleway backend lands
  (the second consumer), extract a provider-neutral interface (extending the existing
  `Backend` protocol) for the methods the off-platform engine calls — roughly
  `apply`/`destroy`/`rebuild`/`transport`/`status` plus a `fingerprint_fields()` so each
  backend declares only its own declaration inputs (Scaleway: provider/region/offer; *not*
  the GCP-only `zone`/`volume`/`nested`), so a non-applicable knob never trips a spurious
  rebuild. The exact method set is fixed against two real implementations, not guessed now.

**Phase-1 shared edit:** only `select_backend`'s `provider` branch. That dispatch point — not
the fuller interface — is Azure's sole hard Phase-1 dependency (see §8). The interface
extraction is §9, deferred to Phase 3.

### 3. OpenTofu module — `opentofu/modules/scaleway/server`

A single module using the first-class `scaleway` Terraform provider:

- `scaleway_baremetal_server` (offer = e.g. `EM-B230E-NVMe-128G`, zone = e.g.
  `fr-par-2`, os = an image that supports cloud-init, ssh_key_ids, user-data =
  rendered cloud-init).
- Reuses the existing `templates/provision/*.sh` via the cloud-init `user-data` path
  exactly as the GCP module does — the provisioning scripts are backend-neutral already.
  **Caveat:** Scaleway accepts **`text/plain` user-data only** ([cloud-init concepts](https://www.scaleway.com/en/docs/elastic-metal/concepts/));
  the rendered provision document must be a single text/plain payload (cloud-config is
  normally text/plain, so this is expected to be fine — confirm at build, no multipart MIME).
- The module itself lives in **vergil-vm**, not this repo, and is fetched by v-tag — see §10.
- No volume module, no firewall-for-IAP, no `enable_nested_virtualization`.
- Outputs: the server ID (for API-driven reinstall, §1) and the tailnet hostname/address
  for the transport (see §4).

### 4. Transport — Tailscale/WireGuard overlay

Scaleway metal has a public IP; we deliberately do **not** expose SSH on it.

- At provision, cloud-init installs `tailscaled` and joins the tailnet using an
  **ephemeral, pre-authorized auth key** (injected as a provision param, like
  `SPEC_FINGERPRINT`). The node sets its Tailscale hostname to the deterministic
  cloud resource name, so it has a stable MagicDNS name.
- Public SSH is firewalled off; SSH is reachable **only over the tailnet**.
- A new `TailscaleTransport` (sibling of `IapTransport`) SSHes to the MagicDNS name.
  It needs no IP allow-list and survives operator roaming (identity is the tailnet),
  preserving the GCP IAP security posture (no public attack surface, roaming-proof).
- **Prerequisite:** the operator's machine is on the same tailnet, and an auth key is
  configured (per-identity, alongside the Scaleway token). Documented in setup.

**Readiness probe.** The GCP path polls cloud-init status over IAP; tailnet-only SSH has
a blind window until `tailscaled` is up and joined. So `await-readiness` for Scaleway
waits for the node to appear on the tailnet — via the Tailscale API, or by polling
SSH-over-MagicDNS for the cloud-init readiness/fingerprint marker — on a bounded timeout.
On timeout it fails **loudly**, pointing at the break-glass below; it does not hang or
silently pass.

**Lockout / break-glass.** If `tailscaled` never comes up (expired/invalid key, Tailscale
outage, ACL/tag mistake) the box has no public SSH and is unreachable over the network.
This is **not** a brick: Scaleway provides hardware-level **out-of-band KVM / serial
console** access, which is always available. The readiness-timeout error names it as the
manual recovery path. The tooling does not automate console recovery in v1.

**Auth-key lifecycle & exposure.** Keys are **ephemeral, pre-authorized, tagged, short-TTL,
and minted per provision** (every create *and* every rebuild/reinstall gets a fresh key).
The key is injected via cloud-init user-data, which is readable in Scaleway's instance
metadata/console — a secret surface — so the short TTL + single-use ephemerality keep a
leaked key near-worthless and self-expiring. User-data is **not** treated as a secret store.

### 5. Credentials & configuration

Replacing GCP's ADC/project:

- **Scaleway**: API access key + secret key, project ID, default zone/region — read
  from the existing identity/config mechanism (e.g. `~/.config/vergil/…`), env-var
  overridable, failing loudly if absent (no silent fallback).
- **SSH key**: registered with Scaleway (or referenced by ID) so the server accepts the
  operator key for the initial cloud-init bootstrap before tailnet is up.
- **Tailscale**: an auth key (ephemeral + pre-authorized + tagged) per identity.

### 6. Spec fields & validation

- `provider = "scaleway"`, `region` (Scaleway zone, e.g. `fr-par-2`), `instance` = the
  Elastic Metal offer name. `zone`/`volume`/`nested` are GCP-only and unused here.
- Validate the offer name and zone against the provider at compose/apply time; a typo
  must fail loudly with the list of valid offers, not a cryptic Terraform error.

### 7. Stock-out handling — multi-zone stock check at create

The "assigned box, no churn" reliability win applies to *holding* a server; **acquiring**
one still has to find available stock, and Elastic Metal offers (especially the cheap AMD
class) **do go out of stock per zone**. So the create path must handle this, or the
headline reliability claim is overstated for the first `create` (and any
`destroy`→`create`).

- **Create-time multi-zone stock check.** Elastic Metal exposes per-zone offer
  availability (console/API — confirm the exact endpoint at build). The create path checks
  the target offer across Scaleway zones (`fr-par-1/2`, `nl-ams-1`, `pl-waw-2`), provisions
  in the first available zone, and if none have stock, fails **loudly**: "offer X out of
  stock in all zones — try offer Y or wait." This is the create-time analog of the #1836
  zone/family fallback.
- Once acquired, the box is held (no churn), so this only gates initial acquisition and
  full teardown→recreate — not rebuild (which reinstalls the same held server, §1).

### 8. Coordination with the in-flight Azure backend

Both Azure and this work introduce provider branching. Azure's **only hard Phase-1
dependency is the `select_backend` `provider` dispatch point** (§2, Phase 1) — that's the
single shared edit and the actual collision risk; whoever lands first adds the branch and
the other adds its `provider` case beside it. Azure is a **cloud-VM** provider (it will
likely keep a VM+disk shape and a private-networking transport), so it diverges from
Scaleway's single-state/Tailscale specifics. The **provider-neutral interface itself is not
a Phase-1 artifact** — it's extracted in Phase 3 (§9) once there are two real consumers
(GCP + Scaleway) to shape it; Azure conforms to that interface when it lands relative to
Phase 3, not before. So the only Phase-1 shared file is `select_backend`.

### 9. Engine abstraction — **Phase 3** (not Phase 1)

`vm_cloud.py` is GCP-coupled: `gcloud` calls (`region_zones`, `_resolve_project`), the
IAP transport, the two-state `apply_volume`/`apply_vm`, and the #1836 capacity
fallback. Extract the provider-specific pieces behind the §2 interface so the engine's
lifecycle-stage framework (`_cs_*` stages) is provider-neutral and the GCP behavior is
unchanged. Keep the GCP capacity-fallback logic in the GCP backend, not the shared engine.

**This is deliberately Phase 3, not Phase 1.** Abstracting the engine before a second
backend exists would be speculative (the right interface comes from two implementations,
not one) and would churn the shipped GCP path for no current consumer. Phase 1 ships only
the `select_backend` dispatch branch (§2) + the dev-fetch override (§10); the engine
extraction happens here in Phase 3, with the Scaleway backend as the validating second
consumer and the full GCP test suite as the behavior-preservation guard.

### 10. Cross-repo split & module fetch (release-ordered)

The OpenTofu module is **not** local to vergil-tooling. The off-platform engine fetches
modules from a **vergil-vm v-tag archive**:

```python
_MODULES_URL = "https://github.com/vergil-project/vergil-vm/archive/refs/tags/{tag}.tar.gz"
_TAG_RE = re.compile(r"^v\d+\.\d+(\.\d+)?$")   # tags only — no branch/local override today
```

So the Scaleway module (`opentofu/modules/scaleway/server`) lives in **vergil-vm** and is
only fetchable once it's in a **released vergil-vm tag**. This makes the feature a
two-repo, release-ordered change:

- **Sequence:** vergil-vm Scaleway-module PR → vergil-vm release (v-tag) → vergil-tooling
  backend referencing that tag. The backend cannot be e2e-"done" until the module is tagged.
- **Dev-fetch override (new, small):** add a `fetch_modules` escape hatch —
  `VRG_MODULES_REF` (a git ref/branch) and/or `VRG_MODULES_PATH` (a local checkout) — so
  the backend is developable and e2e-testable against an unreleased module *before* the prod
  tag exists. Without it, every iteration requires cutting a vergil-vm release. The override
  is dev-only and useful beyond this feature; production still resolves to the v-tag.
- **Coordination:** the module PR (vergil-vm) and the backend PR (vergil-tooling) are
  tracked together under #1851; the module must merge+tag first.

## Testing

- **Dispatch**: `select_backend` returns the Scaleway backend for `provider="scaleway"`,
  GCP backend for `"gcp"`, raises on unknown.
- **Fingerprint**: Scaleway fingerprint includes provider/region/offer and is unaffected
  by GCP-only fields; the GCP fingerprint is byte-for-byte unchanged (no spurious rebuild).
- **Module var mapping**: spec → `scaleway_baremetal_server` vars (offer/zone/os/ssh/
  user-data) are correct; cloud-init carries the provision env + Tailscale auth key.
- **Transport**: `TailscaleTransport` targets the MagicDNS name; no public SSH assumed.
- **Readiness**: the readiness probe waits on the tailnet marker and, on timeout, raises a
  loud error naming the console break-glass (never hangs, never silently passes).
- **Lifecycle**: rebuild drives the **reinstall API against the stored server ID** (same
  server, not a destroy+recreate) and mints a fresh ephemeral key; destroy releases;
  single-state (no volume calls).
- **Stock check**: create sweeps zones for offer availability and provisions in the first
  with stock; all-out-of-stock raises the clear message, not a raw trace.
- **Module fetch**: `VRG_MODULES_REF` / `VRG_MODULES_PATH` override resolves modules from a
  branch/local path; absent the override, production resolves to the v-tag (and rejects a
  non-tag ref).
- **e2e (gated, costs money)**: real provision on Scaleway, `/dev/kvm` present, reachable
  over tailnet, rebuild keeps the same server ID — mirrors the gated GCP cloud e2e.

## Phased implementation

1. **Provider dispatch + dev-fetch override** — the `select_backend` `provider` branch
   (§2, Phase 1) and the `VRG_MODULES_PATH`/`VRG_MODULES_REF` overrides (§10). GCP behavior
   unchanged, fully green, independently mergeable. This is the only Phase-1 shared edit and
   Azure's sole hard Phase-1 dependency. **No interface or engine change here.**
2. **Scaleway module in vergil-vm** (§3) — the `scaleway/server` OpenTofu module, merged and
   **tagged** in a vergil-vm release (the prerequisite for phase 3 e2e; see §10).
3. **Scaleway backend + provider-neutral abstraction** — the engine/interface extraction
   (§2 interface + `fingerprint_fields`, §9) *together with* the Scaleway backend that
   validates it, plus §1, §4, §5, §6, §7 (reinstall-via-API rebuild, Tailscale transport +
   readiness/break-glass, create-time stock check), on top of phases 1–2. The full GCP test
   suite is the behavior-preservation guard; e2e uses the dev-fetch override until the
   phase-2 tag lands.

(Three implementation plans across two repos; this is one cohesive design. The provider-
neutral interface is extracted in phase 3 — with two real consumers to shape it — not phase
1. Phase 2 is the vergil-vm change and gates phase-3 e2e.)

## Alternatives considered

- **Hetzner dedicated (AX42, ~€46/mo)** — ~3× cheaper, but Robot/`installimage`
  provisioning and only a community Terraform provider; the roughest IaC fit, against the
  "cloud-IaC only" decision. Revisit as a future provider if cost dominates.
- **Oracle OCI (E5.Flex, ~$250/mo)** — clean Terraform cloud VM with confirmed nested
  virt; pricier and still a capacity-contended cloud VM. A good *cloud-VM* fallback,
  not the cheapest reliable.
- **Direct key-only public SSH + firewall** — simplest transport, but public attack
  surface and IP allow-lists break on operator roaming (the problem IAP solved). Rejected
  in favor of the overlay.
- **Two-state (server + network volume)** — Scaleway has block storage, but it adds cost
  and complexity for data we've declared non-precious; single-state is simpler and matches
  the 24/7 usage.
