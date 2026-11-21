# vrg-release

**Installed as:** `vrg-release` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_release.py` (stages in
`src/vergil_tooling/lib/release/orchestrator.py`)

Runs the whole release workflow from `develop` to `main`: preflight,
tracking issue, changelog, release PR, merge, CD confirmation,
back-merge and version bump, rolling-tag promotion, cleanup, the
post-release dependency update, and last the consumer refresh. The stages and what
they leave behind are described in the
[Releasing guide](../../guides/releasing.md#the-vrg-release-pipeline).

The `release: <version>` tracking issue's body carries a Contents
section (the pending-release table and the issues the release
contains), the stage checklist, and the release's state block, which
only `vrg-release` writes; see
[Release tracking artifacts](../../guides/releasing.md#release-tracking-artifacts).
`prepare` locks the issue, and `vrg-release` trusts the state block only
while every identity that authored, edited, closed or reopened the issue
is a `human` login of `vrg-trusted-authors`: any other identity stops
the run before it writes anything, as a suspected security breach; see
[The lock and the identity rule](../../guides/releasing.md#the-lock-and-the-identity-rule).

Releasing is a human maintainer action. `vrg-release` refuses to run
under an agent identity.

`vrg-release` only releases a repo that declares itself released with
`[publish] release = true` in `vergil.toml`. When the key is `false` or
absent it refuses up front, before the resume lookup or any stage, and
changes nothing. `vrg-finalize-pr` and `vrg-submit-pr` refuse
`--release` on such a repo the same way, before any push, PR or watch.

## Usage

```bash
vrg-release [minor|major] [options]
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `minor` / `major` | No | Bump to the next minor or major version before releasing. Default: release the current `develop` version |
| `--no-promote` | No | Skip moving the rolling `vX.Y` tag |
| `--without-deps` | No | Skip the `update-deps` stage |
| `--allow-pending-deps` | No | Release even though an open `dependency-update` PR blocks it. For urgent fixes only; the bypassed PRs are listed on the tracking issue |
| `--no-install` | No | Skip the install step. By default the `consumer-refresh` stage runs the `[publish].consumer-refresh` commands as the release's last stage, unless a deferred publish holds it; with this flag it prints them below the summary instead. With none configured the stage says there is nothing to run, which is not an error. `--install` was removed: passing it exits 1 and names this default |
| `--resume [X.Y.Z]` | No | Resume an interrupted release from its open tracking issue. Name the version when more than one release is in flight. Cannot be combined with `minor`/`major` |
| `--skip-audit` | No | Skip the `audit` stage |
| `--output-window N` | No | Rolling output window for the terminal renderer (`0` streams everything) |
| `--output-format` | No | Force the renderer: `rich`, `gha` or `plain` |

## Prerequisites

- Run from the root checkout, on `develop`, with a clean working tree
  that matches `origin/develop`.
- A reachable container runtime (docker or nerdctl) and an authenticated
  `gh`. `git-cliff` runs in the dev container through `vrg-container-run`,
  so it is not needed on the host.

## One release at a time

