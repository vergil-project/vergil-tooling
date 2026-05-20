# Design: Eliminate hardcoded mount in vrg-docker-docs

**Issue:** [#638](https://github.com/vergil-project/vergil-tooling/issues/638)
**Date:** 2026-05-20

## Problem

`vrg_docker_docs.py` builds its Docker argument list from scratch
instead of calling the shared `build_docker_args()` function. This
causes two problems:

1. **Hardcoded special case** — Lines 107-110 check for a sibling
   `mq-rest-admin-common` directory and mount it into the container.
   This is package-specific logic in shared tooling.

2. **Missing shared features** — The hand-built arg list lacks
   worktree `.git` mounts, `DOCKER_EXTRA_VOLUMES`, `DOCKER_NETWORK`,
   `.gitconfig`/`.ssh` mounts, and `MQ_*/GH_*/GITHUB_*` env var
   passthrough. Every other `vrg-docker-*` command gets these from
   `build_docker_args()`.

## Approach

Refactor `vrg_docker_docs.py` to delegate to `build_docker_args()`
and delete the hardcoded mount. Consumers needing sibling mounts use
the existing `DOCKER_EXTRA_VOLUMES` mechanism.

No changes to `build_docker_args()` or `vergil.toml`.

## Changes

### `vrg_docker_docs.py`

1. **Import `build_docker_args`** from `vergil_tooling.lib.docker`.

2. **Replace the hand-built argument list** (lines 93-112) with:
   ```python
   docker_args = build_docker_args(
       repo_root, image, ["bash", "-c", container_cmd],
   )
   ```

3. **Splice port mapping for `serve`**: find the image position in
   the returned list and insert `["-p", f"{port}:8000"]` before it.

4. **Delete the hardcoded mount** (lines 107-110). The
   `mq-rest-admin-common` check is removed entirely.

### `test_vrg_docker_docs.py`

1. **Delete `test_common_sibling_mount`** — the behavior is removed.

2. **Add `test_extra_volumes`** — set `DOCKER_EXTRA_VOLUMES` in the
   env and verify the mount appears in the args.

3. **Add `test_worktree_git_mount`** — create a `.git` file pointing
   at a parent worktree directory and confirm the parent `.git` is
   mounted.

4. **Update existing tests** as needed for the slightly different arg
   ordering that `build_docker_args` produces (e.g., `--pull=always`
   is now present).

## What consumers must do

Any repo that relied on the automatic `mq-rest-admin-common` sibling
mount must set `DOCKER_EXTRA_VOLUMES` in its docs workflow:

```bash
DOCKER_EXTRA_VOLUMES="/path/to/mq-rest-admin-common:/workspace/.mq-rest-admin-common:ro" \
    vrg-docker-docs serve
```

This is not a vergil-tooling change — it is a per-repo configuration
update documented in the issue closure comment.

## Scope exclusions

- No new `vergil.toml` config keys.
- No changes to `build_docker_args()` or `docker.py`.
- No changes to other `vrg-docker-*` commands.
