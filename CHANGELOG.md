# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.4.21] - 2026-05-06

### Bug fixes

- use str() in _check_names to satisfy ty type checker
- restore Python 3.12+ support and auto-prepend .venv/bin in st-validate

### Features

- add GHAS check runs (Semgrep OSS, Trivy) to CI gates required checks
- add --config flag to override remote config source

## [1.4.20] - 2026-05-06

### Bug fixes

- prepend .venv/bin to PATH in st-validate for CI compatibility
- use explicit argument lists in command registry instead of string splitting
- add missing type annotations in test files for mypy strict mode
- add ty as dev dependency and resolve ty type checker diagnostics

### CI

- remove bespoke lint and typecheck jobs duplicated by ci-quality.yml
- remove all bespoke jobs, use reusable workflows for test, audit, and release
- trigger fresh workflow run after standard-actions fix
- trigger fresh CI run after standard-actions self-install fix
- trigger fresh CI run after dev container image rebuild
- trigger fresh CI run after standard-actions PATH fix
- trigger fresh CI run after standard-actions PATH fix for all jobs
- use Python container for common, standards, and release jobs

### Chores

- require Python 3.14 as minimum version

### Documentation

- add claude-plugin to primary-language spec

### Features

- add claude-plugin to primary-language enum
- add claude-plugin version discovery, read, and write

### Refactoring

- remove .venv/bin PATH logic from st-validate

### Styling

- format test_version.py
- format github_config.py for ruff on Python 3.14

### Testing

- add failing test for st-version claude-plugin show
- add bump test for claude-plugin
- verify claude-plugin skips lockfile maintenance
- verify error on missing version key in plugin.json

## [1.4.19] - 2026-05-05

### Bug fixes

- suppress S101 on type-narrowing assertion
- run independent check commands to completion instead of short-circuiting on first failure
- use cmd.split() instead of shell=True for subprocess.run

### Features

- fix Python commands and add install entries to command registry
- add st-validate command with registry-driven check dispatch
- add hadolint and actionlint to common validation checks
- add st-version library and CLI with per-language version discovery, show, bump, and --ref support

### Refactoring

- replace _WARMUP_COMMANDS with registry-driven install lookup
- call st-validate instead of st-validate-local in post-finalization

### Styling

- apply ruff format to new and modified files
- apply ruff format to test_st_validate

### Testing

- achieve 100% branch coverage for st-validate and st-version

## [1.4.18] - 2026-05-05

### Bug fixes

- skip uv tool install when running in the standard-tooling repo itself
- resolve CI failures: mypy no-any-return and ruff format

### Chores

- sweep post-1.4.17 dependency updates

## [1.4.17] - 2026-05-05

### Bug fixes

- add safe.directory for git worktree tests in CI container

### Chores

- upgrade workflows to standard-actions v1.5 and add CI derivation config

### Documentation

- update host-level-tool spec for unified consumption model
- update CLAUDE.md consumption model for unified install

### Features

- unify cache install — Python now gets uv tool install

## [1.4.16] - 2026-05-05

### Bug fixes

- normalize audit comparison for patterns ordering and API default fields

### Chores

- sweep post-1.4.15 dependency updates

### Styling

- apply ruff format

## [1.4.15] - 2026-05-05

### Bug fixes

- add explicit type annotations to read_json() for mypy
- include enabled field in actions permissions PUT body

### Chores

