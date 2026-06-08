# Identity Setup

This guide walks through creating and configuring the GitHub App
identities that VERGIL tooling uses for AI agents. Each contributor
registers **two** GitHub Apps — one per agent role — under their
personal account.

For the model these identities serve, see
[Identity Architecture](identity-architecture.md). For how
credentials are selected at runtime, see
[Credential Management](credential-management.md). For repo-level
onboarding (installing tools, hooks, CI), see
[Consuming Repo Setup](consuming-repo-setup.md).

## The two agent identities

Every AI agent is represented by a GitHub App whose name encodes the
human who owns it and the agent's role: `<username>-vergil-<role>`.
The App's installation is the agent's only credential, and the agent's
capability is bounded entirely by the App's declared permission shape.
There are no agent user accounts and no collaborator grants — the bot
identity (`<username>-vergil-<role>[bot]`) that appears on commits and
PRs comes from the App itself.

| App | Role | Shape |
|---|---|---|
| `<username>-vergil-user` | Daily development (Driver) | write code, read PRs |
| `<username>-vergil-audit` | PR review (Officials) | read code, write PRs (inverted) |

A third role — `<username>-vergil-admin` — is a reserved slot and is
**not** provisioned. Do not create it.

The two Apps have deliberately **inverted** permission shapes. The
user App can write code but only read PRs; the audit App can only read
code but can write (review/comment on) PRs. This split is the core of
the permission model — set the permissions exactly as documented.

## Why GitHub Apps

- **No shadow-ban risk** — Apps are first-class GitHub citizens
- **Short-lived tokens** — 1-hour installation tokens, minted on
  demand, instead of long-lived PATs
- **Multi-org from one identity** — install the same App on every org
  you manage
- **Server-side merge control** — branch protection recognizes the App
  identity
- **Dynamic token resolution** — the tooling acquires tokens per-org at
  runtime from the App's private key; no static token configuration

## Prerequisites

- A GitHub account that is an **owner** of the organizations where the
  agents will operate
- A `~/.config/vergil/keys/` directory on your development machine

---

## Part 1 — The user App (`<username>-vergil-user`)

### Step 1.1 — Register the App

1. Go to **github.com** → your profile icon → **Settings** →
   **Developer settings** (bottom of the left sidebar) →
   **GitHub Apps** → **New GitHub App**.

2. Fill in the registration form:

   | Field | Value |
   |---|---|
   | **GitHub App name** | `<username>-vergil-user` |
   | **Description** | AI agent identity (user / Driver role) owned by `<username>`. Daily development agent running in an isolated VM; authenticates via installation tokens for commits, pushes, and issues. |
   | **Homepage URL** | your GitHub profile URL |
   | **Webhook** | uncheck **"Active"** (no webhook needed) |

3. Under **Repository permissions**, set exactly these and leave
   everything else at **No access**:

   | Permission | Access |
   |---|---|
   | **Contents** | Read and write |
   | **Issues** | Read and write |
   | **Pull requests** | **Read-only** |
   | **Metadata** | Read-only (mandatory; usually auto-selected) |
   | **Workflows** | **No access** — leave untouched |

   !!! warning "Pull requests is Read-only on purpose"
       The user agent does not open or comment on PRs. Its workflow
       ends at "ready for PR"; the human submits the PR from the host.
       `pull_requests: read` is the server-side hard gate that makes
       `gh pr create` fail for this identity even if the soft gate is
       bypassed. The agent still needs `read` to see PR and CI status.

   !!! warning "Never grant Workflows"
       No agent identity may push changes under `.github/workflows/`.
       Leaving Workflows at **No access** is what makes GitHub reject
       such a push server-side. This is deliberate containment, not an
       oversight.

4. Under **Where can this GitHub App be installed?**, select
   **Any account** — this lets you install it on your organizations,
   not just your personal account.

   !!! warning "Cannot change this easily later"
       If you select "Only on this account", the App can only be
       installed on your personal account. To change it afterward:
       App settings → **Advanced** → **Danger zone** → **Make
       public**. Once public and installed elsewhere, this cannot be
       reverted.

