# vrg-pr-issue-linkage

**Installed as:** `vrg-pr-issue-linkage` (Python console script)

Validates that a pull request body links exactly one task with
`Ref` or `Closes`, and rejects the `Fixes`/`Resolves` auto-close
keywords. Runs in CI using the GitHub event payload.

## Usage

This script is called by CI workflows, not invoked directly. It
reads the PR body from `$GITHUB_EVENT_PATH`.

## Validation Rules

The PR body must contain exactly one line matching:

```text
Ref #123          Closes #123
Ref owner/repo#123 Closes owner/repo#123
```

The pattern allows optional leading whitespace, list markers
(`-` or `*`), and an optional colon after the keyword.

`Closes` is the sanctioned auto-close keyword: a task is exactly
one PR, so it closes on merge (`vrg-submit-pr` selects `Closes`
for managed tasks; legacy issues keep `Ref`). `Fixes`/`Resolves`
and their variants remain rejected so there is one close keyword.
This gate is a dependency-free syntax/uniqueness check; the
epic-vs-task policy lives in `vrg-submit-pr`.

## Environment

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `GITHUB_EVENT_PATH` | Yes | Path to the GitHub event JSON payload |

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Valid `Ref` or `Closes` linkage found |
| 1 | Banned keyword (`Fixes`/`Resolves`), multiple/no linkage, or empty PR body |
| 2 | `GITHUB_EVENT_PATH` not set or file not found |
