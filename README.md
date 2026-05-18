# Standard Tooling

## Table of Contents

- [Purpose](#purpose)
- [Installation](#installation)
- [CLI tools](#cli-tools)
- [Git hooks](#git-hooks)
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
git config core.hooksPath .githooks
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
- `vrg-submit-pr` — Standards-compliant PR creation with auto-merge
- `vrg-prepare-release` — Automated release preparation
- `vrg-finalize-repo` — Post-merge cleanup
- `vrg-validate` — Unified validation driver (via vrg-docker-run)
- `vrg-ensure-label` — Idempotent GitHub label creation

## Git hooks

Consumed via `git config core.hooksPath .githooks`:

- `pre-commit` — env-var-plus-`GIT_REFLOG_ACTION` gate. Admits
  `vrg-commit`-driven commits (`VRG_COMMIT_CONTEXT=1`) and derived
  workflows (`amend`, `cherry-pick`, `revert`, `rebase*`, `merge*`).
  Rejects raw `git commit`. Branch / context validation lives in
  `vrg-commit` itself.

## Releasing

Tag releases on `main` using semantic versioning. The release process
publishes both a full tag (`v1.2.0`) and a rolling `v{major}.{minor}` tag
(`v1.2`) that always points to the latest patch. Consuming repos pin to the
`v{major}.{minor}` tag in CI.
