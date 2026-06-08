# Forge Abstraction Strategy: Decoupling Tooling from GitHub

**Issue:** #1521
**Date:** 2026-06-08
**Status:** Draft — Strategy / R&D (not an implementation plan)

## Problem

The Vergil tooling is structurally welded to GitHub. The `vrg-*` command
suite reaches GitHub through a ~560-line `gh`-CLI wrapper
(`lib/github.py`) and roughly 25 distinct `gh` operations, consumed by
15+ tools and several library modules. There is no provider abstraction
anywhere: `vergil.toml` has no notion of a forge, git remotes are parsed
for `github.com` specifically, identity is a GitHub App, and CI delegates
wholesale to GitHub Actions. GitHub is not a dependency we chose and can
swap; it is an assumption baked into every layer.

This is the classic vendor lock-in exposure. A single external provider —
one we do not control and increasingly do not trust — sits on the
critical path of everything the project does. If that provider degrades,
changes terms, or starts charging for what we rely on, we have no
migration path that is anything short of a rewrite. Lock-in is what kills
the independence this project is built to protect.

## Strategic Driver: The Fork-Away-From-Commercial-Capture Pattern

We are moving now, deliberately, for a reason that is historical and not
merely technical.

**(Historical data.)** When an open-source project is absorbed by a
commercial entity, the community's repeated and rational response is to
fork the project out of the company's hands — and the community fork
generally becomes the healthier successor:

- **MariaDB** forked from MySQL after Oracle's acquisition.
- **LibreOffice** forked from OpenOffice after Oracle, which later
  abandoned the original to Apache.
- **Jenkins** forked from Hudson after Oracle.
- **Rocky Linux / AlmaLinux** emerged after IBM-owned Red Hat converted
  CentOS into the upstream-only "CentOS Stream" in 2020, destroying its
  use as a downstream rebuild.

**(This case — data.)** Gitea followed the same script. In October 2022,
two project owners transferred Gitea's domain and trademarks to **Gitea
Ltd**, a for-profit company, without a community vote. The community's
open letter asking for a non-profit steward was refused, so they created
**Forgejo** — a hard fork now governed by **Codeberg e.V.**, a German
non-profit, funded by donations. The fork's maintainers later cited Gitea
"merging very large untested code" causing "major regressions" as a
reason to harden the split.[^gov-lwn][^gov-fork]

**(Judgment.)** That last detail — quality regressions under commercial
stewardship — is the pattern's tell. The recurring lesson of the last
several decades is that commercializing an open-source project tends to
erode the quality and community that made it valuable, because the
incentives shift from the community to the commercial adopter. We should
be skeptical that this case is the exception. Betting on the community
fork (Forgejo) over the commercially-captured original (Gitea) is
therefore the historically-validated move, not the contrarian one.

Choosing Forgejo is also the clearest expression of this project's
values: we prioritize the open-source community and the strategic
functionality it depends on, not the convenience of possible commercial
adopters. Moving to a non-profit-governed forge — and supporting that
non-profit — is that priority made concrete.

## Foundational Principles

1. **Independence is the goal, not a specific forge.** "Self-hosted"
   means *not critically dependent on a cloud service we do not control*
   — running on hardware we own (the development laptop today, a home
   cluster later). The constraint is independence, not any one box.
2. **The abstraction is justified by the migration, not by a hypothetical
   third forge.** The move off GitHub is inherently dual-provider (see
   Section 4). That is what makes a provider-neutral layer load-bearing
   rather than speculative.
3. **Support breadth where it is cheap; do not over-invest where it is
   not.** A clean abstraction makes the eventual Forgejo-vs-Gitea choice
   academic. We lean toward supporting more forges — but the
   cross-forge "lowest common denominator" is a design heuristic, not a
   hard requirement, and we abandon it if the forges diverge and breadth
   stops paying.
4. **Migrate per repo, never big-bang.** The cutover is a controlled,
   per-repository migration over weeks, not a single switch.

## Section 1: Strategic Direction

**Forgejo is the target; Codeberg comes with it.** The primary platform
is a self-hosted Forgejo instance — local-first on owned hardware. Because
**Codeberg runs Forgejo**, a single Forgejo adapter covers both our
self-hosted instance and Codeberg-as-a-provider; they differ only in base
URL and hosted-vs-self-hosted auth. Supporting Forgejo gives Codeberg for
free.

