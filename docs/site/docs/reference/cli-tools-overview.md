# CLI Tools Overview

Every `st-*` command provided by this package, organized by runtime
context. Each entry documents the tool's purpose, where it runs,
what it assumes, and how it fails when those assumptions are violated.

## Host tools

Host tools run on the developer's machine (outside any container).
They drive git, `gh`, SSH, and Docker operations. Installed via
`uv tool install` or the dev-tree override venv.

### vrg-commit

Construct standards-compliant conventional commits with co-author
resolution. Performs five branch/context checks before committing
and sets `ST_COMMIT_CONTEXT=1` so the `.githooks/pre-commit` gate
admits the commit.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_commit` |
| Args | `--type` (required), `--scope`, `--message` (required), `--body`, `--agent` (required) |
| Preconditions | Git repo, staged changes, not detached HEAD, not on protected branch, branch prefix matches branching model, issue number in branch name, not main worktree when `.worktrees/` present |
| Failure mode | `SystemExit` with diagnostic on stderr for each check |
| Exit codes | 0 success, 1 rejection or error |
| Status | Active |

### vrg-submit-pr

Create standards-compliant pull requests. Pushes the current branch
and opens a PR with a populated template body.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_submit_pr` |
| Args | `--issue` (required), `--summary` (required), `--linkage` (default: Fixes), `--notes`, `--title`, `--dry-run` |
| Preconditions | Git repo, `gh` CLI on PATH |
| Failure mode | Subprocess error from `git push` or `gh pr create` |
| Exit codes | 0 success |
| Status | Active |

### vrg-merge-when-green

Poll a PR's CI checks, then merge when they all pass. Designed for
release-workflow PRs where the agent is both author and reviewer.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_merge_when_green` |
| Args | `pr` (positional, required), `--strategy` (merge/squash/rebase), `--no-delete-branch` |
| Preconditions | `gh` CLI on PATH, worktree-aware (skips `--delete-branch` in secondary worktrees) |
| Failure mode | `subprocess.CalledProcessError` from `gh pr checks --fail-fast` on first red check |
| Exit codes | 0 success, non-zero on check failure or merge failure |
| Status | Active |

### vrg-prepare-release

Automate release preparation: create release branch from develop,
merge main, generate changelog and release notes, push, and open PR.
Auto-detects the ecosystem (Python, Maven, Go, Ruby, Cargo,
Claude plugin, VERSION file) to find the version.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_prepare_release` |
| Args | `--issue` (required) |
| Preconditions | On `develop` branch, clean working tree, local develop matches `origin/develop`, `gh` and `git-cliff` on PATH |
| Failure mode | `SystemExit` with clear message for each precondition |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-finalize-repo

Post-merge repository cleanup. Switches to the target branch,
fast-forward pulls, deletes merged local branches (auto-removing
worktrees inside `.worktrees/` when necessary), prunes remotes,
runs validation, and checks the Documentation workflow status.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_finalize_repo` |
| Args | `--target-branch` (default: develop), `--dry-run` |
| Preconditions | Git repo, worktree-aware (auto-switches to main worktree), `vrg-docker-run` on PATH |
| Failure mode | Validation failures return exit 1; docs workflow failure is a soft warning (exit 0) |
| Exit codes | 0 success, 1 validation failure or unrecognized branching model |
| Status | Active |

### vrg-ensure-label

Ensure GitHub labels exist. Three modes: single-label (create/update
one label), sync (provision all labels from the canonical registry),
and project (discover repos via a GitHub Project and sync each).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_ensure_label` |
| Args | `--repo`, `--label`, `--color`, `--description`, `--sync`, `--owner`, `--project` |
| Preconditions | `gh` CLI on PATH |
| Failure mode | argparse validation for incompatible flag combinations; subprocess error from `gh` |
| Exit codes | 0 success |
| Status | Active |

### vrg-docker-run

Run arbitrary commands inside a dev container. Auto-detects the
project language to select the Docker image; falls back to
`dev-base:latest`. Uses `execvp` to replace the process.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_docker_run` |
| Args | `[--] <command> [args...]` (manual parsing, `--` separator) |
| Preconditions | Git repo, `GH_TOKEN` set, Docker daemon running |
| Failure mode | Explicit error message for missing `GH_TOKEN`; `assert_docker_available()` exits with message for Docker; `git.repo_root()` raises on non-git directory |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### vrg-docker-test

Run a repository's test suite inside a dev container. Auto-detects
language and selects appropriate image and test command.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_docker_test` |
| Args | None |
| Preconditions | Git repo, Docker daemon running, language detection or `DOCKER_DEV_IMAGE` + `DOCKER_TEST_CMD` |
| Failure mode | Explicit error for undetected language and unavailable Docker; `git.repo_root()` raises on non-git directory |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### vrg-docker-docs

