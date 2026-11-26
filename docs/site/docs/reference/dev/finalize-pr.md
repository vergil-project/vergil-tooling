# vrg-finalize-pr

**Installed as:** `vrg-finalize-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_finalize_pr.py`

Watches a batch of pull requests until each one has merged, then settles
`develop` once. [`vrg-submit-pr`](submit-pr.md) opens every PR with GitHub
auto-merge on, and GitHub merges each one once its required checks pass.
`vrg-finalize-pr` **never merges a PR** and never turns auto-merge on
(epic vergil-project/.github#1218). A run has five phases:

1. **Watch.** It follows every listed PR, holding no lock, until each one
   has merged, has failed, or the batch deadline has passed.
2. **Settle.** If at least one PR merged, it closes any issue GitHub left
   open, then, under the checkout lock, syncs the main worktree to
   `origin/develop`, cleans up merged branches and worktrees, provisions the
   dev image and runs one `vrg-container-run -- vrg-validate` on the synced
   head.
3. **CD report.** It reports the synced head's CD run without waiting on it.
   With `--wait-cd`, it waits for that run instead.
4. **Report.** It lists every PR as merged or not merged, with the reason
   and what to do next, and gives the validate's result.
5. **Release.** With `--release`, and only on full success, it chains into
   `vrg-release`. Releasing is a human action: an agent's `--release` is
   refused before anything runs.

`vrg-submit-pr` always runs finalize itself, over every PR it opened, so
finalize is run by hand only to watch PRs again after a fix to a PR that
did not merge, or to settle `develop` after something merged on GitHub
(`--cleanup-only`).

Must be run from the **main worktree**: the cleanup removes worktrees,
which is unsafe when the calling shell's CWD is inside one.

## Choosing the PRs

- `vrg-finalize-pr <pr>[,<pr>…]`: a comma list of PR numbers or URLs; one
  PR is a list of one. No prompts: the explicit argument is the
  confirmation. This is the scriptable path, and the one `vrg-submit-pr`
  runs.
- `vrg-finalize-pr --all`: every open PR whose branch has a worktree in
  `.worktrees/`, in branch-name order. A worktree with no open PR is
  listed with the reason it was skipped.
- `vrg-finalize-pr` (no arguments): interactive. It maps each `.worktrees/`
  worktree's branch to its open PR and **always confirms before acting**:
  one candidate asks `Finalize PR #N?`; several present a menu; none asks
  before running the settle phase alone. Worktrees without an open PR are
  listed with the reason they were skipped. It needs an interactive
  terminal and fails fast unless both stdin and stdout are TTYs, since a
  captured stdout would make the prompts invisible (issue #1448). `--yes`
  pre-answers the yes/no prompts, but not the menu.
- `vrg-finalize-pr --cleanup-only`: no PRs and no watch. It runs the
  settle phase and the CD report only, never prompts and never reads
  stdin. `vrg-release` runs it in its `finalize` stage, and a human
  runs it after an automated job merges a PR on GitHub. Its exit codes are
  a watched run's.

A PR list, `--all` and `--cleanup-only` are mutually exclusive.

`--all` and the interactive form match a worktree's branch only to a
same-repo PR whose author is trusted (`vrg-trusted-authors`). A fork PR on
the same branch name, or a PR by an untrusted author, is never a candidate:
it is reported on stderr with its metadata only (repo, number, login,
time), never its title. Several open PRs on one branch are an error naming
them (exit 1), never the first. The branch sweep's merge evidence is
judged the same way.

## Usage

```bash
vrg-finalize-pr [PR[,PR…] | --all | --cleanup-only]
                [--target-branch BRANCH] [--wait-cd]
                [--release]
                [--clean-dirty] [--dry-run] [--yes]
```

## Arguments

| Argument | Description |
| -------- | ----------- |
| `PR` | A PR number or URL, or a comma list of them, to watch. Omit to pick interactively. |
| `--all` | Watch every open PR whose branch has a worktree in `.worktrees/`. |
| `--cleanup-only` | Settle `develop` with no PRs: the sync, cleanup, provision and validate, then the CD report. Never prompts or reads stdin. |
| `--target-branch` | The branch to sync and validate (default: `develop`). |
| `--wait-cd` | Block on the CD run for the synced head and fail if it does not succeed. By default finalize reports that run's link and state without waiting. |
| `--release` | On full success, chain into `vrg-release` (install is part of every release). Refused on a repo that is not released (`[publish] release = true`), before any watch or cleanup. Refused under any agent identity, the same way. |
| `--clean-dirty` | Opt-in: clear a merged/closed worktree whose only dirt is untracked build/validation output, after showing what will be deleted and confirming. Never touches modified tracked files or a reused-branch straggler with unmerged commits (issue #2348). |
| `--dry-run` | Say what would be watched and done without changing anything. The settle phase's steps print what they would do. |
| `--yes` | Pre-answer the interactive yes/no prompts. |

### Removed flags

Each removed flag is refused by name before any side effect, exits 1 and
names what replaced it:

| Flag | Why it is gone |
| ---- | -------------- |
| `--strategy` | Finalize no longer merges. `vrg-submit-pr` chooses the merge method when it turns auto-merge on: squash for a single-issue PR, rebase for a batch PR. |
| `--no-wait-cd` | Not waiting on CD is the default; `--wait-cd` opts in to waiting. |
| `--skip-post-checks` | One run watches every listed PR, then settles `develop` once. Pass the PRs as a comma list. |
| `--install`, `--no-install` | Install is part of every release. |
| `--allow-provenance-violation` | A provenance violation always fails: fix its cause, or fix the classifier if the violation is a false one. See [Provenance](#provenance). |

## Behavior

### 1. Watch

The watch reads each open PR once per poll and judges it in this order:

1. **Develop PRs only.** A PR whose base is not `develop`, or whose head is
   a `release/*` branch, ends as *failed: not a develop PR* and is left
   untouched. A release PR goes through `vrg-release`. The head branch is
   read through the trust layer once, at the start: a PR that is not a
   same-repo PR with a trusted author on its branch (a fork PR, for one)
   ends the same way, and its branch is not named.
2. **Merged.** The PR ends as *merged*. A PR that merged before the run
   started counts as merged at once, and the watch goes on for the rest.
3. **Closed** without merging: *failed: closed*.
4. **Provenance.** See [Provenance](#provenance) below.
5. **The gates.** See [The gates' re-check](#the-gates-re-check) below.
6. **Auto-merge off**, at the start or mid-watch: *failed: auto-merge off*.
   Finalize never turns it back on: someone turned it off on purpose, or
   GitHub did, and a human looks first.
7. **A required check failed:** *failed: CI*, naming the check. A failed
   or cancelled check-run of a superseded workflow run does not count (see
   below). When the base branch's required checks cannot be read, every
   check counts as required.
8. **A required check was cancelled** and no newer run of its workflow
   replaced the cancelled run: *failed: cancelled*, naming the check. The
   PR stays pending for two minutes first, in case a re-run starts.
9. **A conflict** with its base, reported at the same head for two
   minutes: *failed: conflict*. After a push, GitHub takes a while to
   recompute whether the PR can merge, and meanwhile it may still report
   the conflict the push resolved. So the PR stays pending first, shown as
   `conflict reported, waiting for GitHub to settle it`. A new head starts
   the two minutes again, and any other merge state clears the conflict.

**A superseded run is not a failure.** A push to an open PR starts a new
CI run, and the workflow's `concurrency: cancel-in-progress` cancels the
run in flight on the same head. GitHub keeps the cancelled run's
check-runs on the PR, and until the new run's jobs start they are the only
ones there. Finalize judges a failed or cancelled check by its workflow
run: while a newer run of the same workflow, for the same event, is on the
PR's head, the check is not a failure and the PR stays pending.
`vrg-pr-await` and `vrg-repo-status` judge checks the same way.

A PR that ended *failed: CI* or *failed: cancelled* is read again on every
later poll while the watch goes on. If GitHub has merged it, it is reported
as *merged*. Its auto-merge is still on, so a new head pushed to it
meanwhile gets the provenance check and the gates' re-check like any open
PR's: a failure turns its auto-merge off and reports it *failed: provenance*
or *failed: gates* instead.

A PR still pending when the batch deadline passes ends as *timed out*,
and its auto-merge stays on, so it may still merge later. The deadline
covers the whole watch: `$VRG_FINALIZE_DEADLINE` seconds, 7200 by default.
A value that is not a positive whole number is an error before any
waiting. Any failure to read a PR, to run the provenance check or to turn
auto-merge off stops the run with exit 1; none is read as a pending or a
clean PR.

#### Provenance

The watch runs the provenance check on every PR when it starts, and
again each time a PR's head changes. `vrg-submit-pr` ran the same check
before it turned auto-merge on. A violation always fails, and there is
no override (issue #4695): it is either a real problem to investigate or
a bug in the classifier to fix. Each identity's role comes from the
trusted-authors configuration, never from its name alone: the human may
do anything, an agent may not author, edit, close, reopen or merge the
PR or turn auto-merge on (the user identity may not approve it either),
and an identity with no role is **untrusted**, with the same limits as
the audit identity. The one exception is the user identity's **own** PR,
one whose author is that same identity: it may author it, turn
auto-merge on and merge it, but never edit, close, reopen or approve it.
GitHub records a `closed` event, by the merging actor, with every merge;
that one closing is part of the merge and is allowed with it. It is the
first `closed` event after the PR's one `merged` event, by the same
actor, with no other timeline event between them, timed at most 5 seconds
after the merge (GitHub times both to the second and sometimes gives the
close the next one). Any other close, a close and reopen before the merge
included, stays a violation (issues #5140 and #5178). On a PR the one
PR path accepts, a same-repo PR whose branch has a trusted author, any
trusted identity with write access may arm it, whoever authored it
(epic vergil-project/.github#1664, spec §5.1): the user identity may turn
auto-merge on and merge it at clean status, with the closing GitHub
records with that merge, on anyone's such PR. On a fork PR, or one an
untrusted identity opened, those stay violations, and so does every other
forbidden action. An untrusted
approval counts for nothing: it is
reported on stderr, metadata only, and is no violation. What it does
depends on whether the PR has merged:

- **Open PR.** Finalize turns the PR's auto-merge off and reports it
  *failed: provenance*.
- **Merged PR**, one that merged before finalize started included.
  Nothing can be disabled. The PR is reported as merged with a loud
  provenance-violation flag, and the run cannot reach full success: it
  exits 3 (or 4, if develop did not validate) and no chained release
  runs. A human must look at it before any release (issue #4662).

#### The gates' re-check

GitHub keeps a PR's auto-merge on when a new head is pushed to it, so a
fix or a rebase pushed after submit merges by itself once its checks are
green, and nothing turns auto-merge on again for it. The watch is where
that head is judged (epic vergil-project/.github#1384). Right after the
provenance check, at the start and on each new head of an open PR, it
re-runs the gates `vrg-submit-pr` ran before it opened the PR:

- **It refuses another repo's clone.** The gates are judged on local git
  objects, so a run from a clone whose `origin` is not the PR's repository
  stops with exit 1 before anything is read.
- **It asks whether the PR is agent-involved.** It is when an agent login
  opened it, reviewed it, or is the actor of any event on it, or when its
  ready state, on the relay ref `refs/vergil/pr-workflow/<branch>`, says
  so. Turning auto-merge on or off is not involvement, and nor, on a PR
  the one PR path accepts, is its merge and the closing GitHub records
  with it: the actors of those events are not counted. GitHub does not record who pushed a commit, so a ready state counts
  as agent-involved unless its commit's author is trusted and its
  `recorded_by` names an identity that is not an agent; a ready state with
  no `recorded_by`, or a value finalize does not recognise, counts. The
  value is written by whoever ran `report-ready`, so it only ever adds
  involvement: `human` never makes a PR an agent acted on count as
  human-only. A PR with no ready state is judged from its GitHub actors
  alone, and a PR that is not agent-involved skips the gates.
- **It fetches, then evaluates.** It fetches `develop` and the PR's head
  commit (which may have been pushed from elsewhere), moving no feature
  branch's remote-tracking ref, and checks the change from the fetched
  `origin/develop` to the head: the change needs a merge-review record or
  an approved merge-review exemption with its fingerprint. The task issues are the linkage lines of
  the PR's trusted body, or, when it has none, its trusted ready state's.

A head that does not pass, and any read or fetch the re-check cannot
complete, turns the PR's auto-merge off and ends it *failed: gates*, with
the reasons; it is never left armed. A clean rebase keeps the change's
fingerprint, so it passes and its auto-merge stays on. A merged PR is not
re-checked: nothing can be disabled, and the daily agent audit judges it.

#### Status table

The watch builds a status table once at start-up and again after every
poll, about every 15 seconds (issue #4677). It has one row for each PR in
the batch:

```text
Watch status after 0m30s:
  PR     TITLE                                     STATE               WAITING
  #4661  feat(finalize): watch a batch of PRs      merged              0m15s
  #4662  fix(provenance): flag a merged violation  checks pending (2)  0m30s
  #4663  docs: describe the watch                  failed: CI          0m15s
  1 merged, 1 pending, 1 failed; 1h59m30s left before the batch deadline
```

- **PR** and **TITLE**: the PR number and its title, cut to 50
  characters with `...`.
- **STATE**: while the PR is pending, `conflict reported, waiting for
  GitHub to settle it` (a conflict not yet reported for two minutes),
  `checks pending (<n>)` (checks not
  finished yet, counting a required check that has not registered),
  `checks green, merging` (no check left, and GitHub can merge it),
  `behind develop`, or `waiting (<merge state>)` for any other merge
  state, such as `waiting (blocked)`. Once the PR has ended, the outcome
  it ended with: `merged` or a `failed: …` outcome.
- **WAITING**: how long the PR has waited since the watch started. A PR
  that has ended keeps its row, its time frozen when it ended, so the PRs
  can be watched merging one by one.

The last line counts the merged, pending and failed PRs and gives the
time left before the batch deadline (`$VRG_FINALIZE_DEADLINE`).

#### The watch stage

The watch is a stage of finalize's progress display, `watch`, shown
before the settle phase's stages (issue #4694). How the table appears
depends on where the output goes:

- **On a terminal**, the table sits under the stage's spinner and is
  redrawn in place after every poll, never appended. When the watch
  ends, the stage collapses to one line, `✓` when every PR merged with
  no provenance violation and `⚠` otherwise:

  ```text
  ⚠ watch  3m12s — 4 merged, 1 failed
  ✓ close-refs  1.2s
  ✓ checkout-lock  0.0s
  ```

- **Off a terminal** (a pipe, a log, CI), output stays plain and
  append-only. The table is printed at start-up and then only when some
  PR's state changes. Between changes, a heartbeat line is printed every
  five minutes:

  ```text
  Watch status after 5m15s: no change; 1 merged, 2 pending, 0 failed; 1h54m45s left before the batch deadline
  ```

The run log, `.vergil/vrg-finalize-pr-<stamp>.log`, is shared by the
watch and the settle phase. Its first line names the installed
vergil-tooling version (`vrg-finalize-pr: vergil-tooling <version>`),
as does the run's first line of output, so a run can be tied to the
release that made it. It keeps every table in which some PR's
state changed, each under a timestamped `status changed:` line, so the
full history survives the terminal's redraw. When no PR merged, the run
ends by naming the log. The per-PR outcome lines (`<url>: merged`) go to
the stage's output and the log, and the final report is unchanged.

### 2. Settle

The settle phase runs once, after the watch, and only when at least one
PR merged. With no PR merged it is skipped, and the report says so.
`--cleanup-only` runs it with no PRs.

#### Close issues GitHub left open

GitHub sometimes skips the closing-keyword auto-close, which leaves a
merged task open and blocks epic rollup (issue #4002). For each merged
PR, finalize reads its `closingIssuesReferences`: the issues GitHub
itself lists as closed by the PR, through a closing keyword or a
Development link. It never parses the PR text, so an issue the PR only
mentions (`Ref` linkage) is never touched. The PR is read through the
trust layer, and finalize acts only on the references it counts: an issue
in the PR's own repo that a closing line names in a trusted body (the
author and every editor hold the human or agents role), or that a
trusted account linked by hand. Any other listed reference (from an
untrusted body or hand link, or in another repo) is left open and
reported as `<owner>/<repo>#N: left open`, and each untrusted body or
link is reported on stderr by repo, number, login and cause only. If any
counted issue is still open, finalize waits 10 seconds for an auto-close
still in flight, reads again, then closes each one still open with reason
`completed` and the comment "Completed by PR `<owner>/<repo>#N`
(merged `<time>`); GitHub did not auto-close it." Each closed issue is
reported in the output.

This step touches only GitHub, so it runs before the checkout lock is
taken. A failure to read the references (one the trust layer cannot
read in full included, or no trusted authors configured), or to close
any issue, makes finalize exit 1. Every merged PR is handled first, and
the error names each issue still open. The rest of the settle phase
still runs, because the PRs have merged.

#### Sync the advisories of the closed issues

Once the issues are closed, finalize syncs the security advisories
they name (vergil-tooling#4583), so a record's state follows its issue
at merge without waiting for someone to run `vrg-security-record sync`.
It takes every counted closing reference that is now closed (closed by
GitHub, or by the step above) and syncs only the advisories that the
`security`-labelled ones name, reading only the issues each of those
advisories tracks. Each advisory is decided as `sync` decides it (see
[Security Records](../../guides/security-records.md#managing-a-record-through-its-issue)):
a draft whose tracked issues are now all closed as completed, none of
them a queue item, is closed, and each state set is reported as
`GHSA-…: set to closed`. An issue without the `security` label is read
for its labels only, so a merge that closes no security issue makes no
advisories call.

- **Held.** An advisory the close guard holds stays `draft` and is
  reported on a `- held:` line, as `sync` reports it. The stage ends as
  a warning (⚠), naming the next step: retarget it to a fix task, or
  close it with `vrg-security-record close`.
- **Fails.** A broken pair, an untrusted body on an issue it reads, a
  guard read that failed, or any failed read or write (an identity
  without advisories access included) is reported on stderr and makes
  finalize exit 1, naming `vrg-security-record sync` to finish the
  rest. The advisories it could sync are still synced, and the rest of
  the settle phase still runs, because the PRs have merged.

Whenever a merged PR closes a security issue, this step needs
**Repository security advisories: Read and write** on the org's
`.github`: the user App holds it, as does a human's own `gh` login with
access. A reopened issue, an issue closed any other way, and a closing
reference the trust layer does not count are not synced here; run
`vrg-security-record sync` for those.

#### The checkout lock

The sync, cleanup, provision and validate run under the checkout lock,
an exclusive lock on `<main clone>/build/.vergil-state/checkout.lock`
that `vrg-release`'s preflight sync and `vrg-update-deps` also take. Two
finalize runs, or a finalize and a release, never sync or clean up the
same checkout at once. A run that finds the lock held waits, and says
so. The lock is let go before the CD report, so it never covers a wait
on GitHub, and before any chained release, so the release's preflight
can take it.

#### Sync and cleanup

1. **Switch** to the target branch (default: `develop`).
2. **Sync.** Fetch `origin/<target>` with `--tags --force` and
   fast-forward to it. Finalize records the head before and after, for
   the CD report and for the report of a develop that did not validate.
3. **Delete the merged PRs' branches.** Each merged PR's head branch and
   its `.worktrees/` worktree are deleted by name, since a squash merge
   hides the branch from `git branch --merged`. A branch is named only
   once the trust layer finds the PR itself merged, as a same-repo PR with
   a trusted author, on that branch; otherwise its local branch and
   worktree are kept, and the PR is named on the output. The branch's PR-workflow
   relay ref (`refs/vergil/pr-workflow/<branch>`, pushed by
   `report-ready`) is deleted with it, so a cloud-handoff ref never
   outlives its branch (issue #2369). A branch with no local copy is
   skipped, saying so.
4. **Sweep merged branches and worktrees.** Every canonical `.worktrees/`
   worktree whose PR is merged or closed and whose tree is clean is
   removed, with its branch. Local branches merged into the target are
   deleted too, with two guards, so the sweep never races a parallel
   agent session in its creation-to-first-commit window (issue #1445):
    - **Zero-commit branches are skipped.** A branch whose tip equals the
      target's carries no merged work, and it is exactly what an in-flight
      branch looks like before its first commit.
    - **Merge evidence is required.** A branch is swept only when a closed
      or merged PR exists for its head; ancestry alone cannot tell a merged
      branch from one created off an older target tip. A branch skipped for
      lack of evidence is listed with the reason.

    Eternal branches are never deleted. Which they are depends on the
    `branching_model`:

    | Branching Model | Protected Branches |
    | --------------- | ------------------ |
    | `docs-single-branch` | `develop`, `gh-pages` |
    | `library-release` | `develop`, `main`, `gh-pages` |
    | `application-promotion` | `develop`, `release`, `main`, `gh-pages` |

    With no `branching_model`, finalize warns and protects `develop`,
    `main` and `gh-pages`.
5. **Prune.** Any relay ref whose branch no longer exists on origin is
   pruned, as a safety net for a branch deleted outside finalize. Cached
   container images whose branch is live in no repo of the platform repo
   set are removed (skipped on the physical host, which has no default
   repo set); a failed image removal is a warning, never a failure
   (issue #3566). Then `git remote prune origin`.
6. **Working-tree cleanliness gate.** Finalize fails if the target
   branch's working tree is not clean, so leftover files are cleaned up
   before the next issue.

##### Stuck merged worktrees are surfaced, not buried

A merged/closed worktree the sweep cannot remove, because its tree is
dirty or its branch name was reused after a same-named PR merged
(issue #1719), is neither live work nor removable cruft. Finalize reports
every such worktree **prominently after the pipeline**, with the reason,
so finishing with stuck worktrees is impossible to miss (issue #2348).

##### `--clean-dirty`: guarded clear of untracked-only dirt

The common Mac case is a merged worktree whose only "dirt" is
un-gitignored build or validation output. `--clean-dirty` offers an
opt-in path to clear exactly that, after the pipeline, on real stdio:

- Only a **merged/closed** worktree whose **every** uncommitted path is
  **untracked** is a candidate. A reused-branch straggler classifies as
  `no-pr`/`draft` (its merged verdict withheld, issue #1719), so it is
  never in a clearable state and its unmerged commits are never at risk.
- A worktree with **modified tracked files** is refused, since those are
  never discarded, and is left surfaced as needing attention.
- Each removal **shows exactly what will be deleted** (the untracked
  paths) and **requires confirmation** before `git worktree remove
  --force` discards the output and deletes the branch.

Without `--clean-dirty`, anything with tracked modifications or unmerged
commits is protected.

> **Tip:** the cleaner fix is to gitignore your validation output so the
> worktree is never dirtied in the first place. Then it sweeps
> automatically with no `--clean-dirty` needed.

#### Provision and validate

Finalize provisions the target branch's dev image first, since the sync
has just moved the branch (issue #2462), then runs one
`vrg-container-run -- vrg-validate` on the synced head. One validate
covers every PR the run saw merge, and anything else that merged
meanwhile.

When the validate fails, or cannot run (a dev image that could not be
provisioned included), finalize says so loudly after the report:
`<branch> DID NOT VALIDATE`, with the head it validated, the commits the
sync brought in, the validate's full log
(`.vergil/vrg-finalize-validate-<stamp>.log`) and the last 40 lines of
each of its output streams. The CD report still runs.

### 3. CD report

Finalize finds the CD workflow run for the synced head by its commit,
never as "the latest run on the branch", which can be a stale or
unrelated run (issue #2753). It polls up to two minutes for the run to
register.

**By default it reports the run and does not wait on it.** It prints one
line, `CD: <state> <url>`, the state being `queued`, `in progress`,
`succeeded`, `failed`, `cancelled` or `superseded`. A `failed`,
`cancelled` or `superseded` state is a warning (⚠), never a failure: the
release's [develop-CD gate](../../guides/releasing.md#the-develop-cd-gate)
is what refuses to cut a release from a `develop` whose CD is not green.
Two other lines are plain:

- `CD: no CD workflow in this repo`, for a repo whose workflows declare
  no CD, with no warning;
- `CD: no CD run for this head`, when no run registered in time (a change
  CD legitimately skips).

Finalize lists and reads the runs with the current identity's token, as
its other GitHub calls do: as an agent, its App's installation token. Any
other failure to list the runs, no credentials to list them with
included, is an error that names its cause, `CD: error (<cause>)`. It
fails the CD report (exit 1, with or without `--wait-cd`), since the CD
state is not known (issue #5140).

**With `--wait-cd`, finalize blocks on the run** and fails if it does not
succeed:

- **Watch deadline.** The wait defaults to 90 minutes (5400 seconds),
  about twice the longest known CD: vergil-containers' CD routinely takes
  around 43 minutes. Set `VRG_CD_TIMEOUT` (seconds, a positive whole
  number) to change it; a malformed value is an error, and the deadline
  cannot be disabled.
- **Failed jobs are judged as the release gate judges them.** A run that
  failed only in a docs job is a loud warning, not a failure. Any other
  failure names the failed jobs and fails the run (exit 4).
- **Timeout is not failure.** A run still going at the deadline gets the
  headline "CD run still in progress after Ns; result unknown", with the
  run URL, a `gh run view <id>` command, and a pointer to
  `vrg-finalize-pr --cleanup-only --wait-cd` to re-confirm once the run
  has finished. It still fails the run, because an unknown result is not
  a pass, but it never claims the run failed (issue #3316).
- **Superseded is not failure.** A run that concluded `cancelled` is
  checked for a concurrency supersede: every job ended passing or
  cancelled, and every cancelled job carries GitHub's annotation
  "Canceling since a higher priority waiting request for `<group>`
  exists". Such a run ends with a loud warning that this head's CD result
  is not confirmed, naming the group, since GitHub does not record which
  run took its place. Any other cancellation, or one whose jobs or
  annotations cannot be read, is a failure that says why (issue #3834).

### 4. Report

The report is one table with a row per watched PR, laid out like the PR
tables of `vrg-repo-status`: **PR**, **BRANCH**, **TYPE** (the
conventional-commit type, or `deps`, `automation`, `release`,
`lifecycle`), **OUTCOME** and **TITLE**. OUTCOME is `merged` or the
outcome the watch ended with (`failed: CI`, `timed out`, …); a merged PR
with a provenance violation shows `merged, PROVENANCE VIOLATION`:

```text
vrg-finalize-pr report
  PR     BRANCH                   TYPE  OUTCOME     TITLE
  #1173  feature/1170-cache-key   fix   merged      fix(cache): key the image cache by lockfile
  #1174  feature/1171-docs-links  docs  failed: CI  docs: fix the broken guide links

  #1174 https://github.com/o/r/pull/1174 (failed: CI: required check failed: docs)
    next: hand it to an agent, then rerun `vrg-finalize-pr https://github.com/o/r/pull/1174`
  Validate: passed on develop at 51f024e…
  CD: in progress https://github.com/o/r/actions/runs/44377329305
```

Beneath the table, each unmerged PR is named with its reason and the next
step, and a provenance violation is named loudly:

| Outcome | Next step |
| ------- | --------- |
| *failed: CI*, *failed: conflict*, *failed: closed*, *timed out* | Hand it to an agent, then re-run `vrg-finalize-pr <pr>` |
| *failed: cancelled* | Re-run its cancelled checks, then re-run `vrg-finalize-pr <pr>` |
| *failed: auto-merge off*, *failed: provenance* | Investigate before turning auto-merge back on |
| *failed: gates* | Get the missing merge review for its head, then turn auto-merge back on |
| *failed: not a develop PR* | Finalize watches develop PRs only; a release PR goes through `vrg-release` |

It then states the validate's result (`Validate: passed on <branch> at
<sha>`, or why it did not run) and repeats the `CD:` line.

### 5. Release (`--release`)

Releasing is a human action: under any agent identity, `--release` is
refused at once, before any watch or cleanup, `--cleanup-only` included.

On **full success** (every PR merged with no provenance violation, the
validate passed and, with `--wait-cd`, CD did not fail), finalize runs
`vrg-release`, which installs as part of every release. It starts after the lock is let go, so the release's preflight takes the
lock, fast-forwards `develop` itself and cuts from the new head, even if
another PR merged in between (see
[The develop sync](../../guides/releasing.md#the-develop-sync)). Short of
full success, the release is skipped and the run says why. A failed
release never un-merges a PR: finish it with `vrg-release --resume`
once it has opened its tracking issue, or re-run `vrg-release` alone if
it stopped before that.

## Batch PRs (one PR closing several issues)

A PR whose body has more than one `Closes` line is a **batch PR**: 2 to
8 issues of one epic on one branch, exactly one commit per issue, each
subject ending in its issue's `(#N)` (epic vergil-project/.github#1019).
`vrg-submit-pr` re-checks the batch's rules before it opens the PR, and
turns its auto-merge on with the **rebase** method, pinned to the head it
pushed, so `develop` receives one commit per issue and the changelog one
entry per issue. Finalize watches a batch PR like any other.

A batch PR's checks after submit are `vrg-pr-workflow batch-check --pr`'s
(see the [CLI Tools Overview](../cli-tools-overview.md#vrg-pr-workflow)),
which `pr-watch` runs before every push to an open batch PR. Never update
a batch PR by merging `develop` into it (GitHub's update-branch button):
the merge commit breaks the one-commit-per-issue invariant. Since
`develop` does not require a branch to be up to date, a batch PR never
needs that update; a conflict is resolved by rebasing the branch.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Full success: every PR merged with no provenance violation, the validate passed and, with `--wait-cd`, CD did not fail. Also a prompt declined, or `--all` with no open PR. |
| 1 | A tool or usage error: a removed or contradictory flag, not run from the main worktree, non-TTY stdin or stdout in interactive mode, a watch that could not read a PR, or a settle step other than the validate that failed (closing an issue, the sync and cleanup, a dirty working tree) |
| 3 | Develop is healthy, but not every PR merged, or one merged with a provenance violation |
| 4 | Develop did not validate: the validate failed or could not run, or, with `--wait-cd`, CD did not succeed. 4 takes precedence over 1 and 3 |
| 5 | Full success, but the chained release failed |
| 130 | Interrupted (Ctrl-C) |

## Environment

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `VRG_FINALIZE_DEADLINE` | `7200` | The batch deadline for the watch, in seconds |
| `VRG_CD_TIMEOUT` | `5400` | With `--wait-cd`, the CD watch deadline, in seconds |
