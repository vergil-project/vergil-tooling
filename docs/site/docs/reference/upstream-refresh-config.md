# Upstream Refresh Reference (`[upstream-refresh]`)

`vrg-upstream-refresh` keeps a repo's upstream inputs current. The repo
supplies the logic in a hook, and the command supplies the mechanics: it
runs the hook, then opens, refreshes or closes one PR. A human reviews
and merges that PR; nothing auto-merges.

The design is epic
[vergil-project/.github#472](https://github.com/vergil-project/.github/issues/472)
(spec `epics/472-multi-series-runtime/spec.md`, section 5.6).

## How it differs from a dependency update

An upstream refresh and a [dependency update](dependency-update-config.md)
are separate pipelines with separate hooks:

| | Upstream refresh | Dependency update |
|---|---|---|
| Command | `vrg-upstream-refresh` | `vrg-update-deps`, and the `update-deps` stage of `vrg-release` |
| Config | `[upstream-refresh].hook` | `[dependency-update].hook` |
| What changes | Inputs the repo pins from upstream, such as a runtime build or a tool version | Dependencies within their declared bounds |
| Branch | `upstream-refresh` (one, fixed) | `deps/update-<version>` |
| Validation | None; the PR's own CI checks it | One `vrg-validate` run before the PR |
| Merge | Never; a human merges | Merged when green |
| Who runs it | A scheduled ops workflow, or a human | A human |

Neither command runs the other's hook. `vrg-upstream-refresh` never runs
dependency updaters, validation or a release.

## `[upstream-refresh]`

```toml
[upstream-refresh]
hook = "scripts/dev/upstream-refresh.sh"
```

| Key | Type | Default | Description |
|---|---|---|---|
| `hook` | string | *(required)* | Command that refreshes the repo's upstream inputs. Must be non-empty |

The table is optional, but `vrg-upstream-refresh` refuses to run without
it (`vergil.toml has no [upstream-refresh] section`). When the table is
present, `hook` is required, and any other key is an error.

## The hook contract

The command runs the hook as `bash -c "<hook>"` from the repo root, on a
clean checkout of the base branch. The hook:

- **Edits tracked files only.** It must not commit, and it must not
  leave new untracked files. If it does, the command fails and names
  them (`hook left untracked files: <paths>`), and nothing is pushed.
  Files the repo's `.gitignore` ignores are allowed and never committed.
- **May write notes** to the file named in `$VRG_UPSTREAM_REPORT`, one
  note per line. Each non-empty line becomes a bullet under `## Notes`
  in the PR body. The format of each line is the repo's choice.
- **Fails with a non-zero exit.** The command then stops with
  `[upstream-refresh].hook failed (exit <n>): <hook>`, and nothing is
  pushed.

## What the command does

`vrg-upstream-refresh` first fetches `origin/<base>` and requires a
clean working tree with `HEAD` at `origin/<base>`. It then runs the hook
and asks git what changed. Every run ends in one of these outcomes:

| Outcome | When | What happens |
|---|---|---|
| `nothing` | No change and no open refresh PR | Exits quietly; no GitHub writes |
| `closed` | No change, but a refresh PR is open | Closes it with `Nothing to refresh on <base>; closing.` and deletes its branch |
| `opened` | A change and no open refresh PR | Pushes one commit to `upstream-refresh` and opens the PR |
| `refreshed` | A change that differs from the open PR | Force-pushes the new commit, updates the PR body and adds one comment |
| `unchanged` | A change identical to the open PR's tree | Pushes nothing and comments nothing, so the PR does not churn |

The refresh commit is `chore(upstream): refresh upstream inputs`, built
directly on top of `origin/<base>`. Every run recomputes it from
scratch, so the PR always holds a full snapshot rather than an
increment. Its title is the commit subject, and its body lists the
changed files and the hook's notes. The commit is built with git
plumbing, so no local branch moves and no commit hook runs. Pushes use
`--force-with-lease`, so a branch someone else moved is never
overwritten.

A refresh PR is an open PR against the base whose head is
`upstream-refresh` or `upstream-refresh-<anything>` (a manual
re-create). When more than one is open, the oldest is kept and
refreshed. The others are closed with a comment naming it, and their
branches are deleted.

The command works from a shallow, depth-1 checkout, such as the default
`actions/checkout`. It fetches the refs it needs itself, at depth 1 when
the checkout is shallow, so it never deepens the clone.

## CLI

```text
vrg-upstream-refresh [--repo OWNER/NAME] [--base BRANCH] [--dry-run]
```

| Option | Default | Description |
|---|---|---|
| `--repo` | `$GITHUB_REPOSITORY`, then the `origin` remote | Repository that owns the refresh PR |
| `--base` | The repository's default branch | Base branch to refresh against |
| `--dry-run` | off | Run the hook and print the PR body; push nothing and change no PR |

It exits 0 on every outcome above, and 1 on any error or refusal.

## Identity

The command opens PRs, so agent identity modes (`user` and `audit`)
are refused with `vrg-upstream-refresh opens PRs and is not run by
agents`. It runs from a scheduled ops workflow under the per-org
automation App token, or by hand as a human. It needs contents and
pull-requests write on its own repo.

## The scheduled workflow

The reusable vergil-actions workflow `ops-upstream-refresh.yml` runs the
command on a schedule. It mints a repo-scoped App token, checks out with
it so pushes are App-authored and trigger CI, and runs
`vrg-upstream-refresh --base <ref>` in the `prod-base` image. Each
consuming repo calls it from its own `ops.yml` on its own schedule. A
failed run shows as a red scheduled run.
