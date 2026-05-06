# Standard Tooling

## Table of Contents

- [Purpose](#purpose)
- [Installation](#installation)
- [CLI tools](#cli-tools)
- [Git hooks](#git-hooks)
- [Releasing](#releasing)

## Purpose

Shared development tooling for all managed repositories. Structured as a
Python package with CLI entry points (`st-*`), distributed as a
host-level developer tool per
[`docs/specs/host-level-tool.md`](docs/specs/host-level-tool.md).

## Installation

### Local development

```bash
cd standard-tooling
uv sync --group dev
export PATH="$(pwd)/.venv/bin:$PATH"
git config core.hooksPath .githooks
```

### CI (GitHub Actions)

```yaml
- uses: actions/checkout@v4
  with:
    repository: wphillipmoore/standard-tooling
    ref: v1.2
    path: .standard-tooling

- name: Set up standard-tooling
  run: |
    cd .standard-tooling && uv sync --frozen
    echo "$GITHUB_WORKSPACE/.standard-tooling/.venv/bin" >> "$GITHUB_PATH"
```

## CLI tools

- `st-commit` — Standards-compliant conventional commits
- `st-submit-pr` — Standards-compliant PR creation with auto-merge
- `st-prepare-release` — Automated release preparation
- `st-finalize-repo` — Post-merge cleanup
- `st-validate` — Unified validation driver (via st-docker-run)
- `st-ensure-label` — Idempotent GitHub label creation

## Git hooks

Consumed via `git config core.hooksPath .githooks`:

- `pre-commit` — env-var-plus-`GIT_REFLOG_ACTION` gate. Admits
  `st-commit`-driven commits (`ST_COMMIT_CONTEXT=1`) and derived
  workflows (`amend`, `cherry-pick`, `revert`, `rebase*`, `merge*`).
  Rejects raw `git commit`. Branch / context validation lives in
  `st-commit` itself.

## Releasing

Tag releases on `main` using semantic versioning. The release process
publishes both a full tag (`v1.2.0`) and a rolling `v{major}.{minor}` tag
(`v1.2`) that always points to the latest patch. Consuming repos pin to the
`v{major}.{minor}` tag in CI.
