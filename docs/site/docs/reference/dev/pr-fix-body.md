# vrg-pr-fix-body

**Installed as:** `vrg-pr-fix-body` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_pr_fix_body.py`

Repair a PR's body through the same validated builder that
`vrg-submit-pr` uses to create it. The body is regenerated from
corrected fields — summary, issue reference, linkage, notes — so a
body can only ever be replaced by a standards-compliant body.
Free-form PR edits (`gh pr edit`) remain denied for agent identities;
this tool is the narrow repair path that restores what the pr-watch
reconcile loop needs without reopening structural PR mutation.

!!! note "Why this exists"
    A standards failure on the PR *body* (for example a forbidden
    auto-close linkage keyword) is a failing CI check that no code
    patch can fix. The pr-watch USER agent is chartered to reconcile
    failing checks, so it needs a safe way to fix its own PR's body.
    See issue #1459.

## Identity rules

| Identity | Allowed | Scope |
| -------- | ------- | ----- |
| `audit` | No | The audit identity never mutates PRs |
| `user` | Yes | Only its own PR: the PR's head branch must match the session's current branch |
| `human` | Yes | Any PR (the human is the superset) |

## Usage

```bash
vrg-pr-fix-body PR --issue NUMBER --summary TEXT [options]
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `pr` | Yes | PR number or URL |
| `--issue` | Yes | Issue number or cross-repo ref |
| `--summary` | Yes | One-line PR summary |
| `--linkage` | No | Linkage keyword (default: `Ref`) |
| `--notes` | No | Additional notes for the PR |
| `--dry-run` | No | Print the regenerated body without editing |
| `--no-retrigger` | No | Skip the empty-commit push that re-runs CI |

### Linkage Keywords

`Ref`

## CI re-trigger

A body edit alone does not re-run the standards gate:
`pull_request` workflows trigger on `opened/synchronize/reopened`
(not `edited`), and a manual re-run replays the stale event payload.
After editing the body, the tool therefore pushes an empty commit so
CI re-runs against the corrected body. Pass `--no-retrigger` to skip
this and push your own commit instead.

## Examples

```bash
# Preview the regenerated body
vrg-pr-fix-body 1457 --issue 1450 --summary "Port vrg-vm to progress" --dry-run

# Fix the body and re-trigger CI
vrg-pr-fix-body https://github.com/owner/repo/pull/1457 \
  --issue 1450 \
  --summary "Port vrg-vm lifecycle commands to the progress framework" \
  --notes "Body-only fix: replaced auto-close linkage with Ref"
```

## Behavior

1. Denies the `audit` identity.
2. Validates the issue reference and regenerates the body via the
   shared builder (`vergil_tooling.lib.pr_body`); the linkage keyword
   is constrained to the allowed set.
3. `--dry-run`: prints the body and stops.
4. Agent identities: verifies the PR's head branch matches the
   session's current branch.
5. Verifies the PR is open.
6. Replaces the body via `gh pr edit --body-file`.
7. Pushes an empty commit to re-trigger CI (unless `--no-retrigger`).

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Body replaced (and CI re-triggered unless `--no-retrigger`) |
| 1 | Identity/scope/state rejection or validation failure |
