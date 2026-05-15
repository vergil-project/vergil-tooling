# Account Setup

This guide walks through creating and configuring the GitHub
accounts required by VERGIL tooling. Every contributor needs two
accounts: their normal human account and a dedicated
`<username>-vergil` account for AI agent operations.

For repo-level onboarding (installing tools, configuring hooks,
CI), see [Consuming Repo Setup](consuming-repo-setup.md).

## Why two accounts

VERGIL tooling enforces a hard separation between human and agent
identity. The human account reviews, approves, and merges. The
agent account does all development work — commits, PRs, branch
operations. This separation is enforced by credential selection
in `vrg-gh` and branch protection rules on every managed repo.

The `<username>-vergil` naming convention is load-bearing — the
tooling discovers accounts by finding the one ending in `-vergil`
in `gh auth status` and derives the human account name by
stripping the suffix.

## Step 1: Create the `-vergil` GitHub account

1. **Choose an email address.** Use a Gmail alias to route to your
   existing inbox: `<your-email>+github-vergil@gmail.com`. This
   avoids creating a separate email account while keeping the
   GitHub accounts distinct.

2. **Sign up at github.com/signup.**
   - Username: `<your-github-username>-vergil`
   - Email: the alias from above
   - Complete the CAPTCHA and email verification

3. **Enable two-factor authentication.** Settings → Password and
   authentication → Enable 2FA. Download and store the recovery
   codes securely.

4. **Set email preferences.** Settings → Emails:
   - Check "Keep my email addresses private"
   - Check "Block command line pushes that expose my email"
   - Note the `noreply` email address (format:
     `<id>+<username>-vergil@users.noreply.github.com`) — this
     goes in `vergil.toml` co-author entries.

5. **Fill out the profile.** New accounts with empty profiles are
   frequently shadow-banned by GitHub's automated systems. At
   minimum set:
   - Display name
   - Bio (make it clear this is an AI agent identity linked to
     your human account)
   - Link to your human GitHub profile in the bio or website field

## Step 2: Generate a classic PAT

The agent account uses a classic Personal Access Token. From the
`<username>-vergil` account:

1. Settings → Developer settings → Personal access tokens →
   Tokens (classic)
2. Generate new token with scopes:
   - `repo` (full repository access)
   - `read:org` (org membership visibility)
3. Set expiration (12 months recommended)
4. Copy the token immediately — it is shown only once

See the [credential management design][cred-spec] for the full
rationale on classic PATs vs. fine-grained PATs.

## Step 3: Log both accounts into `gh auth`

Both accounts must be logged in on your development machine.
The tooling uses `gh auth token -u <account>` to retrieve tokens
per-subprocess — it never calls `gh auth switch`.

```bash
# Human account (if not already logged in)
gh auth login -h github.com --web -p https

# Agent account — log into the -vergil browser session first,
# then authorize
gh auth login -h github.com --web -p https
```

!!! warning "`gh auth login` has no `-u` flag"
    You cannot specify which account to log in as. The `-u` flag
    does not exist for `gh auth login`. Instead, make sure you
    are logged into the correct GitHub account in your browser
    before authorizing the OAuth flow.

After both logins, switch back to your human account as the
active default:

```bash
gh auth switch -u <your-username>
```

Verify both are present:

```bash
gh auth status
# Should show both <username> and <username>-vergil

gh auth token -u <your-username>          # human PAT
gh auth token -u <your-username>-vergil   # agent PAT
```

## Step 4: Update `vergil.toml`

Each repo's `vergil.toml` needs a co-author entry for your agent
account. The noreply email comes from Step 1.4:

```toml
[project.co-authors]
<username>-vergil = "Co-Authored-By: <username>-vergil <<id>+<username>-vergil@users.noreply.github.com>"
```

This is how `vrg-commit --agent <username>-vergil` resolves the
co-author trailer.

## Step 5: Request collaborator access

The org owner invites `<username>-vergil` as an outside
collaborator on each managed org:

1. Navigate to the repo → Settings → Collaborators
2. Click "Add people"
3. Search for `<username>-vergil`, select it
4. Set role to **Write**
5. Send invitation

Accept the invitation from the `-vergil` account (via email or
the GitHub notifications page).

One invitation per repo. The agent account is an outside
collaborator by design — it does not become an org member.

## Shadow-banning

GitHub's automated systems frequently flag new accounts that have
no activity, no profile content, or that were created in rapid
succession. A shadow-banned account:

- Returns 404 on its profile page
- Can still authenticate and push code
- Cannot be invited as a collaborator (invitation silently fails)

**Prevention:**

- Fill out the profile completely before doing anything else
  (Step 1.5)
- Enable 2FA immediately
- Link to your human account in the bio
- Make a small commit (even to a personal test repo) to establish
  activity

**Recovery:**

If the account gets flagged, contact GitHub support at
support.github.com/contact. Explain that this is a secondary
account for AI-assisted development linked to your primary
account. Response time varies but is typically 1-3 business days.

## Verification checklist

After completing all steps, verify the full chain:

- [ ] `gh auth status` shows both accounts
- [ ] `gh auth token -u <username>` returns a token
- [ ] `gh auth token -u <username>-vergil` returns a token
- [ ] `vrg-gh issue list` succeeds (tests credential selection)
- [ ] `vrg-commit --help` succeeds (tests host tool install)
- [ ] The `-vergil` account profile page loads (not 404)
- [ ] The `-vergil` account has collaborator access on at least
      one managed repo

## Related

- [Consuming Repo Setup](consuming-repo-setup.md) — repo-level
  onboarding
- [Git Workflow](git-workflow.md) — how the per-change cycle
  works
- [Credential management design][cred-spec] — full rationale for
  the two-account model

[cred-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/2026-05-14-credential-management-design.md
