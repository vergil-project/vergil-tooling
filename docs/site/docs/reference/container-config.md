# Container Config Reference (`[container]`)

A repo customizes its **dev container** through the `[container]` section of its
`vergil.toml`. The section is optional; every key defaults to empty, so a repo
that omits `[container]` behaves exactly as it would with an empty table.

Both keys describe the *same* container from two angles: `env-prefixes` controls
which host environment variables `vrg-container-run` forwards **into** the
container, and `system-packages` declares Debian packages installed **inside** it.

## Structure

```toml
[container]
env-prefixes = ["CONAN_AUDIT_PROVIDER_TOKEN"]   # forward matching host env vars
system-packages = ["lilypond"]                  # install Debian packages
```

## Keys

| Key | Type | Default | Semantics |
|---|---|---|---|
| `env-prefixes` | list of strings | `[]` | Host env-var name **prefixes** `vrg-container-run` forwards into the container (accumulates) |
| `system-packages` | list of strings | `[]` | Debian `apt` package **names** baked into the local cached dev image and installed on CI test jobs (see below) |

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
