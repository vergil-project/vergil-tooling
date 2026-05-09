# Migrate repository configuration into `standard-tooling.toml`

Spec for issue #363. Extracts all machine-readable configuration from
`docs/repository-standards.md` into a new `standard-tooling.toml` file,
retires the markdown-based parser, and rolls the change out across all
consuming repos.

## Context

Issue #362 introduced `st-config.toml` with a single field
(`standard-tooling.tag`) for runtime install version pinning. That
file exists in all 17 consuming repos today.

This spec defines `standard-tooling.toml` as the authoritative
per-repo configuration file for all `st-*` tooling. It replaces
the ad-hoc `st-config.toml` and subsumes the machine-readable
portions of `docs/repository-standards.md`.

`docs/repository-standards.md` remains as a documentation file.
Its config sections (repository profile, AI co-authors,
validation policy) are stripped after migration. Each consuming repo gets a follow-on
issue to audit and modernize the remaining documentation content.

## Schema

```toml
[project]
repository-type = "tooling"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]
claude = "Co-Authored-By: wphillipmoore-claude <255925739+wphillipmoore-claude@users.noreply.github.com>"
codex = "Co-Authored-By: wphillipmoore-codex <255923655+wphillipmoore-codex@users.noreply.github.com>"

[markdownlint]
ignore = ["docs/site/docs/research"]

[dependencies]
standard-tooling = "v1.4"
```

### `[project]` table

Five required fields, all kebab-case. Each field is
enum-constrained:

| Field | Allowed values |
|---|---|
| `repository-type` | `library`, `application`, `infrastructure`, `tooling`, `documentation` |
| `versioning-scheme` | `library`, `semver`, `application`, `none` |
| `branching-model` | `library-release`, `application-promotion`, `docs-single-branch` |
| `release-model` | `artifact-publishing`, `tagged-release`, `environment-promotion`, `none` |
| `primary-language` | `python`, `go`, `java`, `ruby`, `rust`, `shell`, `none`, `claude-plugin` |

Value definitions:

- **`repository-type`**: `library` — reusable package consumed as a dependency; `application` — deployed service or runnable program; `infrastructure` — environment, container, or platform configuration; `tooling` — developer CLI or build tooling; `documentation` — prose-only, no runtime artifacts.
- **`versioning-scheme`**: `library` — major-version release lines with independent support windows; `semver` — single active release line following semver; `application` — deploy-driven versioning without public release lines; `none` — no versioned releases.
- **`branching-model`**: `library-release` — develop + main with release branches; `application-promotion` — environment promotion (dev → staging → prod); `docs-single-branch` — single default branch, no release flow.
- **`release-model`**: `artifact-publishing` — release produces a published artifact (package, action); `tagged-release` — release is a git tag consumed directly; `environment-promotion` — release is a deploy, not a tag; `none` — no formal release process.
- **`primary-language`**: the dominant language for toolchain selection; `claude-plugin` for Claude plugin repos (version in `.claude-plugin/plugin.json`); `none` for repos with no runtime code.

> **Note:** Some `versioning-scheme` values may be functionally
> identical — a follow-on issue will audit and rationalize the enum
> after migration.

### `[project.co-authors]` table

Optional. Maps agent names to full `Co-Authored-By` git trailer
strings. Keys are the agent identifier passed to `st-commit
--agent` (e.g., `claude`, `codex`). Values must match the
`Co-Authored-By:` trailer format.

Repos with no AI agent workflow may omit this table entirely.

Expected value format: `Co-Authored-By: <name> <<email>>` — a
standard git trailer. The validator checks for the `Co-Authored-By:`
prefix and the presence of angle-bracketed email.

### `[markdownlint]` table

Optional. Controls markdownlint behavior for `st-validate` (common checks).

| Field | Type | Default | Description |
|---|---|---|---|
| `ignore` | array of strings | `[]` | Relative paths (from repo root) to exclude from markdown file discovery |

Files under any ignored path are skipped during `_find_markdown_files()`
discovery. This is useful for repos with large auto-generated markdown
trees (e.g., research output) that should not be linted.

Repos without this section get default behavior — all files under
`docs/site/` and `README.md` are linted.

### `[dependencies]` table

Version pins for standard-tooling ecosystem components. Keys are
component names, values are git tags.

`standard-tooling` is required. Additional components (e.g.,
`standard-actions`) may be added when consumers exist for them.

### Dropped fields

The following fields from `docs/repository-standards.md` are not
migrated. None have code consumers — they were documentation-only:

- `supported_release_lines`
- `canonical_local_validation_command`
- `validation_required`

### Key naming convention

