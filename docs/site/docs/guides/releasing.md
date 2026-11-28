# Releasing

This guide covers the release workflow for `vergil-tooling`,
including how patch, minor, and major versions affect both the
release author (producer) and downstream consumers.

## Deployment targets

A release reaches three coordinated deployment targets (the original
design is the historical [host-level-tool spec][hlt], written under the
old `standard-tooling`/`st-*` naming):

| Target | Install mechanism | Who uses it |
|---|---|---|
| **Developer host** (macOS) | `vrg-tooling-install vX.Y.Z` (or `vX.Y`, the release its rolling tag names): `uv tool install` from git of the exact release tag's commit, constrained by that commit's `uv.lock`; the release's `install` stage runs it | Host-side commands: `vrg-container-run`, `vrg-commit`, `vrg-submit-pr`, `vrg-release`, `vrg-finalize-pr`, `vrg-host` |
| **Container runtime** (all languages) | `vrg-container-run` cache-first install of the version `vergil.toml` names, in a per-branch cached image | `vrg-*` inside the dev container, locally and in CI, for every consumer |
| **Agent VM** (Lima/cloud) | `vrg-vm` `uv tool install` from git of the identity's release line or version, resolved to one commit and constrained by its `uv.lock` | Agent sessions inside the VM |

The host install is
[`vrg-tooling-install`](../reference/cli-tools-overview.md#vrg-tooling-install).
vergil-tooling's own `vergil.toml` has a `[publish].install` that runs the
installed one on the Mac through the release's line
(`vrg-tooling-install v<MINOR>`, followed by `vrg-vm update --all`), so the
release installs itself on the host once `promote` has moved `vX.Y` to it.
Given a line, `vrg-tooling-install vX.Y` first reads the rolling tag `vX.Y`
with one `git ls-remote`, takes the one `vX.Y.Z` tag on the commit it names,
and then installs and reports that exact release. Given an exact tag
`vX.Y.Z` (which cannot move, under tag protection, so it needs no
`git ls-remote`), it fetches that tag. Either way it installs the commit the
release's tag names with that commit's `uv.lock` as constraints, and runs on
uv's managed 3.15. By hand it also takes any other git ref of vergil-tooling
(a branch such as `develop`, another tag, or a full commit SHA), resolved to
one commit and locked to that commit's `uv.lock`; the next release install
replaces it (see
[`vrg-tooling-install`](../reference/cli-tools-overview.md#vrg-tooling-install)).

A fresh host has no `vrg-tooling-install` yet, and an installed one that
fails cannot fix itself. Both install by hand (see
[Getting Started](../getting-started.md)), with `VERSION` set to the
release, for example `2.1.10`:

```bash
VERSION=X.Y.Z
env -u UV_PYTHON -u UV_PYTHON_PREFERENCE -u UV_NO_MANAGED_PYTHON \
  uv tool install --force --no-config --python 3.15 --managed-python \
  "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v${VERSION}"
vrg-tooling-install "v${VERSION}"
```

The first line installs the tag unlocked; the second reinstalls it locked.

On an agent VM, a release installs with uv too: `vrg-vm` resolves the
version to one commit, installs it with that commit's `uv.lock`, and
removes any vergil package an older VM still carries. A dev ref
(`vrg-vm update --tag develop`, or any non-release ref) is an explicit,
unconstrained uv install of that git ref, and every `vrg-vm` command that
touches the VM prints a `DEV tooling` banner while it is in place. A plain
`vrg-vm update` returns the VM to its release.
See [vergil-tooling inside the VM](../reference/vm-spec.md#vergil-tooling-inside-the-vm).

[hlt]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/host-level-tool.md

## Tag scheme — what's user-facing, what isn't

Releases are tagged on `main`, and consumers pin to those tags.

| Tag pattern | Where | Purpose | User-facing? |
|---|---|---|---|
| `v1.3.0` | `main` | Release tag | Yes — pin in install commands |
| `v1.3` | `main` | Rolling minor tag (force-updated each patch) | Yes — what consumers actually pin to |

Earlier releases also placed a `develop-vX.Y.Z` tag on the `develop`
tip. Those tags are retired: CD no longer creates them, the existing
ones have been deleted, and `vrg-release` neither requires nor reports
one. The changelog and release notes ignore any `develop-v*` tag that
still turns up, and nothing should ever pin to one.

A release's changes are the commits since the merge base of the
release branch and the previous `vX.Y.Z` tag, so a PR merged to
`develop` while a release was in flight appears in the next release.
The changelog heading and the release notes title carry the bare
version (`## [1.3.1] - <date>`, `# Release 1.3.1 (<date>)`). The new
section is added to the top of `CHANGELOG.md`; the entries of earlier
releases are kept as they were published.

## Patch release — the common path

A patch release fixes a bug or refines an existing feature without
adding new public surface. Versions: `v1.3.0` → `v1.3.1`.

### Producer steps

1. Land all the patch's changes via normal feature PRs to `develop`.
   To see what the release will carry, run `vrg-repo-status`: its
   Pending release section lists every change on `develop` that `main`
   does not have yet, with the latest release and the rolling tag
   (see the [`vrg-repo-status` reference](../reference/cli-tools-overview.md#vrg-repo-status)).
2. From the root checkout, on a clean `develop` that matches
   `origin/develop`, run the release:

   ```bash
   vrg-release
   ```

   `vrg-release` is a human-only command: it refuses to run under an
   agent identity. It needs a running container runtime (docker or
   nerdctl) and an authenticated `gh`; it generates the changelog with
   `git-cliff`, and refreshes the lockfile after the version bump, in the
   dev container, through `vrg-container-run`, so the host needs no
   language toolchain. It
   drives the whole release through the stages listed in
   [The `vrg-release` pipeline](#the-vrg-release-pipeline) below, in a
   managed worktree, so the root checkout is never switched off
   `develop`. See the [`vrg-release` reference](../reference/dev/release.md)
   for every flag.
3. While `vrg-release` waits on CD, the post-merge automation runs:
   - CD on `main` creates the release tag (`v1.3.1`).
   - The rolling minor tag (`v1.3`) is **force-updated** to point at
     the new release.
   - The Documentation workflow publishes the new release notes to
     the docs site under `releases/v1.3.1`.
   - **Repos with `[package]`** (vergil-tooling is one) also publish
     signed `.deb`/`.rpm` packages, all before the tag exists:
     `cd-release` builds every package build cell, then a separate
     `package-sign` job signs each `.rpm` and attests every artifact.
     Any package failure stops the release before tagging. The signed
     packages and `packages-manifest.json` are attached to the GitHub
     Release.
   - After tagging, `cd-release` dispatches `package-released` to the
     org's `packages` repository. Its `publish-index` workflow verifies
     every artifact's build attestation (signer workflow `cd-release.yml`,
     source ref `refs/heads/main`) and every `.rpm` signature, then
     rebuilds and signs the apt/dnf index. A failed dispatch does not fail
     the release; the index's weekly reconcile picks the release up.
   - `vrg-release`'s `package-index` stage then polls the published
     index (up to one hour, every 30 seconds) until the new version is
     visible. With several
     `[[package]]` entries it waits for every package, each at its own full
     version, and succeeds only once all of them are listed. While it
     waits, it streams the `Publish package index` run in `<org>/packages`
     that covers the release and prints a heartbeat line on each poll
     otherwise; the index poll alone decides success. The hour bounds
     idle polling only: the stage waits for a followed run however long
     it takes (up to three hours, then it fails naming the run), and the
     hour restarts when that run completes, so the index has a full hour
     to show the release after it. A timeout error says which wait ran
     out. If the run it
     follows fails (`failure`, `timed_out` or `startup_failure`), the
     index still lacks the release and no newer index run is listed, the
     stage fails at once rather than waiting out the hour: its error
     names the failed run, so check that run's failed step before
     resuming. A miss (timeout, a failed publish-index run, or a
     polling error) does not undo the release. It is recorded
     as a deferred publish failure: `promote`, `finalize` and `install`
     still run (see [Promote and the index](#promote-and-the-index)), the
     tracking issue stays open, and `vrg-release` exits 1, naming
     `vrg-release --resume` as the command that finishes the release. On
     `vrg-release --resume`, the stage
     dispatches the `publish-index` workflow in `<org>/packages` itself,
     unless the index already lists the release, then waits as above,
     following only the run that dispatch started or a newer one. Once the
     index lists every package, the state block records it
     (`index_listed`). See the
     [package config reference](../reference/package-config.md#cd).
4. After `finalize` and `update-deps`, the `install` stage runs the
   `[publish].install` commands, with `<VERSION>` expanded to the
   release (`1.3.1`) and `<MINOR>` to its line (`1.3`). It needs
   `promote` done, because an install that follows the rolling `vX.Y`
   tag (vergil-claude-plugin's marketplace, or
   `vrg-tooling-install v<MINOR>`) gets the release only once that tag
   has moved, and before it runs anything it checks with one
   `git ls-remote` that `vX.Y` names this release's tag. It refuses,
   running nothing, when `promote` failed or `vX.Y` names another
   commit (a hand demotion in between, say). A deferred publish (a
   failed CD publish job, or a `package-index` miss) and a failed
   `update-deps` do not hold it back. A repo with no install configured
   is the normal case: the stage line says `none configured`. On
   success the stage is one `✓ install` line; on failure its full
   output is printed below the summary, the stage fails, `vrg-release`
   exits 1, and `vrg-release --resume` runs the install again. No flag
   skips it: the install is part of every release.
   `[publish].consumer-refresh`, the key's old name, is still
   read, with a deprecation warning, for one minor version; setting both
   is an error.
5. Then `release-records` posts a [`Released:` record](#released-records)
   on every issue in the [issue list](#the-issue-list), and last
   `close-tracking` closes the tracking issue with a summary. Both run
   only once no stage before them has failed: any failure, a deferred
   publish or a failed `update-deps` included, leaves the issue open for
   `vrg-release --resume`, and `close-tracking` also waits until every
   record is posted. A clean run names the released tag in its
   completion line (`✓  vrg-release v1.3.1 complete`).
6. Check the end of the run for the `update-deps` result. A failed
   dependency update does not undo the release, but it leaves the
   tracking issue open, and can leave an open PR that blocks the next
   release; see
   [Dependency updates](#dependency-updates).

### The `vrg-release` pipeline

`vrg-release` runs these stages in order. A *fail-fast* stage stops the
run when it fails. A *deferred* stage records its failure and lets the
later stages run, except those [the hold rule](#the-hold-rule) holds
back; the run then exits 1 with a summary. Every tracked
stage posts a completion or failure comment on the release tracking
issue and ticks its box in the issue's stage checklist. The checklist
lists every stage but `publish-status`, which runs after
`close-tracking` has made the last write to the issue and reports only
to the terminal, so a finished release's checklist has every box
ticked. A stage that succeeds also records itself done in the issue's state block, with the
facts later stages need; a [resume](#resuming-a-release) runs only the
stages not recorded done.

| Stage | Mode | What it does |
|---|---|---|
| `audit` | fail-fast | Runs `vrg-github-repo-config audit` on the repo. Releasing vergil-tooling itself adds `--in-tree-templates`, so its managed files are checked against the templates being released, not the installed release's (see the [config audit reference](../reference/config-audit.md#auditing-vergil-tooling-against-its-own-templates)). `--skip-audit` skips it |
| `preflight` | fail-fast | Checks the host prerequisites, `gh` auth and `vergil.toml`. A [resume](#resuming-a-release) runs only these checks. A fresh start then checks that the root checkout is a clean `develop`, [brings it up to `origin/develop`](#the-develop-sync), runs the [main/develop divergence check](#the-maindevelop-divergence-check), checks the version is not tagged, checks that [no other release is open](#one-release-at-a-time), runs the [package re-publish check](#the-package-re-publish-check), runs the [dependency-update gate](#the-dependency-update-gate), and last runs the [develop-CD gate](#the-develop-cd-gate). It then takes the [release lock](#the-release-lock), quiesces `develop` and judges the dependency-update gate again before the cut. Creates the `release/<version>` managed worktree, or adopts one an [interrupted run](#an-interrupted-release) left with nothing on it. On a fresh start it then reads the release's [contents](#release-tracking-artifacts), pinned to the commit the branch is cut from, and prints them: the pending-release table and, under it, the [issue list](#the-issue-list). A resume before `unlock` adopts or re-takes its own lock (see [Resuming and superseding under the lock](#resuming-and-superseding-under-the-lock)) |
| `prepare` | fail-fast | Creates the `release: <version>` tracking issue, with its Contents section, stage checklist and state block, generates the changelog and release notes, and opens the release PR to `main`. It writes the tracking issue into the release lock first, and states the lock in its comment |
| `merge-release` | fail-fast | Waits for the release PR's checks and merges it. If the PR conflicts with `main`, it merges `main` into the release branch and pushes it. A conflict in `CHANGELOG.md` alone is resolved by keeping the release branch's generated changelog; any other conflict aborts the merge and stops with the commands to resolve it by hand. Before the merge it checks for a [stray merge](#stray-merges), which abandons the release |
| `confirm-main` | fail-fast | Watches CD on `main`. The release job must succeed: a failed one is recorded in the state block, and recovered by the resume (see [A failed release job](#a-failed-release-job)). Every other failed publish job is recorded in the state block, by its run and full name, and deferred to `cd-publish` (see [Stale job records](#stale-job-records)) |
| `back-merge-bump` | fail-fast | Creates `release/post-<version>` from `main`, bumps the version, refreshes the lockfile (`uv lock` for Python and `cargo update --workspace` for Rust, in the dev container; for TypeScript, a direct edit of `package-lock.json`'s root version fields that runs no npm), opens a PR to `develop`, arms it through the one PR path (a merge commit) and waits until it merges. Records the back-merge's merge commit |
| `teardown-worktree` | fail-fast | Removes the release worktree and returns to the root checkout. Fail-fast, as every branch-work stage is, so `finalize`, which prunes the release branches, runs only once all of them have succeeded |
| `confirm-develop` | deferred | Watches the CD run on `develop` for the back-merge's merge commit; a failed publish job (the docs publish) is recorded in the state block and deferred to `cd-publish` |
| `promote` | deferred | Moves the rolling `vX.Y` tag to the new release once the tag and the GitHub Release exist, whatever `cd-publish` and `package-index` show (see [Promote and the index](#promote-and-the-index)) |
| `cd-publish` | deferred | Delivers the CD publish jobs the confirm stages recorded as failed (see [Failed CD publish jobs](#failed-cd-publish-jobs)). Nothing to do when none failed. A fresh run fails it, deferred, naming the jobs; `vrg-release --resume` re-runs them |
| `package-index` | deferred | For repos with `[package]`, waits for the new version in the package index. A miss is a deferred publish failure; a resume dispatches the index workflow and waits again |
| `finalize` | deferred | Runs `vrg-finalize-pr --cleanup-only`, which prunes the release branches. The tracking issue stays open until `close-tracking` |
| `update-deps` | deferred | Runs the dependency update on the new `develop` version (skipped with `--without-deps`); see [Dependency updates](#dependency-updates) |
| `install` | deferred | Runs the `[publish].install` commands once `promote` is done and `vX.Y` names the release. `none configured` when the repo has none. Success is one check line; a failure prints the full output below the summary |
| `release-records` | deferred | Posts a [`Released:` record](#released-records) on every issue in the [issue list](#the-issue-list) that has none yet, once no stage before it has failed. It fails naming each issue whose record it could not post; a resume posts only the missing ones |
| `unlock` | deferred | Checks again for a [stray merge](#stray-merges), which it reports and records without failing, then deletes the [release lock](#the-release-lock) by compare-and-swap. A resume at `unlock` deletes what is left of its own release's lock; none held means the stage is done |
| `rearm` | deferred | Offers every open, non-draft PR into `develop` with auto-merge off back to the one PR path, listing on the tracking issue any PR the path refused or held. Idempotent: a re-run offers only the PRs still unarmed |
| `close-tracking` | deferred | Closes the tracking issue with a summary, once no stage before it has failed and the state block records a record posted on every issue |
| `publish-status` | deferred | When a stage failed, names what is left of the release, any deferred publish and any dependency-update failure, and the one command that finishes it: `vrg-release --resume`, or, for a release that cannot finish, `vrg-release --supersede X.Y.Z`. It warns, never failing a second time |

#### The hold rule

A failed stage holds back only the stages that need it; no stage is
optional:

- `promote`, `cd-publish` and `finalize` need only the stages through
  `confirm-develop`. `cd-publish` delivers the jobs the confirm stages
  record, so it waits for `confirm-develop` to succeed and record its own.
- `install` needs those and `promote`.
- `release-records`, `unlock`, `rearm` and `close-tracking` need every stage before them.
- Nothing holds back `package-index` or `update-deps`, and the failure of
  `cd-publish`, `package-index` or `update-deps` holds back none of
  `promote`, `finalize` and `install`.

A held stage does not run (its line shows `held: <stages> failed`) and
is not recorded done. So a failed CD publish job, a `package-index` miss
or a failed dependency update leaves the release tagged, promoted,
finalized and installed, with the tracking issue open, and
`vrg-release --resume`, once the cause is fixed, runs only the stages not
yet done and closes the issue. For a `package-index` miss the resume
dispatches the `publish-index` workflow itself.

#### Failed CD publish jobs

`confirm-main` and `confirm-develop` record each CD publish job that did
not succeed in the tracking issue's state block, as its branch, its CD
run and its full job name. A fresh run's `cd-publish` fails, deferred,
naming them, and the release carries on. Once the cause is fixed,
`vrg-release --resume` delivers them:

- **On `main`**, each recorded run is re-run with
  `gh run rerun <run> --failed` and watched, and its jobs are judged
  again as `confirm-main` judges them: the release job must still have
  succeeded.
- **On `develop`**, a recorded job counts as delivered when a later
  `develop` CD run's same job has succeeded, so an older run never
  overwrites newer docs. Only a job no later run delivered is re-run,
  by itself (`gh run rerun <run> --job <job>`). A listing of `develop`'s
  newest CD runs that does not reach back to the recorded run stops the
  stage rather than re-run it on a guess: so many later runs have
  started that the stage cannot tell whether one delivered the job. Here
  the stage names a step to run by hand: run `develop`'s CD again, then
  resume, and the new run's job, once it succeeds, counts as the
  delivery.

A recorded job whose run has succeeded since, re-run by hand say, is
delivered without a re-run. Each delivered job leaves the state block
as soon as its run is judged, so a second resume re-runs only what is
still recorded. While any job is left, `cd-publish` fails and the issue
stays open; `promote`, `finalize` and `install` still run.

`confirm-develop` checks the CD run for the back-merge's merge commit,
not for the current tip of `develop`. On `vrg-release --resume`, other
PRs (a dependency update or a feature) may have merged to `develop`
since the back-merge. The tip's CD run then says nothing about the
release. A resume reads the back-merge PR from the state block and
confirms its merge commit with GitHub before any stage runs. If the
merge commit can't be found, `confirm-develop` fails. It never falls
back to the tip.

`finalize` does not pass `--wait-cd` to `vrg-finalize-pr`, so
it only reports the CD run of `develop`'s tip and never fails on
it. That tip has the same problem: it may be a later commit's.
`confirm-develop` has already checked the right run, and the hold rule
runs `finalize` only once `confirm-develop` has passed. A second wait
would add nothing. It would also fail the stage on a docs publish
failure that `confirm-develop` has already deferred.

#### Promote and the index

`promote` moves `vX.Y` once the tag and the GitHub Release exist,
whatever the package index or the CD publish jobs show. While the index
is deferred, `vX.Y` may name a version whose packages are not listed
yet. That is accepted: nothing in the fleet installs from the packages.
vergil-tooling's own installs follow the rolling tag: a `vX.Y` pin
reads the tag `vX.Y` with `git ls-remote` and installs the `vX.Y.Z` release
on the commit it names, never simply the newest `vX.Y.Z`, so a line
demoted with `vrg-promote` is followed by the dev-container cache,
`vrg-vm` and `vrg-tooling-install` alike. Once the release's packages
appear in the index, `vrg-release --resume` records them listed and
closes the issue.

### The reload reminder

A running Claude Code session keeps the plugin agent definitions it
loaded until it runs `/reload-plugins` or restarts, while skills are read
fresh when invoked. After a plugin release, a session that is not reloaded
pairs the new skills with the old agents (issue #5434).

A repo opts in with `reload-plugins = true` in its `[publish]` table of
`vergil.toml` (default `false`; a non-boolean value is refused). The key is
keyed on the repo's setting, not its name, so only a plugin repo sets it.
When it is set, a release that finishes ends with a final line telling the
human to run `/reload-plugins`, or restart, in every running Claude Code
session, on the host and in every VM. The tracking issue's summary carries
the same reminder as its last section. A failed release prints no reminder.

### An interrupted release

An interrupt (Ctrl-C) or a crash can stop `vrg-release` at any point.
What a re-run does depends on how far the stopped run got:

- **Stopped before the tracking issue was opened.** `preflight` has
  created the local `release/<version>` branch and its managed worktree
  in `.worktrees/`, but nothing is pushed and no issue records the
  release. Run `vrg-release` again, with the same arguments. A fresh
  start adopts the leftover branch and worktree and fast-forwards them
  to `develop`, as long as nothing on them can be lost: the branch was
  never pushed (`origin` is asked directly), it has no commit `develop`
  lacks, and the worktree is clean and on the branch. Otherwise it
  stops with `Release branch 'release/<version>' already exists` and
  the reason, and leaves everything in place for you to inspect. It
  never deletes a branch or a worktree.
- **Stopped after the tracking issue was opened.** The issue is created
  with its stage checklist and state block in its body, so it is always
  resumable: run `vrg-release --resume`. A fresh `vrg-release` refuses
  while any release is open
  ([one release at a time](#one-release-at-a-time)), whatever version it
  would cut. The resume runs the stage the stopped run was in again,
  from its start; `prepare`, for one, skips what the stopped run already
  did.

### Resuming a release

`vrg-release --resume` reads the tracking issue's state block, under
[the lock and the identity rule](#the-lock-and-the-identity-rule), and
runs only the stages it does not record as done, from the first undone
stage on. Each one it skips prints `already done`. `audit`, `preflight`
and `publish-status` run on every run and are never recorded done.

- **Checks.** `preflight` runs only its host, `gh` auth and config
  checks. The fresh-start checks, the develop sync among them, do not
  run: the release passed them when it started.
- **The lock.** A resume whose first undone stage is before `unlock` holds the [release lock](#the-release-lock) again: it adopts its own broken or stale lock, re-takes an absent one and quiesces again, and refuses a live one or another release's. At `unlock` it deletes its own release's lock. See [Resuming and superseding under the lock](#resuming-and-superseding-under-the-lock).
- **Facts are checked, not trusted.** Before any stage runs, the resume
  confirms what the state block records against GitHub and git: the
  release PR is merged once `merge-release` is done, and the back-merge
  PR once `back-merge-bump` is; the recorded CD run on `main` is for the
  release PR's merge commit, and the one on `develop` for the back-merge
  PR's; and the tag still peels to the recorded commit, which is the
  release PR's merge commit or a single-parent freeze commit on top of it.
  A mismatch stops the run, naming the fact.
- **The worktree before the merge.** When the first undone stage is
  `prepare` or `merge-release`, the resume adopts the release worktree on
  `release/<version>`, creating the local branch from `origin` if needed.
  If the branch is missing both locally and on `origin`, the resume stops,
  naming it, and never cuts a new one from `develop`.
- **The version before the merge.** A fresh start records the release's
  target version in the state block, with the `patch`, `minor` or
  `major` bump that produces it from `develop`'s VERSION, before any
  branch work. On a resume whose first undone stage is `prepare` or
  `merge-release`, the release branch's committed VERSION must be that
  target. If `prepare` stopped before committing the bump, so the branch
  still holds the VERSION the bump starts from, `prepare` completes the
  bump. Any other VERSION, or a VERSION change left uncommitted in the
  worktree, stops the run, naming both versions: a resume never
  continues at a version other than the target.
- **The worktree after the merge.** When the first undone stage is
  `confirm-main`, `back-merge-bump` or `teardown-worktree`, the release PR
  has merged and GitHub has deleted `release/<version>`, so the resume
  never needs the branch. It reuses the release worktree if it is still
  there, whatever branch it is on, and stops if that path is not a git
  worktree. With no worktree there and `back-merge-bump` still to run, it
  creates one detached at `develop`, in which `back-merge-bump` cuts its
  post branch from `main`. Otherwise it runs from the root checkout, and
  `teardown-worktree` finds nothing to remove, does nothing and is
  recorded done.
- **The root checkout.** For any later first undone stage, the resume
  runs from the root checkout, and never needs the release branches,
  which `finalize` may already have pruned.
- **A stage that did not do its work** is not recorded done, so a resume
  runs it again: a stage that failed, and one
  [the hold rule](#the-hold-rule) held back. Any failed stage leaves
  the tracking issue open, so the resume always finds it.
- **`--without-deps`** given on a resume is recorded in the state block,
  as one given on the first run is. It skips `update-deps` on that run
  and on every later resume, and the skip records the stage done.

`--resume` refuses a tracking issue with no state block, or one whose
block is malformed, names another version, or records a stage the
running tooling does not have. A release started by an older
`vrg-release`, whose issue has no state block, cannot be resumed: finish
it by hand, or with the tooling version that started it, then close the
issue.

### A failed release job

When the CD release job on `main` does not succeed, `confirm-main`
records the CD run in the state block as the failed release run, then
fails. The release PR is already merged. Fix the cause, then run
`vrg-release --resume <version>`. Its `confirm-main` recovers the run
before judging it:

1. It checks that the recorded run is for `main`'s tip, the release
   merge, and refuses to re-run it otherwise.
2. It checks that the release PR's head has a CI attempt that passed,
   and names it in the log. It never re-runs that CI run: its gates were
   judged before the merge, and a re-run after the merge fails its
   version check, with the head and `main` at the same version. The
   newest attempt that passed counts, even when a later re-run of the
   same run failed. With no attempt that passed, the resume stops before
   the CD run is touched.
3. It re-runs the failed CD run, and only it, and watches it.
4. It judges the CD run's new attempt through the same release-job gate.
   That job's CI evidence gate takes the same attempt and harvests only
   what that attempt produced (see the
   [CI evidence convention](ci-evidence-convention.md)).
   A release job that passes clears the recorded run; one that fails
   again keeps it recorded, and the next resume recovers it again. Once
   it passes, the stages after `confirm-main` run as in any release.

When the defect is in the release commit itself, or in what its CI
wrote, so no re-run of the CD run can pass, supersede the release instead (see
[Superseding a release](#superseding-a-release)).

### Superseding a release

A release that `--resume` cannot finish, because the defect is in its
tagged commit, is replaced by a new one with
`vrg-release --supersede X.Y.Z`. It is eligible only when `X.Y.Z` is
the open release and either:

- **Tagged:** its tag and GitHub Release exist, and its only stages left
  before `release-records` are among `cd-publish`, `package-index`,
  `update-deps` and `install`; or
- **Untagged:** it halted at `confirm-main` with no tag because its CD
  release job failed, as its state block records (the
  [failed release run](#a-failed-release-job)).

Otherwise the run refuses, naming what is open, and says to finish the
release with `vrg-release --resume X.Y.Z`. A bare `vrg-release` never
supersedes: it refuses while the release is open
([one release at a time](#one-release-at-a-time)).

The superseding release is a fresh release, with every fresh-start
check. It combines with a `minor` or `major` bump, and cannot be
combined with `--resume`: passing both is a usage error (exit 2).
Before anything else, `preflight` reads the superseded
release's state block under
[the lock and the identity rule](#the-lock-and-the-identity-rule),
checks it is eligible, and confirms its facts against GitHub and git as
a resume does. A superseded issue that fails the identity rule is not
read at all: see [A breached tracking issue](#a-breached-tracking-issue). It also confirms the GitHub Release of a tagged one
exists, and that the tag of an untagged one does not exist on `origin`.
Then:

- The [open-release check](#one-release-at-a-time) passes over the
  superseded release's tracking issue, and only it.
- The new release takes over the superseded release's [release lock](#resuming-and-superseding-under-the-lock) without unfreezing `develop`, and records the takeover in the lock history and on both tracking issues. It refuses a superseded release whose lock is running with a fresh heartbeat. With no lock to take over, it takes a fresh lock and quiesces.
- The new release **absorbs** the superseded release's issue list,
  read from its state block, into its own. An issue on both lists
  appears once, with the PRs from both. The tracking issue's Contents
  section names the superseded release under `### Supersedes` and marks
  each absorbed issue, and the state block records the superseded
  version and the absorbed issues.
- Once the new tracking issue and its release PR exist, `prepare`
  closes the superseded one, with a comment naming the release that
  superseded it. Closing it last means a failure there leaves the
  release branch's version bump pushed, for the resume to adopt. The
  superseded release posts no records. A resume of the new release
  closes it if a stopped run did not.
- The new release's [records](#released-records) name the new version.
  On each absorbed issue the record also says
  `- Supersedes: vX.Y.Z`.

An **untagged** superseded release left its release commit on `main`
without a tag, and stopped before its back-merge:

- The [main/develop divergence check](#the-maindevelop-divergence-check)
  accepts that commit and its ancestors on `main`, the superseded
  release PR's merge commit, and refuses any other commit `main` has
  that `develop` lacks. The new release's back-merge reconciles it.
- `develop`'s VERSION was never bumped, so it must still be `X.Y.Z`,
  and the new release takes the next patch, `X.Y.(Z+1)`, bumped on its
  release branch, unless a `minor` or `major` bump is given.
- When its tracking issue is closed, its release worktree and its
  `release/X.Y.Z` branch, local and on `origin`, are removed first.

#### A breached tracking issue

A tracking issue that fails
[the identity rule](#the-lock-and-the-identity-rule) can never pass it
again, so its release can never be resumed and its state block is
never trusted again. `vrg-release --supersede X.Y.Z` still replaces
it. When the superseded issue fails the rule, `preflight` prints a
warning naming the identity and what it did, never reads the state
block, and rebuilds the release from what GitHub and git record about
it instead:

- **Its release PR,** `release/X.Y.Z` into `main`, must be merged. It
  and the back-merge PR are found through the trust layer: only a
  same-repo PR whose author holds a role in `vrg-trusted-authors`
  counts, so a fork PR or an outsider's PR on a branch of the same name
  never stands in for either, and each one left out is reported on
  stderr, metadata only. A read the layer cannot complete refuses. A
  release whose release PR never merged left nothing on `main`, so
  there is nothing to supersede: the run refuses and says to close the
  release PR and the tracking issue by hand, delete `release/X.Y.Z`
  locally and on `origin`, and start a fresh release.
- **Tagged:** when `vX.Y.Z` exists on `origin`, it must be on the
  release PR's merge commit or on a single-parent freeze commit on it,
  its GitHub Release must exist, and its back-merge PR,
  `release/post-X.Y.Z` into `develop`, must be merged.
- **Untagged:** otherwise, every `main` CD run for the release PR's
  merge commit must have completed, and none may have a release job
  that succeeded. That merge commit is the one the main/develop
  divergence check accepts.
- **Its issue list** is rebuilt from the `feature/` PRs between the
  `main` commit its release PR merged onto and the release PR's head,
  as [the issue list](#the-issue-list) reads them. When the breached
  release itself superseded a release, the issues it absorbed are read
  from that release's tracking issue, found by the closing comment the
  human posted on it (a comment by the `human` role of
  `vrg-trusted-authors`, a GitHub user account, that no one else has
  edited, as the trust layer reads it); that issue's
  state is read under the identity rule, or rebuilt the same way when
  it fails the rule too.

The superseding release then runs as any other: its fresh-start checks
judge `develop`'s version, `main` and the open releases, and its
`prepare` closes the breached issue. Stages that leave nothing on
GitHub or git to check (`teardown-worktree`, `confirm-develop`,
`promote` and `finalize`) are not judged: the superseding release
promotes and cleans up for itself.

### A failed back-merge-bump

`back-merge-bump` can fail part-way, after it has created
`release/post-<version>`: in its lockfile refresh, say, or before its PR
merges. Fix the cause and run `vrg-release --resume`. The stage reuses the
post-release branch, local or pushed, when it holds only its own work:

- **At `origin/main` with no commit of its own.** The bump still has to
  run. A bump that fails, in its lockfile refresh or anywhere else, puts
  every file it writes back as it was, so the worktree is clean. A partial
  bump left uncommitted by a run killed mid-bump (changes to the files the
  bump writes, with `VERSION` already at the next version) is discarded
  first, so the bump is not applied twice.
- **One bump commit on `origin/main`**, with the expected subject and
  `VERSION`, and a clean worktree. The bump is done, and the stage goes on
  to its PR.

Any other state stops the stage with an error naming the branch, and
nothing is reset, discarded or pushed over: a branch that does not contain
`origin/main`, other or extra commits, a bump to another version,
uncommitted changes that are not a partial bump, or a local branch whose
tip differs from `origin`'s. Remove or reconcile the branch, then resume.

An open back-merge PR from the earlier run is adopted, armed again
through the one PR path and waited for, and a merged one is skipped. A PR closed without merging is not adopted: the
resume opens a new PR from the reused branch. Here, and when `prepare`
adopts the release PR, only a same-repo PR with a trusted author is
adopted: a fork PR on the branch's name is reported on stderr and never
adopted or merged.

Within a run, `prepare`, `back-merge-bump` and the dependency update never
retry a failed `gh pr create` blindly, since GitHub can open the PR and
still return an error. After a failed attempt the stage looks for an open
PR from its branch into the base, through the trust layer: only a same-repo
PR with a trusted author counts, and a fork PR on the branch's name is
reported on stderr and never adopted. It adopts the PR it finds only when
it has the same title and head commit, and carries on as for a PR it just
opened; an adopted dependency-update PR gets its `dependency-update` label
again. Any other such PR fails the stage, naming the PR. With none open, a
transient error is retried with backoff.

### Waiting for a CD run

`confirm-main`, `confirm-develop` and `cd-publish` wait for a CD run by
reading it with `gh run view` until the attempt they are waiting for is
`completed`, and then judge its jobs. After a re-run, that is the new
attempt the re-run started, never the one that already finished. They
never take `gh run watch`'s exit status as the run's result: it can
fail while the run is still going.

A read that fails is classified first. A transient GitHub error, a 404
on the just-listed run, or a reply that cannot be parsed is retried with
back-off. Any other error stops the stage at once. The stage fails, and
says which, only on one of these:

- **The run's result:** the attempt completed and its jobs did not pass,
  judged as each stage describes.
- **A read failure:** the run could not be read, either for a
  non-transient error, for seven failed reads in a row, or for a read
  still failing when the wait ran out. This is not the run's result:
  check the run, then resume.
- **Result unknown:** the attempt was still not complete after
  `$VRG_CD_TIMEOUT` seconds (default 5400). Once it finishes, resume.

### Stale job records

The confirm stages wait a bounded time for every CD job to reach
`completed` in the GitHub jobs API, because a job's record can lag the
run's. A job that is still not `completed` after that wait has no
conclusion to judge:

- If the run itself concluded `success`, GitHub has confirmed that every
  job succeeded or was skipped, so the record is stale. The stage shows a
  warning, `stale job record: <job>`, and nothing is deferred. No CD
  re-run is needed.
- Under any other run conclusion the job may really have failed, so it is
  deferred: recorded as a failed job and shown as
  `<job> (never settled)`.

A deferred job is recorded and reported by its full name, such as
`release / package-sign` or `docs / docs`, so `cd-publish` can find the
same job again, and a failed job in the release job's own family is
never mistaken for the release itself failing.

### Consumer steps

For most consumers, **no action is required**: the rolling `v1.3`
tag they're already pinned to now points at the patch release. They
get the fix on the next normal action:

| Consumer | When the patch lands |
|---|---|
| Developer host | The release's `install` stage installs the patch on the Mac that ran it; any other host runs [`vrg-tooling-install vX.Y.Z`](#deployment-targets) with the new version (`vrg-host` reports the installed version but never upgrades it) |
| Dev container image | The base images carry no vergil-tooling. The next `vrg-container-run` rebuilds the branch's cached image with the patch once `promote` moves the rolling `vX.Y` tag to it: a `vX.Y` line pin in `vergil.toml` resolves to the release that tag names (see [How the cache tracks vergil-tooling releases](../reference/cli-tools-overview.md#how-the-cache-tracks-vergil-tooling-releases)). An exact `vX.Y.Z` pin stays put |
| Agent VM (Lima/cloud) | `vrg-vm update`, which the release's `install` stage runs on every VM (`vrg-vm update --all`): a `vX.Y` line pin resolves to the patch once `promote` moves the rolling `vX.Y` tag to it; an exact `vX.Y.Z` pin stays put |

A Python repo has nothing of its own to bump: its `pyproject.toml` and
`.venv` carry no vergil-tooling, and it picks up a release through the
dev container image, like a repo in any other language.

## Minor release — opt-in for new features

A minor release adds backwards-compatible new features. Versions:
`v1.3.x` → `v1.4.0`.

### Producer steps

Same steps as patch, with one important addition: the rolling
minor tag changes from `v1.3` to `v1.4`. Any line pin to `v1.3` (a
repo's `vergil.toml`, an agent identity's version) **must be bumped
manually** — the rolling-tag mechanism only handles patches within a
minor.

1. Run `vrg-release minor`. It bumps the version to the next minor
   (`1.4.0`) on the release branch instead of releasing the current
   `develop` version. Everything else matches a patch release.
2. **Coordinate consumer bumps.** Each repo whose `vergil.toml` pins
   `[dependencies].vergil` to `v1.3` needs it updated to `v1.4` (see
   the consumer steps below). This is a per-repo decision — consumers who
   prefer to stay on `v1.3` can keep doing so until they're ready.
3. Update anywhere the documentation pins a minor to the new tag.
   The host install by hand in `getting-started.md` names an exact
   `VERSION` placeholder, not a line, and needs no change.

### Consumer steps

Minor bumps are **deliberate opt-in** at every deployment target:

| Consumer | What changes |
|---|---|
| Developer host | Nothing to change: the host keeps no pin. The release's `install` stage installs `v1.4.0` on the Mac that ran it, through the new line (`vrg-tooling-install v1.4`, `<MINOR>` expanded to the release's line); any other host runs [`vrg-tooling-install v1.4`](#deployment-targets) or `v1.4.0` |
| Dev container image | Set `[dependencies].vergil` in `vergil.toml` to `v1.4`; `vergil.toml` is part of the cache key, so the next `vrg-container-run` rebuilds the branch's cached image with the release the rolling `v1.4` tag names |
| Agent VM (Lima/cloud) | Set the identity's `vergil` version (in `identities.toml`) to `v1.4`, then `vrg-vm rebuild`, which installs and records the new line. A plain `vrg-vm update` keeps upgrading within the line recorded at install time, and `vrg-vm update --tag v1.4` is a one-off override that is not remembered (see [VM spec](../reference/vm-spec.md#vergil-tooling-inside-the-vm)) |

The release author should announce the minor bump in the GitHub
Release notes, calling out any new features and the `v1.4` pin
update.

## Major release — explicit migration

A major release contains breaking changes. Versions: `v1.x.x` →
`v2.0.0`.

### Producer steps

A major release is a minor release plus a deliberate breaking-change
communication and migration window:

1. Document the breaking changes BEFORE cutting the release. A
   `docs/specs/v2-migration.md` (or similar) describing what
   breaks, what to update, and how to recover from common issues.
2. Run `vrg-release major` to release `v2.0.0`. The
   release notes prominently call out the breaking-change list and
   link to the migration guide.
3. Notify consumer maintainers (e.g., update `vergil-tooling`'s
   release tracking issue / project board with a callout). The
   release notes alone are not sufficient for major bumps;
   consumers should be primed.

### Consumer steps

Same as a minor release in mechanics — every deployment target
edits its `v1.x` pin to `v2` deliberately. The difference is
**timing and risk awareness**:

- Read the migration guide before bumping.
- Stage the bump in a feature branch, run validation, fix any
  breakages from removed APIs / changed behaviors.
- Merge the bump PR per the consumer's normal review flow.

## Rolling back a release

A rollback moves a rolling `vX.Y` tag back to an earlier `vX.Y.Z`, so
consumers that follow the tag return to that release. It is a release act
and stays human-gated. The gate is the `Tag protection` ruleset, which lets
only the repository Admin role and the org automation App move a rolling
tag (see the [config audit reference](../reference/config-audit.md#github-checks-rulesets)).
For Claude Code consumers the no-bypass `Rolling tag branch names` ruleset
is part of the gate: Claude Code resolves `v2.1` as a branch before a tag,
so the tag's protection holds only while no `v2.1` branch can exist.
`vrg-promote` also refuses an agent identity, but that stops an agent from
running it by mistake; it is not an access control.
The procedure below is written for the Claude Code plugin,
`vergil-project/vergil-claude-plugin`, whose consumers follow `v2.1`
(epic [vergil-project/.github#760](https://github.com/vergil-project/.github/issues/760),
[spec §5](https://github.com/vergil-project/.github/blob/develop/epics/760-plugin-tag-distribution/spec.md#5-rollback-procedure)).
Moving the tag works the same way in any repo; how other consumers pick up a
backward move depends on how they install and is not covered here.

### Move the tag back

Run these steps exactly, in a human shell on a terminal. `vrg-promote` acts
on the repo of the current directory, and the fleet's repos share tag names
(`v2.1.50` exists in vergil-tooling and vergil-actions too), so running it
from the wrong checkout would move the wrong repo's tag.

1. `cd` into a vergil-claude-plugin checkout.
2. Fetch the tags:

   ```bash
   vrg-git fetch --tags --force origin
   ```

   The rolling tag moves on every release, and without `--force` the fetch
   refuses to update a local tag that has moved.
3. Check the move:

   ```bash
   vrg-promote --dry-run <X.Y.Z>
   ```

   It prints the origin repo and the tag move, and changes nothing:

   ```text
   origin: vergil-project/vergil-claude-plugin
   v2.1 1a2b3c4d5e6f -> v2.1.50 fc17882a9b0c
   ```

   Check that the origin line names vergil-claude-plugin, that the rolling
   tag is `v2.1`, and that the move goes to the release you mean. Always
   pass the version: with none, `vrg-promote` reads the `VERSION` file.
4. Move the tag:

   ```bash
   vrg-promote <X.Y.Z>
   ```

   It checks the move again, prints the two lines from that check and asks
   `Move v2.1 on vergil-project/vergil-claude-plugin? [y/N]`. Read the lines
   again before you answer, in particular the current `v2.1` commit: they
   can differ from the dry run if origin moved in between. Only `y` or
   `yes` moves the tag. It pushes the verified release tag object straight
   to the rolling tag's ref under a lease on origin's value of the rolling
   tag as this run planned it, so it overwrites exactly that tag or is
   refused, and it moves your local rolling tag only after GitHub accepts
   the push.

Both `vrg-promote` runs check the target against origin's current state
(each one fetches `main` and the tags first). The target must be an existing
`vX.Y.Z` tag on origin whose commit is on origin's `main`, or is that
release's freeze commit, and that has a published GitHub Release. A freeze
commit is the commit vergil-actions' CD tags: one commit on top of `main`,
with the subject `chore(release): freeze internal refs to vX.Y.Z`, that
changes nothing but what `vrg-freeze-refs` rewrites for that tag.
`vrg-promote` refuses, with exit 1:

- any other target;
- an agent identity;
- a run with no terminal to confirm on (from a script, CI or a pipe),
  unless it is given `--yes`, which moves the tag with no prompt;
- a declined or unanswered confirmation;
- a move that changed on origin after you confirmed it.

An exit 1 after `Promoted:` (the local tag update failed) or after a
failure to read origin's rolling tag back leaves origin moved or unknown.
After either, re-run `vrg-promote --dry-run <X.Y.Z>` to see where `v2.1` is.

When `v2.1` already points at the target, it prints
`v2.1 already points at vX.Y.Z — already promoted.` and pushes nothing. It
prints no `origin:` line in that case, so check the checkout with
`vrg-git ls-remote --get-url origin` instead. See
the [`vrg-promote` reference](../reference/cli-tools-overview.md#vrg-promote)
for every exit path.

### Pick the move up on each machine

Each machine picks up the move when its plugins are next updated. Run, in a
human shell:

```bash
vrg-host plugins        # the host
vrg-vm update --all     # every running agent VM
```

Then restart Claude Code: a running session keeps the plugin version it
loaded. Both commands refresh the marketplace and the installed plugins and
verify the registered repo and ref, and both refuse an agent identity (see
[Switching an existing machine](consuming-repo-setup.md#switching-an-existing-machine),
which also covers VMs that are not running). Where Claude Code's background
auto-update is on, it does the marketplace and plugin update on its own.

That verification does not prove the rollback reached a machine: the ref is
`v2.1` before and after, so it passes whether or not the plugin moved back.
Finish by checking each machine, host and every VM: `claude plugin list
--json` must show `vergil@vergil-marketplace` at the `X.Y.Z` you rolled back
to. Run `vrg-host plugins` or `vrg-vm update` again on any machine that does
not.

### How long a rollback lasts

A rollback lasts until the next release, whose `promote` stage moves `v2.1`
forward again: promote is part of every release, and no flag skips it.
Keeping the rollback in place until the fix ships is managed by hand: the
human who rolled back cuts no release of the repo until one carries the
fix. Rolling forward again is the [Move the tag back](#move-the-tag-back)
steps with the latest `v2.1.Z`. Nothing holds the tag automatically. An
issue that blocks promotion until it is closed is a separate idea,
[vergil-project/.github#763](https://github.com/vergil-project/.github/issues/763).

A release already in flight undoes a rollback with no prompt, because the
`promote` stage asks nothing. A vergil-claude-plugin release that is still
running moves `v2.1` forward when it reaches `promote`. So does
`vrg-release --resume` of any open `release: <version>` tracking issue whose
`promote` is not recorded done, the release you are rolling back included.
Before you roll back, let a running release finish, and resume every open
tracking issue to its end.

## Dependency updates

Every release updates the repo's dependencies to the newest versions
their declared bounds allow. This is the `update-deps` stage,
and it is the first change on the new `develop` version. It runs after
`finalize`, once the release is tagged and published, so it can
never affect the release itself, and its failure holds back none of
`promote`, `finalize` and `install`. It does hold back `close-tracking`:
a failed update leaves the tracking issue open, and `vrg-release
--resume` runs it again. Pass `--without-deps` to skip it. The
flag is recorded on the tracking issue, so every later `--resume` skips
the stage too. Given on a resume, it skips an update that failed, and
the release then finishes without it.

The stage brings the root checkout's `develop` up to date with
`origin/develop` (the root checkout must be on `develop`), then runs the
same pipeline as [`vrg-update-deps`](../reference/dev/update-deps.md).
Both syncs hold the [checkout lock](#the-develop-sync). A merge can land
between them, so the pipeline's own preflight, which run standalone
refuses a `develop` that differs from `origin/develop`, fast-forwards
one that is behind when the release runs it. The pipeline:

1. Create the `deps/update-<version>` branch in a managed worktree,
   where `<version>` is the new `develop` version. If that branch
   already exists, its PR decides what happens; see
   [An existing update branch](../reference/dev/update-deps.md#an-existing-update-branch).
   On `--resume`, if the update for this version already merged, the
   stage deletes the branch it left, on origin and locally, then
   succeeds, and its tracking-issue comment links the merged PR.
2. Run each applicable updater, committing each one's changes
   separately. The built-in updaters and the per-repo hook are described
   in the [dependency-update reference](../reference/dependency-update-config.md).
3. If nothing changed, stop: no branch is pushed and no PR is opened.
   The tracking-issue comment says "No dependency changes; no PR opened."
4. Otherwise run `vrg-container-run -- vrg-validate` once, push the
   branch, and open a PR to `develop` labelled `dependency-update`.
5. If validation passed, arm the PR through the one PR path (auto-merge
   with a merge commit, pinned to the pushed head) and wait until it
   merges. A PR the path refuses is a merge failure, with the path's
   reason. If a release lock holds the PR unarmed (held), the PR has not
   merged, so the release's own `update-deps` stage fails, saying the PR
   is held and unarmed under the release lock, rather than going on as if
   it had merged. Once
   the PR is confirmed merged, remove the worktree and delete the
   `deps/update-<version>` branch on origin and locally. If the PR did
   not merge, the branch stays on origin, as the open PR's head, for a
   resume. A failed deletion fails the stage, with the branch to delete
   by hand: it is not recorded done, and `vrg-release --resume`, finding
   the PR merged, tries the deletion again.

The stage's comment on the tracking issue always carries a
**Held back by declared bounds** table: the dependencies whose newest
release is outside the bounds the repo declares. These are reported,
never bumped. A dependency on that list moves only when someone widens
its bound deliberately.

### When the dependency update fails

A failed dependency update never undoes the release, but it cannot be
ignored: the release does not finish until the update succeeds or is
skipped. What it leaves behind depends on where it failed:

- **An updater or the repo hook failed** (`update`), for example a
  lookup error, a non-zero hook exit, or a hook that committed: this
  happens before validation, so nothing is pushed and no PR is opened.
  A task titled "Fix failed dependency update for `<version>`" is still
  filed under the repo's ad-hoc epic. It stands on its own: it names the
  failing updater, carries the error and the command to reproduce it on
  `develop`, and is linked from the failure on the tracking issue and in
  the run summary. The worktree is removed. With no labelled PR, it does
  not trip the [dependency-update gate](#the-dependency-update-gate),
  though the open tracking issue still holds back the next release until
  this one is finished.
- **Validation failed** (`validation`), or **the PR's checks or merge
  failed** (`merge`): the PR stays open with its `dependency-update`
  label, and a task titled "Fix failed dependency update for
  `<version>`" is filed under the repo's ad-hoc epic. The PR body gets a
  `Fix task: <url>` line and a failure section. For a validation
  failure, that section shows the output of the failing `vrg-validate`
  stage, or, when that stage cannot be isolated, a labelled tail of the
  raw output. It ends with the command to reproduce the failure.

If a fix task for the same version and failure kind is already open,
for example after `vrg-release --resume` re-runs an update whose
updater failed on a transient error, no second task is filed: the new failure is added
as a comment on the open task, which is linked as above. Only an open
task whose body the trust layer trusts is reused: a look-alike task
opened or edited by an identity with no trusted role is reported on
stderr, metadata only, and never adopted.

Either way `vrg-release` exits 1, and the failure counts once in its
summary: the `update-deps` stage fails, and `publish-status` repeats it
as a warning, alongside any other failure. The install has still run,
but `close-tracking` holds, so the tracking issue stays open:
`vrg-release --resume` runs the update again once its cause is fixed,
and `vrg-release --resume --without-deps` finishes the release without
it, leaving the update to its fix task.

A `validation` or `merge` failure leaves its PR open, and while it is
open the resumed update refuses to start: the update for that version
is still in flight (see
[An existing update branch](../reference/dev/update-deps.md#an-existing-update-branch)).
So once the cause is fixed, either finish the release with
`vrg-release --resume --without-deps`, which leaves the PR to the next
release's [dependency-update gate](#the-dependency-update-gate), or
close the PR deliberately: `vrg-release --resume` then deletes its
stale branch and runs the update afresh on the fixed `develop`.

To fix a failed update, work the fix task like any other task, but cut
its branch from the failed update so it starts from those commits:

```bash
vrg-git fetch origin
vrg-git worktree add -b chore/<fix-issue>-dep-update-<version> \
  .worktrees/issue-<fix-issue>-dep-update origin/deps/update-<version>
```

Fix the cause, then hand off and merge as usual. When the fix PR merges
and closes the fix task as completed, finish the open release as above;
the next fresh `vrg-release` then closes the dependency-update PR for
you, if it is still open.

An `update` failure pushed no `deps/update-<version>` branch, so cut
that fix task's branch from `origin/develop` instead. Once the fix
merges, `vrg-release --resume` applies the update and finishes the
release.

### The dependency-update gate

On a fresh start (not `--resume`), `preflight` lists the open PRs
labelled `dependency-update`. For each one:

- **The PR's fix task is closed as completed.** The update landed
  through the fix PR. A squash-merged fix PR never marks the deps PR as
  merged, so preflight comments on the deps PR, closes it, deletes its
  `deps/update-<version>` branch, and records it in the `prepare` comment
  on the tracking issue. GitHub's delete-on-merge setting never applies
  to a PR closed without merging, so preflight deletes the branch itself,
  but only for a same-repo PR with a trusted author: a fork PR's head is
  in the fork, so the branch of that name here is kept and the fork PR
  is reported on stderr. If that delete fails, the PR stays closed and the gate still clears,
  but the release prints a warning and the `prepare` comment names the
  branch to delete by hand.
- **The fix task is still open, the PR has no fix task, or the fix task
  was closed without being completed.** The PR blocks the release and
  preflight refuses, naming each PR and why it blocks. A fix task
  closed without being completed means the update did not land; whether
  to close the PR or fix it is a human decision.

Only trusted content counts toward the gate. The `Fix task:` link is
read only from a PR body the trust layer trusts, so a PR whose body an
untrusted identity wrote or edited has no fix task and blocks. A
completed close clears the gate only when a trusted identity closed the
fix task: a close by anyone else stops the release before any PR is
touched, naming who closed it, and the human decides.

Clear a blocking PR by landing its fix task, merging the PR, or closing
it deliberately. For an urgent release, `--allow-pending-deps` lets the
release go ahead anyway. The bypass is not silent: it prints a warning
and the `prepare` comment lists the bypassed PRs.

If preflight cannot list the PRs or look up a fix task, it refuses to
release rather than guess. Every fix task is looked up before any PR is
closed, so a failed lookup changes nothing.

### The package re-publish check

For a repo with `[package]`, a fresh start (not `--resume`) refuses to
release when a package it would publish has the same name, full
version, architecture and format as a package an earlier stable
release of the repo already published. Package builds are not
byte-reproducible, so the rebuilt file would differ from the published
one: two packages with one identity but different bytes, a conflict apt
and dnf cannot resolve. The shared package index does not check for it,
so this check and `vrg-package check-unpublished` are what stop it.

The check runs in `preflight`, before anything is branched, tagged or
published. That is the earliest point that knows the full package
versions: they follow from `[package]` (its `version`, its
`upstream-version` joined with the release version, or the release
version when neither is set) and the build matrix, without building
anything.
It runs before the dependency-update gate, so a refusal changes
nothing. The refusal names each conflicting package and the release
that first published it:

```text
vergil-project/example@v1.2.0 would re-publish 4 package(s) at a version an earlier release already published:
  - example-server 2.0.0-1 amd64 (deb): already published by vergil-project/example@v1.1.0
  ...
```

To fix it, bump the package version in `vergil.toml`: a new
`upstream-version`, or for a `version` override a new `version`. An
`upstream-version` package's version ends in the repo version, so each
new release is already a new package version. If the check cannot read the repo's
releases, it refuses rather than skipping. CD runs the same check as a
backstop; see the
[package reference](../reference/package-config.md#the-re-publish-check).

### The main/develop divergence check

A completed release leaves `main` contained in `develop`:
`back-merge-bump` branches `release/post-<version>` from `main` and
merges it to `develop` with a merge commit. `develop` being ahead of
`main` is normal. `main` having commits that `develop` lacks is not. It
means an earlier release stopped between `merge-release` and
`back-merge-bump`, or something landed on `main` directly. A new
release on top of that would fail late, at `merge-release`.

So a fresh start (not `--resume`) fetches `main` and refuses, before
any other fresh-start check, when `origin/main` has commits not on
`origin/develop`. The refusal lists those commits. To recover:

- If an earlier release stopped before its back-merge (its tracking
  issue is still open), finish it with `vrg-release --resume`. A resume
  skips this check, because a release between `merge-release` and
  `back-merge-bump` has `main` ahead of `develop` by design.
- Otherwise, back-merge `main` into `develop` by hand:

  ```bash
  git fetch origin
  git switch -c release/back-merge-main origin/main
  git push -u origin release/back-merge-main
  ```

  Open a PR from `release/back-merge-main` to `develop` and merge it
  with a merge commit. A squash merge leaves `main`'s commits off
  `develop`, and the check keeps refusing. Then re-run `vrg-release`.

A release that supersedes an untagged one (see
[Superseding a release](#superseding-a-release)) passes when `main` is
ahead only by the superseded release's commit and its ancestors.

If preflight cannot fetch `main` or compare the branches, it refuses
rather than skipping.

### One release at a time

A repo has at most one release in flight. A fresh start (not
`--resume`) refuses while **any** `release: X.Y.Z` tracking issue is
open in the repo, whatever its version, and names each open issue with
its recovery:

- An issue with a state block is finished with
  `vrg-release --resume X.Y.Z`.
- An issue whose state block cannot be read is finished the same way,
  once the block is fixed; the refusal gives the parse error.
- An issue with no state block was opened by older `vrg-release`
  tooling, which `--resume` cannot finish. Finish that release by hand,
  then close the issue.

Only an issue whose body the trust layer trusts counts: a
`release: X.Y.Z` issue opened or edited by an identity with no trusted
role is reported on stderr, metadata only, and never blocks the start.

The one exception is `vrg-release --supersede X.Y.Z`, whose check
passes over release `X.Y.Z`'s own tracking issue, and only it (see
[Superseding a release](#superseding-a-release)).

`--resume X.Y.Z` still takes a version, for a repo that older tooling
left with several releases open. If preflight cannot list the open
issues, or the listing may be truncated, it refuses rather than
reading that as no release in flight.

The [release lock](#the-release-lock) is the atomic guard against two releases at once. This check stays as the second guard, for a release past its `unlock` whose tracking issue is still open.

### The release lock

A release holds a per-repository **release lock** from just before the cut
until its `unlock` stage, so `develop` cannot move under it. Only the
release's own PRs merge into `develop` while it is held: the back-merge and
the dependency update, which the release arms with the holder's pass. Every
other PR is left unarmed (see [Agents during a release](#agents-during-a-release)).
The lock is enforced by the tooling, not by convention. It is not the GitHub
lock on the tracking issue (see
[The lock and the identity rule](#the-lock-and-the-identity-rule)), and not
the checkout lock of [the develop sync](#the-develop-sync).

The lock is the ref `refs/vergil/release-lock` on `origin`, a one-file commit
whose JSON record names the holder (identity and host), the release version,
the release worktree, the tracking issue, the current stage, the health
(`running`, or `broken` with the stage it broke at), the start time and a
heartbeat. A release **takes** it with a create-only push, so only one release
can; it **updates** it by compare-and-swap, so a holder whose lock was broken or
reclaimed finds out at its next write and stops. A background writer refreshes
the heartbeat every 60 seconds, through every wait, so a live release always
has a fresh one. Every break, reclaim and takeover of a lock is recorded in a
second ref, `refs/vergil/release-lock-history`. The lock is coordination
among trusted writers, not a security boundary, and is read as written; a lock
that cannot be read or parsed counts as held, never as absent.

#### Taking the lock

A fresh `vrg-release` runs this in `preflight`, before the cut:

1. **Pre-checks, with nothing locked.** The open-release check, the
   main/develop divergence check, the dependency-update gate and the
   develop-CD gate run first. A refusal there exits with nothing locked and
   nothing disarmed.
2. **The lock.** A lock found stale is reclaimed (see
   [Stale locks](#stale-locks-and-breaking-a-lock)). Any other held lock
   refuses the run, naming its holder, version, stage and health. Otherwise the
   release takes the lock.
3. **The quiesce.** The release waits for every arming ref to clear (the
   short-lived per-PR marker the one PR path writes while it arms a PR; one
   whose value is unchanged for 300 seconds is expired and deleted; the whole
   wait is bounded at 900 seconds, past which the release fails naming the
   refs). It then turns auto-merge off on every open PR into `develop`, again
   if it deleted an expired ref, and checks every PR's auto-merge state once
   more. From here `develop` cannot move.
4. **The cut.** The release syncs `develop` again and runs the dependency-update
   gate again, since the set of open dependency-update PRs can change before the
   disarm. If `develop`'s head moved between the pre-checks and the disarm, it
   runs the head-judging gates again on the new head. It then cuts from that
   head, so the gates have judged exactly what is cut. Everything merged before
   the disarm is in the release; a PR still in flight stays at its CI gates.

`prepare` writes the new tracking issue into the lock before it does anything
else, and its stage comment states the lock the release holds: the holder and
host, the version, the lock's commit sha and when it was taken. Every exit
after the lock is taken and before the tracking issue exists, a refusal or a
failure, runs the full unlock path: the lock is deleted, then, if the disarm
had started, every disarmed PR is offered back to the one PR path. A PR is
never left disarmed with no lock held.

#### Holding it, and the stages that end it

The lock's `stage` follows the release, and its `health` turns `broken` at the
first recorded failure, a deferred failure included. A broken release keeps
the lock: `develop` stays frozen until the release is finished with
`vrg-release --resume`, or the human breaks the lock. That is deliberate. A
broken release is a stop-the-line event, and unlocking early would let
`develop` move under stages that still work against it.

Two tracked stages end the lock, after `release-records` and before
`close-tracking`, so the tracking issue stays open until both succeed:

- `unlock` deletes the lock, by compare-and-swap against the holder's last
  write.
- `rearm` offers every open, non-draft PR into `develop` with auto-merge off
  back to the one PR path, which turns it on again. A PR the path refuses or
  holds is listed on the tracking issue and is not a failure. The sweep is
  idempotent: a re-run offers only the PRs still unarmed.

Both stages need every stage before them, as `release-records` and
`close-tracking` do (see [the hold rule](#the-hold-rule)). The stages from
`unlock` on do not depend on the lock: a release that breaks at `rearm`, or
later, stays visible through its open tracking issue, and `--resume` finishes it
without looking for a lock.

#### Stray merges

A PR merged into `develop` after the cut, other than the release's own
back-merge and dependency update, is a **stray merge**: a human merge, say, or
an arm that slipped past the quiesce. The release looks for one twice:

- **Before `merge-release`**, while nothing is tagged or published, a stray merge
  is a hard error that **abandons** the release. The abandon step closes the
  release PR into `main`, removes the `release/<version>` branch (locally and on
  `origin`) and the release worktree, closes the tracking issue as not planned
  with an `abandoned` comment naming the PR, and only then runs the full unlock
  path. The run exits 1, naming the PR; run a fresh `vrg-release`, whose cut
  includes it. An interrupted abandon is repeated by `--resume`, which skips the
  steps already done.
- **Before `unlock`**, a stray merge is reported and recorded in the `unlock`
  stage's comment on the tracking issue, and the release goes on. The release
  is right for what it cut, and the PR goes into the next release.

#### Resuming and superseding under the lock

`vrg-release --resume` at a stage before `unlock` holds the lock again. It
reads the lock and judges it:

- **Its own lock**, when it names this release's identity and tracking issue
  (the host is recorded for information only, so a resume on another machine
  adopts it too), that is `broken` or has a heartbeat older than 900 seconds is
  adopted, with a new token. The resume marks it `running` again at
  `preflight`, and posts a `lock-resumed` comment on the tracking issue saying
  it adopted the lock and its prior health.
- **Its own lock that is `running` with a fresh heartbeat** is refused, naming
  its host and the heartbeat's age: the first run may still be alive, and two
  live runs of one release never both go on. Wait for the heartbeat to go
  stale, or break the lock.
- **No lock**, because it was broken or reclaimed, is re-taken by a create-only
  push, and the re-take is recorded in the lock history and in the `lock-resumed`
  comment. The break or reclaim ended with a re-arm sweep, so the re-take then
  quiesces again, exactly as a fresh take does. If the first undone stage is
  before `merge-release`, it then runs the stray-merge check, and a PR merged
  after the cut abandons the release as above.
- **Another release's lock, or one that cannot be read**, is refused, naming it.

A resume that stops after taking the lock marks it `broken` at `preflight`, so
the next resume adopts it at once. A resume at `unlock` takes and adopts no lock:
it deletes what is left of its own release's lock, finds none held and goes on,
and refuses any other. A resume past `unlock` reads no lock.

`vrg-release --supersede X.Y.Z` takes over the superseded release's lock rather
than taking a new one, by compare-and-swap, so `develop` stays locked
throughout and no re-arm sweep runs in between. It does so only when that lock
names the superseded release and is `broken` or has a stale heartbeat; a lock
`running` with a fresh heartbeat is refused, naming it. The takeover is
recorded in the lock history and in a `lock-takeover` comment on both tracking
issues, and clears the lock's tracking issue until the new release's `prepare`
writes its own, before the superseded release's issue is closed, so the lock
never names a closed issue while the new release runs. The new release's
`prepare` comment carries the same lock line as a fresh one. When the
superseded release holds no lock (it was broken or reclaimed), the new release
takes a fresh one and quiesces, as a fresh start does.

#### Stale locks and breaking a lock

A lock that is `running` with a heartbeat younger than 900 seconds is live and
never stale. Otherwise a lock that names a tracking issue is stale exactly when
that issue is closed: a broken release with an open tracking issue is the
human's to finish, however old its heartbeat. A lock that names no issue is
stale once its heartbeat is 900 seconds old, unless an open release tracking
issue of its version exists. A lock that cannot be read is never stale. A stale
lock is reclaimed by any trusted identity: the next release's `preflight`
does it, and so does a merge watch that finds one (see
[Under the release lock](../reference/dev/finalize-pr.md#under-the-release-lock)).

[`vrg-release-lock status`](../reference/cli-tools-overview.md#vrg-release-lock)
prints the lock (`--json` for a machine): the holder, version, stage, health,
start, heartbeat, whether it is stale and why, and the PRs it holds unarmed.
`vrg-repo-status` shows the same text in its Release lock section.
`vrg-release-lock break --reason "<why>"` is the human's recorded escape hatch,
refused to every agent: it removes any held lock, a live or unreadable one
included, records the reason and the old lock in the lock history and on the
tracking issue, and re-arms the PRs the lock held. A release whose lock was
broken finds out at its next write and stops; finish it with
`vrg-release --resume`, which re-takes the lock. The daily agent audit reports a
`stale-lock`, an `unreadable-lock`, and any PR left unarmed while no lock is
held (an `invariant-breach`); see
[`vrg-agent-audit`](../reference/cli-tools-overview.md#vrg-agent-audit).

### Agents during a release

While a release holds the repository's lock, the release's open PRs into
`develop` stay unarmed and the merge watch shows them held (see
[Under the release lock](../reference/dev/finalize-pr.md#under-the-release-lock)).
Agents may keep working, but a submit does not go out during the lock:
`vrg-submit-pr`, in any mode and in push mode too, pushes nothing and opens
no PR, and exits 7 with the lock's holder, version, stage and health named.
An agent that must wait re-runs it with `--wait-for-release`, which polls the
lock until it clears, within `$VRG_FINALIZE_DEADLINE` (7200 seconds by
default), and then pushes once. A push-mode wait rebases onto a `develop` the
back-merge moved. The deadline exits 6 with nothing pushed. This reverses the
earlier stance that CI keeps running on PRs throughout a release, for submit:
a head pushed during the lock cannot merge until the release is done, and a
push that lands after the release tags its version but before the back-merge
bumps `develop` fails `version / version-bump` and needs a rebase and a second
CI run. The full rules are in
[The release lock](../reference/dev/submit-pr.md#the-release-lock).

### The develop sync

Preflight does not refuse a root checkout that is merely behind
`origin/develop`: another PR may well have merged since you last pulled,
or between `vrg-finalize-pr`'s validate and the release it chains. It
holds the checkout lock (`build/.vergil-state/checkout.lock`, the one
`vrg-finalize-pr` holds while it syncs and cleans up) for just this
sync:

1. It refuses a branch other than `develop`, or a dirty tree.
2. It fetches `origin develop` with `--tags --force`, so the
   already-tagged check sees the remote tags.
3. If local `develop` is behind `origin/develop`, it fast-forwards it
   and prints `Fast-forwarded develop <old>..<new>`. A local `develop`
   that has diverged, or carries commits `origin/develop` lacks, is
   refused: resolve it first.

It then releases the lock, and the release is cut from the new head.
The [develop-CD gate](#the-develop-cd-gate) checks that head, after the
lock is released, since it may wait.

### The develop-CD gate

A fresh start (not `--resume`) refuses to cut a release from a
`develop` whose CD is not green. It is the last preflight check, because
it may wait. It finds the develop CD run for the `develop` head the
[develop sync](#the-develop-sync) reached,
by its commit, the way `vrg-finalize-pr` finds the synced head's run:

- **Queued, pending or in progress.** The gate waits for it, up to
  `$VRG_CD_TIMEOUT` seconds (default 5400). A run still not complete at
  the deadline refuses, as "result unknown": re-run `vrg-release` once
  it finishes. The wait reads the run as the confirm stages do
  ([waiting for a CD run](#waiting-for-a-cd-run)): a run that cannot be
  read refuses as a read failure naming its cause, never as a run still
  in progress.
- **Succeeded.** The release goes ahead.
- **Failed.** The release refuses, naming the failed jobs and linking the
  run. A failure only in a docs job is the exception: `confirm-develop`
  defers a develop docs publish, and so does the gate. It prints a loud
  warning and the release goes ahead.
- **Superseded.** GitHub cancelled it to make way for a higher-priority
  run in the same concurrency group. The gate judges the develop CD run
  created next instead. With none, it refuses: nothing green covers the
  head. Re-run the superseded run, then re-run `vrg-release`.
- **Cancelled any other way.** The release refuses.
- **No run registered** for the head within 120 seconds. The gate judges
  the latest completed develop CD run whose head is an ancestor of the
  release head instead, and requires it to be green.

If the gate cannot list the runs or read a run's jobs, it refuses
rather than skipping. A resume skips the gate: the release it adopts
already passed it.

## Documentation publication

The Documentation workflow (`.github/workflows/docs.yml`) deploys
the MkDocs site to GitHub Pages on every push to `develop` and
`main`. It's container-based: the workflow runs inside
`dev-base:latest` and uses
`vergil-project/vergil-actions/actions/docs-deploy`.

**The post-merge publish is reported, then gated at release.** The CD
workflow (docker build/publish, docs, release) is dispatched
asynchronously by the base-branch push a merge produces.
`vrg-finalize-pr` finds the CD run for the `develop` head it synced,
identified by that commit, not "the latest run on the branch", and
reports it as `CD: <state> <url>` without waiting on it (epic
vergil-project/.github#1218). A failed run is a warning there, not a
failure: the [develop-CD gate](#the-develop-cd-gate) is what refuses to
cut a release from a `develop` whose CD is not green. `--wait-cd` opts
in to blocking on that run: finalize then fails (exit 4) if it does not
succeed, with a direct link to the log. The wait is bounded by
`VRG_CD_TIMEOUT` (seconds, default 90 minutes). A run still going at the
deadline is reported as "still in progress; result unknown", with the
run URL and how to re-check it, not as a failed run. It still fails the
wait (issue #3316). A run that cannot be read while finalize waits is
an error naming its cause, never a run still in progress (issue #5174).

For deeper investigation, view recent runs directly:

```bash
gh run list --repo vergil-project/vergil-tooling \
  --workflow="Documentation" --limit 5
```

## Release tracking artifacts

Each release has a tracking issue, `release: <version>`, which
`vrg-release` creates in its `prepare` stage. The issue is created with
three parts in its body:

- a **Contents** section: the table of what `main..<cut>` holds (each
  PR, or a commit that reached `develop` with no PR, as
  `vrg-repo-status` shows it), read in `preflight` pinned to the
  `develop` commit the release branch is cut from, and under it the
  issues the release contains with their closing PRs (see
  [The issue list](#the-issue-list)); a release that supersedes another
  also names it, and marks the issues it absorbed from it (see
  [Superseding a release](#superseding-a-release));
- the stage checklist (see
  [An interrupted release](#an-interrupted-release));
- the **state block**, a fenced `json` document between
  `<!-- vrg-release:state:begin -->` and
  `<!-- vrg-release:state:end -->`, each alone on its line. It holds
  the version and the cut, and is where the release records its own
  progress. Only `vrg-release` writes it, by a read-modify-write of the
  body. A block that is missing, duplicated or malformed, or names a
  version other than the issue title's, is refused, never replaced by
  defaults. Do not edit it by hand.

`prepare` locks the tracking issue as soon as it creates it, so only
the repository's collaborators can comment on it.

If `preflight` cannot read the contents completely, the release is
refused before the tracking issue is created; the next run adopts the
unpushed release branch it left. Each tracked stage adds
a comment when it completes or fails:

- The `prepare` comment names the release branch, the release PR and
  the tracking issue. It also lists any dependency-update PRs the gate
  closed because their fix task completed, and any it let through with
  `--allow-pending-deps`.
- The `prepare` comment also states the [release lock](#the-release-lock) the release holds: the holder and host, the version, the lock's commit sha and when it was taken. A superseding release's `prepare` comment carries the same line, and a `lock-takeover` comment records the takeover on both tracking issues.
- A resume that holds the lock again posts a `lock-resumed` comment saying whether it adopted its own lock, with the lock's prior health, or re-took it.
- The `unlock` comment says the lock is released and names any [stray merge](#stray-merges), and the `rearm` comment lists the PRs it re-armed, merged, left held or saw refused. A release abandoned on a stray merge closes its tracking issue as not planned, with an `abandoned` comment naming the reason.
- Later comments record the merge, the tag and GitHub Release, the CD
  runs, the back-merge PR and next version, and the promotion.
- The `update-deps` comment carries the held-back table and either the
  dependency-update PR or "No dependency changes; no PR opened."
  (or "Dependency update skipped (--without-deps)."). A failed update
  posts a failure comment with the same table and the failure section
  instead.

The `close-tracking` stage closes the issue with a summary, after every
other tracked stage, once the state block records a
[`Released:` record](#released-records) posted on every issue the
release contains. It stays open when any earlier stage failed, a
deferred publish, a failed dependency update or a record that could not
be posted included, so the release can be resumed. `vrg-release
--resume` finds the open tracking issue and runs only the stages its
state block does not record as done (see
[Resuming a release](#resuming-a-release)).

Use the issue as well to capture the rationale for the release and any
consumer coordination it needs, especially for minor and major bumps.
Follow-ups move to their own issues.

### The issue list

The issue list is the union of the closing references of every PR in
`main..<cut>` whose head branch starts `feature/`, a batch PR included.
A PR's head branch is read only when the trust layer accepts the PR: a
same-repo PR whose author holds a role in `vrg-trusted-authors`. A fork
PR, or one an outsider opened, is an `untrusted` row of the Contents
table, by its commit subject, and is reported on stderr, metadata only.
Release back-merges, dependency updates, automation PRs, untrusted PRs
and commits with no PR contribute none, and a closing reference to an
issue in another repo is ignored. Each issue is listed once, by number,
with every PR that closes it.

Only a trusted closing reference counts:

- one a closing line in the PR body names (`Closes`, `Close` or
  `Closed`, the keywords the fleet's linkage rules sanction), while
  every identity in the body's history (its author, its last editor
  and every earlier editor) holds the `human` or `agents` role, as the
  trust layer judges it (a login of `vrg-trusted-authors` with the
  account type of its role; an org's automation App does not count); or
- one linked to the PR by hand, when every identity that linked it
  holds one of those roles.

A PR body with any other identity in its history is never parsed. Any
other closing reference contributes no issue, and `preflight` names it,
with its PR and the reason (the untrusted identities by login and
cause), in a `WARNING:` line. A release
whose PRs close no issues has an empty list, which is valid. The
trusted authors failing to load, or any of the reads failing or coming
back truncated, refuses the release like any other incomplete
contents lookup.

### `Released:` records

The `release-records` stage posts one comment, as the human running
`vrg-release`, on each issue in the issue list:

```text
<!-- vrg-release:released:vergil-project/vergil-tooling:v2.1.340 -->
Released: vergil-project/vergil-tooling v2.1.340 on 2026-11-20

- Release: https://github.com/vergil-project/vergil-tooling/releases/tag/v2.1.340
- Closed by: #4840 (merge commit 1a2b3c4)
- Install: ran — installed v2.1.340 (commit fab2581)
```

- The date is the day the GitHub Release was published, and each closing
  PR is named with the commit it merged as.
- The install line reads `ran` when the repo configures
  `[publish].install` and `none configured` when it does not; a record
  in a repo with no install attests the release and its publish only.
  When the install's output reports what it installed, in a line
  holding `installed vX.Y.Z (commit <sha>)` as `vrg-tooling-install`
  prints, the `install` stage records that release and commit in the
  state block (`install_version` and `install_commit`), and the line
  names them: `ran — installed v2.1.340 (commit fab2581)`. An install
  that reports neither keeps the plain `` ran (`[publish].install`) ``.
  The stage refuses while the state block does not record `install`
  done.
- Every issue in the list gets its record, whatever its state: closed as
  completed, reopened, or closed another way.
- The stage prints one line per issue as it records it:
  `posted: <issue URL>` for a new record, or
  `found existing: <issue URL>` when the issue already has one.
- A release that supersedes another adds a `- Supersedes: vX.Y.Z` line
  to the record on each issue it absorbed from it (see
  [Superseding a release](#superseding-a-release)).
- A record counts only when its comment's author and last editor both
  hold the `human` role: a `human` login of `vrg-trusted-authors` whose
  account is a GitHub user, as the trust layer judges it. Any other
  comment, an agent's included, is never read for a record: the stage
  reports it on stderr, by issue, login, time and comment only (counts
  by cause under GitHub Actions), and ignores it.
- Before posting on an issue, the stage reads all its comments, every
  page, for a counting record, and records the issue as posted in the
  state block once it has found or posted one. A run interrupted partway
  therefore posts only the rest, and no issue gets two. Each post is
  made once and never retried, so a post whose failure hid a landed
  comment is found by the next run's read instead of repeated.
- A read or post that fails fails that issue, and the stage carries on
  to the rest, then fails naming each. `close-tracking` does not run,
  and `vrg-release --resume` posts the missing records, then closes the
  issue.
- A record is release evidence for the issue:
  [`vrg-release-record check`](../reference/dev/release-record.md) reads
  it, confirms it against git and prints a verdict, which is how a
  deployment task confirms that a blocked-by task is released.

### The lock and the identity rule

The state block is trusted only while the tracking issue is the human's.
Every time `vrg-release` reads the state block, and again in the read
before each write of it, it first locks the issue if it is not locked
(an issue inherited from an older run included), then reads every
identity on the issue through the trust layer, every page, and accepts
only the `human` role: a `human` login of
[`vrg-trusted-authors`](../reference/cli-tools-overview.md#vrg-trusted-authors)
whose account is a GitHub user.

- **The identities that can change the state:** the issue's author,
  the body's last editor and every editor in its edit history, and
  every actor that closed or reopened the issue. Each must hold the
  `human` role. Any other identity, an agent, a deleted account (`ghost`)
  or an account of the wrong type (an App named like the human) included,
  stops the run before it writes anything, with an error starting
  `suspected security breach on tracking issue #<N>:` that names the
  identity, what it did and why it holds no role (`untrusted`,
  `not-accepted`, `deleted` or `wrong-type`). The failure is not commented on
  the issue. Inspect the issue's edit history and events, but do not
  try to clean it up: GitHub keeps the editor of a body edit even once
  its revision is deleted, and keeps the issue's close and reopen
  events, so the issue can never pass the check again and the release
  can never be resumed. Replace the release instead, as
  [A breached tracking issue](#a-breached-tracking-issue) says.
- **Other traces** cannot change the state: a comment or an edit of
  one, a cross-reference, a mention, a subscription, or a label added or
  removed by another identity. Each is printed as a warning naming the
  identity and what it did, never its text, once per run, and the run
  carries on.

A failed read of the trusted authors or of the issue's identities, or
a response that cannot be read in full, stops the run with the call
that failed; it is never taken as "no other identity".
