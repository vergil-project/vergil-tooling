# Container Config Reference (`[container]`)

A repo customizes its **dev container** through the `[container]` section of its
`vergil.toml`. The section is optional; every key defaults to empty, so a repo
that omits `[container]` behaves exactly as it would with an empty table.

The keys describe the *same* container from a few angles: `env-prefixes` controls
which host environment variables `vrg-container-run` forwards **into** the
container, `system-packages` declares Debian packages installed **inside** it, and
`build-command` runs a repo-declared provisioning step when the cached dev image
is built (with `build-cache-files` naming the inputs that invalidate the cache).

## Structure

```toml
[container]
env-prefixes = ["CONAN_AUDIT_PROVIDER_TOKEN"]   # forward matching host env vars
system-packages = ["lilypond"]                  # install Debian packages
build-command = "npm install -g @coderline/alphatab"  # provision non-apt deps
build-cache-files = ["package-lock.json"]       # inputs that invalidate the cache
```

## Keys

| Key | Type | Default | Semantics |
|---|---|---|---|
| `env-prefixes` | list of strings | `[]` | Host env-var name **prefixes** `vrg-container-run` forwards into the container (accumulates) |
| `system-packages` | list of strings | `[]` | Debian `apt` package **names** baked into the local cached dev image and installed on CI test jobs (see below) |
| `build-command` | string | *(unset)* | A shell command run while the cached dev image is built, to provision a non-apt dependency **outside `/workspace`** (see below) |
| `build-cache-files` | list of strings | `[]` | Repo-relative files the `build-command` reads; folded into the image cache hash so a bump forces a rebuild |

## `env-prefixes`

Each entry is a prefix; any host environment variable whose name starts with it
is passed through when `vrg-container-run` launches the container. This is how a
consuming repo gets a secret or token — held only in the host environment — into
the container without hard-coding its name in tooling.

