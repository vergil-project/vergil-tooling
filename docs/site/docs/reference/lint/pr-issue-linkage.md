# vrg-pr-issue-linkage

**Installed as:** `vrg-pr-issue-linkage` (Python console script)

Validates that a pull request body uses `Ref` for issue linkage
and rejects auto-close keywords. Runs in CI using the GitHub event
payload.

## Usage

This script is called by CI workflows, not invoked directly. It
reads the PR body from `$GITHUB_EVENT_PATH`.

## Validation Rules

The PR body must contain at least one line matching:

```text
Ref #123
Ref owner/repo#123
```

The pattern allows optional leading whitespace, list markers
(`-` or `*`), and an optional colon after the keyword.

Auto-close keywords (`Fixes`, `Closes`, `Resolves` and all
variants like `Fix`, `Fixed`, `Close`, `Closed`, `Resolve`,
`Resolved`) are rejected. Issues must remain open until
post-merge workflows succeed; premature closure loses tracking
for multi-PR and multi-repo work.

## Environment

| Variable | Required | Description |
| -------- | -------- | ----------- |
| `GITHUB_EVENT_PATH` | Yes | Path to the GitHub event JSON payload |

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | Valid `Ref` linkage found |
| 1 | Auto-close keyword used, no linkage, or empty PR body |
| 2 | `GITHUB_EVENT_PATH` not set or file not found |
