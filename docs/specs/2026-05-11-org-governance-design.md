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

There are three categories of GitHub identity in the org:

| Identity | Represents | Role |
|---|---|---|
| `<username>` | The human | Reviews, approvals, merges, administration |
| `<username>-agent` | That human's AI agents | All development work, regardless of harness or model |
| `vergil-release[bot]` | The org's GitHub App | Mechanized automation (release PRs, version bumps) |

Each contributor has the first two. The third is an org-level GitHub App
shared by all contributors.

### Rules

- The human identity is the accountable party. The agent identity exists to
  make AI-assisted work distinguishable from human work.
- One agent identity per human — not per harness, not per model. Which
  harness or model produced the work is captured in metadata (commit
  trailers, PR descriptions), not at the identity level.
- Agent accounts are **outside collaborators** in the org, never org
  members. Org membership inherently signals "this is a human who is
  accountable."
- The GitHub App identity is used exclusively for mechanized automation —
  deterministic workflows with no AI decision-making. PRs authored by the
  App are visually distinct (`[bot]` suffix) and immediately communicate
  "no human or AI authored this."
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

The co-author entries in the project config also migrate: the
per-harness entries (`claude`, `codex`) are replaced with a single
`agent` entry. This is coordinated with the VERGIL rename (which
changes the config filename) so both changes land together.

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
- No standing bypass permissions (see No Standing Bypass Policy)

#### Why "Require Up to Date" Is Retained

The "require branches to be up to date" rule guarantees that CI ran
on the exact code that will land on the target branch. Without it,
a PR can pass CI against a stale version of `develop` — another PR
merges, changing the baseline, and the first PR's CI results no
longer reflect reality. Two PRs that touch unrelated code can still
break each other through transitive dependencies, shared state, or
configuration changes. Dropping this rule opens a correctness hole
that contradicts the project's commitment to extreme hygiene and
full coverage.

The cost is merge serialization: at scale of multiple active
contributors, only one PR can merge at a time, and the rest must
rebase and re-run CI after each merge. At scale of one, this cost
is negligible.

**Future mitigation: GitHub merge queue.** GitHub Teams (paid plan)
offers a merge queue feature that eliminates the serialization
penalty while preserving the safety guarantee. The merge queue
batches approved PRs, creates a combined test branch, runs CI once
on the batch, and merges them together. If CI fails, it bisects to
find the broken PR. This is the correct long-term solution — it
closes the correctness hole without the serialization cost. When the
org moves to a paid plan, merge queue should be enabled on `develop`
and `main` as a priority.

### Rules for `main`

All rules from `develop`, plus:

- Restrict who can push/merge to org owners only
- This is the release gate — only the mechanized release workflow
  (running under human credentials) can promote code to `main`

### Release Workflow and Branch Protection

The release workflow creates PRs that must satisfy the same review
rules as any other PR. Release PRs are authored by the org's GitHub
App (`vergil-release[bot]`) and approved by the human who triggered
the release. The App identity is distinct from both the human and
the agent, so the "require review from someone other than the PR
author" rule is satisfied without reusing any contributor's
credentials for a purpose they weren't designed for.

### No Standing Bypass Policy

No identity has standing bypass permissions, including org owners.
Org owners are held to the same standards as everyone else. The act
of running the mechanized release tool (as a human, with human
credentials) is what satisfies the merge requirement for `main`, not
a bypass.

If a situation requires circumventing a rule, the org owner must
temporarily modify the ruleset, perform the operation, and restore
the ruleset. This is a deliberate friction — the cost of modifying
rules should be high enough to discourage casual use but not so high
that it blocks incident response. Ruleset modifications are logged
in the org audit trail. Any modification that enables a merge
without the normal review process is treated as an incident to be
retrospected: what broke, why the normal path couldn't be used, and
what tooling or process change would prevent recurrence.

### Cross-Human Review (Future, #719)

At scale of 2+ human contributors, a CI status check enforces that the
PR approver is a different human than the agent's owner. The check
parses the PR author against the `<username>-agent` naming convention
and rejects approval from the same human.

PRs authored by the GitHub App (`vergil-release[bot]`) are exempt from
cross-human review — they are mechanized automation, not AI work, and
any org member's human identity may approve them.

