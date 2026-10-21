# vrg-repo-profile

**Installed as:** `vrg-repo-profile` (Python console script)

Validates the repository configuration in `vergil.toml` and the
structure of `README.md`. `vrg-validate` runs it as part of its
common checks.

## Usage

```bash
vrg-repo-profile
```

Run from the repository root. The script reads `vergil.toml` and
`README.md` relative to the current directory.

## `vergil.toml` checks

`vergil.toml` is parsed with the same loader every `vrg-*` tool uses,
and any value that loader rejects fails the check. The core rules:

| Key | Rule |
|---|---|
| `[project].repository-type` | Required; one of `application`, `documentation`, `infrastructure`, `library`, `tooling` |
| `[project].versioning-scheme` | Required; one of `application`, `library`, `none`, `semver` |
| `[project].branching-model` | Required; one of `application-promotion`, `docs-single-branch`, `library-release` |
| `[project].release-model` | Required; one of `artifact-publishing`, `environment-promotion`, `none`, `tagged-release` |
| `[project].ghas` | Optional; must be a boolean |
| `[ci].versions` | Required; a non-empty list of strings |
| `[dependencies].vergil` | Required |

An unrecognized `[project].primary-language` is a warning, not a
failure: the language is treated as unset. The recognized values are
`cpp`, `go`, `java`, `python`, `rust` and `typescript`.
Unrecognized sections and keys are also reported as warnings. The
optional tables have their own references, such as
[`[container]`](../container-config.md),
[`[test]`](../test-config.md) and
[`[package]`](../package-config.md).

## `README.md` checks

When `README.md` exists, outside fenced code blocks it must have:

1. exactly one H1 heading,
2. a `## Table of Contents` heading, and
3. no heading-level skips (for example, `##` followed by `####`).

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | `vergil.toml` valid and `README.md` structure correct |
| 1 | `vergil.toml` invalid or `README.md` structure incorrect |
| 2 | `vergil.toml` not found |
