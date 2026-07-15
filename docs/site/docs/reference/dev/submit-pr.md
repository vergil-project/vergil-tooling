# vrg-submit-pr

**Installed as:** `vrg-submit-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_submit_pr.py`

Wrapper that creates standards-compliant pull requests with
proper issue linkage. Has three modes:

- **Template mode** (no arguments) — the normal path. Reads
  `.vergil/pr-workflow.json` (the oracle state file the agent records via
  `vrg-pr-workflow`), shows a preview, asks for confirmation, pushes the
  branch, creates the PR, and emits the `/vergil:pr-watch` handoff line.
- **Relay branch mode** (positional `<branch> [<branch> …]`) — the Mac
  side of the cloud→Mac relay handoff (issue #2368). Opens PRs for
  branches that are **already on origin, worktree-free**: each branch's
  ready-state is resolved from a local worktree's `pr-workflow.json` when
  one exists, else fetched from the relay ref
  `refs/vergil/pr-workflow/<branch>` that `report-ready` always pushes.
  Origin's tip is verified against the recorded `head_sha` and the PR is
  opened **without pushing** (`--head` names the source branch). No
  worktree, no `git.current_branch()`, and no push are involved — the
  branch and its metadata already rode GitHub.
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

# Relay branch mode (cloud handoff): worktree-free, from the main worktree
vrg-submit-pr <branch> [<branch> …]

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

## Relay branch mode (cloud-to-Mac handoff)

Passing one or more branch names opens PRs **worktree-free** for work an
off-platform (cloud x86) agent implemented and pushed. It is the Mac side
of the GitHub relay: the cloud VM and the Mac never share a disk, so the
agent's `report-ready` mirrors its ready-state onto the reserved ref
`refs/vergil/pr-workflow/<branch>` in addition to the local
`.vergil/pr-workflow.json`. Run from the **main worktree**:

```bash
vrg-submit-pr feature/123-x feature/124-y
```

For each branch, the tool:

1. resolves the ready-state from a local worktree's `pr-workflow.json`
   when one exists, else fetches it from the relay ref;
2. verifies the tip of `origin/<branch>` matches the recorded `head_sha`
   (a mismatch fails loudly — the metadata is for a different commit);
3. opens the PR with `--head <branch>` and **does not push** — the branch
   is already on origin.

!!! warning "The relay ref is world-readable"
    On a public repo, anyone can read `refs/vergil/pr-workflow/<branch>`.
    Keep secrets out of the `report-ready` `--title`, `--summary`, and
    `--notes` — they are public the moment they are recorded.

The relay ref is cleaned up by [`vrg-finalize-pr`](finalize-pr.md), which
deletes it alongside the branch on merge and sweeps any orphaned relay ref
whose branch no longer exists.

### Cascade on the relay path (`--finalize`/`--release`/`--install`)

The relay path runs the full cascade too (issue #2398): the cascade is a
**local macOS** action, so it does not matter whether the branch was
implemented on the Mac or on a cloud VM. Once the PR is open, finalizing,
releasing, and installing operate on the PR and the remote — the branch does
not need to be checked out locally (`vrg-finalize-pr` already merges and
cleans up a branch with no local worktree).

```bash
# Open PRs for both cloud-implemented branches, then merge, release, and
# install — all locally, in one command.
vrg-submit-pr --install feature/123-x feature/124-y
```

It uses the same batch semantics as the worktree batch (`--all` / `--select`):
each PR is finalized in turn, then — only if every branch merged — a single
end-of-batch validation runs, followed by **one** `vrg-release` (never one per
branch). A branch that already carries a submitted PR is fail-fast in the
cascade (finalize it directly with `vrg-finalize-pr <url>`); without a cascade
flag it is simply reported and skipped, as before.

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

# Relay branch mode: open PRs for cloud-implemented branches, worktree-free
vrg-submit-pr feature/123-x feature/124-y

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
