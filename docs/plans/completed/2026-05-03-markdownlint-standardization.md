# Implementation Plan: Standardize markdownlint configuration

Spec: `docs/specs/2026-05-03-markdownlint-standardization-design.md`
Issue: https://github.com/wphillipmoore/standard-tooling/issues/476

## Phase 1: Validator changes (standard-tooling)

All work in the `issue-476-markdownlint-standardization` worktree.

### Step 1: Create the configs package

Create `src/standard_tooling/configs/` as a Python package with the
bundled config:

- `src/standard_tooling/configs/__init__.py` (empty)
- `src/standard_tooling/configs/markdownlint.yaml` (canonical config)

No ignore file is needed — the scoped file discovery
(`docs/site/**/*.md` + `README.md`) avoids vendored and internal
content by construction.

Update `[tool.setuptools.package-data]` in `pyproject.toml` to include
the new config files. The current glob (`data/*.json`) does not match
`.yaml` files. Add `configs/*` to the list so that
`importlib.resources` can resolve the bundled config at runtime.

### Step 2: Update `validate_local_common_container.py`

Replace the markdownlint config resolution:

1. Update the module docstring to note that markdownlint now uses
   the bundled canonical config (scope is unchanged).
2. Replace the repo-local `.markdownlint.yaml` check with
   `importlib.resources` resolution of the bundled config.
3. Keep `_find_markdown_files()` and the existing scope unchanged.
4. Keep shellcheck and yamllint sections unchanged.

### Step 3: Remove `st-markdown-standards`

1. Delete `src/standard_tooling/bin/markdown_standards.py`.
2. Remove the `st-markdown-standards` entry from
   `pyproject.toml` `[project.scripts]`.
3. Delete `tests/standard_tooling/test_markdown_standards.py`.

### Step 4: Update tests for the common container validator

1. Update `test_main_markdownlint_with_config` — it currently creates
   a repo-local `.markdownlint.yaml` and checks for `--config`. Change
   it to verify the bundled config is always used regardless of whether
   a repo-local config exists.
2. Add a test verifying that `--config` points to the bundled config
   path (via `importlib.resources`).
3. Keep `_find_markdown_files` tests and existing scope tests unchanged.

### Step 5: Clean up standard-tooling itself

1. Delete the repo-local `.markdownlint.yaml` and `.markdownlintignore`.
2. Delete `releases/.markdownlint.json` (stale directory-level override
   for content outside the lint scope).
3. Run the validator — fix any violations in `docs/site/` + `README.md`
   surfaced by the new canonical rules.

### Step 6: Validate

Run `st-docker-run -- uv run st-validate-local` to confirm all checks
pass. Iterate on violations until green.

### Step 7: Submit PR

Submit via `st-submit-pr`, wait for CI green.

## Phase 2: Release

After the PR merges:

1. Run `st-prepare-release` to cut a new patch release.
2. Follow the standard release workflow through to publish.

## Phase 3: Fleet sweep

After the release ships, sweep each repo that has a local markdownlint
config. The effort is modest — only published documentation
(`docs/site/` + `README.md`) is linted, so violation fixes are limited
to that scope. The main work is deleting stale configs.

**Per repo:**

1. Update `standard-tooling` dependency pin in
   `standard-tooling.toml` (and `pyproject.toml` if Python).
2. Delete `.markdownlint.yaml` / `.markdownlint.json`.
3. Delete `.markdownlintignore`.
4. Delete `releases/.markdownlint.json` if present (stale override).
5. Run validation; fix any violations in `docs/site/` + `README.md`.
6. Submit PR via `st-submit-pr`.

**Suggested sweep order:**

1. standard-tooling-plugin (small, YAML config)
2. standard-actions (small, JSON config)
3. standards-and-conventions (YAML config)
4. standard-tooling-docker (YAML config + standalone CI job removal)
5. ai-research-methodology (YAML config with custom rules)
6. mq-rest-admin-template (JSON, minimal content)
7. mq-rest-admin-dev-environment (JSON)
8. mq-rest-admin-common (JSON + ignore)
9. mq-rest-admin-go (JSON)
10. mq-rest-admin-java (JSON)
11. mq-rest-admin-python (JSON)
12. mq-rest-admin-ruby (JSON + HTML element override)
13. mq-rest-admin-rust (JSON)

Repos with no existing config (career-strategy, laptop-upgrade,
mnemosys-core, mnemosys-operations, the-infrastructure-mindset) gain
coverage automatically when they next update their standard-tooling
pin — no dedicated sweep PR needed unless violations already exist.

## Out of scope

- Repo-local config overrides. The lint scope covers only content we
  fully own (`docs/site/` + `README.md`), so there is no legitimate
  need for per-repo exceptions.
