# Standard Tooling

Standard-tooling is a Python package and script collection providing shared
development tooling for all managed repositories. It delivers CLI tools for
commits, PRs, releases, and validation alongside bash validators and git hooks
-- all consumed via PATH.

## Components

**Python CLI tools** (`src/standard_tooling/`):
`st-commit`, `st-submit-pr`, `st-prepare-release`,
`st-finalize-repo`, `st-validate-local`

**Lint tools** (installed as `st-*`):
`st-repo-profile`, `st-pr-issue-linkage`, validation drivers

**Git hooks** (`.githooks/`):
Env-var gate that admits `st-commit` and blocks raw `git commit`

## Design Principles

- **Portability** -- scripts run on both macOS and Linux
- **shellcheck clean** -- all shell scripts pass shellcheck
- **No repo-specific logic** -- every script works in any consuming
  repository
- **Host-level install** -- `uv tool install` puts `st-*` on PATH;
  no sibling checkout required

## How It Works

1. `standard-tooling` is installed on the developer's host via
   `uv tool install`, placing `st-*` scripts in `~/.local/bin/`.
2. `st-docker-run` bridges host commands into dev container images
   where language runtimes and validators live.
3. Python consumers also declare `standard-tooling` as a dev dep
   via `[tool.uv.sources]` so `uv run st-*` inside the container
   resolves the pinned version.
4. Each repo checks in a `.githooks/pre-commit` env-var gate,
   enabled via `git config core.hooksPath .githooks`.
5. Consuming repos call tools by bare name -- no file copying or
   syncing.

## Quick Links

- [Getting Started](getting-started.md) -- set up a consuming repository
- [Script Reference](reference/index.md) -- documentation for each tool
- [Validation Matrix](guides/validation-matrix.md) -- which checks run where