TOML keys use kebab-case (`repository-type`, `branching-model`).
Python code maps to snake_case internally. This follows TOML
convention and matches the existing `st-config.toml` precedent.

## Reader implementation

### Location

`src/standard_tooling/lib/config.py` (already exists with
`st-config.toml` reader).

### Typed dataclasses

```python
@dataclass
class ProjectConfig:
    repository_type: str
    versioning_scheme: str
    branching_model: str
    release_model: str
    primary_language: str
    co_authors: dict[str, str]  # agent name -> full trailer string

@dataclass
class StConfig:
    project: ProjectConfig
    dependencies: dict[str, str]  # component name -> version tag
```

### Responsibilities

1. Locate and parse `standard-tooling.toml` using `tomllib`
   (stdlib, Python 3.11+).
2. Map kebab-case TOML keys to snake_case dataclass attributes.
3. Validate required fields are present and non-empty.
4. Validate enum values — reject unknown values with a diagnostic.
5. Validate co-author trailer format when `[project.co-authors]`
   is present.
6. Validate `[dependencies]` contains `standard-tooling`.
7. Return typed `StConfig`. No raw dicts leak to callers.

### Backward compatibility

The existing `st_install_tag()` function and `_CONFIG_FILE`
constant continue to read from `st-config.toml` until
`st-docker-run` is migrated. Both files coexist temporarily.

## Consumer migration

### `st-commit` (`src/standard_tooling/bin/st_commit.py`)

- Replace `repo_profile.read_profile(root)` with
  `config.read_config(root)` for `branching_model`.
- Replace `repo_profile.resolve_co_author(agent, root)` with a
  lookup on `config.project.co_authors[agent]`.
- Handle `ConfigError` from validation failures (exit with
  diagnostic).
- Update error messages referencing `PROFILE_FILENAME` to
  reference `standard-tooling.toml`.

### `st-validate` (`src/standard_tooling/bin/st_validate.py`)

- Replace `repo_profile.read_profile(root)` with
  `config.read_config(root)` for `primary_language`.
- Handle `ConfigError` from validation failures (exit with
  diagnostic).

### `st-finalize-repo` (`src/standard_tooling/bin/st_finalize_repo.py`)

- Replace `repo_profile.read_profile(root)` with
  `config.read_config(root)` for `branching_model`.
- Handle `ConfigError` from validation failures (exit with
  diagnostic).

### `repo_profile_cli.py` (`src/standard_tooling/bin/st_repo_profile.py`)

Rewrite to validate `standard-tooling.toml`:

- All `[project]` keys present and non-empty.
- Values match their enum.
- Co-author trailer format (when present).
- `[dependencies]` contains `standard-tooling`.
- README structural checks remain unchanged.

### `validate_local_common_container.py`

Calls `repo_profile_cli.main()`. No change needed — the CLI's
interface (returns exit code) is unchanged.

### `st-docker-run` (`src/standard_tooling/bin/st_docker_run.py`) — deferred

Currently reads `st-config.toml` via `st_install_tag()`. Migrates
to `config.read_config(root).dependencies["standard-tooling"]`
when `st-config.toml` is retired. This is the last consumer to
switch.

## Deleted code

- `src/standard_tooling/lib/repo_profile.py` — entire file.
- All imports and references to `repo_profile` in consumer
  modules.
- The `PROFILE_FILENAME` constant and all markdown-parsing
  logic (`_FIELD_RE`, `_COAUTHOR_RE`, `read_profile`,
  `read_co_authors`, `resolve_co_author`, `profile_path`,
  `RepoProfile`).
- Existing tests for `repo_profile.py` — replaced by new tests
  for the TOML reader.

## Testing

### Unit tests for `config.py`

- Valid config parses into typed `StConfig` dataclass.
- Missing required `[project]` field produces an error.
- Invalid enum value produces an error with diagnostic.
- Malformed co-author trailer format produces an error.
- Missing `standard-tooling.toml` file produces an error.
- Invalid TOML syntax produces an error.
- Missing `[dependencies]` or missing `standard-tooling` key
  produces an error.
- Absent `[project.co-authors]` table is accepted (empty dict).

### Unit tests for `repo_profile_cli.py`

- Valid `standard-tooling.toml` exits 0.
- Invalid or missing fields exit nonzero with diagnostic.
- README structural checks are unchanged — existing tests stay.

### Integration

`st-validate` runs `repo_profile_cli.main()` in the validation
pipeline. Once the validator reads from `standard-tooling.toml`,
tier-1 validation covers the new file automatically. No new
integration test is needed.

Consumer-level tests for the migration are not needed. The
consumers read a config value and use it the same way they always
did. The reader is tested; the consumers' logic is unchanged.

