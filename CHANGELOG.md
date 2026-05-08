## [develop-v1.4.25] - 2026-05-08

### 🚀 Features

- *(validate)* Bundle canonical yamllint config like markdownlint
- *(commit)* Reject GitHub auto-close keywords in commit bodies and PR bodies
- *(release)* Centralize git-cliff configs as bundled package data

### 🐛 Bug Fixes

- *(commit)* Tighten cliff regexes, fix doc to docs, add build and revert types

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.25
## [1.4.24] - 2026-05-07

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.24
## [develop-v1.4.24] - 2026-05-07

### 🚀 Features

- *(github)* Add retry with exponential backoff to all GitHub API calls
- *(wait-until-green)* Detect branch-behind state and auto-update before reporting success

### 🐛 Bug Fixes

- *(docker)* Pass --platform to docker run and docker create for correct arch selection

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.24
## [1.4.23] - 2026-05-07

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.23
## [develop-v1.4.23] - 2026-05-07

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.23
## [1.4.22] - 2026-05-06

### 🐛 Bug Fixes

- *(docs)* Remove stale validate-local references from mkdocs nav and reference index

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.22
## [develop-v1.4.22] - 2026-05-06

### 🚜 Refactor

- *(validate)* Rename validate_local_common_container to validate_common
- *(validate)* Update imports and patch targets for validate_common rename
- *(validate)* Remove legacy validate_local and validate_local_lang modules
- *(validate)* Remove legacy scripts/dev/ shell scripts
- *(validate)* Remove legacy st-validate-local console_script entries
- *(validate)* Rename custom validator lookup from validate-local-custom to validate-custom
- *(docker-run)* Update usage example to reference st-validate

### 📚 Documentation

- *(validate)* Remove legacy st-validate-local reference page
- *(validate)* Update cli-tools-overview for st-validate-local removal
- *(validate)* Update CLAUDE.md to reference st-validate instead of st-validate-local
- *(validate)* Update README.md to reference st-validate

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.22
## [1.4.21] - 2026-05-06

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.21
## [develop-v1.4.21] - 2026-05-06

### 🚀 Features

- *(github-config)* Add GHAS check runs (Semgrep OSS, Trivy) to CI gates required checks
- *(github-config)* Add --config flag to override remote config source

### 🐛 Bug Fixes

- *(github-config)* Use str() in _check_names to satisfy ty type checker
- *(ci)* Restore Python 3.12+ support and auto-prepend .venv/bin in st-validate

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.21
## [1.4.20] - 2026-05-06

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.20
## [develop-v1.4.20] - 2026-05-06

### 🚀 Features

- Add claude-plugin to primary-language enum
- Add claude-plugin version discovery, read, and write

### 🐛 Bug Fixes

- Prepend .venv/bin to PATH in st-validate for CI compatibility
- Use explicit argument lists in command registry instead of string splitting
- Add missing type annotations in test files for mypy strict mode
- Add ty as dev dependency and resolve ty type checker diagnostics

### 🚜 Refactor

- Remove .venv/bin PATH logic from st-validate

### 📚 Documentation

- Add claude-plugin to primary-language spec

### 🎨 Styling

- Format test_version.py
- Format github_config.py for ruff on Python 3.14

### 🧪 Testing

- Add failing test for st-version claude-plugin show
- Add bump test for claude-plugin
- Verify claude-plugin skips lockfile maintenance
- Verify error on missing version key in plugin.json

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.20
- Remove bespoke lint and typecheck jobs duplicated by ci-quality.yml
- Remove all bespoke jobs, use reusable workflows for test, audit, and release
- Trigger fresh workflow run after standard-actions fix
- Trigger fresh CI run after standard-actions self-install fix
- Trigger fresh CI run after dev container image rebuild
- Trigger fresh CI run after standard-actions PATH fix
- Trigger fresh CI run after standard-actions PATH fix for all jobs
- Use Python container for common, standards, and release jobs
- Require Python 3.14 as minimum version
## [1.4.19] - 2026-05-05

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.19
## [develop-v1.4.19] - 2026-05-05

### 🚀 Features

- *(validate)* Fix Python commands and add install entries to command registry
- *(validate)* Add st-validate command with registry-driven check dispatch
- *(validate)* Add hadolint and actionlint to common validation checks
- *(version)* Add st-version library and CLI with per-language version discovery, show, bump, and --ref support

