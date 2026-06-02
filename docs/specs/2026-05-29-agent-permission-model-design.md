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

### Resolution adopted

This spec does **not** create an elevated identity that carries
`workflows: write`. Instead, no agent identity holds `workflows` at
all. The rare push that touches `.github/workflows/` is carried by
the human, whose rights are the superset of every agent's — folded
into the human-triggered `vrg-submit-pr` (see Ensure-pushed below).
The most common workflow-touching change — bumping pinned action
versions — is absorbed by the mechanized, human-run
`vrg-dependency-update` (#918). That tool is an ergonomic
optimization, not a prerequisite: until it exists, action-pin bumps
flow through the same ensure-pushed path as any other
workflow-touching change, so this permission model does not block on
#918. An elevated "admin" identity remains
**defined as a reserved architectural slot**, filled only when a
concrete use case requires it. This is the YAGNI-disciplined
outcome: we keep the slot and its governing invariant, not a
speculative implementation.

### Compliance alignment

The AI contribution compliance review
(`2026-05-27-ai-contribution-compliance-design.md`, #1223) assessed
Vergil's posture against the CPython AI contribution guidelines and
found:

- **P1 (human accountability):** "Structured" — the workflow
  encourages but does not mechanically enforce human ownership of
  submissions.
- **P2 (review before submission):** "Structured" — no mechanism
  forces the human to actually review the diff before it becomes
  a PR.
- **P3 (explainability):** "Advisory" — nothing verifies the human
  understands what they are merging.

This design directly addresses P1 and P2 by moving PR creation to a
human-triggered tool, mechanically enforcing that the human acts to
submit. It moves both from "Structured" to "Enforced." The
`vrg-submit-pr` redesign is also the natural place to address the
P3 follow-up (a structured self-attestation step) proposed in the
compliance spec.

More broadly, the principle of minimizing agent write permissions and
inserting the human at critical decision points aligns with the
emerging industry consensus on AI-assisted engineering: the human
contributor bears full responsibility for all submissions, and the
tooling should enforce — not merely encourage — that accountability.

## Foundational Assumption

The entire architecture rests on one assumption, stated here so that
nothing downstream has to restate it: **the human who operates the
host is the owner, and holds the maximum set of developer and
administrative rights over every resource in play** — the
repositories, the GitHub organization, the Apps, the VMs, the host
itself. The human is the all-powerful party. Everything else is
defined relative to that.

From this, three things follow and are true everywhere in this spec:

1. **Agents are sandboxed minions holding a strict subset.** An agent
   is never the owner. Each agent identity is granted a deliberately
   minimal subset of the human's rights, scoped to its role. It can
   never be granted a right the human does not have.

2. **Delegation to the host is the mechanism, not a workaround.** The
   host is precisely where commands run under *human* control rather
   than *agent* control. When an operation is too dangerous to hand an
   agent, it is delegated to a human-triggered tool on the host — and
   that tool can always carry it, because the host runs with the
   owner's full rights. `vrg-submit-pr`'s ensure-pushed is the worked
   example: it pushes with the human's superset credentials a change
   the agent's subset would have been rejected for.

3. **If the human lacks a required right, the architecture does not
   function — by design.** This is not a gap to be patched with a
   fallback or a runtime probe. If the operator is not the owner with
   the umbrella rights this model assumes, the model is the wrong
   model for that situation, and that is the correct, intended
   outcome. We do not build degraded modes for a non-owner operator;
   we assume the owner.

The corollary used throughout: **if the human cannot perform an
action, neither can the agent.**

## Design Principles

**Agent rights are a subset of the human's.** Per the Foundational
Assumption above, the human operates the host with the full set of
ownership rights, and sandboxing exists to contain that full access
into a minimal per-identity subset. The agent is never granted a
right the human lacks. This is why a human-triggered tool can always
carry an operation the agent could not: the human is the superset.

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

**The human triggers; the agent doesn't.** Agents never escalate
their own privileges or perform the operations that really matter and
cause damage. Those are encapsulated in `vrg-*` tools the human
invokes deliberately, which concentrates human attention on the
higher-risk work.

**Human chokepoints are verification points.** Every high-risk,
irreversible operation is denied to agents and reserved to a
human-triggered `vrg-*` tool on the host. Because that tool runs in
the owner's trusted context — outside any agent's reach — it is also
the place to impose programmatic verification the hard gates cannot.
Where GitHub's permission model is too coarse to *prevent* an unwanted
agent action (the documented gaps), the human-run tool at the
chokepoint can *detect* it before the irreversible step and refuse to
proceed. Each destructive operation thus becomes a sanity-check
opportunity: the merge gate verifies a PR's provenance, a release gate
could verify a release's, and so on. This is the architectural answer
to gaps that cannot be closed server-side — and it complements CI
gates, which check the *content* of a change, by adding host-side
checks on the *conduct* of the identities that produced it.

**Correctness over cost.** Token cost is a real constraint. Granting
agents the ability to perform trivial operations (closing issues,
editing labels) that the human can do with a few clicks is
wasteful. The system should be designed to minimize token expense,
not maximize agent autonomy.

## Three-Layer Security Model

The permission architecture is built on three layers of control with
decreasing trust and increasing hardness. Every permission decision
in this spec references which layers protect it.

**Layer 1 — VM-internal controls (soft gates).** Wrapper scripts
(`vrg-git`, `vrg-gh`), hook guards, subcommand allowlists,
identity-aware tool behavior. These provide early feedback for a
well-behaved agent — like real-time linting that catches mistakes
as you type rather than days later in review. Against an adversarial
agent, these are trivially bypassed: the agent can become root inside
the VM, find the raw binaries, and reverse-engineer the wrappers.
We accept this. Soft gates correct behavior; they do not contain
threats.

**Layer 2 — Server-side controls (hard gates).** GitHub App
installation permissions and branch protection rules.
These are the real security boundary. The agent cannot bypass them
from inside the VM — they are enforced by GitHub's infrastructure,
outside the agent's control. When a soft gate has a corresponding
hard gate, the soft gate is defense-in-depth. When a soft gate has
no corresponding hard gate, this is a documented gap.

**Layer 3 — Operational auditing (detection).** Periodic review of
operations performed within the environment of layers 1 and 2.
Validates that controls are working. Detects moments when soft gates
were bypassed, whether hard gates caught the attempt, and whether
any gaps were exploited. This is the ultimate safety net. Even when
all hard gates are working, auditing confirms they are working and
surfaces patterns that indicate drift or attack. Detection need not
be purely retrospective: at a human-run chokepoint (such as merge), a
detection check can be promoted into a *synchronous gate* that runs
before the irreversible operation and refuses to proceed — see
`vrg-finalize-pr`'s pre-merge provenance check. This is how Layer 3
closes specific gaps that Layer 2 cannot.

### Documented gaps

Where a soft gate exists without a corresponding hard gate, this
spec documents it explicitly. Known gaps:

- **Audit App `pull_requests: write` grants more than review.**
  GitHub offers no "review-only" PR permission and — more
  fundamentally — does not separate *merging* a PR (an administrative
  act) from *writing or updating* one. The soft gate (vrg-gh audit
  allowlist) restricts the audit identity to view/comment/review, but
  server-side the permission also permits PR creation, editing, and
  closing. Mitigation: the audit App holds `contents: read`, and
  merging a PR through the API requires `contents: write` — so the
  audit identity cannot merge anything it creates, and
  `vrg-finalize-pr`'s pre-merge provenance check detects and blocks
  any out-of-role audit action on a PR before the human merges it (see
  that section). This is the sharpest known hard-gate gap — and because audit is the identity we
  most want to run with minimal human attention, it warrants the most
  Mimir scrutiny. It is also a candidate for upstream infrastructure
  advocacy: the correct fix is for GitHub to expose finer-grained PR
  permissions. Documenting it precisely builds the data to make that
  case.

- **Agent can attempt `vrg-submit-pr`.** The soft gate is
  identity-aware tool rejection. The hard gate is `pull_requests:
  read` on the user App, which causes the underlying `gh pr create`
  to fail server-side. This gap is fully covered by a hard gate.

Additional gaps will be documented as they are discovered. Mimir
testing is specifically designed to find and exploit undocumented
gaps.

## The Racetrack Model

The identity architecture uses a motorsport analogy as its conceptual
framework. This is not decoration — the analogy maps cleanly to the
trust relationships and decision authority in the system, and the
documentation uses these terms as the primary vocabulary. The model
is still evolving; the terms below describe the current roles, not a
fixed law derived from first principles.

| Racing role | Description | Vergil identity |
|---|---|---|
| **Driver** | Competes on track, makes tactical decisions, works within the rules | `<user>-vergil-user` — daily development agent |
| **Officials** | Walk pit lane, observe, report infractions — cannot penalize | `<user>-vergil-audit` — reviews PRs, comments on violations |
| **Race Director** | Full authority over the event, makes the calls | `<user>` — human, ultimate authority |
| **Admin** (reserved) | A competitor granted escorted access to a normally off-limits area | reserved slot — not provisioned |

**Driver (the user identity):** The everyday agent — the workhorse,
used all the time. Restricted write permissions, safe for autonomous
and unattended work within those limits. This is the 24/7 identity.

**Officials (the audit identity):** An independent observer that can
only watch and report. Officials walk pit lane looking for
violations. They can flag "you dropped a lug nut" but cannot issue
penalties themselves — they report to Race Control (the human). The
audit identity reads code, reviews PRs against standards, and
comments. It cannot modify code, create PRs, or close issues.
Deliberately scoped for the smallest blast radius, because it is the
identity we most want to run close to unattended.

**Race Director (the human):** The ultimate authority. Reviews
reports from Officials, makes merge/release/strategy decisions,
triggers operational tools. No one overrules the Race Director. The
Driver and Officials never interact directly — everything goes
through Race Control.

**Admin (a reserved slot, not a built identity):** This is *not* a
faster car. It is the same development role granted escorted access
to a normally off-limits area — the regulated CI/CD infrastructure a
competitor never touches unsupervised. The slot is defined but
unoccupied. It is instantiated only when a concrete use case requires
elevated access, and its governing constraint is fixed in advance:
**elevated access is paired with an elevated level of human control
and interaction.** Access and supervision are the same dial.

## Identity Architecture

### GitHub Accounts

| Identity | App / account | Access mechanism | Purpose |
|---|---|---|---|
| User (Driver) | `<user>-vergil-user` | GitHub App, write-shaped permissions | Daily development |
| Audit (Officials) | `<user>-vergil-audit` | GitHub App, read-shaped permissions (inverted) | PR review |
| Human (Chief Steward) | `<user>` | Owner/Admin | Ultimate authority |
| Admin (reserved) | `<user>-vergil-admin` | — | Reserved — not provisioned until a use case requires it |
| Release bot | `vergil-release[bot]` | GitHub App | Release automation |

Every AI agent is represented by a GitHub App whose name encodes both
the human who owns it and the agent's role (`<user>-vergil-<role>`).
The App's installation provides the agent's only credential: access to
a repository is granted by installing the App there, and the agent's
effective capability is bounded entirely by the App's declared
permission shape. There are no agent user accounts and no collaborator
grants — the bot identity (`<user>-vergil-<role>[bot]`) that appears on
commits and PRs comes from the App itself. This keeps non-human
contributions cleanly attributable: as multiple engineers each run
several role-scoped agents, the App names make it immediately visible
which human's which agent did what.

### GitHub App Configuration

Two GitHub Apps are provisioned today (user, audit); a third (admin)
is a reserved slot. Each VM gets exactly one App's credentials. The
credential environment determines the operating mode — no config
flag, no mode switch command.

**User App (`<user>-vergil-user`):**

| Permission | Level | Rationale |
|---|---|---|
| `contents` | write | Push feature branches; push code fixes to iterate on CI |
| `issues` | write | Create and comment on issues |
| `pull_requests` | read | View PRs, check status |
| `metadata` | read | Required baseline |
| `workflows` | none | Server-side hard gate blocks workflow file pushes |

Note that `contents: write` — not `workflows: write` — is what lets
the agent handle a failing CI gate: it pushes *code fixes* to
re-trigger CI. Editing the workflow files themselves is never part of
fixing a gate; that would be changing the rules to pass, the exact
escape hatch we are closing.

**Audit App (`<user>-vergil-audit`):**

| Permission | Level | Rationale |
|---|---|---|
| `contents` | read | Read code for review |
| `issues` | read | Context for understanding PRs |
| `pull_requests` | write | Comment on and review PRs |
| `metadata` | read | Required baseline |
| `workflows` | none | No need |

The audit App has an inverted permission shape compared to the user
App: more PR permission (write vs. read) but less code permission
(read vs. write). The permissions are shaped exactly for the role.

**Admin App (`<user>-vergil-admin`) — reserved, not provisioned.**

This is a defined architectural slot, not a built identity. Its
permissions are intentionally left undefined: there is no concrete
use case yet that requires elevated agent access, and guessing the
permission set would bake in speculation we cannot justify. The one
property fixed now is its governing invariant — elevated access is
paired with an elevated level of human control and interaction. When
a real use case appears, the App is created with the minimal
permissions that case requires, and its delta is recorded in the
Permission Delta Registry.

**Provisional status (audit).** The audit identity's architectural
position (second agent identity, inverted permission shape, Officials
role) is load-bearing for the overall design and is defined here. The
specific permissions and vrg-gh allowlist are provisional — they
are based on the one confirmed use case (PR standards review) and
are subject to revision as additional use cases emerge and the
triggering mechanism is designed.

### VM Architecture

```text
Human host
├── User VM  (daily use, always available)
│   ├── VRG_APP_ID → <user>-vergil-user App
│   ├── VRG_PRIVATE_KEY_PATH → <user>-vergil-user key
│   └── Agent operates as <user>-vergil-user[bot]
│
├── Audit VM  (persistent or on-demand)
│   ├── VRG_APP_ID → <user>-vergil-audit App
│   ├── VRG_PRIVATE_KEY_PATH → <user>-vergil-audit key
│   └── Agent operates as <user>-vergil-audit[bot]
│
└── Admin VM  (reserved — not provisioned)
    └── Created only when the admin slot is filled
```

The human launches each VM deliberately. There is no mode-switching
within a running session. The visual presentation of each VM must be
distinct — the human must never be uncertain about which identity
they are in. Specific visual treatment (color scheme, prompt
indicator, login banner) is an implementation detail, but the
requirement is ambient, persistent differentiation.

**Shared filesystem.** All VMs mount the host's project directory
tree at the same path. The agent writes to the same filesystem the
human sees — there is no file sync or handover mechanism. When the
agent writes `.vergil/pr-template.yml`, the human sees it
immediately on their host terminal. The VM is a credential sandbox
with a shared filesystem, not an isolated environment.

**Mode detection.** Each VM is provisioned with a
`VRG_IDENTITY_MODE` environment variable (values: `user`, `audit`;
`admin` reserved) set by the VM build configuration in the
vergil-vm repo. The variable is written into the user's shell
profile during provisioning, making it ambient and persistent.
The tooling (`vrg-git`, `vrg-gh`, `vrg-submit-pr`) reads
`VRG_IDENTITY_MODE` to select allowlists and behaviors. Absence
of the variable (or an unrecognized value) implies human identity.

`VRG_IDENTITY_MODE` is set by VM provisioning rather than chosen by
the agent, but it is a **Layer 1 ergonomic, not a security control**.
An adversarial agent that becomes root inside the VM can set or unset
it and thereby relax the soft-gate allowlists. This changes nothing
that matters: the identity boundary is the App credential provisioned
to the VM, not the variable. No value of `VRG_IDENTITY_MODE` alters
which private key is present, so none of it alters what GitHub will
actually permit. Spoofing the mode loosens soft gates the spec
already treats as bypassable; the hard gate is untouched.

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
title: "feat(permissions): remove workflow permission from user App"
summary: |
  Removes the workflows permission from the user GitHub App and adds
  vrg-gh restrictions to block issue close/reopen/edit for the user
  identity.
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

### User Identity (Daily Development)

```text
Agent                              Human (Race Director)
──────                             ─────────────────────
1. Write code
2. Commit (vrg-commit)
3. Push branch (vrg-git push)
   └─ If push fails (workflow files
      changed): agent stops, reports
      failure, escalates to human
4. Write .vergil/pr-template.yml
5. Signal: "ready for PR"
                                   6. Review template, edit if needed
                                   7. Run vrg-submit-pr (push + PR)
                                   8. Review PR + CI results
                                   9. Merge (vrg-finalize-pr)
```

The agent's boundary is step 5. Everything after that is a human
operation triggered by `vrg-*` CLI tools.

**Push failure handling.** When `vrg-git push` fails because the
App token lacks `workflows` permission, the wrapper detects the
specific GitHub error and provides identity-aware feedback: the
agent is told its identity is not permitted to push workflow file
changes, and it must stop and escalate to the human. The agent must
not attempt to work around the failure (e.g., by removing workflow
files from the commit and re-pushing). The human carries the
workflow-touching push themselves — in practice via `vrg-submit-pr`,
which pushes the branch with the human's credentials before opening
the PR (see Ensure-pushed below). For the common case of action-pin
bumps, the human runs `vrg-dependency-update` (#918), which
mechanizes the whole update. The escalation must not silently drop
the workflow change: the agent reports clearly what is blocked and
why.

### Workflow File Changes

No agent identity can push changes under `.github/workflows/` — the
server rejects the entire push (not just the offending file) when the
App token lacks `workflows`. This is deliberate: workflow files
define CI/CD containment, so altering them is reserved to the human.
The human carries such pushes via `vrg-submit-pr`'s ensure-pushed
behavior, and the routine action-pin case is mechanized in
`vrg-dependency-update` (#918).

One honest cost: a branch that mixes workflow changes with code
cannot be pushed — and therefore cannot be CI-iterated — by the agent
at all until the human pushes it. In practice workflow changes are
usually isolated, so this rarely bites; when it does, it is the kind
of change that warrants human attention anyway. The anticipated first
trigger for filling the reserved admin slot is this friction becoming
common enough to justify a supervised elevated identity — but the
mechanized dependency update is expected to absorb most of it.

### Audit Identity (PR Review)

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
template, and an ensure-pushed step.

**Template mode (no CLI args):**

1. Read `.vergil/pr-template.yml` — fatal error if absent.
2. Show the human a summary: title, body preview, issue linkage,
   target branch.
3. Prompt for confirmation.
4. Ensure-pushed (see below).
5. Create the PR via `gh pr create`.
6. Delete the template file.
7. Print the PR URL.

**CLI argument mode (args provided):**

The existing argument-based invocation (`vrg-submit-pr --issue 1289
--title ...`) continues to work for direct human use without a
template. This supports the case where the human is making emergency
changes without an agent.

**Neither template nor args:** Fatal error. The tool does not guess.

**Ensure-pushed.** Before creating the PR, `vrg-submit-pr` verifies
the branch is fully pushed to the remote. If the branch has commits
not yet on the remote, it performs the push using the human's host
credentials, then creates the PR. Because the human's credentials are
the superset of any agent's, this push succeeds even when the branch
touches `.github/workflows/` — which the agent's own push would have
been rejected for. This is precisely how workflow-touching changes
reach the remote without any agent holding `workflows: write`.

This is the Foundational Assumption in action, not a separate
credential requirement: `vrg-submit-pr` runs on the host, in the
owner's context, with the human's full GitHub credentials. The push
succeeds because the human holds the superset. If the human cannot
push the change, neither tool nor agent can — and that is the correct
behavior, not a failure to handle.

**Identity-aware enforcement.** `vrg-submit-pr` checks the
credential environment on startup. If it detects any agent identity
(user or audit), it aborts immediately with a clear message: PR
submission is a Race Director operation. This is a Layer 1 soft gate.
The Layer 2 hard gate is `pull_requests: read` on the user App, which
causes the underlying `gh pr create` to fail server-side even if the
soft gate is bypassed.

**Wrapper denial messages.** When `vrg-gh` blocks a subcommand, the
denial message is identity-aware. For human identities, `pr create`
says "use vrg-submit-pr." For agent identities, the same denial
says "PR creation is a Race Director operation" — no mention of
`vrg-submit-pr`, since agents should not know about tools they
cannot use. (The hook guard itself is a dumb gate — it only
redirects raw `git`/`gh` to the wrapper scripts and knows nothing
about subcommands or identity.)

## `vrg-finalize-pr` (Consolidates merge + cleanup, formerly `vrg-finalize-repo`)

This is not a rename — it is a consolidation. Previously two separate
human actions finished a PR: the human merged it by hand (on the web),
then ran `vrg-finalize-repo` to clean up the branch and prune refs.
Both collapse into one human tool. Moving the merge off the web and
into a `vrg-*` tool is the same move applied everywhere in this design:
take a mechanized operation that really matters and put it behind a
human-triggered command. It also creates the exact insertion point the
provenance check needs — a single place, under human control, that
performs the merge and can gate it.

Until now the implicit merge rule was "if the CI gates are green, merge
it" — sound only because every sanity check was assumed to live in the
GitHub CI gates, which today is true. The provenance check is the first
sanity check that *cannot* live in a CI gate (GitHub attributes
identity actions outside the PR's own checks), so it runs in the tool
that performs the merge, immediately before the merge.

This tool finalizes a specific PR:

1. **Pre-merge provenance check.** Fetch the PR's action history —
   reviews and timeline events, each attributed by GitHub to the
   identity that performed it — and cross-check every agent action
   against what that identity's role permits. The audit identity must
   never have created, edited, closed, or reopened the PR; if it did,
   the tool aborts with the offending action named. An audit *approval*
   is permitted but surfaced explicitly, so the human knows a green
   review came from an advisory identity, not an authoritative one. The
   human can override — they hold every right — but only consciously,
   with the violation in front of them.
2. Merge the PR (or confirm already merged).
3. Delete the feature branch (local and remote).
4. Prune remote references.
5. Post-merge housekeeping.

The action history is fetched with read-only `gh api` GET calls to
the PR reviews and issue timeline endpoints. This is available because
the tool runs in the human context, and the same calls are reachable
from the audit context under the identity-aware API allowance (see
"Identity-aware API access") — without ever granting the user agent a
raw-API escape hatch.

This is a human (Race Director) operation. No agent invokes it. The
pre-merge provenance check is the worked example of the "human
chokepoints are verification points" principle: at the irreversible
step it closes the audit identity's `pull_requests: write` hard-gate
gap that GitHub's permission model cannot. It generalizes beyond
audit — the check verifies that *no* agent identity performed an
action its role forbids on the PR being merged.

## `vrg-gh` Restriction Changes

### User Identity Subcommand Allowlist

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
| `api` | **blocked** | Raw API is a broad escape hatch — abusable by a compromised user agent to reach endpoints the subcommand allowlist would otherwise gate |

### Audit Identity Subcommand Allowlist

| Command | Status | Rationale |
|---|---|---|
| `pr view` | allowed | Read PR for review |
| `pr diff` | allowed | Read diff for review |
| `pr list` | allowed | Find PRs to review |
| `pr checks` | allowed | Check CI status |
| `pr comment` | allowed | Post review findings |
| `pr review` | allowed | Submit formal review (including approval) |
| `api` (GET only) | allowed | Read-only API access for review tooling (e.g., PR reviews and timeline endpoints). The audit App's narrow permissions (`contents: read`, `pull_requests: write`, `issues: read`) bound what any API call can reach, so the raw API is low-risk for this identity |
| Everything else | **blocked** | Audit is read-and-comment only |

### Admin Identity Subcommand Allowlist

Reserved. Defined when the admin slot is filled, scoped to the
minimal set its use case requires.

### Mode Detection

The `vrg-gh` wrapper reads `VRG_IDENTITY_MODE` from the environment.
If the value is `audit`, the audit allowlist applies. If `user`, the
user allowlist applies. Absence of the variable implies human
identity, which retains the full allowlist. The environment variable
is set by VM provisioning — the agent does not choose its own mode.

### Identity-aware API access

The raw `gh api` escape hatch is gated per identity rather than
denied wholesale. This is the general principle that the `vrg-gh`
allow/disallow decision is a function of identity, not a single
fixed list:

- **User** — blocked. The user App holds `contents: write`, so a
  compromised user agent with raw API access could reach write
  endpoints that the curated subcommand allowlist deliberately
  withholds. The broad surface is not worth the ergonomic gain.
- **Audit** — allowed for read (GET) calls. The audit App's
  permissions are narrow and mostly read-only (`contents: read`,
  `issues: read`, with `pull_requests: write` the only write
  scope), so even the raw API cannot reach anything the identity
  is not already trusted with. This is what makes read-only API
  access safe to grant here but not to the user.
- **Human** — full, as with every other subcommand.

This identity-aware allowance is what lets the pre-merge provenance
check in `vrg-finalize-pr` query GitHub's review and timeline
endpoints (see that section). The check runs in the human context,
where the API is available; the same endpoints are reachable from
the audit context for review tooling without granting the user
agent a write-capable escape hatch.

## Permission Delta Registry

Every difference between identities is explicitly documented with
a justification.

### Reserved: User → Admin Delta

The admin identity is a reserved slot with no permissions granted
today, so there is no delta to record yet. When the slot is filled,
its delta is added here with a documented use case, per the rules
below. The governing constraint is fixed in advance: any admin delta
must pair elevated access with elevated human involvement.

### User → Audit Delta

| Permission | User | Audit | Justification | Added |
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

## Migration Strategy

### Identity transition

The new identities (`<user>-vergil-user`, `<user>-vergil-audit`) are
created from scratch alongside the existing `<user>-vergil` identity.
(The admin identity is a reserved slot — not created until a use case
requires it.) Both sets operate in parallel during the transition:

- **Phase 1:** Create the two new GitHub accounts and GitHub Apps
  (user, audit). Provision new VMs with the new credentials. The
  existing `<user>-vergil` identity and tooling on 2.0.x continue
  unchanged.

- **Phase 2:** Build the tooling changes (Track A below) as a 2.1
  release using the 2.0 tooling. The new 2.1 tooling is used in the
  new VMs; the old 2.0 tooling continues in the existing VM.

- **Phase 3:** Retire the old `<user>-vergil` identity and 2.0
  tooling. The new VMs and 2.1 become the default.

### Version-gated cutover

The permission model changes land as a minor version upgrade:
2.0.x → 2.1. This provides a clean boundary:

- 2.0.x: existing identity, existing permissions, `vrg-finalize-repo`
- 2.1: new identities, reduced permissions, `vrg-finalize-pr`

During the transition window, `vrg-finalize-repo` ships as a
deprecated alias that prints a warning and calls `vrg-finalize-pr`.
The alias is removed in a subsequent minor version.

### Consuming repo migration

When a consuming repo upgrades from vergil-tooling 2.0 to 2.1,
the following must be updated:

- **CLAUDE.md:** Update agent instructions to reflect reduced
  capabilities (no PR submission, no issue close/reopen/edit).
- **Memory files:** Audit for stale entries that reference old
  workflows, old tool names, or workarounds for behaviors that no
  longer apply.
- **Documentation:** Update any references to `vrg-finalize-repo`,
  agent PR submission workflows, or identity naming.
- **`.gitignore`:** Add `.vergil/` entry.
- **Skills/hooks:** Update any skills that reference agent PR
  submission or the old identity naming convention.

A migration checklist will be maintained as part of the 2.1
release documentation.

## Implementation Approach

Parallel tracks with safe sequencing:

**Track A — Tooling changes:**

- Build revised `vrg-submit-pr` (template mode + ensure-pushed,
  human-triggered).
- Implement `.vergil/` scratch convention.
- Update `vrg-gh` subcommand allowlists (block issue close/reopen/edit,
  block PR merge unconditionally, block PR create/edit/close).
- Make `vrg-gh`'s `gh api` allowance identity-aware: full for human,
  read-only GET for audit, denied for user.
- Consolidate the merge and post-merge cleanup into `vrg-finalize-pr`
  (formerly `vrg-finalize-repo`): the tool now performs the merge and
  runs the pre-merge provenance check immediately before it, fetching
  the PR's action history via read-only `gh api` calls.
- Define the audit identity `vrg-gh` allowlist.

**Track B — Identity and permissions:**

- Register the `<user>-vergil-user` and `<user>-vergil-audit` GitHub
  Apps with the documented (inverted) permission shapes.
- Install both Apps on the target orgs/accounts and capture their App
  IDs and private keys.
- Configure user and audit VMs with their respective App credentials.
- Confirm the user App holds no `workflows` permission.
- Implement visual differentiation for each VM.

**Track C — Audit integration (deferred details):**

- Define the triggering mechanism for audit reviews.
- Integrate coding standards document with the audit agent.
- Determine pre-submission vs. post-submission review workflow.
- Explore local LLM viability for cost-effective audit.

Tracks A and B proceed in parallel. Track C is architecturally
defined in this spec (identity, permissions, vrg-gh allowlist) but
the triggering and integration details are a follow-up design.

**Immediate safe restriction:** Ensure the user App holds no
`workflows` permission now. No workflow changes are queued, so this
costs nothing and activates the server-side hard gate immediately.

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

**When does the admin slot get filled?** The reserved admin identity
has no permissions today. The anticipated first trigger is workflow
iteration becoming too tedious to route through the human on every CI
turn — but the common case (action-pin bumps) is already handled by
`vrg-dependency-update` (#918), so the trigger may not arrive soon.
The slot is instantiated only when a concrete elevated-access use
case appears, and its design must pair the elevated rights with
heightened human involvement.

**Programmatic PR review at the merge gate.** The pre-merge
provenance check in `vrg-finalize-pr` establishes a host-side
insertion point that runs before merge. Today the PR-quality strategy
relies almost entirely on CI gates; the PR *review* process is not yet
leveraged for substantive review of AI-authored changes. The same
chokepoint is the natural home for future programmatic or agentic
review of the PR's *content*, not just its provenance. Scope and
mechanism are a follow-up design.

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

**Cooperative vs. adversarial agents.** The model assumes a
cooperative agent (Vergil) for routine operation, but does not *rely*
on cooperation for containment. A prompt injection can transiently
turn a cooperative agent into an adversarial one (Mimir) for the
duration of the compromise. Soft gates correct a cooperative agent;
the server-side hard gates must contain a momentarily-hostile one.
Where a hard gate is missing (the documented gaps), that is a known,
accepted, and tracked exposure — not an oversight. The dependence on
agent good behavior inside the VM is a strategic risk we name
explicitly rather than pretend away.

**Mimir validation.** The Mimir identity (adversarial testing agent)
will be used to validate this permission model. Mimir should
demonstrate that:

- In the user identity, workflow file pushes are rejected
  (server-side).
- The audit identity cannot modify code or escalate privileges.

Mimir is expected to defeat every Layer 1 soft gate trivially —
becoming root, finding the raw binaries, spoofing `VRG_IDENTITY_MODE`,
reverse-engineering the wrappers — and that is the point: the soft
gates are not where containment lives. Mimir's real work is sustained
pressure on the Layer 2 hard gates and the documented gaps, which is
where the architecture either holds or fails.

Documenting known attack surfaces for Mimir rather than waiting for
discovery is part of the validation strategy. (Mimir's own
implementation is out of scope here and will be designed separately.)

**Infrastructure advocacy.** Some gaps cannot be closed from inside
this architecture because the underlying platform lacks the needed
control — the GitHub PR-permission granularity gap is the clearest
example. Documenting these precisely serves a second purpose beyond
our own validation: it builds the evidence to advocate for the
platform improvements (e.g., separating PR merge from PR write) that
would let any AI-assisted workflow contain agents more tightly. The
architecture is, in part, an opportunity to make the current limits
of AI-safety tooling visible and argue — with data — for taking them
seriously.

**Radical transparency about weak controls.** This spec deliberately
documents its own soft spots loudly rather than burying them — a
strategic choice, not an admission of sloppiness. The current state of
AI-agent security tooling is alarmingly immature; much of what ships
as a "control" is trivially bypassable, and in important respects the
field has regressed against security ground that was settled decades
ago. Naming our gaps precisely is how we prioritize closing them and
how we build the evidence to argue the field must do better. The
documented gaps are a work list, not a disclaimer. Where
vendor-provided agent controls are weak, we say so plainly and route
real containment to the hard gates we control rather than pretending
the soft ones suffice.

**Human accountability.** Every contribution standard reviewed
(including CPython's) requires human accountability. This
architecture enforces it: the human triggers PR submission, the human
triggers merge, the human reviews audit feedback. The agent produces
work; the human takes responsibility for it.
