# Configurable container env-var passthrough

**Issue:** #777
**Date:** 2026-05-25

## Problem

`container.py` hardcodes environment variable prefixes that get passed
into the dev container:

```python
for name in os.environ:
    if name.startswith(("MQ_", "GH_", "GITHUB_")):
        container_args.extend(["-e", name])
```

- `MQ_*` is specific to a single consumer project (mq-rest-admin).
  Hardcoding consumer-specific configuration into shared tooling does not
  scale.
- `GH_*` and `GITHUB_*` are not needed by anything that runs inside the
  container today. The primary use case (validation/testing) does not
  require GitHub credentials. The one tool that does need `GH_TOKEN` in a
  container (`vrg-scorecard`) handles its own injection explicitly.

## Decision

Remove all hardcoded env-var prefix passthrough. Make it fully opt-in
via a new `[container]` section in `vergil.toml`.

### Why `[container]` and not `[docker]`

The tooling completed a docker-to-container rename: `docker.py` became
`container.py`, `vrg-docker-*` became `vrg-container-*`. The config
section name should match.

### Why not nest under `[ci]`

`[ci]` is about CI matrix configuration (Python versions,
integration-test toggle). Env-var passthrough is a container concern, not
a CI concern. Separate sections keep semantics clean.

## Schema

New optional section in `vergil.toml`:

```toml
[container]
env-prefixes = ["MQ_"]
```

- The `[container]` section is optional. If absent, no env vars are
  passed through (clean default for the majority of repos that need no
  passthrough).
- `env-prefixes` is a list of strings. Each entry is matched via
  `str.startswith()` against host environment variable names.
- An empty list (`env-prefixes = []`) is valid and means no passthrough.
- If the `[container]` section is present, `env-prefixes` is required
  (no partial config).
- Only prefix-based matching is supported. Individual env var names are
  not supported as a separate mechanism; a narrow prefix serves the same
  purpose.

### Out of scope

- `DOCKER_NETWORK` and `DOCKER_EXTRA_VOLUMES` remain env-var-driven
  runtime toggles. They are per-invocation concerns (you may or may not
  want a network depending on whether a broker is running), not per-repo
  constants. They may be revisited separately.

## Changes

### `config.py`

- Add `"container"` to `_KNOWN_SECTIONS`.
- Add `"container": frozenset({"env-prefixes"})` to `_KNOWN_KEYS`.
- New dataclass:
  ```python
  @dataclass
  class ContainerConfig:
      env_prefixes: list[str]
  ```
- Add `container: ContainerConfig` to `StConfig`, defaulting to
  `ContainerConfig(env_prefixes=[])` when the section is absent.
- Validation: if `[container]` is present, `env-prefixes` must be a
  list of strings. Same validation pattern as `[markdownlint].ignore`.

### `container.py`

- `build_container_args()` gains a new parameter:
  `env_prefixes: Sequence[str] = ()`.
- The hardcoded `("MQ_", "GH_", "GITHUB_")` tuple is replaced with a
  loop over the caller-supplied prefixes.
- If `env_prefixes` is empty, the env passthrough block is skipped.

### Callers

The three callers of `build_container_args()`:

- `vrg_container_run.py`
- `vrg_container_test.py`
- `vrg_container_docs.py`

Each already reads `vergil.toml` for other config. They will read
`config.container.env_prefixes` and pass it to `build_container_args()`.

### Legacy alias `build_docker_args()`

`container.py` exposes a backward-compatible `build_docker_args()` that
delegates to `build_container_args()`. No production code calls it — only
tests. The alias will forward the new `env_prefixes` parameter so existing
test patterns continue to work.

### Unaffected

`vrg_scorecard.py` injects `GH_TOKEN` explicitly at line 86 via its own
logic. It does not depend on the prefix passthrough and needs no changes.

### Tests

- Update `test_build_docker_args_env_passthrough` to pass prefixes
  explicitly instead of relying on hardcoded ones.
- Update `test_build_container_args_mq_env` similarly.
- Add test: no prefixes means no passthrough.
- Add test: `[container]` section parsing in config tests.

### Documentation

- Update `CLAUDE.md` to remove the "MQ_* env vars are automatically
  passed through" line.
- Update `vrg_container_run.py` help text to remove `GH_TOKEN` from
  documented env vars.
