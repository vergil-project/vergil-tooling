# Releasing

This guide covers the release workflow for `vergil-tooling`,
including how patch, minor, and major versions affect both the
release author (producer) and downstream consumers.

The model: `vergil-tooling` is distributed as a host-level
developer tool plus a dev-container pre-bake (see
[host-level-tool spec][hlt]). Three deployment targets — developer
host, Python project `.venv`, and dev container image — each track
the **rolling minor tag** (e.g. `v1.3`), which is force-updated by
`tag-and-release` on every patch release. The dev container images
rebuild automatically on each `vergil-tooling` release via a
`repository_dispatch` trigger.

[hlt]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/host-level-tool.md

## Tag scheme — what's user-facing, what isn't

Two parallel tag families exist in the repo. Consumers only see and
use the first; the second is internal scaffolding for the changelog
flow.

| Tag pattern | Where | Purpose | User-facing? |
|---|---|---|---|
| `v1.3.0` | `main` | Release tag | Yes — pin in install commands |
| `v1.3` | `main` | Rolling minor tag (force-updated each patch) | Yes — what consumers actually pin to |
| `develop-v1.3.0` | `develop` | Boundary marker for `git-cliff`'s next-changelog scoping | No — internal |

The `develop-` prefix is invisible to install / consumption; never
pin to a `develop-v*` tag.

## Patch release — the common path

A patch release fixes a bug or refines an existing feature without
adding new public surface. Versions: `v1.3.0` → `v1.3.1`.

### Producer steps

1. Land all the patch's changes via normal feature PRs to `develop`.
2. Cut the release:

   ```bash
   vrg-prepare-release --issue <release-tracking-issue>
   ```

   This creates `release/{version}`, merges `main` to pick up prior
   tags, generates `CHANGELOG.md` and `releases/v{version}.md`,
   commits, pushes, opens a PR to `main`.
3. Wait for CI green, then merge:

   ```bash
   vrg-merge-when-green https://github.com/vergil-project/vergil-tooling/pull/<release-pr>
   ```

4. Post-merge automation runs:
   - `tag-and-release` creates the release tag (`v1.3.1`).
   - The rolling minor tag (`v1.3`) is **force-updated** to point at
     the new release.
   - The boundary tag (`develop-v1.3.1`) is created on `develop`.
   - `repository_dispatch` fires a `vergil-tooling-released`
     event at `vergil-docker`, which rebuilds the dev
     images against the now-current `v1.3` tag.
   - `version-bump-pr` opens an auto-bump PR on `develop`
     (e.g., `1.3.1` → `1.3.2`). Merge it.
   - The Documentation workflow publishes the new release notes to
     the docs site under `releases/v1.3.1`.
5. Run `vrg-finalize-pr` to clean up local state.

### Consumer steps

For most consumers, **no action is required**: the rolling `v1.3`
tag they're already pinned to now points at the patch release. They
get the fix on the next normal action:

| Consumer | When the patch lands |
|---|---|
| Developer host | Manual `uv tool upgrade vergil-tooling` |
| Python repo `.venv` | `uv lock --upgrade-package vergil-tooling` (typically batched with other dep bumps) |
| Dev container image | Already rebuilt by `repository_dispatch`; new pulls of `dev-base` etc. carry the patch within the rebuild window (minutes) |

## Minor release — opt-in for new features

A minor release adds backwards-compatible new features. Versions:
`v1.3.x` → `v1.4.0`.

### Producer steps

Same five steps as patch, with one important addition: the rolling
minor tag changes from `v1.3` to `v1.4`. The image's pin in
`vergil-docker` and any per-repo dev-dep declarations
**must be bumped manually** — the rolling-tag mechanism only handles
patches within a minor.

