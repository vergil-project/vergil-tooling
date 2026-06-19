# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/)
and this project adheres to [Semantic Versioning](https://semver.org/).

## [2.1.42] - 2026-06-19

### Refactoring

- remove the legacy pr-template.yml workflow (#1701)

## [2.1.41] - 2026-06-18

### Bug fixes

- surface audit-stage failure detail and distinguish audit crash from non-compliance (#1693)
- abort vrg-release under agent identity (#1696)

### Features

- add ansible-lint conditional common check (#1695)
- order sessions list by workspace then slot (#1692)

## [2.1.40] - 2026-06-17

### Features

- grant the USER agent identity issue edit rights (#1686)

## [2.1.39] - 2026-06-17

### Features

- batch the submit/finalize/release pipeline over multiple PRs (#1682)

## [2.1.38] - 2026-06-17

### Bug fixes

- report in-flight worktrees instead of 'not ready' after submission (#1675)

### Chores

- pin Claude marketplace ref to v2.1 in .claude/settings.json (#1677)

## [2.1.37] - 2026-06-16

### Bug fixes

- distinguish unreachable VM from spec drift in preflight (#1666)

### Documentation

- add vergil-forge observability design (#1661) (#1662)

### Features

- derive the Claude marketplace ref from the vergil.toml version (#1663)
- borrow another repo's VM via [vm] shared_from (#1669)

## [2.1.36] - 2026-06-14

### Bug fixes

- self-heal poisoned uv cache in the in-VM tooling install (#1657)

### Documentation

- add vergil-forge local host design (#1655)

## [2.1.35] - 2026-06-12

### Features

- --install runs consumer-refresh as the post-release cascade step (#1646)
- plumb [vm].port_forwards through to the PORT_FORWARDS template param (#1647)
- add --yes to pre-answer confirmation prompts (#1648)

## [2.1.34] - 2026-06-12

### Features

- --release cascades submit, finalize, and release (#1638)

## [2.1.33] - 2026-06-12

### Features

- rebuild creates the VM when it doesn't exist instead of aborting (#1631) (#1632)
- resumable releases via vrg-release --resume (#1633)

## [2.1.32] - 2026-06-11

### Bug fixes

- write release artifacts into the worktree, not repo_root (#1626) (#1627)

## [2.1.31] - 2026-06-11

### Bug fixes

- poll confirm-main job conclusions; choose finalize strategy by branch prefix (#1622)

## [2.1.30] - 2026-06-11

### Bug fixes

- honor oracle-recorded base; drop legacy release/ inference (#1610)

## [2.1.28] - 2026-06-11

### Features

- add vergil ecosystem updater and per-run selector (#1601)
- allow read-only git subcommands and read-only reflog (#1604)
- VM-local Claude plugin lifecycle and .claude share-set cleanup (#1605)

### Refactoring

- run vrg-release in a managed worktree (#1600)

## [2.1.26] - 2026-06-10

### Bug fixes

- omit allow_forking for public org repos (#1585)

### Features

- mechanized dependency-update workflow (Phase 1) (#1586)

## [2.1.25] - 2026-06-10

### Bug fixes

- offer "none of the above" for primary-language (#1580)

## [2.1.24] - 2026-06-09

### Bug fixes

- patient, heartbeating handshake waits; audit acks from state (#1573)

## [2.1.23] - 2026-06-09

### Bug fixes

- push with --force-with-lease to tolerate rebased branch (#1557) (#1562)

### Features

- let report-fixes revise PR metadata mid-workflow (#1565)
- add guarded vrg-reword to fix a branch-local commit message (#1567)

## [2.1.22] - 2026-06-09

### Documentation

- add transcript-mined vrg-* mechanization candidates spec (#1558)

### Features

- add the PR workflow oracle — local engine and judgment prompts (#1544)

## [2.1.21] - 2026-06-09

### Features

- add vrg-worktree-status and make the finalize sweep squash-merge-aware (#1553)

## [2.1.20] - 2026-06-09

### Bug fixes

- size sessions WORKSPACE column to longest path (#1542)
- accept --identity/--config in any argument position (#1546)
- re-assert -n on session resume so Claude restores the prompt-box title (#1548)

## [2.1.19] - 2026-06-08

### Bug fixes

- defer spec-drift check to post-start so stopped VMs can start (#1536)

### Documentation

- capture paywalled security cost as a migration driver (#1531)
- add Vergil GUI vision & feasibility study (#1532) (#1537)

## [2.1.18] - 2026-06-08

### Bug fixes

- stop wrapped lines from overflowing the live viewport (#1518)
- require forking on org repos; clear error on org fork denial (#1524)
- require non-empty issue/title/summary/notes; reject folded scalars (#1525)

### Documentation

- add forge abstraction strategy (Forgejo/Codeberg) (#1522)

### Features

- add vrg-whoami identity-mode resolver CLI (#1526)

## [2.1.17] - 2026-06-08

### Bug fixes

- compare ruleset required checks by set, not list order (#1509)

### CI

- add build-only docs verification job to ci.yml (#1508)

### Features

- resolve validation command from target repo's vergil.toml (#1510)
- resolve vergil.toml from main, then develop, then local (#1512)

## [2.1.16] - 2026-06-07

### Bug fixes

- replace FETCH_HEAD-dependent pull with fetch + ff-merge of remote-tracking ref (#1503)

### Chores

- ignore .claude/scheduled_tasks.lock runtime lock (#1504)

### Features

- add --finalize flag to chain straight into wait-and-merge (#1500)
- add --all to vrg-vm update for bulk fail-deferred updates (#1502)

## [2.1.15] - 2026-06-07

### Bug fixes

- make wait_for_checks resilient to PR head movement
- read custom-title naming events and break slot collisions by recency

## [2.1.14] - 2026-06-07

### Features

- declared GHAS posture with visibility-aware defaults (#1486)

## [2.1.13] - 2026-06-07

### Bug fixes

- keep consumer-refresh commands visible after the summary (#1478)

### Features

- adopt the progress framework in vrg-finalize-pr (#1482)

## [2.1.12] - 2026-06-06

### Bug fixes

- grant actions: read to the security job (#1473)
- stream vrg-finalize-pr output through the progress session (#1471)

## [2.1.11] - 2026-06-06

### Bug fixes

- adopt canonical quote-strip and command-position matcher (#1453)
- guard merged-branch sweep against in-flight worktrees (#1455)
- validate template linkage against ALLOWED_LINKAGES (#1460)
- cycle the VM after create/rebuild so sessions see provisioned groups (#1465)
- unblock close-finalize by making the release cleanup path non-interactive (#1466)

### Features

- stage-aware progress framework for long-running CLI commands (#1456)
- port vrg-vm lifecycle commands to the progress framework (#1457)
- auto-size the RichRenderer rolling window to terminal height (#1462)
- restore a safe agent path to fix its own PR body (#1464)

## [2.1.10] - 2026-06-05

### Documentation

- adopt marker-delimited template region in CLAUDE.md (#1439)

### Features

- marker-delimited CLAUDE.md template region check (#1439)
- per-profile nested virtualization via the [vm] cascade (#1449)

### Testing

- cover multiple-end-marker diagnostic (#1439)

## [2.1.9] - 2026-06-05

### Bug fixes

- include config path in vergil.toml warnings and errors
- provision and detect identity mode so agent VMs never resolve to human
- select App installation from -R/--repo owner (#1413)
- combine and parallelize list probes per running VM (#1414)
- abort with an error when the target PR is already merged (#1420)
- add .vergil/, build/, .superpowers/ to gitignore baseline (#1425)
- allow branch -D for branches tracking integration branches (#1426)
- verify claude_settings marketplace/plugin keys against canonical template (#1427)
- relocate identity-modes section out of CLAUDE.md templated region
- address validation findings from full pipeline run (#1423)

### Chores

- ignore Superpowers plugin state directory
- bump pip to 26.1.2 for PYSEC-2026-196

### Documentation

- refresh stale agent-facing tooling references
- add progress framework design spec for long-running CLI commands
- relocate progress framework spec to docs/specs
- apply pushback review resolutions to progress framework spec
- add progress framework implementation plan
- apply alignment review resolutions to spec and plan
- add submit-pr/finalize-pr interface upgrade design (#1423)
- apply pushback review resolutions to PR interface design (#1423)
- add PR interface upgrade implementation plan (#1423)
- apply alignment review resolutions to PR interface plan (#1423)
- document root launch, PR inference, and wait-for-green (#1423)
- mark PR interface design as implemented (#1423)

### Features

- list enumerates dedicated VMs from instances only (#1412)
- add canonical worktree discovery and selection library (#1423)
- add pr_for_branch, is_draft, and head_ref helpers (#1423)
- add shared fail-fast wait-and-merge engine (#1423)
- resolve target worktree when run from the repo root (#1423)
- infer the target PR from worktrees and always confirm (#1423)
- wait for green before merging and clean the squash-merged branch (#1423)

### Refactoring

- use shared worktree discovery library (#1423)
- delegate wait_and_merge to shared pr_merge engine (#1423)

## [2.1.8] - 2026-06-05

### Chores

- migrate to vergil v2.1

### Refactoring

- remove deprecated vrg-finalize-repo alias

## [2.1.7] - 2026-06-04

### Bug fixes

- join dedicated-VM instance name tiers with '.' not '--'

## [2.1.6] - 2026-06-04

### Bug fixes

- truncate tracking-issue comments to GitHub's size limit

## [2.1.4] - 2026-06-04

### Features

- replace provision hook with declarative apt_repos + vagrant_plugins (#1381)

## [2.1.3] - 2026-06-04

### Documentation

- port Vergil 2.1 workflow & architecture design (§§1–10)

### Features

- add vrg-await blocking file waiter + atomic channel writer
- add pr_checks, pr_reviews, post_check_run helpers
- add vrg-pr-await settle-predicate waiter
- emit pr-watch one-liner and add vergil-audit/approved poster

## [2.1.2] - 2026-06-04

### Documentation

- normalize identities.toml keys to vergil-<role> (#1370)
- document list observability columns (#1370)

### Features

- parse repo [vm] cascade in vergil.toml (#1370)
- parse nested host-override tables in identities.toml (#1370)
- add compose_vm_spec five-tier overlay (#1370)
- add reversible -- instance-name codec (#1370)
- add composed-spec fingerprint (#1370)
- thread profile packages/hook/fingerprint into create_vm (#1370)
- add fingerprint read + drift status (#1370)
- add _resolve_target base-vs-dedicated resolution (#1370)
- build dedicated VMs from the composed spec (#1370)
- add session/start abort gate, tunable staleness, under-warning (#1370)
- route stop/restart/destroy/update through the resolved instance (#1370)
- add process-tree AGENTS/HUMANS occupancy (#1370)
- discover dedicated VMs and orphans (#1370)
- rewrite list with footprint, occupancy, and SPEC columns (#1370)

## [2.1.1] - 2026-06-02

### Features

- add template mode and identity gate for human-triggered PR submission
- consolidate merge + cleanup into vrg-finalize-pr with pre-merge provenance check

## [2.1.0] - 2026-06-02

### Bug fixes

- list live sessions named only in the VM roster

### Features

- add identity-aware allowlists, agent denials, and gh api gating
- detect workflow permission errors on push with identity-aware guidance

### Refactoring

- use plain human-maintainer wording in messages; rename Race Director to Chief Steward in specs

### Styling

- apply ruff format and simplify push error handling

## [2.0.80] - 2026-06-02

### Bug fixes

- treat ambient App installation tokens as app mode

### Documentation

- rewrite onboarding docs to pure GitHub App identity model

## [2.0.79] - 2026-06-02

### Bug fixes

- fail on red checks, not just mergeStateStatus

## [2.0.78] - 2026-06-01

### Bug fixes

- scope session resolve to the current cwd's project slug

## [2.0.77] - 2026-05-31

### Features

- print one-line feedback for the session resolver decision

## [2.0.76] - 2026-05-30

### Chores

- add .vergil/ scratch directory to gitignore

### Documentation

- rework permission model around subset-of-human-rights, drop track mode
- add foundational host-as-owner assumption and merge-time provenance gate
- consolidate merge into vrg-finalize-pr with provenance check; make gh api identity-aware
- add stale-session lifecycle implementation plan
- align list --sessions task with merged host-side architecture

### Features

- add identity-mode detection module for VM mode awareness
- add PR template library for .vergil/ scratch convention
- add session_stale_days/session_archive_days config
- parse archived session labels; guard parse_name
- add last_active to Slot and SessionRow
- add session age-band classifier
- add plan_session: bands, sweep, most-recent resume, --fresh
- tail-read last timestamped entry for session age
- compute last_active map in resolver state
- compute last_active map in resolver state
- archive a session by appending an archived agent-name
- execute session plan: sweep, stale prompt, resume/create/fork
- emit age + state (active/idle/archived) in --list-json
- wire --fresh, thresholds, and list --sessions states/age

### Testing

- satisfy lint and reach full coverage for foundation modules
- complete branch coverage for stale-lifecycle

## [2.0.75] - 2026-05-30

### Chores

- re-sync PR head with develop to clear stale out-of-date state

### Features

- let model cascade from a top-level identities.toml default

## [2.0.74] - 2026-05-29

### Features

- make --model a first-class session argument

## [2.0.73] - 2026-05-29

### Features

- deterministic Claude session naming, resume, slots, and listing

## [2.0.71] - 2026-05-29

### Bug fixes

- keep ~/.claude/sessions VM-local; do not symlink onto host mount (#1301)

## [2.0.70] - 2026-05-29

### Bug fixes

- filter tracking issue search by exact title match
- retry transient UNKNOWN mergeable state
- skip bypass_actors audit with GitHub App credentials
- symlink VM ~/.claude subdirs to path-preserved host mounts (#1296)

### Documentation

- add AI contribution compliance review
- fix P4 evidence to match actual vrg-commit mechanism
- agent permission model and identity architecture design spec
- incorporate pushback review findings into design spec
- add implementation plan for agent permission model (Track A tooling changes)
- apply alignment resolutions to spec and plan

### Features

- add --allow-empty flag to vrg-commit
- add --base flag to override auto-detected target branch
- print CD workflow run URL before watching
- add --skip-audit flag to bypass repo config audit in preflight

## [2.0.69] - 2026-05-28

### Bug fixes

- make language optional in vrg-release-validate-inputs
- add backward-compatible positional arg for transition

### Documentation

- add design spec and plan for cd-release language separation

## [2.0.68] - 2026-05-28

### Bug fixes

- fetch tags and guard promote when --skip-cd is used
- accept base as a valid language in validate-inputs

### Chores

- trigger CI after v2.0 tag rollback
- trigger CI with updated vergil-actions v2.0

### Features

- replace --skip-cd with --skip-cd-docs

## [2.0.67] - 2026-05-27

### Bug fixes

- pin vergil-actions workflows to v2.0 tag instead of develop

### Features

- add --timeout to start/rebuild and fix mkdir for tooling-tag
- add --skip-cd flag to bypass CD verification

## [2.0.66] - 2026-05-27

### Bug fixes

- call trivy directly instead of wrapping in docker run

### Features

- mount ~/.claude/sessions into VM for session persistence

## [2.0.65] - 2026-05-27

### Features

- forward terminal env vars in vrg-vm session

## [2.0.64] - 2026-05-27

### Bug fixes

- treat --tag as temporary override, not persistent
- rename _ST_GIT_URL to _VRG_GIT_URL

### Features

- show status check delta instead of full lists (#1210)
- show version delta on update

## [2.0.63] - 2026-05-27

### Bug fixes

- output shell strings instead of JSON arrays
- replace _checks_registered with SHA-pinned REST API query
- make wait_for_checks commit-aware and drop fail-fast
- update release subprocess wait_for_checks to use SHA-pinned flow
- rename local variable to avoid CodeQL false positive

### Documentation

- add commit-aware check waiting design spec
- apply pushback review findings to check-waiting spec
- add commit-aware check waiting implementation plan

### Features

- host-path-preserving workspace mount and Claude sub-mounts
- add head_sha() to resolve PR HEAD commit

### Refactoring

- remove unused _NO_CHECKS_PHRASE constant

### Styling

- apply ruff formatting to modified files

## [2.0.62] - 2026-05-27

### Bug fixes

- warn instead of reject unrecognized primary-language values

## [2.0.61] - 2026-05-27

### Bug fixes

- fix type checker errors for optional primary-language
- require GHAS CodeQL check in CI gates ruleset
- rename credential_str to secret_name to resolve CodeQL false positive
- rename credential_secret to credential_secret_name across dataclass and CLI
- stop printing credential secret names to stdout
- eliminate all print paths for credential secret name
- use dataclasses.asdict() to break CodeQL taint chain for credential name
- rename credential_secret_name to publish_env_var to eliminate CodeQL taint source
- restore separate CI/interactive output branches
- correct deny reason in guard.sh fallback
- inject credentials for remote operations in library

### Documentation

- apply alignment review fixes to phase 1 plan

### Features

- add TTY-aware CI/interactive output module
- add unified language metadata registry with ecosystem data
- make primary-language optional, restrict to five real languages
- add vrg-ecosystem-resolve CLI for language metadata lookup
- add vrg-release-validate-inputs CLI for release input validation
- add Phase 2 shell-to-Python utilities (#1188, #1189, #1190)
- add issue reopen to allowed subcommands
- add Phase 3 shell-to-Python utilities (#1186, #1183)
- add Phase 4 security scan orchestration utilities (#1182, #1181, #1191)

### Refactoring

- migrate all callers from validate_commands to languages
- remove shell, none, and claude-plugin from version handling
- handle optional primary-language in repo_init and github_config
- remove stale none language check and apply formatting

### Testing

- restore 100% test coverage for new and modified code

## [2.0.60] - 2026-05-26

### Bug fixes

- add rm to subcommand allowlist
- strip quoted strings before matching raw command names

### Documentation

- add VM voice-to-text design spec

## [2.0.59] - 2026-05-26

### Bug fixes

- detect containers via /proc/1/mountinfo overlay check

## [2.0.58] - 2026-05-26

### Bug fixes

- add 'repo list' to subcommand allowlist
- use Bearer auth for JWT-authenticated API calls

### Features

- inject host VCS identity into VM during credential setup

## [2.0.57] - 2026-05-26

### Features

- add cpus, memory, disk resource fields
- validate cpus, memory, disk syntax at config load
- accept resource overrides in create_vm
- pass identity resource overrides through create and rebuild

### Styling

- fix nested-if and Yoda condition lint warnings
- apply ruff formatting

## [2.0.56] - 2026-05-26

### Bug fixes

- fix guard.sh fallback regex matching command names in filenames
- raise fatal error when no CI checks register after poll timeout
- sync VERSION file and lockfile with pyproject.toml bump to 2.0.56

### Documentation

- add design spec for configurable container env passthrough (#777)
- move design spec to docs/specs/ (#777)
- address pushback: breaking change note and caller accuracy (#777)
- add implementation plan for configurable container env passthrough (#777)
- update docs and fix lint for configurable env passthrough (#777)

### Features

- replace git pre-commit hook with Claude Code PreToolUse hook (#1135, #724)
- add [container].env-prefixes section to vergil.toml (#777)
- make env-var passthrough configurable via env_prefixes parameter (#777)
- wire vrg-container-run to [container].env-prefixes config (#777)
- wire vrg-container-test to [container].env-prefixes config (#777)
- wire vrg-container-docs to [container].env-prefixes config (#777)
- add mv to vrg-git subcommand allowlist (#1134)
- update repo_init and docs for Claude Code hook guard

### Refactoring

- rename StConfig to VergilConfig and st_config to vergil_config (#1136)

## [2.0.54] - 2026-05-25

### Documentation

- stateless VM lifecycle design spec
- stateless VM lifecycle implementation plan

### Features

- vm_age_days reads VM creation time from Lima metadata
- copy_claude_config copies CLAUDE.md and settings.json into VM
- try_update_tooling wraps update with graceful fallback
- staleness enforcement and auto-update in vrg-vm start
- staleness enforcement in vrg-vm session
- vrg-vm rebuild command for stateless VM lifecycle

## [2.0.53] - 2026-05-25

### Bug fixes

- use contains instead of startswith for CD job name matching

## [2.0.52] - 2026-05-25

### Bug fixes

- fall back to config vergil version when no tooling marker exists

### Chores

- bump version to 2.0.52

## [2.0.51] - 2026-05-25

### Bug fixes

- poll for CD run matching branch HEAD instead of taking most recent

### Chores

- bump version to 2.0.51

## [2.0.50] - 2026-05-25

### Chores

- bump version to 2.0.50

## [2.0.49] - 2026-05-25

### Chores

- temporarily point vergil-actions refs to @develop
- bump version to 2.0.49

### Features

- add vrg-vm update and auto-update on session entry

## [2.0.47] - 2026-05-25

### Bug fixes

- use startswith for CD job name matching in confirm phase

### Chores

- bump version to 2.0.47

## [2.0.46] - 2026-05-25

### Bug fixes

- validate runtime against allowlist before execvp
- use literal executables in execvp to satisfy Semgrep taint analysis
- surface subprocess stderr on failure across all lib modules
- update CLAUDE.md consumer template for vrg-container-run rename

### Chores

- bump version to 2.0.46

### Documentation

- add orchestrator refactor implementation plan
- move orchestrator refactor plan to docs/specs/

### Features

- add minor and major bump support to vrg-version
- extract lib/changelog.py and vrg-changelog CLI
- add lib/promote.py and vrg-promote CLI
- add promote and develop CD fields to ReleaseContext
- add --no-promote flag to vrg-release CLI

### Refactoring

- rename vrg-docker-* to vrg-container-* with runtime auto-detection
- simplify preflight: use version.show(), remove inline version detection and override commit
- simplify prepare: use lib/changelog, remove -X ours merge, handle version override on release branch
- rewrite confirm phase: known job expectations, add confirm_develop
- rewrite bump phase: orchestrator-driven back-merge from main
- update finalize summary: back-merge PR label, develop CD
- update orchestrator to new 8-phase sequence with promote and split confirm

## [2.0.45] - 2026-05-24

### Bug fixes

- write onboarding-complete flag for Claude Code interactive TUI

### Chores

- bump version to 2.0.45

## [2.0.44] - 2026-05-24

### Bug fixes

- write .credentials.json for Claude Code interactive TUI auth

### Chores

- bump version to 2.0.44

## [2.0.43] - 2026-05-24

### Bug fixes

- source credential env file in session bash -c wrapper

### Chores

- bump version to 2.0.43

### Documentation

- add release workflow decomposition spec and implementation plan

## [2.0.42] - 2026-05-24

### Chores

- bump version to 2.0.42

### Features

- inject Claude Code OAuth token during credential injection

## [2.0.41] - 2026-05-24

### Bug fixes

- suppress Lima host-CWD warnings in session command

### Chores

- bump version to 2.0.41

## [2.0.40] - 2026-05-24

### Bug fixes

- remove unenforceable core.hooksPath audit check

### Chores

- bump version to 2.0.40

## [2.0.39] - 2026-05-24

### Bug fixes

- replace --workdir with bash -c cd wrapper in session

### Chores

- bump version to 2.0.39

## [2.0.38] - 2026-05-24

### Bug fixes

- restore start_vm in create flow after provisioning

### Chores

- bump version to 2.0.38

## [2.0.37] - 2026-05-24

### Chores

- bump version to 2.0.37

### Features

- resolve VM template tag from vergil-vm config key

## [2.0.36] - 2026-05-24

### Bug fixes

- remove redundant start_vm from create flow

### Chores

- bump version to 2.0.36

## [2.0.35] - 2026-05-24

### Chores

- bump version to 2.0.35

### Features

- derive tooling and template version from identities.toml vergil key

## [2.0.34] - 2026-05-22

### Bug fixes

- route credential mkdir through bash for tilde expansion

### Chores

- bump version to 2.0.34

## [2.0.33] - 2026-05-22

### Bug fixes

- add missing container-tag to audit and test jobs
- make status comment failures non-fatal in orchestrator
- decouple status comment from phase error handling in orchestrator
- raise ReleaseError on comment failure instead of logging warning
- validate template tag to prevent SSRF in fetch_template
- handle first release when no prior tags exist

### Chores

- bump version to 2.0.33

### Documentation

- update Plan 3 for dynamic-only token architecture

### Features

- vrg-vm CLI for identity VM lifecycle management

## [2.0.32] - 2026-05-22

### Bug fixes

- pass app secrets to github-config audit

### Chores

- bump version to 2.0.32

### Documentation

- note personal account installation requirement

### Refactoring

- drop installation_id from identities.toml
- dynamic per-org installation token resolution

## [2.0.31] - 2026-05-21

### Chores

- bump version to 2.0.31

### Refactoring

- make vrg-session command-agnostic

## [2.0.30] - 2026-05-21

### Chores

- bump version to 2.0.30

### Refactoring

- replace workspace registry with path-based resolution

## [2.0.29] - 2026-05-21

### Bug fixes

- use os.execvp instead of subprocess.call
- shell-quote workspace path in bash command strings

### Chores

- bump version to 2.0.29

### Features

- identity config parser for VM session management
- check pre-commit hook content against canonical template
- vrg-session CLI for launching Claude Code in identity VMs

### Refactoring

- simplify vrg-session to use limactl native flags

## [2.0.28] - 2026-05-21

### Bug fixes

- remove repository-type gate from vrg-release preflight
- chdir into cloned repo so git commands in later wizard steps find .git
- route wait_for_checks and watch_workflow through GitHub retry wrapper
- emit run-codeql: false for languages CodeQL does not support
- emit container-suffix and container-tag for audit and test CI jobs
- include Table of Contents section in generated README
- add container-suffix to audit and test workflow calls
- rationalize publish config and confirm-publish phase
- emit release job in render_cd_workflow when publish_release is true
- fix validation issues from identity refactor
- restore sys import dropped during rebase
- skip CI jobs not required by CI gates ruleset
- sync stale VERSION file with pyproject.toml and restore 100% coverage

### Chores

- bump version to 2.0.28
- organize plans into lifecycle subdirectories
- enumerate VM plan numbers (p1-p6) in filenames
- use vrg-github-repo-init for vergil-vm bootstrap
- remove bespoke CI task from vergil-vm plan
- correct vergil-vm plan: docs=yes, dep=v2.0, bootstrap PR workflow

### Documentation

- reconcile VM plans with single-account identity design
- canonical VERSION file design for issue #970
- apply pushback findings to canonical VERSION file design
- implementation plan for canonical VERSION file (#970)

### Features

- add structured phase logging, --verbose flag, and subprocess output capture
- GitHub App installation token exchange
- HTTPS token injection for remote git operations
- add unrecognized-key warnings to vergil.toml parser
- show reads from canonical VERSION file with language cross-check
- bump writes both VERSION and language-specific file
- add version prompt and VERSION file to init wizard

### Refactoring

- extract shared retry module and wire into vrg-gh
- remove credential selection from vrg-gh
- replace account-based co-author with VRG_CO_AUTHOR env var
- delete multi-account credential functions

### Styling

- apply ruff formatting to version module

## [2.0.27] - 2026-05-20

### Bug fixes

- apply alignment review fixes to vrg-release plan
- lint fixes and 100% branch coverage for release modules

### Chores

- bump version to 2.0.27

### Documentation

- agent identity design: GitHub Apps + VM isolation
- update VM plans and specs for GitHub App credential model
- implementation plan for vergil-tooling identity changes
- vrg-release mechanized release workflow design
- add duplicate tracking issue guard to vrg-release spec
- clarify resume semantics require explicit --resume flag
- apply pushback review fixes to vrg-release spec
- implementation plan for vrg-release mechanized workflow
- move vrg-release plan to docs/plans/
- alignment review report for vrg-release spec and plan
- update CLAUDE.md for vrg-release and retired tools

### Features

- add consumer_refresh and docs_workflow to PublishConfig
- add ReleaseContext dataclass and ReleaseError exception
- add tracking module for release issue management
- add preflight module with version detection and checks
- add prepare module for branch creation and changelog
- add merge module for wait-poll-merge logic
- add bump module for version-bump PR handling
- add confirm module for workflow watching and artifact verification
- add finalize module for issue close and repo cleanup
- add handoff module for consumer-refresh display
- add orchestrator for sequential phase execution
- add vrg-release CLI entry point, retire absorbed tools

### Refactoring

- migrate lib/release.py to lib/release/ package

## [2.0.26] - 2026-05-20

### Bug fixes

- remove stale audit-logging reference from CLAUDE.md consumer template

### Chores

- bump version to 2.0.26

### Features

- confirm clone location before cloning in repo init wizard

## [2.0.25] - 2026-05-20

### Chores

- bump version to 2.0.25

### Features

- add label to version selector in site header

## [2.0.24] - 2026-05-20

### Bug fixes

- correct vergil.toml example in VM repository plan

### Chores

- bump version to 2.0.24

### Documentation

- identity-based VM isolation design spec
- fix GitHub account references to use wphillipmoore
- expand credential provisioning to extensible identity model
- VM image management design spec (vergil-vm)
- refine decision boundary and shell customization policy
- pre-built distribution and dynamic tooling management
- implementation plan for vergil-vm repository
- expand prerequisites with manual repo bootstrap steps
- set initial version to 2.1.0
- annotate wrapper restrictions and CLAUDE.md audit compliance
- design spec for eliminating hardcoded mount in vrg-docker-docs
- move design spec to docs/specs/
- implementation plan for build_docker_args refactor
- vrg-github-repo-init design spec
- incorporate pushback review findings into design spec
- vrg-github-repo-init implementation plan
- implementation plans 2-6 for vergil-vm identity VM system
- defer Plan 4 (egress filtering) to v2.2
- multi-platform host support design spec
- clarify documentation target files in platform support spec
- move platform support spec to docs/specs/
- apply pushback review: reframe #902 as dependency, phase documentation, clarify testing

### Features

- replace hand-built docker args with build_docker_args in vrg-docker-docs
- add template data files for repo init
- add prompt helpers and RepoInitContext dataclass
- add checkpoint detection for idempotent resume
- add template rendering functions for all generated files
- add wizard steps 1-2: repo creation and clone
- add wizard step 3: interactive vergil.toml generation
- add wizard step 4: scaffold config files
- add wizard steps 5-6: CI/CD workflows and docs site
- add wizard steps 7-9: branches, GitHub config, Pages
- add wizard orchestrator with idempotent step skipping
- add vrg-github-repo-init CLI entry point

### Refactoring

- remove flat-file audit logging from vrg-git and vrg-gh

### Styling

- apply ruff formatting

### Testing

- add tests for build_docker_args delegation in vrg-docker-docs
- update existing tests to mock build_docker_args, remove sibling mount test
- add coverage tests for repo init wizard and CLI entry point

## [2.0.23] - 2026-05-20

### Chores

- bump version to 2.0.23
- replace stale st- prefix references with vrg- in site docs

### Features

- escalate issue close to human credentials
- replace vergil.toml image-prefix with --prefix CLI flag
- add --prefix CLI flag to vrg-docker-docs and vrg-scorecard

## [2.0.22] - 2026-05-19

### Chores

- bump version to 2.0.22

### Documentation

- add design spec for conditional policy relaxation (#827, #845)
- move design spec to docs/specs/
- add implementation plan for conditional policy relaxation (#827, #845)
- update denied-flags table for conditional push and branch policies (#827, #845)

### Features

- add _is_protected_branch helper (#827)
- add _is_upstream_gone helper (#845)
- allow --force-with-lease on non-protected branches (#827)
- allow branch -D when upstream is gone (#845)

### Styling

- fix lint and formatting issues

### Testing

- cover empty-line branch in _is_upstream_gone

## [2.0.21] - 2026-05-19

### Bug fixes

- support all merge strategies and add --pr flag
- exit non-zero when PR is blocked by branch protection (#806)

### Documentation

- add design and plan for wait-until-green merge-state awareness (#806)

### Features

- add merge_status() helper for combined merge state and review decision query

### Styling

- replace assert with isinstance guard for ruff S101 compliance

## [2.0.20] - 2026-05-19

### Chores

- bump version to 2.0.20

### Features

- add exact-match allowlist to vrg-git for specific denied-subcommand overrides

## [2.0.19] - 2026-05-19

### Chores

- bump version to 2.0.19

### Documentation

- add documentation, fix type safety, and reach 100% coverage

### Features

- add shared issue-linkage regex module
- add CLI tool to extract tracking issue from merge commit
- register console script and add integration test

### Refactoring

- use shared linkage module

## [2.0.18] - 2026-05-19

### Bug fixes

- skip local checks when --repo targets a different repository

### Chores

- bump version to 2.0.18

### Documentation

- add tooling gap analysis and expansion plan
- add design for core.hooksPath audit check (#825)
- move hooks-path audit spec to docs/specs/
- add implementation plan for core.hooksPath audit check (#825)

### Features

- add core.hooksPath audit check (#825)

### Styling

- add noqa annotations for subprocess security lints

### Testing

- add test for wrong core.hooksPath value
- add test for correctly configured core.hooksPath
- update integration tests for core.hooksPath check

## [2.0.17] - 2026-05-19

### Chores

- bump version to 2.0.17

### Features

- add vrg-scorecard with help output
- register vrg-scorecard console script entry point

### Testing

- add token injection and docker exec tests
- add image prefix resolution tests
- verify token failure propagation

## [2.0.16] - 2026-05-18

### Bug fixes

- hardcode co-author noreply ID while agent account is shadow-banned

### Chores

- bump version to 2.0.16
- remove co-authors section, normalize vergil dep to v2.0

## [2.0.15] - 2026-05-18

### Chores

- bump version to 2.0.15

## [2.0.14] - 2026-05-18

### Bug fixes

- fix validation: type errors, stale co-author refs, formatting, and coverage gaps
- make desired_security_settings visibility-aware
- record skipped fields in ConfigDiff during diff
- omit None security fields from apply PATCH body
- render skipped fields in CLI audit/diff output
- make skipped param required in diff helpers for full coverage
- only print GHAS skip message for security fields

### Chores

- bump version to 2.0.14

### Documentation

- add Vergil identity account setup guide
- publish identity, credential, and permission architecture to site docs
- clarify credential store setup as sequential browser-authenticated steps
- rewrite defense-in-depth to distinguish client-side constraints from server-side security
- apply pushback review to repo config audit design
- add implementation plan for repo config audit
- rewrite repo config audit plan in TDD red/green/refactor format
- replace stale st-* references with vrg-* across docs and source
- add auto-discovery design spec
- apply pushback review to auto-discovery design spec
- add implementation plan for co-author auto-discovery
- add cross-repo rollout task to implementation plan
- add implementation plan for private repo visibility gating (#826)

### Features

- add local config audit library, shared CLAUDE.md template, and vrg-github-repo-config CLI
- add resolve_co_author_trailer for dynamic co-author discovery
- replace config-based co-author lookup with dynamic API resolution

### Refactoring

- replace local _discover_accounts with import from lib/github
- remove co-author parsing from config.py

## [2.0.13] - 2026-05-15

### Bug fixes

- add explicit credential selection via GH_TOKEN injection

### Chores

- bump version to 2.0.13

### Refactoring

- replace -agent convention with -vergil across tooling, specs, and config

## [2.0.12] - 2026-05-15

### Bug fixes

- use human credentials for all operations while agent account is flagged (#799)

### Chores

- bump version to 2.0.12

## [2.0.11] - 2026-05-15

### Bug fixes

- deduplicate accounts in gh auth status discovery

### Chores

- bump version to 2.0.11

### Features

- move deny rules to project-level settings and deploy to vergil-tooling

## [2.0.10] - 2026-05-14

### Chores

- bump version to 2.0.10
- remove per-repo templates in favor of org defaults

### Documentation

- add credential management design spec
- apply pushback review fixes to credential management design spec
- add credential management implementation plan
- apply alignment review fixes to credential management design spec and plan
- add execution order cross-references between permission model and credential management plans
- add supersession notice to org governance credential section
- add supersession notices to org governance setup plan
- add credential selection cross-references to permission model
- update consuming repo setup and CLAUDE.md for credential management model

### Features

- add vrg-git safe wrapper with subcommand allowlist and audit logging
- add vrg-gh safe wrapper with credential selection and audit logging
- deploy permission model with fully qualified path deny patterns

### Refactoring

- remove GH_TOKEN hard gate from vrg-docker-run

## [2.0.9] - 2026-05-14

### Chores

- bump version to 2.0.9

## [2.0.8] - 2026-05-14

### Bug fixes

- replace hardcoded wphillipmoore/* with vergil-project/* in action patterns

### Chores

- bump version to 2.0.8

### Documentation

- add Claude Code permission model design spec (#754)
- apply pushback review fixes to permission model design spec
- add permission model implementation plan (#754)
- apply alignment review fixes to permission model implementation plan

## [2.0.7] - 2026-05-14

### Chores

- bump version to 2.0.7

### Documentation

- add .github profile repository design spec (#753)
- apply pushback review fixes to .github profile repo design spec
- add .github profile repository implementation plan (#753)
- apply alignment review fixes to .github profile repo spec and plan

### Refactoring

- consolidate [dependencies] to single 'vergil' key

## [2.0.6] - 2026-05-13

### Chores

- bump version to 2.0.6

### Documentation

- replace stale standard-tooling and wphillipmoore references
- replace standard-tooling references with vergil-tooling in site docs

## [2.0.5] - 2026-05-13

### Bug fixes

- omit allow_forking for public org repos (API rejects it with HTTP 422)

### Chores

- bump version to 2.0.5

## [2.0.4] - 2026-05-13

### Bug fixes

- update hardcoded org URL from wphillipmoore to vergil-project
- surface captured API output in CalledProcessError messages

## [2.0.2] - 2026-05-13

### Bug fixes

- rename stale st_repo_profile import to vrg_repo_profile

### CI

- update vergil-actions refs from v1.5 to v2.0

### Chores

- bump version to 1.4.37
- update plugin identity to vergil-marketplace

### Documentation

- add VERGIL rename design spec
- add VERGIL rename implementation plan
- move rename plan to docs/plans/
- apply alignment review fixes to VERGIL rename plan
- add VERGIL org governance design spec
- apply pushback review fixes to org governance design
- add tooling impact section to org governance design spec
- add VERGIL org governance setup implementation plan
- add Task 14 to create deferred work issues in new org
- fix agent PAT sequencing in org governance plan

### Features

- rename to vergil-tooling under vergil-project org (#723)

### Refactoring

- remove skip-rulesets escape hatch
- consolidate co-author config to single agent identity
- update st-commit and tests for agent identity convention

## [1.4.36] - 2026-05-11

### Bug fixes

- fix lint, type errors, and coverage gaps from prefix refactor

### Chores

- bump version to 1.4.36

### Documentation

- migrate standards and development docs from standards-and-conventions
- migrate AI collaboration standards from standards-and-conventions
- add design spec for PR and issue template redesign
- move design spec to docs/specs/
- add implementation plan for template redesign
- fix stale documentation

### Features

- add [docker] image-prefix field to standard-tooling.toml schema
- make image prefix configurable in docker.py, default to prod
- read image prefix from config in st-docker-run and st-docker-docs
- default --repo to current git remote, drop --yes flag

### Refactoring

- remove Testing section from PR body
- replace PR template with redirect stub
- redesign issue template as 3-field form

### Styling

- fix ruff format in test file

## [1.4.35] - 2026-05-10

### Chores

- bump version to 1.4.35

### Documentation

- add spec and plan for post-publish tag verification (#664)

## [1.4.34] - 2026-05-09

### Bug fixes

- skip worktrees with uncommitted changes instead of crashing

### Chores

- bump version to 1.4.34

### Styling

- format test file

## [1.4.33] - 2026-05-09

### Bug fixes

- prevent allow_forking HTTP 422 on user-owned repos and require [ci] section

### Chores

- bump version to 1.4.33

### Features

- update workflow refs to new CI/CD filenames (#383)
- add ops workflow for scheduled GitHub config audit (#174)

## [1.4.32] - 2026-05-09

### Bug fixes

- specify container-tag in publish-release workflow
- use current standard-actions workflow filenames until rename releases (#383)
- keep release job key until check name registry updates (#383)
- restore string comparison for workflow_call boolean inputs (#383)
- add missing container-tag to release job in cd.yml (#657)

### Chores

- bump version to 1.4.31

### Features

- rename release/version-bump check to version/version-bump (#383)
- rename publish workflows to cd convention, reformat ci.yml (#383)
- include ci.yml reformat in workflow rename (#383)

## [1.4.30] - 2026-05-09

### Bug fixes

- pass boolean to ci-security reusable workflow inputs
- address formatting issue in fetch_actual_state
- pass required language input to publish-release reusable workflow (#645)
- replace Fixes with Ref in PR template to match CI linkage rules
- specify container-tag in publish-release caller

### Chores

- bump version to 1.4.28
- migrate to reusable publish/docs workflows
- trigger CI re-run
- rename source files to match st-* script names

### Features

- add 8 new fields to DesiredRepoSettings and derivation
- add FetchResult wrapper and extract new fields in fetch
- include new fields in repo settings PATCH body
- thread visibility from fetch through CLI plumbing
- make allowed action patterns language-specific (#613)
- detect and fail fast on merge conflicts in PR-waiting scripts (#641)
- auto-update branch when behind base before merging (#641)

### Testing

- update lib tests for new fields and FetchResult

## [1.4.27] - 2026-05-08

### Chores

- bump version to 1.4.27
- sweep post-1.4.26 dependency updates (#621)
- replace st-validate-local references with st-validate in active docs and specs
- add publish.release and publish.docs to standard-tooling.toml

### Documentation

- add design spec and pushback review for repo settings coverage (#610)
- add implementation plan and alignment review for repo settings coverage (#610)

### Features

- add [publish] section to standard-tooling.toml schema
- add [publish] section to desired state for naming validation

## [1.4.26] - 2026-05-08

### Bug fixes

- remove unreachable early return to restore 100% coverage

### Chores

- prepare release 1.4.25
- bump version to 1.4.26
- add #603 design spec and implementation plan

### Documentation

- add design spec and implementation plan for Go license allowlist (#604)
- add design spec and implementation plan for Java license audit (#600)

### Features

- add swatinem/* to allowed action patterns
- add Go license allowlist to centralized audit (#604)
- add license_finder decisions file for Ruby audit
- add {configs} placeholder expansion to language_commands()
- add license_finder to Ruby audit registry
- add Java license allowlist to centralized audit (#600)

### Styling

- format validate_commands.py

### Testing

- add failing test for Go license allowlist (#604)
- strengthen Go audit test to verify allowlist flag (#604)

## [1.4.25] - 2026-05-08

### Bug fixes

- tighten cliff regexes, fix doc to docs, add build and revert types

### Chores

- prepare release 1.4.24
- bump version to 1.4.25

### Features

- bundle canonical yamllint config like markdownlint
- reject GitHub auto-close keywords in commit bodies and PR bodies
- centralize git-cliff configs as bundled package data

## [1.4.24] - 2026-05-07

### Bug fixes

- pass --platform to docker run and docker create for correct arch selection

### Chores

- prepare release 1.4.23
- bump version to 1.4.24

### Features

- add retry with exponential backoff to all GitHub API calls
- detect branch-behind state and auto-update before reporting success

## [1.4.23] - 2026-05-07

### Bug fixes

- remove stale validate-local references from mkdocs nav and reference index

### Chores

- prepare release 1.4.22
- bump version to 1.4.23

## [1.4.22] - 2026-05-06

### Chores

- prepare release 1.4.21
- bump version to 1.4.22
- update dev dependencies for 1.4.22 cycle

### Documentation

- remove legacy st-validate-local reference page
- update cli-tools-overview for st-validate-local removal
- update CLAUDE.md to reference st-validate instead of st-validate-local
- update README.md to reference st-validate

### Refactoring

- rename validate_local_common_container to validate_common
- update imports and patch targets for validate_common rename
- remove legacy validate_local and validate_local_lang modules
- remove legacy scripts/dev/ shell scripts
- remove legacy st-validate-local console_script entries
- rename custom validator lookup from validate-local-custom to validate-custom
- update usage example to reference st-validate

## [1.4.21] - 2026-05-06

### Bug fixes

- use str() in _check_names to satisfy ty type checker
- restore Python 3.12+ support and auto-prepend .venv/bin in st-validate

### Chores

- prepare release 1.4.20
- bump version to 1.4.21

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

- prepare release 1.4.19
- bump version to 1.4.20
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

### Chores

- prepare release 1.4.18
- bump version to 1.4.19

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

- prepare release 1.4.17
- bump version to 1.4.18
- sweep post-1.4.17 dependency updates

## [1.4.17] - 2026-05-05

### Bug fixes

- add safe.directory for git worktree tests in CI container

### Chores

- prepare release 1.4.16
- bump version to 1.4.17
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

- prepare release 1.4.15
- bump version to 1.4.16
- sweep post-1.4.15 dependency updates

### Styling

- apply ruff format

## [1.4.15] - 2026-05-05

### Bug fixes

- add explicit type annotations to read_json() for mypy
- include enabled field in actions permissions PUT body

### Chores

- prepare release 1.4.14
- bump version to 1.4.15
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

- prepare release 1.4.13
- bump version to 1.4.14
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

- prepare release 1.4.12
- bump version to 1.4.13
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

### Chores

- prepare release 1.4.11
- bump version to 1.4.12

### Features

- add _checks_registered probe and polling loop to wait_for_checks

### Styling

- apply ruff format to test_github.py
- fix line length in test_main_dry_run_release_branch
- apply ruff format to test_submit_pr.py

### Testing

- cover wait_for_checks polling behavior

## [1.4.11] - 2026-05-01

### Chores

- prepare release 1.4.10
- bump version to 1.4.11

### Features

- add container guard to st-validate-local

## [1.4.10] - 2026-05-01

### Bug fixes

- Fix --pull=always breaking cached image lookup; route Python through cache
- Fix ruff format violations

### Chores

- prepare release 1.4.9
- bump version to 1.4.10

## [1.4.9] - 2026-05-01

### Chores

- prepare release 1.4.8
- bump version to 1.4.9
- organize plans into proposed/in-progress/completed lifecycle

## [1.4.8] - 2026-05-01

### Bug fixes

- replace pip install with uv tool install in docker cache build

### Chores

- prepare release 1.4.7
- bump version to 1.4.8
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

- prepare release 1.4.6
- bump version to 1.4.7
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

- prepare release 1.4.5
- bump version to 1.4.6
- change license from GPL-3.0-only to GPL-3.0-or-later

## [1.4.5] - 2026-04-29

### Bug fixes

- eliminate unreachable elif branch for full coverage

### Chores

- prepare release 1.4.4
- bump version to 1.4.5
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

### Chores

- prepare release 1.4.3
- bump version to 1.4.4

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

- prepare release 1.4.1
- merge main into release/1.4.2
- prepare release 1.4.2
- bump version to 1.4.3
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

### Chores

- bump version to 1.4.2 (#361)

### Documentation

- fix spec-plan alignment issues from pushback review (#366)

### Features

- decouple standard-tooling from dev container images (#362) (#364)

## [1.4.1] - 2026-04-28

### Chores

- prepare release 1.4.0
- bump version to 1.4.1
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

- prepare release 1.3.4
- bump version to 1.3.5
- delete ci-push.yml; collapse three-tier CI to two-tier
- migrate standard-actions refs from @develop to @v1.3
- remove add-to-project.yml workflow
- remove st-list-project-repos and st-set-project-field
- remove st-observatory and dead-code registry module
- bump version to 1.4.0
- regenerate lockfile for 1.4.0

### Features

- add post-publish workflow to verify dev container images carry the released version

## [1.3.4] - 2026-04-27

### Bug fixes

- declare GPL-3.0-only license metadata in pyproject.toml

### Chores

- prepare release 1.3.3
- bump version to 1.3.4

## [1.3.3] - 2026-04-27

### Bug fixes

- auto-remove worktree before deleting merged branch (#315)

### Chores

- prepare release 1.3.2
- bump version to 1.3.3

## [1.3.2] - 2026-04-26

### Bug fixes

- regenerate v1.3.1 notes with --unreleased

### Chores

- prepare release 1.3.1
- bump version to 1.3.2

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

### Chores

- prepare release 1.3.0
- bump version to 1.3.1

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

- prepare release 1.2.2
- bump version to 1.2.3
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

- prepare release 1.2.1
- bump version to 1.2.2
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

- prepare release 1.2.0
- merge main into release/1.2.0
- prepare release 1.2.0
- bump version to 1.2.1
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

### Chores

- prepare release 1.1.4
- bump version to 1.1.5

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

### Chores

- merge main into release/1.1.0
- prepare release 1.1.0
- merge main into release/1.1.1
- prepare release 1.1.1
- merge main into release/1.1.2
- prepare release 1.1.2
- merge main into release/1.1.3
- prepare release 1.1.3
- bump version to 1.1.4

### Features

- annotate synced scripts with provenance comments (#58)

## [1.1.3] - 2026-02-21

### Bug fixes

- strip ^{} suffix from dereferenced tags in sync-tooling.sh (#51)

### Chores

- bump version to 1.1.3 (#48)

## [1.1.2] - 2026-02-21

### Bug fixes

- handle empty docsite_files array with set -u
- prevent --actions-compat from leaking during self-update re-exec
- accept cross-repo issue references in PR linkage check (#36)

### Chores

- add commit.sh and submit-pr.sh to managed files list (#18)
- bump version to 1.1.1 (#37)
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
