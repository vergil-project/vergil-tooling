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
| **Developer host** (macOS) | `uv tool install` from the git URL, pinned to the rolling minor tag | Host-side commands: `vrg-container-run`, `vrg-commit`, `vrg-submit-pr`, `vrg-release`, `vrg-finalize-pr`, `vrg-host` |
| **Container runtime** (all languages) | `vrg-container-run` cache-first install of the version `vergil.toml` names, in a per-branch cached image | `vrg-*` inside the dev container, locally and in CI, for every consumer |
| **Agent VM** (Lima/cloud) | Signed `vergil-tooling` package via `apt` from <https://vergil-project.github.io/packages>, pinned to the identity's release line or version | Agent sessions inside the VM |

The canonical host install (see [Getting Started](../getting-started.md)):

```bash
uv tool install --python 3.15 'vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@v2.1'
```

On an agent VM, a dev ref (`vrg-vm update --tag develop`, or any non-release
ref) is an explicit `uv tool install --python 3.15` from git that shadows the
packaged copy,
and every `vrg-vm` command that touches the VM prints a `DEV tooling` banner
while it is in place. A plain `vrg-vm update` returns the VM to the package.
See [vergil-tooling inside the VM](../reference/vm-spec.md#vergil-tooling-inside-the-vm).

[hlt]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/host-level-tool.md

## Tag scheme — what's user-facing, what isn't

Two parallel tag families exist in the repo. Consumers only see and
use the first; the second is an internal marker of where `develop`
stood when a release was published.

| Tag pattern | Where | Purpose | User-facing? |
|---|---|---|---|
| `v1.3.0` | `main` | Release tag | Yes — pin in install commands |
| `v1.3` | `main` | Rolling minor tag (force-updated each patch) | Yes — what consumers actually pin to |
| `develop-v1.3.0` | `develop` | Marks the `develop` tip when the release was published | No — internal |

The `develop-` prefix is invisible to install / consumption; never
pin to a `develop-v*` tag.

The changelog and release notes do not use the `develop-v*` tags as
their boundary. CD places one on the `develop` tip after the release
has merged, so a PR merged to `develop` while the release was in flight
would sit behind it without having shipped. A release's changes are
instead the commits since the merge base of the release branch and the
previous `vX.Y.Z` tag, so such a PR appears in the next release. The
new section is added to the top of `CHANGELOG.md`; the entries of
earlier releases are kept as they were published.

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
   - The boundary tag (`develop-v1.3.1`) is created on `develop`.
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
     visible, so the consumer refresh never races the index. With several
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
     re-running it. A miss (timeout, a failed publish-index run, or a
     polling error) does not undo the release. It is recorded
     as a deferred publish failure: the tracking issue stays open with a
     deferral comment, the consumer-refresh stage holds (it
     prints a warning not to advertise the version instead of the refresh
     commands), the stage error tells you to re-run the `publish-index`
     workflow in `<org>/packages`, `promote` leaves `vX.Y` where it is
     (see [A promote held back by the index](#a-promote-held-back-by-the-index)),
     and `vrg-release` exits 1. See the
     [package config reference](../reference/package-config.md#cd).
4. After a clean release, `vrg-release` prints the consumer-refresh
   commands and then runs them (pass `--no-install` to only print them,
   and act on them yourself).
   A clean run names the released tag in its completion line
   (`✓  vrg-release v1.3.1 complete`) and in the
   `Consumer refresh commands for v1.3.1:` header.
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
| `audit` | fail-fast | Runs `vrg-github-repo-config audit` on the repo. Releasing vergil-tooling itself adds `--in-tree-templates`, so its managed files are checked against the templates being released, not the installed release's (see the [config audit reference](../reference/config-audit.md#auditing-vergil-tooling-against-its-own-templates)). `--skip-audit` skips it |
| `preflight` | fail-fast | Checks the host prerequisites, `gh` auth, `vergil.toml`, and that the root checkout is a clean `develop`, then [brings it up to `origin/develop`](#the-develop-sync). On a fresh start it also runs the [main/develop divergence check](#the-maindevelop-divergence-check), checks the version is not tagged, that no tracking issue exists for it, runs the [package re-publish check](#the-package-re-publish-check), runs the [dependency-update gate](#the-dependency-update-gate), and last runs the [develop-CD gate](#the-develop-cd-gate). Creates the `release/<version>` managed worktree, or adopts one an [interrupted run](#an-interrupted-release) left with nothing on it |
| `prepare` | fail-fast | Creates the `release: <version>` tracking issue, generates the changelog and release notes, and opens the release PR to `main` |
| `merge-release` | fail-fast | Waits for the release PR's checks and merges it. If the PR conflicts with `main`, it merges `main` into the release branch and pushes it. A conflict in `CHANGELOG.md` alone is resolved by keeping the release branch's generated changelog; any other conflict aborts the merge and stops with the commands to resolve it by hand |
| `confirm-main` | fail-fast | Watches CD on `main`. The release job must succeed; other failed publish jobs are recorded as deferred publish failures (see [Stale job records](#stale-job-records)) |
| `back-merge-bump` | fail-fast | Creates `release/post-<version>` from `main`, bumps the version, refreshes the lockfile (`uv lock` for Python and `cargo update --workspace` for Rust, in the dev container; for TypeScript, a direct edit of `package-lock.json`'s root version fields that runs no npm), and merges it to `develop`. Records the back-merge's merge commit |
| `teardown-worktree` | deferred | Removes the release worktree and returns to the root checkout |
| `confirm-develop` | deferred | Watches the CD run on `develop` for the back-merge's merge commit; a failed docs publish is deferred |
| `package-index` | deferred | For repos with `[package]`, waits for the new version in the package index. A miss is a deferred publish failure, not a halted stage |
| `promote` | deferred | Moves the rolling `vX.Y` tag to the new release (skipped with `--no-promote`). It runs after `package-index`, so `vX.Y` moves only once the index has the release, and skips when `package-index` deferred (see [A promote held back by the index](#a-promote-held-back-by-the-index)) |
| `close-finalize` | deferred | Closes the tracking issue with a summary and runs `vrg-finalize-pr --cleanup-only` |
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

`close-finalize` does not pass `--wait-cd` to `vrg-finalize-pr`, so
finalize only reports the CD run of `develop`'s tip and never fails on
it. That tip has the same problem: it may be a later commit's.
`confirm-develop` has already checked the right run, and
`close-finalize` runs `vrg-finalize-pr` only when every earlier stage
passed. A second wait would add nothing. It would also fail the stage on
a docs publish failure that `confirm-develop` has already deferred.

#### A promote held back by the index

`promote` runs after `package-index`, so git-based installs of `vX.Y`
(which follow the tag) and packaged installs (which take the newest
`X.Y.*` in the index) get the same release from the moment the tag moves.
When `package-index` defers, `promote` skips and `vX.Y` stays on the
previous release, so the two still agree. The deferral comment, the
`close-finalize` outcome line and the `publish-status` error name the
pending promote. Once the release's packages appear in the index, finish
it by hand:

1. Move the tag with the [`vrg-promote` steps](#move-the-tag-back),
   passing the version: `vrg-promote <X.Y.Z>`.
2. Close the release tracking issue, which stays open until then.

Do not use `vrg-release --resume` for it: the release is complete, with
a deferral, not halted. A release cut with `--no-promote` has no pending
promote, deferral or not.

A release started on tooling that ran `promote` before `package-index`
cannot be resumed by tooling with the new order: `--resume` refuses a
tracking issue whose stage checklist differs from the running tooling's.
Finish such a release with the tooling version that started it.

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
  with its stage checklist in its body, so it is always resumable:
  run `vrg-release --resume`. A fresh `vrg-release` refuses, since a
  tracking issue already exists for the version. The resume adopts the
  local release branch and worktree, unpushed or not, and the `prepare`
  stage skips what the stopped run already did.

A tracking issue opened by an older `vrg-release` can lack the stage
checklist, if that run was interrupted in `prepare`. `--resume` takes
it over when the `release/<version>` branch exists, locally or on
`origin`: the branch shows the fresh start's checks passed, and
`prepare` writes the checklist. With no branch it refuses; close the
issue and run `vrg-release` again.

### A failed release job

When the CD release job on `main` does not succeed, `confirm-main`
fails, and the release PR is already merged. `vrg-release --resume`
alone cannot recover it: it watches the latest attempt of the same CD
run and judges that same failed release job again. `vrg-release` does
not re-run the job itself, since only you can tell that the cause is
fixed. The `confirm-main` failure, on the terminal and on the tracking
issue, lists the recovery steps with the run IDs filled in:

1. `gh run rerun <ci-run> --repo <repo>`: re-run the release PR's CI
   run. The release job's evidence gate harvests that run's artifacts,
   so a fix that changes what CI writes reaches the gate only through a
   fresh CI attempt.
2. `gh run watch <ci-run> --repo <repo> --exit-status`.
3. `gh run rerun <cd-run> --repo <repo>`: re-run the failed CD run on
   `main`.
4. `gh run watch <cd-run> --repo <repo> --exit-status`.
5. `vrg-release --resume <version>`.

Run them once the fix for the cause is released. Stop at any watch that
fails. The resume judges the CD run's new attempt through the same
release-job gate.

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

An open back-merge PR from the earlier run is adopted and merged, and a
merged one is skipped. A PR closed without merging is not adopted: the
resume opens a new PR from the reused branch.

Within a run, `prepare`, `back-merge-bump` and the dependency update never
retry a failed `gh pr create` blindly, since GitHub can open the PR and
still return an error. After a failed attempt the stage looks for an open
PR from its branch into the base. It adopts one only when it has the same
head branch (in this repository), base, title and head commit, and carries
on as for a PR it just opened; an adopted dependency-update PR gets its
`dependency-update` label again. Any other open PR fails the stage, naming
the PR. With none open, a transient error is retried with backoff.

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
| Developer host | Manual `uv tool upgrade vergil-tooling` (`vrg-host` reports the installed version but never upgrades it) |
| Python repo `.venv` | `uv lock --upgrade-package vergil-tooling` (typically batched with other dep bumps) |
| Dev container image | The base images carry no vergil-tooling. The next `vrg-container-run` rebuilds the branch's cached image with the patch once it is in the package index: a `vX.Y` line pin in `vergil.toml` resolves to the newest release on the line (see [How the cache tracks vergil-tooling releases](../reference/cli-tools-overview.md#how-the-cache-tracks-vergil-tooling-releases)). An exact `vX.Y.Z` pin stays put |
| Agent VM (Lima/cloud) | `vrg-vm update`: an apt upgrade of the packaged install within its pin, once the patch is in the package index. A `vX.Y` line pin picks the patch up; an exact `vX.Y.Z` pin stays put |

## Minor release — opt-in for new features

A minor release adds backwards-compatible new features. Versions:
`v1.3.x` → `v1.4.0`.

### Producer steps

Same steps as patch, with one important addition: the rolling
minor tag changes from `v1.3` to `v1.4`. Any per-repo dev-dep
declarations **must be bumped manually** — the rolling-tag mechanism
only handles patches within a minor.

1. Run `vrg-release minor`. It bumps the version to the next minor
   (`1.4.0`) on the release branch instead of releasing the current
   `develop` version. Everything else matches a patch release.
2. **Coordinate consumer bumps.** Each Python repo that has
   `[tool.uv.sources]` pinning to `v1.3` needs its `pyproject.toml`
   updated to `v1.4`. This is a per-repo decision — consumers who
   prefer to stay on `v1.3` can keep doing so until they're ready.
3. Update the canonical install command in `getting-started.md`
   (and anywhere else the documentation pins a minor) to the new
   tag.

### Consumer steps

Minor bumps are **deliberate opt-in** at every deployment target:

| Consumer | What changes |
|---|---|
| Developer host | `uv tool install --python 3.15 --reinstall 'vergil-tooling @ git+...@v1.4'` (or update the pinned tag in their notes / shell history) |
| Python repo `.venv` | Edit `pyproject.toml` `[tool.uv.sources]` to `tag = "v1.4"`, then `uv lock --upgrade-package vergil-tooling` |
| Dev container image | Set `[dependencies].vergil` in `vergil.toml` to `v1.4`; `vergil.toml` is part of the cache key, so the next `vrg-container-run` rebuilds the branch's cached image with the newest `v1.4` release |
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
only the repository Admin role move a rolling tag (see the
[config audit reference](../reference/config-audit.md#github-checks-rulesets)).
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
forward again. Keeping it in place until the fix ships is managed by hand:
the human who rolled back makes sure the next release carries the fix, or
cuts it with `vrg-release --no-promote`. A release cut with `--no-promote`
leaves `v2.1` where it is; promote it later with the `vrg-promote` steps in
[Move the tag back](#move-the-tag-back), passing its version. Rolling
forward again is the same steps with the latest `v2.1.Z`. Nothing holds the
tag automatically. An issue that blocks promotion until it is closed is a
separate idea,
[vergil-project/.github#763](https://github.com/vergil-project/.github/issues/763).

Two cases undo a rollback with no prompt, because the `promote` stage asks
nothing:

- **A release already in flight.** A vergil-claude-plugin release that is
  still running moves `v2.1` forward when it reaches `promote`. So does any
  open `release: <version>` tracking issue, the release you are rolling back
  included, even with its `promote` stage ticked: `vrg-release --resume`
  runs every stage again, `promote` with it. Before you roll back, let a
  running release finish, and close every open tracking issue or resume it
  only with `--no-promote`.
- **A resume without `--no-promote`.** `vrg-release` does not remember
  `--no-promote`: each run, `--resume` included, promotes unless that run
  passes the flag. Pass `--no-promote` to every `vrg-release --resume` of a
  release you are holding back.

## Dependency updates

Every release ends by updating the repo's dependencies to the newest
versions their declared bounds allow. This is the `update-deps` stage,
and it is the first change on the new `develop` version. It runs after
`consumer-refresh`, once the release is tagged and published, so it can
never affect the release itself. Pass `--without-deps` to skip it.

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

If preflight cannot fetch `main` or compare the branches, it refuses
rather than skipping.

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
  it finishes.
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
wait (issue #3316).

For deeper investigation, view recent runs directly:

```bash
gh run list --repo vergil-project/vergil-tooling \
  --workflow="Documentation" --limit 5
```

## Release tracking artifacts

Each release has a tracking issue, `release: <version>`, which
`vrg-release` creates in its `prepare` stage. The issue is created with
the stage checklist in its body (see
[An interrupted release](#an-interrupted-release)), and each tracked
stage adds a comment when it
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
