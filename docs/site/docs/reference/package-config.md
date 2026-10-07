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

[package.python]
runtime = "3.14.8"            # exact vergil-python CPython patch to build against and depend on
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
| `version` | string | repo version | Explicit package version (`[0-9][0-9A-Za-z.+~]*`), for when it is not the repo's semver (e.g. `vergil-python`) |
| `noarch` | boolean | `false` | Architecture-independent: one build cell on amd64, emitting deb `all` / rpm `noarch` |
| `exclude` | list of globs | `[]` | Targets to drop from the default set |
| `targets` | list of globs | *(unset)* | Explicit subset replacing the default set |
| `native` | list of globs | `[]` | Selected targets that get their own build cell inside their own OS |

### `[package.python]`

Valid only with `builder = "python"`, and required there.

| Key | Type | Default | Semantics |
|---|---|---|---|
| `runtime` | string | *(required)* | Exact CPython patch (`3.Y.Z`) of the `vergil-python` runtime to build against and depend on |
| `commands` | list of strings | all of `[project.scripts]` | The commands to shim into `/usr/bin`; each must be in `[project.scripts]` |

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

- the first selected target of that format on amd64, else the first on arm64
  (targets sort by key, so `rhel/10` comes before `rhel/9`);
- drawn from the shared targets; a `native` target is chosen only when the
  format has no shared target.

With the defaults, `reduced` gives the same 2 build cells and 2 test cells:
`test-rhel-10-amd64` and `test-ubuntu-24.04-amd64`. A deb-only product
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
   `vergil-python<runtime>` with the OS package manager, as a consumer would.
2. Create the venv at its final path, `/opt/<vendor>/<name>/venv`, against
   `/opt/vergil/python/<runtime>/bin/python3.Y`.
3. Install from `uv.lock` only (`uv sync --frozen --no-dev --no-editable
   --compile-bytecode`). `uv.lock` must exist next to `vergil.toml`.
4. Apply the glibc floor guard.
5. Add `/usr/bin/<cmd>` symlinks to `venv/bin/<cmd>` for each shimmed command.

The package depends on the runtime it was built against:
`vergil-python<X.Y.Z> (>= <pbs-version>)` (deb) or
`vergil-python<X.Y.Z> >= <pbs-version>` (rpm).

### `staged`

Runs the optional `build-command`, merges the overlay's files over the staging
root, and packages the result. Used for `vergil-python` itself and for the
`<org>-archive-keyring` package.

## Install layout

```text
/opt/<vendor>/python/<X.Y.Z>/      runtime (vergil-python)
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
- `builder = "python"` without `[package.python].runtime`, or a runtime that is
  not an exact `3.Y.Z` patch;
- a missing `uv.lock` for the python builder;
- `builder = "staged"` without `name`, or with neither a `build-command` nor
  overlay `contents`;
- `[package.python]` with `builder = "staged"`, or `[package.staged]` with
  `builder = "python"`;
- an overlay that is not a YAML mapping or that sets a key outside the list
  above.

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

## Inspecting the matrix

```bash
vrg-container-run -- vrg-package matrix
```

prints the resolved build and test cells as JSON without building anything.
Add `--tier reduced` to see the reduced feature-PR matrix.
