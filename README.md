# Standard Tooling

## Table of Contents

- [Purpose](#purpose)
- [Installation](#installation)
- [CLI tools](#cli-tools)
- [Claude Code hook guard](#claude-code-hook-guard)
- [Releasing](#releasing)

## Purpose

Shared development tooling for all managed repositories. Structured as a
Python package with CLI entry points (`vrg-*`), distributed as a
host-level developer tool per
[`docs/specs/host-level-tool.md`](docs/specs/host-level-tool.md).

## Installation

### Local development

```bash
cd vergil-tooling
uv sync --python 3.15 --group dev
export PATH="$(pwd)/.venv/bin:$PATH"
```

### CI (GitHub Actions)

The vergil-actions reusable workflows install vergil-tooling themselves,
through the
[`shared/setup/vergil`](https://github.com/vergil-project/vergil-actions/blob/develop/docs/site/docs/actions/shared-setup-vergil.md)
action, at the version `[dependencies].vergil` in `vergil.toml` names, on
Python 3.15. A consuming repo adds no install step of its own.

## CLI tools

- `vrg-commit` — Standards-compliant conventional commits
- `vrg-submit-pr` — Standards-compliant PR creation with auto-merge on,
  finalizing by default
- `vrg-release` — Mechanized end-to-end release workflow, ending with
  the dependency update
- `vrg-update-deps` — Update dependencies within their declared bounds,
  validate, and merge the result (also run by every `vrg-release`)
- `vrg-upstream-refresh` — Run the repo's `[upstream-refresh]` hook, check
  the patch against its path fence, and open a PR that merges itself once
  CI passes (run on a schedule)
- `vrg-finalize-pr` — Watch a batch of auto-merging PRs until they merge,
  then sync, clean up and validate develop once
- `vrg-validate` — Unified validation driver (via vrg-container-run)
- `vrg-ensure-label` — Idempotent GitHub label creation
- `vrg-package` — Build, install-test and index binary OS packages
  (`.deb`/`.rpm`) from `[package]` in `vergil.toml`
- `vrg-host` — Report, upgrade and pin the macOS host dependencies
  (`status`, `upgrade`, `pin`, `unpin`)

Lima and cloud agent VMs install vergil-tooling as a signed apt package
rather than with `uv`; see the
[`vergil-project/packages` README](https://github.com/vergil-project/packages#readme).

## Claude Code hook guard

Raw `git` and `gh` commands are blocked in AI agent sessions by
`vrg-hook-guard`, a Claude Code `PreToolUse` hook. Each repo ships a
thin shell shim at `.claude/hooks/guard.sh` that calls
`vrg-hook-guard` to enforce the `vrg-git`/`vrg-gh` wrapper policy.

## Releasing

Tag releases on `main` using semantic versioning. The release process
publishes both a full tag (`v1.2.0`) and a rolling `v{major}.{minor}` tag
(`v1.2`) that always points to the latest patch. Consuming repos pin to the
`v{major}.{minor}` tag in CI.
