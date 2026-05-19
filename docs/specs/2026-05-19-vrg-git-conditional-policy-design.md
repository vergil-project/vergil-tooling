# vrg-git conditional policy relaxation

**Issues:** #827, #845
**Date:** 2026-05-19

## Summary

Relax two blanket denials in `vrg-git` with conditional checks that
allow safe operations in verified contexts while continuing to deny
dangerous ones.

## Change 1: `--force-with-lease` on non-protected branches (#827)

### Problem

`vrg-git` denies all force push variants. After rebasing a feature
branch onto develop, the agent cannot push the rewritten history and
must ask the human to run raw `git push --force-with-lease`.

### Design

- `-f` and `--force` remain unconditionally denied.
- `--force-with-lease` is allowed when the current branch is not
  protected.
- Protected branches: `develop`, `main`, and any branch starting
  with `release/`.
- Branch detection: inline `git rev-parse --abbrev-ref HEAD`
  (vrg-git is self-contained stdlib-only; no import of
  `vergil_tooling.lib.git`).

### New function

`_is_protected_branch() -> bool` — runs `git rev-parse --abbrev-ref
HEAD` and returns `True` if the result is `develop`, `main`, or
starts with `release/`.

### Modified behavior in `_check_denied_flags`

When `subcmd == "push"` and the flag is `--force-with-lease`:

1. Call `_is_protected_branch()`.
2. If protected, deny with message: `"push --force-with-lease is
   denied on protected branch <name>."`
3. If not protected, allow (return `None` for this flag).

`-f` and `--force` bypass this check entirely and remain denied.

## Change 2: `branch -D` when upstream is gone (#845)

### Problem

After a squash merge on GitHub, the remote branch is deleted but
`git branch -d` fails locally because git cannot detect the squashed
commits as merged. `git branch -D` is needed but vrg-git denies it.

### Design

- `--force` on branch remains unconditionally denied.
- `-D <branch-name>` is allowed when the target branch's upstream
  tracking ref shows `[gone]` in `git branch -vv` output.
- If `-D` is used without a branch name, denied.
- If the branch has no upstream or the upstream is not `[gone]`,
  denied.

### New function

`_is_upstream_gone(branch_name: str) -> bool` — runs `git branch
-vv`, finds the line whose first non-whitespace token matches
`branch_name`, and returns `True` if the line contains `[gone]`.

### Modified behavior in `_check_denied_flags`

When `subcmd == "branch"` and `-D` is in the args:

1. Find the branch name argument (the token after `-D`).
2. Call `_is_upstream_gone(branch_name)`.
3. If upstream is gone, allow.
4. Otherwise deny with message: `"branch -D is denied (upstream is
   not gone for <name>)."`

If no branch name can be determined, deny with the existing message.

### Edge cases

| Scenario | Result |
|---|---|
| `-D` with no branch argument | Denied |
| Branch never pushed (no upstream) | Denied |
| Branch upstream exists and is active | Denied |
| Branch upstream is `[gone]` | Allowed |
| `--force` on branch (any context) | Denied |

## Architecture

Both checks are private functions in `vrg_git.py`. No changes to:

- The public interface (`main()` signature)
- The log format (still logs `allowed`/`denied`)
- The `_FLAG_DENY` dict structure (push and branch entries remain
  but are handled by the conditional path before the generic check)

### Why not import `lib.git`?

`vrg_git.py` is a security boundary. It deliberately imports only
stdlib modules. Adding a dependency on `lib.git` would widen the
attack surface and create a circular concern (the safety wrapper
depending on the library it wraps). Small inline helpers are the
right trade-off.

## Testing

Existing tests that assert blanket denial of `--force-with-lease`
and `-D` will be updated to reflect the conditional behavior.

New test cases:

### Push force-with-lease

- Allowed on `feature/123-foo` (non-protected, mocked branch
  detection)
- Denied on `develop` (protected)
- Denied on `main` (protected)
- Denied on `release/2.0.22` (protected, prefix match)
- `-f` and `--force` remain denied regardless of branch

### Branch -D

- Allowed when upstream is `[gone]` (mocked `git branch -vv` output)
- Denied when upstream is active
- Denied when branch has no upstream
- Denied when no branch name provided after `-D`
- `--force` remains denied regardless of upstream status
