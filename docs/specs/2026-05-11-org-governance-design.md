# VERGIL Org Governance Design

**Issue:** #717
**Date:** 2026-05-11
**Status:** Draft

## Problem

The VERGIL migration moves repositories from a personal GitHub account to
the `vergil-project` GitHub org. The existing security model — a single
human using a single PAT for all operations, with AI agents sharing those
credentials — is insufficient for an org that aims to accept external
contributions. The org needs a governance model that:

- Distinguishes human work from AI-generated work at the identity level
- Enforces human review of all changes before they reach protected branches
- Restricts AI agents to the minimum permissions required for development
- Scales from one contributor to many without redesigning the model
- Mechanizes administrative operations so AI agents never need human
  credentials

## Foundational Principles

1. **AI is a nondeterministic system operating in a space that demands
   deterministic results.** The governance model exists to bridge that gap.
2. **AI contributions are welcome. Unaccountable AI contributions are not.**
   Every change must have a human who reviewed it, understands it, and takes
   responsibility for it.
3. **The speed limit is human comprehension, not code generation.** AI agents
   can pile up PRs as fast as they want. Nothing ships until a human
   understands what it does and why.
4. **Where enforcement can be hard-gated, it must be. Where it cannot,
   compliance must be audited post-facto.** The system assumes good faith
   but verifies through auditing.

## Section 1: Identity Model

Each contributor has exactly two GitHub identities:

| Identity | Represents | Role |
|---|---|---|
| `<username>` | The human | Reviews, approvals, merges, administration, releases |
| `<username>-agent` | That human's AI agents | All development work, regardless of harness or model |

### Rules

- The human identity is the accountable party. The agent identity exists to
  make AI-assisted work distinguishable from human work.
- One agent identity per human — not per harness, not per model. Which
  harness or model produced the work is captured in metadata (commit
  trailers, PR descriptions), not at the identity level.
- Agent accounts are **outside collaborators** in the org, never org
  members. Org membership inherently signals "this is a human who is
  accountable."
- Creating an agent identity is a requirement for any contributor who uses
  AI tools. Contributors who do not use AI operate under the standard
  open-source model with their human identity only.
- "The AI did it" is not a defense. You are accountable for everything
  your agent produces.

### Naming Convention