### 🐛 Bug Fixes

- *(validate)* Suppress S101 on type-narrowing assertion
- *(validate)* Run independent check commands to completion instead of short-circuiting on first failure
- *(validate)* Use cmd.split() instead of shell=True for subprocess.run

### 🚜 Refactor

- *(docker)* Replace _WARMUP_COMMANDS with registry-driven install lookup
- *(finalize)* Call st-validate instead of st-validate-local in post-finalization

### 🎨 Styling

- *(validate,version)* Apply ruff format to new and modified files
- *(validate)* Apply ruff format to test_st_validate

### 🧪 Testing

- *(validate,version)* Achieve 100% branch coverage for st-validate and st-version

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.19
## [1.4.18] - 2026-05-05

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.18
## [develop-v1.4.18] - 2026-05-05

### 🐛 Bug Fixes

- *(docker-cache)* Skip uv tool install when running in the standard-tooling repo itself
- *(docker-cache)* Resolve CI failures: mypy no-any-return and ruff format

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.18
## [1.4.17] - 2026-05-05

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.17
## [develop-v1.4.17] - 2026-05-05

### 🚀 Features

- *(docker)* Unify cache install — Python now gets uv tool install

### 🐛 Bug Fixes

- *(ci)* Add safe.directory for git worktree tests in CI container

### 📚 Documentation

- Update host-level-tool spec for unified consumption model
- Update CLAUDE.md consumption model for unified install

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.17
- *(ci)* Upgrade workflows to standard-actions v1.5 and add CI derivation config
## [1.4.16] - 2026-05-05

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.16
## [develop-v1.4.16] - 2026-05-05

### 🐛 Bug Fixes

- *(github-config)* Normalize audit comparison for patterns ordering and API default fields

### 🎨 Styling

- *(github-config)* Apply ruff format

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.16
## [1.4.15] - 2026-05-05

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.15
## [develop-v1.4.15] - 2026-05-05

### 🚀 Features

- *(github-config)* Add desired state data model
- *(github)* Add read_json() helper for gh api calls
- *(config)* Add [ci] section to standard-tooling.toml schema
- *(config)* Add [github] override section to TOML schema
- *(github-config)* Add fixed desired state functions
- *(validate)* Add per-language command registry
- *(github-config)* Add CI gates ruleset derivation
- *(github-config)* Add compute_desired_state() top-level function
- *(github-config)* Add fetch_actual_state() for GitHub API reads
- *(github-config)* Add diff computation engine
- *(github-config)* Add st-github-config CLI with audit and diff modes
- *(github-config)* Implement apply mode for st-github-config CLI
- *(github-config)* Add classic branch protection cleanup during apply

### 🐛 Bug Fixes

- Add explicit type annotations to read_json() for mypy
- *(github-config)* Include enabled field in actions permissions PUT body

### 🎨 Styling

- Format test_config.py with ruff
- Apply ruff format to new files

### 🧪 Testing

- Cover _lang_has_check unknown check kind branch

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.15
## [1.4.14] - 2026-05-04

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.14
## [develop-v1.4.14] - 2026-05-04

### 🚀 Features

- *(markdownlint)* Add [markdownlint].ignore support to standard-tooling.toml

### 🐛 Bug Fixes

- *(markdownlint)* Fix formatting and add coverage for invalid ignore type

### 📚 Documentation

