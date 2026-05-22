# Identity Setup

This guide walks through creating and configuring the GitHub App
identity required by VERGIL tooling. Each contributor registers
one GitHub App that acts as their agent identity across all
managed organizations.

For repo-level onboarding (installing tools, configuring hooks,
CI), see [Consuming Repo Setup](consuming-repo-setup.md).

## Why a GitHub App

VERGIL uses GitHub App installation tokens for agent
authentication. Compared to the legacy multi-account model
(deprecated), Apps provide:

- **No shadow-ban risk** — Apps are first-class GitHub citizens
- **Short-lived tokens** — 1-hour installation tokens vs.
  long-lived PATs
- **Multi-org from one identity** — install the same App on
  every org you manage
- **Server-side merge control** — branch protection recognizes
  the App identity
- **Dynamic token resolution** — the tooling acquires tokens
  on demand per-org, no static configuration needed

## Prerequisites

- A GitHub account that is an **owner** of the organizations
  where the agent will operate
- `~/.config/vergil/keys/` directory on your development machine

## Step 1: Register the GitHub App

1. Go to **github.com** > your profile icon > **Settings** >
   **Developer settings** > **GitHub Apps** > **New GitHub App**

2. Fill in the registration form:

   | Field | Value |
   |---|---|
   | **App name** | `<username>-vergil` |
   | **Description** | AI agent identity for the Vergil project. Used by automated agents running inside isolated Lima VMs to manage repositories. Authenticates via installation tokens for GitHub API operations including commits, pull requests, and issue management. |
   | **Homepage URL** | Your GitHub profile URL |
   | **Webhook** | Uncheck "Active" (no webhook needed) |

3. Under **Repository permissions**, set:

   | Permission | Access |
   |---|---|
   | **Contents** | Read and write |
   | **Pull requests** | Read and write |
   | **Issues** | Read and write |

4. Under **Where can this GitHub App be installed?**, select
   **Any account**.

   !!! warning "Cannot change this later without extra steps"
       If you select "Only on this account" during creation, the
       App can only be installed on your personal account — not
       on any organizations. To change it after creation: go to
       the App settings > **Advanced** > under the **Danger
       zone** section, click **Make public**. Once public and
       installed on other accounts, this cannot be reverted.

5. Click **Create GitHub App**.

## Step 2: Generate a private key

1. On the App's settings page (you land here after creation),
   scroll to **Private keys**
2. Click **Generate a private key**
3. Your browser downloads a `.pem` file (named with the App
   name and date)
4. Move it to your Vergil keys directory:

```bash
mv ~/Downloads/<app-name>.<date>.private-key.pem \
   ~/.config/vergil/keys/
```

Keep the original filename — it includes the creation date,
which is useful metadata for key rotation.

## Step 3: Install on organizations and personal account

The tooling resolves tokens dynamically based on the repository
owner (org or user) parsed from the git remote URL. The App
must be installed on every account that owns repositories the
agent will operate on.

1. From the App settings page, click **Install App** in the
   left sidebar
2. Click **Install** next to each organization where the agent
   should operate
3. For each installation, choose **All repositories** (or
   select specific repos)
4. Click **Install**

Repeat for every organization. If you also have personal
repositories managed by the agent, install the App on your
personal account the same way — it appears in the same list
alongside your organizations. Without this installation, the
tooling cannot acquire tokens for personal repos and will fall
back to ambient `gh` auth.

You can return to this page to add more accounts later.

## Step 4: Record the App ID

The **App ID** is shown near the top of the App's **General**
settings page (Developer settings > GitHub Apps > your App >
Edit).

You do not need to record installation IDs — the tooling
resolves these dynamically at runtime via the GitHub API.

## Step 5: Configure `identities.toml`

Create or update `~/.config/vergil/identities.toml`:

```toml
[identities.vergil]
vm_instance = "vergil-agent"
auth_type = "app"
app_id = <your-app-id>
private_key_path = "~/.config/vergil/keys/<app-name>.<date>.private-key.pem"
```

The `private_key_path` is relative to your home directory (if
it starts with `~`) or absolute.

## Verification checklist

After completing all steps, verify:

- [ ] Private key file exists in `~/.config/vergil/keys/`
- [ ] `identities.toml` has the correct App ID and key path
- [ ] The App is installed on all target organizations and your
      personal account if needed (check Developer settings >
      GitHub Apps > your App > Install App)
- [ ] The App shows the correct repository permissions
      (Contents, PRs, Issues — all Read and write)

Full end-to-end verification requires a running identity VM
(see Plan 3: Credential Provisioning).

## Related

- [Consuming Repo Setup](consuming-repo-setup.md) — repo-level
  onboarding
- [Git Workflow](git-workflow.md) — how the per-change cycle
  works
- [Single-account identity design][identity-spec] — full
  rationale for the GitHub App model

[identity-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/2026-05-20-single-account-identity-design.md
