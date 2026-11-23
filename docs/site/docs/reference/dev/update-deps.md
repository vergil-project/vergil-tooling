# vrg-update-deps

**Installed as:** `vrg-update-deps` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_update_deps.py` (pipeline in
`src/vergil_tooling/lib/update_deps/`)

Updates a repo's dependencies to the newest versions their declared
bounds allow, validates once, and drives the result through a PR to
merge. Every `vrg-release` already runs the same pipeline as its
`update-deps` stage (see
[Dependency updates](../../guides/releasing.md#dependency-updates)), so
run this command by hand only when you want an update outside a
release, or to move the repo to a new vergil series with `--vergil`.

`vrg-update-deps` merges its own PR, so it is human-only: it refuses to
run under an agent identity.

## Usage

```bash
vrg-update-deps [--only NAME[,NAME] | --skip NAME[,NAME] | --vergil X.Y]
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `--only NAME[,NAME]` | No | Run only these updaters |
| `--skip NAME[,NAME]` | No | Run every applicable updater except these |
| `--vergil X.Y` | No | Move the repo to vergil series `X.Y`. Runs only the `vergil` updater, in bump mode |
| `--output-window N` | No | Rolling output window for the terminal renderer (`0` streams everything) |
| `--output-format` | No | Force the renderer: `rich`, `gha` or `plain` |

`--only`, `--skip` and `--vergil` are mutually exclusive. The updater
names are `python`, `cargo`, `npm`, `vergil`, `images` and `hook`; an unknown
name is an error. The updaters are described in the
[dependency-update reference](../dependency-update-config.md).

## The Cargo updater

In a repo with `Cargo.toml` and `Cargo.lock`, the `cargo` updater runs
`cargo update` in the dev container. It moves every locked crate to the
newest version its `Cargo.toml` requirement allows and never edits a
`Cargo.toml`. Its commit and the PR body name each crate it moved, from
and to. When `cargo update` rewrites `Cargo.lock` without moving any
version, the commit is `chore(deps): refresh Cargo.lock`.

It then reports, in the held-back report, every direct dependency of
every workspace member (normal, dev and build) whose locked version is
below the newest version on crates.io that is neither yanked nor a
pre-release. Each entry's note gives the reason:

| Note | Meaning |
| ---- | ------- |
| `outside the Cargo.toml requirement` | The newest version does not satisfy the dependency's requirement |
| `needs a newer Rust than rust-version` | The newest version's `rust_version` is above the package's `rust-version` |
| Both notes, joined by a semicolon | Both of the above |
| `held by the resolver: another requirement in the dependency graph` | Neither: another crate's requirement on the same dependency holds it |

The newest versions come from Cargo's sparse index
(`https://index.crates.io`), the data `cargo update` itself reads, not
from the crates.io web API. A workspace-inherited dependency
(`foo.workspace = true`) is checked against the workspace's
requirement. Path and git dependencies are not reported, since they have
no index entry, and neither is a crates.io dependency that
`[patch.crates-io]` or `[replace]` points at a path or git crate. A
dependency from a registry other than crates.io, or patched to one, fails
the updater rather than go unchecked.

A failed `cargo update` or `cargo metadata`, an unreadable `Cargo.toml`
or `Cargo.lock`, and a failed sparse-index read each fail the update as
an `update` failure. None of them reads as "no change" or as an empty
held-back report.

## The npm updater

In a repo with `package.json` and `package-lock.json`, the `npm` updater
runs `npm update --ignore-scripts` in the dev container. It moves every
locked package to the newest version its `package.json` range allows,
and no dependency's install script runs. It changes only
`package-lock.json`: if `package.json` differs by a single byte
afterwards, as a `save` setting in the repo's `.npmrc` would make it, the
updater fails, naming it. Its commit and the PR body name each package it
moved, from and to, nested copies included. When `npm update` rewrites
`package-lock.json` without moving any version, the commit is
`chore(deps): refresh package-lock.json`.

Every lockfile entry the update added or moved must carry a `license`
field, which the license gate reads. One that does not fails the
updater, naming the package and version, so the cause surfaces in the
update rather than later on its PR.

It then runs `npm ci --ignore-scripts` and `npm outdated --json`, both
in the dev container, so npm itself matches every range. Each direct
dependency in `dependencies`, `devDependencies` or
`optionalDependencies` whose locked version is below the registry's
`latest` dist-tag is reported in the held-back report. Each entry's note
gives the reason:

| Note | Meaning |
| ---- | ------- |
| `outside the package.json range` | The newest version the range allows (`wanted`) is below `latest` |
| `held by the resolver: another requirement in the dependency graph` | The locked version is below `wanted`: a peer requirement or an `overrides` entry, for example, holds it |
| Both notes, joined by a semicolon | Both of the above |

