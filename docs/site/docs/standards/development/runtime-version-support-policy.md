# Runtime Version Support Policy

## Purpose

Define which language versions Vergil-managed repositories build and test,
where that set comes from, and how it changes. This policy is cross-language;
language pages such as
[Python version management](python/version-management.md) apply it.

## Scope

Applies to every repository that declares a `[project].primary-language` in
`vergil.toml`: Python, Java, Go, Rust, C++ and TypeScript.

## Definitions

- **GA release**: A production-ready version published through the language's
  official release channel. Pre-releases (alphas, betas, release candidates)
  are never GA.
- **EOL (end of life)**: A release series no longer receiving any patches from
  its upstream.
- **Version window**: The set of versions of a language that Vergil supports
  and builds images for, published by vergil-containers.
- **Hard gate**: A CI check that blocks PR merge when it fails. Every CI gate
  is a hard gate. See
  [Source Control Guidelines](../source-control-guidelines.md#ci-gates) and the
  [CI Architecture](../../guides/ci-architecture.md) guide.

## The version window

The window is recorded in one place: the version manifest in
[vergil-containers](https://github.com/vergil-project/vergil-containers),
`docker/versions/manifest.yml`. vergil-containers generates every other version
list from it, including `docker/versions/window.json`, the published window
that consuming repos read at their `[dependencies].vergil` ref.

Each language's window follows its upstream community's own support rule. The
shared rules and each language's policy page live in vergil-containers'
[language version lifecycle](https://github.com/vergil-project/vergil-containers/blob/develop/docs/site/docs/languages/lifecycle.md)
docs. In short:

- a version joins the window once it is GA, and leaves it at upstream EOL with
  no exceptions;
- the window grows only at the top: an older version is never backfilled;
- versions are compared at `x.y` (or at the major, for languages numbered by
  whole majors), so any patch release satisfies a version.

## The lifecycle job

The lifecycle job in vergil-containers adds and drops versions. For each
language it reads upstream release and EOL data, applies that language's
policy and computes the target window. For a language whose automation is
`pr` it opens the pull request that changes the manifest; for one whose
automation is `report` it reports the gap in an issue until a human acts.

Repos do not edit anything when the window moves, and there are no per-repo
tiers, preview periods or job labels to maintain.

## Which versions a repo builds

One resolver (`src/vergil_tooling/lib/versions/resolve.py`) turns a repo's
`vergil.toml` and the window into the versions it builds. See
[Version resolution](../../guides/ci-architecture.md#version-resolution).

### Libraries

A repo with `repository-type = "library"` provides code that runs in other
repos' environments, so it must work on every version its consumers can be on.

- Default: every window version.
- Every window version is a **hard CI leg**. There is no advisory tier.
- A `[ci].versions` pin may narrow the set to any non-empty subset of the
  window.

### Applications

Every other repository type resolves as an app: it ships its own environment
and builds one version.

- Default: the newest window version.
- A `[ci].versions` pin must name exactly one window version. Pinning more than
  one is an error; a repo that needs several versions is a library.
- A packaged Python app resolves to the newest version that has a published
  `vergil-python` runtime (see
  [Python version management](python/version-management.md#packaged-runtime)).

### Pins

`[ci].versions` is optional. A pin is visible technical debt, not a tier:

- every pinned version must be in the window, so a pin on a dropped version
  fails the repo's own build;
- C++ and TypeScript versions carry a compiler or runtime family (`clang-20`,
  `gcc-14`, `node-24`), so those repos must pin;
- `[ci].primary-version` chooses the primary among the resolved versions;
  without it the primary is the newest.

## Moving forward

A new window version reaches every unpinned repo on its next build. If it
breaks a repo, fix forward, or pin and file an issue. A version that leaves the
window leaves every unpinned repo's matrix the same way.

When a version leaves a library's matrix, remove the compatibility shims,
conditional code paths and dependency pins that existed only for it, and raise
the `requires-python` floor (or the language's equivalent) to the new oldest
version. `vrg-validate` warns when a declared floor falls below the window's
oldest version.

## Per-language support rules

The window's contents are in the manifest; this page does not repeat them. The
rule each language's window follows, from its vergil-containers policy page:

| Language | Support rule |
| --- | --- |
| Python | Every GA minor that is not EOL |
| Go | The two newest releases |
| Rust | The newest stable release and the one before it (N-1) |
| Java | LTS releases that are not EOL |
| TypeScript (Node.js) | LTS majors that are not EOL |
| C++ (Clang) | The newest LLVM major and the one before it |
| C++ (GCC) | The maintained series that the image's base distribution packages |

Rust's upstream supports only the newest stable release. Vergil keeps one
more (N-1) to absorb the six-week cadence, so a consumer is not forced to move
on the day a release ships.

A language whose automation is `report` can lag its rule until a human applies
the reported change, so its manifest entry, not the rule, is what repos build.

## Related documents

- Python version management:
  [version-management.md](python/version-management.md)
- Deprecation warning policy:
  [deprecation-warnings.md](deprecation-warnings.md)
- Source code management guidelines:
  [source-control-guidelines.md](../source-control-guidelines.md)
