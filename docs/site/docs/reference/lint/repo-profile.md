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
| `[project].repository-type` | Required; one of `application`, `documentation`, `infrastructure`, `library`, `tooling` (see [Repository type and versions](#repository-type-and-versions)) |
| `[project].versioning-scheme` | Required; one of `application`, `library`, `none`, `semver` |
| `[project].branching-model` | Required; one of `application-promotion`, `docs-single-branch`, `library-release` |
| `[project].release-model` | Required; one of `artifact-publishing`, `environment-promotion`, `none`, `tagged-release` |
| `[project].ghas` | Optional; must be a boolean |
| `[ci].versions` | Optional pin; when set, a non-empty list of strings |
| `[ci].primary-version` | Optional; a string |
| `[dependencies].vergil` | Required |

The recognized `[project].primary-language` values are `cpp`, `go`,
`java`, `python`, `rust` and `typescript`. A retired language
(`ruby`) is rejected outright. Any other unrecognized value is a
warning, not a failure: the language is treated as unset.
Unrecognized sections and keys are also reported as warnings. The
optional tables have their own references, such as
[`[container]`](../container-config.md),
[`[test]`](../test-config.md) and
[`[package]`](../package-config.md).

## Repository type and versions

A repo's CI versions come from the language version window that
vergil-containers publishes as `window.json`, read at the repo's
`[dependencies].vergil` ref (see
[Version resolution](../../guides/ci-architecture.md#version-resolution)).
`[project].repository-type` decides how many of them the repo builds:

| Type | Resolves as | Builds |
|---|---|---|
| `library` | library | Every version in the window |
| `application`, `infrastructure`, `tooling`, `documentation` | app | One version: the newest in the window |

A library is a repo whose code runs in other repos' environments,
whether imported there or installed there as a tool, so it must work
on every supported version.

`[ci].versions` is an optional pin that replaces the default:

- every pinned version must be in the window;
- an app pin must name exactly one version;
- C++ and TypeScript versions carry a compiler or runtime family
  (`clang-20`, `gcc-14`, `node-24`), so those repos must pin.

Each violation is an error. `[ci].primary-version`, when set, must
name a resolved version; without it the primary is the newest resolved
version.

`vrg-repo-profile` checks only the shape of these keys. The window
rules are enforced by the resolver (`lib/versions/resolve.py`), which
`vrg-validate`, `vrg-container-run` and the CI setup step
(`vrg-ci-resolve`) all run.

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
