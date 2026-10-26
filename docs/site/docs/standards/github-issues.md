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
- Intake item: An uncurated capture — a bug, an idea, an investigation, a
  one-line "don't forget X" — linked under its home's **intake epic** until
  intake review routes it. It carries no kind label: its parent *is* its state.
  See [Intake and planning queues](#intake-and-planning-queues).
- Planning item: A reviewed item judged to need a full epic, linked under its
  home's **planning epic** while it waits for (or undergoes) `epic-create`.

## The invariant: every issue belongs to an epic

In any repo with a `vergil.toml`, **every issue is either a legitimate top-level
kind or has an epic parent.** The legitimate top-level kinds are:

- **epics** (`epic` label), including the standing ad-hoc, intake, and planning
  epics;
- **archives** (`archive` label) — the per-quarter buckets of closed standing-epic
  children;
- **release tracking issues** (`release: X.Y.Z`), which `vrg-release` creates
  top-level for the length of a release.

There is no opt-out and no "unlinked" state. An issue's parent tells you where
it stands:

| State | Parent | Buildable? |
|---|---|---|
| **Intake** — captured, not reviewed | the intake epic | no |
| **Planning** — needs a full epic | the planning epic | no |
| **Ready** — a one-off, or part of a planned initiative | a repo's ad-hoc epic, or a finite epic | yes |

The sanctioned tools create every issue already linked (`vrg-issue-create`
requires `--epic`; `vrg-intake-create` links under intake). Issues born outside
the tools — the web UI, a script, a direct API call — are **healed, not
rejected**: the daily sweep attaches every orphan to its home's intake epic
(see [Compliance invariants](#compliance-invariants)).

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
  on demand (idempotent). Its *closed* children are continuously drained into
  per-quarter **archives** so the live umbrella shows only in-flight work — see
  [Ad-hoc epic archiving](#ad-hoc-epic-archiving).

### Standing epics

The ad-hoc epic is one of three kinds of **standing epic** — perpetual
umbrellas that are created and reused idempotently, never auto-close, and never
gate rollup:

| Kind | Title | Labels | Where | Holds |
|---|---|---|---|---|
| Ad hoc | `Epic (ad hoc): <repo>` | `epic` + `ad-hoc` | one per repo, in the repo's epic home | ready one-off work |
| Intake | `Epic (intake): <name>` | `epic` + `intake` | one per queue home | captured, unreviewed items and every orphan the sweep finds |
| Planning | `Epic (planning): <name>` | `epic` + `planning` | one per queue home | items waiting for or undergoing `epic-create` |

The intake and planning **queue home** follows the
[epic-home rule](#epic-home-the-orggithub-rule): every public repo shares the
org's pair in `<org>/.github` (`<name>` is the org), while a **private** repo
self-homes its own pair (`<name>` is the repo), so private work never surfaces
in a public issue. `vrg-issue-create --epic` accepts `adhoc`, `intake`, and
`planning` as sentinels that resolve (creating if absent) the target repo's
standing epic of that kind.

### Ad-hoc epic archiving

A live ad-hoc epic accumulates closed children forever, so its "what's in
flight" signal drowns in finished work. To keep the umbrella honest, closed
children are **continuously drained into per-quarter archives** — a live
ad-hoc epic ends up holding only its still-open children.

- **Archives** are titled `Archive (ad hoc): <repo> — <YYYY>-Q<n>` (the
  separator is a space, an em-dash `—`, and a space). They live in the same
  resolved home as the live epic and carry the `archive` + `ad-hoc` labels. The
  `archive` label replaced the `epic` label these buckets used to carry (`ad-hoc`
  is kept), so terminal per-quarter buckets no longer clutter `label:epic`
  views; only the live `Epic (ad hoc): <repo>` epic keeps `epic` + `ad-hoc`.
- **Bucketing is by close quarter.** Each closed child is re-parented into the
  archive for the quarter of its `closedAt` timestamp (UTC), so the `YYYY-Qn`
  archive holds the ad-hoc work that *finished* in that quarter — not the work
  filed in it.
- **The live epic is never renamed, recreated, or closed.** It only loses its
  closed children to the archives; it stays the one perpetual umbrella per repo.
- **Only canonical per-repo ad-hoc epics are archived.** Both the batch drain and
  the on-close event path key on the repo named in the `Epic (ad hoc): <repo>`
  title, so a closed child always lands in *its own repo's* quarter archive — even
  for a public repo whose ad-hoc epic is homed centrally in `.github`. A
  special-purpose `ad-hoc`-labelled epic whose title bare-name is a description
  rather than a real repo (e.g. a standing reliability collection) is **left
  untouched** — it is not treated as a drainable ad-hoc epic.
- **Past-quarter archives are closed; the current-quarter archive stays open.**
  A closed archive is a stable historical record; the current one keeps
  receiving this quarter's closures. A late straggler whose close-quarter
  archive was already closed is re-parented into it regardless.
- **Idempotent and dry-run by default.** Every step is check-before-act
  (archives are create-if-missing by exact title, an already-parented child is
  skipped, an already-closed past archive is skipped), so a run that dies
  partway is repaired by the next one. Empty quarters get no archive. A single
  drain creates **at most one archive per quarter** however many same-quarter
  children it moves, and if a duplicate archive ever exists the **oldest is
  reused** rather than the run failing on the ambiguity — so the drain is safe
  to re-run and self-heals.
- **Intake and planning drain the same way.** Closed intake and planning items
  (dropped, split, planned) move into `Archive (intake): <name> — <YYYY>-Q<n>`
  and `Archive (planning): <name> — <YYYY>-Q<n>` buckets (labels `archive` +
  `intake` / `planning`), so only open items count against GitHub's
  100-sub-issue cap. An archive bucket holds at most 95 children; a full one
  rolls over to a new segment.

#### Running the drain

```bash
# Dry-run: report which closed children move to which quarter archive,
# which archives would be created, and which past archives would close.
vrg-adhoc-epic archive --repo <owner>/<repo>

# Apply the moves and closes.
vrg-adhoc-epic archive --repo <owner>/<repo> --apply

# Visibility-aware sweep across every repo in an org.
vrg-adhoc-epic archive --all-in <ORG> --apply
```

`--repo` targets a single repo; `--all-in <ORG>` sweeps every repo in the org,
resolving each repo's ad-hoc home by visibility. Without `--apply` the command
mutates nothing.

#### Normalizing legacy archives

Archives created before the `archive`-label rename carry the old
`Epic (ad hoc): <repo> — <YYYY>-Qn` title and the `epic` label. `vrg-adhoc-epic
normalize` converts them in place to the current form — retitling to
`Archive (ad hoc): …`, adding `archive`, and removing `epic` (keeping `ad-hoc`):

```bash
# Dry-run: list every legacy-form archive that would be converted.
vrg-adhoc-epic normalize --all-in <ORG>

# Apply the conversions.
vrg-adhoc-epic normalize --all-in <ORG> --apply
```

It sweeps every repo's resolved home, so it covers both the centrally-homed
archives of public repos and the self-homed archives of private ones, and it
converts both open and closed archives. It is **idempotent** — an
already-converted archive is new-form and is skipped — so it is safe to re-run
and doubles as a straggler net. The creation path is also self-healing: the next
time the drain touches a quarter whose archive is still in legacy form, it heals
that archive in place rather than minting a duplicate, so there is no ordering
hazard between the code landing and this sweep running.

#### Automation (no new schedule)

Two existing call sites keep archives current with no dedicated cron:

- **Steady state — on close.** When a child of a *live* ad-hoc epic closes, the
  `issues.closed` rollup path (`vrg-epic-rollup`) archives just that child into
  its close-quarter archive.
- **Backstop + backfill.** The daily `vrg-epic-audit --close` sweep runs the
  org-wide drain for every standing kind (ad-hoc, intake, planning): it
  distributes any backlog the steady-state path missed and closes past-quarter
  archives. This is the same trusted sweep that closes other
  provably-complete drift, so it is allowed to mutate under the scheduled
  automation signal.

### Creating an epic (the `epic-create` workflow)

Non-trivial work starts with the `epic-create` workflow — the **default entry
point**, since a solution worth designing is worth tracking rather than
brainstorming and walking away. Every run is tracked in a **planning item** (see
[Planning](#planning)): `epic-create <issue>` starts or resumes a planning item
(an intake item is first routed to planning), a free-text idea first becomes a
new planning item, and a bare `epic-create` summarizes the planning queue and
asks which one to work on. `epic-create` is the **outer** orchestrator: it
runs the whole design pipeline and seeds the epic's bookend tasks (below) at
defined handoffs:

1. `superpowers:brainstorming` — explore intent, one question at a time
   (**interactive**).
2. Initialize the epic in its [home](#epic-home-the-orggithub-rule) and seed the
   bookend tasks — the docs-first opening task, the documentation-review sweep,
   and the terminal **retrospective** (see the bookend convention below).
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

**An epic is never closed until you have decided what comes next AND confirmed
the docs reflect what changed.** Almost no real problem is fully resolved by one
epic — you deliver a completed subset and acknowledge the follow-on. Epic work
runs through a **three-skill lifecycle**, each with a distinct cardinality:
`epic-create` (**once** — open the epic) → `epic-implement` (**N times** — drive
the tasks, pausing and resuming as blockers allow) → `epic-retrospective`
(**once** — the terminal finishing gate). So every epic carries fixed bookends:

- **Opening bookend — documentation.** A docs-first task carrying the epic's
  spec and plan, born from planning; it lands before the implementation tasks.
- **Closing bookends — ordered:**
  1. **Documentation-review sweep** (runs first) — verify the shipped work is
     fully reflected in the human-facing docs across every repo the epic touched;
     it spawns per-repo doc tasks where other repos' docs need work.
  2. **Retrospective (terminal — the final gate)** — a mandatory, **single**,
     backward-looking record that partners the spec and plan
     (`epics/<N>-<slug>/retrospective.md`), so a later reader follows
     **spec → plan → retrospective**. Authored with `epic-retrospective`, whose
     preflight refuses to run until every other child of the epic is closed; its
     docs PR **closes the epic**.
- **Follow-on brainstorm — semi-optional.** Reviewing what shipped and
  brainstorming the follow-on epic(s) is a **forward-looking** concern that **no
  longer defines the end of an epic**: seed it at creation only when a known
  enabling chain already exists, otherwise accrue it during implementation or
  skip it. Its outcomes are recorded in the retrospective.

The bookends need no new closing mechanism — they ride the existing rollup. A
finite epic rolls up only when every child is closed, and the retrospective is
the last to close, so it **naturally gates closure** as the final bookend. The
convention is mechanized as prose in the `epic-create` / `epic-retrospective`
skills (`vergil-claude-plugin`), not in rigid tooling, because the retrospective
and any follow-on are inherently agentic judgment.

### Epic home (the `<org>/.github` rule)

An epic's home repo is derived from repository visibility:

- A **public** repo homes its epics centrally in the org's `.github`.
- A **private** repo (with a public `.github`) homes its epics **in itself**.
- A **private** `.github` means the whole org is private, so everything homes in
  `.github`.
- A **private** repo whose owner has **no** `.github` at all (typically a
  personal account) homes its epics **in itself**: there is no central home.
  Only GitHub's "repository does not exist" answer counts as no `.github`; any
  other failure to read its visibility is an error.

Ad-hoc epics follow the same rule — `Epic (ad hoc): <repo>` lives in the repo's
resolved home. See
[Epic home visibility flips](../guides/epic-home-visibility-flip.md) for
relocating epics when a repo's visibility changes.

### Compliance invariants

`vrg-epic-audit` reports (read-only) any drift from the model:

- **Epics live in `.github`.** An open `epic`-labelled issue outside `.github` in
  a *public* repo is a violation (a private repo self-homes legitimately).
- **No stray `.github` issues.** The epic home holds only epics, the
  `archive`-labelled per-quarter buckets, and issues linked under an epic —
  managed tasks and intake/planning items alike; any other open issue there is a
  stray.
- **An epic is never closed while a child is open.** A closed finite epic with an
  open child is a violation (perpetual standing epics are exempt — they never
  roll up; their closed children are drained into per-quarter archives
  instead, see [Ad-hoc epic archiving](#ad-hoc-epic-archiving)).
- **Every issue has an epic parent** (the
  [invariant](#the-invariant-every-issue-belongs-to-an-epic)). The audit lists
  each **orphan** — an open issue in a managed repo (non-archived, with a
  `vergil.toml`) that is not a legitimate top-level kind and has no epic parent —
  together with the intake epic it would be attached to. It also lists issues
  still carrying the retired `triage` / `idea` / `research` labels, issues
  parented only by a body `Parent:` line, and self-homed intake/planning epics
  in a repo that has since become public.
- **No private work under public epics.** An issue in a private repo parented
  under a public `.github` epic is reported as a likely unsupported
  public → private flip, for a human to resolve (see
  [Epic home visibility flips](../guides/epic-home-visibility-flip.md)). Tooling
  never migrates or retracts it.

#### The orphan pass (auto-heal)

`vrg-epic-audit --close` — run daily by the scheduled `ops-epic-sweep` workflow
with `VRG_EPIC_SWEEP=1`, or by a human — follows its drift closes and drains
with an org-wide **orphan pass**, in order:

1. **Nativize** body-only `Parent:` links into native sub-issue links, so every
   consumer reads the same parent.
2. **Strip** the retired `triage` / `idea` / `research` labels from every open
   issue (its parent is left alone).
3. **Move** a now-public repo's self-homed intake and planning items under the
   `.github` queues, then close the emptied self-homed epics — the only epics
   the pass ever closes.
4. **Attach** every orphan to its home's intake epic (creating it if absent).
   Placement is intake review's job, not the sweep's.

The pass is idempotent and never closes an item. Before each link it checks the
target queue epic's open children against the cap (95, under GitHub's
100-sub-issue limit); a full intake or planning epic **stops the pass** with
exit 1, listing every item it did not link — never a silent skip. A full intake
queue is a signal to run intake review. The same pass, dispatched once by hand
through `ops-epic-sweep`'s `workflow_dispatch`, is the one-time migration of an
org's existing backlog.

## Intake and planning queues

Not every capture is ready to be built. Work flows through two **standing
queues** before it is ready — **intake → planning → ready** — and the queue an
item sits in is simply its parent epic (see
[Standing epics](#standing-epics)). There are no kind labels and no fourth
state.

### Capture: `vrg-intake-create`

Capture has near-zero friction: a one-line "don't forget X" is a valid intake
item, with no category to choose. Create one with the sanctioned tool (the
`intake-capture` skill drives it):

```bash
vrg-intake-create --title "<what>" [--body TEXT | --body-file PATH]
```

The issue is filed in the current repo's queue home — `<org>/.github` for a
public repo, the repo itself for a private one (override with `--repo`) — and is
linked under that home's intake epic as it is created; there is no unlinked
path. `vrg-triage-create` survives for one release as a **deprecated alias**: it
warns, ignores `--kind`, and forwards to `vrg-intake-create`.

### Viewing the queues: `vrg-queue`

```bash
vrg-queue intake    # open intake items, oldest first
vrg-queue planning  # open planning items: waiting / in-progress / ready-to-close
vrg-queue deferred  # open deferred items, least recently deferred first
vrg-queue           # all three queues, in that order
```

It defaults to the current org's `.github` queues; `--repo <owner>/<repo>`
selects that repo's queue home (a self-homed private repo's own queues), and
`--json` emits machine-readable output. It is read-only.

### Intake review: `vrg-intake-route`

Intake is reviewed oldest first, in small batches (the `intake-review` skill).
For each item the reviewer checks its current state and recommends a route; the
human confirms the batch; the route is applied with `vrg-intake-route`, which
owns the mechanics so nothing is hand-rolled:

| Route | Command | Effect |
|---|---|---|
| Drop | `vrg-intake-route <ref> drop --reason TEXT` | Comment the reason and close (not planned). Closed is archived and stays searchable. |
| Defer | `vrg-intake-route <ref> defer --reason TEXT --revisit-when TEXT [--date YYYY-MM-DD]` | Move the item into the deferred queue with a dated record of why it waits, when to look again, and where it came from. |
| One-off | `vrg-intake-route <ref> one-off --repo OWNER/REPO [--epic REF]` | Move the item into its target repo and link it under that repo's ad-hoc epic (default) or a named epic. |
| Split | `vrg-intake-route <ref> split --into REF [--into REF …] --reason TEXT` | Comment links to the replacement tasks (already created with `vrg-issue-create`), then close. |
| To planning | `vrg-intake-route <ref> planning` | Reparent from the intake epic to the planning epic of the same queue home. |

A move **transfers** the issue, keeping its history; if GitHub refuses the
transfer, the item is recreated in the target repo with a link back and the
original closed with a pointer. Either way the resulting issue is re-linked under
its new parent. A route is refused when the item's container epic does not allow
it (see [`vrg-intake-route`](../reference/cli-tools-overview.md#vrg-intake-route)
for the route table), for a cross-org target, and for a private item headed to a
public repo or epic.

### Planning

A planning item records its own epic-creation process: `epic-create` always
works through one, and posts progress comments (what is done, what is blocked,
where to resume) so a session can pause and pick up days later. Three states,
nothing more:

| State | Representation |
|---|---|
| Waiting | Open under the planning epic, no `in-progress` label |
| In progress | Open under the planning epic, `in-progress` label; detail in the latest progress comment |
| Completed | Closed — "planned as #N", or with the reason it is not worth doing |

Progress comments carry machine-readable marker lines — `Epic: <ref>`,
`Docs task: <ref>`, and `Tasks filed: yes` (the latest of each wins) — and the
epic body gains a `Planning: <ref>` line. `vrg-queue planning` reports an item
**ready to close** once it is `in-progress`, all three markers are present, and
the docs task is closed (its spec + plan PR merged).

### Queue items are not buildable

An item parented under an intake or planning epic is not PR-workable:
`vrg-pr-workflow report-ready` and `vrg-submit-pr` (and, upstream, the
`issue-implement` skill) refuse it, naming the step to take first — route it
with intake review, or expand it with `epic-create`. Routing it out of the queue
is the only way to lift the refusal. The check reads the issue's native parent,
not a label.

### Epics and archives are not tasks

An `epic`- or `archive`-labelled issue is an umbrella, never a task, so the
tooling refuses one wherever a task is expected: `vrg-pr-workflow report-ready
--issue` and `vrg-submit-pr` (a PR closes exactly one task, and an epic is
closed only by rollup), `vrg-epic-link --task`, and `vrg-issue-create
--blocked-by`. Create the task under the epic first and use its number. This
check reads the issue's labels; a failed read refuses rather than passing,
except at `report-ready`, which defers to `vrg-submit-pr`'s check.

## Labels

Every managed repo carries the same label set. The canonical registry is
`src/vergil_tooling/data/labels.json` in vergil-tooling.
`vrg-github-repo-init` provisions it into a new repo, and
`vrg-ensure-label --sync --repo <owner>/<repo>` brings an existing repo
up to date (see
[`vrg-ensure-label`](../reference/cli-tools-overview.md#vrg-ensure-label)).

| Label | Meaning |
|---|---|
| `bug` | Defect or incorrect behavior to fix |
| `feature` | New feature or capability |
| `docs` | Documentation-only change |
| `style` | Formatting, whitespace, no code change |
| `refactor` | Code restructuring, no behavior change |
| `test` | Adding or updating tests |
| `chore` | Maintenance, tooling, configuration |
| `ci` | CI/CD pipeline changes |
| `build` | Build system or dependencies |
| `rtfm` | Resolved by reading existing docs |
| `observability` | Monitoring, reporting, health checks |
| `dependency-update` | Mechanized dependency update; an open one gates the next release (see [Dependency updates](../guides/releasing.md#dependency-updates)) |
| `epic` | Umbrella issue; finite epics close when all sub-issues close |
| `ad-hoc` | Perpetual umbrella for ad-hoc work; never auto-closes |
| `intake` | Marks the standing intake epic (with `epic`) and its archives (with `archive`); never auto-closes |
| `planning` | Marks the standing planning epic (with `epic`) and its archives (with `archive`); never auto-closes |
| `in-progress` | Planning item whose `epic-create` session is underway |
| `archive` | Terminal per-quarter archive of closed standing-epic work (ad-hoc, intake, planning); historical record |
| `hotfix` | Emergency fix that skipped formal planning; flagged for retroactive review |
| `validation` | Post-merge validation: run the checklist, record PASS/FAIL as a comment; never auto-closed |
| `deployment` | Deployment task: install/sync merged changes so they are usable; never auto-closed |
| `retrospective` | Epic retrospective bookend; PR-workable, its docs PR publishes `retrospective.md` |

The parent epic carries an issue's state, so no label restates it. `intake` and
`planning` label only the standing epics (and their archives), the way `ad-hoc`
does, and `in-progress` is the single per-item label, used only on planning
items. The former intake kind labels `triage`, `idea`, and `research` are
**retired**: no tool or skill applies them, and the daily orphan pass strips
them from every open issue. They are no longer in the registry, and a label
sync (`vrg-ensure-label --sync`) deletes them from any repo that still has
them, which also strips them from any issue still carrying them.

`dependency-update` goes on PRs, not issues: `vrg-update-deps` and the
`vrg-release` `update-deps` stage put it on every dependency-update PR
they open, and create the label first if the repo does not have it.

Every command that applies a label works the same way: label presence is
ensured **at the point of use**, not by a periodic sync. `vrg-intake-create`,
`vrg-issue-create`, and the epic and ad-hoc commands create each label they
are about to apply (idempotently, with the registry's colour and description)
before they apply it, so a repo that was never label-synced still works. A
label that is not in the registry is refused with an error rather than created
or dropped, and no issue is created.

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
  kind. The same `Blocked-by:` reflink works on a **plain task** too — pass
  `--blocked-by` to `vrg-issue-create --kind task` and it is appended to the task
  body — so an epic driver's plain-task frontier (which open tasks have every
  dependency closed) is machine-derivable, not inferred from plan prose.

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
