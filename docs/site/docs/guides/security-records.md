# Security records

A security finding never goes into a public issue. Each finding gets a
**security record**: a private draft advisory that holds the details,
paired with a neutral issue that drives the work. This guide covers the
policy and how to work with a record. The command is
[`vrg-security-record`](../reference/dev/security-record.md). The design
is epic
[vergil-project/.github#517](https://github.com/vergil-project/.github/issues/517).

## The policy

- **Every security finding gets one record.** A record is one pair:
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

paad writes such findings only to its gitignored `paad/security/` folder,
which is local scratch: it lives on one machine and is lost to
`git clean -x` or a deleted worktree. A security record is the durable,
private home for them.

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

- the issue body ends with
  `Security advisory: <org>/.github <GHSA-id>`;
- the advisory description has one line per issue it tracks:
  `Tracking issue: <owner>/<repo>#<N>`.

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
or `low`. Map a paad severity to it with this table:

| GitHub severity | agentic-review | agentic-owasp | pushback, agentic-architecture |
|---|---|---|---|
| `critical` | Critical | Critical | n/a |
| `high` | Important | High | High |
| `medium` | Suggestion | Medium | Medium |
| `low` | n/a | Low | Low |

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

- a `security` issue has no `Security advisory:` line, or names an
  advisory that does not exist or does not track it;
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

- **transfer** (`one-off` into another repo): after the move, the
  advisory's tracking line is rewritten to the issue's new number. If
  that fails, the move stands and the error prints the `retarget` command
  to run;
- **recreate** (when GitHub refuses the transfer): the advisory is
  retargeted to the copy **before** the original closes. If that fails,
  the original stays open and the error says how to finish;
- **split**: the advisory is retargeted to every task in `--into`
  **before** the original closes. If that fails, the route stops and the
  original stays open. The advisory then closes only once every task is
  closed.

An issue the advisory stops tracking loses its `security` label and
advisory line, and gets the neutral comment
`Security record moved to <refs>`.

## Security items in planning

A security item can go through planning like any other. Planning
discusses the problem in general terms; the details stay in the advisory.
The planning issue keeps tracking the advisory until the plan exists.

- **Hand the advisory to the fix.** When `epic-create` files the tasks
  (its step 9), retarget the advisory from the planning issue to the
  task or tasks that fix the problem, then post `Tasks filed: yes`:

  ```bash
  vrg-security-record retarget GHSA-xxxx-xxxx-xxxx \
    --to vergil-project/vergil-tooling#101 \
    --to vergil-project/vergil-tooling#102
  ```

  From then on, the PRs that close those tasks are what close the
  advisory.
- **The guard.** `vrg-queue planning` never shows a planning issue as
  `ready-to-close` while an advisory still tracks it. It shows it as
  `in-progress` with the note `held: advisory <GHSA-id> still tracks this
  planning issue — retarget it to the fix task(s) with vrg-security-record
  retarget`. Don't close the planning issue by hand before the retarget:
  that would close the advisory with the fix unwritten.

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