- *(markdownlint)* Update published docs for bundled markdownlint config
- *(plans)* Add cross-repo cleanup implementation plan

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.14
- Retrigger CI with issue linkage (#482)
## [1.4.13] - 2026-05-03

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.13
## [develop-v1.4.13] - 2026-05-03

### 🚀 Features

- *(finalize-repo)* Fail on dirty working tree after cleanup (#477)
- *(markdownlint)* Bundle canonical config and remove per-repo configs (#476) (#481)

### 📚 Documentation

- Markdownlint standardization spec, plan, and reviews (#476) (#478)
- Narrow markdownlint standardization to published docs scope (#476) (#480)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.13
- Bump standard-actions CI pin to v1.4.7
- Add memory management policy (#474)
- *(changelog)* Replace blanket chore skip with targeted mechanical-commit filters (#475)
## [1.4.12] - 2026-05-01

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.12
## [develop-v1.4.12] - 2026-05-01

### 🚀 Features

- Add _checks_registered probe and polling loop to wait_for_checks

### 🐛 Bug Fixes

- Make --title required in st-submit-pr

### 🎨 Styling

- Apply ruff format to test_github.py
- Fix line length in test_main_dry_run_release_branch
- Apply ruff format to test_submit_pr.py

### 🧪 Testing

- Cover wait_for_checks polling behavior

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.12
## [1.4.11] - 2026-05-01

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.11
## [develop-v1.4.11] - 2026-05-01

### 🚀 Features

- *(validate-local)* Add container guard to st-validate-local

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.11
## [1.4.10] - 2026-05-01

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.10
## [develop-v1.4.10] - 2026-05-01

### 🐛 Bug Fixes

- *(docker)* Fix --pull=always breaking cached image lookup; route Python through cache
- *(docker)* Fix ruff format violations

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.10
## [1.4.9] - 2026-05-01

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.9
## [develop-v1.4.9] - 2026-05-01

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.9
- *(docs)* Organize plans into proposed/in-progress/completed lifecycle
## [1.4.8] - 2026-05-01

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.8
## [develop-v1.4.8] - 2026-05-01

### 🐛 Bug Fixes

- *(docker-cache)* Replace pip install with uv tool install in docker cache build

### 📚 Documentation

- Add spec, plan, and pushback review for uv tool install and guard cleanup

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.8
- Add consumer-refresh config to standard-tooling.toml
- *(finalize-repo)* Remove shutil.which guards and make docs failure fatal
- *(prepare-release)* Remove _ensure_tool guard and shutil.which dependency
- *(markdown-standards)* Remove markdownlint shutil.which guard
- *(docs)* Remove all pip install references from host-level-tool spec
- *(finalize-repo)* Update docstring and validation failure label to reflect fatal semantics
- *(lint)* Fix S607 noqa, duplicate pytest import, and pip reference in releasing guide
## [1.4.7] - 2026-04-30

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.7
## [develop-v1.4.7] - 2026-04-30

### 🐛 Bug Fixes

- Force-update tags on git fetch to prevent stale local state
- Add --pull=always to docker run to prevent stale image cache
- Use uv run for validation in Python repos during finalization

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.7
- Retire st-config.toml in favor of standard-tooling.toml
- Retrigger CI after standard-actions v1.4.5
- Retrigger CI (force action cache refresh)
- Retrigger CI (GitHub Actions tag cache)
- Trigger CI for PR #417
- Pin ci-security workflow to v1.4.5 to bypass tag cache
- Pin ci-security workflow to v1.4.6
- Remove dead validate_local_common wrapper
## [1.4.6] - 2026-04-29

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.6
## [develop-v1.4.6] - 2026-04-29

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.6
- Change license from GPL-3.0-only to GPL-3.0-or-later
## [1.4.5] - 2026-04-29

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.5
## [develop-v1.4.5] - 2026-04-29

### 🚀 Features

- Add typed TOML reader for standard-tooling.toml

### 🐛 Bug Fixes

- *(finalize)* Eliminate unreachable elif branch for full coverage

### 🚜 Refactor

- Migrate st-commit from repo_profile to config.read_config
- Migrate st-validate-local from repo_profile to config.read_config
- Migrate st-finalize-repo from repo_profile to config.read_config
- Rewrite repo-profile-cli to validate standard-tooling.toml

### 📚 Documentation

- Add spec, plan, and reviews for standard-tooling.toml migration (#363)
- Strip config sections from repository-standards.md, update references

### 🎨 Styling

- Fix ruff TC003 and SIM117 lint errors
- Apply ruff format to modified files

### 🧪 Testing

- Add failing tests for standard-tooling.toml reader
- Rewrite repo-profile-cli tests for TOML validation
- Add missing coverage for ConfigError handlers and dead code removal

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.5
- Seed standard-tooling.toml with this repo's values
- Delete repo_profile.py — replaced by config.read_config
## [1.4.4] - 2026-04-29

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.4
## [develop-v1.4.4] - 2026-04-29

### 🚀 Features

- Add st-wait-until-green command for CI polling

### 🐛 Bug Fixes

- Reject invocation from secondary worktree instead of os.chdir
- Re-allow legacy chore/bump-version and chore/next-cycle-deps branch prefixes

### 🎨 Styling

- Move Path import to TYPE_CHECKING block
- Fix import ordering in release.py

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.4
## [1.4.3] - 2026-04-29

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.3
## [develop-v1.4.3] - 2026-04-29

### 🚀 Features

- *(cli)* Add st-check-pr-merge and branch check in st-merge-when-green
- Add next-cycle-deps pattern to release branch allow-list

### 🐛 Bug Fixes

- Drop --delete-branch from st-merge-when-green; st-finalize-repo handles cleanup
- Use CWD-relative README.md lookup in repo-profile instead of git.repo_root
- Retain st-markdown-standards as markdownlint-only entry point for CI compatibility

### 🚜 Refactor

- Unify release-cycle branches under release/ prefix
- Decompose st-markdown-standards: direct markdownlint in validate-local, structural checks in repo-profile

### 📚 Documentation

- Update spec and docs for cache-first architecture (#362)
- Mark all decouple plan phases complete with PR refs and follow-up issue links (#385)

### 🎨 Styling

- Apply ruff format to new and modified files
- Apply ruff format to test files

### 🧪 Testing

- Add coverage tests for check_pr_merge edge cases

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.3
- Retrigger checks after adding issue linkage
- Update all Python dependencies for next cycle
- *(ci)* Remove docker dispatch and verification pipeline
- *(ci)* Point ci-security ref to @develop for install fix (#362)
- *(ci)* Restore ci-security ref to @v1.4 after standard-actions 1.4.2 release (#379)
- Remove auto-close linkage keywords from st-submit-pr
## [1.4.2] - 2026-04-29

### ⚙️ Miscellaneous Tasks

- Merge main into release/1.4.2
- Prepare release 1.4.2
## [1.4.1] - 2026-04-28

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.1
## [develop-v1.4.2] - 2026-04-29

### 🚀 Features

- *(docker)* Decouple standard-tooling from dev container images (#362) (#364)

### 📚 Documentation

- *(spec)* Fix spec-plan alignment issues from pushback review (#366)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.2 (#361)
## [develop-v1.4.1] - 2026-04-28

### 📚 Documentation

- *(fleet)* Rewrite docs for host-install model and deprecate include-and-remember

### ⚙️ Miscellaneous Tasks

- Bump version to 1.4.1
- Retrigger CI after adding issue linkage
- *(ci)* Upgrade standard-actions from @v1.3 to @v1.4
- *(cli)* Audit st-* catalog: remove broken entry points, add CLI tools overview
- *(cli)* Change st-submit-pr default linkage from Fixes to Ref (#358)
## [1.4.0] - 2026-04-28

### ⚙️ Miscellaneous Tasks

- Prepare release 1.4.0
## [develop-v1.4.0] - 2026-04-28

### 🚀 Features

- *(ci)* Add post-publish workflow to verify dev container images carry the released version

### 🐛 Bug Fixes

- *(docker)* Replace docker info with docker version for daemon reachability check
- *(finalize)* Auto-chdir to main worktree instead of erroring from a secondary worktree
- *(merge)* Skip --delete-branch when running from a secondary worktree
- *(ci)* Bump stale standard-actions pins from @v1.1 to @v1.3
- *(docker-run)* Support --help and -h as program options

### ⚙️ Miscellaneous Tasks

- Bump version to 1.3.5
- *(ci)* Delete ci-push.yml; collapse three-tier CI to two-tier
- *(ci)* Migrate standard-actions refs from @develop to @v1.3
- *(workflows)* Remove add-to-project.yml workflow
- *(cli)* Remove st-list-project-repos and st-set-project-field
- *(observatory)* Remove st-observatory and dead-code registry module
- *(version)* Bump version to 1.4.0
- *(version)* Regenerate lockfile for 1.4.0
## [1.3.4] - 2026-04-27

### ⚙️ Miscellaneous Tasks

- Prepare release 1.3.4
## [develop-v1.3.4] - 2026-04-27

### 🐛 Bug Fixes

- *(packaging)* Declare GPL-3.0-only license metadata in pyproject.toml

### ⚙️ Miscellaneous Tasks

- Bump version to 1.3.4
## [1.3.3] - 2026-04-27

### ⚙️ Miscellaneous Tasks

- Prepare release 1.3.3
## [develop-v1.3.3] - 2026-04-27

### 🐛 Bug Fixes

- *(finalize)* Auto-remove worktree before deleting merged branch (#315)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.3.3
## [1.3.2] - 2026-04-26

### ⚙️ Miscellaneous Tasks

- Prepare release 1.3.2
## [develop-v1.3.2] - 2026-04-26

### ⚙️ Miscellaneous Tasks

- Bump version to 1.3.2
## [1.3.1] - 2026-04-26

### 🐛 Bug Fixes

- *(release)* Regenerate v1.3.1 notes with --unreleased

### ⚙️ Miscellaneous Tasks

- Prepare release 1.3.1
## [develop-v1.3.1] - 2026-04-26

### 🚀 Features

- *(publish)* Dispatch standard-tooling-released event after release tag (#301)
- *(validate-local)* Add yamllint to canonical validation; pin rules in .yamllint (#302)

### 🐛 Bug Fixes

- *(prepare-release)* Use --unreleased instead of --latest for release notes (#298)
- *(validate-local)* Remove dead skip-filter from _find_yaml_files
- *(validate-local)* Move Path import into TYPE_CHECKING block (TC003)
- *(docs)* Use reference-style links to satisfy markdownlint and lint
- *(docs)* Add S607 noqa for gh CLI invocation
- *(finalize)* Use shutil.which to get gh absolute path (S607)
- *(finalize)* Use 'git branch -D' for already-vetted merged branches (#307)

### 📚 Documentation

- *(releasing)* Document patch/minor/major release workflow; add docs-publish sanity check (#303)

### 🎨 Styling

- *(prepare-release)* Wrap git-cliff cmd tuple to satisfy line-length lint
- *(finalize)* Apply ruff format

### ⚙️ Miscellaneous Tasks

- Bump version to 1.3.1
## [1.3.0] - 2026-04-26

### 🐛 Bug Fixes

- *(release)* Regenerate v1.3.0 release notes with correct content

### ⚙️ Miscellaneous Tasks

- Prepare release 1.3.0
## [develop-v1.3.0] - 2026-04-26

### 🚀 Features

- *(release)* Add Rust/Cargo ecosystem support to st-prepare-release (#176)
- *(prepare-release)* Add claude-plugin ecosystem detector (#186)
- Run st-validate-local after finalization (#201)
- *(markdown-standards)* Add single-file mode and remove sphinx references (#203)
- Container-first validation infrastructure (#205)
- Add docker-docs wrapper for containerised docs preview (#209)
- Port all bash scripts to Python entry points (#216)
- Pass GH_TOKEN through to dev containers (#223)
- Add st-docker-run general-purpose container command wrapper (#239)
- Add dual-venv host bootstrap for st-docker-run (#240)
- Mount ~/.ssh in container for git SSH remote operations (#253)
- Run validation via st-docker-run in st-finalize-repo (#254)
- Adopt git worktree convention for parallel AI agent development (#264)
- *(release)* Add st-merge-when-green and stop auto-merging PRs in st-submit-pr/st-prepare-release (#276)
- Refuse feature-branch commits from main worktree (#259) (#275)

### 🐛 Bug Fixes

- Scope markdownlint to docs/site and README.md only (#197) (#200)
- Accept st-docker-test entry point in validate-local preflight (#218)
- Use GHCR image URLs as default dev container references (#232)
- Update docker-test references to st-docker-test (#234)
- Mount host .gitconfig into container for git identity (#245)
- Mock Path.home in docker_test empty volumes test (#246)
- Remove individual validation commands from CLAUDE.md (#250)
- *(finalize)* Refuse to run from a secondary worktree (#278)
- *(git)* Set ST_COMMIT_CONTEXT=1 in git.run for commit calls (#295) (#296)

### 🚜 Refactor

- *(validation)* Normalize validation stack to one container per run (#282)
- *(commit)* Consolidate pre-commit checks into st-commit; add env-var gate (#292)

### 📚 Documentation

- Add consolidated git-workflow guide as canonical entry point (#271)
- Rewrite onboarding docs for Docker/plugin/worktree reality (#273)
- *(specs)* Add git-URL dev-dependency convention spec (#285)
- *(specs)* Reject git-URL dev-dep approach; add pushback report (#287)
- *(specs)* Add host-level-tool spec, plan, pushback, and alignment artifacts (#290)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.2.3
- Update dependencies for next development cycle (#172)
- *(plugins)* Install standard-tooling plugin via marketplace (#180)
- *(docs)* Strip CLAUDE.md boilerplate now covered by plugin (#183)
- Use .markdownlintignore for lint exclusions (#195)
- Remove commit-msg hook and commit-message linter (#196)
- Use dev-docs container for docs CI (#210)
- Restore standards-compliance after wrapper fallback landed (#219)
- Update CLAUDE.md for docker-only standard-tooling (#221)
- Ban MEMORY.md usage in CLAUDE.md (#225)
- Remove legacy bash wrapper scripts and use st-* entry points directly (#227)
- Add .coverage to .gitignore (#229)
- Rename dev-docs references to dev-base (#252)
- Remove MEMORY.md ban from CLAUDE.md (#267)
## [1.2.2] - 2026-03-01

### ⚙️ Miscellaneous Tasks

- Prepare release 1.2.2
## [develop-v1.2.2] - 2026-03-01

### 🚀 Features

- Add Rust development tooling (#139)
- *(generate)* Add st-generate-commands CLI for multi-language MQSC method generation (#154)
- *(labels)* Add canonical label registry and sync modes to st-ensure-label (#164)

### 🐛 Bug Fixes

- Ruby list DISPLAY methods without name_default use required positional name param (#158)

### 📚 Documentation

- Move Releases nav to right of Home for consistency (#136)
- Add multi-repo finalization workflow rules to CLAUDE.md (#156)
- Add Python 3.12 to dev-python version matrix in CLAUDE.md (#166)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.2.2
- *(ci)* Pass run-standards and run-security flags to ci-security workflow (#137)
- Deploy standardized issue template (#163)
- Add concurrency group to ci-push workflow (#167)
## [1.2.1] - 2026-02-26

### ⚙️ Miscellaneous Tasks

- Prepare release 1.2.1
## [develop-v1.2.1] - 2026-02-26

### 🚀 Features

- *(release)* Add Ruby ecosystem detection to st-prepare-release (#88)
- Add st-observatory CLI for cross-repo health reports (#91)
- *(hooks)* Allow dots in branch name validation (#93)
- *(docker)* Add Docker dev images and docker-test script (#96)
- *(docker)* Publish dev container images to GHCR (#98)
- *(docker)* Add shellcheck and markdownlint to all dev images (#110)
- *(release)* Generate per-release verbose release notes files (#114)
- *(docker)* Add CI quality gates for dev container images (#121)
- *(docker)* Harden dev images with patched base packages, Node 22 LTS, shellcheck 0.11.0 (#126)

### 🐛 Bug Fixes

- Resolve commit-msg hook fallback relative to hook directory, not consuming repo (#90)
- *(docker)* Disable fail-fast in docker-publish matrix (#100)
- *(docker)* Bump Go dev images from 1.23 to 1.25 and 1.26 (#105)
- *(docker)* Use v2 module path for go-licenses (#107)
- *(docker)* Use hadolint binary instead of container to avoid musl/node24 incompatibility (#122)
- *(docker)* Suppress DL3028 hadolint warning for gem version pinning (#123)
- Exclude auto-generated markdown from markdownlint (#125)
- *(docker)* Add SHELL pipefail directive to all Dockerfiles (#127)
- *(docker)* Expand trivyignore with upstream-unfixable CVEs (#128)

### 🚜 Refactor

- Remove --docs-only flag from st-submit-pr (#117)

### 📚 Documentation

- Add three-tier CI architecture guide (#109)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.2.1
- Trigger CI with updated PR body
- *(docker)* Add Python 3.12 and Java 17 to image matrix (#104)
- Migrate CI to three-tier model (#112)
- Add per-release notes and generate retroactive release notes (#116)
- *(dev)* Add typecheck.sh for mypy type checking (#119)
- *(docs)* Add Releases nav section with navigation.indexes support (#130)
## [1.2.0] - 2026-02-23

### ⚙️ Miscellaneous Tasks

- Prepare release 1.2.0
- Merge main into release/1.2.0
- Prepare release 1.2.0
## [develop-v1.2.0] - 2026-02-23

### 🚀 Features

- *(hooks)* Add chore/ as allowed branch prefix in pre-commit hook (#66)
- Restructure as Python package with PATH-based consumption (#73)
- *(lint)* Add commit-messages range validator for CI (#76)

### 🐛 Bug Fixes

- Update add-to-project action to v1.0.2 (#64)
- *(ci)* Read version from pyproject.toml in publish and docs workflows (#82)

### 🚜 Refactor

- *(lint)* Remove commit-messages range validator (#77)

### 📚 Documentation

- *(hooks)* Document git hooks and validation rules (#71)
- Add MkDocs documentation site (#72)
- Update documentation site for PATH-based architecture (#79)

### 🧪 Testing

- Achieve 100% line and branch coverage for all Python modules (#74)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.1.5
## [1.1.4] - 2026-02-21

### ⚙️ Miscellaneous Tasks

- Prepare release 1.1.4
## [develop-v1.1.4] - 2026-02-21

### 🚀 Features

- Annotate synced scripts with provenance comments (#58)

### 🐛 Bug Fixes

- Validate CHANGELOG.md with markdownlint before committing (#55)
- Allow merge commits through commit-msg hook (#57)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.1.4
## [1.1.3] - 2026-02-21

### 🐛 Bug Fixes

- Fix CHANGELOG.md formatting for markdownlint compliance

### ⚙️ Miscellaneous Tasks

- Merge main into release/1.1.3
- Prepare release 1.1.3
## [1.1.2] - 2026-02-21

### 🐛 Bug Fixes

- Fix CHANGELOG.md formatting for markdownlint compliance

### ⚙️ Miscellaneous Tasks

- Merge main into release/1.1.2
- Prepare release 1.1.2
## [1.1.1] - 2026-02-20

### 🐛 Bug Fixes

- Fix CHANGELOG.md formatting for markdownlint compliance

### ⚙️ Miscellaneous Tasks

- Merge main into release/1.1.1
- Prepare release 1.1.1
## [1.1.0] - 2026-02-20

### 🐛 Bug Fixes

- Fix CHANGELOG.md formatting for markdownlint compliance

### ⚙️ Miscellaneous Tasks

- Merge main into release/1.1.0
- Prepare release 1.1.0
## [develop-v1.1.3] - 2026-02-21

### 🐛 Bug Fixes

- Strip ^{} suffix from dereferenced tags in sync-tooling.sh (#51)

### ⚙️ Miscellaneous Tasks

- Bump version to 1.1.3 (#48)
## [develop-v1.1.2] - 2026-02-21

### 🚀 Features

- Add add-to-project workflow for standards project
- Add GitHub Project helper scripts for skill automation (#12)
- Add ci and build to allowed conventional commit types (#13)
- Add commit and PR submission wrapper scripts (#17)
- *(submit-pr)* Support cross-repo issue references (#23)
- *(release)* Add VERSION file detector to prepare_release.py (#27)
- *(ci)* Add category prefixes to CI job names (#31)
- *(validate)* Add validate_local.sh dispatch architecture (#34)
- *(hooks)* Validate issue-linked branch names in pre-commit hook (#44)
- *(ci)* Add publish workflow for automated tagging and version bumps (#46)

### 🐛 Bug Fixes

- *(lint)* Accept cross-repo issue references in PR linkage check (#36)

### 📚 Documentation

- Document release-before-sync requirement (#20)
- Ban MEMORY.md usage in CLAUDE.md (#32)
- Ban heredocs in shell commands (#33)

### ⚙️ Miscellaneous Tasks

- Add commit.sh and submit-pr.sh to managed files list (#18)
- Bump version to 1.1.1 (#37)
- *(ci)* Remove push trigger from CI workflow (#41)
## [0.0.0-test] - 2026-02-17

### 🚀 Features

- Add CI workflow, CLAUDE.md, and repository infrastructure (#6)
## [1.0.2] - 2026-02-17

### 🐛 Bug Fixes

- Prevent --actions-compat from leaking during self-update re-exec
## [1.0.1] - 2026-02-17

### 📚 Documentation

- Add canonical source comment to repo-profile.sh
## [1.0.0] - 2026-02-17

### 🚀 Features

- Initial scaffold with reconciled canonical scripts

### 🐛 Bug Fixes

- Handle empty docsite_files array with set -u
