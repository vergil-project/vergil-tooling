# Script Reference

Standard-tooling provides Python CLI tools installed as `st-*` console
scripts, plus git hooks. For the full audit of every tool's runtime
preconditions, host-vs-container classification, and failure modes,
see the [CLI Tools Overview](cli-tools-overview.md).

## Host tools

Run on the developer's machine. Installed via `uv tool install` or
the dev-tree override venv.

| Tool | Purpose |
| ---- | ------- |
| [st-commit](dev/commit.md) | Standards-compliant commit wrapper |
| [st-submit-pr](dev/submit-pr.md) | Standards-compliant PR submission wrapper |
| [st-merge-when-green](cli-tools-overview.md#st-merge-when-green) | Poll PR checks, then merge |
| [st-prepare-release](dev/prepare-release.md) | Automated release preparation |
| [st-finalize-repo](dev/finalize-repo.md) | Post-merge repository cleanup |
| [st-ensure-label](cli-tools-overview.md#st-ensure-label) | Ensure GitHub labels exist |
| [st-docker-run](cli-tools-overview.md#st-docker-run) | Run commands inside a dev container |
| [st-docker-test](cli-tools-overview.md#st-docker-test) | Run test suite inside a dev container |
| [st-docker-docs](cli-tools-overview.md#st-docker-docs) | Preview/build MkDocs in a dev container |
| [st-generate-commands](cli-tools-overview.md#st-generate-commands) | Generate MQSC command methods |

## Container tools

Run inside dev containers launched by `st-docker-run`.

| Tool | Purpose |
| ---- | ------- |
| [st-validate-local](dev/validate-local.md) | Local validation driver |
| `st-validate-local-common` | Shared checks (repo profile, markdown, shellcheck, yamllint) |
| `st-validate-local-python` | Python-specific validation (lint, typecheck, test, audit) |
| `st-validate-local-rust` | Rust-specific validation |
| `st-validate-local-go` | Go-specific validation |
| `st-validate-local-java` | Java-specific validation |
| [st-repo-profile](lint/repo-profile.md) | Repository profile attribute validation |
| [Markdown validation](lint/markdown-standards.md) | Markdownlint with bundled canonical config |

## CI-only tools

| Tool | Purpose |
| ---- | ------- |
| [st-pr-issue-linkage](lint/pr-issue-linkage.md) | PR body issue linkage validation |

## Git Hooks

| Hook | Purpose |
| ---- | ------- |
| [pre-commit](hooks/pre-commit.md) | Env-var gate (admits `st-commit`, rejects raw `git commit`) |
