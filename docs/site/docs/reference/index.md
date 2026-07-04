# Script Reference

Vergil-tooling provides Python CLI tools installed as `vrg-*` console
scripts, plus git hooks. For the full audit of every tool's runtime
preconditions, host-vs-container classification, and failure modes,
see the [CLI Tools Overview](cli-tools-overview.md).

## Host tools

Run on the developer's machine. Installed via `uv tool install` or
the dev-tree override venv.

| Tool | Purpose |
| ---- | ------- |
| [vrg-commit](dev/commit.md) | Standards-compliant commit wrapper |
| [vrg-submit-pr](dev/submit-pr.md) | Standards-compliant PR submission wrapper |
| [vrg-merge-when-green](cli-tools-overview.md#vrg-merge-when-green) | Poll PR checks, then merge |
| [vrg-prepare-release](dev/prepare-release.md) | Automated release preparation |
| [vrg-finalize-pr](dev/finalize-pr.md) | Merge a PR and run post-merge cleanup |
| [vrg-ensure-label](cli-tools-overview.md#vrg-ensure-label) | Ensure GitHub labels exist |
| [vrg-container-run](cli-tools-overview.md#vrg-container-run) | Run commands inside a dev container |
| [vrg-container-test](cli-tools-overview.md#vrg-container-test) | Run test suite inside a dev container |
| [vrg-container-docs](cli-tools-overview.md#vrg-container-docs) | Preview/build MkDocs in a dev container |
| [vrg-generate-commands](cli-tools-overview.md#vrg-generate-commands) | Generate MQSC command methods |

## Container tools

Run inside dev containers launched by `vrg-container-run`.

| Tool | Purpose |
| ---- | ------- |
| [vrg-validate](cli-tools-overview.md#vrg-validate) | Unified validation driver (common + language-specific checks) |
| [vrg-repo-profile](lint/repo-profile.md) | Repository profile attribute validation |
| [Markdown validation](lint/markdown-standards.md) | Markdownlint with bundled canonical config |

## Git Hooks

| Hook | Purpose |
| ---- | ------- |
| [pre-commit](hooks/pre-commit.md) | Env-var gate (admits `vrg-commit`, rejects raw `git commit`) |
