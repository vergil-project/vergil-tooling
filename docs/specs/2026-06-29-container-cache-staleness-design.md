# Container cache staleness — base-digest-aware cache key

- **Issue:** vergil-project/vergil-tooling#1973
- **Date:** 2026-06-29
- **Status:** Approved (design); offline-fallback default amended by #1982

> **Amendment (vergil-project/vergil-tooling#1982).** The "offline / registry-down
> → use the local base, warn, continue (`verified=False`)" path described below was
> the *default*. In practice it let a host validate against a stale local base for
> an unknown length of time (the exact `ansible-lint` failure that motivated #1973),
> with only a soft `(offline?)` warning — a silent failure. As of #1982 a failed
> base pull is a **hard error by default**: `resolve_base_digest` raises with a
> message that names the real pull error and the stale-cache risk. The local-base
> degradation is retained as an **explicit opt-in** via `VRG_ALLOW_STALE_BASE=1`
> (or `resolve_base_digest(..., allow_stale=True)`); only on that opt-in path does
> it warn and return `verified=False`. The "no local base → raise" and
> "pull succeeds → `verified=True`" behaviours are unchanged.

## Problem

`src/vergil_tooling/lib/container_cache.py` maintains a per-branch dev image with
vergil-tooling and language dependencies pre-installed. The cached image is reused
when its **cache key** matches. Today that key (`compute_cache_hash`) is a SHA-256
over:

- the repo's cache-sensitive dependency files (`uv.lock`, `go.sum`, `pom.xml`,
  `Gemfile.lock`, `Cargo.lock`, plus `vergil.toml`), and
- the repo directory name (as salt).

It **never incorporates the identity of the base image** the cache was built from.
The base is referenced by a moving tag (`…-base:latest` or a major tag). When
`vergil-containers` republishes that tag — e.g. adding `ansible-lint` in v2.1.1 —
the base image's content changes but the tag string does not, the dependency files
do not, so the cache key is unchanged and the **stale cached image is reused
indefinitely**.

### Why `develop` is uniquely affected

The cache tag is `{base_repo}:{base_tag}--{sanitized-branch}--{dep-hash}`. Two
properties have to hold for an old cache to keep matching: a stable branch name and
a stable dependency hash.

- **Feature / worktree branches** have ever-changing names, so each new branch
  lands in a fresh tag namespace with no prior cache → forced rebuild; and
  `vrg-finalize-pr` removes them on merge. They self-heal.
- **`develop`** is one permanent name, and its dependency files rarely change, so
  both properties hold. The same cached image is reused for weeks across many
  upstream image rebuilds. This is the trap.

### Observed impact

On the MQ Resiliency Lab for Linux repo, `vrg-validate` on `develop` failed during
finalize with `FileNotFoundError: 'ansible-lint'`. The host `vrg-validate` now runs
the ansible-lint check (vergil-tooling#1667), but the `develop` cache had been built
from a base image predating ansible-lint (added in vergil-containers v2.1.1). New
check code, old cached image.

### Compounding gap

`_build_cached_image` runs `docker create <base>` with **no `--pull`**, so even a
forced rebuild can re-bake a stale *local* base image. The reuse path does no
network at all, so a republished tag is never noticed.

## Goals / non-goals

**Goals**

- A cached image is reused only if it was built from the base image currently
  published under its tag.
- When the base moves, the cache rebuilds **automatically** on the next container
  run — no manual step, picked up the morning after a nightly central rebuild.
- Detection uses a principled signal (the base image's content digest), not an
  inventory of expected tools.
- Offline / registry-unreachable runs degrade gracefully rather than breaking the
  dev loop.

**Non-goals**

- Throttling the freshness check. We check on every container run for simplicity
  (the user accepted the per-run cost). A timestamp-based throttle is a trivial
  later addition if the overhead bites; explicitly out of scope here.
- Probing the image for specific tools / an image capability manifest. Rejected as
  brittle and tactical.
- Changing the direct-run (non-cached) path, which already uses `--pull=always`.

## Design

### 1. Base digest in the cache key

`compute_cache_hash` gains the base image's content digest as an input:

```
hash = sha256( sorted(dep file bytes) + base_digest + salt )[:8]
```

When the central image moves, the digest changes → the hash changes →
`ensure_cached_image` finds the old `…--develop--<oldhash>` image, sees the suffix
no longer matches the current hash, removes it, and rebuilds. **No new comparison
logic and no tool list** — the existing "hash mismatch → rebuild" path does the
work, and it is correct for any change to the base image (CVE patch, new tool,
anything).

### 2. Resolving the current base digest

A new helper `resolve_base_digest(base_image, *, runtime) -> tuple[str, bool]`
returns `(digest, verified)`:

1. `docker pull <base_image>` with a bounded timeout. This refreshes the local copy
   so a moved base is both **seen** and **available to rebuild from**. A pull of an
   unchanged image is a cheap manifest check with no layer downloads.
2. `docker image inspect <base_image> --format '{{.Id}}'` → the digest string.
3. **Offline / registry-down:** if the pull fails but a local copy of the base
   exists, inspect and use the **local** digest, return `verified=False`, and print
   a one-line warning that surfaces the pull's actual error (e.g. `denied`,
   `unauthorized`, a network error, or a timeout) rather than guessing the cause:
   `warning: could not verify base image freshness for '<image>' (pull failed:
   <reason>); using local image`.
4. If the pull fails and there is no local base, raise — nothing could run anyway.

`pull` + `inspect` is chosen over `manifest inspect` because both work on docker and
podman (the runtime is already auto-detected). `manifest inspect` is lighter but
less portable; noted as a possible future optimization.

The digest used in the key is the locally-resolved image `Id`. It is consistent
per machine/platform, which is all the cache key requires (caches are local).

### 3. Build-path fix

Because step 2 already pulled the base, `_build_cached_image`'s
`docker create <base>` now builds from the fresh local base, closing the
re-bake-stale-base gap. We additionally pass `--pull=missing` to `docker create`
as a safety net — pull only if the image is absent locally. We deliberately do
**not** use `--pull=always`: an offline `create --pull=always` would fail even
when a usable local copy exists, defeating the offline fallback. With
`--pull=missing`, a present local base (always the case after `resolve_base_digest`
ran) is used as-is, and an offline build never breaks on a registry it can't reach.

### 4. Placement

All changes are contained in `container_cache.py`, threaded through the single
chokepoint `ensure_cached_image`. Callers (`vrg-container-run`, `vrg-validate`, the
validators) are unchanged.

`vrg-container-cache status` (`bin/vrg_container_cache.py`) must compute its hash
**identically to `ensure_cached_image`**. It currently calls
`compute_cache_hash(files)` with no salt, while `ensure_cached_image` passes
`salt=repo_root.name` — so `status` already computes a different hash than the one
baked into the cached tag and its `current`/`stale` readout is unreliable today
(independently of this bug). The fix aligns it to the same computation: the
`repo_root.name` salt **and** the base digest (via `resolve_base_digest`). After
this, `status` reports `stale` exactly when `ensure_cached_image` would rebuild —
including when the base image has moved.

## Data flow

```
ensure_cached_image(repo_root, lang, base_image)
  files            = cache_sensitive_files(...)        # unchanged
  base_digest, ok  = resolve_base_digest(base_image)   # NEW: pull + inspect
  current_hash     = compute_cache_hash(files, base_digest, salt=repo_root.name)
  existing         = find_cached_image(base_image, branch)
    if existing and existing_hash == current_hash:  reuse
    else:                                           rmi old (if any) + rebuild
  rebuild uses the freshly-pulled base
```

## Error handling

- **Registry unreachable, local base present:** warn, use local digest, continue
  (`verified=False`). The dev loop never breaks because GHCR is down.
- **Registry unreachable, no local base:** hard error (cannot run a container at
  all).
- **Pull timeout:** treated as registry-unreachable (above), via a bounded timeout
  on the pull subprocess.
- **`inspect` failure on a present image:** hard error (unexpected; surfaced, not
  swallowed).

No failure is silently swallowed; the only soft path is the explicit offline
fallback, which warns.

## Testing

Existing tests already mock `subprocess.run` for runtime calls; these fit the same
pattern.

- `compute_cache_hash`: hash changes iff `base_digest` changes; stable when files
  and digest are unchanged.
- `resolve_base_digest`:
  - pull succeeds → returns inspected digest, `verified=True`;
  - pull fails, local image present → returns local digest, `verified=False`,
    emits warning;
  - pull fails, no local image → raises.
- `ensure_cached_image`:
  - base digest changed vs existing cache → old image removed, rebuild invoked;
  - base digest same → existing image reused, no rebuild, no extra build calls;
  - rebuild path pulls the base.
- `vrg-container-cache status`: computes the same hash as `ensure_cached_image`
  (salt + base digest); reports `stale` when the resolved base digest differs from
  the cached image's hash (or when dep files differ), `current` otherwise. Add a
  regression test that a current cache reports `current` (guards the prior
  missing-salt inconsistency).

## Rollout

- No config or interface changes; purely internal to the cache layer.
- First run after upgrade resolves the live base digest and rebuilds any cache that
  was built from an older base — self-correcting on landing.
- The MQ `develop` failure is resolved the next time `vrg-validate` runs there once
  this ships (the rebuild picks up the ansible-lint-bearing base).
