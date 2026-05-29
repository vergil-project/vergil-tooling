# Agent Permission Model and Identity Architecture

**Date:** 2026-05-29
**Status:** Draft
**Issue:** #1289

## Motivation

Moving agents from the developer's host into VMs with GitHub App
credentials exposed a gap: the App needed `workflows` permission to
push changes to `.github/workflows/` files. Workflow files define
CI/CD enforcement and containment — granting agents the ability to
modify them creates a potential escape hatch that undermines the
security controls those workflows enforce.

This discovery triggered a broader review. The agent had been
operating with the human's full permissions, and the move to App
credentials was the first time those permissions were explicitly
scoped. The question became: for every write permission the agent
holds, can we remove it and encapsulate the need in a
human-controlled utility?

Three problems converge:

1. **Workflow permission is a security escape hatch.** An agent that
   can modify CI/CD workflows can weaken the containment that
   protects everything else.

2. **Agents are unreliable at operational procedures.** They skip
   steps, close issues prematurely, and drift from prescribed
   workflows. Mechanized CLI utilities (like `vrg-release`) are
   the proven alternative.

3. **Human review at AI production speed is impractical.** The rate
   at which agents produce code exceeds what humans can review
   line-by-line. Mechanical CI gates catch rule violations, but
   contextual quality (naming conventions, architectural
   consistency, coding standards compliance) requires LLM-based
   review.

## Design Principles

**Minimize agent write permissions.** Every write permission granted
to the agent must have a documented, real-world justification. Read
access is generally acceptable; write access is the attack surface.

**Server-side enforcement is the real boundary.** Client-side
controls (vrg-git/vrg-gh wrappers, hook guards) are defense-in-depth
but not adversary-resistant. GitHub's permission model is the hard
gate.

**Mechanize operational procedures.** Agents write code. Humans
trigger operational tools. Every step that "really matters and causes
damage" is encapsulated in a `vrg-*` CLI utility that the human
invokes. `vrg-release` is the model.

**The human switches, the agent doesn't.** Agents never decide to
escalate their own privileges. The human makes a conscious decision
to use elevated-access tooling, which concentrates attention on the
higher-risk work.

**Correctness over cost.** Token cost is a real constraint. Granting
agents the ability to perform trivial operations (closing issues,
editing labels) that the human can do with a few clicks is
wasteful. The system should be designed to minimize token expense,
not maximize agent autonomy.

## The Racetrack Model

The identity architecture uses a motorsport analogy as its conceptual
framework. This is not decoration — the analogy maps cleanly to the
trust relationships and decision authority in the system, and the
documentation uses these terms as the primary vocabulary.

| Racing role | Description | Vergil identity |
|---|---|---|
| **Driver** | On track, makes tactical decisions, follows the rules | `<user>-vergil-user` — daily development agent |
| **Track mode** | Driver with safety systems off, Race Director on radio | `<user>-vergil-admin` — elevated rights, human supervising |
| **Officials** | Walk pit lane, observe, report infractions — cannot penalize | `<user>-vergil-audit` — reviews PRs, comments on violations |
| **Race Director** | Full authority over the event, makes the calls | `<user>` — human, ultimate authority |

**Driver (street mode):** The agent in its normal operating mode.
Restricted write permissions, safe for autonomous and unattended
work. The safety nannies are on. This is the 24/7 mode.

**Track mode:** The same driver in a more powerful car. The human
(Race Director) made the conscious decision to use the elevated-access
VM. Attention is concentrated. You don't use this for routine work —
you use it for specific tasks that require elevated rights, then shut
it down. Like turning off traction control at the racetrack: you
accept more risk, so your behavior adjusts accordingly.

**Officials (audit mode):** An independent observer that can only
watch and report. Officials walk pit lane looking for violations.
They can flag "you dropped a lug nut" but cannot issue penalties
themselves — they report to Race Control (the human). The audit
identity reads code, reviews PRs against standards, and comments.
It cannot modify code, create PRs, or close issues.

**Race Director (human):** The ultimate authority. Reviews reports
from Officials, makes merge/release/strategy decisions, triggers
operational tools. No one overrules the Race Director. The Driver
and Officials never interact directly — everything goes through
Race Control.

## Identity Architecture

### GitHub Accounts

| Identity | Account pattern | Collaborator access | Purpose |
|---|---|---|---|
| Driver | `<user>-vergil-user` | Outside collaborator, Write | Daily development |
| Admin | `<user>-vergil-admin` | Outside collaborator, Write | Elevated operations |
| Audit | `<user>-vergil-audit` | Outside collaborator, Read | PR review |
| Human | `<user>` | Owner/Admin | Ultimate authority |
| Release bot | `vergil-release[bot]` | GitHub App | Release automation |

