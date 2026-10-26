# vrg-security-record

**Installed as:** `vrg-security-record` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_security_record.py` (library:
`src/vergil_tooling/lib/security_records.py`)

Records a security finding as a **pair**: a private draft repository
security advisory in `<org>/.github` that holds the details, and a
neutral `security`-labelled intake issue that carries only a pointer to
it. For the policy, neutral wording and the severity mapping, see
[Security Records](../../guides/security-records.md).

## Usage

```text
vrg-security-record create --repo OWNER/NAME --title TEXT --body-file FILE
                           --details-file FILE --severity {critical,high,medium,low}
                           [--summary TEXT] [--issue OWNER/REPO#N] [--resume GHSA-ID]
vrg-security-record list [--repo OWNER/NAME] [--state {draft,closed,all}] [--details]
vrg-security-record show (GHSA-ID | OWNER/REPO#N)
vrg-security-record retarget GHSA-ID --to OWNER/REPO#N [--to OWNER/REPO#N ...]
```

## Output rule

An advisory's summary and description reach stdout only through `show`
and `list --details`, both meant for an interactive terminal. Every other
line, and every error message, names only GHSA IDs, issue refs, labels
and states. A failed GitHub call is reported with its exit status, command
and stderr; its response body is dropped, because for an advisory call it
may hold the advisory text.

`list --details` is refused when `VRG_EPIC_SWEEP` is set, so advisory
text can never reach the scheduled sweep's public Actions logs.

## Where things are created

- `--repo` always names the **affected** repo, never where anything is
  created.
- The advisory is always created in `<org>/.github`, where `<org>` is the
  owner of `--repo` (or of the `--to` refs for `retarget`). The command
  checks that `<org>/.github` is public and otherwise refuses with:

  ```text
  vrg-security-record: <org>/.github is not public; security records need a public org .github (private orgs are unsupported)
  ```

- The issue is created in the affected repo's queue home (`<org>/.github`
  for a public repo, the repo itself for a private one) and linked under
  that home's intake epic, which is created if absent.

Every run scopes its GitHub App token to `<org>`. In human mode the
human's own `gh` login is used.

## create

Creates the pair, or finishes one.

| Flag | Meaning |
|---|---|
| `--repo OWNER/NAME` | Required. The affected repo. Its bare name becomes the advisory's package name (ecosystem `other`). |
| `--title TEXT` | Required. The neutral issue title. |
| `--body-file FILE` | Required. The neutral issue body. The `Security advisory:` line is appended to it. |
| `--details-file FILE` | Required. The private details; sent only to the advisory, as its description. |
| `--severity LEVEL` | Required. `critical`, `high`, `medium` or `low`. |
| `--summary TEXT` | The advisory summary, at most 1024 characters. Default: the first line of the details file. An empty summary is refused. |
| `--issue OWNER/REPO#N` | Attach mode: use this existing issue instead of creating one. |
| `--resume GHSA-ID` | Finish the pair of this existing advisory instead of creating a new one. |

A fresh `create` runs these steps in order:

1. **Duplicate guard.** Look for a draft in `<org>/.github` with the same
   summary and the same affected repo. If one exists, refuse, naming its
   GHSA ID and the command that finishes its pair.
2. **Advisory.** Create the draft. The request is sent **exactly once**,
   with no retry: drafts can't be deleted, so a retried create could
   leave a second record. It never publishes, never requests a CVE and
   never creates a private fork.
3. **Issue.** Ensure the `security` label in the queue home, then create
   the issue there with the label and the body plus
   `Security advisory: <org>/.github <GHSA-id>`.
4. **Link.** Add the issue as a sub-issue of the queue home's intake epic.
5. **Tracking line.** Write `Tracking issue: <owner>/<repo>#<N>` into the
   advisory's description.

On success it prints `Recorded <GHSA-id> in <org>/.github, tracked by
<owner>/<repo>#<N>.`

**Failure.** Every failure exits 1 and says what exists:

- If creating the advisory fails, nothing else was created. If the draft
  was in fact created (a lost response), a re-run is refused by the
  duplicate guard, which names its GHSA ID.
- If any later step fails, the message names the advisory, the step that
  failed, and the exact command that finishes the pair: the same
  arguments plus `--resume <GHSA-id>`, and `--issue <ref>` once the issue
  exists. `--summary` is left out of that command, since no advisory is
  created on resume. Run the printed command; never re-run the original.

**Attach mode** (`--issue` without `--resume`) attaches a new advisory to
an existing issue instead of creating one. It adds the `security` label
and the `Security advisory:` line to that issue, then writes the tracking
line. The issue keeps its current parent. The past-issues audit uses this
so that one problem never gets two issues.

**Resume** (`--resume`) skips the duplicate guard and the advisory, and
completes whatever steps are missing:

- with `--issue`, it marks that issue (idempotent), links it under the
  intake epic **only if it has no parent yet**, and writes the tracking
  line;
- without `--issue`, it looks among the intake epic's sub-issues for one
  whose body has the advisory's `Security advisory:` line (it never uses
  search, whose index can lag). If none is found it creates the issue and
  links it. Then it writes the tracking line.

Every step is idempotent, so running a resume command twice is safe.

## list

Lists the records in the org's `.github`, one line per advisory, sorted
by GHSA ID:

```text
GHSA-xxxx-xxxx-xxxx  draft  vergil-tooling  tracks: vergil-project/.github#123
GHSA-yyyy-yyyy-yyyy  triage  (no affected repo)  tracks: (no tracking issue)  [external report]
```

Each line has the GHSA ID, the advisory state, the affected repo (its
package name) and the tracked issues. `[external report]` marks an
advisory that came from private vulnerability reporting (the `triage`
state or a submission).

| Flag | Meaning |
|---|---|
| `--repo OWNER/NAME` | Only advisories about this affected repo. Its owner selects the org. Without it, the org is taken from the current repo's `origin` remote. |
| `--state STATE` | `draft`, `closed` or `all`. Default: `all`, which includes every state (also `triage`, `published` and `withdrawn`). |
| `--details` | Also print each advisory's summary on an indented `summary:` line. Interactive use only; refused when `VRG_EPIC_SWEEP` is set. |

With no match it prints `No security records in <org>/.github match.`

## show

Prints one pair with its details, to the terminal: the `list` line, then
`summary:` and `description:` with the advisory's text.

- `show GHSA-ID` takes the org from the current repo's `origin` remote.
- `show OWNER/REPO#N` reads that issue's `Security advisory:` line and
  uses the issue's owner as the org. An issue with no such line is
  refused.

An advisory that does not exist in `<org>/.github` is refused.

## retarget

Moves an advisory's tracking to new issues. It is the one way a link
moves; `vrg-intake-route` and the `epic-create` planning handoff both use
it.

```bash
vrg-security-record retarget GHSA-xxxx-xxxx-xxxx \
  --to vergil-project/vergil-tooling#101 --to vergil-project/vergil-tooling#102
```

Every `--to` must be a full `owner/repo#N` ref, and all of them must be
in one org. The steps run in order, and each starts only once the one
before it succeeded:

1. **Mark** each target: ensure the `security` label in its repo, add it
   to the issue, and append the `Security advisory:` line (skipped when
   already present).
2. **Rewrite** the advisory's description: every `Tracking issue:` line is
   replaced by one line per target. The rest of the description is kept.
3. **Unmark** every issue the advisory tracked before and no longer does:
   remove its `Security advisory:` line and `security` label, and comment
   `Security record moved to <refs>` (posted once, even on a re-run).

A failure at any step exits 1 with a message naming the advisory and the
step. Because a later step never runs before an earlier one succeeds, an
advisory never ends up tracking an issue that lacks the label and line.
A failure while marking leaves the tracking lines unchanged, so re-running
the same command is safe. A failure while unmarking comes after the
rewrite: the advisory already tracks the targets, and the issue the error
names still carries the label and line. A re-run does not revisit it (the
advisory no longer tracks it), so remove them by hand; until then the
sweep reports that issue as a broken pair. On success it prints
`Retargeted <GHSA-id> to <refs>.`

The library's `retarget` also takes a `renamed=` list, which the CLI does
not expose. `vrg-intake-route` uses it for a transfer: the old number of a
transferred issue is replaced in the tracking lines but never unmarked,
because the old number now leads to the moved issue itself.

## Related commands

These commands read or move records as part of their own work:

- **`vrg-epic-audit --close`** (the scheduled sweep) syncs each paired
  advisory's state to its issues and reports broken pairs. It only moves
  an advisory between `draft` and `closed`: it never changes a
  `published`, `withdrawn` or `triage` advisory, or an unpaired external
  report, which it lists as `unpaired external report (left untouched)`.
  Its summary line is `Security records: <n> advisory state(s) synced, <m>
  broken pair(s).` It exits 1 when any pair is broken (after syncing the
  rest), or when the pass fails, including when the org's `.github` is
  private. An org with no `security` issues and no `.github` repo prints
  `Security records: none.` The read-only `vrg-epic-audit` prints the same
  pass as a `(preview)` with `would sync`, makes no change, and does not
  fail on a broken pair.
- **`vrg-intake-route`** retargets a security item's advisory on a
  transfer, a recreate or a split. A split or recreate retargets before
  the original closes and stops with the original open on failure; a
  transfer retargets after the move and, on failure, prints the
  `retarget` command to run. An item with the `security` label but no
  advisory line, or the reverse, gets a warning and no retarget.
- **`vrg-queue planning`** keeps a planning issue at `in-progress`, with a
  `held: advisory <GHSA-id> …` note, while an advisory still tracks it.
  It reads the advisories only when some planning item names one.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Refused (a private `.github`, a duplicate, bad refs, an unreadable file, `--details` under the sweep), a partial pair, a failed retarget, a GitHub API failure, or an authentication failure |
| 2 | Invalid arguments |
