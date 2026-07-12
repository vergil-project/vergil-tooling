# GitHub Issue Standards

## Purpose

Define a consistent, enforced workflow for GitHub issues so all changes are
tracked, reviewable, and auditable.

## Scope

Applies to all repositories that use GitHub issues and pull requests.

## Definitions

- Issue: The unit of tracked work in GitHub.
- Primary issue: The single issue a pull request is intended to close.
- Sub-issue: A scoped unit of work that contributes to a parent issue but does
  not complete it.
- Operational task: A not-PR-workable task whose acceptance is proven by
  *running* something and recording the result as a comment — not by merging a
  pull request. Two kinds: **validation** (verify) and **deployment** (make
  merged work usable). See [Operational tasks](#operational-tasks).
- Intake item: An uncurated capture — `triage`, `idea`, or `research` — that is
  not yet a task, held in a queue until it is groomed into the epic/task model.
  See [Intake queues](#intake-queues).

## Core rules

- Every pull request must have a primary GitHub issue. No exceptions.
- Work must not begin until the issue exists.
- One primary issue per pull request. If a PR must close multiple issues,
  document why in the PR description.

## Acceptance criteria

Every issue must specify acceptance criteria if they are not intuitively
obvious. If acceptance criteria are ambiguous, get explicit human confirmation
before proceeding.

Examples:

- Docs-only issues: satisfied when documentation changes are merged.
- Bug reports: may require reporter confirmation or verified reproduction and
  resolution steps.

## Issue templates

Repositories must use GitHub Issue Forms and disable blank issues so all issues
capture the required structure.

Minimum required fields:

- Summary
- Problem or goal
- Acceptance criteria
  - include an explicit "criteria are obvious" option
  - require explicit criteria when not obvious
- Validation or evidence

Required configuration:

- `.github/ISSUE_TEMPLATE/issue.yml` (or equivalent form name)
- `.github/ISSUE_TEMPLATE/config.yml` with `blank_issues_enabled: false`

## Issue creation and linking

- If a human already specified an issue, use it as the primary issue.
- If no issue exists, create one before creating a branch or changing files.
- If no issue exists, create it immediately using best-effort assumptions and
  explicitly note those assumptions in the issue body. Do not delay work by
  asking for an issue number unless acceptance criteria are materially
  ambiguous.
- The PR description must link to the primary issue.
- If an issue has no special acceptance criteria, include a closing keyword in
  the PR description so the issue auto-closes on merge.
- If acceptance criteria are specified, use a non-closing reference in the PR
  description and close the issue only when the criteria are satisfied.

## Sub-issues

Create a sub-issue when:

- the parent issue is too large for a single PR
- the PR will not fully resolve the parent issue
- the work can be reviewed and merged independently

Sub-issue rules:

- Link each sub-issue to its parent with the sanctioned tooling (see below).
- The PR should close the sub-issue, not the parent, unless the PR completes
  the parent's full scope.

### Linking a sub-issue

Raw `gh api` is blocked for agents, so sub-issue links are created through the
sanctioned tools. Both establish the same native GitHub sub-issue relationship
(with a portable `Parent:` body reflink as the cross-forge fallback) that the
rollup reads — never hand-roll the link.

- **A new task under its parent** — create it already linked:

  ```bash
  vrg-issue-create --epic <owner>/<repo>#<parent> --repo <owner>/<repo> \
    --title "<what>"
  ```

- **An existing task** — link it (or backfill a reflink-only child):

  ```bash
  vrg-epic-link --epic <owner>/<repo>#<parent> --task <owner>/<repo>#<child>
  ```

## Epics

An **epic** is an umbrella issue — carrying the `epic` label — over the *task*
issues that deliver it. Tasks may live in other repos in the same org; the link
is a native GitHub sub-issue, with a portable `Parent:` body reflink as the
cross-forge fallback.

### Finite vs perpetual epics

- **Finite epic** — a bounded initiative with a definite end. It **rolls up**:
  once every child task is closed, `vrg-finalize-pr` closes the epic. A *managed
  task* (an issue with an `epic`-labelled parent) links its PR with `Closes`, so
  the task auto-closes on merge and the final close rolls its epic up.
- **Perpetual (ad-hoc) epic** — a standing umbrella for unplanned work in one
  repo. Titled `Epic (ad hoc): <repo>`, labelled `epic` + `ad-hoc`, one per repo,
  and it **never auto-closes**. Target it with `vrg-issue-create --epic adhoc`.
  (The older `standing` alias is **retired** — only `ad-hoc` remains.)
  `vrg-adhoc-epic ensure --repo <owner>/<repo>` creates a repo's ad-hoc epic
  on demand (idempotent).

### Creating an epic (the `epic-create` workflow)

Non-trivial work starts with the `epic-create` workflow — the **default entry
point**, since a solution worth designing is worth tracking rather than
brainstorming and walking away. `epic-create` is the **outer** orchestrator: it
runs the whole design pipeline and seeds the two bookend tasks (below) at
defined handoffs:

1. `superpowers:brainstorming` — explore intent, one question at a time
   (**interactive**).
2. Initialize the epic in its [home](#epic-home-the-orggithub-rule) and seed the
   docs-first and review-last bookend tasks.
3. Write `spec.md`, then `paad:pushback` on it (**interactive**).
4. Human review.
5. `superpowers:writing-plans` → `plan.md` (**automated** — no gating).
6. `paad:alignment` — reconcile the plan against the spec (**interactive**).
7. One docs PR (spec + plan) that closes the docs-first task.
8. File the implementation tasks and link them under the epic.

The **four-stage interaction doctrine** governs the pipeline: `brainstorming`,
`pushback`, and `alignment` are interactive while `writing-plans` is automated,
and every interactive stage gates **only** on judgment calls that materially
affect the outcome — minor, obvious corrections are batched into a single
end-of-stage "correct me if I'm wrong" review rather than gating each one. The
goal is to front-load analysis so implementation runs near-automated. If the
design collapses to a single-PR change, it drops onto the target repo's ad-hoc
epic instead of minting a finite epic. The full doctrine lives in the
`epic-create` skill (`vergil-claude-plugin`).

### The bookend convention

**An epic is never closed until you have decided what comes next.** Almost no
real problem is fully resolved by one epic — you deliver a completed subset and
acknowledge the follow-on. So **every epic carries at least two tasks**, and
its first and last are fixed bookends:

- **Opening bookend — documentation.** A docs-first task carrying the epic's
  spec and plan, born from planning; it lands before the implementation tasks.
- **Closing bookend — review + follow-on.** A documentation-review task (verify
  the shipped work is fully reflected in the docs) plus a follow-on-brainstorm
  task that reviews what shipped — successes, failures, mid-flight changes,
  newly found problems and opportunities — and files the follow-on epic(s). If
  the answer is the rare "nothing further," the epic is done; you always ask.

The bookend needs no new closing mechanism — it rides the existing rollup. A
finite epic rolls up only when every child is closed, so the closing review
task **naturally gates closure**. The convention is mechanized as prose in the
`epic-create` skill, not in rigid tooling, because choosing the right follow-on
is an inherently agentic judgment.

### Epic home (the `<org>/.github` rule)

An epic's home repo is derived from repository visibility:

- A **public** repo homes its epics centrally in the org's `.github`.
- A **private** repo (with a public `.github`) homes its epics **in itself**.
- A **private** `.github` means the whole org is private, so everything homes in
  `.github`.

Ad-hoc epics follow the same rule — `Epic (ad hoc): <repo>` lives in the repo's
resolved home. See
[Epic home visibility flips](../guides/epic-home-visibility-flip.md) for
relocating epics when a repo's visibility changes.

### Compliance invariants

`vrg-epic-audit` reports (read-only) any drift from the model:

- **Epics live in `.github`.** An open `epic`-labelled issue outside `.github` in
  a *public* repo is a violation (a private repo self-homes legitimately).
- **No stray `.github` issues.** The epic home holds only epics, intake
  (`triage`/`idea`/`research`), and tasks linked under an epic; any other open
  issue there is a stray.
- **An epic is never closed while a child is open.** A closed finite epic with an
  open child is a violation (perpetual `ad-hoc` epics are exempt — they never
  roll up).

## Intake queues

Not every capture is ready to be a task. Three **intake queues** hold uncurated
work until it is groomed into the epic/task model. Each is a label, and all
three route to the org's `.github` **by default**, so the entire org-wide intake
backlog is one filtered view beside the epic roster:

| Kind | Captures | Graduates into |
|---|---|---|
| `triage` | A problem or bug not yet understood — needs diagnosis | an epic (or a task) |
| `idea` | A spark — "what if we did this" | a feature or epic |
| `research` | An investigation that yields a **reproducible** result | an epic with tooling PRs and a report |

A result worth having is worth reproducing, so **research is not ad-hoc work** —
it graduates into a proper finite epic with automated tooling, never a hand-run
one-off.

Create an intake item with the sanctioned tool. `--kind` selects the shape and
stamps its label (default `triage`); the target defaults to the org's `.github`:

```bash
vrg-triage-create --kind {triage|idea|research} --title "<what>"
```

Intake lives in `.github`, never in a member repo — an intake item is not yet a
single-PR task, so the "member repos hold only single-PR tasks" invariant
requires it to sit with the epic roster instead. (This `.github` default
supersedes the earlier current-repo default for `vrg-triage-create`.) Grooming
an intake item into an epic or a task is a separate, periodic review step.

## Closing behavior

- Default: auto-close issues via PR closing keywords.
- If acceptance criteria are specified, do not auto-close. The agent
  finalizing the PR is responsible for determining closure once the criteria
  are met.
- If auto-closing is disabled or the PR targets a non-default branch, close
  the issue manually after merge only when acceptance criteria are satisfied.
- A closed issue must reflect completed work. If work is deferred, keep the
  issue open or create a follow-up issue and link it explicitly.

## Operational tasks

Some work is proven not by merging a PR but by *running* something after merge
and recording the result as a comment. These are **operational tasks** — a
family of not-PR-workable task types. Two kinds:

- **Validation** — *verify* prior work is correct (a cold rebuild, a
  live-environment check, a deploy smoke test). Run with the `issue-validate`
  skill.
- **Deployment** — *make merged work usable*: install/sync/deploy it into the
  environment so the next step can run against it. Run with the `issue-deploy`
  skill.

They share one mechanism; each kind supplies its own label, scaffold, and run
skill.

**Merged vs deployed.** An implementation task closes when its PR merges. But the
next step sometimes needs the change not just *merged* but *deployed and usable*.
A deployment task makes that explicit, and its closure **is** the "deployed"
signal — so the common shape of an epic's tail is **implement → deploy →
validate**, each `Blocked-by` the last.

Shared rules (both kinds):

- **Acceptance is a recorded result, not a merge.** The task is proven by running
  its procedure and posting `Outcome: SUCCESS` (or `Outcome: FAILURE`) as a
  comment.
- **Not PR-workable.** It has no code PR and never auto-closes; the PR tooling
  (`vrg-submit-pr`, `vrg-pr-workflow report-ready`) refuses it.
- **Closes only on SUCCESS.** On failure it stays open — like a pull request that
  cannot merge — and the parent epic stays open too. (Validation files a fix
  issue; deployment retries first, then files a fix issue only for a genuine
  defect.)
- **Gates epic closure** by staying open — an open operational child holds the
  epic until it succeeds, so rollup is honest.
- **Records dependencies as `Blocked-by:` reflinks.** `vrg-epic-audit` reads them
  to report each as *runnable* (dependencies closed) or *blocked*, tagged by
  kind.

Create one with the sanctioned path — never hand-roll the body:

```bash
vrg-issue-create --epic <org>/.github#N --repo <org>/<repo> \
  --kind {validation|deployment} --title "<what>" --blocked-by <org>/<repo>#<TASK>
```

This stamps the kind's label and an executable scaffold: an author-defined
**precondition self-check** (a machine probe or a human-attested statement — no
mechanism is prescribed; if a precondition is unmet, record `blocked` and stop,
never fabricating), the procedure, the acceptance criteria, and a
**SUCCESS/FAILURE results template**.

Add a **validation** task when acceptance needs a check the pipeline's own tests
cannot do (a cold rebuild, a live check, a deploy smoke test); provisioning and
infrastructure work carry a cold-rebuild validation by default. Add a
**deployment** task when the next step needs the change deployed and usable, not
merely merged.

**Deployment autonomy boundary.** A deployment task owns only the **agent-safe**
deploy steps (install/sync/restart). Where deploying needs a **release**
(bump/tag/publish), that release is a **human-gated precondition** — attested,
never performed by the agent — the same policy that keeps PR submission and merge
in human hands.
