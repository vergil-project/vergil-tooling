# Remove the interactive dual-agent audit loop; keep the audit identity dormant

- **Issue:** #1872
- **Date:** 2026-06-25
- **Status:** Approved (design)

## Problem

The repository carries an interactive dual-agent (USER/AUDIT) audit loop: two
Claude Code agents share a worktree, coordinate through a filesystem oracle
(`vrg-pr-workflow` + `.vergil/pr-workflow.json`), and the AUDIT agent runs
judgment checks on the USER agent's delta before a PR is opened. A parallel
ambition was to run the same review in CI.

Both have failed:

- The interactive USER↔AUDIT coordination never worked reliably in practice.
- The CI path requires a Claude API call per PR. At this repository's PR
  volume that is economically infeasible.

This is not a design to repair the loop. It is a decision that the loop is the
wrong design, and a plan to remove it cleanly.

## Decision: remove the *loop*, keep the *identity*

A single distinction governs the entire change:

- **The interactive loop dies.** The filesystem oracle's turn-taking
  machinery, the local AUDIT-agent runtime, and the skills that drove them are
  removed.
- **The audit *identity* stays, dormant.** The `vergil-audit` GitHub App,
  `IdentityMode.AUDIT`, `vrg-audit-approve`, `Role.AUDIT`, the `vrg-gh` audit
  allowlists, `vrg-whoami` audit mode, and VM audit provisioning are all
  retained, untouched, with their tests. They are the landing pad for a
  future API-driven agentic review — the realistic long-term form of the
  capability.

The surviving everyday workflow is the solo path that already works: an agent
implements an issue, records PR metadata, and a human submits and merges.

## Scope

In scope: code, tests, and docs in **this repository** (`vergil-tooling`).

Out of scope, tracked as follow-ups (see end):

- The skills, which live in the separate `vergil-claude-plugin` repository.
- Decommissioning org/infra (not happening — the App and identity stay).
- The branch-protection edit itself (an ops action), though its sequencing is
  load-bearing and specified below.

## Design

### 1. `vrg-pr-workflow` collapses to run-and-done

The oracle stops being a coordinator and becomes a metadata recorder.

**CLI (`src/vergil_tooling/bin/vrg_pr_workflow.py`)**

- Keep: `report-ready` (writes PR metadata to `.vergil/pr-workflow.json`) and
  `status` (prints state).
- Delete: `next` and its handshake, `_next_audit`, `_agent_role`,
  `submit-check`, `report-fixes`, `escalate`, `abort`, `resolve`.

`report-ready` no longer takes a turn or waits; it records metadata and exits.
With no AUDIT agent, there are no findings to fix (`report-fixes`), no rounds,
and no escalation path (`escalate`/`abort`/`resolve`).

