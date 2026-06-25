# vrg-submit-pr

**Installed as:** `vrg-submit-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_submit_pr.py`

Wrapper that creates standards-compliant pull requests with
proper issue linkage. Has two modes:

- **Template mode** (no arguments) — the normal path. Reads
  `.vergil/pr-workflow.json` (the oracle state file the agent records via
  `vrg-pr-workflow`), shows a preview, asks for confirmation, pushes the
  branch, creates the PR, and emits the `/vergil:pr-watch` handoff line.
- **CLI mode** (`--issue/--summary/--title`) — direct invocation for
  human emergency use.

!!! warning "Human-run tool"
    PR submission is a human action. Agent identities are blocked;
    agents hand off via `.vergil/pr-workflow.json` instead.

## Prerequisites

When running inside a dev container, `GH_TOKEN` must be set so `gh` can
authenticate. The
[Getting Started prerequisites](../../getting-started.md#prerequisites)
cover `gh auth login` and how `GH_TOKEN` flows through to the
container.

## Usage

```bash
# Template mode (normal): from the repo root or inside the worktree
vrg-submit-pr

# CLI mode (emergency): from inside the worktree
vrg-submit-pr --issue NUMBER --summary TEXT --title TEXT [options]
```

## Running from the repo root

In template mode, `vrg-submit-pr` may be run from the repo root — it
resolves the target worktree itself:

- **One submittable worktree** (contains `.vergil/pr-workflow.json`):
  announced and entered automatically; the usual preview + `[y/N]`
  confirmation follows.
- **Several submittable worktrees:** a numbered menu shows each
  worktree with its issue number and title; pick one.
- **None:** an error lists each worktree and why it was skipped.

Run from inside a worktree, behavior is unchanged. The tool is
interactive by design — it is a human touch point of the workflow and
requires a terminal; root launches fail fast when stdin is not a TTY.

## Arguments

| Argument | Required (CLI mode) | Description |
| -------- | ------------------- | ----------- |
| `--issue` | Yes | Issue number or cross-repo ref |
| `--summary` | Yes | One-line PR summary |
| `--title` | Yes | PR title |
| `--linkage` | No | Linkage keyword (default: `Ref`) |
| `--notes` | No | Additional notes for the PR |
| `--base` | No | Override the auto-detected target branch |
| `--dry-run` | No | Print PR body without executing |
| `--finalize` | No | After creating the PR, chain straight into `vrg-finalize-pr` |

### Linkage Keywords

`Ref`

## Examples

```bash
# Normal flow: resolve the worktree, preview, confirm
vrg-submit-pr

# Preview without submitting
vrg-submit-pr --dry-run

# Emergency CLI mode
vrg-submit-pr \
  --issue 42 \
  --summary "Add new lint check for X" \
  --title "feat(lint): add new check for X"

# Submit and merge-on-green in one step
vrg-submit-pr --finalize
```

## Behavior

1. Template mode from the repo root: resolves the target worktree
   (see above) and moves into it — the invoking shell is unaffected.
2. Reads `.vergil/pr-workflow.json` (template mode) or validates CLI
   arguments, including the issue reference format.
3. Detects target branch from the current branch:
    - `release/*` branches target `main`
    - All other branches target `develop`
4. Shows the PR preview and asks `Submit this PR? [y/N]`
   (template mode).
5. Pushes the branch to origin with the human's host credentials.
6. Creates the PR via `gh pr create` and deletes the template.
7. Prints the PR URL — which can be passed straight to
   [`vrg-finalize-pr`](finalize-pr.md).

## Chaining into finalize (`--finalize`)

`--finalize` folds the manual two-command sequence
(`vrg-submit-pr`, then `vrg-finalize-pr <pr>`) into one step: after
the PR is created, the tool hands off to the
[`vrg-finalize-pr`](finalize-pr.md) wait-and-merge flow from the main
worktree root. Use it when the decision to merge-on-green has already
been made at submit time.

Semantics are identical to running `vrg-finalize-pr <pr-url>` by hand
(same merge-strategy default, same post-merge cleanup), and the
failure modes split cleanly:

- A **submit failure** stops before finalize runs — no half-finalized
  state.
- A **finalize failure** leaves the created PR unaffected and prints
  it clearly so you can re-run `vrg-finalize-pr <pr-url>` alone.

Like the rest of the tool, `--finalize` is human-only — the agent
identity gate runs before either mode.

## Batch mode (`--all` / `--select`)

Selecting two or more ready worktrees submits them as a single
serialized batch (issue #1673). Use `--all` for every ready worktree,
or `--select <tokens>` for a comma-separated subset matched by issue
number or worktree directory name (e.g. `--select 1673,1681`); an
unmatched or ambiguous token is a hard error that names it. A single
selection runs the unchanged single-PR path.

The batch is optimized so each expensive CI gate runs **exactly once**.
For each worktree in turn it:

1. **rebases the branch onto the latest `develop`** — the step that
   guarantees the gate runs against the final state, so a later merge
   is never `BEHIND` and never re-runs CI;
2. submits the PR;
3. with `--finalize`/`--release`/`--install`, finalizes it
   (`vrg-finalize-pr <url> --skip-post-checks` — merge and cleanup,
   deferring validation).

After every item merges, post-merge validation and the CD check run
**once**, then — with `--release`/`--install` — a single `vrg-release`
ships all the changes in one version bump.

The batch asks **one** confirmation up front (skipped with `--yes`),
then runs unattended. It is **fail-fast**: the first failure (rebase
conflict, red gate, merge conflict, provenance violation) stops the
batch and prints a `merged` / `failed` / `not started` summary.
Already-merged PRs stay merged; re-running picks up only the remaining
ready worktrees.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | PR created (and, with `--finalize`, merged and cleaned up) |
| 1 | Validation failure, declined confirmation, or no submittable worktree |
| other | With `--finalize`: the PR was created but finalize failed — re-run `vrg-finalize-pr` alone |
