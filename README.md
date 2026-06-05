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
uv sync --group dev
export PATH="$(pwd)/.venv/bin:$PATH"
```

### CI (GitHub Actions)

```yaml
- uses: actions/checkout@v4
  with:
    repository: vergil-project/vergil-tooling
    ref: v1.2
    path: .vergil-tooling

- name: Set up vergil-tooling
  run: |
    cd .vergil-tooling && uv sync --frozen
    echo "$GITHUB_WORKSPACE/.vergil-tooling/.venv/bin" >> "$GITHUB_PATH"
```

## CLI tools

- `vrg-commit` — Standards-compliant conventional commits
- `vrg-submit-pr` — Standards-compliant PR creation (manual merge)
- `vrg-release` — Mechanized end-to-end release workflow
- `vrg-finalize-pr` — Merge a PR and run post-merge cleanup
- `vrg-validate` — Unified validation driver (via vrg-container-run)
- `vrg-ensure-label` — Idempotent GitHub label creation

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
