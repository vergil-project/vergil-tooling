# Pushback Review: GitHub configuration enforcement and validation refactor

**Date:** 2026-05-04
**Spec:** docs/specs/2026-05-04-github-config-enforcement-design.md
**Commit:** 809e73986aef2bafd3311d58fcb6f5dbdc94f5e4

## Source Control Conflicts

None — no conflicts with recent changes. Two relevant notes:

- The recently added `[markdownlint].ignore` support (76b7846) added a
  `MarkdownlintConfig` dataclass to `StConfig`. The spec's TOML schema
  description only mentioned `[project]`, `[ci]`, and `[github]` without
  acknowledging existing sections.
- The container guard added in da03b00 enforces that `st-validate-local`
  runs inside a dev container. The spec's proposed multi-container execution
  model requires revisiting this guard.

## Issues Reviewed

### [1] The canonical check name registry is undefined
- **Category:** Ambiguity
- **Severity:** Critical
- **Issue:** The check name registry is the linchpin connecting rulesets, CI
  workflows, and `st-validate --only/--skip`. The spec said "names TBD during
  implementation" and left the CI job structure (individual jobs vs.
  consolidated) as an implementation decision. These aren't implementation
  details — they're the core design decision that unblocks everything else.
- **Resolution:** Define the check name registry in the spec before
  implementation starts. List every canonical name, the job structure that
  produces it, and the naming convention for matrix expansions.

### [2] Multi-version execution model conflicts with current container architecture
- **Category:** Feasibility
- **Severity:** Serious
- **Issue:** The spec proposed `st-validate` running language checks "once per
  version in the matching dev container image" but the current tool runs
  inside a single container (enforced by a recently added container guard).
  The version matrix breaks the single-container assumption.
- **Resolution:** Host-orchestrated model. `st-validate` becomes a host-side
  tool that calls `st-docker-run` per version in the matrix. The container
  guard is removed. This reverses the recent da03b00 addition but is the
  clean architectural answer for multi-version support.

### [3] Step 8 is a cross-repo effort hiding in a single line item
- **Category:** Scope imbalance
- **Severity:** Serious
- **Issue:** "Refactor CI workflows across all repos" means touching CI
  workflows in 13+ repos — each needs a PR, testing, and merging. Every
  other step is internal to standard-tooling. This step also has an
  undocumented ordering dependency: workflows must produce the new check
  names before rulesets can enforce them.
- **Resolution:** Expand step 8 into a phased rollout plan. Define the
  per-repo migration sequence, how to validate each one before moving on,
  and make the step 8 to step 9 ordering explicit.

### [4] No rollback plan for fleet-wide `st-github-config apply`
- **Category:** Omission
- **Severity:** Serious
- **Issue:** Step 9 writes rulesets across 13+ repos via the GitHub API. If
  the derivation engine computes an incorrect check list, it blocks merges
  fleet-wide. No recovery mechanism was specified.
- **Resolution:** `apply` refuses to act unless `audit` passes first — i.e.,
  unless the current CI workflows already produce the expected check names.
  This proves the names match before enforcing them and naturally enforces
  the step 8 to step 9 ordering.

### [5] `scripts/dev/*.sh` fate unclear
- **Category:** Ambiguity
- **Severity:** Moderate
- **Issue:** The spec described `scripts/dev/*.sh` as both the "per-repo
  customization point" and an "escape hatch." Audit of the fleet revealed
  the four core scripts (lint, typecheck, test, audit) are either trivial
  container-local invocations (standard-tooling) or identical boilerplate
  wrapping `st-docker-test` with two per-language variables (image name and
  command) that are fully derivable from `standard-tooling.toml`.
- **Resolution:** `st-validate` absorbs all four standard checks centrally
  with per-language commands defined in the tool. Scripts deleted from all
  repos. `st-docker-test` removed if fully subsumed. No per-repo
  customization for now — that's a future design concern on a clean slate.

### [6] Disabled security settings with no rationale
- **Category:** Omission
- **Severity:** Moderate
- **Issue:** `vulnerability_alerts: disabled` and
  `dependabot_security_updates: disabled` with no explanation in the spec.
- **Resolution:** Add rationale: Dependabot security updates disabled because
  they apply patches unsupervised. Vulnerability alerts considered redundant
  with Trivy/Semgrep/language-specific audit coverage. Separate issue created
  to reevaluate GitHub security offerings holistically.

### [7] `labels.json` path wrong
- **Category:** Contradiction
- **Severity:** Minor
- **Issue:** Spec referenced `data/labels.json`. Actual path is
  `src/standard_tooling/data/labels.json`.
- **Resolution:** Corrected in spec.

### [8] Spec doesn't acknowledge existing TOML sections
- **Category:** Omission
- **Severity:** Minor
- **Issue:** Architecture described the TOML schema as `[project]` + new
  `[ci]` + new `[github]` without mentioning existing `[dependencies]` and
  `[markdownlint]` sections.
- **Resolution:** Added acknowledgment that existing sections are unchanged.

### [9] `allowed_actions: all` too permissive
- **Category:** Security
- **Severity:** Minor
- **Issue:** `allowed_actions: all` allows any published GitHub Action to run.
  Fleet audit revealed only 8 distinct action owners are actually used.
- **Resolution:** Changed to `allowed_actions: selected` with
  `patterns_allowed` restricted to the 8 owners in use: `actions/*`,
  `github/*`, `docker/*`, `ruby/*`, `actions-rust-lang/*`, `astral-sh/*`,
  `pypa/*`, `wphillipmoore/*`.

## Unresolved Issues

None — all issues addressed.

## Summary

- **Issues found:** 9
- **Issues resolved:** 9
- **Unresolved:** 0
- **Spec status:** Needs update (check name registry must be defined; several
  architectural decisions need to be written into the spec before
  implementation can begin)