## Cross-repo rollout

### Phase 1 — seed `standard-tooling.toml` (all 17 repos, parallel)

Add `standard-tooling.toml` to every repo with that repo's values
(mapped from current `docs/repository-standards.md`). The old
validator ignores this file — these are small, safe PRs.

### Phase 2 — build the TOML reader (standard-tooling only)

1. Expand `config.py` with typed reader and validation.
2. Migrate all consumers to the new reader.
3. Delete `repo_profile.py`.
4. Rewrite `repo_profile_cli.py` to validate the new file.
5. Strip config sections (repository profile, AI co-authors,
   validation policy) from this repo's
   `docs/repository-standards.md`.
6. Update CLAUDE.md and documentation references.
7. Release as a 1.4.x patch.

Consuming repos pin to `@v1.4` (a floating ref) and pick up the
new validator automatically. Because Phase 1 already seeded
`standard-tooling.toml`, the new validator finds the file in
every repo with no further changes.

### Phase 3 — clean up `docs/repository-standards.md` (16 consuming repos, parallel)

1. Strip config sections from `docs/repository-standards.md`.
2. Create a follow-on issue per repo to audit and modernize the
   remaining documentation content.

Safe because the new validator (active via the floating `@v1.4`
pin) reads `standard-tooling.toml`, not the markdown.

### Phase 4 — retire `st-config.toml`

1. Update `st-docker-run` to read `[dependencies]` from
   `standard-tooling.toml` instead of `st-config.toml`.
2. Update `docker_cache.py`: replace `"st-config.toml"` with
   `"standard-tooling.toml"` in `_CACHE_FILES` and
   `_DEFAULT_CACHE_FILES` so config changes continue to
   invalidate the Docker image cache.
3. Delete `st-config.toml` from all 17 repos (parallel sweep).

### Ordering constraints

Phase 1 must land before Phase 2 (the new validator expects the
file to exist). Phase 2 must be released before Phase 3 (stripping
markdown is only safe once the new validator is active). Phase 4
can happen any time after Phase 2.

## File coexistence during migration

| Phase | `standard-tooling.toml` | `st-config.toml` | `docs/repository-standards.md` |
|---|---|---|---|
| Before | absent | `[standard-tooling]` `tag` | config + docs |
| Phase 1 | all repos (seeded) | unchanged | config + docs |
| Phase 2 | all repos (validated) | unchanged | this repo: docs only; others: config + docs |
| Phase 3 | all repos | unchanged | docs only (config stripped) |
| Phase 4 | all repos | deleted | docs only |

## Value mapping reference

Values for each consuming repo's `standard-tooling.toml`, derived
from current `docs/repository-standards.md`:

| Repo | repository-type | versioning-scheme | branching-model | release-model | primary-language |
|---|---|---|---|---|---|
| standard-tooling | tooling | semver | library-release | tagged-release | python |
| standard-tooling-docker | tooling | semver | library-release | tagged-release | shell |
| standard-tooling-plugin | library | semver | library-release | tagged-release | none |
| standard-actions | library | library | library-release | artifact-publishing | shell |
| standards-and-conventions | documentation | library | library-release | artifact-publishing | none |
| the-infrastructure-mindset | documentation | none | docs-single-branch | none | none |
| ai-research-methodology | library | library | library-release | artifact-publishing | python |
| mq-rest-admin-common | library | library | library-release | artifact-publishing | none |
| mq-rest-admin-dev-environment | infrastructure | semver | library-release | tagged-release | shell |
| mq-rest-admin-go | library | library | library-release | artifact-publishing | go |
| mq-rest-admin-java | library | library | library-release | artifact-publishing | java |
| mq-rest-admin-python | library | library | library-release | artifact-publishing | python |
| mq-rest-admin-ruby | library | library | library-release | artifact-publishing | ruby |
| mq-rest-admin-rust | library | library | library-release | artifact-publishing | rust |
| mq-rest-admin-template | library | library | library-release | artifact-publishing | (see note) |
| mnemosys-core | application | application | application-promotion | environment-promotion | none |
| mnemosys-operations | application | application | application-promotion | environment-promotion | none |

Co-author identities: 15 repos use `wphillipmoore-claude` /
`wphillipmoore-codex`. `mnemosys-operations` uses
`mnemosys-claude` / `mnemosys-codex`. `mnemosys-core` has no
co-authors configured.

`mq-rest-admin-template` is a repository template. Its
`primary-language` is set per-instantiation. The template's own
`standard-tooling.toml` should use `none` since the template
itself has no primary language; generated repos set the correct
value at creation time.
