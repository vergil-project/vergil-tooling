# Shell-to-Python Migration: Action Script Extraction

**Date:** 2026-05-27
**Umbrella issue:** #1192
**Issues:** #1181, #1182, #1183, #1184, #1185, #1186, #1188, #1189, #1190, #1191
**Dropped:** #1187 (deprecated action — replaced by `vrg-release` orchestrator)
**Scope:** vergil-tooling only (Python utilities + tests). Corresponding
vergil-actions updates are tracked by the upstream issues referenced in each
vergil-tooling issue.

## Motivation

Ten composite actions in vergil-actions contain embedded shell scripts that
have grown complex enough to be unmaintainable: duplicated `case` statements,
nested shell-in-Docker, `jq` pipelines for SARIF parsing, `sed` regex for
TOML extraction. These scripts cannot be unit tested, produce only
`::error`/`::warning` annotations, and require shell expertise to modify.

Extracting them into Python utilities in vergil-tooling provides:

- **Testability** — unit tests with fixtures for every code path.
- **Deduplication** — shared logic (language metadata, SARIF evaluation)
  defined once.
- **Richer output** — GitHub Actions step summaries, structured JSON,
  human-readable interactive output.
- **Safety** — `subprocess.run()` with argument lists replaces `eval` and
  nested shell quoting.

## Output Module: TTY-Aware Formatting

All utilities in this collection share a common output strategy driven by
terminal detection. These tools are primarily CI tools — the default output
is optimized for running inside a GitHub Action. Interactive mode is for
debugging and triage.

### New file: `lib/output.py`

Detection mechanism: `sys.stdout.isatty()`. When stdout is a TTY, the
utility is being used interactively (debugging, triage). When it is not,
the utility is running in CI. No CLI flags, no environment variable sniffing.

| Function | CI (not a TTY) | Interactive (TTY) |
|----------|---------------|-------------------|
| `emit_error(msg, file?, line?)` | `::error file=...,line=...::msg` | Red text to stderr |
| `emit_warning(msg, file?, line?)` | `::warning file=...,line=...::msg` | Yellow text to stderr |
| `write_output(key, value)` | Appends `key=value` to `$GITHUB_OUTPUT` | Prints `key: value` to stdout |
| `write_summary(markdown)` | Appends to `$GITHUB_STEP_SUMMARY` | Prints to stdout |
| `is_ci() -> bool` | `True` | `False` |

The module is a formatting concern only. Calling utilities decide *what* to
emit; the output module decides *how* to format it.

**Missing env var handling:** In CI mode, `write_output()` and
`write_summary()` require `$GITHUB_OUTPUT` and `$GITHUB_STEP_SUMMARY`
respectively. If these env vars are not set (non-GitHub CI, or piped
local invocation), the functions fall back to printing to stdout —
same as interactive mode, but without color.

## Phase 1 — Foundation

Covers issues #1184, #1185. Builds the shared infrastructure that all
subsequent phases depend on.

### Primary Language Enum Cleanup

As part of the language metadata unification, clean up `config.py`'s
`primary-language` enum. Today it allows `python`, `go`, `java`, `ruby`,
`rust`, `shell`, `none`, and `claude-plugin`. The last three are not
programming languages with toolchain support:

- **`shell` and `none`** — identical behavior everywhere (base container,
  no validation commands, no CodeQL, no ecosystem metadata). These are
  the same thing with different labels.
- **`claude-plugin`** — exists solely for dual-file version writes
  (`VERSION` + `.claude-plugin/plugin.json`). All other behavior is
  identical to `none`.

**Change:** `primary-language` becomes optional, accepting only the five
real languages: `python`, `go`, `java`, `ruby`, `rust`. Repos that
currently use `shell`, `none`, or `claude-plugin` omit the field (or set
it to an empty string). The `claude-plugin` version-file behavior moves
to a separate config mechanism (e.g., `[publish].version-files`).

This makes `primary-language` mean "a first-class programming language
with build, test, lint, and publish toolchain support" — which is exactly
what the unified language registry models.

**Files affected:** `lib/config.py` (enum, validation), `lib/version.py`
(dual-file writes, `_LANGUAGES_WITH_SEPARATE_VERSION`),
`lib/repo_init.py` (container suffix map, CI templates).

### Unified Language Metadata Module

**Refactor:** `lib/validate_commands.py` → `lib/languages.py`

