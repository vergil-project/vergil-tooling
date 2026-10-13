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

## Rotating an App private key

Routine rotation replaces an agent App's private key while the old
key is still trusted. The user App (`<username>-vergil-user`) and the
audit App (`<username>-vergil-audit`) each have their own key, so
rotate them one at a time with the steps below. If a key is known or
suspected to be exposed, do not use this procedure. Follow
[Responding to a compromised credential](#responding-to-a-compromised-credential)
instead, which deletes the old key first.

A GitHub App can hold more than one active private key at a time.
Rotation relies on that: you add the new key, switch the agent VM to
it, prove it works, and only then delete the old one. The agent is
never left without a working credential.

No rotation cadence is set for these keys. The date in each key's
filename (see [Account Setup](account-setup.md), Step 1.3) records
when it was generated.

### Steps

1. **Generate a new key.** On the App's settings page, go to
   **Private keys** and click **Generate a private key**, as in
   [Account Setup](account-setup.md), Step 1.3 (user App) or Step 2.3
   (audit App). Leave the existing key in place for now.

2. **Store it next to the old key.** Move the downloaded file into
   `~/.config/vergil/keys/` and keep GitHub's filename, which carries
   the generation date:

   ```bash
   mv ~/Downloads/<username>-vergil-<role>.<YYYY-MM-DD>.private-key.pem \
      ~/.config/vergil/keys/
   ```

3. **Point `identities.toml` at the new key.** In
   `~/.config/vergil/identities.toml`, set the identity's
   `private_key_path` to the new file. The `app_id`, the App's
   permissions, and its installations do not change.

4. **Re-inject the key into the agent VM.** Provisioning copies the
   key into the VM as `~/.config/vergil/app.pem`, and it does so only
   when the VM is created, started, restarted, or rebuilt. A running VM
   keeps using the old key until one of those happens:

   - **Lima:** `vrg-vm restart --identity <identity>` stops and
     starts the VM, then re-injects credentials. This ends any session
     running in the VM, so do it between sessions.
   - **Off-platform:** `vrg-vm rebuild --identity <identity>`.
     Off-platform VMs do not support `restart`, so the rebuild destroys
     the VM and creates it again. Its persistent volume is reattached.

   Do this for every VM provisioned from the identity, including any
   dedicated workspace VM (pass its `<org>/<repo>` after the command,
   for example `vrg-vm restart --identity <identity> <org>/<repo>`).

5. **Verify the new key.** Inside the VM, run a read that has to mint
   an installation token, for example:

   ```bash
   vrg-gh pr list --repo <owner>/<repo>
   ```

   `pr list` is allowed for both the user and the audit identity. Use
   a repository on an account the App is installed on. The check
   passes when the command lists PRs with no warnings. If the key
   cannot mint a token, `vrg-gh` prints
   `GitHub App auth failed, falling back to ambient auth` on stderr.
   Treat that warning as a failed check even if the command goes on to
   return output.

   `vrg-whoami --explain` is not a key check. It reports which
   identity the VM resolves to, from the injected files, but it does
   not use the key to authenticate.

6. **Delete the old key on GitHub.** Back on the App's **Private keys**
   list, delete the old key. Each key is listed with its creation date
   and a SHA-256 fingerprint. To match a local file to a fingerprint,
   run:

   ```bash
   openssl rsa -in <key-file>.pem -pubout -outform DER \
     | openssl sha256 -binary | openssl base64
   ```

   Do not delete the old key before step 5 passes. Until it is deleted,
   a copy of it can still mint tokens.

7. **Delete the old key file.** Remove the old `.pem` from
   `~/.config/vergil/keys/`.

Repeat the steps for the other App.

The org automation App (`vergil-release[bot]`) is not covered by these
steps. Its key is consumed by the `APP_PRIVATE_KEY` Actions secret,
not by an agent VM. The CI publishing secrets are not covered either.
For both, see
[Org automation App](#org-automation-app-vergil-releasebot) and
[CI publishing secrets](#ci-publishing-secrets) under the compromise
procedure below.

## Responding to a compromised credential

Use this procedure when a credential is known or suspected to be
exposed: a private key copied off its host, a secret printed in a log,
a lost or untrusted machine. Act on suspicion. Revoking a credential
that turns out to be safe costs a few minutes of re-provisioning.
Leaving a leaked one live can cost far more.

### The response, in order

1. **Contain.** Revoke or suspend the credential so it cannot be used
   again. The per-credential steps are below.
2. **Halt dependent work.** Stop the operations that use the
   credential, such as agent sessions, releases, and publishing
   workflows, until a replacement is in place. If the agent VM is the
   suspected source of the leak, stop it (`vrg-vm stop` on Lima) or
   destroy it (`vrg-vm destroy` off-platform, where `stop` is not
   supported). An off-platform VM's injected credentials live on its
   ephemeral boot disk and are deleted with it.
3. **Review activity.** Check what the identity did during the window
   of suspected exposure (see [Reviewing activity](#reviewing-activity)).
4. **Restore.** Issue a replacement credential and re-provision
   whatever consumed the old one.
5. **Retrospect.** Find out how the credential was exposed, and decide
   what process or tooling change stops it from happening again.

### Agent App private key (user or audit App)

The private key in `private_key_path` is the agent's only long-lived
credential. Installation tokens are derived from it and expire after
about an hour.

**Blast radius.** The holder of the key can mint tokens for every
account the App is installed on. Each token can reach the repositories
that installation covers. The token is limited to the App's declared
permissions, as listed in [Account Setup](account-setup.md):

| | `<username>-vergil-user` key | `<username>-vergil-audit` key |
|---|---|---|
| Contents | **Read and write.** Can push branches and read all code in covered repos, private repos included. | **Read-only.** Can read all code in covered repos, private repos included. |
| Issues | **Read and write.** Can create, edit, and comment on issues. | Read-only |
| Pull requests | Read-only | **Read and write.** Can write PR reviews and comments. |
| Workflows | No access. Cannot push under `.github/workflows/`. | No access. Cannot push under `.github/workflows/`. |
| Merge | Cannot open, approve, or merge PRs. | Cannot merge, because merging needs `contents: write`. |

Branch protection still limits a leaked user-App key. It cannot push
directly to a protected branch or merge without review. It can still
push malicious content to feature branches, and it can read every
private repo its installations cover. Because both Apps can read code,
treat a leak of either key as exposure of that code.

**Contain.**

- **Delete the compromised key.** Open the App's settings page and go
  to **Private keys**. Delete the compromised key so that no new
  tokens can be minted from it. A token minted before the deletion
  stays valid until it expires (about an hour).
- **Suspend the installations** to cut off access immediately, before
  the outstanding tokens expire. In the settings of each account the
  App is installed on, open the App's installation and suspend it.
  Unsuspend it after the replacement key is in place.

**Restore.** Generate a new private key for the same App as in
[Account Setup](account-setup.md), Step 1.3 or 2.3. Point
`private_key_path` in `identities.toml` at the new file and delete the
old `.pem` from `~/.config/vergil/keys/`. Then re-inject the key into
the agent VM. Credentials are injected when a VM is created, started,
restarted, or rebuilt, so run `vrg-vm restart` on Lima or
`vrg-vm rebuild` off-platform. The App ID, permissions, and
installations do not change. Verify the new key as described in
[Rotating an App private key](#rotating-an-app-private-key), step 5.
For routine, non-emergency key rotation, use that procedure instead.

### The human account

**Blast radius.** This is the most severe case. The human account
holds the authority that the agent Apps lack: review, approval, merge,
and administration (see
[Identity Architecture](identity-architecture.md)). It is also an
owner of the orgs the agents work in, and it owns both agent Apps,
which are registered under its Developer settings. Anyone in control of
the human account can therefore generate fresh keys for both agent
Apps.

**Contain and restore.** Secure the GitHub account itself, then treat
**both** agent App keys as compromised and work through the App-key
procedure above for each one. Review the account's recent merge and
administrative activity in addition to the agent activity.

### Org automation App (`vergil-release[bot]`)

The org-level App does mechanized release work. It creates release
PRs, and a human approves and merges them (see
[Identity Architecture](identity-architecture.md#the-release-app)).
CI also reaches the org's App through the `APP_CLIENT_ID` and
`APP_PRIVATE_KEY` Actions secrets. The `cd-release` workflow uses
them to dispatch to the org's `packages` repository (see
[Package config](../reference/package-config.md)). The `ops` and
`epic-rollup` workflows also receive them.

**Blast radius.** The App is installed at the org level, not per
contributor, so a leaked key reaches every repo that installation
covers. Its permission set is not listed in the Account Setup tables.
Read the exact permissions from the App's settings page before you
assess the impact. Release PRs it creates still need human approval to
merge.

**Contain and restore.** Follow the same steps as for an agent App key:
delete the key, suspend the installation if access must stop at once,
and generate a new key. Then update the `APP_PRIVATE_KEY` secret
wherever it is stored. Until you do, the workflows that depend on it
(release dispatch, ops, epic roll-up) will fail.

### CI publishing secrets

The release workflow forwards per-ecosystem secrets to `cd-release`
(see [CI Architecture](ci-architecture.md#cd-release-publishing-secrets)):

| Secret | Blast radius |
|---|---|
| `CARGO_REGISTRY_TOKEN`, `RUBYGEMS_API_KEY`, `CENTRAL_USERNAME`/`CENTRAL_TOKEN` | Can publish to that registry under the project's account |
| `GPG_PRIVATE_KEY`/`GPG_PASSPHRASE` (java) | Can sign artifacts as the project |
| `PACKAGE_SIGNING_KEY`/`PACKAGE_SIGNING_PASSPHRASE` | Can sign `.deb`/`.rpm` packages that apt/dnf clients will trust, because clients trust the org signing key |

Python publishing uses PyPI OIDC trusted publishing, so there is no
stored PyPI token to leak.

**Contain.** Revoke the token or key **at its issuer**, such as the
registry account or the GPG key, so that a leaked copy stops working.
Changing only the GitHub secret leaves the leaked copy valid. Halt
releases until a replacement is in place.

**Restore.** Issue a replacement at the issuer. Then update the
repository, organization, or environment secret that held it.
`PACKAGE_SIGNING_*` live in the `package-signing` environment. The
package signing key is more involved to replace than the others,
because clients pin it. Its primary fingerprint is pinned in
vergil-tooling, checked by `vrg-vm`, published in
[Getting Started](../getting-started.md), and shipped in the
`vergil-archive-keyring` package (see
[VM spec](../reference/vm-spec.md)). Replacing it means changing all
of those together.

### Reviewing activity

- **Org audit log.** In the organization's settings, open **Audit
  log** and filter by the compromised identity across the window of
  suspected exposure. For an agent App, that identity is the
  `<username>-vergil-<role>[bot]` actor. Repeat for every org the
  App is installed on.
- **Repository history.** Every commit, PR, review, and comment made
  through an App carries its bot identity, so the App's work stands
  out in branch, PR, and issue history. Look for unexpected branches,
  pushes, PRs, reviews, and issue edits.
- **Release artifacts.** For a publishing secret, check the registry
  or package index for versions you did not release.

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
