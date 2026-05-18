# Credential Management

VERGIL uses `gh auth` as the sole credential store and selects
credentials per-subprocess based on the operation being performed.
No global state changes, no ambient environment variables, no
custom keychain management.

For account creation and initial setup, see
[Account Setup](account-setup.md). For the identity model that
credentials serve, see
[Identity Architecture](identity-architecture.md).

## Token Strategy

### Classic PATs

Both accounts use classic Personal Access Tokens. Fine-grained
PATs were evaluated and abandoned:

- Fine-grained PATs are scoped to a single resource owner — a
  token for `vergil-project` cannot access `diogenes-project`.
  Multi-org requires N×2 tokens.
- `gh auth` stores one token per account per host. Multiple
  fine-grained PATs for the same account cannot coexist.
- Fine-grained PATs cannot be created by outside collaborators
  scoped to an org. The agent account is an outside collaborator
  by design.

Classic PATs work across all repos the account has access to,
regardless of org. One token per account covers everything.

### Token Scopes

**Human account:**

| Scope | Purpose |
|---|---|
| `repo` | Full repository access |
| `admin:org` | Org settings, collaborator management |
| `workflow` | GitHub Actions |
| `read:org` | Org membership visibility |

**Agent account:**

| Scope | Purpose |
|---|---|
| `repo` | Full repository access |
| `read:org` | Org membership visibility |

The agent PAT intentionally excludes `admin:org` and `workflow`.
Token scope is not the primary security boundary — it is the
fourth layer of defense. See
[Permission Model](permission-model.md) for the full
defense-in-depth architecture.

## Credential Store

### `gh auth` as the Single Source

Both accounts are logged into `gh auth` on the developer's
machine. No custom keychain entries, no platform-specific secure
store abstraction. Setup is a one-time operation per machine.

The login command is the same both times — what changes is which
GitHub account is authenticated in your browser when you authorize
the OAuth flow.

!!! warning "`gh auth login` has no `-u` flag"
    You cannot specify which account to log in as on the command
    line. The browser session determines which account gets
    logged in.

**Step 1 — Log in the human account.** Make sure you are signed
into github.com as your personal account in the browser, then
run:

```bash
gh auth login -h github.com --web -p https
```

Complete the OAuth authorization in the browser. `gh` adds your
human account to its credential store.

**Step 2 — Log in the agent account.** Switch your browser session
to the `-vergil` account (sign out and sign back in as
`<username>-vergil`), then run the same command again:

```bash
gh auth login -h github.com --web -p https
```

Complete the OAuth authorization. `gh` detects that this is a
different account and adds it alongside the first — it does not
replace it.

**Step 3 — Restore the human account as the active default.**
After both logins, switch back so your human account is active
for any raw `gh` commands:

```bash
gh auth switch -u <your-username>
```

### Token Retrieval

The tooling retrieves tokens without changing global state:

```bash
gh auth token -u <username>          # Human PAT
gh auth token -u <username>-vergil   # Agent PAT
```

This is a read operation. It does not change the active account
and is safe to call from parallel processes.

## Credential Selection

### The Core Principle

The tool selects the credential, not the caller. There is no flag,
no env var, no override. The `vrg-gh` wrapper determines the
appropriate identity based on the command being executed. This is
a hard security boundary.

### Default: Agent Account

All operations default to the agent account. Development
operations — creating PRs, pushing branches, viewing status,
creating issues — run under the agent identity.

### Escalation to Human Account

Specific operations escalate to the human account when both the
command and its context pass validation:

- **Merge:** Allowed under the human account only when the target
  matches release workflow patterns (e.g., `release/*` branches
  merging to `main`, back-merge from `main` to `develop`).
- **Approval:** Allowed under the human account only for release
  PRs authored by `vergil-release[bot]`.
- **Other admin operations:** Denied entirely.

Escalation requires both a permitted command and a validated
context. A merge request for a feature branch is denied even
though merges are conditionally allowed.

### No Human Detection

The tooling does not distinguish between a human caller and an
agent caller. It always operates in agent-restricted mode. If a
human runs `vrg-gh pr merge` on a feature branch, it is denied
the same way. The human's escape hatch is raw `gh` or the GitHub
UI.

## GH_TOKEN Injection

Credential selection is per-subprocess via the `GH_TOKEN`
environment variable. The tooling never calls `gh auth switch`.

### How It Works

1. `_discover_accounts()` parses `gh auth status` to find the
   `-vergil` account and derive the human account name.
2. `_human_token()` calls `gh auth token -u <human>` and caches
   the result for the process lifetime.
3. `_gh_env()` builds an environment dict with `GH_TOKEN` set to
   the appropriate token.
4. Every `subprocess.run` call that invokes `gh` receives this
   environment dict via the `env` parameter.

Parallel sessions are fully isolated. Each process resolves its
own token independently. No shared state, no lock contention.

### Graceful Degradation

If credential discovery fails (account not logged in, `gh` not
installed), `_gh_env()` returns `None` and the subprocess inherits
the parent environment. This allows the tooling to function in CI
environments where credentials are provided through other
mechanisms.

## Account Discovery

The discovery algorithm is deliberately simple:

1. Run `gh auth status` and parse all logged-in accounts.
2. Find the one account ending in `-vergil`.
3. Derive the human account by stripping the suffix.
4. If zero or more than one `-vergil` account exists, fail with
   an explicit error.

Any number of other accounts can be present — `-mimir`, legacy
`-agent`, personal accounts — they are all ignored. Only the
`-vergil` suffix matters.

### Failure Modes

- **No `-vergil` account:** Explicit error naming the convention
  and suggesting `gh auth login`.
- **Multiple `-vergil` accounts:** Explicit error listing the
  accounts found.
- **Required account not in `gh auth`:** Explicit error suggesting
  the missing login.
- **Never silent fallback:** The tooling never substitutes one
  account for another. If the required credential is unavailable,
  the operation fails.

## Security Model

The credential system does not depend on token scope as the
primary enforcement mechanism. Defense in depth, in priority order:

1. **Account permissions** — the agent account is an outside
   collaborator with Write access to specific repos. It cannot
   access repos it hasn't been invited to, regardless of token
   scope.
2. **Branch protection** — rulesets prevent merging without review,
   prevent direct pushes to protected branches, require CI to
   pass.
3. **`vrg-gh` wrapper** — gates which operations the agent can
   attempt and which credential is used for each.
4. **Token scope** — the narrowest classic PAT scopes that support
   the required operations.

## Multi-Org Support

A classic PAT works across all repos the account has access to,
regardless of which org owns them. Adding a new org requires only
inviting the agent account as an outside collaborator — no token
changes, no credential reconfiguration.

## Related

- [Account Setup](account-setup.md) — creating accounts and
  logging into `gh auth`
- [Identity Architecture](identity-architecture.md) — the
  two-account model and naming conventions
- [Permission Model](permission-model.md) — enforcement layers
  beyond credential selection
- [Credential management design spec][cred-spec] — full decision
  rationale and alternatives considered

[cred-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/2026-05-14-credential-management-design.md