Merges the existing per-language validation command registry with ecosystem
metadata (build command, publish command, credential secret name) from #1184.
Provides the canonical supported-languages set that #1185 needs.

**Data model:**

```python
@dataclass(frozen=True)
class EcosystemInfo:
    build_cmd: list[str] | None                  # e.g. ["uv", "build"]
    publish_cmd: list[str] | None                # e.g. ["uv", "publish"]
    credential_secret: str | None                # e.g. "PYPI_TOKEN"

@dataclass(frozen=True)
class Language:
    name: str                                    # "python", "go", etc.
    checks: dict[CheckKind, list[list[str]]]     # existing _REGISTRY data
    ecosystem: EcosystemInfo                     # build/publish/credential
```

Commands are `list[str]` (subprocess argv), not shell strings — this
eliminates the `eval "$PUBLISH_CMD"` pattern in the current action.

**Registry:** Module-level `dict[str, Language]` replacing the current
`_REGISTRY`. The registry contains exactly the five supported languages.
Populated with the same validation command data plus the new ecosystem
fields.

**Public API:**

- `supported_languages() -> frozenset[str]` — the five languages that have
  full toolchain support. This is the same set as `config.py`'s
  `primary-language` enum after the cleanup.
- `ecosystem_metadata(language: str) -> EcosystemInfo` — build/publish/credential
  for a language. Raises `ValueError` for unsupported languages.
- `language_commands(language, kind) -> list[list[str]]` — existing API,
  preserved for backward compatibility. Reimplemented as a thin wrapper
  over the new registry.
- `CheckKind` enum — re-exported from the new module.

**Callers to update** (full refactor — update import paths):

- `bin/vrg_validate.py` — imports `CheckKind`, `language_commands`
- `lib/repo_init.py` — imports from `validate_commands`
- `lib/github_config.py` — imports from `validate_commands`
- `lib/container_cache.py` — imports from `validate_commands`
- `tests/vergil_tooling/test_validate_commands.py` — rename to
  `test_languages.py`, update imports
- `tests/vergil_tooling/test_vrg_validate.py` — imports `CheckKind`

### CLI: `vrg-ecosystem-resolve` (#1184)

Accepts a language identifier. Prints ecosystem metadata: build command,
publish command, credential secret name. Uses the output module (JSON with
`$GITHUB_OUTPUT` writes in CI, key-value pairs interactive).

Entry point: `bin/vrg_ecosystem_resolve.py`

### CLI: `vrg-release-validate-inputs` (#1185)

Accepts language plus optional flags (`--container-tag`, `--registry-publish`).
Validates the combination:

- Language must be in `supported_languages()`.
- `--container-tag` and `--registry-publish` have language-specific
  compatibility rules.

Reports all validation failures (not just the first). Uses the output module.

Entry point: `bin/vrg_release_validate_inputs.py`

## Phase 2 — Extend Existing Code

Covers issues #1188, #1189, #1190. Builds on existing modules with minimal
new code.

### PR Body Compliance Retrofit (#1188)

`vrg-pr-issue-linkage` (`bin/vrg_pr_issue_linkage.py`) already implements
the PR body compliance check: reads `GITHUB_EVENT_PATH`, parses PR JSON,
checks `AUTOCLOSE_RE` from `lib/linkage.py`, validates `LINKAGE_RE`.

The tooling-side work is limited to retrofitting the output module — replacing
raw `print()` calls with `emit_error()` / `write_summary()`.

The remaining work (updating the standards-compliance action to call
`vrg-pr-issue-linkage`) is a vergil-actions change tracked by the upstream
issue.

### Version Divergence Comparison (#1189)

**New file:** `lib/version_divergence.py`

Core function:

```python
def compare_versions(head: str, main: str | None) -> DivergenceResult
```

Returns a result indicating: diverged (head != main), equal (not bumped),
or no-prior-release (main is `None` or empty). The "no prior release" case
is a normal success path, not an error.

**CLI:** `vrg-version-divergence`

Accepts head version and main version as positional arguments. Main version
is optional (missing = no prior release). Exit code: 0 = diverged or no
prior release, 1 = versions equal (not bumped).

**Structured output:** In addition to exit codes, the CLI writes output
keys so the calling action can distinguish between result states:
- `write_output("status", "diverged" | "first-release" | "equal")`
- `write_output("head_version", ...)`
- `write_output("main_version", ...)` (empty string for first-release)

Uses the output module.

Entry point: `bin/vrg_version_divergence.py`

### vergil.toml Version Resolution CLI (#1190)

