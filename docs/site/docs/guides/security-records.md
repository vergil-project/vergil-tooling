# Security records

A security finding never goes into a public issue. Each finding gets a
**security record**: a private draft advisory that holds the details,
paired with a neutral issue that drives the work. This guide covers the
policy and how to work with a record. The command is
[`vrg-security-record`](../reference/dev/security-record.md). The design
is epic
[vergil-project/.github#517](https://github.com/vergil-project/.github/issues/517),
extended to several advisories per issue by epic
[vergil-project/.github#573](https://github.com/vergil-project/.github/issues/573).

## The policy

- **Every security finding gets one record**, except a security
  follow-up that `issue-implement` finds and the human does not rule
  `record` (it is fixed on the branch, or rejected), and a merge-review
  security finding the human rules `fix` or `wont-fix` (see
  [Recording findings from a review](#recording-findings-from-a-review)).
  A record is one pair (an issue may name several advisories, one per
  finding; see [The two reference lines](#the-two-reference-lines)):
  - a **draft repository security advisory** in `<org>/.github`, which
    holds the details;
  - a neutral **`security`-labelled issue** in the intake queue, which
    holds only a title, a body that says nothing about the problem, and a
    pointer to the advisory.
- **The same rule for every repo, public or private.** Issues are always
  neutral, and the details always live in the advisory. A private repo
  gets no exception: its issues are written as if they were public.
- **Fix it within about a day.** Security is a top priority. A record is
  meant to be short-lived: once it is captured, fixing it comes first,
  with a target of about a day. This target is policy. The tooling does
  not enforce it.
- **Advisories are secure notes, not a disclosure process.** A record is
  never published and no CVE is requested. Nothing in the tooling stops a
  human from publishing one later, but that is a separate, deliberate
  decision.

Advisories **complement** issues. They don't replace them: the issue is
what intake review routes, what a task's PR closes, and what the sweep
follows.

### What counts as a security finding

The definition is paad's, used by every paad review skill:

> a finding is security-related if reading it would help an attacker —
> any OWASP Top 10:2025 category, and anything in a payment,
> tenant-isolation, or secret-handling path; on the edge, treat it as
> security.

A paad skill run by hand writes such findings only to its gitignored
`paad/security/` folder. In the review rounds of `epic-create`, paad
runs unattended and writes nothing: it returns
its security findings in a delimited section of its result, which the
caller keeps in a main clone's ignored `build/.vergil-state/` (the
[paad review doctrine §1.5 and §1.9](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/paad-review-doctrine.md#15-state-directories)).
Both are local scratch: they live on one machine and are lost to
`git clean -fdx`, a deleted worktree or a deleted state directory. A
security record is the durable, private home for them.

### Where a record lives

- **The advisory** is always in **`<org>/.github`**, whichever repo the
  finding is about. The affected repo is recorded as the advisory's
  package name (ecosystem `other`), so it stays queryable.
- **The issue** is created in the affected repo's **queue home**, the same
  place `vrg-intake-create` files an item, and linked under its intake
  epic:
  - a public repo's queue home is `<org>/.github`;
  - a private repo keeps its own queues, so its issue stays in the
    private repo.

  It is never created in a public code repo itself.

The org's `.github` repo must be **public**. GitHub documents repository
security advisories for public repos only. When `<org>/.github` is not
public, every `vrg-security-record` command refuses, the sweep fails, and
`vrg-intake-route` and `vrg-queue planning` fail when they meet a security
item. None of them falls back to an issue-only record. Orgs with a private `.github`, and
repos owned by a user account rather than an org, are not supported.

### The two reference lines

Each half of a pair names the other with an exact line, alone on its
line:

- the issue body has one line per advisory it names:
  `Security advisory: <org>/.github <GHSA-id>`;
- the advisory description has one line per issue it tracks:
  `Tracking issue: <owner>/<repo>#<N>`.

So the link is many-to-many. One advisory can track several issues (a
finding fixed by more than one task), and one issue can name several
advisories (one task that fixes more than one finding, or a planning issue
that collected several). An issue keeps the `security` label while **any**
of its advisory lines remains; the label goes only with the last one.

The GHSA ID reveals only that a record exists. Don't edit these lines by
hand; `vrg-security-record` writes and moves them.

### The labels

| Label | Meaning |
|---|---|
| `security` | Has a private security record in the org's `.github` advisories. The sweep checks every issue with this label. |
| `security-disclosed` | Past issue that disclosed a security problem publicly; fixed, no private record. The sweep ignores it. |

## Writing a neutral issue

The tool cannot judge whether text is neutral. **That is the caller's
duty**, whether the caller is a human or an agent. The issue title and
body go to `--title` and `--body-file`; the details go to
`--details-file`, which is sent only to the advisory.

A neutral issue:

- names the **affected area** (a command, a module, a workflow) so that
  intake review can route it, and nothing more;
- does not describe the weakness, how to trigger it, what an attacker
  gains, or which input, path, endpoint or secret is involved;
- has no severity in its title or body (the severity is on the
  advisory);
- carries no excerpts of code, logs, payloads or tool output that show
  the problem.

The same rule covers everything public that the work produces: comments,
commit messages, PR titles and bodies, `report-ready` notes and the relay
ref, and Actions logs. Say "fix the security record for `<area>`", or
name the GHSA ID; never restate the finding.

The examples on this page use placeholder text, never a real finding. A
neutral title is `Test record: security-records API check`, and a neutral
body is `Tracked privately; see the security advisory named below.` The
details file holds everything else, and its first line becomes the
advisory summary unless `--summary` is given:

```text
PROBE: security-records API check (no real finding)

Probe for epic #517. Not a vulnerability.
```

## Severity

An advisory's severity uses GitHub's terms: `critical`, `high`, `medium`
or `low`. Map a paad severity to it with the table in the
[paad review doctrine §1.3](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/paad-review-doctrine.md#13-severity),
which covers every paad review skill. That table is the only copy, so
this guide links to it rather than repeating it.

A finding with no paad severity (one an agent or a human found directly)
is given a severity by whoever records it.

## Recording a finding

```bash
vrg-security-record create --repo vergil-project/vergil-tooling \
  --title "Test record: security-records API check" \
  --body-file neutral-body.md \
  --details-file details.md \
  --severity low
```

`create` runs the steps in a fixed order: a duplicate check, the advisory,
the issue, the link under the intake epic, then the advisory's
`Tracking issue:` line. If a step fails after the advisory exists, it
prints the exact command that finishes the pair and exits 1. Drafts can't
be deleted, so never re-run the original command; run the printed one.
See the [CLI reference](../reference/dev/security-record.md#create) for
attach mode (`--issue`) and resume (`--resume`).

## Managing a record through its issue

**The issue drives the record.** Work the issue like any other: route it,
build the fix, close it with the PR. Never change an advisory's state by
hand.

The scheduled sweep (`vrg-epic-audit --close`, run by the org's automation
App) keeps each advisory's state in step with the issues it tracks:

- the advisory is **closed** when **every** issue it tracks is closed,
  whether fixed or dropped as not planned;
- it is a **draft** while **any** of them is open, so reopening an issue
  reopens its advisory.

A manual state change to a draft or closed advisory is overwritten on the
next run. The sweep only moves an advisory between `draft` and `closed`.
It never touches an advisory in any other state (published, withdrawn, or
`triage`), and never touches an unpaired external report.

Each run also checks every pair and reports the broken ones, by GHSA ID
and issue ref only. A pair is broken when:

- a `security` issue has no `Security advisory:` line, or any of its
  lines names an advisory that does not exist or does not track it;
- an advisory has no `Tracking issue:` lines, or tracks an issue that is
  not a `security`-labelled issue, or one that does not point back;
- an advisory is not named by any `security` issue and is not an
  external report.

The sweep reports a broken pair and does not repair it; a human decides
what to do. Broken pairs make `vrg-epic-audit --close` exit 1, after it
has synced the rest. An org whose `.github` is private also makes it exit
1.

**External reports.** `.github` accepts private vulnerability reports from
outside (see its `SECURITY.md`). GitHub marks such a report with the
`triage` state or a submission. When no `security` issue names one, the
sweep lists it as an *unpaired external report*, never treats it as
broken, and never changes it. To work on one, attach it to an issue with
`vrg-security-record create --issue`.

The sweep's output goes to the public Actions logs of `.github`, so it
names only GHSA IDs, issue refs, labels and states, never advisory text.

### When the issue moves

A record's link moves only through `vrg-security-record retarget`, and
`vrg-intake-route` calls it for you:

- **transfer** (`one-off` into another repo): after the move, **every**
  advisory the issue names is retargeted, in body order, so each tracking
  line is rewritten to the issue's new number. If one fails, the move
  stands and the error names every advisory still not retargeted, with
  one `retarget` command for each, to run in order;
- **recreate** (when GitHub refuses the transfer): **every** advisory the
  issue names is retargeted to the copy, in body order, **before** the
  original closes. If one fails, the original stays open, the copy stays
  unlinked, and the error names every advisory still not retargeted and
  how to finish;
- **split**: an item naming **one** advisory has it retargeted to every
  task in `--into` **before** the original closes. If that fails, the
  route stops and the original stays open. The advisory then closes only
  once every task is closed.

  An item naming **several** advisories is refused before anything
  changes, because which advisory goes to which task can't be inferred:

  ```text
  vrg-intake-route: <owner>/<repo>#<N> names several advisories (<GHSA-id>, <GHSA-id>); retarget each by hand with vrg-security-record retarget, then split
  ```

  Retarget each advisory to the task or tasks that fix it, one at a time.
  The last retarget leaves the item with no advisory line and no
  `security` label, so the split that follows is an ordinary one.

An issue an advisory stops tracking loses that advisory's line, and gets
the neutral comment `Security record moved to <refs>`. It loses its
`security` label only when that was its last advisory line; while it
still names another advisory, the label and the other lines stay.

## Security items in planning

A security item can go through planning like any other. Planning
discusses the problem in general terms; the details stay in the advisory.
A planning issue may name several advisories, and it keeps tracking each
of them until the plan exists.

- **Hand each advisory to its fix.** When `epic-create` files the tasks
  (its step 9), retarget **each** advisory the planning issue names from
  the planning issue to the task or tasks that fix it. Run one
  `retarget` per advisory, **one after another, never in parallel**: each
  retarget rewrites the planning issue's body, so two at once can write
  back a line the other removed. Post `Tasks filed: yes` only after every
  retarget has succeeded:

  ```bash
  vrg-security-record retarget GHSA-xxxx-xxxx-xxxx \
    --to 'vergil-project/vergil-tooling#101' \
    --to 'vergil-project/vergil-tooling#102'
  vrg-security-record retarget GHSA-yyyy-yyyy-yyyy \
    --to 'vergil-project/vergil-tooling#103'
  ```

  From then on, the PRs that close those tasks are what close each
  advisory.
- **A finding the design itself resolves.** A finding that the revised
  design resolves on its own, with no task implementing it (for example,
  the spec now removes the mechanism it was about), is retargeted to the
  epic's **documentation task**, whose PR publishes the revised spec. Do
  this **only** when the weakness exists only in the design and no
  shipped code has it. A finding about shipped behaviour always goes to a
  fix task that changes the code.
- **The guard.** `vrg-queue planning` never shows a planning issue as
  `ready-to-close` while **any** advisory still tracks it, whether or not
  its body names that advisory. It
  shows it as `in-progress` with a note that counts the advisories still
  tracking it and names them by GHSA ID only:

  ```text
  held: 1 advisory still tracks this planning issue (GHSA-xxxx-xxxx-xxxx) — retarget each to the task or tasks that fix it with vrg-security-record retarget
  held: 2 advisories still track this planning issue (GHSA-xxxx-xxxx-xxxx, GHSA-yyyy-yyyy-yyyy) — retarget each to the task or tasks that fix it with vrg-security-record retarget
  ```

  After one of two retargets, the planning issue stays held by the other.
  Don't close the planning issue by hand before every retarget: that
  would close the remaining advisories with the fix unwritten.

## Recording findings from a review

Security findings can surface while the work is still being designed or
built. Three recipes cover the findings raised inside the epic/task
framework: design review during `epic-create`, the security
follow-ups `issue-implement` finds, and the security findings of a
task's merge review. All use `create`; design review
also uses `retarget`, through `epic-create`'s step 9. As everywhere on
this page, the refs and GHSA IDs are placeholders, not a real finding.

**Design review, during `epic-create`.** The review runs in rounds
against the spec and the plan while a planning issue tracks the work.
Each finding is recorded once the human has decided it. Only a finding
that is new, or that repairs an earlier round's change, is recorded: one
an earlier round already decided has its record. How it is recorded
depends on the decision:

- an **accepted** finding (the spec or plan now states the requirement)
  is recorded on the planning issue with attach mode,
  `vrg-security-record create --issue <planning issue> …`. Step 9 then
  hands it off to its fix like any other advisory the planning issue
  names (see [Security items in planning](#security-items-in-planning));
- a **rejected** finding (the human decided it is not a problem) is
  recorded with plain `create`, and the intake issue it creates is then
  closed as "not planned", so the sweep closes the advisory. The human's
  reason goes into the advisory's details, never into a public record;
- a **dropped** finding is recorded with plain `create`, and its intake
  issue stays open, so intake review decides what becomes of it.

The round's comment on the planning issue gives only the count of
security findings, how many were accepted, and the GHSA IDs the round
created. The rules are in the
[paad review doctrine §2.7](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/paad-review-doctrine.md#27-security-findings).

**Security follow-ups, during `issue-implement`.** A security follow-up
that `issue-implement` finds while it implements a task (a gap it hits,
or a concern a sub-agent reports) stays private until the human rules on
it. The task's public records give it only as a count; its details stay
in the conversation, or, when `epic-implement` carries out the human's
rulings, in the task's private directory,
`build/.vergil-state/issue-implement/<N>/` in the main clone of the
task's repo. Only a `record` ruling creates a record:

- **`fix`**: the finding is fixed on the branch and not recorded. The
  weakness never reached `develop`, so it needs no advisory, and the
  task's records give it only as a count;
- **`record`**: the finding is recorded with plain `create`, which gives
  it its own intake issue. A finding the task's records already show as
  recorded is not recorded again;
- **`reject`**: no fix and no record, and no public record gives a
  reason.

A follow-up is never attached to the task, which is why it is recorded
with plain `create` and no `--issue`: a record on the task would mark the
task publicly, pointing from the shipped diff to the open record. The
rules are in the
[paad review doctrine §1.7](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/paad-review-doctrine.md#17-creating-issues).

**Security findings, during a merge review.** A security finding the
[merge review](merge-review.md#security-findings) raises keeps its
details in the task's private directory,
`build/.vergil-state/issue-implement/<N>/merge-review/security-<ID>.md`,
and every public record names it by ID or count only. The human rules on
it through `vrg-ruling` (see [Human Replies](human-replies.md#security-items)):

- **`fix`**: fixed on the branch and not recorded, as for a follow-up;
- **`file`** or **`defer`**: recorded with plain `create` and no
  `--issue`, before the ruling's response is posted, so the response's
  `filed` or `deferred` outcome is real. The merge review does not hand
  off while any such finding is still unrecorded;
- **`wont-fix`**: an accepted risk, with no record. The task's closing
  comment and the PR notes give only the count, as `accepted-risk`.

## Checking existing records after the several-advisories change

An issue naming one advisory is a valid case of the several-advisories
form, so existing records need no converting and there is no migration
code. To confirm an org's live records are consistent, check them by
hand:

- `vrg-security-record list` shows every advisory and the issues it
  tracks;
- `vrg-epic-audit` (read-only) prints the sweep's pass as a preview,
  including its broken-pair report.

The sweep reports a broken pair and never repairs it, so a human repairs
each one it reports, by hand.

## Before a private repo goes public

Because issues are neutral everywhere, a private repo's issues hold no
security details, and nothing needs converting. Some places are outside
the tooling's reach, though. Before you make a private repo public, check
each of these by hand:

- [ ] **Git history and commit messages**, on every branch and tag that
      will be published.
- [ ] **PR bodies and review comments**, including closed PRs.
- [ ] **Wikis and Actions logs.** Logs become public with the repo.
- [ ] **Issues written before this policy existed.** Run the past-issues
      audit on the repo: attach a record to each unfixed one
      (`vrg-security-record create --issue`), and label each fixed one
      `security-disclosed`.

The repo's epics move separately; see
[Epic Home Visibility Flips](epic-home-visibility-flip.md).

## Accepted trade-offs

- **Advisories are not next to their code.** Every record is in
  `<org>/.github`, so every admin and security manager of `.github` can
  see every draft, including one about a private (for example, client)
  repo. Today those are the same people. The package-name field keeps the
  affected repo queryable.
- **A public issue shows that a problem exists.** For a public repo the
  issue is public. Its `security` label and advisory line, together with
  a title that names the affected area, tell anyone that an unfixed
  security problem exists in that area. The issue never says what the
  problem is, and the record is meant to live about a day. This small,
  deliberate disclosure is accepted in exchange for a visible, queryable
  label.
- **Agents may manage advisories.** Records are notes: the user agent
  creates, edits and retargets them, and the sweep closes and reopens
  them. There is no strong protection against an agent editing or closing
  one.

## Permissions

`vrg-security-record` calls the advisories API directly, with the
identity's App token or, in human mode, the human's own `gh` login. It
does not go through `vrg-gh api`, which stays denied to the user agent.

Two Apps need **Repository security advisories: Read and write**:

- every user App (`<username>-vergil-user`), for `vrg-security-record`
  and the hooks in `vrg-intake-route` and `vrg-queue`;
- each org's automation App (`<org>-automation`), for the sweep.

The audit App does not get it; see
[Account Setup](account-setup.md#step-11-register-the-app).

## Related

- [vrg-security-record](../reference/dev/security-record.md): the CLI
  reference
- [Account Setup](account-setup.md): App permissions
- [Identity Architecture](identity-architecture.md#the-org-automation-apps):
  the org automation Apps
- [GitHub Issues](../standards/github-issues.md#intake-planning-and-deferred-queues):
  the intake, planning and deferred queues
