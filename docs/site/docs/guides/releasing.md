# Releasing

This guide covers the release workflow for `vergil-tooling`,
including how patch, minor, and major versions affect both the
release author (producer) and downstream consumers.

The model: `vergil-tooling` is distributed as a host-level
developer tool plus a dev-container pre-bake (see
[host-level-tool spec][hlt]). Three deployment targets — developer
host, Python project `.venv`, and dev container image — each track
the **rolling minor tag** (e.g. `v1.3`), which is force-updated by
`tag-and-release` on every patch release. The dev container images
rebuild automatically on each `vergil-tooling` release via a
`repository_dispatch` trigger.

[hlt]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/host-level-tool.md

## Tag scheme — what's user-facing, what isn't

Two parallel tag families exist in the repo. Consumers only see and
use the first; the second is internal scaffolding for the changelog
flow.

| Tag pattern | Where | Purpose | User-facing? |
|---|---|---|---|
| `v1.3.0` | `main` | Release tag | Yes — pin in install commands |
| `v1.3` | `main` | Rolling minor tag (force-updated each patch) | Yes — what consumers actually pin to |
| `develop-v1.3.0` | `develop` | Boundary marker for `git-cliff`'s next-changelog scoping | No — internal |

The `develop-` prefix is invisible to install / consumption; never
pin to a `develop-v*` tag.

## Patch release — the common path

A patch release fixes a bug or refines an existing feature without
adding new public surface. Versions: `v1.3.0` → `v1.3.1`.

### Producer steps