`report-fixes` also carried a second capability (#1565): revising the recorded
PR metadata before submission. That is intentionally dropped, not lost.
Pre-submit, an agent re-runs `report-ready`, which overwrites `pr_metadata`
(its only guard is owner-is-user, and the simplified single-agent state has no
turn-taking, so re-running is always safe). Post-submit, metadata corrections
go through `vrg-pr-fix-body` against the live PR. The "revise" path consolidates
onto those two; `report-fixes` is removed.

**Engine (`src/vergil_tooling/lib/pr_workflow/engine.py`)**

- Delete: `audit_ack`, `apply_check`, `_complete_review`,
  `next_pending_check`, `apply_report_fixes`, `directive_for`, and all
  owner-flipping logic.
- Keep, simplified: `init_state` (no paired/solo distinction, no `owner`
  handshake), `apply_report_ready` (records `pr_metadata`, sets a terminal
  `status: "ready"`), `apply_submitted` (records the submission).

**State (`src/vergil_tooling/lib/pr_workflow/state.py`)**

Slim `WorkflowState` to the fields the run-and-done path actually uses:

```
issue, branch, base, pr_metadata, git (base_sha/head_sha),
submitted, created_at, updated_at, schema_version
```

Drop `owner`, `mode`, `participants`, `checks`, `round`, `history`,
`escalation`, `phase`. Bump `schema_version`. No migration: the file is
ephemeral per-PR working state, regenerated on each `report-ready`. One nuance:
the worktree scanner (`worktrees._probe_pr_workflow`) *does* cross-read other
worktrees' state files, so a leftover v1 file is read after the bump — but the
load error is caught there and surfaced as a captured reason (never a crash or
a silent failure), and it clears itself the next time that worktree runs
`report-ready`. So the bump needs no migration code; it degrades gracefully.

**Transport (`src/vergil_tooling/lib/pr_workflow/local_transport.py` and the
`Transport` ABC in `src/vergil_tooling/lib/pr_workflow/transport.py`)**

- Delete from `LocalFileTransport`: `wait_until_owner`, `wait_until_present`
  (nothing waits anymore).
- **Trim the `Transport` ABC** (`transport.py`) by the same two methods, so the
  abstract contract matches the surviving run-and-done surface
  (`read`/`write`/`head_sha`/`merge_base`, whatever remains). This is
  consistent with the parked #1865 cloud design, whose own note says a future
  `GitHubTransport` implements "the same contract minus the polling loop." Keep
  the ABC seam — only the dead polling methods go.
- Keep: atomic read/write helpers.

**Delete entirely**

- `src/vergil_tooling/lib/pr_workflow/registry.py` (the check registry —
  but see §3: the prompt files it loaded are preserved, not deleted).
- `src/vergil_tooling/lib/pr_workflow/settings.py` `max_rounds` (no rounds).

**Keep**

- `src/vergil_tooling/lib/pr_workflow/submission.py` (`read_pr_fields`,
  `record_submission`) — still feeds `vrg-submit-pr`.

**`vrg-submit-pr` (`src/vergil_tooling/bin/vrg_submit_pr.py`)**

- Remove the dual-agent handoff line that instructs pasting `/vergil:pr-watch`
  "into both agent sessions." Do not print a replacement line here: the
  pr-watch skill's USER-only shape is a plugin follow-up, so this repo simply
  drops the "both sessions" wording rather than guessing the future single-agent
  invocation.

### 2. The audit identity stays — untouched

No changes to any of the following. They remain as dormant infrastructure for
a future API-driven review, and their tests remain green:

- `vergil-audit` GitHub App (org infra).
- `IdentityMode.AUDIT` in `src/vergil_tooling/lib/identity_mode.py`.
- `vrg-audit-approve` (`src/vergil_tooling/bin/vrg_audit_approve.py`) and its
  `pyproject.toml` entry — the mechanism that posts the `vergil-audit/approved`
  check-run, which a future CI review will call.
- `Role.AUDIT` and `*-vergil-audit` login classification in
  `src/vergil_tooling/lib/pr_provenance.py`.
- The `_ALLOWED_AUDIT` allowlist and audit branches in
  `src/vergil_tooling/bin/vrg_gh.py`.
- `vrg-whoami` audit mode.
- VM audit credential provisioning.

### 3. Judgment criteria are preserved, not deleted

The six judgment checks
(`commit-message-fidelity`, `pr-description-fidelity`, `docstring-accuracy`,
`site-docs-reflection`, `scope-coherence`, `test-adequacy`) exist only as
prompt markdown files consumed by the now-deleted `registry.py`. They are the
*criteria* a future API review will apply, so they are retained — decoupled
from the dead loop.

- Move the six prompt files from
  `src/vergil_tooling/lib/pr_workflow/prompts/` to a stable documentation
  location: `docs/audit-criteria/`.
- Add a short `docs/audit-criteria/README.md` explaining that these are the
  judgment criteria for a future API-driven agentic review, that the
  interactive loop that once consumed them was removed in #1872, and that they
  are reference material, not wired into any running code.

### 4. The merge gate — relax to non-required

The `vergil-audit/approved` status check is required on branch protection, and
its only poster was the loop being removed. Leaving it required would hang
every PR. Therefore:

- **Relax `vergil-audit/approved` to non-required** on branch protection so
  merges proceed on human approval + CI. `vrg-audit-approve` stays dormant,
  ready to re-require the check when the API review ships.

This is an org-side ops action (a GitHub branch-protection setting, not repo
code), but it is **release-coordinated, not a deferred follow-up**: it must
land together with this change so PRs do not hang. See "Release sequencing"
below.

### 5. Documentation

- **`CLAUDE.md`** — In "Identity modes and PR submission," simplify the
  PR-handoff prose to the run-and-done `report-ready` → `vrg-submit-pr` flow
  and remove the dual-agent loop description. **Keep** `audit` listed as a
  valid identity mode (the identity is retained).
- **Site docs** — `docs/site/docs/guides/identity-architecture.md` keeps the
  audit identity but notes the local interactive loop is gone;
  `docs/site/docs/reference/dev/submit-pr.md` drops dual-agent handoff prose.
- **Old specs** — the dual-agent oracle/workflow designs
  (`2026-06-04-vergil-2.1-workflow-design.md`,
  `2026-06-05-pr-interface-design.md`,
  `2026-06-08-pr-workflow-oracle-design.md` and its phase plans) get a short
  superseding note at the top pointing at this document. History is not
  rewritten.

### 6. Tests

- Remove tests that cover the deleted loop machinery: the dual-agent
  orchestration/handshake, `submit-check`, `report-fixes`, owner-flipping,
  `wait_until_owner`/`wait_until_present`, the registry, and paired-mode
  integration/e2e flows.
- Slim the `tests/vergil_tooling/pr_workflow/` suite to the run-and-done
  surface (`report-ready`, `status`, slimmed state, submission).
- **Keep** all audit-identity tests:
  `test_vrg_audit_approve.py`, the `IdentityMode.AUDIT` cases in
  `test_identity_mode.py`, the audit allowlist cases in `test_vrg_gh.py`, and
  the `Role.AUDIT` cases in `test_pr_provenance.py`.

## Components and boundaries after the change

- **`vrg-pr-workflow`** — a thin recorder: write PR metadata, read it back. One
  clear purpose, no coordination, no waiting.
- **`vrg-audit-approve`** — dormant gate-poster, unchanged, awaiting the API
  review.
- **`docs/audit-criteria/`** — inert reference criteria.
- **The audit identity** — a dormant capability, fully intact.

## Risks and mitigations

- **Skills calling removed subcommands.** The current `/vergil:issue-implement`
  and `/vergil:pr-watch` drive the workflow by calling `vrg-pr-workflow next`
  (and `report-fixes`, etc.). Releasing tooling that deletes those subcommands
  while a repo still runs the old skills hard-fails the agent session
  mid-flight. Mitigated by sequencing: the plugin-skill update is a
  release-blocking predecessor (see "Release sequencing"). This is the most
  important coordination point — it is the exact mid-session-failure mode that
  motivated removing this machinery, so it must not recur during the removal.
- **Merge gate hang.** Relaxing `vergil-audit/approved` to non-required must
  land with this change (see §4 and "Release sequencing"), or PRs hang
  unmergeable once the loop that posted the check is gone.
- **Stray audit-mode VM resolving to `user`.** Not applicable while the
  identity is retained — `IdentityMode.AUDIT` still resolves normally. No
  change to VM provisioning.
- **Orphaned docstrings.** `vrg-reword` and `vrg-pr-fix-body` mention the now
  dormant judgment checks in prose only (no code dependency — verified). Left
  untouched here; scheduled for a stale-code sweep follow-up.

## Release sequencing

This change has two release-blocking predecessors that are *not* free to drift
as open-ended follow-ups. They must be ordered:

1. **First — `vergil-claude-plugin` skills (predecessor, must land before the
   tooling subcommand removals reach any repo).** Remove `/vergil:issue-audit`;
   simplify `/vergil:issue-implement` (drop the audit hand-off line and stop
   driving the workflow through `vrg-pr-workflow next`); simplify
   `/vergil:pr-watch` to USER-only. As part of this work, **audit that every
   consuming repo properly depends on v2.1 of the plugin**, so no repo runs
   stale skills against the new tooling. Until this lands and propagates, the
   tooling subcommand removals must not ship to consumers.
2. **With the tooling release — branch protection.** Relax
   `vergil-audit/approved` to non-required (org-side setting) so merges proceed
   on human approval + CI once the loop's check-poster is gone.
3. **Then — this work (#1872):** the `vrg-pr-workflow` collapse, transport/ABC
   trim, criteria relocation, docs, and tests.

## Out of scope — genuine deferred follow-up

- **Stale-code sweep** — re-evaluate whether `vrg-reword` and `vrg-pr-fix-body`
  are still needed, and scrub their dead-check docstring references. This is the
  only item that can safely defer; it neither blocks nor is blocked by the
  release sequencing above.
