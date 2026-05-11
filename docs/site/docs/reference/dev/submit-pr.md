# st-submit-pr

**Installed as:** `st-submit-pr` (Python console script)

**Source:** `src/standard_tooling/bin/st_submit_pr.py`

Wrapper that creates standards-compliant pull requests with
proper issue linkage.

!!! warning "Required for AI agents"
    AI agents **must** use this tool instead of raw
    `gh pr create`. The tool constructs the PR body from
    CLI arguments.

## Prerequisites

When running inside a dev container, `GH_TOKEN` must be set so `gh` can
authenticate. The
[Getting Started prerequisites](../../getting-started.md#prerequisites)
cover `gh auth login` and how `GH_TOKEN` flows through to the
container.

## Usage

```bash
st-submit-pr \
  --issue NUMBER --summary TEXT --title TEXT [options]
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `--issue` | Yes | Issue number or cross-repo ref |
| `--summary` | Yes | One-line PR summary |
| `--title` | Yes | PR title |
| `--linkage` | No | Linkage keyword (default: `Ref`) |
| `--notes` | No | Additional notes for the PR |
| `--dry-run` | No | Print PR body without executing |

### Linkage Keywords

`Ref`

## Examples

```bash
# Standard PR
st-submit-pr \
  --issue 42 \
  --summary "Add new lint check for X" \
  --title "feat(lint): add new check for X"

# Dry run to preview
st-submit-pr \
  --issue 42 \
  --summary "Fix regex bug" \
  --title "fix(regex): handle edge case" \
  --dry-run
```

## Behavior

1. Validates arguments and issue reference format.
2. Detects target branch from the current branch:
    - `release/*` branches target `main`
    - All other branches target `develop`
3. Pushes the branch to origin.
4. Creates the PR via `gh pr create`.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | PR created |
| 1 | Validation failure |