This direction also opens the option of **supporting Codeberg e.V.
financially** — a German non-profit forge aligns directly with the
independence thesis, and patronage is consistent with prioritizing the
commons over commercial capture.

Gitea remains a *possible future adapter*, not a current target. The
abstraction leaves the seam for it (Section 4), but we do not build it
until there is a concrete reason to.

## Section 2: GitHub Exposure Map

The coupling surface, tiered by how hard each part is to make
provider-neutral. (Effort is expressed as complexity tiers —
*mechanical / moderate / hard* — not calendar estimates.)

| Area | Tier | Notes |
|---|---|---|
| Core forge ops: PR/issue/label/release/commit-status (~25 `gh` calls in `lib/github.py`) | **Mechanical** | Same concepts on every forge; 1:1 adapter mapping |
| Audit gate (`vergil-audit/approved`) | **Mechanical** | Reproducible via commit-status + required context (Section 5) |
| Git remote / URL parsing (`git@github.com:`, `https://github.com/`) | **Mechanical** | Detect forge from remote; route to adapter |
| `vergil.toml` schema (no forge concept today) | **Mechanical** | Add a `forge` section (Section 4) |
| Auth / identity (GitHub App → PAT/OAuth) | **Moderate** | Real rework; simpler than GitHub Apps |
| CI / Actions (`vergil-actions` reusable workflows) | **Moderate–Hard** | ~80% ports; runner/registry/marketplace are real work (Section 6) |
| GitHub-only config: rulesets, Projects v2 | **Hard / drop** | No Forgejo equivalent; degrade or abandon |

The hook guard (`vrg-hook-guard`) is already provider-agnostic — it
blocks raw `git`/`gh` regardless of forge. The config tools were
pre-emptively renamed `org-config` / `repo-config` (away from
`github-config`) specifically to keep this door open; the abstraction
instinct already exists in the codebase and this work formalizes it.

## Section 3: Research Findings (Verified)

All claims below were verified against primary sources (see References).
Each is labeled by confidence.

1. **API: GitHub-*shaped*, not drop-in.** *(High.)* Gitea/Forgejo expose
   their own `/api/v1/` REST API with GitHub-*named* webhook headers and
   the same core concepts, but it is **not** GitHub's API. The `gh` CLI
   does **not** work against them; a separate client is
   required.[^api-forgejo][^api-gitea] Hard divergences are exactly the
   GitHub-proprietary features: no GitHub Apps, no check-runs API, no
   rulesets, no Projects v2.

2. **Prior art: mature in Go, absent in Python.** *(High.)* Multiple
   mature Go abstractions exist (`drone/go-scm`,
   `fluxcd/go-git-providers`, and the newer but pre-1.0
   `git-pkgs/forge`).[^pa-goscm][^pa-flux][^pa-forge] There is **no**
   mature Python multi-forge library: `pyforgejo` is Forgejo-only (an
   auto-generated OpenAPI client), `py-gitea` is Gitea-only, PyGithub is
   GitHub-only.[^pa-pyforgejo][^pa-pygitea] A Python project must build
   the abstraction itself — but on top of these existing single-forge
   clients, so the work is the interface and mapping, not HTTP plumbing.

3. **Actions: "familiar, not compatible," ~80% mechanical.** *(High.)*
   Forgejo's docs state it outright: designed to be familiar to GitHub
   Actions users, *not* compatible.[^ci-forgejo] Concrete breaks: no
   Marketplace (actions work by full path but lose discovery/auto-update;
   internal actions must be self-hosted); `permissions` and
   `continue-on-error` keys are ignored; the `github` context is
   partially populated; `github-script` does not work; runner labels must
   be manually mapped to images or jobs hang; `ghcr.io` → local registry;
   `github.token` → `gitea.token`.[^ci-practitioner]

4. **The audit gate survives.** *(High.)* No check-runs and no GitHub App,
   **but** Forgejo/Gitea have a commit-status API, and branch protection
   can require named status contexts (glob-matched) that a bot satisfies.
   Bot auth is a PAT or OAuth2 token.[^auth-gitea][^auth-forgejo] The
   merge gate is fully reproducible (Section 5).

