# CLI Tools Overview

Every `st-*` command provided by this package, organized by runtime
context. Each entry documents the tool's purpose, where it runs,
what it assumes, and how it fails when those assumptions are violated.

## Host tools

Host tools run on the developer's machine (outside any container).
They drive git, `gh`, SSH, and Docker operations. Installed via
`uv tool install` or the dev-tree override venv.

### st-commit

Construct standards-compliant conventional commits with co-author
resolution. Performs five branch/context checks before committing
and sets `ST_COMMIT_CONTEXT=1` so the `.githooks/pre-commit` gate
admits the commit.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.commit` |
| Args | `--type` (required), `--scope`, `--message` (required), `--body`, `--agent` (required) |
| Preconditions | Git repo, staged changes, not detached HEAD, not on protected branch, branch prefix matches branching model, issue number in branch name, not main worktree when `.worktrees/` present |
| Failure mode | `SystemExit` with diagnostic on stderr for each check |
| Exit codes | 0 success, 1 rejection or error |
| Status | Active |

### st-submit-pr

Create standards-compliant pull requests. Pushes the current branch
and opens a PR with a populated template body.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.submit_pr` |
| Args | `--issue` (required), `--summary` (required), `--linkage` (default: Fixes), `--notes`, `--title`, `--dry-run` |
| Preconditions | Git repo, `gh` CLI on PATH |
| Failure mode | Subprocess error from `git push` or `gh pr create` |
| Exit codes | 0 success |
| Status | Active |

### st-merge-when-green

Poll a PR's CI checks, then merge when they all pass. Designed for
release-workflow PRs where the agent is both author and reviewer.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.merge_when_green` |
| Args | `pr` (positional, required), `--strategy` (merge/squash/rebase), `--no-delete-branch` |
| Preconditions | `gh` CLI on PATH, worktree-aware (skips `--delete-branch` in secondary worktrees) |
| Failure mode | `subprocess.CalledProcessError` from `gh pr checks --fail-fast` on first red check |
| Exit codes | 0 success, non-zero on check failure or merge failure |
| Status | Active |

### st-prepare-release

Automate release preparation: create release branch from develop,
merge main, generate changelog and release notes, push, and open PR.
Auto-detects the ecosystem (Python, Maven, Go, Ruby, Cargo,
Claude plugin, VERSION file) to find the version.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.prepare_release` |
| Args | `--issue` (required) |
| Preconditions | On `develop` branch, clean working tree, local develop matches `origin/develop`, `gh` and `git-cliff` on PATH |
| Failure mode | `SystemExit` with clear message for each precondition |
| Exit codes | 0 success, 1 error |
| Status | Active |

### st-finalize-repo

Post-merge repository cleanup. Switches to the target branch,
fast-forward pulls, deletes merged local branches (auto-removing
worktrees inside `.worktrees/` when necessary), prunes remotes,
runs validation, and checks the Documentation workflow status.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.finalize_repo` |
| Args | `--target-branch` (default: develop), `--dry-run` |
| Preconditions | Git repo, worktree-aware (auto-switches to main worktree), `st-docker-run` or `st-validate-local` on PATH |
| Failure mode | `SystemExit` if neither validator is found; validation failures return exit 1; docs workflow failure is a soft warning (exit 0) |
| Exit codes | 0 success, 1 validation failure or unrecognized branching model |
| Status | Active |

### st-ensure-label

Ensure GitHub labels exist. Three modes: single-label (create/update
one label), sync (provision all labels from the canonical registry),
and project (discover repos via a GitHub Project and sync each).

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.ensure_label` |
| Args | `--repo`, `--label`, `--color`, `--description`, `--sync`, `--owner`, `--project` |
| Preconditions | `gh` CLI on PATH |
| Failure mode | argparse validation for incompatible flag combinations; subprocess error from `gh` |
| Exit codes | 0 success |
| Status | Active |

### st-docker-run

Run arbitrary commands inside a dev container. Auto-detects the
project language to select the Docker image; falls back to
`dev-base:latest`. Uses `execvp` to replace the process.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.docker_run` |
| Args | `[--] <command> [args...]` (manual parsing, `--` separator) |
| Preconditions | Git repo, `GH_TOKEN` set, Docker daemon running |
| Failure mode | Explicit error message for missing `GH_TOKEN`; `assert_docker_available()` exits with message for Docker; `git.repo_root()` raises on non-git directory |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### st-docker-test

Run a repository's test suite inside a dev container. Auto-detects
language and selects appropriate image and test command.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.docker_test` |
| Args | None |
| Preconditions | Git repo, Docker daemon running, language detection or `DOCKER_DEV_IMAGE` + `DOCKER_TEST_CMD` |
| Failure mode | Explicit error for undetected language and unavailable Docker; `git.repo_root()` raises on non-git directory |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### st-docker-docs

