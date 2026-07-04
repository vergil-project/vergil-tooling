# Git Hooks and Validation

> **Looking for the overall workflow?** See
> [Git Workflow](site/docs/guides/git-workflow.md) for the
> big-picture guide covering branching, commit/PR/finalize cycle,
> worktrees, and both enforcement layers together. This page is the
> detailed reference for the Claude Code hook guard and the local
> validators only.

## Table of Contents

- [Overview](#overview)
- [Claude Code Hook Guard](#claude-code-hook-guard)
  - [Setup](#setup)
  - [How It Works](#how-it-works)
- [Validators](#validators)
  - [repo-profile](#repo-profile)
  - [markdown-standards](#markdown-standards)
- [Validation Matrix](#validation-matrix)
- [Configuration Points](#configuration-points)
- [Exit Code Conventions](#exit-code-conventions)
- [Error Reference](#error-reference)

## Overview

This repository enforces code quality through three complementary
entry points that share a common set of validators:

- **Claude Code hook guard** fires on every `Bash` tool
  invocation in Claude Code, blocking raw `git` and `gh`
  commands before they execute.
- **CI workflows** run the same validators on pull requests,
  ensuring standards are enforced even when hooks are not
  installed.
- **Claude Code plugin hooks** (delivered by
  [`vergil-claude-plugin`](https://github.com/vergil-project/vergil-claude-plugin))
  enforce a subset of the same rules at the agent-tool level —
  catching problems before they reach the `git commit` that the
  pre-commit hook would evaluate. Covered in detail in the plugin
  repo; summarized under
  [Validation Matrix](#validation-matrix) below.

All hooks and validators are managed by vergil-tooling.
Consuming repositories resolve host-side `vrg-*` tools via
`uv tool install` and in-container validators via the dev
container image's pre-bake or a Python dev-dep declaration.

## Claude Code Hook Guard

### Setup

Every managed repo ships two files that work together:

- **`.claude/hooks/guard.sh`** — a shell shim that delegates to
  `vrg-hook-guard` (when vergil-tooling is installed) or falls
  back to a `jq`-based hard deny for raw `git`/`gh` commands.
- **`.claude/settings.json`** — wires `guard.sh` as a
  `PreToolUse` hook on the `Bash` matcher.

No per-clone configuration is required. The hook fires
automatically in every Claude Code session that opens the repo.

### How It Works

The hook guard intercepts every `Bash` tool invocation in
Claude Code. It uses regex matching to detect raw `git` and
`gh` commands and blocks them, while allowing `vrg-git`,
`vrg-gh`, and other `vrg-*` wrappers through. The guard is
only active in repos that contain a `vergil.toml`.

The five commit-context checks below live in `vrg-commit`
itself and run before `git commit` is invoked.

**1. Detached HEAD check** — Commits on a detached HEAD are
blocked unconditionally. Create a named branch first.

**2. Protected branch check** — Direct commits to `develop`,
`release`, and `main` are forbidden. These branches accept
changes only through pull requests.

**3. Branching model lookup** — `vrg-commit` reads
`branching_model` from `docs/repository-standards.md` to
determine which branch prefixes are allowed.

**4. Branch prefix check** — The current branch name must
match one of the allowed prefixes for the repository's
branching model:

- **docs-single-branch** — `feature/*`, `bugfix/*`, `chore/*`
- **application-promotion** — `feature/*`, `bugfix/*`,
  `hotfix/*`, `chore/*`, `promotion/*`
- **library-release** — `feature/*`, `bugfix/*`, `hotfix/*`,
  `chore/*`, `release/*`

If the branching model is missing, the hook falls back to
`feature/*`, `bugfix/*`, and `chore/*` with a warning.

**5. Issue number check** — Work branches (`feature/*`,
`bugfix/*`, `hotfix/*`, `chore/*`) must include a repository
issue number in the branch name. The required format is:

```text
{type}/{issue}-{description}
```

For example: `feature/42-add-caching`. The `release/*` and
`promotion/*` prefixes are exempt because they are created by
automated workflows and have no associated issue.

The full pattern:
`^(feature|bugfix|hotfix|chore)/[0-9]+-[a-z0-9][a-z0-9-]*$`

## Validators

### repo-profile

Validates that `docs/repository-standards.md` contains all
six required attributes with non-placeholder values.

**Required attributes**:

- `repository_type`
- `versioning_scheme`
- `branching_model`
- `release_model`
- `supported_release_lines`
- `primary_language`

Values containing `<`, `>`, or `|` are rejected as
placeholders.

### markdown-standards

Validates markdown files using markdownlint and structural
checks.

**File discovery**:

- Standard docs: all `*.md` files under `docs/`, excluding
  `docs/sphinx/`, `docs/site/`, and `docs/announcements/`.
  Also includes `README.md` if present.
- Doc-site files: `*.md` under `docs/sphinx/` and
  `docs/site/` (markdownlint only, no structural checks).
- `CHANGELOG.md`: markdownlint only (no structural checks).

**Structural checks** (standard docs only):

- Exactly one H1 heading per file
- A `## Table of Contents` section must be present
- No heading level skips (e.g., jumping from H2 to H4)

Code blocks (fenced with `` ``` `` or `~~~`) are excluded
from structural analysis.

## Validation Matrix

The following table shows where each validation runs:

- **vrg-commit**: checks run by `vrg-commit` before invoking
  `git commit`
- **hook guard**: the Claude Code `PreToolUse` hook
  (`.claude/hooks/guard.sh` → `vrg-hook-guard`) that blocks raw
  `git` and `gh` commands
- **plugin**: PreToolUse/PostToolUse hooks from
  [`vergil-claude-plugin`](https://github.com/vergil-project/vergil-claude-plugin)
  fire when Claude Code invokes Bash / Write / Edit tools. Catches
  patterns that the hook guard does not cover (e.g., heredoc
  escaping bugs, worktree convention).
- **CI**: runs in GitHub Actions on pull requests

| Validation                          | vrg-commit | hook guard | plugin | CI  |
|-------------------------------------|:----------:|:----------:|:------:|:---:|
| Detached HEAD                       | yes        |            |        |     |
| Protected branch (commit on `develop`/`main`) | yes | | yes | |
| Branch prefix                       | yes        |            |        |     |
| Issue number in branch              | yes        |            |        |     |
| Raw `git` commands forbidden        |            | yes        | yes    |     |
| Raw `gh` commands forbidden         |            | yes        | yes    |     |
| Heredoc in CLI args forbidden       |            |            | yes    |     |
| Commit must originate from `.worktrees/*` | | | yes (adopted repos) | |
| MEMORY.md writes                    |            | | *(removed 2026-04-23; formerly plugin)* | |
| Repository profile                  |            |            |        | yes |
| Markdown standards                  |            |            |        | yes |

The hook guard and plugin hooks for raw `git`/`gh` commands overlap
deliberately: the hook guard (`.claude/hooks/guard.sh`) catches all
raw `git`/`gh` invocations via regex matching, while the plugin
provides additional pattern-specific blocks (heredocs, worktree
convention enforcement). Together they close the loop. See
[Git Workflow → Two enforcement layers](site/docs/guides/git-workflow.md#two-enforcement-layers)
for the coordinated view.

## Configuration Points

**`docs/repository-standards.md`** — The repository profile
is the primary configuration surface. It controls:

- **`branching_model`**: Determines which branch prefixes
  `vrg-commit` allows.
- **`Co-Authored-By` entries**: Defines approved AI agent
  identities for co-author trailer resolution.
- **Six required attributes**: Validated by `repo-profile`
  in CI.

**`.markdownlint.yaml`** — Controls markdownlint rules.
When present, `markdown-standards` passes it via `--config`.

## Exit Code Conventions

All hooks and validators follow a consistent exit code
scheme:

- **`0`** — Validation passed.
- **`1`** — Validation failed. The input does not meet
  the required standard.
- **`2`** — Usage error. Required input is missing or the
  environment is misconfigured (e.g., missing file path
  argument, `markdownlint` not installed,
  `GITHUB_EVENT_PATH` not set).

## Error Reference

**`"ERROR: detached HEAD is not allowed for commits."`**
— `vrg-commit` blocks commits on a detached HEAD.
Create a named branch before committing.

**`"ERROR: direct commits to protected branches are
forbidden"`** — `vrg-commit` blocks commits to
`develop`, `release`, or `main`. Create a feature branch
and open a pull request.

**`"ERROR: branch name must use {prefixes}"`** — The
current branch does not match any allowed prefix for the
repository's branching model. Rename the branch.

**`"WARNING: branching_model not found"`** — The
repository profile does not contain a `branching_model`
attribute. The hook falls back to `feature/*`, `bugfix/*`,
and `chore/*`. Add the attribute to suppress this warning.

**`"ERROR: branch name must include a repo issue number"`**
— Work branches must follow the `{type}/{issue}-{desc}`
format. Example: `feature/42-add-caching`.

**`"ERROR: unrecognized branching_model"`** — The
`branching_model` value in the repository profile is not
one of the three supported models.

**`"ERROR: repository profile missing required attribute"`**
— One of the six required attributes is not present in
`docs/repository-standards.md`.

**`"ERROR: repository profile attribute appears to be a
placeholder"`** — An attribute value contains `<`, `>`,
or `|`, indicating it has not been filled in.

**`"ERROR: no markdown files found to lint."`** — No
markdown files were discovered. Ensure docs exist under
`docs/` or that `README.md` is present.

**`"ERROR: markdownlint not found."`** — The
`markdownlint` CLI is not installed. Run
`npm install --global markdownlint-cli`.

**`"ERROR: pull request body is empty"`** — The PR has
no body text. Add issue linkage to the PR description.

**`"ERROR: pull request body contains a GitHub auto-close
keyword"`** — The PR body uses `Fixes`, `Closes`, `Resolves`
or a variant. Replace with `Ref #N`. Issues must remain open
until post-merge workflows succeed.

**`"ERROR: pull request body must include primary issue
linkage"`** — The PR body does not contain `Ref` followed by
an issue reference.

**`"ERROR: repository profile not found"`** — The file
`docs/repository-standards.md` does not exist. Create it
with the required attributes.
