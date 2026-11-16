# Python Coding Standards Overview

## Purpose

Define consistent Python standards that emphasize readability, maintainability,
and long-term survivability across repositories.

## Core Principles

- PEP compliance is the default and highest priority.
- Readability overrides cleverness or brevity.
- Exceptions must be explicit, documented, and justified.

## Tooling Expectations

- Default linting: ruff.
- Default type checking: mypy in strict mode and ty with default settings.
- Mypy remains authoritative until ty cutover is explicitly approved.
- If a repository uses different tools, document the reason and equivalents.
- Invoke Python as `python3` and use a project-specific environment for all
  Python commands. See `docs/development/environment-and-tooling.md`.

## Tool Ownership

Where each tool lives follows the
[Tool Ownership Guideline](../tool-ownership.md). For Python:

- The repository declares pytest, pytest-cov and any pytest plugins, because
  its tests import them.
- The image owns ruff, mypy, pip-audit and pip-licenses. This migration is
  pending
  ([vergil-project/.github#1210](https://github.com/vergil-project/.github/issues/1210)):
  repositories still declare these tools until it lands.

## CI Gates

See [Source Control Guidelines](../../source-control-guidelines.md#ci-gates)
for the hard-gates-only standard: every CI check is a hard gate, and every
version a repo resolves from the version window is a hard CI leg (see the
[Runtime Version Support Policy](../runtime-version-support-policy.md)).

Required checks for Python repositories are maintained in the
[vergil-actions CI gates documentation](https://vergil-project.github.io/vergil-actions/latest/ci-gates/required-checks/).

## Document Map

- Naming conventions: [naming-conventions.md](naming-conventions.md)
- CLI entry points: [cli-entry-points.md](cli-entry-points.md)
- Import-time side effects: [import-time-side-effects.md](import-time-side-effects.md)
- Type hints: [type-hints.md](type-hints.md)
- Testing and coverage: [testing-and-coverage.md](testing-and-coverage.md)
- Dependency management: [dependency-management.md](dependency-management.md)
- Python version management: [version-management.md](version-management.md)
- Ty migration plan: [ty-migration-plan.md](ty-migration-plan.md)
