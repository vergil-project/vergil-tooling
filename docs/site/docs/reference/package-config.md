# Package Config Reference (`[package]`)

A repo publishes its product as signed OS packages (`.deb` for Ubuntu, `.rpm`
for RHEL) by adding a `[package]` section to its `vergil.toml`. The section is
optional. A repo without it builds no packages, does not call `ci-package`, and
sees no change in CI or CD.

With `[package]` present:

- `vrg-validate` checks the section and the optional overlay file (see
  [Validation](#validation)).
- PR CI builds every package and install-tests it in a clean container of
  each target OS. The result is the `package / evidence` gate.
- A release builds the same packages, signs them, attaches them to the GitHub
  release, and notifies the org's package repository, which indexes them.

The design is epic
[vergil-project/.github#356](https://github.com/vergil-project/.github/issues/356)
(spec `epics/356-binary-package-publishing/spec.md`).

## Structure

```toml
[package]
builder = "python"            # python | staged
vendor  = "vergil"            # install root /opt/<vendor>/<name>/
summary = "Shared development tooling for Vergil-managed repositories"
smoke   = "vrg-whoami --mode" # the install test runs this after install

# Target selection. Default: every registered target. Set at most one of:
# exclude = ["rhel/*/arm64"]  # drop targets (glob)
# targets = ["ubuntu/*/*"]    # explicit subset replacing the default

# native  = ["ubuntu/26.04/*"]  # own build cell, inside that OS
# name    = "..."               # required for staged; python defaults to pyproject [project].name
# version = "3.14.4+20261001"   # explicit package version; default: the repo version
# noarch  = true                # architecture-independent: built once, deb `all` / rpm `noarch`

[package.python]               # optional for builder = "python"
# runtime = "3.14.8"          # exact-patch pin; default: derived from the resolved version
# commands = ["vrg-git"]      # subset of [project.scripts] to shim; default: all

# For builder = "staged" instead of [package.python]:
# [package.staged]
# build-command = "packaging/build.sh"  # populates $VRG_STAGING_ROOT for $VRG_TARGET_ARCH
```

## Keys

| Key | Type | Default | Semantics |
|---|---|---|---|
| `builder` | string | *(required)* | `python` (a Python venv product) or `staged` (any pre-built tree) |
| `vendor` | string | *(required)* | Vendor directory: the product installs under `/opt/<vendor>/<name>/` |
| `summary` | string | *(required)* | One-line package description |
| `smoke` | string | *(required)* | Shell command the install test runs after installing the package |
| `name` | string | see semantics | Package name. Required for `staged`; `python` defaults to `pyproject.toml` `[project].name` |
| `version` | string | repo version | Explicit package version (`[0-9][0-9A-Za-z.+~]*`), for when it is not the repo's semver (e.g. `vergil-python`). A release refuses a version an earlier release already published; see [the re-publish check](#the-re-publish-check) |
| `noarch` | boolean | `false` | Architecture-independent: one build cell on amd64, emitting deb `all` / rpm `noarch` |
| `exclude` | list of globs | `[]` | Targets to drop from the default set |
| `targets` | list of globs | *(unset)* | Explicit subset replacing the default set |
| `native` | list of globs | `[]` | Selected targets that get their own build cell inside their own OS |

### `[package.python]`

Valid only with `builder = "python"`, and optional there.

| Key | Type | Default | Semantics |
|---|---|---|---|
| `runtime` | string | derived | Exact-patch pin (`3.Y.Z`) on the `vergil-python` runtime. Absent, the runtime is derived (see below) |
| `commands` | list of strings | all of `[project.scripts]` | The commands to shim into `/usr/bin`; each must be in `[project.scripts]` |

`vergil-python` publishes one package per CPython series,
`vergil-python<X.Y>`, versioned `<cpython>+<pbs-tag>` (for example
`3.15.1+20261021`). Its patch floats: each release replaces the previous
patch. A package always builds against, installs and depends on the
per-series package, at `/opt/vergil/python/<X.Y>`.

With no `runtime`, the series is derived: it is the newest resolved
version whose window entry marks the runtime published (epic
[vergil-project/.github#407](https://github.com/vergil-project/.github/issues/407),
spec §7). The build takes whatever patch the series package serves. This
is the recommended setting.

A `runtime` pin keeps its `X.Y` in the window and builds on
`vergil-python<X.Y>`, never the legacy per-patch `vergil-python<X.Y.Z>`.
It is exact: after installing the series package, the build checks that
the CPython part of the installed version equals the pinned `X.Y.Z`. If
the repository serves any other patch, the build fails with the served
version and a fix: remove the pin, or update it to the served patch.
A pin cannot hold the build on an older patch, because the series
package no longer serves it. The pin governs the build only. The built
package depends on `vergil-python<X.Y> (>= <installed version>)`, so
consumers still take later patches of the series.

### `[package.staged]`

Valid only with `builder = "staged"`.

| Key | Type | Default | Semantics |
|---|---|---|---|
| `build-command` | string | *(unset)* | Command that populates `$VRG_STAGING_ROOT` (an empty directory standing in for `/`) for `$VRG_TARGET_ARCH`. A non-zero exit is a hard error |

A `staged` package needs a `build-command`, overlay `contents`, or both. A
staging root that ends up empty is a build-time error.

## Targets

Supported targets live in a registry in vergil-tooling
(`src/vergil_tooling/lib/package/targets.py`). Each target is
`<distro>/<version>/<arch>`, and the distro implies the format:

| Target | Format | Suite | glibc | Install-test image |
|---|---|---|---|---|
| `ubuntu/24.04/{amd64,arm64}` | `.deb` | `noble` | 2.39 | `ubuntu:24.04` |
| `ubuntu/26.04/{amd64,arm64}` | `.deb` | `resolute` | 2.43 | `ubuntu:26.04` |
| `rhel/9/{amd64,arm64}` | `.rpm` | `el9` | 2.34 | `registry.access.redhat.com/ubi9/ubi` |
| `rhel/10/{amd64,arm64}` | `.rpm` | `el10` | 2.39 | `registry.access.redhat.com/ubi10/ubi` |

Adding a target, such as Ubuntu 28.04, is a registry change and a
vergil-tooling release. No workflow changes.

### Matrix resolution and the 2×2 default

`vrg-package matrix` turns the config into explicit JSON lists of build cells
and test cells. The workflows consume only that JSON.

- Every non-`native` selected target on an architecture shares **one build
  cell** (`shared-amd64`, `shared-arm64`). It builds in `ubuntu:24.04` and
  emits every format its targets need.
- Each `native` target gets its own build cell (`native-<distro>-<version>-<arch>`)
  inside a container of that OS.
- A `noarch` package has exactly one build cell, `shared-noarch`, on amd64.
- Every selected target gets its own **test cell**
  (`test-<distro>-<version>-<arch>`).

With the defaults (2 distros × 2 versions × 2 architectures, no `native`) this
gives **2 build cells, 4 artifacts (a `.deb` and an `.rpm` per architecture)
and 8 test cells**. Runners are `ubuntu-24.04` for amd64 and `ubuntu-24.04-arm`
for arm64.

### Tiers: full and reduced

`vrg-package matrix --tier {full,reduced}` picks how much of the matrix runs.
The default is `full`: every build cell and every test cell, as above.

`reduced` is the cheaper gate for feature PRs (the full matrix runs on release
PRs). It keeps **every build cell**, because build breaks are cheap to catch
and common, and **one test cell per format present**:

- the **oldest** selected release of that format (compared numerically, so
  RHEL 9 before RHEL 10), on amd64, else on arm64. The oldest release is the
  strictest compatibility case: lowest glibc, oldest rpm/dnf and systemd. A
  package that installs there nearly always installs on newer releases, which
  the `full` tier still covers on release PRs;
- drawn from the shared targets; a `native` target is chosen only when the
  format has no shared target.

With the defaults, `reduced` gives the same 2 build cells and 2 test cells:
`test-rhel-9-amd64` and `test-ubuntu-24.04-amd64`. A deb-only product
(`targets = ["ubuntu/*/*"]`) gets one, `test-ubuntu-24.04-amd64`.

The printed JSON carries `"tier"`, and `--github-output` writes a `tier=` line
after `enabled`, `build` and `test`. `--manifest` is unaffected: the build
cells, and so the release artifact manifest, are the same in both tiers.

Because a shared cell builds on Ubuntu 24.04 for every target, the python
builder applies a **glibc floor guard**: any ELF object in the venv that needs
a `GLIBC_x.y` symbol newer than the lowest glibc among the cell's targets is a
hard error. Trip it, and the fix is to mark the affected targets `native`.

The package revision is `1` for shared builds, and `1~<suite>` (deb) or
`1.<suite>` (rpm) for `native` builds.

## Builders

### `python`

In each build cell, as root:

1. Enable the vergil package repository and install
   `vergil-python<X.Y>` with the OS package manager, as a consumer would.
   With a `runtime` pin, check that it serves the pinned patch.
2. Create the venv at its final path, `/opt/<vendor>/<name>/venv`, against
   `/opt/vergil/python/<X.Y>/bin/python<X.Y>`.
3. Install from `uv.lock` only (`uv sync --frozen --no-dev --no-editable
   --compile-bytecode`). `uv.lock` must exist next to `vergil.toml`.
4. Apply the glibc floor guard.
5. Add `/usr/bin/<cmd>` symlinks to `venv/bin/<cmd>` for each shimmed command.

The package depends on the runtime it was built against:
`vergil-python<X.Y> (>= <installed-version>)` (deb) or
`vergil-python<X.Y> >= <installed-version>` (rpm).

### `staged`

Runs the optional `build-command`, merges the overlay's files over the staging
root, and packages the result. Used for `vergil-python` itself and for the
`<org>-archive-keyring` package.

## Install layout

```text
/opt/<vendor>/python/<X.Y>/        runtime (vergil-python)
/opt/<vendor>/<name>/venv/         product venv
/usr/bin/<cmd>  -> venv/bin/<cmd>  command shims
/etc/<vendor>/<name>/              config (overlay, noreplace)
/var/lib/<vendor>/<name>/          state
```

A package owns every directory strictly below `/opt/<vendor>`, never
`/opt/<vendor>` itself, which is shared.

## The overlay contract

Files beyond the builder's output (systemd units, `/etc` config, maintainer
scripts) go in an optional `packaging/nfpm.overlay.yaml`, merged over the nFPM
config the tooling generates. nFPM's schema is not re-encoded in TOML.

The tooling owns the package's **identity** (`name`, `version`, `release`,
`arch` and so on). The overlay may set only these top-level keys:

| Key | Meaning |
|---|---|
| `contents` | Extra files: mappings with a string `dst` (relative `src` paths resolve against the repo root) |
| `scripts` | Maintainer scripts: script name → repo-relative path |
| `overrides` | Per-format settings under `deb` / `rpm`: `depends`, `contents`, `scripts`, and the relationship keys |
| `provides`, `conflicts`, `replaces`, `recommends`, `suggests` | Relationships: lists of strings |

`depends` is not allowed at the top level, because deb and rpm spell
dependencies differently. Put it under `overrides.deb.depends` and
`overrides.rpm.depends`.

### Systemd units: the helper-macro rule

Install tests run in plain containers without systemd as PID 1, so:

- Maintainer scripts that enable or start units **must** use the standard
  helpers, which do nothing when systemd is not running:
  `deb-systemd-helper` / `deb-systemd-invoke` on Ubuntu, and
  `/usr/lib/systemd/systemd-update-helper` (the `%systemd_post` /
  `%systemd_preun` equivalent) on RHEL.
- A raw `systemctl` call (bare or by path) in any overlay maintainer script is
  a **hard error at package build time**. Comments are ignored.
- The install test checks that each shipped unit file is installed and passes
  `systemd-analyze verify`. Whether the service actually starts and works is
  proven in a lab, not in CI.

## Validation

`vrg-validate` treats each of these as a hard error, never a warning:

- a target glob in `targets`, `exclude` or `native` that matches nothing
  (`native` must match a *selected* target);
- `exclude` and `targets` both set;
- a selection that resolves to zero targets;
- `native` combined with `noarch = true`;
- a `[package.python].runtime` that is not an exact `3.Y.Z` patch;
- a missing `uv.lock` for the python builder;
- `builder = "staged"` without `name`, or with neither a `build-command` nor
  overlay `contents`;
- `[package.python]` with `builder = "staged"`, or `[package.staged]` with
  `builder = "python"`;
- an overlay that is not a YAML mapping or that sets a key outside the list
  above.

Unrecognized keys are the exception: an unknown key in `[package]`,
`[package.python]` or `[package.staged]` only prints an
`unrecognized key` warning on stderr, and validation still passes.

`vrg-validate` checks the overlay's shape only, because nFPM is not in the dev
container. nFPM's own config check runs at build time in CI.

## CI and CD

### PR CI

Add a `package` job to `.github/workflows/ci.yml` that calls the reusable
workflow:

```yaml
  package:
    uses: vergil-project/vergil-actions/.github/workflows/ci-package.yml@v2.1
```

It runs `matrix`, then one `build` job per build cell (unsigned artifacts),
then one `install-test` job per test cell. Each install test:

1. for `python` products, enables the vergil repository as a consumer would,
   so the runtime resolves from the live repository (`staged` products install
   standalone);
2. installs the artifact and verifies any shipped systemd units;
3. runs `smoke` and resolves each shim under a sanitized environment
   (`env -i` with a system-only `PATH`), so only the packaged binaries are
   found, never the `uv` copy of vergil-tooling that drives the test;
4. removes the package and asserts nothing remains under
   `/opt/<vendor>/<name>` or in the shims.

All legs roll up into the single `package / evidence` gate. Like the other
evidence gates it is required only where it applies: the per-repo ruleset adds
`package / evidence` for repos whose `vergil.toml` has `[package]`. After
adding `[package]`, re-apply the repo's GitHub configuration so the gate
becomes required.

### CD

A release builds the same cells, then a separate `package-sign` job signs every
`.rpm` and attests every artifact before the release is tagged. Any failure is
fatal before tagging. The caller has two requirements:

- **A `package-signing` environment**, restricted to `main`, holding
  `PACKAGE_SIGNING_KEY` and `PACKAGE_SIGNING_PASSPHRASE`. Only `package-sign`
  uses it, so repos without `[package]` never reference it.
- **`secrets: inherit`** on the `cd-release` caller. Environment secrets reach
  a job in a cross-repo reusable workflow only with `inherit`; an explicit
  secrets map leaves `package-sign` with an empty key (proven by
  vergil-project/packages#6). Semgrep flags `secrets: inherit`, so suppress it
  inline with a justification:

```yaml
  release:
    if: github.ref == 'refs/heads/main'
    uses: vergil-project/vergil-actions/.github/workflows/cd-release.yml@v2.1
    with:
      language: python
      container-tag: "3.14"
    # `inherit` is required: environment secrets reach a job in a cross-repo
    # reusable workflow only with `secrets: inherit`. The callee is first-party.
    secrets: inherit  # nosemgrep: yaml.github-actions.security.secrets-inherit.secrets-inherit
```

After the release is tagged, `cd-release` dispatches to the org's `packages`
repository with the org's GitHub App token (`APP_CLIENT_ID` /
`APP_PRIVATE_KEY`, also reached through `inherit`), which indexes the new
packages. A dispatch failure is deferred: it does not fail the release.

### The re-publish check

A package's identity is its name, full version (`<version>-<revision>`),
architecture and format. Builds are not byte-reproducible, so a release
that rebuilds an identity an earlier release already published ships
different bytes under the same identity. The package index refuses that
(the same identity must be byte-identical), and since the index is built
from every release of every repo, the refusal fails every repo's index
publish. The index refusal stays as the last line of defence, but two
earlier checks stop such a release before it publishes:

1. **`vrg-release` preflight**, on a fresh start, before anything is
   tagged. This is the earliest point that knows the full versions: the
   package name, `version` (or the release version) and the revision of
   every artifact in the release manifest are all known without
   building. See
   [Releasing](../guides/releasing.md#the-package-re-publish-check).
2. **`vrg-package check-unpublished`**, for the CD packaging path to run
   before it builds, as a backstop for a release that did not go through
   `vrg-release`:

   ```bash
   vrg-package check-unpublished --version "$(vrg-version show)"
   ```

   `--repo` defaults to `$GITHUB_REPOSITORY`. It needs `gh` with read
   access to the repo's releases.

Both compare the identities the release would publish against those
attached to every earlier stable release that carries a
`packages-manifest.json`, which are the releases the index collects. The
published identities are read from the attached file names, which nFPM
derives from the same metadata the index reads. The release's own tag is
never counted as an earlier release. Any failure to read the releases
refuses the release; the check never skips. A repo without `[package]`
passes.

To fix a refusal, bump `[package].version`. For example, use a new
`+<date>` build suffix, or add a rebuild counter
(`3.14.8+20261003` → `3.14.8+20261003.1`). The revision (`-1`) is set by
the tooling and cannot be configured.

### One runtime per release

A `python` release must depend on one runtime on every architecture.
Build cells run in parallel and at slightly different times, so if each
cell read the version window itself, a window change mid-release could
give each architecture a different runtime. This happened to
vergil-tooling 2.1.258: amd64 got `vergil-python3.15` and arm64 got
`vergil-python3.14` (vergil-project/vergil-tooling#3478).

To prevent it, the runtime is resolved once:

1. **`vrg-package matrix`** resolves the runtime: the `runtime` pin, or
   the derived series. It records the result as `runtime` on every build
   cell and in `packages-manifest.json`, and prints it on stderr.
   Other builders record no runtime.
2. **`vrg-package build --runtime <runtime>`** builds against the
   recorded value and never re-reads the window. Pass the cell's
   `runtime` unchanged. An empty value counts as none recorded, so the
   same line works for every builder. A value that differs from a
   `runtime` pin is an error. Without `--runtime`, as in a local build,
   the build resolves the runtime itself. Either way it prints the
   runtime it chose and where that came from.
3. **`vrg-package check-runtime`** fails the release if any package
   disagrees. Run it where every cell's packages are together, before
   anything is published:

   ```bash
   vrg-package check-runtime --manifest packages-manifest.json --artifacts dist/packages
   ```

   Every `.deb` and `.rpm` must depend on exactly the recorded
   `vergil-python<X.Y>` and on no other runtime series. Each mismatch is
   listed. A directory with no packages fails. A manifest with no
   `runtime` (a non-`python` release) passes.

## The package index (`packages.toml`)

The org's package repository lists the products it indexes in
`packages.toml`, which `vrg-package index` reads. Retention is optional:

```toml
vendor = "vergil"
products = ["vergil-project/packages", "vergil-project/vergil-tooling"]

[retention]
keep = 3                          # releases kept per major.minor line (default 3)
lines = 2                         # newest major.minor lines kept per product (default 2)
retire = ["vergil-python3.14.8"]  # exact package names to drop (default: none)
```

The index is rebuilt from scratch on every run. For each product it keeps
the newest `keep` releases in each of the newest `lines` major.minor lines,
then adds the newest provider of every dependency of a kept package, so
everything indexed is installable.

`retire` drops packages by exact name, for every format (`deb` and `rpm`)
and architecture, whichever release ships them. A retired package is dropped
before dependencies are resolved, so it keeps none of its own dependencies in
the index. Each entry must be a non-empty string, listed once. If a package
the index keeps still depends on a retired name, the index build fails rather
than publishing a package that cannot be installed. To fix it, remove the name
from `retire` or make sure retention no longer keeps the dependent release.
Omitting `retire`, or setting it to `[]`, retires nothing.

Every `retire` entry must name a package that some collected release ships,
in any format or architecture. The check runs against everything collected
before retention drops anything, so a name found only in releases that
retention would evict still counts. An entry that matches nothing fails the
index build. It is either a typo, which would otherwise leave the package
published, or a package whose releases are gone; either way, remove it from
`packages.toml`.

## Inspecting the matrix

```bash
vrg-container-run -- vrg-package matrix
```

prints the resolved build and test cells as JSON without building anything.
Add `--tier reduced` to see the reduced feature-PR matrix.