For a fully worked example (wiring the ConanCenter audit token end to end across
local runs, CI, and agent VMs), see
[C++ Overview → ConanCenter Audit Token](../standards/development/cpp/overview.md#conancenter-audit-token).
The passthrough design is specified in
[`docs/specs/2026-05-25-configurable-container-env-passthrough-design.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/2026-05-25-configurable-container-env-passthrough-design.md).

## `system-packages`

A list of Debian package **names** — nothing more. Vergil's container model
otherwise treats a system dependency as a property of the **language**
(`dev-python:3.14` means the same thing everywhere), which is what makes the
per-branch image cache sound. `system-packages` is the sanctioned escape hatch
for the case where one repo genuinely needs a binary that is not a language
package — for example a repo whose tests shell out to the LilyPond renderer
(`apt-get install lilypond`) — **without** adding anything repo-specific to the
shared base images (epic
[vergil-project/.github#272](https://github.com/vergil-project/.github/issues/272)).

The packages are installed with:

```text
apt-get install -y --no-install-recommends <names>
```

from the base image's **existing** apt sources (Debian main). The surface is
deliberately narrow — **names only**:

- **No** third-party apt sources, signing keys, `add-apt-repository`, or install
  scripts.
- **No** version pins, build flags, or post-install steps.

### Where the packages take effect

The same declaration is applied by two different paths, which agree on **what**
is installed while differing on **when**:

- **Locally** the packages are baked into the per-branch cached dev image that
  `vrg-container-run` builds over the base
  ([`container_cache.py`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/lib/container_cache.py)),
  so they are present for **every** check in that one image.
- **In CI** they are installed by a composite setup step **only on the jobs that
  run the repo's own tests** — never on lint or typecheck (composite action added
  in
  [vergil-actions#807](https://github.com/vergil-project/vergil-actions/issues/807)).

**Test-runtime contract.** A declared system package is a **test-runtime**
dependency. Because it is test-only in CI, **repos must not rely on a system
package during lint or typecheck.** The local one-image convenience (visible to
every check) does not license depending on it outside test jobs; the divergence
is deliberate, sound because these binaries are genuinely needed only at test
time. See
[CI Architecture → Repo-specific system packages](../guides/ci-architecture.md#repo-specific-system-packages)
for the mechanism in full.

### Fail-closed on the build architecture

The cache layer builds on the host architecture (Apple Silicon → `linux/arm64`;
the CI runner's arch). If a declared package has **no installation candidate**
for that architecture, the build and the CI setup step both **fail immediately**,
naming the package and the architecture:

```text
system package 'X' is not installable on linux/arm64 (apt: no installation candidate)
```

There is no silent skip and no degraded image — a missing dependency stops the
build rather than producing a container that fails mysteriously inside a test.

### Cache key

`vergil.toml` is already part of the cache-sensitive file set, so editing
`system-packages` changes the per-branch image's cache hash and forces a rebuild.
No special-casing is needed, and an empty or absent list is byte-identical to a
repo that declares no packages at all.

### Trust model

A repo can express Debian package *names* only, installed from the base image's
existing sources, as root — exactly what the base image already grants. It cannot
fetch arbitrary URLs or run arbitrary code. Adding to `system-packages` is a
`vergil.toml` change **reviewed like any config change, via the PR diff**: the
list is a plain, greppable set of names. There is **no separate allowlist** — the
constrained surface plus the fail-closed build is the backstop for typos and
nonexistent names.

### Inspecting the declared list

`vrg-container-system-packages` reads the key through the same single accessor
the local bake and CI both use (`container_system_packages` in
[`config.py`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/lib/config.py)):

```bash
vrg-container-system-packages                   # the declared names, one per line
vrg-container-system-packages --install-script  # the exact apt snippet CI runs
```

The plain-list mode is a human-facing inspection affordance ("what would this
repo install?"); `--install-script` emits the single, shared apt speller that
both the local image build and the CI step execute verbatim.

## `build-command`

A shell command run once while the per-branch cached dev image is built (after the
`system-packages` apt step and the vergil-tooling install, before the language
warmup). It is the escape hatch for a dependency that is **not** an apt package and
**not** a language package the base image already carries — for example a JS
library a test driver consumes at runtime (epic
[vergil-project/.github#291](https://github.com/vergil-project/.github/issues/291)).

### The out-of-workspace contract

`vrg-container-run` bind-mounts the repo over `/workspace` at run time, which
**masks** anything the build wrote under `/workspace`. So a `build-command` must
install its artifact **outside `/workspace`** (a global/system location baked into
the image), or the run-time mount hides it. A global install is the usual way to
land outside the workspace — e.g. `npm install -g <pkg>` writes to
`/usr/lib/node_modules`, which is image-resident and survives the mount.

Landing the artifact outside `/workspace` makes it **present**, but *present* is
not the same as *resolvable*, and the two differ by artifact kind:

- **Executables** are on `PATH`. A globally-installed CLI (`npm install -g` of a
  package with a `bin`, a `pip install` console script, a Go binary in
  `/usr/local/bin`) is on `PATH` and runs as-is — no extra configuration.
- **Libraries are not automatically on the language's module-resolution path.**
  This is the correction to the earlier contract wording: a global `npm install -g`
  of a *library* is **not** "on the default resolution path." A global npm install
  puts *executables* on `PATH` but does **not** add the npm global root
  (`/usr/lib/node_modules`) to Node's `require` search path. Without help,
  `require("<lib>")` / `require.resolve("<lib>")` returns `MODULE_NOT_FOUND` even
  though the package is baked into the image (validated in
  [vergil-project/vergil-tooling#2766](https://github.com/vergil-project/vergil-tooling/issues/2766);
  this also corrects the design spec's §4, tracked for the epic doc-sweep
  [#2756](https://github.com/vergil-project/vergil-tooling/issues/2756)).

### `NODE_PATH` for baked npm libraries

To make a baked npm **library** resolvable, `vrg-container-run` sets `NODE_PATH`
to the npm global root (`/usr/lib/node_modules`, i.e. `npm root -g` on the vergil
base images) **whenever the repo declares a `build-command`**, so
`require.resolve("<lib>")` resolves out of the box. A repo that declares no
`build-command` is unaffected — no `NODE_PATH` is set, and its container
environment is byte-for-byte unchanged.

Two caveats:

- **CommonJS only.** `NODE_PATH` is honoured by CommonJS `require`; **ESM `import`
  ignores it**. A library consumed via ESM `import` needs a different mechanism
  (e.g. a repo-local `node_modules` populated by `npm ci`, or staging the module
  into a directory ESM resolution walks up to).
- **Overridable.** An explicit `NODE_PATH` in the host environment wins, so a
  consumer can point resolution at a different location at run time.

The value rides the container **run** invocation rather than being baked into the
image with `commit --change ENV`, because the `nerdctl` runtime's `commit --change`
supports only `CMD`/`ENTRYPOINT` directives (not `ENV`); setting it on the run
path works identically across the `docker` and `nerdctl` runtimes.

### Cache key

Like `system-packages`, editing `build-command` changes `vergil.toml`, which is in
the cache-sensitive file set, so a change forces an image rebuild. When the command
reads a lockfile whose *contents* (not the command string) determine what gets
installed, list that lockfile in `build-cache-files` so a dependency bump also
invalidates the cache.

### Trust model

`build-command` is a deliberate **broadening** of the surface `system-packages`
grants. Where `system-packages` is names-only — Debian packages installed from
the base image's existing sources, with no allowlist because the surface is
already constrained — `build-command` is **arbitrary shell, run as root, at
image-build time**. It can fetch URLs, run installers, and write anywhere in the
image. That is the point (it is the escape hatch for a dependency apt cannot
express), and it is what makes it a larger grant.

The controls that keep it sound:

- **Reviewed via the `vergil.toml` PR diff.** The command is a plain string in
  the repo's own `vergil.toml`; adding or changing it is a config change read and
  approved like any other in the PR diff. There is **no separate allowlist**
  (unlike the names-only `system-packages` surface, none is needed) — the diff is
  the review surface.
- **The fail-closed build is the backstop.** The command runs while the cached
  image is built; a non-zero exit **fails the build** rather than producing a
  degraded image, exactly as the `system-packages` apt step does. A broken
  provisioning step stops the build, it does not leak into a mysteriously failing
  test.
- **Artifacts are image-resident and out-of-workspace.** Whatever the command
  installs lands in the image outside `/workspace` (see the out-of-workspace
  contract above); it never writes into the mounted repo tree at run time.
- **No build-time network secrets by default.** The build step runs with the
  image build's environment, not the run-time forward set — a `build-command`
  sees a host secret only if the repo deliberately forwards it. Keep credentials
  out of the command string, which is stored in `vergil.toml` verbatim.

### Inspecting the declared command

`vrg-container-build-command` reads the key through the same single accessor the
local bake and CI both use (`container_build_command` in
[`config.py`](https://github.com/vergil-project/vergil-tooling/blob/develop/src/vergil_tooling/lib/config.py)):

```bash
vrg-container-build-command            # the declared command (nothing if unset)
vrg-container-build-command --script   # the same command, emitted for CI to run
```

Both modes print the command verbatim; a repo that declares no `build-command`
prints nothing. `--script` is the CI-consumption entry point — the CI test jobs
call it to obtain the exact command to run per job (see
[CI Architecture → Repo-specific build steps](../guides/ci-architecture.md#repo-specific-build-steps)),
mirroring how `vrg-container-system-packages --install-script` feeds the apt step.
