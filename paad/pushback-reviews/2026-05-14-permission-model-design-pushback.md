# Pushback Review: Permission Model Design

**Date:** 2026-05-14
**Spec:** docs/specs/2026-05-14-permission-model-design.md
**Commit:** 5cf710ee405fe36cfaeaaf761fa17627a7f11e4b

## Source Control Conflicts

None — no conflicts with recent changes. Recent commits (version
bumps, VERGIL rename cleanup, config consolidation, .github profile
spec) do not contradict the spec's assumptions. All referenced tools,
hooks, and architecture match the current codebase.

## Issues Reviewed

### [1] Read-only bash exceptions are not constrained against command chaining

- **Category:** Security
- **Severity:** Serious
- **Issue:** Phase 1 read-only exceptions (`grep`, `cat`, `find`,
  etc.) go through raw Bash, not through a wrapper. Claude Code's
  documented behavior splits compound commands on shell operators and
  evaluates each subcommand independently — deny rules on `git`/`gh`
  should fire against embedded dangerous commands. However, there is a
  known implementation gap (anthropics/claude-code#28784) where prefix
  matching on the full command string can produce unexpected approvals.
  The spec's risk mitigation depends on matching behavior that is not
  reliably enforced.
- **Resolution:** Document both the documented matching behavior and
  the implementation gap in the spec's Risks table. Note that deny
  rules on `git`/`gh` provide the primary defense. Add a fallback
  option: a PreToolUse hook that rejects shell operators in
  read-only-allowlisted commands, deployable if the implementation gap
  is not resolved before phase 1. Applied to spec.

### [2] `git commit` not explicitly listed in denied subcommands

- **Category:** Ambiguity
- **Severity:** Moderate
- **Issue:** Section 2's "Denied Subcommands" list omitted `commit`.
  It was implicitly denied by the allowlist approach, but given that
  controlling `git commit` is the single most important operation the
  permission model exists to enforce, the omission was conspicuous.
- **Resolution:** Added `commit` to the denied subcommands list with
  a note explaining that all commits flow through `vrg-commit`. Applied
  to spec.

### [3] `git stash drop` and `stash clear` can destroy work

- **Category:** Omissions
- **Severity:** Moderate
- **Issue:** `stash` is allowed with no flag restrictions, but `drop`
  and `clear` permanently delete stashed work.
- **Resolution:** Leave unrestricted. Stash is an out-of-band recovery
  mechanism, not a normal workflow operation. The real fix is the
  tooling that prevents the mistakes leading to stash usage. In the
  recovery context where an agent has stashed something and determined
  it is no longer needed, `drop`/`clear` are legitimate. Added
  documentation of this reasoning to the stash row in the spec.
  Applied to spec.

### [4] `git checkout -- <specific-file>` silently discards changes

- **Category:** Security
- **Severity:** Moderate
- **Issue:** The spec denies broad restore patterns (`-- .`, `-- *`)
  but allows specific-file restore (`-- path/to/file`), which
  irreversibly discards uncommitted changes to the named file.
- **Resolution:** Leave as-is. The agent already has equivalent
  destructive capability through the Write tool (`acceptEdits` mode),
  and worktree isolation is the real safety net. Added documentation
  of this reasoning to the checkout row in the spec. Applied to spec.

### [5] Consuming repos need `.claude/settings.json` permission updates

- **Category:** Omissions
- **Severity:** Moderate
- **Issue:** The migration rollout covers CLAUDE.md updates and
  vergil-tooling's own permission config, but does not include a step
  for deploying `.claude/settings.json` to consuming repos. Without
  it, agents in consuming repos would be prompted for every `vrg-*`
  command.
- **Resolution:** Added migration step 4: deploy
  `.claude/settings.json` to each consuming repo with the `Bash(vrg-*)`
  allowlist and provide a template `settings.local.json` for phase 1
  read-only exceptions. Applied to spec.

### [6] Redundant permission patterns in project settings

- **Category:** Ambiguity
- **Severity:** Minor
- **Issue:** Project settings listed `Bash(vrg-*)`, `Bash(vrg-git *)`,
  and `Bash(vrg-gh *)`. The latter two are subsets of the wildcard and
  can never match anything it does not already cover.
- **Resolution:** Removed redundant entries. Single `Bash(vrg-*)`
  wildcard covers all current and future VRG tools. Applied to spec.

### [7] Hook count mismatch — `remind-finalize` missing

- **Category:** Contradictions
- **Severity:** Minor
- **Issue:** The spec said "11 hooks" but the actual `hooks.json`
  has 12. The PostToolUse hook `remind-finalize` was missing from both
  the count and the Hook Evolution table.
- **Resolution:** Updated count to 12 and added `remind-finalize` to
  the evolution table as "Still primary — operational reminder."
  Applied to spec.

## Unresolved Issues

None — all issues addressed.

## Summary

- **Issues found:** 7
- **Issues resolved:** 7
- **Unresolved:** 0
- **Spec status:** Ready for implementation
