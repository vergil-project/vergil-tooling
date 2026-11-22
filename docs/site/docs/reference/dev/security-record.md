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
vrg-security-record retarget GHSA-ID --from 'OWNER/REPO#N' --to 'OWNER/REPO#N' [--to 'OWNER/REPO#N' ...]
                             [--drop 'OWNER/REPO#N' ...]
vrg-security-record sync [--dry-run]
vrg-security-record close GHSA-ID --reason TEXT
vrg-security-record note GHSA-ID --file FILE
```

## Output rule

An advisory's summary and description reach stdout only through `show`
and `list --details`, both meant for an interactive terminal. Every other
line, and every error message, names only GHSA IDs, issue refs, labels
and states. A failed GitHub call is reported with its exit status, command
and stderr; its response body is dropped, because for an advisory call it
may hold the advisory text.

`list --details` is refused when `VRG_EPIC_SWEEP` is set, so advisory
text can never reach the scheduled sweep's public Actions logs. `sync`,
`close` and `note` print no advisory text at all. `close` does not print its
`--reason` either: the reason goes only into the public comments it
posts. `note` never prints its note, which goes only into the private
advisory.

## Where things are created

- `--repo` always names the **affected** repo, never where anything is
  created.
- The advisory is always created in `<org>/.github`, where `<org>` is the
  owner of `--repo` (or of the `--from` and `--drop` refs for `retarget`, whose
  `--to` refs in another org move it there: see
  [The cross-org move](#the-cross-org-move)).

- The issue is created in the affected repo's queue home, `<org>/.github`,
  and linked under
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

**Move lines.** Before any GitHub call, `create` refuses (exit 1, with
nothing created) a `--body-file`, `--details-file` or `--summary` that
has an exact `Moved from: <GHSA-id>` or
`Moved to: <org>/.github <GHSA-id>` line, with `--resume` too. Only a
cross-org `retarget` writes those lines, as `note` also enforces. The
message names the file or flag and never quotes its text.

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

`create` makes the advisory's **first** pairing, on its own path, not
through `retarget`. Before it creates, marks or links any issue, it
checks the advisory's tracking lines: an advisory that already tracks
another issue is refused (exit 1, with no resume command, since resuming
cannot fix it), naming the issues it tracks. One that already tracks the
resumed issue alone is a resume of its own pair, and only the missing
steps run. Move an advisory that is already paired with `retarget`.

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

Prints pairs with their details, to the terminal: for each, the `list`
line, then `summary:` and `description:` with the advisory's text.

- `show GHSA-ID` prints that one pair. It takes the org from the current
  repo's `origin` remote.
- `show OWNER/REPO#N` reads **every** `Security advisory:` line in that
  issue's body and prints one pair per advisory, in body order, separated
  by a blank line. It uses the issue's owner as the org. An issue with no
  such line is refused with `<owner>/<repo>#<N> has no Security advisory:
  line`.

If any advisory to print does not exist in `<org>/.github`, `show`
refuses and prints no pair at all, not even the ones that exist. The
refusal names every missing advisory:

```text
vrg-security-record: advisory GHSA-xxxx-xxxx-xxxx does not exist in vergil-project/.github
vrg-security-record: advisories GHSA-xxxx-xxxx-xxxx, GHSA-yyyy-yyyy-yyyy do not exist in vergil-project/.github
```

## retarget

Moves an advisory's tracking from an issue it tracks to new issues. It is
the one way a link moves; `vrg-intake-route` and the `epic-create`
planning handoff both use it.

```bash
vrg-security-record retarget GHSA-xxxx-xxxx-xxxx --from 'vergil-project/.github#500' \
  --to 'vergil-project/vergil-tooling#101' --to 'vergil-project/vergil-tooling#102'
```

| Flag | Meaning |
|---|---|
| `--from REF` | Required. The issue the advisory moves from, which it must track now. |
| `--to REF` | Required, repeatable. The complete set of issues the advisory tracks afterwards. |
| `--drop REF` | Repeatable. An open tracked issue deliberately left untracked: the human's decision. |

