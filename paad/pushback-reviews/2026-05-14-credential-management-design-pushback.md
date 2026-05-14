# Pushback Review: Credential Management Design

**Date:** 2026-05-14
**Spec:** docs/specs/2026-05-14-credential-management-design.md
**Commit:** b2b743f

## Source Control Conflicts

None — no conflicts with recent changes. The spec correctly identifies
the current state of `github.py` (no credential management, inherits
ambient `GH_TOKEN`), `vrg-docker-run` (hard-requires `GH_TOKEN` at
line 80), and the permission model spec (which scopes out credentials).
Both accounts are already logged into `gh auth` — `gh auth token -u`
works for both, validating the retrieval mechanism.

## Issues Reviewed

### [1] `vrg-gh` gets two jobs that may conflict
- **Category:** Scope imbalance / Feasibility
- **Severity:** Serious
- **Issue:** The permission model spec (#754) defines `vrg-gh` as a
  subcommand allowlist with flag validation. This credential spec adds
  credential selection as a second responsibility. The two specs
  contradict: #754 denies `pr merge` unconditionally, while this spec
  allows it under human credentials for release workflows.
- **Resolution:** Reconcile the two specs. The credential management
  spec supersedes the permission model's blanket denial of `pr merge`
  and `pr review --approve` — these move from denied to conditionally
  allowed with context validation and credential escalation. The
  escalation logic is factored out as a testable component within
  `vrg-gh`. The permission model plan (Task 2) must be updated.
  Spec updated to document the interaction explicitly.

### [2] Agent PAT scopes don't match reality
- **Category:** Contradiction
- **Severity:** Moderate
- **Issue:** The spec says agent PAT should have `repo` and `read:org`
  only, but the actual token includes `gist` and `workflow`. The
  `workflow` scope is needed to push commits that modify workflow files.
- **Resolution:** Dropped — classic PATs use checkbox scopes, and the
  spec's own security argument (Section 2) already positions token scope
  as the fourth defense layer, not the first. The scope list is
  aspirational guidance, not a hard security boundary.

### [3] `gh auth login --with-token -u` doesn't exist
- **Category:** Feasibility
- **Severity:** Serious
- **Issue:** The setup commands in Section 3 and Section 8 use
  `gh auth login --with-token -u <username>-agent`, but `--with-token`
  doesn't accept `-u`. The `gh` CLI determines the account from the
  token by calling the GitHub API.
- **Resolution:** Fixed. Setup commands updated to remove `-u` flag.
  Added verification steps. Section 8 onboarding updated to match.

### [4] Account discovery is underspecified
- **Category:** Ambiguity
- **Severity:** Moderate
- **Issue:** Section 4 says discover the human account from
  `gh auth status` "or" a config value in `vergil.toml`. Which source
  wins is undefined. Additionally, if the active account is the agent,
  the derivation logic would compute `<name>-agent-agent`.
- **Resolution:** Fixed. Discovery enumerates all logged-in accounts
  and picks the one that doesn't end in `-agent`. No `vergil.toml`
  dependency — the hardcoded username in `vergil.toml` is a known bug
  to be cleaned up separately.

### [5] `vrg-docker-run` credential retrieval adds unnecessary complexity
- **Category:** Feasibility
- **Severity:** Moderate
- **Issue:** Section 6 proposed "retrieve the token when needed" logic
  in `vrg-docker-run`, requiring the tool to predict whether the inner
  command needs GitHub access. Investigation confirmed that no container
  commands (`vrg-validate`, tests, linters) use `GH_TOKEN` — GitHub
  operations are host-side by design.
- **Resolution:** Fixed. Section 6 simplified to: remove the hard gate,
  no credential retrieval logic. The env-var passthrough in `docker.py`
  is left as-is; cleanup of hardcoded prefixes tracked in #777.

### [6] Token expiration and rotation are unaddressed
- **Category:** Omission
- **Severity:** Moderate
- **Issue:** The governance spec (#717) had detailed credential
  lifecycle procedures. This spec retires `vrg-credential-audit` and
  replaces it with nothing. Expired tokens in `gh auth` would produce
  opaque 401 errors.
- **Resolution:** Added Section 8 (Credential Lifecycle — Deferred)
  documenting what is deferred and requiring the implementation plan
  to create follow-on issues for each item. Updated
  `vrg-credential-audit` status from "Descope" to "Deferred."

### [7] "What Gets Updated" table is incomplete
- **Category:** Omission
- **Severity:** Minor
- **Issue:** Missing entries for vergil-tooling CLAUDE.md,
  `vrg_docker_run.py` usage text, and consuming repo CLAUDE.md files.
- **Resolution:** Fixed. Three entries added to the table.

### [8] Section 5 understates the refactoring scope
- **Category:** Ambiguity
- **Severity:** Moderate
- **Issue:** Section 5 claimed credential selection would be
  "transparent" through `github.py`, but `github.py` functions
  need different credentials depending on caller context (e.g.,
  `create_pr` uses App token during release but agent token normally).
  The calling code is the only place that knows the context.
- **Resolution:** Fixed. Section 5 rewritten: `github.py` stays
  unaware of credentials (inherits from process environment).
  Mechanized tools set `GH_TOKEN` in their process environment
  before calling `github.py`. The pseudocode in Section 4 updated
  to remove the "shared between CLI and library" claim.

## Summary

- **Issues found:** 8
- **Issues resolved:** 8
- **Unresolved:** 0
- **Spec status:** Ready for implementation planning