5. Click **Create GitHub App**. You land on the App's settings page,
   which shows the **App ID** and **Client ID** near the top.

### Step 1.2 — Record the App ID and Client ID

On the **General** settings page, record both identifiers:

- **App ID** — a short number (e.g., `3940487`).
- **Client ID** — a string beginning with `Iv` (e.g.,
  `Iv23li...`).

The tooling authenticates using the **App ID** today (it becomes the
JWT `iss` claim via `VRG_APP_ID`). GitHub is gradually migrating
GitHub App authentication from the numeric App ID toward the Client
ID; that migration is only partially rolled out, so the App ID is
still what the tooling uses. Record **both** now so the eventual
switch to Client ID needs no return trip to the GitHub UI.

You do not need installation IDs — the tooling resolves those
dynamically at runtime.

### Step 1.3 — Generate a private key

1. Ensure the keys directory exists:

   ```bash
   mkdir -p ~/.config/vergil/keys
   ```

2. On the App settings page, scroll to **Private keys** → click
   **Generate a private key**. Your browser downloads a `.pem` file.

3. Move it into the keys directory, keeping the original filename:

   ```bash
   mv ~/Downloads/<username>-vergil-user.<YYYY-MM-DD>.private-key.pem \
      ~/.config/vergil/keys/
   ```

   GitHub names the file `<app-name>.<YYYY-MM-DD>.private-key.pem`
   (for example, `wphillipmoore-vergil-user.2026-06-02.private-key.pem`).
   Keep the name — the date is useful metadata for key rotation.

!!! note "No install prompt is expected here"
    On a freshly created App you may see no "install this App" banner
    after generating the key. Installation is a separate, deliberate
    step (next) — do not wait for a prompt.

### Step 1.4 — Install the App

1. On the App's settings page, left sidebar → **Install App**.
2. GitHub lists every account you can install on — your personal
   account and each organization you own.
3. Click **Install** next to **each account that owns repositories
   this agent will operate on**. A single App installed on multiple
   accounts is normal and expected — it works across every org you
   manage. Without an installation on a given account, the tooling
   cannot mint a token for that account's repos.
4. Choose repository access:
   - **All repositories** — covers every repo in that account,
     including ones added later (low friction).
   - **Only select repositories** — tighter; you return here to add
     more.
5. Click **Install**. You land on the installation's configuration
   page (its URL contains an installation ID, which you do **not**
   need to record — the tooling resolves it at runtime).

The user App is now fully provisioned. Configuring
`identities.toml` happens in **Part 3**, after the audit App exists.

---

## Part 2 — The audit App (`<username>-vergil-audit`)

The audit App follows the **same flow** as the user App, with two
differences: the name, and an **inverted permission shape** (it reads
code and writes PRs, the mirror of the user App).

### Step 2.1 — Register the App

Follow Step 1.1, changing:

- **GitHub App name**: `<username>-vergil-audit`
- **Description**: AI agent identity (audit / Officials role) owned by
  `<username>`. PR-review agent running in an isolated VM; reads code
  and writes PR reviews/comments via installation tokens.
- **Repository permissions** — note these are **inverted** from the
  user App:

  | Permission | Access |
  |---|---|
  | **Contents** | **Read-only** |
  | **Issues** | **Read-only** |
  | **Pull requests** | **Read and write** |
  | **Metadata** | Read-only (mandatory) |
  | **Workflows** | **No access** — leave untouched |