### GitHub App Configuration

Three separate GitHub Apps, each with distinct permission sets.
Each VM gets exactly one App's credentials. The credential
environment determines the operating mode — no config flag, no
mode switch command.

**Street-mode App (`vergil-app`):**

| Permission | Level | Rationale |
|---|---|---|
| `contents` | write | Push feature branches |
| `issues` | write | Create and comment on issues |
| `pull_requests` | read | View PRs, check status |
| `metadata` | read | Required baseline |
| `workflows` | none | Server-side blocks workflow file pushes |

**Track-mode App (`vergil-admin-app`):**

| Permission | Level | Rationale |
|---|---|---|
| `contents` | write | Push feature branches |
| `issues` | write | Create and comment on issues |
| `pull_requests` | read | View PRs, check status |
| `metadata` | read | Required baseline |
| `workflows` | write | Push workflow file changes under human supervision |

**Audit-mode App (`vergil-audit-app`):**

| Permission | Level | Rationale |
|---|---|---|
| `contents` | read | Read code for review |
| `issues` | read | Context for understanding PRs |
| `pull_requests` | write | Comment on and review PRs |
| `metadata` | read | Required baseline |
| `workflows` | none | No need |

The audit App has an inverted permission shape compared to street
mode: more PR permission (write vs. read) but less code permission
(read vs. write). The permissions are shaped exactly for the role.

### VM Architecture

```text
Human host
├── Street-mode VM  (daily use, always available)
│   ├── VRG_APP_ID → vergil-app
│   ├── VRG_PRIVATE_KEY_PATH → vergil-app key
│   └── Agent operates as <user>-vergil-user
│
├── Track-mode VM  (on-demand, human supervising)
│   ├── VRG_APP_ID → vergil-admin-app
│   ├── VRG_PRIVATE_KEY_PATH → vergil-admin-app key
│   └── Agent operates as <user>-vergil-admin
│
└── Audit VM  (persistent or on-demand)
    ├── VRG_APP_ID → vergil-audit-app
    ├── VRG_PRIVATE_KEY_PATH → vergil-audit-app key
    └── Agent operates as <user>-vergil-audit
```

The human launches each VM deliberately. There is no mode-switching
within a running session. The visual presentation of each VM must be
distinct — the human must never be uncertain about which mode they
are in. Specific visual treatment (color scheme, prompt indicator,
login banner) is an implementation detail, but the requirement is
ambient, persistent differentiation.

## The `.vergil/` Scratch Convention

A gitignored scratch directory at the worktree root, used for staging
structured data between the agent and the human. Not committed, not
tracked — purely a communication channel.

```text
<worktree>/
  .vergil/
    pr-template.yml    ← agent writes, vrg-submit-pr reads
    (future scratch files as needs arise)
```

Every repo's `.gitignore` includes `.vergil/`. The directory is
created on demand by whichever tool writes to it first.

### PR Template Format

```yaml
# .vergil/pr-template.yml
# Generated by agent — review and edit before running vrg-submit-pr
issue: 1289
title: "feat(permissions): remove workflow permission from street-mode App"
summary: |
  Removes the workflows permission from the street-mode GitHub App
  and adds vrg-gh restrictions to block issue close/reopen/edit
  in street mode.
linkage: Ref
notes: |
  This PR does not include workflow file changes.
```

Fields map directly to `vrg-submit-pr` arguments. The agent fills
them in; the human can inspect and edit before running the command.

### Template Lifecycle

The template file is ephemeral:

1. Agent writes `.vergil/pr-template.yml`.
2. If the file already exists, the agent warns and overwrites —
   a leftover template indicates a previous cycle was not completed.
3. Human reviews and optionally edits the file.
4. Human runs `vrg-submit-pr`, which reads and deletes the file.
5. The file should never persist between agent→human cycles.

## Revised Agent Workflow

### Street Mode (Daily Development)

```text
Agent                              Human (Race Director)
──────                             ─────────────────────
1. Write code
2. Commit (vrg-commit)
3. Push branch (vrg-git push)
   └─ Server rejects if workflow
      files changed (tripwire)
4. Write .vergil/pr-template.yml
5. Signal: "ready for PR"
                                   6. Review template, edit if needed
                                   7. Run vrg-submit-pr (creates PR)
                                   8. Review PR + CI results
                                   9. Merge (vrg-finalize-pr)
```

