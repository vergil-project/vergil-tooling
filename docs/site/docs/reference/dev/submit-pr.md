# vrg-submit-pr

**Installed as:** `vrg-submit-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_submit_pr.py`

Opens standards-compliant pull requests against `develop` with GitHub
auto-merge on, then, by default, runs one
[`vrg-finalize-pr`](finalize-pr.md) over them, which watches them merge
and settles `develop` once (epic vergil-project/.github#1218). Submitting
a PR is the human's decision to merge it: GitHub merges it once its
required checks pass. Has three modes:

- **Template mode** (no arguments): the normal path. Reads
  `.vergil/pr-workflow.json` (the oracle state file the agent records
  with `vrg-pr-workflow report-ready`), shows a preview, asks for
  confirmation, pushes the branch and opens the PR. From the repo root,
  `--all` and `--select` submit several ready worktrees at once.
- **Relay branch mode** (positional `<branch> [<branch> …]`): the Mac
  side of the cloud→Mac relay handoff (issue #2368). Opens PRs for
  branches that are **already on origin, worktree-free**: each branch's
  ready-state is resolved from a local worktree's `pr-workflow.json` when
  one exists, else fetched from the relay ref
  `refs/vergil/pr-workflow/<branch>` that `report-ready` always pushes.
- **CLI mode** (`--issue/--summary/--title`): direct invocation for
  human emergency use. It asks no confirmation and stays single-issue.

!!! warning "Human-run tool"
    PR submission is a human action. Agent identities are refused;
    agents hand off with `vrg-pr-workflow report-ready` instead.

## Prerequisites

When running inside a dev container, `GH_TOKEN` must be set so `gh` can
authenticate. The
[Getting Started prerequisites](../../getting-started.md#prerequisites)
cover `gh auth login` and how `GH_TOKEN` flows through to the
container.

## Usage

```bash
# Template mode (normal): from the repo root or inside the worktree
vrg-submit-pr [--all | --select TOKENS] [options]

# Relay branch mode (cloud handoff): worktree-free, from the main worktree
vrg-submit-pr <branch> [<branch> …] [options]

# CLI mode (emergency): from inside the worktree
vrg-submit-pr --issue NUMBER --summary TEXT --title TEXT [options]
```

## The one submit path

Every mode gates and previews its PRs, asks for **one** confirmation for
the whole selection (`--yes` skips it; CLI mode asks none), then opens
each PR in turn:

1. For a batch PR, re-check its rules (see
   [Batch PRs](#batch-prs-one-pr-closing-several-issues)).
2. Push the branch, in the modes that push.
3. Hold origin's tip to the head the PR must open at, then open the PR
   with `gh pr create` and record the submission. `gh pr create` is
   never retried blindly, since GitHub can open the PR and still return
   an error. After a failed attempt submit looks for an open PR from the
   branch into the base. It adopts one only when it has the same head
   branch (in this repository), base, title and head commit, and carries
   on as for a PR it just opened. Any other open PR fails the submit,
   naming the PR. With none open, a transient error is retried with
   backoff and any other error fails the submit.
4. Run the **provenance check**. A violation leaves the PR's auto-merge
   off and counts as a failed submit, unless
   `--allow-provenance-violation` is given. Advisories are printed but
   do not block. A check that cannot run is a failed submit too.
5. Turn **auto-merge** on, pinned to that head (`expectedHeadOid`): the
   **squash** method for a single-issue PR, **rebase** for a batch PR.
   When GitHub refuses because the PR can already merge, submit merges
   it directly with the same method and head, and says so. When either
   write returns an error, submit reads the PR back first: auto-merge on
   with that method, or the PR merged at that head, means the write
   landed (a retry after a lost response), and counts as done.

Submit **never rebases a branch**: rebasing before handoff is the
agent's job, and `develop` does not require a branch to be up to date,
so ready PRs merge in parallel. A PR whose submit fails is named, with
its reason, in a summary printed at the end, and the other PRs go on:
nothing is fail-fast, since one PR no longer waits on another.

PRs are opened against **`develop` only**. A `--base`, or a base
recorded by `report-ready`, naming any other branch is refused, since
releases go through `vrg-release`.

Every mode driven by a `report-ready` state (template, batch and relay)
opens the PR only at the `head_sha` the latest `report-ready` recorded,
and refuses rather than pick another head (issue #3815): a local
worktree's checked-out HEAD must be that commit, a branch reopened by
`vrg-pr-workflow unfreeze` and not re-reported is refused, a local state
and relay ref that disagree are refused, and origin's tip is checked
against the recorded head again right before `gh pr create`. Auto-merge
is pinned to that same head.

## Finalize by default

Once every PR in the selection has been tried, submit runs **one**
`vrg-finalize-pr <pr>,<pr>,…` over the PRs whose submit succeeded, from
the main worktree. It watches them merge, then syncs, cleans up and
validates `develop` once (see [vrg-finalize-pr](finalize-pr.md)). A PR
whose submit failed is left out of that list and named after finalize's
report. When no PR opened, finalize is not run at all.
`$VRG_FINALIZE_DEADLINE` is checked before any PR opens, so a malformed
value refuses the run with nothing done.

- **`--release`** is passed to finalize, which chains into `vrg-release`
  only on full success. It is dropped when any PR failed to submit, and
  the summary says the release was skipped. `vrg-release` installs by
  default; `--no-install` (which requires `--release`) passes through to
  skip the install. `--release` is refused on a repo that is not
  released, before any push, PR or merge.
- **`--allow-provenance-violation`** is passed to finalize too. There it
  covers only an open PR's auto-merge: a PR that merged with a violation
  still blocks full success, so no release runs.
- **`--no-finalize`** opens the PRs with auto-merge on and returns
  without watching them. It prints each PR's URL, the
  `/vergil:pr-watch <PR_URL>` line that starts the agent's monitoring,
  and the `vrg-finalize-pr` command that would have watched them. The PRs
  still merge on their own; run that command later to settle `develop`.
  It cannot be combined with `--release`.

A finalize that exits with a tool error leaves the PRs open with
auto-merge on, and submit prints the command to re-run the finalize
alone.

## Running from the repo root

In template mode, `vrg-submit-pr` may be run from the repo root. It
resolves the target worktree itself:

- **One submittable worktree** (contains `.vergil/pr-workflow.json`):
  announced and entered automatically; the usual preview and `[y/N]`
  confirmation follow.
- **Several submittable worktrees:** a numbered menu shows each
  worktree with its issue number and title; pick one.
- **None:** an error lists each worktree and why it was skipped.

Run from inside a worktree, it submits that worktree. The tool is
interactive by design: it is a human touch point of the workflow and
requires a terminal; root launches fail fast when stdin is not a TTY.

A worktree whose PR is already submitted is not submitted again: submit
says so and points at `vrg-finalize-pr`.

### Several worktrees (`--all` / `--select`)

From the repo root, `--all` selects every ready worktree, and
`--select <tokens>` a comma-separated subset matched by issue number or
worktree directory name (for example `--select 1673,1681`); an unmatched
or ambiguous token is a hard error that names it. They are separate PRs,
unrelated to a [batch PR](#batch-prs-one-pr-closing-several-issues). All
of them go through the one submit path above, behind one confirmation,
and one finalize run watches them all.

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
   (a mismatch fails loudly: the metadata is for a different commit);
3. opens the PR with `--head <branch>` and **does not push**, since the
   branch is already on origin.

When a branch has a local worktree ready-state **and** origin lacks the
branch (for example a workflow-file change the agent's token could not
push), it is pushed with the human's credentials and opened locally
instead (issue #2794).

The PRs then follow the one submit path and the default finalize like
any other mode: finalizing, releasing and installing act on the PRs and
the remote, so the branches need no local checkout. A branch that
already carries a submitted PR is refused, naming the
`vrg-finalize-pr <url>` command to watch it instead; with
`--no-finalize` it is reported and skipped.

!!! warning "The relay ref is world-readable"
    On a public repo, anyone can read `refs/vergil/pr-workflow/<branch>`.
    Keep secrets out of the `report-ready` `--title`, `--summary`, and
    `--notes`: they are public the moment they are recorded.

The relay ref is cleaned up by [`vrg-finalize-pr`](finalize-pr.md), which
deletes it alongside the branch once the PR merges and sweeps any
orphaned relay ref whose branch no longer exists.

## Arguments

| Argument | Required (CLI mode) | Description |
| -------- | ------------------- | ----------- |
| `branches` | No | Relay branch mode: one or more branches to open PRs for. Cannot be combined with `--all`/`--select` or the CLI-mode flags |
| `--issue` | Yes | Issue number or cross-repo ref |
| `--summary` | Yes | One-line PR summary |
| `--title` | Yes | PR title |
| `--linkage` | No | Linkage keyword (default: `Ref`) |
| `--notes` | No | Additional notes for the PR |
| `--base` | No | Target branch. Only `develop`, the default, is accepted; any other branch is refused |
| `--all` | No | From the repo root: select every ready worktree |
| `--select` | No | From the repo root: a comma-separated list of ready worktrees, by issue number or worktree directory name |
| `--no-finalize` | No | Open the PRs with auto-merge on and return without watching them |
| `--release` | No | Pass `--release` to `vrg-finalize-pr`, which chains into `vrg-release` (installing by default) on full success. Not allowed with `--no-finalize` |
| `--no-install` | No | With `--release`, run `vrg-release --no-install` |
| `--allow-provenance-violation` | No | Turn auto-merge on even for a PR whose provenance check finds a violation (a conscious human override); also passed to `vrg-finalize-pr`, where it covers only an open PR |
| `--dry-run` | No | Print what would be done without executing |
| `--yes` | No | Skip the confirmation |

### Removed flags

Each removed flag is refused by name before any side effect, exits 1 and
names the new default:

| Flag | Why it is gone |
| ---- | -------------- |
| `--finalize` | Finalizing is the default; `--no-finalize` opts out |
| `--install` | `--release` installs by default; `--no-install` opts out |

### Linkage Keywords

`Ref`, or `Closes`, which submit auto-selects for a managed task (an
issue with an `epic`-labelled parent) so it closes on merge. `Fixes` and
`Resolves` are rejected.

## Examples

```bash
# Normal flow: resolve the worktree, preview, confirm, open with
# auto-merge on, then watch it merge and settle develop
vrg-submit-pr

# Every ready worktree, one confirmation, one finalize
vrg-submit-pr --all

# Merge, then release (installing by default)
vrg-submit-pr --release

# Open with auto-merge on and return; watch later with vrg-finalize-pr
vrg-submit-pr --no-finalize

# Relay branch mode: open PRs for cloud-implemented branches, worktree-free
vrg-submit-pr feature/123-x feature/124-y

# Preview without submitting
vrg-submit-pr --dry-run

# Emergency CLI mode
vrg-submit-pr \
  --issue 42 \
  --summary "Add new lint check for X" \
  --title "feat(lint): add new check for X"
```

## Batch PRs (one PR closing several issues)

A `report-ready` state can hand off a **batch**: 2 to 8 issues of one epic,
implemented on one branch with exactly one commit per issue, in order (epic
vergil-project/.github#1019). Every mode driven by a ready-state (template,
relay branch and `--all`/`--select`) submits it as one PR:

- the per-issue refusals (cross-repo, non-task, operational, queue item) run
  on every issue, and the first refusal stops the submit;
- the PR body carries one `Closes` line per issue, in the batch's order;
- after the confirmation and before any push or `gh pr create`, it fetches
  `develop` and re-checks the batch hard rules and the
  one-commit-per-issue invariant at the head the PR opens at. A failure
  opens nothing for that PR and is named in the summary;
- auto-merge uses the **rebase** method, pinned to that head, so
  `develop` gets one commit and one changelog entry per issue.

The checks are the ones `vrg-pr-workflow report-ready` ran (see the
[CLI Tools Overview](../cli-tools-overview.md#vrg-pr-workflow)). CLI mode
stays single-issue: a batch is handed off only through `report-ready`.

## The planning issue of a docs task

An epic's planning is done when its documentation task's PR, the spec and the
plan, merges. So that PR also closes the epic's **planning issue**
(vergil-project/.github#950). When the PR closes one issue, `vrg-submit-pr`
checks whether that issue is an epic's documentation task:

1. Its parent epic's body names the planning issue on a
   `Planning: <owner>/<repo>#<M>` line.
2. That planning issue's latest `Docs task:` marker names the PR's issue.
   Only comments by trusted authors count.

If both hold, the body gets one more line under its issue linkage:

```markdown
## Issue Linkage

- Closes #20
- Planning issue: Closes #7
```

It adds the line only when the planning issue is in the PR's own repo, is
open, sits under the planning epic, and has no security advisory still
tracking it. When any of these fails, it prints the reason, adds no line, and
the planning issue stays open for the human or `epic-review planning` to deal
with. A failed GitHub read aborts the submit, as the other per-issue checks
do. Any other issue gets no extra line and no message, and a batch PR is never
checked.

The line does not begin with the keyword, so the tools that read a body's
task linkage (batch detection, `batch-check`, merge review, the drift audit)
see only the task, and the PR stays an ordinary squash-merged PR. GitHub
closes the planning issue on merge, and
[`vrg-finalize-pr`](finalize-pr.md) closes any closing reference GitHub left
open. `vrg-pr-workflow report-ready` previews the result in its response as
`planning_close`.

## Exit Codes

Submit returns finalize's exit status, with two adjustments: when any PR
failed to submit it returns at least 3, and a tool error from finalize
(1, or any code finalize does not define) always wins.

| Code | Meaning |
| ---- | ------- |
| 0 | Every PR opened and finalize reached full success (with `--no-finalize`: every PR opened with auto-merge on). Also a worktree whose PR is already submitted |
| 1 | A usage error or a refusal before anything opened (a removed or contradictory flag, an agent identity, a declined confirmation, no submittable worktree, a malformed `$VRG_FINALIZE_DEADLINE`, `--release` on a repo that is not released), or a tool error from finalize |
| 3 | Not every PR merged: a submit failed, or finalize reported a PR not merged. With `--no-finalize`, or when no PR opened, a failed submit exits 3 |
| 4 | Develop did not validate after the merges (finalize's 4) |
| 5 | Every PR merged and develop validated, but the chained release failed |
