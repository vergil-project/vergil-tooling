# vrg-commit

**Installed as:** `vrg-commit` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_commit.py`

Wrapper that constructs standards-compliant commit messages in
Conventional Commits format.

!!! warning "Required for AI agents"
    AI agents **must** use this tool instead of raw `git commit`.

!!! note "No AI co-author trailer"
    Commits carry no AI `Co-Authored-By` trailer. AI co-author
    attribution was dropped, and Claude Code's own attribution is
    turned off in each repo's checked-in `.claude/settings.json`
    (`"attribution": { "commit": "", "pr": "" }`; see #3200).

## Usage

```bash
vrg-commit \
  --type TYPE --scope SCOPE --message MESSAGE [options]
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `--type` | Yes | Conventional commit type |
| `--scope` | Yes | Conventional commit scope |
| `--message` | Yes | Commit description |
| `--body` | No | Detailed commit body |
| `--allow-empty` | No | Allow a commit with no staged changes (e.g. to re-trigger CI) |

### Allowed Types

`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`,
`build`, `revert`

## Examples

```bash
# Feature with scope
vrg-commit \
  --type feat --scope lint \
  --message "add new check"

# Documentation with body
vrg-commit \
  --type docs --scope readme --message "update README" \
  --body "Expanded usage section"
```

## Behavior

1. Reads `branching_model` from `vergil.toml`.
2. Validates the branch context: no detached HEAD, not a protected
   branch, branch prefix matches the branching model, issue number
   in the branch name, not the main worktree when `.worktrees/` is
   present.
3. Refuses the commit when the branch has been reported ready
   (`vrg-pr-workflow report-ready`) and is frozen.
4. Rejects a body containing a GitHub auto-close keyword.
5. Verifies staged changes exist (unless `--allow-empty`).
6. Constructs the commit message in a temporary file and runs
   `git commit --file`.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Commit created successfully |
| 1 | Validation failure or error |
