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
vrg-commit --fixup COMMIT [--allow-empty]
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `--type` | Yes, unless `--fixup` | Conventional commit type |
| `--scope` | Yes, unless `--fixup` | Conventional commit scope |
| `--message` | Yes, unless `--fixup` | Commit description |
| `--body` | No | Detailed commit body |
| `--fixup` | No | Make a `fixup! <subject>` commit of COMMIT, an earlier commit on this branch. Refused with `--type`, `--scope`, `--message` or `--body` |
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

# Fixup of an earlier commit on this branch, staged changes only
vrg-commit --fixup 1a2b3c4
```

## Behavior

1. Reads `[project].branching-model` from `vergil.toml`.
2. Validates the branch context: no detached HEAD, not a protected
   branch, branch prefix matches the branching model, issue number
   in the branch name, not the main worktree when `.worktrees/` is
   present.
3. Refuses the commit when the branch has been reported ready
   (`vrg-pr-workflow report-ready`) and is frozen.
4. With `--fixup`, refuses a target that does not resolve to a commit,
   is not reachable from `HEAD`, or is already reachable from
   `origin/develop` (or when `origin/develop` cannot be resolved).
5. Rejects a body containing a GitHub auto-close keyword.
6. Verifies staged changes exist (unless `--allow-empty`).
7. Constructs the commit message in a temporary file and runs
   `git commit --file`; with `--fixup` it runs
   `git commit --fixup=COMMIT` instead, which writes the
   `fixup! <subject>` message.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Commit created successfully |
| 1 | Validation failure or error |