For a package Vergil pins to one major in
`configs/typescript/dev-dependencies.json`, the note adds, for example,
`Vergil accepts typescript 6 only (dev-dependencies.json)`, so a reader
sees the hold is Vergil's. A locked version at or above `latest` (a
prerelease ahead of the dist-tag, say) is not reported. When
`npm outdated` gives no current version (an optional dependency `npm ci`
skipped on this platform), the version comes from the lockfile's
`node_modules/<name>` entry. Dependencies declared as `file:`, `link:`,
git, GitHub or URL specs are not reported, since they have no registry
release, and neither are peer dependencies.

`npm outdated` exits 1 whenever something is outdated, so the updater
reads its JSON rather than its exit status: exit 0 with nothing outdated
and exit 1 with outdated packages are the normal cases. A failed
`npm update` or `npm ci`, an unreadable `package.json` or
`package-lock.json`, a lockfile older than v2, a changed `package.json`,
an added or moved entry with no `license`, and an `npm outdated` run that
prints no JSON, malformed JSON or an `error` object, or exits otherwise,
each fail the update as an `update` failure. None of them reads as "no
change" or as an empty held-back report.

## Prerequisites

- Run from the root checkout, on `develop`, with a clean working tree
  that matches `origin/develop`.
- An authenticated `gh`.
- No dependency update for the current `develop` version may be in
  flight. See [An existing update branch](#an-existing-update-branch).

## An existing update branch

If the `deps/update-<version>` branch for the current `develop` version
already exists locally or on origin, `preflight` looks up the branch's
PRs and acts on the newest one:

| Branch state | What `preflight` does |
| ------------ | --------------------- |
| The PR merged | The update for this version is already done. It prints the PR's URL and exits 0 without updating anything |
| The PR was closed without merging | The branch is stale. It deletes the branch on origin and locally, naming it, then runs the update as usual |
| A PR is open | Refuses: the update is still in flight. Land or close the PR first |
| The branch has no PR | Refuses: the update is still in flight |

`preflight` also refuses, without deleting anything, when the branch's
tip is not the head of its newest PR. That means the branch name was
reused after the PR finished, so the branch holds commits that no PR
covers. If a stale branch cannot be deleted, `preflight` refuses and
names the branch to delete by hand.

After it merges its own PR, the `merge` stage deletes the
`deps/update-<version>` branch on origin. GitHub's delete-on-merge
setting usually deletes it first, and the stage then reports that it is
already gone. If the delete fails, the stage prints a warning with the
command to run by hand. The run still succeeds, because the update has
merged.

## Stages

| Stage | What it does |
| ----- | ------------ |
| `preflight` | Checks the prerequisites and creates the `deps/update-<version>` managed worktree |
| `update` | Runs each applicable updater in order (`python`, `cargo`, `npm`, `vergil`, `images`, then `hook`), committing each one's changes separately |
| `validate` | Runs `vrg-container-run -- vrg-validate` once, if anything changed |
| `prepare-pr` | Pushes the branch and opens a PR to `develop` labelled `dependency-update`, creating the label if the repo lacks it |
| `merge` | Waits for the PR's checks and merges it, if validation passed, then deletes its branch on origin (see [Waiting for green](release.md#waiting-for-green)) |
| `fix-task` | After any failure, files the fix task; after a validation or merge failure, also links it from the PR body |
| `finalize` | Prints the held-back report and removes the worktree and local branch |
| `status` | Exits non-zero if a failure was recorded |

When no updater changes anything, no branch is pushed and no PR is
opened.

## Failures

- **`update`**: an updater or the hook failed. Nothing is pushed and no
  PR is opened, but a "Fix failed dependency update for `<version>`"
  task is filed under the repo's ad-hoc epic. It names the failing
  updater and carries the error and the command to reproduce it on
  `develop`. With no labelled PR, it does not gate the next
  `vrg-release`.
- **`validation`** or **`merge`**: the PR stays open with its
  `dependency-update` label, a "Fix failed dependency update for
  `<version>`" task is filed under the repo's ad-hoc epic, and the PR
  body gets a `Fix task: <url>` line and a failure section. While that
  PR is open, the next `vrg-release` refuses to start; see
  [The dependency-update gate](../../guides/releasing.md#the-dependency-update-gate).

A re-run at the same `develop` version that fails the same way (the
same failure kind) does not file a second task. The new failure is
added as a comment on the open task, and that task is the one linked
from the PR body and the run summary. Only an open task whose body is
trusted is reused: a look-alike task opened or edited by an identity
with no trusted role is reported on stderr and never adopted. If the
lookup for an open task fails, the run fails rather than risk filing a
duplicate.

The fix flow is in
[When the dependency update fails](../../guides/releasing.md#when-the-dependency-update-fails).

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Updated and merged, nothing to update, or the update for this `develop` version had already merged |
| 1 | A failure was recorded, a precondition failed, or the command was refused |
| 130 | Interrupted (Ctrl-C) |