5. **Governance favors Forgejo; the nuance holds.** *(High.)* Forgejo is
   under Codeberg e.V. (non-profit), donation-funded, with documented
   governance; it became a hard fork in February 2024 but intends to keep
   API compatibility where possible.[^gov-lwn][^gov-fork] Gitea remains
   open-source and active with a commercial hosted tier — for-profit
   stewardship is not automatically fatal, but the observable behavior
   (un-voted trademark transfer, quality regressions) is the warning the
   pattern predicts.

**Research gaps (carried as risks).** The Actions and governance threads
were the thinnest in the automated pass and were gap-filled by direct
source reads; they warrant deeper validation before any CI port begins.
`git-pkgs/forge` is pre-1.0 and should not be treated as stable.

## Section 4: Abstraction Architecture

### The interface is a seam, not a stub

Define one `Forge` interface modeled on the **operations the tooling
actually performs** (the ~25 verbs behind `lib/github.py`), not on any
forge's API surface: `create_pr`, `merge_pr`, `pr_status`,
`post_status_check`, `ensure_label`, `create_tracking_issue`,
`resolve_tracking_issue`, and so on.

Two real adapters implement it:

- **`GitHubForge`** — wraps today's `gh`/API code, refactored behind the
  interface.
- **`ForgejoForge`** — wraps `pyforgejo`; serves both self-hosted Forgejo
  and Codeberg.

The "placeholder" for future forges is the **seam itself** — the `Forge`
interface, a small provider registry, and a `forge.type` field in
`vergil.toml` — **not** speculative stub classes. A stub with no caller
rots and lies about being tested; the seam carries the option value at no
maintenance cost. Adding Gitea later is one new adapter class, registered.

### Why two adapters, and why that keeps the interface honest

The decisive justification for the abstraction is **the migration is
inherently dual-provider.** The cutover is per-repo over weeks, so there
is a window where some repos are on GitHub and some on Forgejo, driven by
the same tooling, selected per repo by `forge.type`. That forces two live
adapters at once.

This matters twice over:

1. **It is not speculation.** We have two *real* implementations from day
   one — GitHub (migrating from) and Forgejo (migrating to). An
   abstraction designed against a single implementation always leaks that
   implementation's shape; one validated against two real forges cannot.
2. **It is nearly free.** The tooling is welded to `gh` across ~25 call
   sites that must be touched to migrate anyway. Routing them through the
   interface *during that forced rewrite* is marginal extra cost. Coding
   straight to Forgejo and "extracting the interface later" means touching
   all 25 sites twice.

**(Honest counter-case.)** Absent the GitHub legacy — a greenfield,
Forgejo-only world — the abstraction would be premature YAGNI and we would
code straight against Forgejo. The layer earns its keep specifically
*because* we are migrating *from* something, and that migration is
dual-provider. It pays for itself on the first migration, independent of
ever adding a third forge.

### Handling GitHub-only features

Resolved design: **lowest-common-denominator core + capability flags.**

- The **merge-safety path** uses only primitives both forges have:
  commit-status (not check-runs), simple required-context branch
  protection (not rulesets). This keeps the core portable.
- **GitHub-only extras** (Projects-v2-based label discovery, ruleset
  management) become optional capabilities the interface exposes via
  `supports_x()` flags. On GitHub they work at full power; on Forgejo
  they degrade gracefully or no-op.

LCD is the heuristic for the *core*, not a ban on forge-specific power
where it is genuinely useful (Principle 3).

### Config

Add to `vergil.toml`:

```toml
[forge]
type = "github"        # | "forgejo"   (default "github" for back-compat)
base-url = "..."       # for self-hosted Forgejo / Codeberg
```

`forge.type` drives the provider registry. Default `github` preserves
every existing repo's behavior with no change, enabling per-repo opt-in
migration.

## Section 5: Reproducing the Audit Gate on Forgejo

The `vergil-audit/approved` mechanism is the project's merge-safety
keystone, and it is fully reproducible without GitHub Apps or check-runs:

