# vrg-submit-pr

**Installed as:** `vrg-submit-pr` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_submit_pr.py`

Wrapper that creates standards-compliant pull requests with
proper issue linkage. Has two modes:

- **Template mode** (no arguments) — the normal path. Reads
  `.vergil/pr-template.yml` (written by the agent), shows a preview,
  asks for confirmation, pushes the branch, creates the PR, and emits
  the `/vergil:pr-watch` handoff line.
- **CLI mode** (`--issue/--summary/--title`) — direct invocation for
  human emergency use.

!!! warning "Human-run tool"
    PR submission is a human action. Agent identities are blocked;
    agents hand off via `.vergil/pr-template.yml` instead.

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

- **One submittable worktree** (contains `.vergil/pr-template.yml`):
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
```

## Behavior

1. Template mode from the repo root: resolves the target worktree
   (see above) and moves into it — the invoking shell is unaffected.
2. Reads `.vergil/pr-template.yml` (template mode) or validates CLI
   arguments, including the issue reference format.
3. Detects target branch from the current branch:
    - `release/*` branches target `main`
    - All other branches target `develop`
4. Shows the PR preview and asks `Submit this PR? [y/N]`
   (template mode).
5. Pushes the branch to origin with the human's host credentials.
6. Creates the PR via `gh pr create` and deletes the template.
7. Prints the `/vergil:pr-watch` line to paste into both agent
   sessions, plus the PR URL — which can be passed straight to
   [`vrg-finalize-pr`](finalize-pr.md).

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | PR created |
| 1 | Validation failure, declined confirmation, or no submittable worktree |
