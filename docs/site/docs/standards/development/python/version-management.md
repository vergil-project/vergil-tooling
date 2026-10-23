# Python Version Management

## Purpose

Define how a Vergil-managed repository chooses the Python versions it builds,
tests and deploys on, and how those versions change over time.

## Scope

These rules apply to the Python interpreter used in development, CI, and
packaged deployments. The cross-language model is in the
[Runtime Version Support Policy](../runtime-version-support-policy.md); this
page applies it to Python.

## Core principles

- **Vergil owns the version.** A repo does not choose its interpreter. The
  supported versions come from one published version window, and every tool
  that runs Python reads them from it.
- **One source, no hand-maintained lists.** The window is generated from the
  version manifest in vergil-containers. Nothing in a consuming repo, its
  `ci.yml` or its container setup repeats the version list.
- **Moving forward is the default.** A new window version reaches every repo
  that does not pin on its next build. Pinning is an explicit, visible choice.
- **Development, CI and deployment agree.** The local container, every CI leg
  and the packaged runtime all come from the same window.

## Sources of truth

- **The version manifest.** vergil-containers records every supported Python
  version in `docker/versions/manifest.yml` and publishes it as
  `docker/versions/window.json` at every Vergil ref (the *version window*).
  The current Python window is 3.12, 3.13, 3.14 and 3.15. The Python support
  rule (every GA minor that is not EOL, with no backfill) is set out in
  vergil-containers'
  [Python support policy](https://github.com/vergil-project/vergil-containers/blob/develop/docs/site/docs/languages/python.md).
- **The repo's `vergil.toml`.** `[project].repository-type` decides how many
  window versions the repo builds, and `[ci].versions` optionally pins them
  (see below). The window is read at the repo's `[dependencies].vergil` ref
  (for example `v2.1`), so a window change reaches the repo without a
  vergil-tooling release.
- **One resolver.** `src/vergil_tooling/lib/versions/resolve.py` turns the two
  into the versions the repo builds and its primary version. The dev container,
  `vrg-validate`, the required-check computation and the CI setup step
  (`vrg-ci-resolve`) all call it. See
  [Version resolution](../../../guides/ci-architecture.md#version-resolution).

## Which versions a repo builds

| Repository type | Default | `[ci].versions` pin |
| --- | --- | --- |
| `library` | Every window version | Any non-empty subset of the window |
| Any other type (an app) | The newest window version | Exactly one window version |

- A library's code runs in other repos' environments, so its CI builds every
  version a consumer can be on. Every window version is a hard CI leg.
- An app ships its own environment, so it builds one version. Pinning more
  than one version is an error: if a repo really needs several, it is a
  library and should say so.
- A pin outside the window is an error. When a pinned version leaves the
  window, the repo fails its own build, which is how end of life reaches a
  repo.
- `[ci].primary-version` sets the primary (the local container's version and
  the version single-container CI jobs use). Without it, the primary is the
  newest resolved version.

vergil-tooling itself is a `library` with no pin: its CI covers 3.12 to 3.15
and local validation runs on 3.15.

## Toolchain selection in the repo

`vrg-validate` checks the repo's own Python settings against the window
(`src/vergil_tooling/lib/versions/extensions/python.py`):

- **`.python-version` is an error.** It selects the interpreter version behind
  Vergil's back. Delete it.
- **A `requires-python` floor below the window's oldest version, or an upper
  bound that excludes its newest, is a warning.** A floor above the oldest
  version is a deliberate restriction and is not reported. Keep
  `requires-python` as a floor with no upper bound.
- **uv's `python-preference` and `python-downloads` are allowed.** They choose
  where an interpreter comes from, not which version, and Vergil overrides the
  source wherever it runs Python.

Vergil hands uv the resolved version itself. The dev container, the
cached-image build and every CI leg set `UV_PYTHON` to the resolved version
and `UV_PYTHON_PREFERENCE=only-system`, so uv always uses the image's own
interpreter. The docs builds resolve no version and set only
`UV_PYTHON_PREFERENCE=only-system`. See
[Container Config](../../../reference/container-config.md).

## Packaged runtime

A repo with `[package].builder = "python"` deploys on a `vergil-python`
runtime. The runtime is published **per series**: the package
`vergil-python<x.y>` installs at `/opt/vergil/python/<x.y>` and floats on the
newest CPython patch of that series.

- **Derived by default.** Without a pin, the runtime series is the newest
  resolved version whose `vergil-python` runtime is published (the window
  records this as `runtime_published`). Today that is 3.15. A packaged app
  resolves only to a version that has a published runtime; a packaged library
  still tests every resolved version, and only the derived series needs a
  runtime.
- **A pin is an exact patch check.** `[package.python].runtime = "x.y.z"`
  selects the `x.y` series, and the build fails unless the per-series package
  serves exactly that CPython patch
  ([#3455](https://github.com/vergil-project/vergil-tooling/issues/3455)).
  The pinned series must be in the window.
- **`vrg-update-deps` moves a pin within its series.** The `vergil` updater
  bumps the pin to the patch every build and install-test index serves, and
  holds it back while the indexes disagree. A newer series is reported as held
  back, never applied
  ([#3456](https://github.com/vergil-project/vergil-tooling/issues/3456); see
  the [dependency-update reference](../../../reference/dependency-update-config.md#vergil)).

See the [package config reference](../../../reference/package-config.md) for
the `[package.python]` keys.

## How versions change

The lifecycle job in vergil-containers adds and drops Python versions. When a
new minor is GA it opens the pull request that adds it to the manifest; when a
minor reaches end of life it opens the one that drops it. Once the change is
in the published window:

- an unpinned library gains or loses a CI leg on its next build;
- an unpinned app moves to the newest version on its next build (a packaged
  app moves once that version's runtime is published);
- a repo that pins a dropped version fails until it updates the pin.

There is no per-repo preview period, tier label or cutover step. A repo that
breaks on a new version fixes forward, or pins and files an issue; a pin is
visible technical debt.

## Related documents

- Runtime version support policy:
  [runtime-version-support-policy.md](../runtime-version-support-policy.md)
- CI architecture:
  [Version resolution](../../../guides/ci-architecture.md#version-resolution)
- Python dependency management: [dependency-management.md](dependency-management.md)
- Dependency anchor records: [dependency anchor records](../../dependency-anchor-records.md)
