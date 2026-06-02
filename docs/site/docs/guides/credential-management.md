# Credential Management

VERGIL agents authenticate as **GitHub Apps**. There are no stored
tokens, no classic PATs, and no `gh auth` account juggling in the
agent path. The tooling holds an App's private key, mints a
short-lived installation token on demand, and discards it. Each
operation runs under the App identity whose permission shape bounds
what it can do.

For App registration and initial setup, see
[Account Setup](account-setup.md). For the identity model that
credentials serve, see
[Identity Architecture](identity-architecture.md).

## The credential is the App

An agent's only credential is its GitHub App private key, recorded
in `identities.toml` as `private_key_path`. From that key the
tooling derives everything else at runtime:

- **No static tokens.** Installation tokens live ~1 hour and are
  minted per operation. There is nothing long-lived to leak or
  rotate beyond the private key itself.
- **No `gh auth` dependency in the agent path.** The agent does not
  rely on a logged-in account or a `-vergil` suffix. The App ID and
  private key are injected into the agent VM by provisioning.
- **Permission shape is the boundary.** The token a given App mints
  can only do what that App's declared permissions allow. The user
  App's token cannot write PRs; the audit App's token cannot write
  code. See [Identity Architecture](identity-architecture.md) for
  the inverted shapes.

## How a token is minted

The implementation lives in `src/vergil_tooling/lib/github.py`. The
flow is:

1. **Read the App config.** `VRG_APP_ID` and `VRG_PRIVATE_KEY_PATH`
   identify the App and locate its private key. In an agent VM these
   come from the identity's `app_id` and `private_key_path` in
   `identities.toml`.
2. **Generate a JWT.** A short-lived JWT is signed with the private
   key, using the App ID as the `iss` claim (`iat` backdated 60s,
   `exp` 10 minutes out).
3. **Resolve the installation.** The JWT authenticates a call to
   `GET /app/installations`; the tooling matches the installation
   for the target org.
4. **Mint an installation token.** The matched installation yields a
   ~1-hour token, which is injected as `GH_TOKEN` into the `gh`
   subprocess environment for that operation.

The token is never written to disk and never persisted across
operations. Parallel sessions each mint their own token
independently — no shared state, no lock contention.

!!! note "App ID, not Client ID — for now"
    The tooling authenticates using the numeric **App ID** today (it
    becomes the JWT `iss` claim via `VRG_APP_ID`). GitHub is
    gradually migrating GitHub App authentication toward the Client
    ID, but that migration is only partially rolled out. Record the
    Client ID during setup so the eventual switch needs no return
    trip to the GitHub UI, but the App ID is what the tooling uses
    until the migration lands.

## Credential selection

### The tool selects the credential, not the caller

There is no flag, no env var override, no way for the caller to
choose a different identity. Which App a VM authenticates as is
fixed by provisioning (`VRG_APP_ID` / `VRG_PRIVATE_KEY_PATH` for that
VM). The user VM is the user App; the audit VM is the audit App.

### The permission shape decides what succeeds

Because each App's token is bounded by its declared permissions, the
identity boundary is enforced server-side, not by the wrapper:

- The **user** App can push code and read PRs, but cannot open,
  comment on, approve, or merge PRs — its token holds
  `pull_requests: read`.
- The **audit** App can write PR reviews, but cannot write code or
  merge — its token holds `contents: read`, and merging through the
  API requires `contents: write`.

The `vrg-gh`/`vrg-git` wrappers add a soft ergonomic layer (clear
errors, allowlists) on top of this hard gate, but the App permission
shape is the real boundary.

### Graceful degradation

If App config is absent (no `VRG_APP_ID` / `VRG_PRIVATE_KEY_PATH`),
the token-minting path returns nothing and the `gh` subprocess
inherits the parent environment. This lets the tooling run in CI
environments where credentials are provided through other mechanisms
(for example, an Actions-provided token).

## Security model

Credential security does not rest on token scope. Defense in depth,
in priority order:

1. **App permission shape** — the installation token can only
   perform what the App declares. This is the primary, server-side
   boundary. A user-App token physically cannot merge a PR.
2. **Branch protection** — rulesets prevent merging without review,
   prevent direct pushes to protected branches, and require CI to
   pass.
3. **`vrg-gh` / `vrg-git` wrappers** — gate which operations an agent
   may attempt and surface clear errors. A soft ergonomic layer,
   bypassable by root, not the security boundary.
4. **Short-lived tokens** — ~1-hour installation tokens minted on
   demand, with no long-lived secret beyond the private key.

## Multi-org support

A single App installed on multiple accounts works across every org
the contributor operates in. Installation tokens are minted per-org
from the same private key, so adding a new org requires only
installing the existing App on it — no new keys, no credential
reconfiguration.

## Related

- [Account Setup](account-setup.md) — registering the Apps,
  generating private keys, and configuring `identities.toml`
- [Identity Architecture](identity-architecture.md) — the agent-App
  model and naming conventions
- [Permission Model](permission-model.md) — enforcement layers
  beyond credential selection
- [Credential management design spec][cred-spec] — full decision
  rationale and alternatives considered

[cred-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/2026-05-14-credential-management-design.md