1. Cut and merge `release/v1.4.0` exactly like a patch release.
2. **Bump `vergil-docker`'s pin** in
   [`docker/common/vergil-tooling-uv.dockerfile`][docker-frag]:

   ```dockerfile
   ARG ST_TOOLING_TAG=v1.4   # was v1.3
   ```

   Open a small PR in `vergil-docker`, merge it, and the
   next image rebuild (whether triggered by your release or a
   subsequent push there) carries the new minor.
3. **Coordinate consumer bumps.** Each Python repo that has
   `[tool.uv.sources]` pinning to `v1.3` needs its `pyproject.toml`
   updated to `v1.4`. This is a per-repo decision — consumers who
   prefer to stay on `v1.3` can keep doing so until they're ready.
4. Update the canonical install command in `getting-started.md`
   (and anywhere else the documentation pins a minor) to the new
   tag.

[docker-frag]: https://github.com/vergil-project/vergil-docker/blob/develop/docker/common/vergil-tooling-uv.dockerfile

### Consumer steps

Minor bumps are **deliberate opt-in** at every deployment target:

| Consumer | What changes |
|---|---|
| Developer host | `uv tool install --reinstall 'vergil-tooling @ git+...@v1.4'` (or update the pinned tag in their notes / shell history) |
| Python repo `.venv` | Edit `pyproject.toml` `[tool.uv.sources]` to `tag = "v1.4"`, then `uv lock --upgrade-package vergil-tooling` |
| Dev container image | Wait for `vergil-docker` to land the `ARG` bump, then pull the rebuilt image |

The release author should announce the minor bump in the GitHub
Release notes, calling out any new features and the `v1.4` pin
update.

## Major release — explicit migration

A major release contains breaking changes. Versions: `v1.x.x` →
`v2.0.0`.

### Producer steps

A major release is a minor release plus a deliberate breaking-change
communication and migration window:

1. Document the breaking changes BEFORE cutting the release. A
   `docs/specs/v2-migration.md` (or similar) describing what
   breaks, what to update, and how to recover from common issues.
2. Cut and merge `release/v2.0.0` like a minor release. The
   release notes prominently call out the breaking-change list and
   link to the migration guide.
3. Bump `vergil-docker`'s `ARG ST_TOOLING_TAG=` to `v2`
   in a separate PR — coordinate so consumers can opt in on their
   own schedule.
4. Notify consumer maintainers (e.g., update `vergil-tooling`'s
   release tracking issue / project board with a callout). The
   release notes alone are not sufficient for major bumps;
   consumers should be primed.

### Consumer steps

Same as a minor release in mechanics — every deployment target
edits its `v1.x` pin to `v2` deliberately. The difference is
**timing and risk awareness**:

- Read the migration guide before bumping.
- Stage the bump in a feature branch, run validation, fix any
  breakages from removed APIs / changed behaviors.
- Merge the bump PR per the consumer's normal review flow.

## Documentation publication

The Documentation workflow (`.github/workflows/docs.yml`) deploys
the MkDocs site to GitHub Pages on every push to `develop` and
`main`. It's container-based: the workflow runs inside
`dev-base:latest` and uses
`vergil-project/vergil-actions/actions/docs-deploy`.

**Sanity-check the docs publication.** Docs publication is async
relative to the merge — a failure on this workflow doesn't block
the PR. To catch silent failures, `vrg-finalize-pr` checks the
status of the most recent CD workflow run on `develop`
after pulling the merge. If it failed, finalize prints the failure
with a direct link to the log and exits non-zero, so you investigate
before moving on to the next PR.

For deeper investigation, view recent runs directly:

```bash
gh run list --repo vergil-project/vergil-tooling \
  --workflow="Documentation" --limit 5
```

## Release tracking artifacts

Each release has a tracking issue (referenced as `--issue N` to
`vrg-prepare-release`). Use it to:

- Capture the rationale for the release (what's in it, what's not).
- Document any consumer coordination required (especially for
  minor / major bumps).
- Link to the release PR and any follow-up issues surfaced during
  release prep.

The tracking issue closes when the release PR merges; follow-ups
move to their own issues.