At scale of one, the check short-circuits (exit 0) — the single-human
model (agent authors, human reviews) is sufficient.

This check is an absolute requirement the moment a second human joins
the org.

## Section 3: Credential Management

> **Superseded.** This section is superseded by the credential
> management design spec
> (`docs/specs/2026-05-14-credential-management-design.md`, #775).
> The approach described below (fine-grained PATs, custom keychain
> management) was replaced with classic PATs managed through
> `gh auth`, with credential selection enforced by the `vrg-gh`
> wrapper. The content below is retained as historical context.

### Current State

A single `GH_TOKEN` is exported globally in the shell environment. All
operations — human and AI — authenticate as the same GitHub user. This
provides no identity separation and no enforcement capability.

### New Model

No `GH_TOKEN` in the shell environment by default. Two PATs per
contributor plus a shared GitHub App for mechanized automation,
selected per-operation based on role.

### Per-Contributor Credential Storage

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

### GitHub App (Mechanized Automation)

A GitHub App (`vergil-release`) is installed org-wide and used
exclusively for mechanized automation — operations that are
deterministic and involve no AI decision-making.

**App permissions:**

- Contents: Write (create branches, push commits)
- Pull requests: Write (create and update PRs)
- Metadata: Read (required by GitHub)

The App does not need merge, administration, or approval permissions.
Approval and merge are always performed by a human PAT.

**Private key storage:**

The App's private key is stored in the secure credential store
alongside the PATs. Contributors who run releases need access to it.
At scale of one, this is a single entry in the macOS Keychain. At
scale of many, the key is distributed via a shared secret store.

**Token exchange:**

The release tool reads the private key, generates a short-lived JWT,
and exchanges it for a GitHub App installation token via the GitHub
API. Installation tokens expire after 1 hour — there are no
long-lived automation credentials. The token exchange is a small
function in the release tool (~20 lines of Python with `PyJWT`).

**CI usage:**

GitHub Actions workflows already use this pattern via
`actions/create-github-app-token`. The existing App (currently in
the personal account) is migrated to the `vergil-project` org as
part of the VERGIL rename. The CI and local release tool use the
same App with the same private key.

### Credential Tooling

#### Core Principle

**The tool selects the credential, not the developer.** No global
`GH_TOKEN`, no manual env var switching. Each Vergil tool knows
which role it operates in and retrieves the correct credential from
the platform's secure store at invocation time. The developer's
only responsibility is a one-time setup: store the credentials
using a setup utility.

This means all GitHub-facing operations flow through Vergil
tooling (`vrg-*`). Raw `gh` and `git` commands are fine for
read-only use (inspecting state, triaging problems), but any
operation that authenticates against the GitHub API should go
through a tool that enforces correct credential context. This is
the long-term direction — not an immediate ban on raw `gh`, but
the design principle that drives tooling decisions.

#### Standard Credential Names

All platforms use the same logical names for credential entries.
The secure store backend varies by platform, but the names are
consistent:

| Credential | Store entry name | Used by |
|---|---|---|
| Human PAT | `vergil/human-pat` | Admin tools, approval/merge steps |
| Agent PAT | `vergil/agent-pat` | Development tools, AI agent sessions |
| App private key | `vergil/app-private-key` | Release tool (PR creation step) |
| App ID | `vergil/app-id` | Release tool (token exchange) |

#### Platform-Specific Secure Storage

Three platforms are supported. Contributors on other platforms are
responsible for porting the mechanism; the tooling provides a
pluggable backend interface.

| Platform | Backend | Lookup mechanism |
|---|---|---|
| macOS | Keychain | `security find-generic-password` (or `keyring` Python library) |
| Linux | Secret Service (GNOME Keyring, KWallet) | `keyring` Python library via `SecretService` backend |
| Windows | Windows Credential Manager | `keyring` Python library via `WinVault` backend |

The `keyring` Python library provides a unified API across all
three platforms. The Vergil tooling uses `keyring` as the
abstraction layer, with the platform-native backend selected
automatically. Contributors store credentials once via a setup
command (`vrg-setup-credentials` or equivalent) that prompts for
each token and writes it to the platform's secure store under the
standard names.

#### Credential Isolation by Context

Each tool category retrieves only the credential it needs. No tool
has access to credentials outside its role:

- **Development tools** (`vrg-commit`, `vrg-submit-pr`, etc.) —
  retrieve the agent PAT. Never touch the human PAT or App key.
- **AI agent sessions** — launched with the agent PAT injected via
  session launch hook (Claude Code hook, shell wrapper, or
  equivalent). The hook retrieves the agent PAT from the secure
  store and sets `GH_TOKEN` for the session. The human PAT and App
  key are never exposed to the agent session.
- **Administrative tools** — retrieve the human PAT. Never touch
  the agent PAT.
- **Release tool** (`vrg-release`) — retrieves both the human PAT
  and the App private key. Sets `GH_TOKEN` per-invocation: the App
  installation token for PR creation steps, the human PAT for
  approval and merge steps. Credentials are scoped to the
  subprocess, not the parent shell.
- **The App identity is never used for development or
  administration.** Development tools never touch the human token
  or the App key. Administrative tools never touch the agent token.

#### The `gh` CLI

The `gh` CLI respects `GH_TOKEN` as its authentication source.
Vergil tools set `GH_TOKEN` in the subprocess environment when
invoking `gh` — the token is scoped to that invocation, not
exported to the parent shell. The `gh auth login` state is not
used; if `GH_TOKEN` is absent, the tool fails with an explicit
error rather than falling back to ambient credentials.

#### What the Tooling Cannot Enforce

The credential tooling controls what happens inside Vergil tools.
It cannot prevent a contributor from globally exporting a token in
their shell profile or using the wrong credential with raw `gh`
commands. These scenarios are addressed by:

- **Guidelines and setup utilities** that make the correct path
  easy and the incorrect path require deliberate effort
- **Post-facto auditing** via the org audit log, which reveals
  when operations were performed under an unexpected identity
- **GitHub-side hard gates** (PAT scope, branch protection) that
  limit the damage from credential misuse regardless of what
  happens on the developer's machine

### Enforcement Model

- **Hard gates:** PAT scope restrictions (agent PATs cannot perform admin
  operations), App permission restrictions (the App cannot merge or
  approve), branch protection rulesets (agent accounts and the App
  cannot merge), required reviews (all PRs require human approval)
- **Post-facto auditing:** Verify that contributors used the correct
  credentials for the type of work performed. The org audit log tracks
  the authenticated actor for every operation — discrepancies between
  the actor and the expected role are flagged. The three-identity model
  (human, agent, App) makes misuse visible: an agent token performing
  admin work or a human token creating development PRs both stand out.

### Credential Lifecycle

#### Rotation Schedule

All fine-grained PATs are created with a 1-year expiration. The App
private key does not expire automatically and is rotated on the same
annual cadence.

Rotation is proactive, not reactive — credentials are refreshed
before they expire, not after they break something. The governance
tooling includes a credential audit command (`vrg-credential-audit`
or equivalent) that reports:

- All PATs and App keys in use across the org
- Expiration dates and time remaining
- Last-used timestamps (via GitHub API token metadata)
- Stale credentials: tokens that exist but have not been used in a
  configurable window (e.g., 90 days) — these indicate inactive
  contributors whose access should be reviewed
- Approaching expiration: tokens within 30 days of expiry trigger a
  warning

This audit runs on a regular schedule (monthly or as a CI job) and
surfaces a report to org owners. The goal is that no credential
expires unexpectedly during work — expiration during an active
session represents a failure in the audit and reporting process.

#### Compromise Response

Upon reported or suspected compromise of any credential:

1. **Immediately revoke** the compromised credential (PAT or App
   private key) via GitHub settings
2. **Halt operations** that depend on the compromised credential —
   no new PRs, merges, or releases until replacement credentials
   are provisioned
3. **Audit recent activity** under the compromised identity using the
   org audit log — review all operations performed since the
   estimated compromise window
4. **Issue replacement credentials** with the same scope and
   expiration policy
5. **Retrospect** — how was the credential exposed, and what process
   or tooling change prevents recurrence

The blast radius depends on which credential is compromised:

| Credential | Blast radius | Immediate risk |
|---|---|---|
| Agent PAT | Can push branches, create PRs and issues | Branch protection prevents merging; spam PRs and malicious branch content are possible |
| Human PAT | Can approve, merge, administer | Full access to protected branches and org settings |
| App private key | Can create PRs as automation identity | Cannot merge or approve; similar to agent PAT but org-wide |

Human PAT compromise is the most severe — it should trigger
immediate rotation of all credentials for that contributor and a
full audit of recent merge and admin activity.

#### Observability

The credential audit is not a one-time check — it is ongoing
observability into the org's credential namespace. Over time, this
surfaces patterns that inform governance decisions:

- Contributors whose tokens are never used may be stale and should
  have access reviewed
- Contributors who use tokens heavily may need broader scope or
  dedicated tooling support
- Unusual usage patterns (agent token performing admin-like
  operations, human token creating development PRs) indicate
  credential misuse even when the operations succeed

The org audit log is the primary data source. GitHub exposes token
metadata (last used, permissions) via the API, which the credential
audit tool consumes.

### For Future Contributors

The pattern is documentable: create a `<username>-agent` account,
generate two scoped PATs, store them in your credential manager, and
the tooling selects the right one. The GitHub App private key is
shared with contributors who need to run releases. Contributors who
do not use AI agents have one token and everything works normally.

## Section 4: Release Workflow Mechanization

The release workflow is the one process that currently creates and merges
PRs autonomously. Under the new model, it is fully mechanized — PR
creation runs under the GitHub App identity, and approval and merge run
under the human's credentials. No AI decision-making is involved.

### Current Flow (AI-Assisted, Honor System)

1. AI agent runs `st-prepare-release` — creates release branch, updates
   changelog, opens PR to `main`
2. AI agent runs `st-merge-when-green` — waits for CI, merges to `main`
3. AI agent opens back-merge PR from `main` to `develop`, merges it

### New Flow (Fully Mechanized, Human Credentials)

1. Human runs `vrg-release` — a single Python command that orchestrates
   the entire release
2. It retrieves the human PAT and the GitHub App private key from the
   credential store
3. It generates a short-lived App installation token via JWT exchange
4. Using the App token: creates the release branch, updates changelog,
   opens PR to `main`
5. It waits for CI to pass
6. Using the human PAT: approves and merges the PR to `main`
7. Using the App token: creates the back-merge PR to `develop`
8. Using the human PAT: approves and merges the back-merge PR
9. Using the human PAT: tags the release, cleans up branches

### Key Properties

- The entire release is triggered by one human decision: running the
  command
- PR creation uses the GitHub App (so the author is
  `vergil-release[bot]`), approval and merge use the human PAT (so
  the reviewer is the human) — branch protection is satisfied by the
  author/reviewer separation, and the audit trail cleanly
  distinguishes mechanized automation from both human and AI work
- No AI agent *decision-making* at any point — this is deterministic,
  mechanical code promotion, and the App identity makes that visible
- The App installation token expires after 1 hour — if the release
  takes longer than expected, the tool re-generates the token
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
  the same identity convention, or is that only for org members?).
  This is a prerequisite for the cross-human review check (#719) —
  the check parses the `<username>-agent` naming convention to
  identify the owning human, so #719 must account for contributors
  who may not follow the convention.
- Community policies for non-AI contributors (standard open-source
  contribution model, but the review and quality bar needs defining)
- Detailed auditing procedures for verifying credential compliance
- Automated tooling for evaluating AI-generated contributions during
  code review

### What Is Explicitly Out of Scope

Autonomous AI contributions — AI agents operating without a human
in the loop — are not supported. Every contribution must have a
human who directed the work, reviewed the output, and takes
accountability for the result. An unaccountable AI cannot be held
responsible for what it produces, and this project's governance
model is built on the assumption that humans run things. This is
not a temporary constraint pending better AI tooling; it is a
foundational design decision.

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
- Required PR reviews, required CI status checks, no standing bypass
- New repos automatically inherit the governance model without
  per-repo configuration

### GitHub App

A GitHub App (`vergil-release`) is registered under the org and
installed org-wide. New repos automatically receive the App's
permissions without per-repo configuration.

- **App ID and private key** are stored as org-level Actions secrets
  (`APP_ID`, `APP_PRIVATE_KEY`) for CI workflows, and in the secure
  credential store for local release tool use
- The existing GitHub App (currently on the personal account) is
  migrated to the org as part of the VERGIL rename — this is not a
  new App, it is the same App re-homed
- App permissions are limited to Contents:Write, Pull requests:Write,
  and Metadata:Read — it cannot merge, approve, administer, or
  access secrets

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

## Section 7: Tooling Impact

### Configuration Enforcement Tools

The current `st-github-config` tool evolves into two separate tools
with distinct operational cadences:

**`vrg-org-config`** (new) — manages org-level settings:

- Org security settings (2FA requirement, default permissions)
- Org-level rulesets (branch protection, required reviews, no
  standing bypass) that cascade to all repos
- Outside collaborator defaults
- GitHub App installation verification
- Run rarely — org-level configuration changes infrequently

**`vrg-repo-config`** (evolution of `st-github-config`) — manages
per-repo settings:

- Repository settings (merge strategies, wiki, projects, etc.)
- Security settings (secret scanning, push protection)
- Actions permissions (allowed actions patterns)
- CI gate rulesets (language-specific required status checks)
- Tag protection rulesets
- For personal repos (not in an org): also manages branch protection
  rulesets, since there is no org layer to cascade from

The tool detects whether the repo is in an org at runtime by
querying the GitHub API (`owner.type`), as it already does today.
The `is_org` flag determines the scope of enforcement — org repos
get a narrower per-repo scope because the governance enforcement
is handled by the org layer above.

### Naming Convention

Tool names abstract away "GitHub" — `org-config` and `repo-config`
rather than `github-config`. This keeps the door open for future
forge portability (e.g., Gitea) without renaming the tools.

### Uniform Rigor

The same governance standard applies to all repos, personal or org.
The `is_org` flag determines the *mechanism* of enforcement (org-level
rulesets vs. per-repo branch protection), not the *standard*. A
personal repo receives the same branch protection rules as an org
repo — the tool simply manages them at the repo level instead of
relying on org-level cascading.

### Escape Hatch Removal

The `skip-rulesets` override in `[github]` config is removed. There
are no escape hatches. If a repo cannot comply with the standard
rulesets, the correct response is to fix the repo or the tooling,
not to bypass enforcement. The override was added for a repo that
is being archived and is not used by any active repo.

### Configuration File Changes

No new fields are added to `vergil.toml` for org detection — the
tool queries the GitHub API at runtime. The `[github]` section
loses the `skip-rulesets` field and gains no replacements. The
long-term direction is for `vergil.toml` to declare what the repo
*is* (language, CI config, project metadata), not how the repo is
*managed* — management policy is hardcoded and uniform.

## Dependencies

- VERGIL rename (#717 context, separate plan) — org must exist before
  governance is applied
- `.github` profile repo (#718) — community health files and org README
- Cross-human review CI check (#719) — required at humans > 1
- GitHub App migration — existing App re-homed to `vergil-project` org
  as part of the VERGIL rename
- Release workflow mechanization — required before branch protection
  rules can be fully enforced without breaking the release flow

## Risks

| Risk | Mitigation |
|---|---|
| Agent PAT scope too narrow, blocking legitimate development operations | Start with the documented scope, widen if specific operations fail — prefer fixing tooling over widening scope |
| Release workflow not mechanized before branch protection is enforced | Phase the rollout: enable rulesets after `vrg-release` is functional |
| GitHub App private key distribution at scale | At scale of one, the key is in the macOS Keychain. At scale of many, use a shared secret store. The key is only needed by contributors who run releases, not all contributors |
| App installation token expires mid-release | The release tool detects expiry and re-generates the token. Installation tokens live 1 hour; releases complete in minutes |
| Contributors resist the two-identity requirement | Document the rationale clearly; the bar for contribution is higher than most OSS projects, and that is intentional |
| Merge serialization on `develop` at scale | Acceptable at scale of one. At scale of many, enable GitHub merge queue (requires Teams plan) to batch PRs without losing the "up to date" guarantee |
| Single human bottleneck for all reviews | Acceptable at scale of one; at scale of 2+, cross-review distributes the load |
| Credential management adds friction to development workflow | Invest in tooling that makes credential selection invisible — the developer should not think about tokens |
