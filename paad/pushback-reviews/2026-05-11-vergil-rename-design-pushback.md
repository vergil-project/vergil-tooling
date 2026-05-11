# Pushback Review: VERGIL Rename Design

**Date:** 2026-05-11
**Spec:** `docs/specs/2026-05-11-vergil-rename-design.md`
**Commit:** 169e3729d392215f409aea2cacdbf8ebfa4c94fe

## Source Control Conflicts

One conflict: PR #708 (`feature/706-docker-image-prefix`, merged within
the last 2 weeks) added a `[docker] image-prefix` field to
`standard-tooling.toml` and hardcoded `ghcr.io/wphillipmoore` in
`src/standard_tooling/lib/docker.py`. The spec's config file section
did not account for the `[docker]` section or the hardcoded GHCR prefix.
Addressed in issue [1] below.

## Issues Reviewed

### [1] GHCR container registry migration missing entirely

- **Category:** Omission
- **Severity:** Critical
- **Issue:** The spec covered GitHub repo renames, Python package
  renaming, CLI prefix changes, env vars, and config files — but never
  mentioned container images. `docker.py` hardcodes
  `_GHCR = "ghcr.io/wphillipmoore"`, used by `st-docker-run` and
  `st-docker-docs`. GHCR packages are org-scoped and don't move with
  repo transfers. Without re-publishing images to
  `ghcr.io/vergil-project/*`, the entire validation pipeline breaks
  fleet-wide.
- **Resolution:** Add a Container Registry section covering
  re-publishing images, updating the `_GHCR` constant, and maintaining
  dual-availability during the consumer sweep window.

### [2] Plugin namespace flagged as risk with no mitigation

- **Category:** Ambiguity
- **Severity:** Serious
- **Issue:** The Risks section identified that `standard-tooling:*`
  skill names would break but provided no concrete plan for how the
  namespace is configured or what needs to change.
- **Resolution:** Accepted as-is. No external users exist — hard
  cutover is fine. Plugin skill breakage during the rename window is
  an acceptable cost handled ad hoc by the engineer and AI agents.

### [3] Config file scope understates what changes

- **Category:** Omission
- **Severity:** Serious
- **Issue:** The config section only described the filename rename and
  `[dependencies]` key change. The actual config file also contains
  `[publish] consumer-refresh` (with hardcoded GitHub URL and package
  name), `[docker]` (from recently merged #706), and
  `[project.co-authors]` (with `wphillipmoore-*` bot accounts).
  Executors following the spec literally would miss these fields.
- **Resolution:** Expand into a full field inventory table listing
  every section/field containing an old name.

### [4] Consumer repo inventory is vague

- **Category:** Ambiguity
- **Severity:** Moderate
- **Issue:** "~10-12 consumer repos" is too vague for an execution
  plan requiring every repo to complete a successful release. Without
  an exact manifest, a forgotten repo keeps referencing old names
  indefinitely.
- **Resolution:** Add Appendix A listing all consumer repos by name
  with notes, generated from `gh repo list` and verified before
  execution day.

### [5] Execution window is ambitious

- **Category:** Feasibility
- **Severity:** Moderate
- **Issue:** 4 sequential core repo releases + 10-12 consumer repo
  releases in "one morning/day" is aggressive. Core repos are
  sequential (dependency chain), each blocking on CI (~5-8 min).
  If the morning target slips, the fleet stays frozen in a
  partially-migrated state.
- **Resolution:** Add a Phase 1 / Phase 2 checkpoint. Phase 1 (core
  repos) must complete before breaking. Phase 2 (consumer sweep) can
  resume in a separate window. Images remain available at both GHCR
  paths during the gap.

### [6] v2.0 semver commitment — rename-only scope

- **Category:** Ambiguity
- **Severity:** Minor
- **Issue:** The spec bumps to v2.0.0 but doesn't state whether the
  rename is the only breaking change, leaving the door open for
  unrelated breaks to creep in during execution.
- **Resolution:** Add a one-liner scoping v2.0 to the rename.
  Execution-time fixes may introduce additional breaks as needed,
  but no unrelated breaking changes are bundled intentionally.

## Unresolved Issues

None — all issues were addressed.

## Summary

- **Issues found:** 6
- **Issues resolved:** 6
- **Unresolved:** 0
- **Spec status:** Updated and ready for implementation planning