`config.py` already implements the version resolution logic via
`vrg_install_tag(repo_root)` — reads `vergil.toml`, returns
`[dependencies].vergil`, with an env var override
(`VRG_DOCKER_INSTALL_TAG`). The tooling-side library function exists.

The remaining work is a thin CLI entry point wrapping the existing
function: `vrg-resolve-tooling-version` prints the resolved version
string. Uses the output module.

Entry point: `bin/vrg_resolve_tooling_version.py`

## Phase 3 — Standalone Utilities

Covers issues #1186, #1183. Independent of Phase 2; can run in parallel.

### Action Reference Freezing (#1186)

**New file:** `lib/freeze_refs.py`

Core functions:

- `collect_yaml_files(dirs: list[Path]) -> list[Path]` — replaces the
  duplicated `find` + `mapfile` shell pattern.
- `freeze_references(content: str, owner_repo: str, tag: str) -> str` —
  applies two regex transformations using `re.sub` with named groups:
  1. `./actions/<path>` → `owner/repo/actions/<path>@tag`
  2. `owner/repo/<path>@develop` → `owner/repo/<path>@tag`
- `validate_no_unfrozen(content: str, filename: str) -> list[Finding]` —
  checks for remaining unfrozen references. Returns structured findings.

File collection is done once and shared between freeze and validate (fixing
the duplication in the shell version).

The exact regex patterns are derived from the existing `sed` expressions
in the composite action. See vergil-actions#602 for the source of truth
on pattern specifics (nested paths, already-frozen refs, SHA-pinned refs).

**CLI:** `vrg-freeze-refs`

Accepts `--owner-repo`, `--tag`, and optional `--scan-dirs` (defaults to
`.github/workflows` and `actions/`). Runs freeze then validate in a single
pass. Uses the output module.

Entry point: `bin/vrg_freeze_refs.py`

### Docs Staging and Nav Patching (#1183)

These are already Python code trapped in shell heredocs inside the docs
deploy action. Extraction is mechanical: pull the scripts out, add proper
argument parsing, wrap with the output module.

**CLI:** `vrg-docs-stage`

Stages changelog and release notes into the docs build directory. Generates
a semver-sorted release index. Key arguments (derived from the action's
shell variable setup, see vergil-actions#599): `--version` (release
version), `--docs-dir` (target docs directory), `--changelog` (path to
changelog file), `--release-notes` (path to release notes).

Entry point: `bin/vrg_docs_stage.py`

**CLI:** `vrg-docs-patch-nav`

Patches `mkdocs.yml` nav entries with release version links. Key arguments:
`--version` (release version), `--mkdocs-yml` (path to mkdocs config).

Entry point: `bin/vrg_docs_patch_nav.py`

Both get unit tests with fixture mkdocs.yml files and directory structures.
The detailed logic already exists in the heredocs; implementation pulls it
out verbatim and wraps it.

## Phase 4 — Security Scan Orchestration

Covers issues #1182, #1181, #1191. Depends on the output module from Phase 1.
Within this phase, #1182 (SARIF evaluation) must land before #1181 (semgrep)
and #1191 (Trivy), which are independent of each other.

### Shared SARIF Evaluation (#1182)

**New file:** `lib/sarif.py`

Core functions:

- `parse_sarif(path: Path) -> dict` — load and validate a SARIF JSON file.
- `parse_sarif_directory(directory: Path) -> list[dict]` — glob for `*.sarif`
  files, load all. Handles CodeQL's multi-file output.
- `evaluate_findings(sarif_data, severity_filter={"warning", "error"}) -> EvaluationResult` —
  filters findings by severity level. Returns structured result: count,
  grouped findings, pass/fail.
- `format_summary(result: EvaluationResult) -> str` — markdown table for
  step summaries.

Replaces the identical `jq --slurp` pipelines in both the CodeQL and semgrep
composite actions.

**CLI:** `vrg-sarif-evaluate`

Accepts a file path or directory path, optional `--severity` filter. Exit
code: 0 = clean, 1 = findings above threshold. Uses the output module for
annotations and step summary.

Entry point: `bin/vrg_sarif_evaluate.py`

### Semgrep Scan Orchestration (#1181)

**New file:** `lib/semgrep.py`

Core functions:

