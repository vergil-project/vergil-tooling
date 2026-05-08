# Commit type cleanup: cliff regex precision, `doc`->`docs`, add `build`/`revert`

**Issue:** [#598](https://github.com/wphillipmoore/standard-tooling/issues/598)
**Date:** 2026-05-08

## Background

An audit of commit type handling revealed three problems:

1. **Imprecise cliff regexes.** All `commit_parsers` patterns use bare
   prefixes (`^feat`, `^fix`, etc.) without a delimiter. This means
   `^doc` accidentally matches `docs:` (which happens to be correct
   behavior by luck) and every pattern would false-match a hypothetical
   type that shares a prefix (e.g., `^fix` matching `fixed:`).

2. **`doc` vs `docs` inconsistency.** `st-commit` uses `docs` (the
   Conventional Commits standard). The cliff configs use `^doc`. The
   match works by accident because `^doc` is a prefix of `docs`, but
   the intent is wrong.

3. **Missing types.** `build` has a `commit_parsers` entry in neither
   cliff config despite being in `st-commit`'s `ALLOWED_TYPES`.
   `revert` is absent from both cliff configs and `ALLOWED_TYPES`.

## Scope

Commitlint (the Node.js tool proposed in the issue) is out of scope.
`st-commit` already enforces type validation at commit creation time,
making a second validator redundant. Adding a Node.js dependency to a
Python-only toolchain is not justified.

## Changes

### 1. Tighten all `commit_parsers` regexes (both cliff configs)

Replace bare prefix patterns with delimiter-aware patterns using
`[(:!]` after the type name. This matches the three valid
conventional-commit continuations:

- `:` — `type: description`
- `(` — `type(scope): description`
- `!` — `type!: description` (breaking change)

The `chore(release):` skip rule already uses a precise literal match
and does not need changes.

### 2. Fix `^doc` to `^docs[(:!]`

Both `cliff.toml` and `cliff-release-notes.toml` change the
Documentation parser from `^doc` to `^docs[(:!]`, aligning with
`st-commit`'s `ALLOWED_TYPES` and the Conventional Commits standard.

### 3. Add `build` and `revert` parser entries (both cliff configs)

| Type | Group name |
|------|-----------|
| `build` | Build |
| `revert` | Reverts |

### 4. Add `revert` to `st-commit` `ALLOWED_TYPES`

`commit.py` line 21 adds `"revert"` to the tuple. `build` is already
present.

### 5. Update documentation

`docs/repository-standards.md` line 45 adds `revert` to the type
list.

## Files changed

| File | Change |
|------|--------|
| `src/standard_tooling/configs/cliff.toml` | Tighten all regexes, fix `doc`->`docs`, add `build`/`revert` |
| `src/standard_tooling/configs/cliff-release-notes.toml` | Same as above |
| `src/standard_tooling/bin/commit.py` | Add `revert` to `ALLOWED_TYPES` |
| `docs/repository-standards.md` | Add `revert` to type list |

## Backward compatibility

Existing `doc:` commits in git history will no longer match
`^docs[(:!]`. This is acceptable: `doc:` was never a valid
conventional-commit type, and any historical `doc:` commits were
created before `st-commit` standardized on `docs`.

Existing `build:` commits already work — they just lacked a changelog
group and were silently filtered by `filter_unconventional = true`.
Adding the parser entry surfaces them.

`revert:` commits from `git revert` use Git's own format
(`Revert "original message"`), not conventional-commit format, so
they are unaffected by `commit_parsers`. Only manually-crafted
`revert:` commits through `st-commit` will match the new entry.
