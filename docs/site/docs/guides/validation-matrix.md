# Validation Matrix

This page maps every validation check to where it runs, its trigger,
and its exit codes.

## Check Summary

| Check | Hook | CI | Script |
| ----- | ---- | -- | ------ |
| Branch naming | Yes | -- | `pre-commit` |
| Repository profile | -- | Yes | `st-repo-profile` |
| Markdown standards | -- | Yes | `st-validate-local-common` |
| PR issue linkage | -- | Yes | `st-pr-issue-linkage` |
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

### st-repo-profile

**Trigger:** PR opened or updated

Validates `docs/repository-standards.md` has all six required
attributes.

### Markdown validation (st-validate-local-common)

**Trigger:** PR opened or updated

Runs markdownlint on published markdown (`docs/site/**/*.md` and
`README.md`) using the canonical config bundled in standard-tooling.
See the [Markdown Validation](../reference/lint/markdown-standards.md)
reference for config details and file scope.

### st-pr-issue-linkage

**Trigger:** PR opened or updated

Validates the PR body contains `Fixes #N`, `Closes #N`,
`Resolves #N`, or `Ref #N`.

## Exit Code Reference

| Code | Meaning | Scripts |
| ---- | ------- | ------- |
| 0 | Success | All scripts |
| 1 | Validation failure | All scripts |
| 2 | Usage error | Most lint scripts (missing args or file) |
