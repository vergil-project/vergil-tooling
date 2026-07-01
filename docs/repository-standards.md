# Standard Tooling Repository Standards

## Table of Contents

- [External tooling dependencies](#external-tooling-dependencies)
- [CI gates](#ci-gates)
- [Local deviations](#local-deviations)

## External tooling dependencies

- markdownlint (markdownlint-cli)
- shellcheck
- uv

## CI gates

Hard gates (required status checks on `develop`):

- Standards compliance (`.github/workflows/ci.yml`):
  - Repository profile validation (`repo-profile`)
  - Markdownlint (`markdown-standards`)
  - Commit message lint (CI validator)
  - Issue linkage validation (`pr-issue-linkage`)
  - Shellcheck on all bash scripts
  - Python lint, type-check, and tests

Local hard gates (pre-commit hooks):

- Branch naming enforcement (`vrg-commit`):
  branching-model-aware prefix validation.

## Commit and PR scripts

AI agents **must** use the wrapper scripts for commits and PR submission.
Do not construct commit messages or PR bodies manually.

### Committing

```bash
vrg-commit \
  --type TYPE --message MESSAGE --agent AGENT \
  [--scope SCOPE] [--body BODY]
```

- `--type` (required): `feat|fix|docs|style|refactor|test|chore|ci|build|revert`
- `--message` (required): commit description
- `--agent` (required): `claude` or `codex`
- `--scope` (optional): conventional commit scope
- `--body` (optional): detailed commit body

The script resolves the correct `Co-Authored-By` identity from
`vergil.toml` and the git hooks validate the result.

### Submitting PRs

```bash
vrg-submit-pr \
  --issue NUMBER --summary TEXT \
  [--linkage KEYWORD] [--title TEXT] \
  [--notes TEXT] [--dry-run]
```

- `--issue` (required): GitHub issue number (just the number)
- `--summary` (required): one-line PR summary
- `--linkage` (optional, default: `Ref`): `Ref` or `Closes`. `vrg-submit-pr`
  auto-selects `Closes` for a managed task (an issue with an `epic`-labeled
  parent) so it auto-closes on merge; legacy issues keep `Ref`. `Fixes`/
  `Resolves` are rejected — `Closes` is the one sanctioned close keyword
- `--title` (optional): PR title (default: most recent commit subject)
- `--notes` (optional): additional notes
- `--dry-run` (optional): print generated PR without executing

The script detects the target branch and merge strategy automatically.

## Local deviations

- None.
