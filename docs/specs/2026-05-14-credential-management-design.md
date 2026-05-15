# Credential Management Design

**Issue:** #775
**Date:** 2026-05-14
**Status:** Draft
**Supersedes:** Org governance design (#717) Section 3 (Credential
Management)

## Problem

The VERGIL project spans multiple GitHub orgs (`vergil-project`,
Diogenes, mq-rest-admin, and potentially more). Each org is managed
independently. The governance design (#717) specified fine-grained
PATs stored in macOS Keychain with custom retrieval tooling, but this
approach has proven unworkable:

1. **Fine-grained PATs are scoped to a single resource owner.** A
   token for `vergil-project` cannot access `diogenes-project` repos.
   Multi-org requires N×2 tokens (human + agent for each org).
2. **`gh auth` stores one token per account per host.** Multiple
   fine-grained PATs for the same account cannot coexist in `gh auth`,
   pushing credential management into a custom keychain system.
3. **Fine-grained PATs cannot be created by outside collaborators
   scoped to an org** (#761). The agent account is an outside
   collaborator by design, making fine-grained PATs impractical.
4. **Custom keychain management duplicates what `gh auth` already
   does.** Building `vrg-setup-credentials`, `vrg-credential-audit`,
   and per-platform secure store backends is significant work that
   reimplements existing infrastructure.

This spec replaces the credential management section of the org
governance design with an approach built on `gh auth` and classic
PATs, with credential selection enforced by the `vrg-gh` wrapper.

## Foundational Principles

1. **The tool selects the credential, not the caller.** No flags, no
   env vars, no mode switches. The wrapper determines the appropriate
   identity based on the command being executed.
2. **Agent-level access is the only mode.** The tooling is built for
   the restricted case. Humans who need broader access use raw `gh`
   or the GitHub UI — escape hatches that agents don't have.
3. **Server-side permissions are the enforcement boundary.** Token
   scope is broad (classic PAT); account access is narrow (outside
   collaborator on specific repos). Branch protection prevents
   merging. The wrapper prevents the agent from even attempting
   operations outside its role.
4. **No global state changes.** Credential selection is per-subprocess
   via `GH_TOKEN` env var injection. `gh auth switch` is never called.
   Parallel sessions are fully isolated.

## Section 1: Identity Model

Unchanged from the org governance design (#717, Section 1). Two
accounts per contributor plus a shared GitHub App:

| Identity | Role | Scope |
|---|---|---|
| `<username>` | Human — reviews, approvals, merges, admin | Org owner/member across all orgs |
| `<username>-vergil` | AI agents — all development work | Outside collaborator on each org |
| `vergil-release[bot]` | GitHub App — mechanized release automation | Org-level installation per org |

The naming convention `<username>-vergil` is load-bearing — the
tooling derives the agent account name from the human account name.

## Section 2: Token Strategy

### Token Type: Classic PATs

Both accounts use classic PATs. Fine-grained PATs are abandoned for
the reasons stated in the Problem section.

**Human account PAT scopes:**
- `repo` (full repository access)
- `admin:org` (org settings, collaborator management)
- `workflow` (GitHub Actions)
- `read:org` (org membership visibility)

**Agent account PAT scopes:**
- `repo` (full repository access)
- `read:org` (org membership visibility)

The agent PAT intentionally excludes `admin:org`, `workflow`, and
other administrative scopes. While classic PATs are coarser than
fine-grained PATs, scope restrictions still limit what the token
can do at the API level.

### Why Classic PATs Are Acceptable

The security model does not depend on token scope as the primary
enforcement mechanism. Defense in depth:

1. **Account permissions** — the agent account is an outside
   collaborator with Write access to specific repos. It cannot
   access repos it hasn't been invited to, regardless of token
   scope.
2. **Branch protection** — rulesets prevent merging without review,
   prevent direct pushes to protected branches, require CI to pass.
3. **`vrg-gh` wrapper** — gates which operations the agent can
   attempt and which credential is used for each.
4. **Token scope** — the narrowest classic PAT scopes that support
   the required operations. This is the fourth layer, not the first.

### Multi-Org Support

A classic PAT works across all repos the account has access to,
regardless of which org owns them. One token per account covers
all orgs. Adding a new org requires only inviting the agent account
as an outside collaborator — no token changes.

## Section 3: Credential Store

### `gh auth` as the Sole Credential Layer

Both accounts are logged into `gh auth` on the developer's machine.
No custom keychain entries, no `vergil/*` keychain names, no
platform-specific secure store abstraction.

**Setup (one-time per machine):**

```bash
# Human account (already logged in for most developers)
echo "<human-classic-pat>" | gh auth login --with-token

# Agent account
echo "<agent-classic-pat>" | gh auth login --with-token
```

The `--with-token` flag determines the account from the token itself
by calling the GitHub API — there is no `-u` flag for `--with-token`
mode. When the second token resolves to a different user, `gh` adds
it as an additional logged-in account automatically.

**Verification (run after both logins):**

```bash
gh auth status    # Should show both accounts logged in
```

**Token retrieval (used by tooling):**

```bash
gh auth token -u <username>          # Returns human PAT
gh auth token -u <username>-vergil    # Returns agent PAT
```

This is a read operation. It does not change the active account.
It does not affect other sessions. It is safe to call from parallel
processes.

### What Gets Retired

The following are retired once the `vrg-gh` wrapper is operational:

| Item | Current state | Action |
|---|---|---|
| `vergil/human-pat` keychain entry | Fine-grained PAT | Delete from keychain |
| `vergil/agent-pat` keychain entry | Never created (#761, closed as won't-fix) | No action needed |
| `vergil/app-id` keychain entry | GitHub App ID | Retain (used by release tooling for App token exchange) |
| `vergil/app-private-key` keychain entry | GitHub App private key | Retain (used by release tooling for App token exchange) |
| `GH_TOKEN` keychain entry | Classic PAT | Delete after transition |
| `GH_TOKEN` in `.zshrc` `_KEYCHAIN_VARS` | Loads classic PAT into env | Remove after transition |
| `vrg-setup-credentials` (planned) | Never built | Cancel — `gh auth login` replaces it |
| `vrg-credential-audit` (planned) | Never built | Deferred — the need for credential visibility remains; see Section 8 |

The GitHub App keychain entries (`vergil/app-id`,
`vergil/app-private-key`) are retained because the App token
exchange for release automation is a separate mechanism from
contributor credential management. The App is not a contributor
identity — it is an org-level automation identity with its own
auth flow (JWT exchange for short-lived installation tokens).

## Section 4: `vrg-gh` Credential Selection

### Design Constraint

`vrg-gh` is responsible for choosing which account's token to use
based on the command being executed. The caller has no input into
this decision. There is no flag, no env var, no override. This is
a hard security boundary.

### Default: Agent Account

All operations default to the agent account. This is the safe
baseline. Development operations — creating PRs, pushing branches,
viewing status, creating issues — run under the agent identity.

### Escalation to Human Account

Specific operations are permitted to escalate to the human account,
but only when the command AND its context pass validation. The
credential gate checks both what command and what context:

- **Merge:** Allowed under the human account only when the target
  matches release workflow patterns (e.g., `release/*` branches
  merging to `main`, back-merge from `main` to `develop`). Merging
  arbitrary feature branches is denied.
- **Approval:** Allowed under the human account only for release
  PRs authored by the GitHub App (`vergil-release[bot]`).
- **Other admin operations:** Denied entirely through the wrapper.

The specific per-command role mapping is an implementation detail
determined during `vrg-gh` development. The design constraint is:
escalation requires both a permitted command and a validated
context.

**Interaction with permission model (#754):** The permission model
spec denies `pr merge` and `pr review --approve` unconditionally
in the `vrg-gh` subcommand allowlist. This spec supersedes that
denial for release workflow operations. The permission model plan
(Task 2) must be updated: `pr merge` and `pr review --approve`
move from "denied" to "conditionally allowed with context
validation and credential escalation." The escalation logic should
be factored out as a testable component within `vrg-gh`.

### Mechanism

```python
# Pseudocode — the shared credential selection function
def select_credential(command: list[str]) -> str:
    role = determine_role(command)  # agent or human
    if role == "human":
        validate_context(command)   # raises if context invalid
    account = f"{human_account}-vergil" if role == "agent" else human_account
    return subprocess.run(
        ["gh", "auth", "token", "-u", account],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

def run_gh(command: list[str]) -> ...:
    token = select_credential(command)
    env = {**os.environ, "GH_TOKEN": token}
    return subprocess.run(["gh", *command], env=env, ...)
```

This logic lives in the `vrg-gh` CLI entry point. Mechanized
tools that need credential escalation (`vrg-merge-when-green`,
`vrg-prepare-release`, etc.) set `GH_TOKEN` directly in their
process environment before calling `github.py` — see Section 5.

### No Human Detection

The tooling does not distinguish between a human caller and an
agent caller. It always operates in agent-restricted mode. If a
human runs `vrg-gh pr merge` on a feature branch, it is denied
the same way it would be for an agent. The human's escape hatch
is raw `gh` or the GitHub UI — tools the agent does not have
access to.

### Account Discovery

The wrapper discovers account names by enumerating logged-in
accounts from `gh auth status`:

1. List all accounts logged into `gh auth` for `github.com`
2. Identify the human account: the one whose name does **not**
   end in `-vergil`
3. Derive the agent account: `<human-account>-vergil`
4. If both or neither match the convention, fail with an
   explicit error

This is deterministic regardless of which account is currently
active (`gh auth switch` has no effect on the discovery). No
config values in `vergil.toml`, no hardcoded usernames. A new
contributor's accounts are discovered the same way.

### Failure Modes

- **Required account not in `gh auth`:** Explicit error naming
  the missing account, suggesting `gh auth login`.
- **Escalation denied:** Explicit error explaining which context
  validation failed and why (e.g., "merge denied: branch
  `feature/foo` does not match release workflow pattern").
- **Never silent fallback:** The wrapper never substitutes one
  account for another. If the required account is unavailable,
  the operation fails.

## Section 5: Integration with `github.py`

The existing `github.py` module calls `gh` via `subprocess.run`
with no credential management — it inherits the ambient `GH_TOKEN`.
This continues to work under the new model. `github.py` remains
unaware of credentials — it inherits from the process environment
as it does today.

**Credential selection is the caller's responsibility.** The
`vrg-gh` CLI handles the simple case: agent by default, human
for release-pattern operations. Mechanized tools that call
`github.py` directly (`vrg-merge-when-green`,
`vrg-prepare-release`, etc.) set `GH_TOKEN` in their own process
environment before calling `github.py` functions. This is how
the release workflow switches between App token (for PR creation)
and human token (for approval and merge) — each phase sets the
appropriate token before invoking `github.py`.

This keeps `github.py` simple and avoids threading credential
logic through every function signature. The calling code changes
only to set `GH_TOKEN` before its `github.py` calls — the
library's API surface does not change.

## Section 6: `vrg-docker-run` and Container Credentials

`vrg-docker-run` currently hard-requires `GH_TOKEN` in the
environment (line 80 of `vrg_docker_run.py`). Under the new model:

1. **Remove the hard gate.** `GH_TOKEN` in the ambient environment
   is no longer guaranteed or expected. The container launches
   regardless.

GitHub CLI and git operations are host-side tools by design. The
container runs tests, linters, and builds — it does not need
GitHub credentials. `vrg-validate` and other container commands
do not use `GH_TOKEN`. No credential retrieval logic is added to
`vrg-docker-run`.

The existing env-var passthrough in `docker.py` (which forwards
`GH_*`, `GITHUB_*`, and `MQ_*` prefixes into the container) is
left as-is for now. Cleaning up the hardcoded prefix list is
tracked separately (#777).

## Section 7: Transitional State

### Current State (the bridge)

`.zshrc` loads `GH_TOKEN` from macOS Keychain into the shell
environment. This classic PAT for `wphillipmoore` is used by
everything. The honor system. This continues to work while
`vrg-gh` is implemented.

### Migration Sequence

1. **Immediate (no code changes):** Ensure both accounts have
   classic PATs and are logged into `gh auth`. Verify
   `gh auth token -u wphillipmoore` and
   `gh auth token -u wphillipmoore-vergil` both return tokens.
   The `GH_TOKEN` from `.zshrc` continues as the ambient fallback.

2. **When `vrg-gh` lands:** The wrapper handles all credential
   selection. `github.py` uses the shared logic. All `vrg-*` tool
   operations go through the credential gate. The ambient
   `GH_TOKEN` is no longer consumed by vergil tooling.

3. **After validation period:** Remove `GH_TOKEN` from
   `_KEYCHAIN_VARS` in `.zshrc`. Delete the `GH_TOKEN` and
   `vergil/human-pat` keychain entries. `gh auth` is the sole
   credential store. Raw `gh` commands fall back to `gh auth`'s
   active account (the human account).

### What Gets Updated

| Document | Section | Change |
|---|---|---|
| Org governance design (#717) | Section 3 (Credential Management) | Superseded by this spec |
| Org governance setup plan | Tasks 2, 3, 10 (PAT generation, keychain storage) | Rewritten for classic PATs and `gh auth` |
| Permission model design (#754) | `vrg-gh` wrapper (Section 3) | Gains credential selection responsibility; `pr merge` and `pr review --approve` change from denied to conditionally allowed |
| Permission model plan | Task 2 (`vrg-gh`) | Updated to include credential selection logic and escalation component |
| Consuming repo setup guide | Environment setup | Updated to reference `gh auth`, not `GH_TOKEN` export |
| `vrg_docker_run.py` | Usage text (line 40) | Remove `GH_TOKEN (required)` — no longer required |
| Consuming repo CLAUDE.md files | Development commands | Updated to note `vrg-gh` handles credential selection |

## Section 8: Credential Lifecycle (Deferred)

Token expiration monitoring, rotation procedures, and compromise
response are real requirements that are deferred from this spec.
The org governance design (#717, Section 3) defined these in
detail for fine-grained PATs; the requirements carry forward for
classic PATs with minor adjustments.

### What Is Deferred

- **Token expiration monitoring.** Classic PATs can be created
  with expiration dates (GitHub defaults to expiring tokens).
  An expired token in `gh auth` still returns from
  `gh auth token` but fails with 401 on API calls. A periodic
  report (not inline warnings) is the likely mechanism.
- **Rotation procedures.** Annual rotation cadence, proactive
  refresh before expiration, documented steps for replacing
  tokens in `gh auth`.
- **Compromise response.** Revocation, audit, replacement —
  carried forward from the governance design with adjustments
  for classic PATs and `gh auth`.
- **Whether `vrg-credential-audit` survives.** The planned tool
  was descoped (Section 3), but the need for credential
  visibility remains. It may be revived in modified form.

### Follow-On Work

The implementation plan must include tasks to create issues for
each deferred item above. The 12-month expiration window on
newly-created PATs provides runway, but the issues must exist
to prevent this work from being silently dropped.

## Section 9: New Contributor Onboarding

The setup process for a new contributor:

1. Create a `<username>-vergil` GitHub account (the `-vergil` suffix
   is load-bearing — the tooling uses it to distinguish human from
   agent accounts)
2. Generate classic PATs for both accounts
3. Log both accounts into `gh auth`:
   ```bash
   # Human account (order matters — log in the human account first)
   echo "<human-pat>" | gh auth login --with-token

   # Agent account (gh resolves the username from the token)
   echo "<agent-pat>" | gh auth login --with-token
   ```
4. Verify both accounts are visible:
   ```bash
   gh auth status              # Should list both accounts
   gh auth token -u <username>          # Returns human PAT
   gh auth token -u <username>-vergil    # Returns agent PAT
   ```
5. Org owner invites `<username>-vergil` as outside collaborator
   to each org
6. Install vergil-tooling (`uv tool install`)

No per-org token setup. No keychain configuration. No custom
credential utilities. One classic PAT per account covers all orgs.
