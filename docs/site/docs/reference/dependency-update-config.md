# Dependency Update Reference (`[dependency-update]`)

Every `vrg-release` ends with an `update-deps` stage that updates the
repo's dependencies to the newest versions their declared bounds allow
(see [Dependency updates](../guides/releasing.md#dependency-updates)).
[`vrg-update-deps`](dev/update-deps.md) runs the same pipeline by hand.
This page covers what that pipeline updates: the built-in updaters, the
per-repo hook a repo configures with `[dependency-update]` in
`vergil.toml`, and the held-back report.

The design is epic
[vergil-project/.github#381](https://github.com/vergil-project/.github/issues/381)
(spec `epics/381-release-dep-updates/spec.md`).

## Which mechanism do I use?

A dependency update and an [upstream refresh](upstream-refresh-config.md)
are separate pipelines. Ask one question of the input: does the repo's
code depend on it, or does the repo deliver it?

- **The code depends on it: a dependency update.** Libraries, lockfiles
  and the vergil line are what the repo is built on. They move after
  each release, at the start of the next development cycle, so a whole
  cycle runs against them before the next release ships them.
- **The repo delivers it: an upstream refresh.** Upstream content that
  the repo packages and publishes, such as vergil-python's CPython and
  python-build-standalone builds or vergil-containers' tool binaries and
  toolchains, is refreshed before a release, and the release publishes
  it. Its current users are listed on the
  [upstream refresh page](upstream-refresh-config.md#which-mechanism-do-i-use).

The current users of a dependency update, each from the `update-deps`
stage of its `vrg-release`:

- **Every repo released with `vrg-release`** keeps its
  `uses: vergil-*/…@vX.Y` refs in step with `[dependencies].vergil`
  through the `vergil` updater.
- **vergil-tooling** refreshes its `uv.lock` through the `python`
  updater.
- **vergil-vm** runs a hook (`scripts/dev/update-deps.sh`) that upgrades
  its OpenTofu providers within their ranges and bumps the nerdctl
  version within its major.

A dependency update merges its own PR once it is green, but only from a
`vrg-release` or `vrg-update-deps` that a human runs. An upstream
refresh PR merges unattended, as the org automation App, from its
scheduled job once CI passes and the path fence has allowed the change
([vergil-project/.github#1168](https://github.com/vergil-project/.github/issues/1168),
carrying out the policy decided in
[vergil-project/.github#470](https://github.com/vergil-project/.github/issues/470)).

## Updaters

The updaters run in this order. Each one runs only when the repo has
what it updates, and each one's changes become a separate commit on the
`deps/update-<version>` branch.

| Name | Runs when | Commit message |
|---|---|---|
| `python` | `pyproject.toml` and `uv.lock` exist | `chore(deps): uv lock --upgrade` |
| `cargo` | `Cargo.toml` and `Cargo.lock` exist | `chore(deps): cargo update`, listing the crates moved, or `chore(deps): refresh Cargo.lock` |
| `npm` | `package.json` and `package-lock.json` exist | `chore(deps): npm update`, listing the packages moved, or `chore(deps): refresh package-lock.json` |
| `vergil` | `vergil.toml` exists | `chore(deps): …` naming what changed |
| `images` | A Dockerfile, workflow or `action.yml` is tracked | `chore(deps): bump image tags` |
| `hook` | `[dependency-update].hook` is set | `chore(deps): repo hook` |

Bounds are always the repo's own: a dependency is never moved past what
its manifest allows. Anything newer than the bounds allow is reported in
the [held-back report](#the-held-back-report) instead. Third-party
GitHub Actions are not updated. A registry or index lookup that fails is
an error, never "nothing to update".

### `python`

Runs `uv lock --upgrade` in the dev container, which refreshes `uv.lock`
within the ranges `pyproject.toml` declares.

It then compares every directly declared requirement with its latest
release on PyPI. The direct requirements are those in
`[project] dependencies`, every dependency group, `[build-system]
requires` and `[tool.uv] constraint-dependencies`. A requirement whose
latest release falls outside its own specifier is reported as held
back. Requirements without a specifier, URL requirements and transitive
packages are never reported.

### `cargo`

Runs `cargo update` in the dev container, which moves every crate in
`Cargo.lock` to the newest version its `Cargo.toml` requirement allows.
It never edits a `Cargo.toml`.

It then reads every workspace member's normal, dev and build
dependencies from `cargo metadata`, also run in the dev container, and
compares each one's locked version with the newest version in Cargo's
sparse index (`https://index.crates.io`) that is neither yanked nor a
pre-release. A dependency locked below that version is reported as held
back, with a note giving the reason: `outside the Cargo.toml
requirement`, `needs a newer Rust than rust-version`, both, or, when
neither applies, `held by the resolver: another requirement in the
dependency graph`. A workspace-inherited dependency is checked against
the workspace's requirement. Path and git dependencies, and crates.io
dependencies patched to a path or git crate, are never reported; a
dependency from another registry, or patched to one, is an error. See
[the Cargo updater](dev/update-deps.md#the-cargo-updater).

### `npm`

Runs `npm update --ignore-scripts` in the dev container, which moves
every package in `package-lock.json` to the newest version its
`package.json` range allows. It never edits `package.json`, and fails if
`package.json` changed, as a `save` setting in `.npmrc` would make it.
Every entry it added or moved must carry a `license`, or it fails.

It then runs `npm ci --ignore-scripts` and `npm outdated --json` in the
dev container, so npm matches the ranges itself. A direct dependency
(`dependencies`, `devDependencies` or `optionalDependencies`) locked
below its `latest` dist-tag is reported as held back, with a note giving
the reason: `outside the package.json range`, `held by the resolver:
another requirement in the dependency graph`, or both, plus `Vergil
accepts <name> <major> only (dev-dependencies.json)` for a package whose
major Vergil fixes. `file:`, `link:`, git and URL dependencies are never
reported. See [the npm updater](dev/update-deps.md#the-npm-updater).

### `vergil`

Keeps the vergil ecosystem references consistent with
`[dependencies].vergil` in `vergil.toml`:

- Rewrites drifting `uses: vergil-*/…@vX.Y` refs in
  `.github/workflows/` and in every `action.yml`/`action.yaml` in the
  repo. It never touches `.claude/settings.json`, which carries no
  marketplace entry.
- With [`vrg-update-deps --vergil X.Y`](dev/update-deps.md), first
  sets `[dependencies].vergil` to `vX.Y`, then rewrites the refs to
  match.

For a repo with an exact `[package.python] runtime` pin such as `3.14.8`
(see [Package Config](package-config.md)), it also bumps the pin to the
CPython patch that the pin's series package, `vergil-python3.14`, serves.
That package's version is `<cpython>+<pbs_tag>.<release>` (for example
`3.14.9+20261021.1.0.4`, where `1.0.4` is the vergil-python release;
releases before v1.0.4 have no `.<release>` suffix), and the patch is
the part before the `+`. Each
index installs the newest version it lists, and the patch counts only
when every apt and rpm index the package build and install test read
from serves the same one, so the bump cannot pass validation and then
break the next release.

The pin never leaves its series. A newer series, such as
`vergil-python3.15`, is reported as held back and never applied; moving
to it is a reviewed change.

The deb and rpm indexes publish separately, so right after a
vergil-python release they can briefly serve different patches. While
they disagree the pin is left unchanged and the held-back report names
each patch and the indexes serving it, for example
`indexes disagree (3.14.9 on deb noble/amd64; 3.14.8 on rpm el9/x86_64);
bump held until they agree`. A later run bumps the pin once they agree.

The update fails loudly, rather than reporting "no change", when:

- any of those indexes lacks the series package, or cannot be read.
  The legacy per-patch packages (`vergil-python3.14.8`) are retired and
  are not read;
- a series package version is not `<cpython>+<pbs_tag>`, optionally
  followed by `.<release>`;
- the series package serves a patch on another series, or a patch
  older than the pin.

### `images`

Updates container image tags in Dockerfile `FROM` lines, workflow
`container:` and `image:` values, and the defaults of `action.yml`
inputs whose names end in `image`. Tags are looked up anonymously in
the image's registry.

| Tag shape | Example | What happens |
|---|---|---|
| Exact `x.y.z` (optional `v` prefix) | `0.71.0`, `v1.2.3` | Bumped to the newest tag with the same major. A newer major is reported, never bumped |
| Numeric series | `3.14-slim`, `24.04`, `16` | Never bumped. A newer series with the same suffix is reported |
| `latest`, digests, expressions | `${{ inputs.image }}`, `$BASE` | Skipped |
| `ghcr.io/vergil-project/*` | | Skipped |
| Anything else | `trixie-slim` | Skipped |

Toolchain and language majors are therefore only ever reported.

### `hook`

The per-repo hook, configured below. It always runs last, so it sees
the lockfiles the built-in updaters have already refreshed.

## `[dependency-update]`

```toml
[dependency-update]
hook = "scripts/dev/update-deps.sh"
```

| Key | Type | Default | Semantics |
|---|---|---|---|
| `hook` | string | *(required)* | Command to run after the built-in updaters. Must be non-empty |

The table is optional; without it, no hook runs. When the table is
present, `hook` is required. `hook` is the only key; any other key is
reported as an unrecognised-key warning.

Use the hook for what the built-in updaters do not cover, such as
binary tool versions or provider pins. Keep repo-specific update logic
in the repo, not in vergil-tooling.

This hook is separate from the [`[upstream-refresh]`](upstream-refresh-config.md)
hook: `vrg-update-deps` never runs the upstream-refresh hook, and
`vrg-upstream-refresh` never runs this one.

### The hook contract

The hook runs from the root of the update worktree, inside the dev
container:

```text
vrg-container-run -- env VRG_DEPS_REPORT=/workspace/.vergil/deps-report.jsonl <hook>
```

The `hook` string is split into words the way a shell would, but it is
not run through a shell. To use pipes, `&&` or other shell syntax, put
them in a script and point `hook` at the script.

- **Edit files; do not commit.** The driver commits the hook's changes
  as `chore(deps): repo hook`. If the hook moves `HEAD`, the update
  fails.
- **Exit 0 on success.** A non-zero exit fails the update. It is never
  treated as "nothing to update".
- **Report what you hold back** by appending lines to
  `$VRG_DEPS_REPORT` (see below). The file starts empty on each run.

A hook failure is an `update` failure: nothing is pushed and no PR is
opened, but a fix task naming the hook is filed.

## Package changes

The dependency-update PR body lists what each updater changed, after
its **Updated** section. Each updater that changed something gets a
count, and every package it moved is a row in one table:

```markdown
- **python** — 2 packages
- **hook** — not itemized

| Updater | Package | From | To |
|---|---|---|---|
| python | requests | 2.31.0 | 2.32.0 |
| python | idna | (new) | 3.10 |
```

Each updater takes its list from a machine-readable source:

- `python` diffs the package versions in the parsed `uv.lock` before and
  after `uv lock --upgrade`, transitive packages included. A package
  added or dropped shows `(new)` or `(removed)`.
- `cargo` diffs the crate versions in the parsed `Cargo.lock` before and
  after `cargo update` the same way. A crate locked at several versions
  shows them comma-joined.
- `npm` diffs the package versions in the parsed `package-lock.json`
  before and after `npm update` the same way. A package installed at
  several versions (nested copies) shows them comma-joined.
- `vergil` lists the `vergil` version it bumped and the
  `vergil-python<x.y>` runtime pin it moved. Normalizing workflow refs to
  the declared version moves no package.
- `images` lists each image ref whose tag it bumped.
- `hook` is not itemized: its edits are the repo's own.

An updater that changed files but moved no package version says "no
package versions changed".

## The held-back report

The held-back report lists the dependencies whose newest release is
outside the bounds the repo declares. It appears in the dependency-update
PR body, in the `update-deps` comment on the release tracking issue, and
at the end of every `vrg-update-deps` run, as a table:

| Source | Dependency | Current | Latest | Note |
|---|---|---|---|---|
| python | example-lib | 1.4.2 | 2.0.0 | project.dependencies: example-lib<2 |
| cargo | serde | 1.0.200 | 2.0.0 | outside the Cargo.toml requirement |

When nothing is held back it says "No dependencies are held back by
their declared bounds."

A hook adds entries by writing one JSON object per line to
`$VRG_DEPS_REPORT`:

```json
{"source": "tofu", "name": "hashicorp/aws", "current": "5.80.0", "latest": "6.2.0", "note": "provider pinned to ~> 5.0"}
```

| Key | Required | Type |
|---|---|---|
| `source` | Yes | string |
| `name` | Yes | string |
| `current` | Yes | string |
| `latest` | Yes | string |
| `note` | No | string |

Blank lines are ignored. Every other line must be a JSON object that
has all the required keys, no other keys, and only string values. A
malformed line fails the update, and the error names the line number.