The agent's boundary is step 5. Everything after that is a human
operation triggered by `vrg-*` CLI tools.

### Track Mode (Elevated Operations)

Same workflow, except step 3 succeeds for workflow file changes
because the admin App has `workflows:write`. The human is actively
supervising because they made the conscious decision to use the
track-mode VM.

### Audit Mode (PR Review)

The triggering mechanism is a separate design problem (see Open
Questions). For v1, the human directs the audit agent to review
specific PRs or branches.

The audit agent can perform reviews either pre-submission (human asks
"review this branch before I submit the PR") or post-submission
(human asks "review PR #1234"). Pre-submission review is preferred
because issues get caught before the PR exists, and the development
agent can fix them before submission.

## `vrg-submit-pr` Changes

The tool gains a new operating mode driven by the `.vergil/`
template.

**Template mode (no CLI args):**

1. Read `.vergil/pr-template.yml` — fatal error if absent.
2. Show the human a summary: title, body preview, issue linkage,
   target branch, file list on the branch.
3. Prompt for confirmation.
4. Create the PR via `gh pr create`.
5. Delete the template file.
6. Print the PR URL.

**CLI argument mode (args provided):**

The existing argument-based invocation (`vrg-submit-pr --issue 1289
--title ...`) continues to work for direct human use without a
template. This supports the case where the human is making emergency
changes without an agent.

**Neither template nor args:** Fatal error. The tool does not guess.

**Credential selection:** `vrg-submit-pr` is a human-triggered tool.
When the street-mode App has `pull_requests: read`, the server-side
rejects PR creation attempts regardless of client-side credential
selection. This is the hard gate. The tool's credential logic is
defense-in-depth — it should select the human's PAT when creating
PRs, but even if it selected the wrong credential, the server would
reject it. The agent never invokes `vrg-submit-pr` directly.

## `vrg-finalize-pr` (Renamed from `vrg-finalize-repo`)

The rename signals the scope change. This tool finalizes a specific
PR:

1. Merge the PR (or confirm already merged).
2. Delete the feature branch (local and remote).
3. Prune remote references.
4. Post-merge housekeeping.

This is a human (Race Director) operation. No agent invokes it.

## `vrg-gh` Restriction Changes

### Street Mode Subcommand Allowlist

| Command | Status | Rationale |
|---|---|---|
| `issue create` | allowed | Agent opens issues |
| `issue comment` | allowed | Agent comments on issues |
| `issue view` | allowed | Read operation |
| `issue list` | allowed | Read operation |
| `issue close` | **blocked** | Race Director operation — premature closure is a known problem |
| `issue reopen` | **blocked** | Race Director operation |
| `issue edit` | **blocked** | Race Director operation — labels, milestones, assignments are strategic |
| `pr view` | allowed | Read operation |
| `pr checks` | allowed | Read operation |
| `pr list` | allowed | Read operation |
| `pr diff` | allowed | Read operation |
| `pr comment` | allowed | Agent comments for context |
| `pr review` | allowed | Non-approval review comments |
| `pr review --approve` | **blocked** | Race Director or Officials operation |
| `pr create` | **blocked** | Race Director operation via `vrg-submit-pr` |
| `pr edit` | **blocked** | Race Director operation |
| `pr merge` | **blocked** | Race Director operation via `vrg-finalize-pr` — no agent merges, period |
| `pr close` | **blocked** | Race Director operation |
| `run view` | allowed | Read operation |
| `run list` | allowed | Read operation |
| `run watch` | allowed | Read operation |
| `repo view` | allowed | Read operation |
| `repo list` | allowed | Read operation |

### Track Mode Subcommand Allowlist

Identical to street mode. Track mode does not relax the `vrg-gh`
allowlist. The only difference is server-side: workflow file pushes
succeed via `vrg-git push` because the admin App has
`workflows:write`. This keeps the design simple and avoids
permission creep ("I'm in admin mode so I might as well...").

### Audit Mode Subcommand Allowlist

| Command | Status | Rationale |
|---|---|---|
| `pr view` | allowed | Read PR for review |
| `pr diff` | allowed | Read diff for review |
| `pr list` | allowed | Find PRs to review |
| `pr checks` | allowed | Check CI status |
| `pr comment` | allowed | Post review findings |
| `pr review` | allowed | Submit formal review (including approval) |
| Everything else | **blocked** | Audit is read-and-comment only |

### Mode Detection

The `vrg-gh` wrapper detects mode from the credential environment.
If `VRG_APP_ID` matches the audit App, the audit allowlist applies.
No configuration flag — the VM's credentials determine the mode.

