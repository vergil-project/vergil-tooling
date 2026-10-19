# vrg-release

**Installed as:** `vrg-release` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_release.py` (stages in
`src/vergil_tooling/lib/release/orchestrator.py`)

Runs the whole release workflow from `develop` to `main`: preflight,
tracking issue, changelog, release PR, merge, CD confirmation,
back-merge and version bump, rolling-tag promotion, cleanup, consumer
refresh, and the post-release dependency update. The stages and what
they leave behind are described in the
[Releasing guide](../../guides/releasing.md#the-vrg-release-pipeline).

Releasing is a human maintainer action. `vrg-release` refuses to run
under an agent identity.

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
| `--install` | No | After a clean release, run the `[publish].consumer-refresh` commands instead of only printing them. Fails if none are configured |
| `--resume [X.Y.Z]` | No | Resume an interrupted release from its open tracking issue. Name the version when more than one release is in flight. Cannot be combined with `minor`/`major` |
| `--skip-audit` | No | Skip the `audit` stage |
| `--output-window N` | No | Rolling output window for the terminal renderer (`0` streams everything) |
| `--output-format` | No | Force the renderer: `rich`, `gha` or `plain` |

## Prerequisites

- Run from the root checkout, on `develop`, with a clean working tree
  that matches `origin/develop`.
- `git-cliff` on `PATH` and an authenticated `gh`.

## The dependency-update gate

On a fresh start (not on `--resume`), preflight refuses while an open
PR carries the `dependency-update` label. First it closes, with a
comment, any such PR whose linked fix task was closed as completed (the
update landed through the fix PR), and deletes its head branch. A failed
branch delete is reported, not fatal. `--allow-pending-deps` lets the
release past whatever still blocks. Lookup failures refuse the release.
See [The dependency-update gate](../../guides/releasing.md#the-dependency-update-gate).

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | Every stage succeeded |
| 1 | A stage failed, including a deferred failure such as a missed publish job or a failed dependency update; or the command was refused |
| 130 | Interrupted (Ctrl-C) |