| GitHub today | Forgejo equivalent |
|---|---|
| GitHub App identity + installation token | Bot account + PAT (or OAuth2) |
| `check-run` named `vergil-audit/approved` | **commit status** with context `vergil-audit/approved` |
| Ruleset requires the check | Branch protection **requires the status context** (glob-matched) |
| App posts the check-run | Bot PAT posts the commit status via the status API |

The security property is preserved: only the audit bot identity can post
the named status, and branch protection refuses merge without it. The
mechanism differs (status vs. check-run, PAT vs. App); the guarantee does
not. This was the one piece feared to be a blocker, and it is mechanical.

## Section 6: CI / Actions Strategy

Porting `vergil-actions` is the largest single cost center, and it is
*moderate-to-hard*, not impossible. Forgejo Actions reuses the workflow
YAML model and runs an `act`-based runner, so ~80% of standard workflows
port. The concrete work (from Section 3.3):

- **Action sourcing.** No Marketplace. Reference actions by full path,
  vendor them, or mirror them into the Forgejo instance. The reusable
  `ci-*` / `cd-*` workflows currently pulled from `vergil-actions@v2.1`
  must be re-homed and referenced by Forgejo paths.
- **Runner provisioning.** Self-hosted runners with explicit label→image
  mapping; Docker-based (`gitea/act_runner`) with daemon access. We own
  scaling.
- **Registry.** Replace `ghcr.io` references with a local registry;
  handle credentials ourselves.
- **Token/context.** `github.token` → `gitea.token`; ignored keys
  (`permissions`, `continue-on-error`) and partial `github` context need
  audit; `github-script` steps need shell replacements.

**(Judgment.)** CI is the natural candidate to **phase last**. The forge
abstraction (git hosting + PR + audit-gate workflow) can land and be
validated on a local Forgejo instance while CI still runs on GitHub or a
minimal local runner. The deep `vergil-actions` port is a separable,
later effort and should not gate the migration.

## Section 7: Migration Model

- **Per-repo, config-driven.** `forge.type` in each repo's `vergil.toml`
  selects the provider. Default `github` means nothing changes until a
  repo opts in.
- **Dual-provider window.** During the controlled cutover, some repos run
  on GitHub and some on Forgejo under the same tooling — the property that
  justifies the abstraction (Section 4).
- **Controlled then aggressive.** Migrate a small number of repos first,
  validate the full loop (clone, branch, PR, audit-gate, merge, finalize)
  on the self-hosted Forgejo instance, then widen.
- **Reversible.** Because both adapters remain live, a repo can move back
  to GitHub if a gap surfaces — the abstraction is insurance in both
  directions.

## Section 8: GitHub Trigger Plan

This is R&D until GitHub's behavior crosses a line. Pre-deciding the
triggers means the move is a known quantity, not a panic. Execute the
migration when any of these occurs:

- GitHub begins charging for capabilities currently relied on for free,
  at terms we decline to accept.
- A sustained reliability regression (repeated multi-day degradations
  that block work).
- A terms-of-service or policy change hostile to the project's model.
- An acquisition/governance event that materially changes GitHub's
  trajectory.

Until a trigger fires, the deliverable is *readiness*: the abstraction
seam, the Forgejo adapter, and a validated migration loop — built at
whatever cadence bandwidth allows.

## Section 9: Scope, Non-Goals, and YAGNI Guardrails

**In scope:** the `Forge` interface; `GitHubForge` and `ForgejoForge`
adapters; `forge.type` config; the audit-gate reproduction; a validated
per-repo migration loop on self-hosted Forgejo.

**Non-goals / guardrails:**

- **No speculative stubs.** Future forges are a seam, not stub classes.
- **No LCD purity for its own sake.** LCD is the core heuristic;
  capability flags carry forge-specific features.
- **No GitLab in this effort.** Different paradigm (MRs, different API);
  out of scope unless a concrete need appears.
- **Re-evaluation trigger.** If, over the coming months, Forgejo and Gitea
  visibly *diverge* and no real second-forge need materializes, stop
  investing in cross-forge breadth and let the "abstraction" simply be a
  clean Forgejo client. If they *converge*, the layer gains value and we
  lean in. This is decided later, with data — not now.

## Section 10: Phased Path (Bandwidth-Aware)

