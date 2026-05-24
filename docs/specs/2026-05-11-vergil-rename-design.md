# VERGIL Rename Design

**Issue:** [#551](https://github.com/wphillipmoore/standard-tooling/issues/551)
**Milestone:** v2.0
**Date:** 2026-05-11

## Summary

Rename the standard-tooling project suite to VERGIL (Validation Engine
for Repository Governance, Integration & Lifecycle). This covers
renaming all four core repositories, transferring them to a new GitHub
organization, updating the Python package and CLI prefix, and sweeping
all consumer repos to update references. The CLI architecture stays
as-is (individual scripts); the Click-based unified `vergil` command is
a separate effort.

## Naming Inventory

### GitHub organization

Create a new org: **`vergil-project`**

### Repository renames and transfers

| Current (`wphillipmoore/`) | New (`vergil-project/`) |
|---|---|
| `standard-tooling` | `vergil-tooling` |
| `standard-actions` | `vergil-actions` |
| `standard-tooling-docker` | `vergil-docker` |
| `standard-tooling-plugin` | `vergil-claude-plugin` |

### Python package

| Artifact | Old | New |
|---|---|---|
| PyPI package | *(unpublished)* | `vergil-tooling` |
| Python module | `standard_tooling` | `vergil_tooling` |
| Source directory | `src/standard_tooling/` | `src/vergil_tooling/` |
| Test directory | `tests/standard_tooling/` | `tests/vergil_tooling/` |

### CLI prefix

All console scripts change from `st-*` to `vrg-*`:

`st-commit` → `vrg-commit`, `st-validate` → `vrg-validate`,
`st-docker-run` → `vrg-container-run`, etc.

### Environment variables

All `ST_`-prefixed environment variables change to `VRG_`:

`ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT`, etc.

### Config file

| Artifact | Old | New |
|---|---|---|
| Config filename | `standard-tooling.toml` | `vergil.toml` |
| Dependency key | `[dependencies] standard-tooling = "v1.4"` | `[dependencies] vergil = "v2.0"` |

The config file represents the VERGIL system, not a specific repo. A
single version pins all four core repos — they are released in lockstep
and guaranteed consistent within a version.

**Full field inventory** — every section/field containing an old name:

| Section | Field | Change |
|---|---|---|
| `[dependencies]` | `standard-tooling = "v1.4"` | `vergil = "v2.0"` |
| `[publish]` | `consumer-refresh` (contains `standard-tooling @ git+https://github.com/wphillipmoore/...`) | Update package name, GitHub org, and version tag |
| `[docker]` | `image-prefix` | No rename needed, but GHCR prefix in code changes (see Container registry) |
| `[project.co-authors]` | `wphillipmoore-claude`, `wphillipmoore-codex` | Decision point: bot accounts stay as-is unless renamed separately |

### Container registry

All dev container images move from `ghcr.io/wphillipmoore/*` to
`ghcr.io/vergil-project/*`:

| Image | Old | New |
|---|---|---|
| Prod base | `ghcr.io/wphillipmoore/prod-base` | `ghcr.io/vergil-project/prod-base` |
| Dev base | `ghcr.io/wphillipmoore/dev-base` | `ghcr.io/vergil-project/dev-base` |

The hardcoded GHCR prefix in `src/standard_tooling/lib/docker.py`
(`_GHCR = "ghcr.io/wphillipmoore"`) changes to
`ghcr.io/vergil-project`.

GHCR packages are scoped to the owning user/org. Transferring a repo
does not automatically move its container packages. After
`vergil-docker` is transferred and renamed, all images must be
re-published to `ghcr.io/vergil-project/*` before downstream repos
can pull them. During the consumer sweep, images must be available at
both the old and new GHCR paths — consumers not yet swept still
reference the old prefix.

### Versioning

v2.0 is scoped to the rename. The intent is minimum necessary breaking
changes; execution-time fixes may introduce additional breaks as
needed, but no unrelated breaking changes are bundled.

- **Four core repos:** v1.x → v2.0.0 (may iterate to v2.0.x during
  stabilization). The `v2.0` tag tracks the latest patch level.
- **Consumer repos:** minor version bump (1.x → 1.x+1) to reflect the
  dependency rename; no consumer-side logic changes

## Execution Model

### Preconditions

All repos frozen. No in-flight PRs, no active development across the
fleet. Single engineer, single execution window (target: one
morning/day). Phase 1 (core repos) must complete before breaking.
Phase 2 (consumer sweep) can resume in a separate window if needed.

### Phase 1 — Core repos

**Step 1: Create the org**

Create `vergil-project` GitHub org with the owner account.

**Step 2: Rename, transfer, update, and release in dependency order**

The four repos have a dependency chain that determines sequencing:

1. **`vergil-docker`** — base layer, no dependencies on the other three
2. **`vergil-actions`** — reusable workflows, references docker images
3. **`vergil-tooling`** — CLI tools, references actions and docker
4. **`vergil-claude-plugin`** — consumes vergil-tooling

For each repo:

1. Rename the repo on GitHub
2. Transfer to the `vergil-project` org
3. Update all internal references (imports, URLs, config, workflow
   `uses:` lines)
4. For vergil-docker specifically: re-publish all container images to
   `ghcr.io/vergil-project/*` (GHCR packages don't move with repo
   transfers)
5. For vergil-tooling specifically: rename the Python module
   (`src/standard_tooling/` → `src/vergil_tooling/`), update
   `pyproject.toml`, rename CLI entry points `st-*` → `vrg-*`,
   update `_GHCR` prefix in `docker.py`
6. Release v2.0.0 (may iterate to v2.0.x if issues surface)
7. Verify the release before moving to the next repo

GitHub redirects from the old `wphillipmoore/*` URLs remain active
throughout, providing a safety net.

### Phase 2 — Consumer sweep

**Checkpoint:** Phase 1 must complete before breaking. Phase 2 can
resume in a separate window if needed — core repos are the critical
path, consumers can trail. During this gap, images remain available at
both `ghcr.io/wphillipmoore/*` (old) and `ghcr.io/vergil-project/*`
(new) so un-swept consumers continue to work.

Once all four core repos are stable at v2.0.x, sweep through each
consumer repo (see Appendix A for the full manifest):

1. Rename `standard-tooling.toml` → `vergil.toml`
2. Update all config fields (see Config file inventory above):
   `[dependencies]`, `[publish] consumer-refresh`, etc.
3. Update `.githooks/pre-commit` (`st-commit` → `vrg-commit`,
   `ST_COMMIT_CONTEXT` → `VRG_COMMIT_CONTEXT`)
4. Update workflow files (`uses: wphillipmoore/standard-actions/...` →
   `uses: vergil-project/vergil-actions/...`)
5. Update `uv tool install` references
6. Update `CLAUDE.md` / `AGENTS.md` references
7. Minor version bump, release, verify

### Definition of done

Every repo in the fleet has completed a successful release under the
new names. For the four core repos, that means a v2.0.x release through
the full publish workflow. For consumer repos, that means a minor
version release with updated references.

## Out of Scope

- **CLI redesign** — the Click-based unified `vergil` subcommand is a
  separate issue
- **README / marketing material** — separate effort after the rename
  lands
- **`standards-and-conventions` repo** — historical reference, not part
  of the rename
- **PyPI bare `vergil` name** — issue #561 continues independently; if
  acquired later, triggers a separate deprecation cycle
- **Diogenes rename** — separate process following the same playbook
- **GitHub redirect cleanup** — redirects are maintained indefinitely
  by GitHub; all consumer references are updated in Phase 2 regardless

## Risks

### GitHub rename + transfer ordering

Renaming and transferring a repo to an org creates a redirect chain.
The recommended order is transfer-then-rename: this moves the repo to
`vergil-project/standard-tooling` first, then renames it to
`vergil-project/vergil-tooling`. This keeps the org as the stable
anchor while the name changes. GitHub maintains the full redirect
chain either way.

### Git remotes on local clones

Local checkouts and worktrees will still have `origin` pointing at old
URLs. Git follows redirects, but remotes should be updated to avoid
relying on them. The plan should include a local cleanup step.

### Claude Code plugin namespace

Skills currently appear as `standard-tooling:*` (e.g.,
`standard-tooling:publish`). The plugin namespace is determined by the
plugin's registration. The plan must account for how the namespace is
configured and ensure skills resolve correctly under the new name.

### Rollback

Given the frozen fleet and single engineer, rollback is
straightforward: transfer repos back to `wphillipmoore`, rename back to
`standard-*`. GitHub redirects work in both directions. There is no
existing PyPI presence for `standard-tooling`, so no published package
to worry about.

## Reusable Playbook

This rename establishes a pattern for future project renames (notably
the Diogenes rename of `ai-research-methodology`):

1. Create a `<name>-project` GitHub org
2. Rename and transfer repos in dependency order
3. Re-publish container images to the new GHCR scope
4. Update all internal references
5. Release, verify
6. Sweep consumer repos

## Appendix A: Consumer Repo Manifest

Repos under `wphillipmoore/` that consume standard-tooling (have a
`standard-tooling.toml` or reference `st-*` tools). Verify each
before execution day — repos may be archived or added between now
and then.

| Repository | Notes |
|---|---|
| `ai-research-methodology` | Also the future Diogenes rename candidate |
| `career-strategy` | |
| `cognition` | |
| `home-equity-project` | |
| `lunatick-racing` | |
| `mempalace` | |
| `mnemosys-core` | |
| `mnemosys-ios` | |
| `mnemosys-operations` | |
| `mq-rest-admin-common` | |
| `mq-rest-admin-dev-environment` | |
| `mq-rest-admin-go` | |
| `mq-rest-admin-java` | |
| `mq-rest-admin-python` | |
| `mq-rest-admin-ruby` | |
| `mq-rest-admin-rust` | |
| `paad` | Claude Code plugin — may have skill namespace references |
| `the-infrastructure-mindset` | |
| `renegade-dotfiles` | Verify: may not consume standard-tooling |

**Not consumers** (no sweep needed):

- `cpan-afs-command`, `cpan-netapp`, `perl5-MQSeries` — legacy Perl,
  pre-dates standard-tooling
- `standards-and-conventions` — historical reference, explicitly out
  of scope