- sweep post-1.4.14 dependency updates (#492)

### Features

- add desired state data model
- add read_json() helper for gh api calls
- add [ci] section to standard-tooling.toml schema
- add [github] override section to TOML schema
- add fixed desired state functions
- add per-language command registry
- add CI gates ruleset derivation
- add compute_desired_state() top-level function
- add fetch_actual_state() for GitHub API reads
- add diff computation engine
- add st-github-config CLI with audit and diff modes
- implement apply mode for st-github-config CLI
- add classic branch protection cleanup during apply

### Styling

- format test_config.py with ruff
- apply ruff format to new files

### Testing

- cover _lang_has_check unknown check kind branch

## [1.4.14] - 2026-05-04

### Bug fixes

- fix formatting and add coverage for invalid ignore type

### Chores

- retrigger CI with issue linkage (#482)
- sweep post-1.4.13 dependency updates (#482)

### Documentation

- update published docs for bundled markdownlint config
- add cross-repo cleanup implementation plan

### Features

- add [markdownlint].ignore support to standard-tooling.toml

## [1.4.13] - 2026-05-03

### CI

- bump standard-actions CI pin to v1.4.7

### Chores

- add memory management policy (#474)
- replace blanket chore skip with targeted mechanical-commit filters (#475)

### Documentation

- markdownlint standardization spec, plan, and reviews (#476) (#478)
- narrow markdownlint standardization to published docs scope (#476) (#480)

### Features

- fail on dirty working tree after cleanup (#477)
- bundle canonical config and remove per-repo configs (#476) (#481)

## [1.4.12] - 2026-05-01

### Bug fixes

- make --title required in st-submit-pr

### Features

- add _checks_registered probe and polling loop to wait_for_checks

### Styling

- apply ruff format to test_github.py
- fix line length in test_main_dry_run_release_branch
- apply ruff format to test_submit_pr.py

### Testing

- cover wait_for_checks polling behavior

## [1.4.11] - 2026-05-01

### Features

- add container guard to st-validate-local

## [1.4.10] - 2026-05-01

### Bug fixes

- Fix --pull=always breaking cached image lookup; route Python through cache
- Fix ruff format violations

## [1.4.9] - 2026-05-01

### Chores

- organize plans into proposed/in-progress/completed lifecycle

## [1.4.8] - 2026-05-01

### Bug fixes

- replace pip install with uv tool install in docker cache build

### Chores

- add consumer-refresh config to standard-tooling.toml
- remove shutil.which guards and make docs failure fatal
- remove _ensure_tool guard and shutil.which dependency
- remove markdownlint shutil.which guard
- remove all pip install references from host-level-tool spec
- update docstring and validation failure label to reflect fatal semantics
- fix S607 noqa, duplicate pytest import, and pip reference in releasing guide

### Documentation

- add spec, plan, and pushback review for uv tool install and guard cleanup

## [1.4.7] - 2026-04-30

### Bug fixes

- force-update tags on git fetch to prevent stale local state
- add --pull=always to docker run to prevent stale image cache
- use uv run for validation in Python repos during finalization

### Chores

- next-cycle dependency updates for 1.4.6
- retire st-config.toml in favor of standard-tooling.toml
- retrigger CI after standard-actions v1.4.5
- retrigger CI (force action cache refresh)
- retrigger CI (GitHub Actions tag cache)
- trigger CI for PR #417
- pin ci-security workflow to v1.4.5 to bypass tag cache
- pin ci-security workflow to v1.4.6
- remove dead validate_local_common wrapper

## [1.4.6] - 2026-04-29

### Chores

- change license from GPL-3.0-only to GPL-3.0-or-later

## [1.4.5] - 2026-04-29

### Bug fixes

- eliminate unreachable elif branch for full coverage

### Chores

- seed standard-tooling.toml with this repo's values
- delete repo_profile.py — replaced by config.read_config

### Documentation

- add spec, plan, and reviews for standard-tooling.toml migration (#363)
- strip config sections from repository-standards.md, update references

### Features

- add typed TOML reader for standard-tooling.toml

### Refactoring

- migrate st-commit from repo_profile to config.read_config
- migrate st-validate-local from repo_profile to config.read_config
- migrate st-finalize-repo from repo_profile to config.read_config
- rewrite repo-profile-cli to validate standard-tooling.toml

### Styling

- fix ruff TC003 and SIM117 lint errors
- apply ruff format to modified files

### Testing

- add failing tests for standard-tooling.toml reader
- rewrite repo-profile-cli tests for TOML validation
- add missing coverage for ConfigError handlers and dead code removal

## [1.4.4] - 2026-04-29

### Bug fixes

- reject invocation from secondary worktree instead of os.chdir
- re-allow legacy chore/bump-version and chore/next-cycle-deps branch prefixes

### Features

- add st-wait-until-green command for CI polling

### Styling

- move Path import to TYPE_CHECKING block
- fix import ordering in release.py

## [1.4.3] - 2026-04-29

### Bug fixes

- drop --delete-branch from st-merge-when-green; st-finalize-repo handles cleanup
- use CWD-relative README.md lookup in repo-profile instead of git.repo_root
- retain st-markdown-standards as markdownlint-only entry point for CI compatibility

### CI

- retrigger checks after adding issue linkage

### Chores

- update all Python dependencies for next cycle
- remove docker dispatch and verification pipeline
- point ci-security ref to @develop for install fix (#362)
- restore ci-security ref to @v1.4 after standard-actions 1.4.2 release (#379)
- remove auto-close linkage keywords from st-submit-pr

### Documentation

- update spec and docs for cache-first architecture (#362)
- mark all decouple plan phases complete with PR refs and follow-up issue links (#385)

### Features

- add st-check-pr-merge and branch check in st-merge-when-green
- add next-cycle-deps pattern to release branch allow-list

### Refactoring

- unify release-cycle branches under release/ prefix
- decompose st-markdown-standards: direct markdownlint in validate-local, structural checks in repo-profile

### Styling

- apply ruff format to new and modified files
- apply ruff format to test files

### Testing

- add coverage tests for check_pr_merge edge cases

## [1.4.2] - 2026-04-29

### Documentation

- fix spec-plan alignment issues from pushback review (#366)

### Features

- decouple standard-tooling from dev container images (#362) (#364)

## [1.4.1] - 2026-04-28

### Chores

- retrigger CI after adding issue linkage
- upgrade standard-actions from @v1.3 to @v1.4
- audit st-* catalog: remove broken entry points, add CLI tools overview
- change st-submit-pr default linkage from Fixes to Ref (#358)

### Documentation

- rewrite docs for host-install model and deprecate include-and-remember

## [1.4.0] - 2026-04-28

### Bug fixes

- replace docker info with docker version for daemon reachability check
- auto-chdir to main worktree instead of erroring from a secondary worktree
- skip --delete-branch when running from a secondary worktree
- bump stale standard-actions pins from @v1.1 to @v1.3
- support --help and -h as program options

### Chores

- delete ci-push.yml; collapse three-tier CI to two-tier
- migrate standard-actions refs from @develop to @v1.3
- remove add-to-project.yml workflow
- remove st-list-project-repos and st-set-project-field
- remove st-observatory and dead-code registry module

### Features

- add post-publish workflow to verify dev container images carry the released version

## [1.3.4] - 2026-04-27

### Bug fixes

- declare GPL-3.0-only license metadata in pyproject.toml

## [1.3.3] - 2026-04-27

### Bug fixes

- auto-remove worktree before deleting merged branch (#315)

## [1.3.2] - 2026-04-26

### Bug fixes

- regenerate v1.3.1 notes with --unreleased

## [1.3.1] - 2026-04-26

### Bug fixes

- regenerate v1.3.0 release notes with correct content
- use --unreleased instead of --latest for release notes (#298)
- remove dead skip-filter from _find_yaml_files
- move Path import into TYPE_CHECKING block (TC003)
- use reference-style links to satisfy markdownlint and lint
- add S607 noqa for gh CLI invocation
- use shutil.which to get gh absolute path (S607)
- use 'git branch -D' for already-vetted merged branches (#307)

### Documentation

- document patch/minor/major release workflow; add docs-publish sanity check (#303)

### Features

- dispatch standard-tooling-released event after release tag (#301)
- add yamllint to canonical validation; pin rules in .yamllint (#302)

### Styling

- wrap git-cliff cmd tuple to satisfy line-length lint
- apply ruff format

## [1.3.0] - 2026-04-26

### Bug fixes

- scope markdownlint to docs/site and README.md only (#197) (#200)
- accept st-docker-test entry point in validate-local preflight (#218)
- use GHCR image URLs as default dev container references (#232)
- update docker-test references to st-docker-test (#234)
- mount host .gitconfig into container for git identity (#245)
- mock Path.home in docker_test empty volumes test (#246)
- remove individual validation commands from CLAUDE.md (#250)
- refuse to run from a secondary worktree (#278)
- set ST_COMMIT_CONTEXT=1 in git.run for commit calls (#295) (#296)

### CI

- use dev-docs container for docs CI (#210)
- restore standards-compliance after wrapper fallback landed (#219)

### Chores

- update dependencies for next development cycle (#172)
- install standard-tooling plugin via marketplace (#180)
- strip CLAUDE.md boilerplate now covered by plugin (#183)
- use .markdownlintignore for lint exclusions (#195)
- remove commit-msg hook and commit-message linter (#196)
- update CLAUDE.md for docker-only standard-tooling (#221)
- ban MEMORY.md usage in CLAUDE.md (#225)
- remove legacy bash wrapper scripts and use st-* entry points directly (#227)
- add .coverage to .gitignore (#229)
- rename dev-docs references to dev-base (#252)
- remove MEMORY.md ban from CLAUDE.md (#267)

### Documentation

- add consolidated git-workflow guide as canonical entry point (#271)
- rewrite onboarding docs for Docker/plugin/worktree reality (#273)
- add git-URL dev-dependency convention spec (#285)
- reject git-URL dev-dep approach; add pushback report (#287)
- add host-level-tool spec, plan, pushback, and alignment artifacts (#290)

### Features

- add Rust/Cargo ecosystem support to st-prepare-release (#176)
- add claude-plugin ecosystem detector (#186)
- run st-validate-local after finalization (#201)
- add single-file mode and remove sphinx references (#203)
- container-first validation infrastructure (#205)
- add docker-docs wrapper for containerised docs preview (#209)
- port all bash scripts to Python entry points (#216)
- pass GH_TOKEN through to dev containers (#223)
- add st-docker-run general-purpose container command wrapper (#239)
- add dual-venv host bootstrap for st-docker-run (#240)
- mount ~/.ssh in container for git SSH remote operations (#253)
- run validation via st-docker-run in st-finalize-repo (#254)
- adopt git worktree convention for parallel AI agent development (#264)
- add st-merge-when-green and stop auto-merging PRs in st-submit-pr/st-prepare-release (#276)
- refuse feature-branch commits from main worktree (#259) (#275)

### Refactoring

- normalize validation stack to one container per run (#282)
- consolidate pre-commit checks into st-commit; add env-var gate (#292)

## [1.2.2] - 2026-03-01

### Bug fixes

- Ruby list DISPLAY methods without name_default use required positional name param (#158)

### CI

- add concurrency group to ci-push workflow (#167)

### Chores

- update ruff 0.15.4, certifi 2026.2.25, hadolint 2.14.0 (#134)
- pass run-standards and run-security flags to ci-security workflow (#137)
- deploy standardized issue template (#163)

### Documentation

- move Releases nav to right of Home for consistency (#136)
- add multi-repo finalization workflow rules to CLAUDE.md (#156)
- add Python 3.12 to dev-python version matrix in CLAUDE.md (#166)

### Features

- add Rust development tooling (#139)
- add st-generate-commands CLI for multi-language MQSC method generation (#154)
- add canonical label registry and sync modes to st-ensure-label (#164)

## [1.2.1] - 2026-02-26

### Bug fixes

- resolve commit-msg hook fallback relative to hook directory, not consuming repo (#90)
- disable fail-fast in docker-publish matrix (#100)
- bump Go dev images from 1.23 to 1.25 and 1.26 (#105)
- use v2 module path for go-licenses (#107)
- use hadolint binary instead of container to avoid musl/node24 incompatibility (#122)
- suppress DL3028 hadolint warning for gem version pinning (#123)
- exclude auto-generated markdown from markdownlint (#125)
- add SHELL pipefail directive to all Dockerfiles (#127)
- expand trivyignore with upstream-unfixable CVEs (#128)

### CI

- migrate CI to three-tier model (#112)

### Chores

- trigger CI with updated PR body
- add Python 3.12 and Java 17 to image matrix (#104)
- add per-release notes and generate retroactive release notes (#116)
- add typecheck.sh for mypy type checking (#119)
- add Releases nav section with navigation.indexes support (#130)

### Documentation

- add three-tier CI architecture guide (#109)

### Features

- add Ruby ecosystem detection to st-prepare-release (#88)
- add st-observatory CLI for cross-repo health reports (#91)
- allow dots in branch name validation (#93)
- add Docker dev images and docker-test script (#96)
- publish dev container images to GHCR (#98)
- add shellcheck and markdownlint to all dev images (#110)
- generate per-release verbose release notes files (#114)
- add CI quality gates for dev container images (#121)
- harden dev images with patched base packages, Node 22 LTS, shellcheck 0.11.0 (#126)

### Refactoring

- remove --docs-only flag from st-submit-pr (#117)

## [1.2.0] - 2026-02-23

### Bug fixes

- update add-to-project action to v1.0.2 (#64)
- read version from pyproject.toml in publish and docs workflows (#82)

### Documentation

- document git hooks and validation rules (#71)
- add MkDocs documentation site (#72)
- update documentation site for PATH-based architecture (#79)

### Features

- add chore/ as allowed branch prefix in pre-commit hook (#66)
- restructure as Python package with PATH-based consumption (#73)
- add commit-messages range validator for CI (#76)

### Refactoring

- remove commit-messages range validator (#77)

### Testing

- achieve 100% line and branch coverage for all Python modules (#74)

## [1.1.4] - 2026-02-21

### Bug fixes

- fix CHANGELOG.md formatting for markdownlint compliance
- fix CHANGELOG.md formatting for markdownlint compliance
- fix CHANGELOG.md formatting for markdownlint compliance
- fix CHANGELOG.md formatting for markdownlint compliance
- validate CHANGELOG.md with markdownlint before committing (#55)
- allow merge commits through commit-msg hook (#57)

### Features

- annotate synced scripts with provenance comments (#58)

## [1.1.3] - 2026-02-21

### Bug fixes

- strip ^{} suffix from dereferenced tags in sync-tooling.sh (#51)

## [1.1.2] - 2026-02-21

### Bug fixes

- handle empty docsite_files array with set -u
- prevent --actions-compat from leaking during self-update re-exec
- accept cross-repo issue references in PR linkage check (#36)

### Chores

- add commit.sh and submit-pr.sh to managed files list (#18)
- remove push trigger from CI workflow (#41)

### Documentation

- add canonical source comment to repo-profile.sh
- document release-before-sync requirement (#20)
- ban MEMORY.md usage in CLAUDE.md (#32)
- ban heredocs in shell commands (#33)

### Features

- initial scaffold with reconciled canonical scripts
- add CI workflow, CLAUDE.md, and repository infrastructure (#6)
- add add-to-project workflow for standards project
- add GitHub Project helper scripts for skill automation (#12)
- add ci and build to allowed conventional commit types (#13)
- add commit and PR submission wrapper scripts (#17)
- support cross-repo issue references (#23)
- add VERSION file detector to prepare_release.py (#27)
- add category prefixes to CI job names (#31)
- add validate_local.sh dispatch architecture (#34)
- validate issue-linked branch names in pre-commit hook (#44)
- add publish workflow for automated tagging and version bumps (#46)
