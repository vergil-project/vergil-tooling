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
names are `python`, `vergil`, `images` and `hook`; an unknown name is an
error. The updaters are described in the
[dependency-update reference](../dependency-update-config.md).

## Prerequisites

- Run from the root checkout, on `develop`, with a clean working tree
  that matches `origin/develop`.
- An authenticated `gh`.
- The `deps/update-<version>` branch for the current `develop` version
  must not exist locally or on origin. If it does, an earlier update for
  that version is still in flight; land or close its PR and delete the
  branch first.

## Stages

| Stage | What it does |
| ----- | ------------ |
| `preflight` | Checks the prerequisites and creates the `deps/update-<version>` managed worktree |
| `update` | Runs each applicable updater in order (`python`, `vergil`, `images`, then `hook`), committing each one's changes separately |
| `validate` | Runs `vrg-container-run -- vrg-validate` once, if anything changed |
| `prepare-pr` | Pushes the branch and opens a PR to `develop` labelled `dependency-update`, creating the label if the repo lacks it |
| `merge` | Waits for the PR's checks and merges it, if validation passed |
| `fix-task` | After a validation or merge failure, files the fix task and links it from the PR body |
| `finalize` | Prints the held-back report and removes the worktree and local branch |
| `status` | Exits non-zero if a failure was recorded |

When no updater changes anything, no branch is pushed and no PR is
opened.

## Failures

- **`update`**: an updater or the hook failed. Nothing is pushed, no PR
  is opened and no fix task is filed.
- **`validation`** or **`merge`**: the PR stays open with its
  `dependency-update` label, a "Fix failed dependency update for
  `<version>`" task is filed under the repo's ad-hoc epic, and the PR
  body gets a `Fix task: <url>` line and a failure section. While that
  PR is open, the next `vrg-release` refuses to start; see
  [The dependency-update gate](../../guides/releasing.md#the-dependency-update-gate).

The fix flow is in
[When the dependency update fails](../../guides/releasing.md#when-the-dependency-update-fails).

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Updated and merged, or nothing to update |
| 1 | A failure was recorded, a precondition failed, or the command was refused |
| 130 | Interrupted (Ctrl-C) |