Everything else (Webhook unchecked, **Any account**, the "Make
public" warning) is identical to Step 1.1.

### Step 2.2 — Record the App ID and Client ID

Same as Step 1.2 — record both for the audit App.

### Step 2.3 — Generate a private key

Same as Step 1.3. The downloaded file is named
`<username>-vergil-audit.<YYYY-MM-DD>.private-key.pem`; move it into
`~/.config/vergil/keys/`.

### Step 2.4 — Install the App

Same as Step 1.4 — install the audit App on the same set of accounts
as the user App.

---

## Part 3 — Configure `identities.toml`

`~/.config/vergil/identities.toml` is the host-side configuration that
tells VM provisioning which App credentials to inject into each agent
VM. The dual-identity model uses one stanza per role.

```toml
default_identity = "vergil-user"
vergil = "v2.0"        # vergil-tooling version for the human host
vergil-vm = "v2.1"     # VM template tag (from the vergil-vm repo)

[identities.vergil-user]
vm_instance = "vergil-user"
auth_type = "app"
app_id = <user-app-id>
private_key_path = "~/.config/vergil/keys/<username>-vergil-user.<date>.private-key.pem"
vergil = "v2.1"        # pin this VM's vergil-tooling to 2.1 (set once 2.1 is released)
# client_id = "Iv..."  # recorded for the App ID → Client ID migration; not yet read by the tooling

[identities.vergil-audit]
vm_instance = "vergil-audit"
auth_type = "app"
app_id = <audit-app-id>
private_key_path = "~/.config/vergil/keys/<username>-vergil-audit.<date>.private-key.pem"
vergil = "v2.1"
# client_id = "Iv..."
```

Notes:

- **`app_id`** is the identifier the tooling uses today (it becomes
  `VRG_APP_ID` inside the VM). The Client ID is recorded only as a
  comment until the App ID → Client ID migration lands in the tooling.
- **`private_key_path`** is relative to your home directory when it
  starts with `~`, otherwise absolute.
- **Per-identity `vergil` pin.** Setting `vergil = "v2.1"` on the
  agent identities lets the agent VMs run new tooling while the human
  host stays on a stable `v2.0` (the top-level default). Only set the
  `v2.1` pin once 2.1 has been released.
- **`VRG_IDENTITY_MODE`** (`user` / `audit`) is derived from the
  identity's stanza name at provisioning time — a name containing
  `user` provisions as user mode, `audit` as audit mode; a name
  containing neither (or both) fails provisioning. The mode is
  written to `~/.config/vergil/identity-mode` in the VM and exported
  from the shell profile. It selects the identity-aware allowlists
  at runtime; it is a soft-gate ergonomic, not the security boundary
  (the App credential is).
- **Query identity with `vrg-whoami`, never a single environment
  variable.** `VRG_IDENTITY_MODE` is only the first of five fallback
  steps (env var → mode file → `app.pem` → `VRG_APP_ID` → human); an
  unset value means "fall through," not "default to human." Ask the
  authoritative resolver instead: `vrg-whoami` prints the resolved role,
  `vrg-whoami --mode` emits a single token for scripting
  (`export VRG_IDENTITY_MODE="$(vrg-whoami --mode)"`), and
  `vrg-whoami --explain` reports which signal resolved and warns when
  signals disagree.

## Verification checklist

After both Apps are registered, installed, and configured:

- [ ] Two private key files exist in `~/.config/vergil/keys/` (one per
      App), with their generation dates in the filenames
- [ ] `identities.toml` has a `vergil-user` and a `vergil-audit` stanza, each with
      the correct App ID and key path
- [ ] Each App shows the correct **inverted** repository permissions
      (user: contents/issues write, PRs read; audit: contents/issues
      read, PRs write; neither holds Workflows)
- [ ] Both Apps are installed on every account that owns repos the
      agents will operate on
- [ ] The reserved `<username>-vergil-admin` App was **not** created

Full end-to-end verification requires the agent VMs to be provisioned
(vergil-vm) and vergil-tooling 2.1 to be released and pinned.

## Related

- [Identity Architecture](identity-architecture.md) — the model these
  Apps implement
- [Credential Management](credential-management.md) — how installation
  tokens are minted and selected at runtime
- [Consuming Repo Setup](consuming-repo-setup.md) — repo-level
  onboarding
