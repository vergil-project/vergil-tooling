# Vergil Tooling

Vergil-tooling is a Python package and script collection providing shared
development tooling for all managed repositories. It delivers CLI tools for
commits, PRs, releases, and validation alongside bash validators and git hooks
-- all consumed via PATH.

## Components

**Python CLI tools** (`src/vergil_tooling/`):
`vrg-commit`, `vrg-submit-pr`, `vrg-prepare-release`,
`vrg-finalize-pr`, `vrg-validate`

**Lint tools** (installed as `vrg-*`):
`vrg-repo-profile`, `vrg-pr-issue-linkage`, validation drivers

**Claude Code hook guard** (`.claude/hooks/`):
PreToolUse hook that blocks raw `git` and `gh`, routing through `vrg-*` wrappers

## Design Principles

- **Portability** -- scripts run on both macOS and Linux
- **shellcheck clean** -- all shell scripts pass shellcheck
- **No repo-specific logic** -- every script works in any consuming
  repository
- **Host-level install** -- `uv tool install` puts `vrg-*` on PATH;
  no sibling checkout required

## How It Works

1. `vergil-tooling` is installed on the developer's host via
   `uv tool install`, placing `vrg-*` scripts in `~/.local/bin/`.
2. `vrg-container-run` bridges host commands into dev container images
   where language runtimes and validators live.
3. Python consumers also declare `vergil-tooling` as a dev dep
   via `[tool.uv.sources]` so `uv run vrg-*` inside the container
   resolves the pinned version.
4. Each repo ships a `.claude/hooks/guard.sh` shim wired via
   `.claude/settings.json`, blocking raw `git`/`gh` in agent sessions.
5. Consuming repos call tools by bare name -- no file copying or
   syncing.

## Quick Links

- [Getting Started](getting-started.md) -- set up a consuming repository
- [Script Reference](reference/index.md) -- documentation for each tool
- [Validation Matrix](guides/validation-matrix.md) -- which checks run where