The agent account name follows the pattern `<username>-agent`. This
convention is load-bearing — the cross-human review CI check (#719)
parses the PR author's username to identify the owning human.

### Metadata Capture

The specific harness and model used for a contribution are recorded in
commit trailers (e.g., `Co-Authored-By:`) and PR descriptions. This is
telemetry for auditing and analysis, not a security boundary. The
security boundary is human vs. not-human.

### Migration

The existing accounts `wphillipmoore-claude` and `wphillipmoore-codex`
are retired. A new `wphillipmoore-agent` account is created. Existing
commit history remains attributed to the old accounts (Git does not
rewrite attribution). All new work uses the `-agent` convention.

## Section 2: Branch Protection Rulesets

GitHub org-level rulesets define protection rules once and apply them
across all repos. This is a significant advantage over per-repo branch
protection — new repos automatically inherit the governance model.

### Protected Branches

`main` and `develop` in all repos.

### Rules for `develop`

- Require a pull request before merging (no direct pushes)
- Require at least 1 approving review
- Require review from someone other than the PR author
- Require status checks to pass (CI must be green)
- Require branches to be up to date before merging
- No bypass — not even for org owners

### Rules for `main`

All rules from `develop`, plus:

- Restrict who can push/merge to org owners only
- This is the release gate — only the mechanized release workflow
  (running under human credentials) can promote code to `main`

### Release Workflow and Branch Protection

The release workflow creates PRs that must satisfy the same review
rules as any other PR. Since the release tool runs under the human's
credentials, and the "require review from someone other than the PR
author" rule applies, the release PRs are opened by the human's
agent account and approved by the human. This means the release tool
uses the agent PAT to create the PR and the human PAT to approve and
merge it — maintaining the same author/reviewer separation that
governs all other PRs.

### No Bypass Policy

There are no bypass permissions for any identity, including org owners.
If a situation requires bypassing a rule, that is an exceptional
circumstance handled by temporarily modifying the ruleset — and it
represents a failure in the tooling or process that should be addressed.

Org owners are held to the same standards as everyone else. The act of
running the mechanized release tool (as a human, with human credentials)
is what satisfies the merge requirement for `main`, not a bypass.

### Cross-Human Review (Future, #719)

At scale of 2+ human contributors, a CI status check enforces that the
PR approver is a different human than the agent's owner. The check
parses the PR author against the `<username>-agent` naming convention
and rejects approval from the same human.

At scale of one, the check short-circuits (exit 0) — the single-human
model (agent authors, human reviews) is sufficient.

This check is an absolute requirement the moment a second human joins
the org.

## Section 3: Credential Management

### Current State

A single `GH_TOKEN` is exported globally in the shell environment. All
operations — human and AI — authenticate as the same GitHub user. This
provides no identity separation and no enforcement capability.

### New Model

No `GH_TOKEN` in the shell environment by default. Two PATs per
contributor, stored securely, selected per-operation based on role.

### Credential Storage

Two fine-grained PATs per contributor, stored in a secure credential
store (macOS Keychain or equivalent):

- **Human PAT** — full org owner/member privileges, used for
  administration, approvals, merges, and releases
- **Agent PAT** — scoped to the minimum permissions required for
  development:
  - Contents: Write (push branches, create commits)
  - Pull requests: Write (open and update PRs)
  - Issues: Write (create and update issues)
  - Scoped to repos in the `vergil-project` org only
  - Explicitly excluded: administration, actions, merge via API,
    org settings, secrets, deployments

### Credential Selection

- AI agent sessions receive the agent PAT at launch time (via session
  launch mechanism — shell wrapper, Claude Code hook, or equivalent)
- Administrative and release tooling retrieves the human PAT from the
  credential store
- **Development tools never touch the human token. Administrative tools
  never touch the agent token.**

### The `gh` CLI

The `gh` CLI (which the Vergil tools shell out to) respects `GH_TOKEN`
as its authentication source. Credential selection can be implemented
by setting `GH_TOKEN` to the appropriate value before invoking `gh`. No
changes to `gh` itself are needed.

### Enforcement Model

- **Hard gates:** PAT scope restrictions (agent PATs cannot perform admin
  operations), branch protection rulesets (agent accounts cannot merge),
  required reviews (agent PRs require human approval)
- **Post-facto auditing:** Verify that contributors used the correct
  credentials for the type of work performed. The org audit log tracks
  the authenticated actor for every operation — discrepancies between
  the actor and the expected role are flagged.

### For Future Contributors

The pattern is documentable: create a `<username>-agent` account,
generate two scoped PATs, store them in your credential manager, and
the tooling selects the right one. Contributors who do not use AI agents
have one token and everything works normally.

## Section 4: Release Workflow Mechanization

The release workflow is the one process that currently creates and merges
PRs autonomously. Under the new model, it must run entirely under human
credentials.

### Current Flow (AI-Assisted, Honor System)

1. AI agent runs `st-prepare-release` — creates release branch, updates
   changelog, opens PR to `main`
2. AI agent runs `st-merge-when-green` — waits for CI, merges to `main`
3. AI agent opens back-merge PR from `main` to `develop`, merges it

### New Flow (Fully Mechanized, Human Credentials)

1. Human runs `vrg-release` — a single Python command that orchestrates
   the entire release
2. It retrieves both credentials from the credential store
3. Using the agent PAT: creates the release branch, updates changelog,
   opens PR to `main`
4. It waits for CI to pass
5. Using the human PAT: approves and merges the PR to `main`
6. Using the agent PAT: creates the back-merge PR to `develop`
7. Using the human PAT: approves and merges the back-merge PR
8. Using the human PAT: tags the release, cleans up branches

### Key Properties

- The entire release is triggered by one human decision: running the
  command
- PR creation uses the agent PAT (so the author is the agent),
  approval and merge use the human PAT (so the reviewer is the
  human) — branch protection is satisfied by the same
  author/reviewer separation that governs all other PRs
- No AI agent *decision-making* at any point — this is deterministic,
  mechanical code promotion, even though it uses the agent identity
  for PR authorship
- The dependency update step (which currently involves AI judgment)
  must also be mechanized — deterministic resolution, not AI
  interpretation
- Fully covered by unit and integration tests

### Impact on Existing Tools

- `st-merge-when-green` in its current form (callable by AI agents)
  is no longer a standalone operation for normal development — PRs
  wait for human approval and human-initiated merge
- It may survive as an internal function called by the mechanized
  release tool, but it is no longer an AI-accessible entry point
- `st-prepare-release` evolves into the front half of `vrg-release`

### The Broader Principle

If an operation requires human credentials, it must be fully
mechanized. No AI agent should ever need the human token, and no
human should have to do mechanical button-clicking that could be
scripted. This raises the bar on administrative operations — they
must be deterministic, tested, and scriptable.

## Section 5: Contributor Guidelines

### For Contributors Using AI

- You must create a `<username>-agent` GitHub account for AI-assisted
  development
- All AI-assisted work must be committed and PR'd under the agent
  identity
- All reviews, approvals, and merges are performed under your human
  identity
- You are accountable for everything your agent produces
- PRs must pass CI (the full validation pipeline)
- At 2+ human contributors, cross-human review is required — you cannot
  approve your own agent's PRs (#719)

### For Contributors Not Using AI

- Standard open-source contribution model: fork, branch, PR, review
- PRs require approval from an org member
- No requirement to create an agent account

### What Is Not Yet Defined

These items are deferred as future work:

- How external contributors' AI agents are handled (must they follow
  the same identity convention, or is that only for org members?)