1. Land all the patch's changes via normal feature PRs to `develop`.
2. From the root checkout, on a clean `develop` that matches
   `origin/develop`, run the release:

   ```bash
   vrg-release
   ```

   `vrg-release` is a human-only command: it refuses to run under an
   agent identity. It needs `git-cliff` on `PATH` and an authenticated
   `gh`. It drives the whole release through the stages listed in
   [The `vrg-release` pipeline](#the-vrg-release-pipeline) below, in a
   managed worktree, so the root checkout is never switched off
   `develop`. See the [`vrg-release` reference](../reference/dev/release.md)
   for every flag.
3. While `vrg-release` waits on CD, the post-merge automation runs:
   - CD on `main` creates the release tag (`v1.3.1`).
   - The rolling minor tag (`v1.3`) is **force-updated** to point at
     the new release.
   - The boundary tag (`develop-v1.3.1`) is created on `develop`.
   - `repository_dispatch` fires a `vergil-tooling-released`
     event at `vergil-containers`, which rebuilds the dev
     images against the now-current `v1.3` tag.
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
     index (up to 20 minutes, every 30 seconds) until the new version is
     visible, so the consumer refresh never races the index. While it
     waits, it streams the `Publish package index` run in `<org>/packages`
     that covers the release and prints a heartbeat line on each poll
     otherwise; the index poll alone decides success. A miss
     (timeout or polling error) does not undo the release. It is recorded
     as a deferred publish failure: the tracking issue stays open with a
     deferral comment, the consumer-refresh stage holds (it
     prints a warning not to advertise the version instead of the refresh
     commands), the stage error tells you to re-run the `publish-index`
     workflow in `<org>/packages`, and `vrg-release` exits 1. See the
     [package config reference](../reference/package-config.md#cd).
4. Act on the consumer-refresh commands `vrg-release` prints at the end
   (or pass `--install` to have it run them after a clean release).
5. Check the end of the run for the `update-deps` result. A failed
   dependency update does not fail the release, but it leaves an open
   PR that blocks the next one; see
   [Dependency updates](#dependency-updates).

### The `vrg-release` pipeline

`vrg-release` runs these stages in order. A *fail-fast* stage stops the
run when it fails. A *deferred* stage records its failure and lets the
later stages run; the run then exits 1 with a summary. Every tracked
stage posts a completion or failure comment on the release tracking
issue and ticks its box in the issue's stage checklist.

| Stage | Mode | What it does |
|---|---|---|
| `audit` | fail-fast | Runs `vrg-github-repo-config audit` on the repo. `--skip-audit` skips it |
| `preflight` | fail-fast | Checks the host prerequisites, `gh` auth, `vergil.toml`, and that the root checkout is a clean `develop` matching `origin/develop`. On a fresh start it also runs the [main/develop divergence check](#the-maindevelop-divergence-check), checks the version is not tagged, that no tracking issue exists for it, runs the [package re-publish check](#the-package-re-publish-check), and runs the [dependency-update gate](#the-dependency-update-gate). Creates the `release/<version>` managed worktree |
| `prepare` | fail-fast | Creates the `release: <version>` tracking issue, generates the changelog and release notes, and opens the release PR to `main` |
| `merge-release` | fail-fast | Waits for the release PR's checks and merges it. If the PR conflicts with `main`, it merges `main` into the release branch and pushes it. A conflict in `CHANGELOG.md` alone is resolved by keeping the release branch's generated changelog; any other conflict aborts the merge and stops with the commands to resolve it by hand |
| `confirm-main` | fail-fast | Watches CD on `main`. The release job must succeed; other failed publish jobs are recorded as deferred publish failures (see [Stale job records](#stale-job-records)) |
| `back-merge-bump` | fail-fast | Creates `release/post-<version>` from `main`, bumps the version, and merges it to `develop`. Records the back-merge's merge commit |
| `teardown-worktree` | deferred | Removes the release worktree and returns to the root checkout |
| `confirm-develop` | deferred | Watches the CD run on `develop` for the back-merge's merge commit; a failed docs publish is deferred |
| `promote` | deferred | Moves the rolling `vX.Y` tag to the new release (skipped with `--no-promote`) |
| `package-index` | deferred | For repos with `[package]`, waits for the new version in the package index. A miss is a deferred publish failure, not a halted stage |
| `close-finalize` | deferred | Closes the tracking issue with a summary and runs `vrg-finalize-pr --cleanup-only --no-wait-cd` |
| `consumer-refresh` | deferred | Prints the `[publish].consumer-refresh` commands |
| `update-deps` | deferred | Runs the dependency update on the new `develop` version (skipped with `--without-deps`); see [Dependency updates](#dependency-updates) |
| `publish-status` | deferred | Repeats any deferred publish failure, and any dependency-update failure, so neither is lost |

`close-finalize` leaves the tracking issue open, and skips the cleanup,
when an earlier stage has failed, so the release can be resumed with
`vrg-release --resume`. A deferred publish failure also leaves the
issue open, with a comment that says how to finish each deferred item,
but the release itself is complete: re-run the CD publish (or the
`publish-index` workflow, for a `package-index` miss) rather than
resuming.

`confirm-develop` checks the CD run for the back-merge's merge commit,
not for the current tip of `develop`. On `vrg-release --resume`, other
PRs (a dependency update or a feature) may have merged to `develop`
since the back-merge. The tip's CD run then says nothing about the
release. A resume re-runs `back-merge-bump`, which adopts the existing
back-merge PR and records its merge commit again. If the merge commit
can't be found, `confirm-develop` fails. It never falls back to the tip.

`close-finalize` passes `--no-wait-cd` to `vrg-finalize-pr`. Without a
merge of its own, finalize's CD check would watch the tip of `develop`,
which has the same problem. `confirm-develop` has already checked the
right run, and `close-finalize` runs `vrg-finalize-pr` only when every
earlier stage passed. A second wait would add nothing. It would also
fail the stage on a docs publish failure that `confirm-develop` has
already deferred.

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
  deferred as `<job> (never settled)`.

A failed job in the release job's own family, such as
`release / package-sign`, is reported by its full name rather than as
`release`, so the message is never mistaken for the release itself
failing.

### Consumer steps

For most consumers, **no action is required**: the rolling `v1.3`
tag they're already pinned to now points at the patch release. They
get the fix on the next normal action:

| Consumer | When the patch lands |
|---|---|
| Developer host | Manual `uv tool upgrade vergil-tooling` (or `vrg-host upgrade vergil-tooling` on macOS) |
| Python repo `.venv` | `uv lock --upgrade-package vergil-tooling` (typically batched with other dep bumps) |
| Dev container image | Already rebuilt by `repository_dispatch`; new pulls of `dev-base` etc. carry the patch within the rebuild window (minutes) |
| Agent VM (Lima/cloud) | `vrg-vm update`: an apt upgrade of the packaged install within its pin, once the patch is in the package index. A `vX.Y` line pin picks the patch up; an exact `vX.Y.Z` pin stays put |

## Minor release — opt-in for new features

A minor release adds backwards-compatible new features. Versions:
`v1.3.x` → `v1.4.0`.

### Producer steps

Same steps as patch, with one important addition: the rolling
minor tag changes from `v1.3` to `v1.4`. The image's pin in
`vergil-containers` and any per-repo dev-dep declarations
**must be bumped manually** — the rolling-tag mechanism only handles
patches within a minor.

1. Run `vrg-release minor`. It bumps the version to the next minor
   (`1.4.0`) on the release branch instead of releasing the current
   `develop` version. Everything else matches a patch release.
2. **Bump `vergil-containers`'s pin** in
   [`docker/common/vergil-tooling-uv.dockerfile`][docker-frag]:

   ```dockerfile
   ARG ST_TOOLING_TAG=v1.4   # was v1.3
   ```

   Open a small PR in `vergil-containers`, merge it, and the
   next image rebuild (whether triggered by your release or a
   subsequent push there) carries the new minor.
3. **Coordinate consumer bumps.** Each Python repo that has
   `[tool.uv.sources]` pinning to `v1.3` needs its `pyproject.toml`
   updated to `v1.4`. This is a per-repo decision — consumers who
   prefer to stay on `v1.3` can keep doing so until they're ready.
4. Update the canonical install command in `getting-started.md`
   (and anywhere else the documentation pins a minor) to the new
   tag.

[docker-frag]: https://github.com/vergil-project/vergil-containers/blob/develop/docker/common/vergil-tooling-uv.dockerfile

### Consumer steps

Minor bumps are **deliberate opt-in** at every deployment target:

| Consumer | What changes |
|---|---|
| Developer host | `uv tool install --reinstall 'vergil-tooling @ git+...@v1.4'` (or update the pinned tag in their notes / shell history) |
| Python repo `.venv` | Edit `pyproject.toml` `[tool.uv.sources]` to `tag = "v1.4"`, then `uv lock --upgrade-package vergil-tooling` |
| Dev container image | Wait for `vergil-containers` to land the `ARG` bump, then pull the rebuilt image |
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
3. Bump `vergil-containers`'s `ARG ST_TOOLING_TAG=` to `v2`
   in a separate PR — coordinate so consumers can opt in on their
   own schedule.
4. Notify consumer maintainers (e.g., update `vergil-tooling`'s
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

## Dependency updates

Every release ends by updating the repo's dependencies to the newest
versions their declared bounds allow. This is the `update-deps` stage,
and it is the first change on the new `develop` version. It runs after
`consumer-refresh`, once the release is tagged and published, so it can
never affect the release itself. Pass `--without-deps` to skip it.

The stage brings the root checkout's `develop` up to date with
`origin/develop` (the root checkout must be on `develop`), then runs the
same pipeline as [`vrg-update-deps`](../reference/dev/update-deps.md):

1. Create the `deps/update-<version>` branch in a managed worktree,
   where `<version>` is the new `develop` version. If that branch
   already exists, its PR decides what happens; see
   [An existing update branch](../reference/dev/update-deps.md#an-existing-update-branch).
   On `--resume`, if the update for this version already merged, the
   stage succeeds and its tracking-issue comment links the merged PR.
2. Run each applicable updater, committing each one's changes
   separately. The built-in updaters and the per-repo hook are described
   in the [dependency-update reference](../reference/dependency-update-config.md).
3. If nothing changed, stop: no branch is pushed and no PR is opened.
   The tracking-issue comment says "No dependency changes; no PR opened."
4. Otherwise run `vrg-container-run -- vrg-validate` once, push the
   branch, and open a PR to `develop` labelled `dependency-update`.
5. If validation passed, wait for the PR's checks, merge it, and
   delete its branch on origin.

The stage's comment on the tracking issue always carries a
**Held back by declared bounds** table: the dependencies whose newest
release is outside the bounds the repo declares. These are reported,
never bumped. A dependency on that list moves only when someone widens
its bound deliberately.

### When the dependency update fails

A failed dependency update never fails the release, but it cannot be
ignored. What it leaves behind depends on where it failed:

- **An updater or the repo hook failed** (`update`), for example a
  lookup error, a non-zero hook exit, or a hook that committed: this
  happens before validation, so nothing is pushed and no PR is opened.
  A task titled "Fix failed dependency update for `<version>`" is still
  filed under the repo's ad-hoc epic. It stands on its own: it names the
  failing updater, carries the error and the command to reproduce it on
  `develop`, and is linked from the failure on the tracking issue and in
  the run summary. The worktree is removed. With no labelled PR, it does
  not gate the next release.
- **Validation failed** (`validation`), or **the PR's checks or merge
  failed** (`merge`): the PR stays open with its `dependency-update`
  label, and a task titled "Fix failed dependency update for
  `<version>`" is filed under the repo's ad-hoc epic. The PR body gets a
  `Fix task: <url>` line and a failure section. For a validation
  failure, that section shows the output of the failing `vrg-validate`
  stage, or, when that stage cannot be isolated, a labelled tail of the
  raw output. It ends with the command to reproduce the failure.

If a fix task for the same version and failure kind is already open,
for example after `vrg-release --resume` re-runs an update that failed
on a transient error, no second task is filed: the new failure is added
as a comment on the open task, which is linked as above.

Either way `vrg-release` exits 1, and the failure counts once in its
summary: the `update-deps` stage fails, and `publish-status` repeats it
as a warning. If a publish job also failed, `publish-status` fails with
both.

To fix a failed update, work the fix task like any other task, but cut
its branch from the failed update so it starts from those commits:

```bash
vrg-git fetch origin
vrg-git worktree add -b chore/<fix-issue>-dep-update-<version> \
  .worktrees/issue-<fix-issue>-dep-update origin/deps/update-<version>
```

Fix the cause, then hand off and merge as usual. When the fix PR merges
and closes the fix task as completed, the next `vrg-release` closes the
dependency-update PR for you.

An `update` failure pushed no `deps/update-<version>` branch, so cut
that fix task's branch from `origin/develop` instead. Once the fix
merges, the next `vrg-update-deps` run, or the next `vrg-release`,
applies the update.

### The dependency-update gate

On a fresh start (not `--resume`), `preflight` lists the open PRs
labelled `dependency-update`. For each one:

- **The PR's fix task is closed as completed.** The update landed
  through the fix PR. A squash-merged fix PR never marks the deps PR as
  merged, so preflight comments on the deps PR, closes it, deletes its
  `deps/update-<version>` branch, and records it in the `prepare` comment
  on the tracking issue. GitHub's delete-on-merge setting never applies
  to a PR closed without merging, so preflight deletes the branch itself.
  If that delete fails, the PR stays closed and the gate still clears,
  but the release prints a warning and the `prepare` comment names the
  branch to delete by hand.
- **The fix task is still open, the PR has no fix task, or the fix task
  was closed without being completed.** The PR blocks the release and
  preflight refuses, naming each PR and why it blocks. A fix task
  closed without being completed means the update did not land; whether
  to close the PR or fix it is a human decision.

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
one. The shared package index refuses that, and because the index is
built from every release of every repo, the refusal would fail every
repo's index publish until the duplicate assets were removed by hand.

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
vergil-project/vergil-python@v1.0.4 would re-publish 4 package(s) at a version an earlier release already published:
  - vergil-python3.15 3.15.1+20261021-1 amd64 (deb): already published by vergil-project/vergil-python@v1.0.3
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

If preflight cannot fetch `main` or compare the branches, it refuses
rather than skipping.

## Documentation publication

The Documentation workflow (`.github/workflows/docs.yml`) deploys
the MkDocs site to GitHub Pages on every push to `develop` and
`main`. It's container-based: the workflow runs inside
`dev-base:latest` and uses
`vergil-project/vergil-actions/actions/docs-deploy`.

**The post-merge publish is awaited, not glanced at.** The CD
workflow (docker build/publish, docs, release) is dispatched
asynchronously by the base-branch push the merge produces. To catch
silent failures, `vrg-finalize-pr` blocks on the CD run **this merge
triggered** — identified by the merge commit SHA, not "the latest run
on the branch" — and waits for it to complete. If that run does not
succeed, finalize prints the failure with a direct link to the log and
exits non-zero, so you investigate before moving on. The wait is
bounded by timeouts and `--no-wait-cd` opts out (issue #2753). The watch
deadline defaults to 90 minutes and can be changed with `VRG_CD_TIMEOUT`
(seconds). A run still going at the deadline is reported as "still in
progress; result unknown", with the run URL and how to re-check it, not
as a failed run. It still fails finalize (issue #3316).

For deeper investigation, view recent runs directly:

```bash
gh run list --repo vergil-project/vergil-tooling \
  --workflow="Documentation" --limit 5
```

## Release tracking artifacts

Each release has a tracking issue, `release: <version>`, which
`vrg-release` creates in its `prepare` stage. Its body carries the
stage checklist, and each tracked stage adds a comment when it
completes or fails:

- The `prepare` comment names the release branch, the release PR and
  the tracking issue. It also lists any dependency-update PRs the gate
  closed because their fix task completed, and any it let through with
  `--allow-pending-deps`.
- Later comments record the merge, the tag and GitHub Release, the CD
  runs, the back-merge PR and next version, and the promotion.
- The `update-deps` comment carries the held-back table and either the
  dependency-update PR or "No dependency changes; no PR opened."
  (or "Dependency update skipped (--without-deps)."). A failed update
  posts a failure comment with the same table and the failure section
  instead.

The `close-finalize` stage closes the issue with a summary, which
happens before the last few stages run, so their comments land on the
closed issue. It stays open when an earlier stage failed (so the
release can be resumed) or a publish job was deferred. `vrg-release
--resume` finds the open tracking issue and runs the pipeline again,
adopting the branches, PRs and tag the earlier run already created.

Use the issue as well to capture the rationale for the release and any
consumer coordination it needs, especially for minor and major bumps.
Follow-ups move to their own issues.
