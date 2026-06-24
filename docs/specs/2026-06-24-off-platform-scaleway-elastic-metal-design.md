# Off-platform: Scaleway Elastic Metal backend + provider-dispatch abstraction

- **Issue:** vergil-tooling#1851
- **Status:** Design (brainstormed)
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
- **rebuild** → **OS reinstall on the same allocated server** (Scaleway supports
  reinstall/reprovision in place). Keeps the box — **no capacity loss** — wipes the
  disk. This is the bare-metal analog of GCP's rebuild, minus the disk-survival.
- **destroy** → release the server; capacity returns to Scaleway's pool, all state gone.
- Native KVM: the `nested` flag and `70-nested-virt.sh` are satisfied with no config.

### 2. Provider-dispatch abstraction (the seam Azure conforms to)

Today `off-platform` resolves directly to a GCP-specific `OffPlatformBackend`. Introduce:

- `spec.provider` already exists; `select_backend` branches on it when
  `spec.off_platform` is true: `"gcp"` → the GCP backend, `"scaleway"` → the Scaleway
  backend. Unknown provider → explicit error.
- A **provider-neutral backend interface** (extending the existing `Backend` protocol)
  with the methods the off-platform engine calls. The minimum seam:
  - `apply(state_dir) -> ApplyResult` (host/address + whatever transport needs),
  - `destroy(state_dir)`, `rebuild(state_dir)`,
  - `transport() -> Transport`,
  - `status() -> str`,
  - `fingerprint_fields() -> list[str]` (provider-specific declaration inputs).
- **Fingerprint**: `spec_fingerprint` already includes `provider`; it must include the
  provider-relevant fields only (Scaleway: provider/region/offer; *not* the GCP-only
  `zone`/`volume`/`nested`). Each backend declares its fields so a non-applicable knob
  never trips a spurious rebuild.

This is the **only file both this work and Azure must touch** (`select_backend` +
the interface). The contract above is the convergence point (see §8).

### 3. OpenTofu module — `opentofu/modules/scaleway/server`

A single module using the first-class `scaleway` Terraform provider:

- `scaleway_baremetal_server` (offer = e.g. `EM-B230E-NVMe-128G`, zone = e.g.
  `fr-par-2`, os = an image that supports cloud-init, ssh_key_ids, user-data =
  rendered cloud-init).
- Reuses the existing `templates/provision/*.sh` via the cloud-init `user-data` path
  exactly as the GCP module does — the provisioning scripts are backend-neutral already.
- No volume module, no firewall-for-IAP, no `enable_nested_virtualization`.
- Outputs: the server's tailnet hostname/address for the transport (see §4).

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

### 7. Stock-out handling

Elastic Metal offers can be **out of stock** in a given zone (a real, if rarer,
analog to GCP's capacity stockout). v1: detect the out-of-stock apply error, surface
a clear message naming the offer/zone and pointing at the other Scaleway zones
(`fr-par-1/2`, `nl-ams-1`, `pl-waw-2`). A zone sweep can reuse the #1836 pattern but
is **deferred** unless stock-outs prove common — bare metal you hold doesn't churn the
way on-demand VMs do.

### 8. Coordination with the in-flight Azure backend

Both Azure and this work introduce provider branching. The convergence contract is §2:
`select_backend` branches on `spec.provider`, and each provider implements the
backend interface. Azure is a **cloud-VM** provider (it will likely keep a
VM+disk shape and a private-networking transport), so it diverges from Scaleway's
single-state/Tailscale specifics — but both must use the **same dispatch seam and
interface**. Whichever lands second rebases onto the seam; this spec owns its
definition. The only expected shared-file edits are `select_backend` and the backend
interface/protocol module.

### 9. Engine abstraction

`vm_cloud.py` is GCP-coupled: `gcloud` calls (`region_zones`, `_resolve_project`), the
IAP transport, the two-state `apply_volume`/`apply_vm`, and the #1836 capacity
fallback. Extract the provider-specific pieces behind the §2 interface so the engine's
lifecycle-stage framework (`_cs_*` stages) is provider-neutral and the GCP behavior is
unchanged. Keep the GCP capacity-fallback logic in the GCP backend, not the shared engine.

## Testing

- **Dispatch**: `select_backend` returns the Scaleway backend for `provider="scaleway"`,
  GCP backend for `"gcp"`, raises on unknown.
- **Fingerprint**: Scaleway fingerprint includes provider/region/offer and is unaffected
  by GCP-only fields; the GCP fingerprint is byte-for-byte unchanged (no spurious rebuild).
- **Module var mapping**: spec → `scaleway_baremetal_server` vars (offer/zone/os/ssh/
  user-data) are correct; cloud-init carries the provision env + Tailscale auth key.
- **Transport**: `TailscaleTransport` targets the MagicDNS name; no public SSH assumed.
- **Lifecycle**: rebuild maps to reinstall (same server), destroy to release; single-state
  (no volume calls).
- **Stock-out**: the out-of-stock apply error surfaces the clear message, not a raw trace.
- **e2e (gated, costs money)**: real provision on Scaleway, `/dev/kvm` present, reachable
  over tailnet — mirrors the gated GCP cloud e2e.

## Phased implementation

1. **Provider-dispatch abstraction** — extract the seam (§2, §9); GCP behavior unchanged,
   fully green. Independently testable and the foundation Azure also consumes.
2. **Scaleway backend + module + transport** — §1, §3, §4, §5, §6, §7 on top of phase 1.

(Likely two implementation plans; this is one cohesive design.)

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