- Detailed auditing procedures for verifying credential compliance
- Automated tooling for evaluating AI-generated contributions during
  code review

## Section 6: Org-Level Configuration

### Org Security Settings

- Require two-factor authentication for all org members
- Default repository permission for org members: **Write** (push
  branches; merge is gated by rulesets, not repo permissions)
- Outside collaborators (agent accounts): granted **Write** on
  specific repos as needed
- Do not allow forking of private repos (if any exist)

### Org-Level Rulesets

- `main` and `develop` are protected in every repo
- Required PR reviews, required CI status checks, no bypass
- New repos automatically inherit the governance model without
  per-repo configuration

### Default Community Health Files

These live in the `.github` profile repo (#718):

- `CONTRIBUTING.md` — contributor guidelines from Section 5
- Pull request template
- Issue templates
- Org-level README

Any repo without its own versions inherits the org defaults.

### Audit Log

GitHub org audit logs track who did what across all repos. This is
the backstop for the trust-but-verify model: if someone uses the
wrong credentials, the audit log shows the discrepancy between the
authenticated actor and the expected role.

### What Stays Per-Repo

- CI workflow files (`.github/workflows/`)
- CODEOWNERS files (when the team grows enough to need path-based
  review assignment)
- Repo-specific branch protections beyond org defaults (unlikely
  to be needed initially)

## Dependencies

- VERGIL rename (#717 context, separate plan) — org must exist before
  governance is applied
- `.github` profile repo (#718) — community health files and org README
- Cross-human review CI check (#719) — required at humans > 1
- Release workflow mechanization — required before branch protection
  rules can be fully enforced without breaking the release flow

## Risks

| Risk | Mitigation |
|---|---|
| Agent PAT scope too narrow, blocking legitimate development operations | Start with the documented scope, widen if specific operations fail — prefer fixing tooling over widening scope |
| Release workflow not mechanized before branch protection is enforced | Phase the rollout: enable rulesets after `vrg-release` is functional |
| Contributors resist the two-identity requirement | Document the rationale clearly; the bar for contribution is higher than most OSS projects, and that is intentional |
| Single human bottleneck for all reviews | Acceptable at scale of one; at scale of 2+, cross-review distributes the load |
| Credential management adds friction to development workflow | Invest in tooling that makes credential selection invisible — the developer should not think about tokens |