Every ref must be a full `owner/repo#N` ref. `--from` and every
`--drop` must be in one org, the advisory's, and every `--to` in one org;
when that is another org, the retarget is a
[cross-org move](#the-cross-org-move). The refs are single-quoted so the command pastes into any
shell: unquoted, the `#` is a glob operator in zsh with `EXTENDED_GLOB`
set. The `retarget` commands that `vrg-intake-route` prints for an
advisory it could not retarget are quoted the same way.

Two checks run first, after the tracking lines and the org's
`security`-labelled issues are read and before anything is written.
Each refusal exits 1, names the issues the advisory tracks, and prints
the command that would succeed:

- **Source check.** `--from` must be an issue a `Tracking issue:` line
  names:

  ```text
  vrg-security-record: advisory <GHSA-id>: --from <ref> is not an issue it tracks (it tracks <refs>); run: vrg-security-record retarget <GHSA-id> --from '<tracked ref>' --to '<ref>' …
  ```

  A re-run of an interrupted or finished retarget is accepted too: when
  the tracking lines are already exactly the `--to` set and `--from`
  still names the advisory (an unmark not yet done) or carries the
  `Security record moved to <refs>` comment for exactly that set (a
  finished run). Such a re-run moves nothing; it only finishes the
  unmarks.
- **Open-issue check.** The **dropped** issues are those that are not a
  `--to`: the issues in the tracking lines plus every `security`-labelled
  issue whose line names this advisory. Each one still open, other than
  `--from` (which is being handed on), is refused unless a `--drop`
  names it. An issue the listing does not return (one that lost its
  label) has its state read directly; a failed read fails the run. The
  refusal's command keeps every open dropped issue as a `--to`, and says
  that leaving one untracked is the human's decision; it never prints a
  `--drop`:

  ```text
  vrg-security-record: advisory <GHSA-id>: this would stop tracking the open issue <ref>; to keep tracking every open issue, run: vrg-security-record retarget <GHSA-id> --from '<ref>' --to '<ref>' --to '<ref>'. Leaving an open issue untracked is the human's decision
  ```

The steps then run in order, and each starts only once the one before it
succeeded:

1. **Mark** each target: ensure the `security` label in its repo, add it
   to the issue, and append the `Security advisory:` line (skipped when
   already present).
2. **Rewrite** the advisory's description: every `Tracking issue:` line is
   replaced by one line per target. The rest of the description is kept.
   An unchanged description is not written.
3. **Unmark** every issue that names this advisory but is no longer a
   target. These are the issues in the old tracking lines, plus every
   `security`-labelled issue in the org whose `Security advisory:` line
   names this GHSA ID (found by REST listing, never by search). Each one
   gets the comment `Security record moved to <refs>` (posted once, even on
   a re-run). Then this advisory's line is removed, together with the
   `security` label when no other advisory line remains, in one edit. An
   issue that also names other advisories keeps their lines and the
   label. An issue that names only other advisories is never touched.
   An issue that was **transferred** since it was tracked is never
   written at its old number, which GitHub answers with a 404. If it now
   lives at one of the targets, nothing is left to unmark: it keeps its
   line and label. Otherwise the unmark follows the transfer and acts on
   the issue at its current ref. Either way a line
   `<GHSA-id>: <old-ref> was transferred to <ref>: …` says which it did.
   `--from` is unmarked **last**, after every other dropped issue, so a
   run interrupted before then leaves it naming the advisory, and the
   re-run's source check accepts it.

An issue naming several advisories needs one `retarget` per advisory.
Run them one after another, never in parallel: each rewrites the issue's
body, so two at once can write back a line the other removed. They can
run in either order; the label goes only with the last line.

A failure at any step exits 1 with a message naming the advisory and the
step. Because a later step never runs before an earlier one succeeds, an
advisory never ends up tracking an issue that lacks the label and line.
After a failure at any step, re-run the same command: it finishes the
job, and once the job is done a re-run writes nothing. An issue that still carries this advisory's line and label is found
again by the listing, so an interrupted unmark is completed. The one case
a re-run cannot reach is an issue whose `security` label was removed by
hand while its advisory line stayed; `sync` reports it as a broken pair.
On success it prints `Retargeted <GHSA-id> to <refs>.`

The library's `retarget` takes the source as `source=` and also takes a
`renamed=` list, which the CLI does not expose. `vrg-intake-route` passes
the routed item as the source, and as targets the advisory's other
tracked issues plus the new issue or issues, so a route never meets the
open-issue check. It uses `renamed=` for a transfer: the old number of a
transferred issue is replaced in the tracking lines but never unmarked,
because the old number now leads to the moved issue itself. Its recovery
commands name that old number as `--from`; without `renamed=` the old
number is the source, and its unmark finds the issue already at a target.

### The cross-org move

When every `--to` is in an org other than the advisory's, `retarget`
moves the advisory to that org's `.github` (epic
vergil-project/.github#1417). GitHub cannot move an advisory, so the move
copies it. A `--to` set spanning orgs is refused before any read:

```text
vrg-security-record: advisory <GHSA-id>: the --to issues span orgs (<org>, <org>), and every fix of one advisory must be in one org. Either duplicate the advisory into each org, each copy tracking that org's own fix tasks, or break the finding up into separate advisories
```

Every check runs before the first write, and each refusal exits 1:

- The original must exist, be a `draft` and not an external report, with
  at most one `Moved to:` line. A `closed` original carrying the line is
  a finished move, re-run.
- The original must not itself be a copy a move made (an advisory with a
  `Moved from:` line). A copy never moves again: its original's
  `Moved to:` line would name an advisory that tracks nothing, and `sync`
  would report that original broken for good. The refusal names the way
  out, one copy of the finding per org: keep the copy tracking its own
  org's fix tasks and create a separate advisory in the new org.
- The source check and the open-issue check, as above. Once a re-run
  finds the original's tracking lines already removed, `--from` must
  carry the `Security record moved to <refs>` comment for exactly the
  `--to` set. The open-issue refusal for a move prints the same command,
  since a move closes the original and cannot keep tracking an open
  issue in its own org:

  ```text
  vrg-security-record: advisory <GHSA-id>: this would stop tracking the open issue <ref>; a move to <org>/.github closes this advisory, so it cannot keep tracking an issue in <org>. Close or retarget that work first, then run: vrg-security-record retarget <GHSA-id> --from '<ref>' --to '<ref>' …. Leaving an open issue untracked is the human's decision
  ```

- The fix org's `.github` advisories are listed, and each one with a
  `Moved from: <GHSA-id>` line naming this advisory alone on its line is a
  candidate. One candidate is reused only when it is a
  `draft` and not an external report; more than one candidate, or any
  that is an external report or in another state, refuses the move,
  naming each, and nothing is created. A reused copy must also be the one
  the original's `Moved to:` line names, when it has one, and track no
  issue or exactly the `--to` set.
- With no copy to reuse, the original's severity must be `critical`,
  `high`, `medium` or `low`.

Then, each step only once the one before succeeded, every fix-org call
under that org's token and every other under the advisory's:

1. **Create** the copy, unless one is reused: a draft in the fix org's
   `.github` with the original's summary, its details less the
   `Tracking issue:` lines plus `Moved from: <GHSA-id>`, its severity, and
   the first `--to`'s repo as the affected repo. The create is sent
   exactly once.
2. **Hand it on**: mark every `--to` with the copy's
   `Security advisory: <fix-org>/.github <new GHSA-id>` line and give the
   copy one `Tracking issue:` line per `--to`.
3. **Close the original**: write `Moved to: <fix-org>/.github <new
   GHSA-id>` on it (its tracking lines kept last), unmark every dropped
   issue, `--from` last, remove its tracking lines, and set it `closed`.

A failure at any step exits 1 naming the advisory and the step; the
identical command finishes the move, reusing the copy by its
`Moved from:` line, and writes nothing once the move is done. On success
it prints any transfer notes, then
`Moved <GHSA-id> to <fix-org>/.github as <new GHSA-id>, tracked by <refs>; <GHSA-id> is closed as moved.`

The library's `retarget` returns the transfer notes followed by the
original's `Moved to:` line; `fix_org` is the one-org check, and
`moved_from_line`, `moved_to_line`, `parse_moved_from` and
`parse_moved_to` build and read the two lines, and `refuse_move_lines`
refuses caller text holding either one. Only the move writes them:
`note` refuses a note holding either line, and `create` refuses a body
file, details file or `--summary` holding one (see [create](#create)).

## sync

```bash
vrg-security-record sync [--dry-run]
```

Runs the security-records pass over the current repo's org (from its
`origin` remote; there is no `--org` flag). It lists the advisories in
`<org>/.github` and every `security`-labelled issue in the org's managed
repos, then sets each paired advisory to `closed` when every issue it
tracks is closed, and back to `draft` while any is open. It only moves an
advisory between `draft` and `closed`: it never changes a `published`,
`withdrawn` or `triage` advisory, or an unpaired external report, which
it lists as `unpaired external report (left untouched)`. Broken pairs are
listed as `- broken: <GHSA-id or issue ref>: <problem>`, never repaired.

**Moved advisories.** An advisory that is not an external report and
carries one `Moved to: <org>/.github <GHSA-id>` line is moved, a terminal
state: it is never synced and never broken for tracking nothing. The pass
lists each org's `.github` it names once, under that org's token, and
reports `- broken: <GHSA-id>: moved to <org>/.github <GHSA-id>, which
does not exist` (or `which has no Moved from: <GHSA-id> line`, or `which
tracks no issue`) for a copy that does not hold it. An advisory with more
than one `Moved to:` line is broken. A failed read of the other org fails
the pass.

**The close guard.** A draft advisory whose tracked issues are all closed
stays `draft` when any of them is a queue item (its native parent is a
live intake, planning or deferred epic, or a per-quarter archive of one,
recognized by its `archive` label or its stamped title) or was closed
with any reason other than completed. The parent is read only for an
issue closed as completed. Each advisory the guard holds is listed by
GHSA ID and the issue refs that fired it, with the next step:

```text
- held: GHSA-xxxx-xxxx-xxxx (org/repo#12): retarget it to a fix task, or close it with vrg-security-record close
```

A hold does not fail the pass. The guard only stops a move to `closed`:
an advisory already `closed` (a human closed it with
[`close`](#close)) is left alone and never reported as held. When an
issue's parent cannot be read, that advisory is left unchanged and
listed as `- error: <GHSA-id> (<issue ref>): reading its parent failed,
left unchanged: <error>`; the rest are still synced.

Its summary line is `Security records: <n> advisory states synced, <m>
broken pairs, <h> held, <e> errors.` Each noun agrees with its count, so a
count of 1 reads `1 advisory state synced`, `1 broken pair` or `1 error`.
It exits 1 when any pair is broken or any parent read failed (after
syncing the rest), or when the pass fails. An org with no `security` issues and no `.github`
repo prints `Security records: none.`

| Flag | Meaning |
|---|---|
| `--dry-run` | Preview: print the pass as `Security records (preview): <n> advisory states would sync, <m> broken pairs, <h> held, <e> errors.`, change nothing, and exit 0 even on a broken pair or a failed parent read. |

**Permission.** `sync` needs **Repository security advisories: Read and
write** on `<org>/.github` (`--dry-run` needs Read), so it runs as the
human's own `gh` login or the user App, never in the scheduled sweep.
`vrg-epic-audit --close` makes no security-advisory call, so its token
needs no advisories access (vergil-project/vergil-tooling#4313). Nothing
runs `sync` on a schedule: the human or the user agent runs it, after
closing or reopening a `security` issue and regularly in any case, and
`vrg-epic-audit` previews it read-only.

## close

```bash
vrg-security-record close GHSA-xxxx-xxxx-xxxx \
  --reason "the work it tracked was dropped with its epic"
```

Closes a draft advisory and leaves an audit trail: a public comment with
the GHSA ID and the reason on every issue the record tracks. It is how an
agent closes a record that `sync` would not close, such as one in a
broken pair or one the close guard holds, or records why a record it closes was dropped. It takes the
org from the current repo's `origin` remote, as `show` and `sync` do.

| Flag | Meaning |
|---|---|
| `--reason TEXT` | Required. Why the record is closed: one line, at most 500 characters. It is **public**, so it must be neutral, like an issue title: never the finding, its severity or any other advisory detail. |

The issues are the ones the advisory's `Tracking issue:` lines name, plus
any `security`-labelled issue in the org whose `Security advisory:` line
names it (found by REST listing, never by search). An issue that was
transferred is followed to where it is now. The steps run in order, and
each starts only once the one before it succeeded:

1. **Check.** Refuse, changing nothing, when:
   - the advisory does not exist in `<org>/.github`;
   - it is an external report, or in any state other than `draft` or
     `closed` (`published`, `withdrawn`, `triage`): those are a human's
     decision, and `sync` leaves them alone too;
   - no issue tracks it and none names it, so no issue could carry the
     comment;
   - any of its issues is still open. `sync` sets an advisory back to
     draft while any issue it tracks is open, so the close would not
     hold: close the issue first (as not planned, for dropped work), or
     retarget the record.
2. **Comment.** Post `Security record <GHSA-id> closed: <reason>` on each
   issue, skipping one that already carries that exact comment.
3. **Close.** Set the advisory's state to `closed`, unless it already is.

The comments go first, so a closed record always has its audit trail.
After a failure at any step, re-run the same command: it posts no
comment twice and finishes the job. A failure exits 1 with a message
naming the advisory and the step.

On success it prints `Closed <GHSA-id> in <org>/.github; the reason is on
<refs>.`, or, for an advisory that was already closed,
`<GHSA-id> was already closed in <org>/.github; the reason is on <refs>.`
Before it, each transferred issue gets a line
`<GHSA-id>: <old ref> was transferred to <new ref>: commented there.`

`close` needs the same **Repository security advisories: Read and write**
as `sync`, and runs as the human's own `gh` login or the user App.

## note

```bash
vrg-security-record note GHSA-xxxx-xxxx-xxxx --file note.md
```

Appends a dated note to an advisory's description, so a record can be
kept accurate as designs change: a resolution later superseded, a
retarget, a fix that landed elsewhere. It is **append-only**: the text
already there is never changed or removed, so the record keeps its
history. There is no command that rewrites an advisory's text. It takes
the org from the current repo's `origin` remote, as `show`, `sync` and
`close` do.

| Flag | Meaning |
|---|---|
| `--file FILE` | Required. The note text, read as UTF-8. It is **private**: it goes only into the advisory, never to stdout, stderr or any issue. |

The note is added at the end of the description under a header line
`Note (<YYYY-MM-DD>):`, the UTC date of the run, followed by the file's
text with its leading and trailing blank lines and each line's trailing
spaces dropped. The advisory's `Tracking issue:` lines are moved after
it, in their order, so they stay the last lines and `retarget`, `list`
and `sync` read them as before.

It refuses, changing nothing, when:

- the file cannot be read, or holds no text;
- any line of the note is a `Tracking issue:` line, which would change
  what the record tracks: use `retarget` for that;
- any line of the note is a `Moved from:` or `Moved to:` line, which
  would change where the record lives: only `retarget` writes those
  lines, when it moves a record to another org;
- the advisory does not exist in `<org>/.github`;
- it is an external report, or in any state other than `draft` or
  `closed` (`published`, `withdrawn`, `triage`), which stay a human's
  decision.

A re-run with the same file on the same day adds nothing when the
advisory's last note is already that exact note, so a run whose response
was lost can be repeated safely; it prints the same line. Any other note,
even one repeating an older note, is added. On
success it prints only `Noted <GHSA-id> in <org>/.github.` A failure
exits 1 with a message naming the advisory and the step, scrubbed of
`gh`'s response body, as the other subcommands do.

Keep the note file out of the repo, in a scratch directory, and delete
it once the note is recorded. `note` needs **Repository security
advisories: Read and write** on `<org>/.github`, as `sync` and `close`
do.

## Related commands

These commands read or move records as part of their own work:

- **`vrg-epic-audit`** (read-only, without `--close`) prints the same pass
  as `sync --dry-run`. `vrg-epic-audit --close`, the scheduled sweep, does
  not run it.
- **`vrg-intake-route`** retargets a security item's advisories on a
  transfer, a recreate or a split. A transfer or recreate retargets
  **every** advisory the item names, in body order. A recreate retargets
  before the original closes and stops with the original open on
  failure; a transfer retargets after the move and, on failure, names
  every advisory still not retargeted with the `retarget` command for
  each. A split retargets the item's one advisory before the original
  closes and stops with the original open on failure; a split of an item
  naming several advisories is refused before any change, naming their
  GHSA IDs, since which advisory goes to which task can't be inferred.
  An item with the `security` label but no advisory line, or the
  reverse, gets a warning and no retarget.
- **`vrg-queue planning`** keeps a planning issue at `in-progress` while
  it is held: while **any** advisory still tracks it, whether or not its
  body names that advisory, or while its body names, with the `security`
  label on, an advisory the listing does not return (one that cannot be
  read). The note counts them and names them by GHSA ID only
  (`advisory still tracks` for one, `advisories still track` for more),
  and ends with the retarget command:

  ```text
  held: <k> advisories still track this planning issue (<GHSA-id>[, …]) — retarget each to the task or tasks that fix it with vrg-security-record retarget <GHSA> --from '<owner>/<repo>#<N>' --to <task>
  held: <k> advisories this planning issue names cannot be read (<GHSA-id>[, …]): re-run with a token that can read the repository's advisories, or have the human check it — retarget each to the task or tasks that fix it with vrg-security-record retarget <GHSA> --from '<owner>/<repo>#<N>' --to <task>
  ```

  An item held both ways gives both parts, joined by a semicolon. A
  `Security advisory:` line on an issue without the `security` label,
  when no advisory tracks the issue, is a broken pair: the note reports it
  (`broken pair: …`) and the item is not held. The docs PR's planning
  close applies the same rule and gives the same reason.

  It lists each org's advisories once per view, and skips an org with no
  `.github` unless one of its items has an advisory line (then the home
  is read anyway, and a failed read fails the view). An advisory line
  naming an org other than the issue's own is refused, not ignored.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success |
| 1 | Refused (a duplicate, bad refs, an unreadable file, `--details` under the sweep, a `retarget`, `close` or `note` the checks refuse, a `create` onto an advisory already paired), a partial pair, a failed retarget, close or note, a broken pair or a failed parent read found by `sync` (not `--dry-run`), a GitHub API failure, or an authentication failure |
| 2 | Invalid arguments |
