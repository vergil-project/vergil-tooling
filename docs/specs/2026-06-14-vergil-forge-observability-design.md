# vergil-forge Observability — Co-located Metrics Stack — Design

**Issue:** #1661
**Date:** 2026-06-14
**Status:** Draft — Design
**Related:** #1653 (vergil-forge local host design — this is its
observability companion). Carries the DNA of the
`mq-cluster-tooling` observability work (Prometheus + Grafana,
provisioned-as-code, fail-loud, layered dashboard) adapted to a single
Lima *service* VM rather than a multi-node fault-drill lab.

## Purpose

Give `vergil-forge` — the local-first Forgejo host (#1653) — a thin,
**co-located** observability layer: one Grafana tab that answers *"is the
forge healthy, and what is it doing to itself?"* at a glance. The forge is
about to absorb heavy parallel-agent traffic — pushes, PR creation,
label/status writes, CI webhooks, gates, audits, sanity checks — and soon
local-Ollama AI-driven gates. Today the only way to know what the instance
is doing is to shell into the VM and read container logs. This design adds a
single pane.

This is the observability half of the forge; the host runtime is #1653. It
lives in the **`vergil-forge` repo's domain** (it operates that one service),
exactly like the host design — recorded here in `vergil-tooling/docs/specs/`
alongside its sibling because that is where the forge design currently lives.

## Foundational Decisions

These were settled during design and frame everything below.

1. **Co-located, scaled down.** Prometheus + Grafana run as additional
   containers *inside* the `vergil-forge` VM, alongside Forgejo + Postgres.
   It is a VM we own — we run what we want there. We carry the mq *DNA*, not
   its topology: there is no multi-node fleet and no fault-drill plane to
   engineer around, so the mq "separate the observer" and "scrape plane the
   drama can't sever" principles **do not apply**. The observer sharing the
   VM's fate is acceptable: if the forge VM is down, there is nothing to
   observe anyway.

2. **Fleet-wide is named, not designed.** Eventually these per-VM
   observability layers across the VM fleet could be tied into one roll-up.
   We do **not** design that now (YAGNI — its shape is unknown). We only keep
   this stack clean enough that it is not hostile to a later roll-up.

3. **v1 is service health; the pipeline view is a designed-for fast-follow.**
   v1 leans entirely on off-the-shelf exporters. The bespoke "what is the
   agent fleet doing" pipeline view, and the AI-gate view, are **named and
   reserved** (dashboard rows + a metric contract), built in follow-up specs.
   This mirrors the mq "payoff early, layer up" philosophy.

4. **Metrics-only v1; logs deferred.** A Loki/Alloy log + timeline layer is
   named as the next layer but **not built in v1** — the same call the mq lab
   made (it shipped the metric foundation first and deferred its Loki/Promtail
   timeline narration to a follow-up spec).

5. **Durable metrics history.** `prometheus-data/` joins `forgejo-data/` and
   `postgres-data/` on the host volume, with bounded retention. The forge is a
   long-lived service, not an ephemeral lab, so long-horizon trends survive VM
   rebuilds.

## Carried From mq-cluster-tooling (and What We Drop)

The mq observability work (`docs/specs/2026-06-10-lab-observability-design.md`
and `2026-06-11-observability-layered-dashboard-design.md`) established
patterns we deliberately reuse:

| mq pattern | Here? | Note |
|---|---|---|
| Prometheus + Grafana, standard free OSS | **Keep** | The instrument. |
| Provisioned as code; rebuild → identical | **Keep** | Via the forge's own mechanism (compose + checked-in config), **not** Ansible. |
| Dashboards-as-checked-in-JSON, Grafana file-provisioning | **Keep** | |
| Layered, most-important-first dashboard | **Keep** | Reserved top rows for the future headline. |
| Fail loud — `up==0` → red, no stale panels | **Keep** | |
| A CLI slice (`mqlab obs …`) | **Keep** | Folded into `vrg-forge`. |
| "Visit a URL" access | **Keep** | Lima-forwarded Grafana port. |
| Loki/timeline narration deferred to a follow-up | **Keep** | Named below. |
| Separate observer VM (`obs`) + data-plane probe (`mon-probe`) | **Drop** | Single VM; no failover to stay alive through. |
| Scrape plane drills can't sever (`net-mgmt`) | **Drop** | No fault drills here. |
| `render_scrape_targets(topology)` / `render_dashboard(topology)` | **Drop** | One VM, a fixed handful of local targets — a small static `prometheus.yml`, no renderer. |
| Custom textfile collectors via privileged systemd timers | **Defer** | Arrives with the pipeline layer (its emit path). |

Dropping the renderer machinery is the single biggest simplification: the mq
lab needed it because scrape targets came from a 19-node `topology.yaml`. Here
there is one VM with a fixed local target list, so the config is a small
checked-in file. We keep the *principle* (config-as-code, reproducible)
without the projection machinery.

## Architecture

Everything runs inside the existing `vergil-forge` Lima VM, extending the
container stack rather than adding infrastructure.

```text
macOS host (MacBook Pro)
│
├─ host-mounted volumes (virtiofs)        ← durable state
│    forgejo-data/   postgres-data/   prometheus-data/   ← NEW (bounded retention)
│    (grafana config + dashboards are CODE in config/grafana/, not host state)
│
└─ Lima VM: vergil-forge
     └─ container runtime
          ├─ forgejo            (existing)  ── native /metrics (token) ─┐
          ├─ postgres           (existing)                              │
          ├─ postgres_exporter  NEW  ── Postgres internals  ────────────┤  scraped
          ├─ node_exporter      NEW  ── the VM's own CPU/mem/disk/net ──┤  by
          ├─ cadvisor           NEW  ── per-container CPU/mem/IO  ───────┤  Prometheus
          ├─ prometheus         NEW  ── scrape + host-mounted TSDB  ◄────┘
          └─ grafana            NEW  ── one dashboard, Lima-forwarded port
```

**Upgrades** stay an image-tag bump + restart, identical to the forge host
model. The new containers are stateless except Prometheus, whose TSDB is the
one host-mounted addition.

### Scrape targets (static `prometheus.yml`)

A single VM means a fixed, hand-maintained target list — no renderer:

| Job | Target | Provides |
|---|---|---|
| `prometheus` | `localhost:9090` | self-monitoring |
| `node` | `node_exporter:9100` | VM host: CPU/mem/disk/net |
| `cadvisor` | `cadvisor:8080` | per-container CPU/mem/IO |
| `postgres` | `postgres_exporter:9187` | Postgres internals (§ below) |
| `forgejo` | `forgejo:3000/metrics` | Forgejo native metrics (bearer token) |

Targets are container-DNS names on the stack's internal network. Forgejo's
metrics endpoint is gated by a bearer token (`[metrics].TOKEN`); the scrape
config carries it (runtime-injected secret, never committed).

## The Layered Dashboard

One Grafana dashboard, read top-to-bottom so the eye lands on the most
important layer first — the mq "most-important-first" convention, with the
top rows **reserved** for the future headline exactly as the mq dashboard
reserved its MQ-service row and filled it later.

```text
╔═ vergil-forge — Status ═══════════════════════════════════════════╗
║ ▌AI GATES            (reserved — future, once Ollama lands)        ║
║ ▌PR / GATE / AUDIT PIPELINE   (reserved — fast-follow spec)        ║
╟───────────────────────────────────────────────────────────────────╢
║ ▌FORGEJO   up? · HTTP req/latency · repos/users · queue/worker     ║  ← v1
║ ▌POSTGRES  connections · locks/waits · slow queries · write        ║  ← v1
║            concurrency · cache hit · table/index bloat             ║
║ ▌INFRA     VM CPU/mem/disk/net  +  per-container (forgejo,         ║  ← v1
║            postgres) resource use                                  ║
╚═══════════════════════════════════════════════════════════════════╝
```

When the pipeline and AI-gate layers land, they slot in **above** the health
rows as the new headline, without a redesign.

### Forgejo row

From Forgejo's native Prometheus endpoint: instance up/down, HTTP request
rate and latency, repo/user/issue counts, background queue and worker health.
The off-the-shelf "is the service responsive" view.

### Postgres row — the forge's central wager, made visible

Postgres gets real depth because the forge design *bets* on it: the entire
"Postgres over SQLite" rationale (#1653) is **parallel-agent write
concurrency** — many agents pushing, opening PRs, and writing labels/statuses
as distinct concurrent writers. The Postgres row is built to show exactly that
axis:

- Active/idle connections and connection saturation.
- **Lock waits and contention** — the direct read on whether concurrent agent
  writers are serializing.
- Transaction/commit/rollback rate.
- Cache hit ratio, slow-query indicators.
- Table/index bloat over time.

This turns the host design's central bet into something watchable: when the
agent fleet hammers the forge, you see whether Postgres is absorbing the
concurrency or contending.

### Infra row

`node_exporter` for the VM's own resources and `cadvisor` for per-container
CPU/mem/IO — so "the VM is fine but the Forgejo container is pinned" is
distinguishable from "the whole VM is starved."

## Persistence, Retention & Backup

- **`prometheus-data/`** is host-mounted (durable across rebuilds) with
  **bounded retention — 30 days (configurable)**. Enough for "was last week
  busier" without unbounded growth.
- **Grafana keeps no durable host state.** Datasource and dashboards are
  provisioned from checked-in code, so Grafana's own sqlite is ephemeral — a
  rebuild reprovisions identically. No `grafana-data/` host mount.
- **Backup.** Metrics history is *useful, not precious*: unlike git repos it
  is **not pushed off-box**. The forge's periodic local snapshot *optionally*
  tars `prometheus-data/`; the off-box mirror stays git-only. Observability
  adds **zero** new off-box backup obligation.

## Access — "Visit a URL" — and `vrg-forge` Integration

Grafana binds to a **Lima-forwarded port** → `http://localhost:3000` from the
macOS host. Plain HTTP, host-local — consistent with forge v1 networking
(host + its VMs, no TLS/DNS). Agent VMs reach it the same way they reach
Forgejo (host gateway / shared Lima network), but the primary consumer is the
operator's browser on the host.

The observability containers are **part of the same compose stack**, so they
fold into the existing lifecycle CLI rather than adding a parallel one:

| `vrg-forge …` | Observability behavior |
|---|---|
| `bootstrap` | Provision Grafana datasource + dashboard; set the Forgejo `[metrics]` token; inject `postgres_exporter` credentials |
| `up` / `down` | Obs containers start/stop **with** Forgejo + Postgres (one stack) |
| `status` | Report Prometheus/Grafana health **and each scrape target up/down**, alongside VM/container/service health |
| `obs open` | **New verb** — port-forward and print the Grafana URL (the mq `obs open` analog) |
| `backup` | Local snapshot optionally includes `prometheus-data/` |

## Provisioned as Code — Repo Layout

Extends the `vergil-forge` repo structure from #1653:

```text
compose/
  forgejo-stack.yml            # existing — gains the obs services
                               #   (or a sibling observability.yml merged in)
config/
  app.ini.template             # existing — gains a [metrics] section
                               #   (enable + bearer token)
  prometheus/
    prometheus.yml             # static scrape config (the § scrape targets)
  grafana/
    provisioning/
      datasources/prometheus.yml
      dashboards/dashboards.yml
    dashboards/
      vergil-forge-status.json # the layered dashboard, checked in
```

Rebuild the VM and the entire stack plus the dashboard return identical — the
same reproducibility contract as the rest of `vergil-forge`.

## Deferred Layers — Contract Sketched Now, Built Later

Naming the **seam** now so future code emits into a defined shape and the
reserved dashboard rows have somewhere to connect.

### Pipeline layer (fast-follow spec)

PR/gate/audit events originate in short-lived CI/agent processes
(`vrg-pr-workflow`, the oracle loop, audit gates) — **not** long-running
scrape targets. So the carrier is a **Pushgateway or a small custom
exporter**, the analog of mq's QM-owner textfile collector. Contract:

- `forge_gate_result{gate,repo,verdict}`
- `forge_gate_duration_seconds{gate}`
- `forge_pr_inflight`
- `forge_audit_latency_seconds`

Pushgateway-vs-exporter is decided in *that* spec; v1 only reserves the row
and names the seam.

### AI-gate layer (future — once local Ollama is running)

Two metric sources:

1. **Ollama runtime** — inference latency, tokens in/out, model, queue depth,
   host CPU/GPU/mem. (Ollama exposes some metrics; a thin wrapper may be
   needed for the rest.)
2. **The CI-gate → AI call instrumentation** — which gate invoked which
   model, the verdict, wall-clock, token cost, errors/retries. Same emit path
   as the pipeline layer.

Contract sketch:

- `forge_ai_gate_inference_seconds{model,gate}`
- `forge_ai_gate_tokens{dir,model}`
- `forge_ai_gate_verdict{gate,verdict}`
- `forge_ai_gate_errors{gate,model}`

Defined now so the emit points exist when the code that calls Ollama is
written; built when Ollama is actually running.

### Logs / timeline (deferred, mq-style)

A Loki + log-shipper (Alloy/Promtail) layer making Forgejo, Postgres, and
container logs searchable in the same Grafana tab, and a timeline of forge
events. Named as the next layer; not in v1.

## Cross-cutting Concerns

### Fail loud — no stale panels

A silently stale dashboard lies. A failed scrape surfaces as Prometheus
`up == 0` → a **red tile**, never an empty or last-known-good panel; Grafana
"No data" is styled as a fault, not silence. v1 has no custom textfile
collectors yet (the `*_last_write_timestamp` staleness discipline arrives with
the pipeline layer), so v1 fail-loud is the Prometheus `up` discipline plus
no-data-as-red. No swallowed errors in any glue, per repo policy.

### Secrets

Runtime-injected, never committed: the `postgres_exporter` DB credentials, the
Forgejo metrics bearer token, and the Grafana admin password — via the forge's
existing secret mechanism, mirroring the mq lab-secret pattern. `.gitignore`
already covers the forge's secret material.

### Testing & validation

`vrg-container-run -- vrg-validate` remains the **only** validation command:

- Grafana dashboard JSON is lint-validated.
- `prometheus.yml` is checked with `promtool check config`.
- Any `vrg-forge obs` Python glue is unit-tested to the repo's coverage bar.

## Phased Path

- **Phase 0 — Stack up.** Add the obs services to the compose stack;
  `prometheus.yml`; Grafana datasource + a minimal dashboard; enable Forgejo
  `[metrics]`. Goal: `vrg-forge up` brings Prometheus + Grafana up and
  `vrg-forge obs open` lands a human on a live Grafana URL.
- **Phase 1 — Infra layer.** `node_exporter` + `cadvisor` wired; the infra
  row shows VM and per-container health. Kill a container → its panel reacts.
- **Phase 2 — Service internals.** `postgres_exporter` + the Forgejo native
  scrape; the Postgres and Forgejo rows, including the write-concurrency
  panel.
- **Phase 3 — Durability + lifecycle polish.** `prometheus-data/`
  host-mount + retention; `vrg-forge status`/`backup` integration.
- **Phase 4+ — Deferred layers** as they become necessary: the pipeline row,
  the AI-gate row (when Ollama lands), the Loki/timeline layer.

## Open Questions

- **Container metrics: cAdvisor vs. the runtime's own endpoint.** cAdvisor is
  one extra container and the standard choice; if the Lima container runtime
  already exposes per-container metrics, that could replace it. Settle in the
  plan.
- **Forgejo native metrics coverage.** Confirm which signals Forgejo's
  `/metrics` actually exposes for the version pinned by #1653, and whether the
  Forgejo row needs any supplement.
- **Retention window.** 30 days proposed; confirm against host disk budget and
  how far back trends are actually useful.
- **`obs open` vs. a fixed forwarded port.** Whether to add the `obs open`
  verb or simply document a stable forwarded Grafana port — minor, settle in
  the plan.

## Success Criteria

1. From a freshly bootstrapped forge, `vrg-forge up` + `vrg-forge obs open`
   lands a human on a live Grafana URL with no hand-configuration.
2. The dashboard reads top-to-bottom: reserved headline rows / Forgejo /
   Postgres / infra.
3. **Infra:** killing the Forgejo container reddens its panel within a scrape
   interval; the VM-vs-container distinction is visible.
4. **Postgres:** active connections and lock-wait/contention are visible — a
   burst of parallel-agent writes is watchable as it happens.
5. **Forgejo:** instance up/down, request rate, and queue/worker health are
   visible from the native endpoint; empty panels (metrics not enabled) are
   treated as a **failure**, not a pass.
6. Metrics history survives a VM rebuild (host-mounted TSDB) within the
   retention window.
7. The whole stack is reproducible from scratch and passes `vrg-validate`.
