# Validation Matrix

This page maps every validation check to where it runs, its trigger,
and its exit codes.

## Check Summary

| Check | Hook | CI | Script |
| ----- | ---- | -- | ------ |
| Branch naming | Yes | -- | `pre-commit` |
| Repository profile | -- | Yes | `vrg-repo-profile` |
| Markdown standards | -- | Yes | `vrg-validate` (common checks) |
| PR issue linkage | -- | Yes | `vrg-pr-issue-linkage` |
| Shellcheck | -- | Yes | CI workflow step |

## Local Hooks

### pre-commit

**Trigger:** Every `git commit`

| Check | Error Message | Fix |
| ----- | ------------- | --- |
| Detached HEAD | `detached HEAD is not allowed` | Create a branch |
| Protected branch | `direct commits...forbidden` | Create a feature branch |
| Bad prefix | `branch name must use...` | Rename branch |
| Missing issue | `must include a repo issue` | Rename to `type/123-desc` |

## CI Checks

### vrg-repo-profile

**Trigger:** PR opened or updated

Validates `docs/repository-standards.md` has all six required
attributes.

### Markdown validation (vrg-validate)

**Trigger:** PR opened or updated

Runs markdownlint on published markdown (`docs/site/**/*.md` and
`README.md`) using the canonical config bundled in vergil-tooling.
See the [Markdown Validation](../reference/lint/markdown-standards.md)
reference for config details and file scope.

### vrg-pr-issue-linkage

**Trigger:** PR opened or updated

Validates the PR body links exactly one task with `Ref #N` or
`Closes #N`. `Closes` auto-closes a task on merge (a task is one
PR); `Fixes`/`Resolves` and variants are rejected so there is
one sanctioned close keyword.

## Exit Code Reference

| Code | Meaning | Scripts |
| ---- | ------- | ------- |
| 0 | Success | All scripts |
| 1 | Validation failure | All scripts |
| 2 | Usage error | Most lint scripts (missing args or file) |