On a fresh start (not on `--resume`), preflight refuses while any
`release: X.Y.Z` tracking issue is open in the repo, whatever its
version. The refusal names each open issue and the
`vrg-release --resume X.Y.Z` that finishes it, or, for an issue with no
state block (left by older tooling), says to finish that release by
hand and then close the issue. Lookup failures refuse the release. See
[One release at a time](../../guides/releasing.md#one-release-at-a-time).

## The dependency-update gate

On a fresh start (not on `--resume`), preflight refuses while an open
PR carries the `dependency-update` label. First it closes, with a
comment, any such PR whose linked fix task was closed as completed (the
update landed through the fix PR), and deletes its head branch. A failed
branch delete is reported, not fatal. `--allow-pending-deps` lets the
release past whatever still blocks. Lookup failures refuse the release.
See [The dependency-update gate](../../guides/releasing.md#the-dependency-update-gate).

## The develop-CD gate

On a fresh start (not on `--resume`), preflight last requires the
develop CD run for the local `develop` head to be green. It waits on a
run still queued or in progress, up to `$VRG_CD_TIMEOUT` seconds, and
refuses on a failed, timed-out or cancelled run, with the run's link. A
failure only in a docs job is a warning. A superseded run is replaced by
the next develop CD run. With no run for the head, the latest completed
run on an ancestor must be green. Lookup failures refuse the release.
See [The develop-CD gate](../../guides/releasing.md#the-develop-cd-gate).

## Waiting for green

The `merge-release` and `back-merge-bump` stages, and `vrg-update-deps`'s
`merge` stage, wait for their PR's checks and merge it once everything is
green and current. The wait is **bounded** by a deadline: it never blocks
forever, and it never merges past a non-terminal check. Doomed outcomes
abort immediately rather than after the wait: a draft PR, merge conflicts,
a failed check (named in the error), or a branch still behind after five
update attempts. A branch that is merely behind its base is updated
automatically and the wait restarts.

**Orphaned check-runs.** GitHub occasionally leaves a check-run
non-terminal (`queued`/`in_progress`) even though the workflow run that
owns it has already completed — an *orphan* that would never go green on
its own. The waiter detects this **while waiting**, not only at the
deadline (issue #3358). About every 30 seconds it checks the backing run
(`gh run view`) of each pending Actions check. Once a run has completed,
its check gets a one-minute grace period to turn terminal. If the check is
still pending after that, the backing job's own conclusion decides
(`GET /repos/{owner}/{repo}/actions/jobs/{job_id}`):

- The job **succeeded** and the check is **not required**: the check is
  treated as passed and a warning names it (`'test / matrix' check-run
  stale; backing job succeeded`), so the merge goes ahead instead of waiting
  out the deadline. Checks that really failed are still caught by the
  failed-checks gate.
- The job **succeeded** but the check is one of the target branch's
  **required** status checks: the check is a **suspected orphan** (issue
  #3379). GitHub keeps blocking the merge on a required check it reports as
  pending, so passing it would only move the wait to the merge step. If it
  is still pending when the grace period below runs out, the error names the
  check, says GitHub will block the merge until it settles, and gives the
  same close-and-reopen guidance.
- The job **succeeded** but the required checks **could not be read** (see
  *Required checks* below): a **suspected orphan** for the same reason,
  because the wait cannot tell whether GitHub will block on the stale check.
  The error, if the grace runs out, says the required-check lookup failed.
  Without a stale check, a failed lookup still only warns and waits on
  registered checks.
- The job **did not succeed**, has no conclusion, or its conclusion **cannot
  be read**: a **suspected orphan**. If the grace runs out, the wait aborts
  with recovery guidance: close and reopen the PR to re-run the gate, then
  re-run the waiting command. The error names each such check, its job's
  outcome and its job link.

**Grace period for suspected orphans.** During a GitHub Actions incident,
GitHub can write job results back minutes late, and at the moment of
checking that looks exactly like an orphan (issue #3431). Closing and
reopening the PR then would throw away results that are on their way. So a
suspected orphan is not declared at once. The waiter prints a warning naming
the suspects (`… have a completed run but no result yet … GitHub may be
delayed; re-checking for up to 5m00s …`) and keeps polling, re-reading each
suspect's job about every 30 seconds. A result that arrives in time lets the
wait finish normally. Only a check still pending when the grace runs out is
declared orphaned, and the error says how long each check waited. The grace
defaults to 5 minutes from when the backing run was first seen completed.
Set `VRG_ORPHAN_GRACE` (seconds, a positive whole number) to change it; a
malformed value is an error. `vrg-pr-await` applies the same grace before it
reports a PR as unable to merge with reason `orphaned_check`.

The deadline is the backstop. When it elapses with checks still pending,
each one is checked against its backing run again, and a non-terminal check
over a completed run aborts with the same guidance. Genuinely
still-running checks are waited on as normal; if the deadline elapses
with nothing orphaned (for example an app-posted status that is legitimately
slow), the wait fails with a timeout error that names every still-pending
check and its run/job link, with the recovery path: re-run the stuck job,
then re-run the waiting command (`vrg-release --resume`, or `vrg-update-deps`).

**Deadline.** The deadline defaults to 75 minutes (4500 seconds, about
twice the longest known CI run, 36.5 minutes) and is shared by every merge wait:
`vrg-release` (merge-release, back-merge-bump) and
`vrg-update-deps`. Set `VRG_CHECKS_TIMEOUT`
(seconds, a positive whole number) to change it; a malformed value is an
error, and the deadline cannot be disabled. While waiting, the waiter prints
each pending check with how long it has been pending, re-reporting whenever
the pending set changes and at least once a minute.

**Stale check-runs.** GitHub occasionally sets a check-run's `conclusion`
and `completedAt` but leaves its `status` at `IN_PROGRESS`. `gh pr checks`
derives its state from `status`, so it reports such a check as pending
indefinitely. The waiter checks each pending check's `conclusion` in the
PR's `statusCheckRollup`. A check that has a conclusion is terminal and is
classified by that conclusion, so a stale success passes and a stale failure
fails. A warning names the inconsistent check (issue #3170).

**Required checks.** "Green" includes the target branch's *required*
status checks, read from both classic branch protection
(`GET /repos/{owner}/{repo}/branches/{branch}`) and repository rulesets
(`GET /repos/{owner}/{repo}/rules/branches/{branch}`). CI aggregator jobs
such as `test / evidence` only register as checks once their dependencies
finish, so every *registered* check can be done while a required one does
not exist yet. The wait goes on until each required check is
registered and terminal, printing what it awaits (for example
`Awaiting required checks: test / evidence (not registered)`). If the
merge is still refused with "the base branch policy prohibits the merge"
and no check has failed, the wait treats it as *not ready*: it polls and
re-attempts within the same deadline, then fails naming the outstanding
required checks. A policy block that waiting cannot clear, such as a
required review (`reviewDecision=REVIEW_REQUIRED`), aborts at once. If the
required checks cannot be read (permissions or an API error), the wait
prints a warning and falls back to waiting on registered checks only
(issue #3061).

A required check can also be one a job's upload creates: `Semgrep OSS`,
`Trivy` and `CodeQL` are registered by GitHub Advanced Security only once
the `security / semgrep`, `security / trivy` or `security / codeql` job
uploads its SARIF results. When that job fails before its upload, the check
never registers. So when a required check is unregistered and its producing
job has already failed (or been cancelled), the wait stops at once,
even with other checks still running. It fails naming the unregistered
check and the producing job with its result (for example `Required check
Semgrep OSS was never registered and will not arrive: its producing job
security / semgrep (failed) ended before uploading it.`), rather than
waiting out the deadline. A required check with no known producing job
still gets a fail-fast, only later: once every registered check has
finished and one has failed, the wait stops waiting for it and names the
failed checks as its likely cause (issue #4294).

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Every stage succeeded |
| 1 | A stage failed, including a deferred failure such as a missed publish job, a failed consumer refresh or a failed dependency update; or the command was refused |
| 130 | Interrupted (Ctrl-C) |
