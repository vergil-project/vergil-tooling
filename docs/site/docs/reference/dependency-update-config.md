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

## Updaters

The updaters run in this order. Each one runs only when the repo has
what it updates, and each one's changes become a separate commit on the
`deps/update-<version>` branch.

| Name | Runs when | Commit message |
|---|---|---|
| `python` | `pyproject.toml` and `uv.lock` exist | `chore(deps): uv lock --upgrade` |
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

### `vergil`

Keeps the vergil ecosystem references consistent with
`[dependencies].vergil` in `vergil.toml`:

- Rewrites drifting `uses: vergil-*/…@vX.Y` refs in
  `.github/workflows/` and in every `action.yml`/`action.yaml` in the
  repo.
- Sets the vergil marketplace `source.ref` in `.claude/settings.json`
  to `main`.
- With [`vrg-update-deps --vergil X.Y`](dev/update-deps.md), first
  sets `[dependencies].vergil` to `vX.Y`, then rewrites the refs to
  match.

For a repo with an exact `[package.python] runtime` pin such as `3.14.8`
(see [Package Config](package-config.md)), it also bumps the pin to the
CPython patch that the pin's series package, `vergil-python3.14`, serves.
That package's version is `<cpython>+<pbs_tag>` (for example
`3.14.9+20261020`), and the patch is the part before the `+`. Each
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
- a series package version is not `<cpython>+<pbs_tag>`;
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

## The held-back report

The held-back report lists the dependencies whose newest release is
outside the bounds the repo declares. It appears in the dependency-update
PR body, in the `update-deps` comment on the release tracking issue, and
at the end of every `vrg-update-deps` run, as a table:

| Source | Dependency | Current | Latest | Note |
|---|---|---|---|---|
| python | example-lib | 1.4.2 | 2.0.0 | project.dependencies: example-lib<2 |

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