- `resolve_rulesets(language, has_dockerfiles, has_workflows, extra_config) -> list[str]` —
  language-to-ruleset mapping (e.g., `go` → `p/golang`). Auto-detects
  Dockerfile and Actions workflow presence for context-specific rulesets.
  The `go`/`golang` mapping lives here — `go` is the canonical language key,
  `golang` is semgrep's outlier mapped at this integration layer.
- `run_scan(rulesets, target_dir, output_path) -> ScanResult` — subprocess
  execution with exit code handling. Semgrep uses non-zero exit codes that
  still produce valid SARIF; this function distinguishes scan failures from
  finding-present results.

**CLI:** `vrg-semgrep-scan`

Assembles rulesets based on language and repo content, runs the scan, then
evaluates results via `lib/sarif.py` (imported, not shelled out). Uses the
output module.

Entry point: `bin/vrg_semgrep_scan.py`

### Trivy Scan Orchestration (#1191)

**New file:** `lib/trivy.py`

Core functions:

- `build_docker_args(scan_type, target, trivyignore, ...) -> list[str]` —
  constructs the `docker run` argument list. Replaces the nested
  shell-in-Docker pattern with explicit subprocess argument construction.
- `run_scan(scan_type, target, output_dir) -> ScanResult` — executes the
  scan-once-convert-twice workflow: Trivy scan to JSON, convert to table
  (stdout), convert to SARIF (file). Single function handles both filesystem
  and image scans parameterized by `scan_type`, deduplicating the
  near-identical steps in the current action.
- `generate_sbom(target, output_path)` — CycloneDX SBOM generation via
  Docker.

**CLI:** `vrg-trivy-scan`

Accepts `--type` (filesystem | image), target path/image, optional
`--trivyignore`. Runs scan, converts outputs, evaluates SARIF via
`lib/sarif.py`. Uses the output module.

Entry point: `bin/vrg_trivy_scan.py`

## Testing Strategy

Each utility gets unit tests with fixtures. Tests cover both the library
functions (logic) and the CLI entry points (argument parsing, exit codes,
output formatting).

| Utility | Test fixtures |
|---------|--------------|
| `lib/output.py` | Mock TTY / non-TTY, mock `$GITHUB_OUTPUT` file |
| `lib/languages.py` | Language name inputs (valid, invalid, edge cases) |
| `vrg-ecosystem-resolve` | Per-language expected metadata |
| `vrg-release-validate-inputs` | Input combination matrix (valid, incompatible) |
| `vrg-resolve-tooling-version` | Sample vergil.toml files (valid, missing field, malformed) |
| `vrg-pr-issue-linkage` | PR event JSON payloads (already has tests; extend for output module) |
| `vrg-version-divergence` | Version pairs (diverged, equal, no prior release) |
| `vrg-freeze-refs` | YAML workflow files with unfrozen references |
| `vrg-docs-stage` | Mock docs directory structure, changelog files |
| `vrg-docs-patch-nav` | Sample mkdocs.yml files |
| `vrg-sarif-evaluate` | SARIF JSON files (clean, warnings, errors, multi-file) |
| `vrg-semgrep-scan` | Ruleset resolution inputs; scan subprocess mocking |
| `vrg-trivy-scan` | Docker argument construction; scan subprocess mocking |

The language metadata refactor includes backward-compatibility tests
confirming that `language_commands()` returns identical results before and
after the restructuring.

**Testing tiers:** All tests above are Tier 1 (local, fast, mocked
subprocesses). Phase 4's scan orchestrators (semgrep, Trivy) cannot be
integration-tested against live tools without significant infrastructure.
Tier 1 mocked tests verify argument construction, output parsing, and
SARIF evaluation. Real-world validation will happen during rollout as
actions adopt the new utilities. A dedicated integration testing framework
for external tool orchestration is a future consideration, not a
prerequisite for this migration.

## Implementation Order

Each phase is independently shippable. The vergil-actions side can adopt
utilities as they land, tracked by the upstream issues.

| Phase | Issues | Dependencies |
|-------|--------|-------------|
| 1 — Foundation | #1184, #1185 | None (output module built first within this phase) |
| 2 — Extend existing | #1188, #1189, #1190 | Output module from Phase 1 |
| 3 — Standalone | #1186, #1183 | Output module from Phase 1 |
| 4 — Scan orchestration | #1182, #1181, #1191 | Output module from Phase 1; #1182 before #1181/#1191 |

Phases 2 and 3 are independent of each other and can run in parallel.
Within Phase 4, #1182 (SARIF evaluation) must land before #1181 (semgrep)
and #1191 (Trivy), which are independent of each other.