Preview or build MkDocs documentation inside a dev container.
Supports `serve` (live-reload) and `build` subcommands. For Python
repos, wraps with `uv sync --group docs`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_docker_docs` |
| Args | `<serve\|build> [mkdocs args...]` (manual parsing) |
| Preconditions | Git repo, Docker daemon |
| Failure mode | Usage message on missing/unknown subcommand |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### vrg-generate-commands

Generate MQSC command methods for all language ports (Python, Ruby,
Java, Go, Rust) from `mapping-data.json`. Updates target files
between `BEGIN/END GENERATED MQSC METHODS` markers.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_generate_commands` |
| Args | `--language` (required), `--mapping-data` (required), `--target`, `--mapping-pages-dir`, `--check` |
| Preconditions | `mapping-data.json` file exists at the given path |
| Failure mode | Explicit error for missing mapping data file |
| Exit codes | 0 success, 1 error or `--check` mismatch |
| Status | Active |

## Container tools

Container tools run inside dev containers launched by `vrg-docker-run`.
They assume language toolchain dependencies (ruff, mypy, shellcheck,
markdownlint, yamllint) are available on PATH.

### vrg-validate-common

Shared validation checks for all repos: repository profile
validation, markdownlint on published markdown (`docs/site/**/*.md`
and `README.md`) using the bundled canonical config, shellcheck on
`scripts/`, yamllint on `.github/` and `docs/` YAML files, hadolint
on `Dockerfile*`, and actionlint on `.github/workflows/`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.validate_common` |
| Args | None |
| Preconditions | Git repo, `shellcheck` and `yamllint` on PATH |
| Failure mode | Propagates exit codes from each tool |
| Exit codes | 0 all passed, non-zero on first failure |
| Status | Active (called internally by `vrg-validate`) |

### vrg-repo-profile

Validate the repository profile in `docs/repository-standards.md`.
Checks that all required attributes are present and none contain
placeholder values.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_repo_profile` |
| Args | None |
| Preconditions | `docs/repository-standards.md` exists |
| Failure mode | Exit 2 if profile file not found; exit 1 for missing or placeholder attributes |
| Exit codes | 0 valid, 1 invalid, 2 file not found |
| Status | Active |

## CI-only tools

### vrg-pr-issue-linkage

Check that a pull request body uses `Ref` for issue linkage and
does not contain auto-close keywords (`Fixes`, `Closes`, `Resolves`
and variants). Reads the GitHub event payload from
`GITHUB_EVENT_PATH`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.st_pr_issue_linkage` |
| Args | None |
| Preconditions | `GITHUB_EVENT_PATH` set and pointing to a valid JSON file |
| Failure mode | Exit 2 for missing env var or file; exit 1 for auto-close keyword or missing linkage |
| Exit codes | 0 valid, 1 rejected linkage or missing linkage, 2 infrastructure error |
| Status | Active |

## Removed in this audit

### st-list-project-repos (removed)

Entry point declared in `pyproject.toml` but the source module
`vergil_tooling.bin.list_project_repos` did not exist. Would crash
on import with `ModuleNotFoundError`. The underlying function
(`list_project_repos`) lives in `vergil_tooling.lib.github` and is
consumed by `vrg-ensure-label --owner/--project` mode.

### st-set-project-field (removed)

Entry point declared in `pyproject.toml` but the source module
`vergil_tooling.bin.set_project_field` did not exist. Would crash
on import with `ModuleNotFoundError`.

## Audit notes

### Precondition consistency

Most tools check preconditions explicitly and fail with clear
messages. Notable gaps:

- `vrg-submit-pr` does not validate that `gh` is on PATH before
  attempting `git push` and `gh pr create`. Failure surfaces as a
  subprocess error rather than a clear precondition message.
- `vrg-docker-run` checks `GH_TOKEN` explicitly; `vrg-docker-test`
  does not (it will fail inside the container when `gh` commands
  run without a token, but the error is less clear).

### Args style

Most tools use `argparse`. Two exceptions:

- `vrg-docker-run` parses `sys.argv` manually with a `--` separator.
- `vrg-docker-docs` parses `sys.argv` manually with subcommands.

Both are intentional: `vrg-docker-run` passes everything after `--`
through to `docker run`, and `vrg-docker-docs` has a simpler
interface than argparse would provide. No alignment needed.

### Exit code contract

- 0: success
- 1: check failure, validation error, or precondition violation
- 2: infrastructure error (used by `vrg-repo-profile`,
  `st-markdown-standards`, `vrg-pr-issue-linkage`)

The 1-vs-2 distinction is not universal. Tools added before the
convention was established use 1 for all errors.