## Permission Delta Registry

Every difference between identities is explicitly documented with
a justification.

### Street → Admin Delta

| Permission | Street | Admin | Justification | Added |
|---|---|---|---|---|
| `workflows` | none | write | Workflow files define CI/CD containment. Changes require elevated access under direct human supervision in the track-mode VM. | 2026-05-29 |

### Street → Audit Delta

| Permission | Street | Audit | Justification | Added |
|---|---|---|---|---|
| `contents` | write | read | Audit reads code but must not modify it. | 2026-05-29 |
| `pull_requests` | read | write | Audit must comment on and submit reviews. | 2026-05-29 |
| `issues` | write | read | Audit has no need to create or modify issues. | 2026-05-29 |

### Rules

1. Every new delta requires a documented real-world use case before
   it is granted.
2. Speculative permissions are not added. The delta grows only when
   a justified use case is discovered.
3. Deltas are reviewed whenever the permission model is modified.

## Implementation Approach

Parallel tracks with safe sequencing:

**Track A — Tooling changes:**

- Build revised `vrg-submit-pr` (template mode, human-triggered).
- Implement `.vergil/` scratch convention.
- Update `vrg-gh` subcommand allowlists (block issue close/reopen/edit,
  block PR merge unconditionally, block PR create/edit/close).
- Rename `vrg-finalize-repo` to `vrg-finalize-pr`.
- Define the audit mode `vrg-gh` allowlist.

**Track B — Identity and permissions:**

- Create `vergil-admin-app` GitHub App with the documented permissions.
- Create `vergil-audit-app` GitHub App with the documented permissions.
- Set up `<user>-vergil-admin` and `<user>-vergil-audit` GitHub accounts.
- Configure track-mode and audit-mode VMs with respective credentials.
- Remove `workflows` permission from the street-mode App.
- Implement visual differentiation for each VM.

**Track C — Audit integration (deferred details):**

- Define the triggering mechanism for audit reviews.
- Integrate coding standards document with the audit agent.
- Determine pre-submission vs. post-submission review workflow.
- Explore local LLM viability for cost-effective audit.

Tracks A and B proceed in parallel. Track C is architecturally
defined in this spec (identity, permissions, vrg-gh allowlist) but
the triggering and integration details are a follow-up design.

**Immediate safe restriction:** Remove `workflows` permission from
the street-mode App now. No workflow changes are queued, so this
costs nothing and activates the server-side tripwire immediately.

## Open Questions

**Audit triggering mechanism.** How does the audit agent get
activated to review a PR? Options under consideration:

- Human-directed (v1): human tells the audit VM to review a
  specific PR or branch.
- Persistent watcher: audit VM polls for new PRs.
- Agentic CI gate: GitHub Actions workflow calls an LLM API.
- Local LLM: audit runs against a local model, eliminating API
  cost — the strategic direction.

**Pre-submission vs. post-submission review.** The audit agent could
review branches before the PR exists (catching issues earlier) or
review submitted PRs (integrating with GitHub's review system).
Pre-submission is preferred but the workflow integration needs design.

**Audit as a merge gate.** If the audit identity can approve PRs,
branch protection could require audit approval before merge. This
adds a quality gate but also adds a dependency — if the audit agent
is down or slow, merges are blocked.

**Visual differentiation specifics.** The requirement is that each
VM is visually distinct. The specific treatment (color scheme, prompt
indicators, login banners) needs design and user testing.

**Rename transition for `vrg-finalize-pr`.** The rename from
`vrg-finalize-repo` affects consuming repos and documentation. The
transition strategy needs to preserve backward compatibility during
the migration window.

## Strategic Context

**Local LLM direction.** The audit role is deliberately scoped to be
feasible on a local model: focused task, read-only, structured
output. As local LLM infrastructure matures (Ollama on developer
hardware), the audit identity becomes cost-neutral. The architecture
should not assume cloud API access.

**Mimir validation.** The Mimir identity (adversarial testing agent)
will be used to validate this permission model. Mimir should
demonstrate that:

- In street mode, workflow file pushes are rejected (server-side).
- In track mode, an adversarial agent can exploit elevated rights —
  confirming why track mode requires active human supervision.
- The audit identity cannot modify code or escalate privileges.

Documenting known attack surfaces for Mimir rather than waiting for
discovery is part of the validation strategy.

**Human accountability.** Every contribution standard reviewed
(including CPython's) requires human accountability. This
architecture enforces it: the human triggers PR submission, the human
triggers merge, the human reviews audit feedback. The agent produces
work; the human takes responsibility for it.