Sequenced so each phase delivers standalone value and can pause between.

- **Phase 0 — Spike (near-term, ~one focused session).** Stand up a local
  Forgejo instance. Build a minimal `ForgejoForge` behind the seam for the
  thinnest vertical slice: create a branch, open a PR, post the
  `vergil-audit/approved` status, merge under required-context protection.
  Goal: prove the gate and the seam end-to-end on owned hardware.
- **Phase 1 — Extract the seam.** Introduce the `Forge` interface and
  refactor `lib/github.py` behind `GitHubForge`. No behavior change; the
  default stays GitHub. This is the load-bearing refactor.
- **Phase 2 — Complete the Forgejo adapter.** Map the full ~25-operation
  surface; add `forge.type` config and the provider registry.
- **Phase 3 — Migrate pilot repos.** Move a few repos via `forge.type`;
  validate the full loop; keep GitHub adapter live (dual-provider window).
- **Phase 4 — CI port (separable).** Re-home `vergil-actions` to Forgejo
  Actions; provision runners and registry. Deferrable; does not gate
  Phases 0–3.
- **Phase 5 — Widen.** Controlled-then-aggressive rollout across repos.

## Dependencies

- A self-hosted Forgejo instance on owned hardware (Phase 0 prerequisite).
- `vrg-whoami` canonical identity resolver (#1520) — identity must be
  queryable, not inferred, as the auth model generalizes beyond GitHub.
- Deeper validation of the Actions and governance research threads before
  Phase 4.

## Risks

| Risk | Mitigation |
|---|---|
| Abstraction designed against one forge leaks GitHub's shape | The migration forces two real adapters (GitHub + Forgejo) from day one — the interface is validated against both |
| Over-investment in cross-forge breadth that never pays | LCD is a heuristic, not a requirement; explicit re-evaluation trigger on converge/diverge (Section 9) |
| `vergil-actions` port is larger than estimated | Phase CI last and separately; the forge migration does not depend on it |
| No mature Python multi-forge library to lean on | Build a thin layer on existing single-forge clients (`pyforgejo`); the work is interface + mapping, not transport |
| `git-pkgs/forge` (Go) pre-1.0 — not a dependency we can adopt | We build in Python regardless; Go libraries are reference, not runtime |
| Forgejo Actions incompatibilities discovered mid-port | Validated spike (Phase 0) decouples git/PR/audit migration from CI; CI gaps do not block the move |
| GitHub-only features (rulesets, Projects v2) have no Forgejo equivalent | Capability flags + graceful degradation; the merge-safety core uses only LCD primitives |
| Hosted-vs-self-hosted Forgejo (Codeberg) auth/rate-limit differences | Same adapter, different config; validate Codeberg as a distinct deployment of the Forgejo adapter |

## References

[^api-forgejo]: Forgejo API usage — <https://forgejo.org/docs/latest/user/api-usage/>
[^api-gitea]: Gitea API usage — <https://docs.gitea.com/development/api-usage>
[^pa-goscm]: drone/go-scm — <https://github.com/drone/go-scm>
[^pa-flux]: fluxcd/go-git-providers — <https://github.com/fluxcd/go-git-providers>
[^pa-forge]: git-pkgs/forge — <https://github.com/git-pkgs/forge>
[^pa-pyforgejo]: pyforgejo — <https://pypi.org/project/pyforgejo/>
[^pa-pygitea]: py-gitea — <https://github.com/Langenfeld/py-gitea>
[^ci-forgejo]: Forgejo Actions vs GitHub Actions — <https://forgejo.org/docs/latest/user/actions/github-actions/>
[^ci-practitioner]: Gitea/Forgejo advanced CI/CD (practitioner) — <https://mylinux.work/guides/gitea-forgejo-advanced-cicd/>
[^auth-gitea]: Gitea protected branches — <https://docs.gitea.com/usage/access-control/protected-branches>
[^auth-forgejo]: Forgejo branch protection — <https://forgejo.org/docs/latest/user/protection/>
[^gov-lwn]: LWN: the Gitea/Forgejo fork — <https://lwn.net/Articles/963095/>
[^gov-fork]: Forgejo: Forking Forward — <https://forgejo.org/2024-02-forking-forward/>