Preview or build MkDocs documentation inside a dev container.
Supports `serve` (live-reload) and `build` subcommands. For Python
repos, wraps with `uv sync --group docs`.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.docker_docs` |
| Args | `<serve\|build> [mkdocs args...]` (manual parsing) |
| Preconditions | Git repo, Docker daemon |
| Failure mode | Usage message on missing/unknown subcommand |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### st-generate-commands

Generate MQSC command methods for all language ports (Python, Ruby,
Java, Go, Rust) from `mapping-data.json`. Updates target files
between `BEGIN/END GENERATED MQSC METHODS` markers.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.generate_commands` |
| Args | `--language` (required), `--mapping-data` (required), `--target`, `--mapping-pages-dir`, `--check` |
| Preconditions | `mapping-data.json` file exists at the given path |
| Failure mode | Explicit error for missing mapping data file |
| Exit codes | 0 success, 1 error or `--check` mismatch |
| Status | Active |

## Container tools

Container tools run inside dev containers launched by `st-docker-run`.
They assume language toolchain dependencies (ruff, mypy, shellcheck,
markdownlint, yamllint) are available on PATH.

### st-validate-local

Shared driver for pre-PR local validation. Reads `primary_language`
from the repository profile, then dispatches to
`validate-local-common`, `validate-local-<lang>`, and optionally
`validate-local-custom`.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.validate_local` |
| Args | None |
| Preconditions | Git repo; repository profile (soft — falls back to empty language) |
| Failure mode | Propagates exit codes from child validators |
| Exit codes | 0 all passed, 1 any check failed |
| Status | Active |

### st-validate-local-common

Shared validation checks for all repos: repository profile
validation, markdownlint on published markdown (`docs/site/**/*.md`
and `README.md`) using the bundled canonical config, shellcheck on
`scripts/`, and yamllint on `.github/` and `docs/` YAML files.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.validate_local_common_container` |
| Args | None |
| Preconditions | Git repo, `shellcheck` and `yamllint` on PATH |
| Failure mode | Propagates exit codes from each tool |
| Exit codes | 0 all passed, non-zero on first failure |
| Status | Active |

### st-validate-local-python / -rust / -go / -java

Language-specific validation. Runs `scripts/dev/{lint,typecheck,test,audit}.sh`
in sequence. All four entry points share a single source module;
the language is determined from the entry point name or `--language`.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.validate_local_lang` |
| Args | `--language` (optional; inferred from entry point name) |
| Preconditions | Git repo, `scripts/dev/*.sh` present and executable |
| Failure mode | Error message if language cannot be determined; propagates script exit codes |
| Exit codes | 0 all passed, 1 any script failed |
| Status | Active |

### st-repo-profile

Validate the repository profile in `docs/repository-standards.md`.
Checks that all required attributes are present and none contain
placeholder values.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.repo_profile_cli` |
| Args | None |
| Preconditions | `docs/repository-standards.md` exists |
| Failure mode | Exit 2 if profile file not found; exit 1 for missing or placeholder attributes |
| Exit codes | 0 valid, 1 invalid, 2 file not found |
| Status | Active |

## CI-only tools

### st-pr-issue-linkage

Check that a pull request body includes primary issue linkage.
Reads the GitHub event payload from `GITHUB_EVENT_PATH`.

| Attribute | Value |
|---|---|
| Source | `standard_tooling.bin.pr_issue_linkage` |
| Args | None |
| Preconditions | `GITHUB_EVENT_PATH` set and pointing to a valid JSON file |
| Failure mode | Exit 2 for missing env var or file; exit 1 for missing linkage |
| Exit codes | 0 valid, 1 missing linkage, 2 infrastructure error |
| Status | Active |

## Removed in this audit

### st-list-project-repos (removed)

Entry point declared in `pyproject.toml` but the source module
`standard_tooling.bin.list_project_repos` did not exist. Would crash
on import with `ModuleNotFoundError`. The underlying function
(`list_project_repos`) lives in `standard_tooling.lib.github` and is
consumed by `st-ensure-label --owner/--project` mode.

### st-set-project-field (removed)

Entry point declared in `pyproject.toml` but the source module
`standard_tooling.bin.set_project_field` did not exist. Would crash
on import with `ModuleNotFoundError`.

## Audit notes

### Precondition consistency

Most tools check preconditions explicitly and fail with clear
messages. Notable gaps:

- `st-submit-pr` does not validate that `gh` is on PATH before
  attempting `git push` and `gh pr create`. Failure surfaces as a
  subprocess error rather than a clear precondition message.
- `st-docker-run` checks `GH_TOKEN` explicitly; `st-docker-test`
  does not (it will fail inside the container when `gh` commands
  run without a token, but the error is less clear).

### Args style

Most tools use `argparse`. Two exceptions:

- `st-docker-run` parses `sys.argv` manually with a `--` separator.
- `st-docker-docs` parses `sys.argv` manually with subcommands.

Both are intentional: `st-docker-run` passes everything after `--`
through to `docker run`, and `st-docker-docs` has a simpler
interface than argparse would provide. No alignment needed.

### Exit code contract

- 0: success
- 1: check failure, validation error, or precondition violation
- 2: infrastructure error (used by `st-repo-profile`,
  `st-markdown-standards`, `st-pr-issue-linkage`)

The 1-vs-2 distinction is not universal. Tools added before the
convention was established use 1 for all errors.

### st-validate-local-common

`st-validate-local-common` points directly at the
`validate_local_common_container` module. The former passthrough
wrapper (`validate_local_common.py`) was removed in issue #403.
