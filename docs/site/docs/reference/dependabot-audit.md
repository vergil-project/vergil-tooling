# Dependabot Alert Audit

`vrg-dependabot-audit` fails when a repository has any open Dependabot
alert. It is the gate for the fleet's report-only Dependabot posture
(epic vergil-project/.github#420). Dependabot alerts are on for every
managed repo, Dependabot never opens a pull request, and this command
turns an open alert into a failing run.

```bash
vrg-dependabot-audit --repo OWNER/REPO
```

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_dependabot_audit` |
| Args | `--repo OWNER/REPO` (required) |
| Preconditions | A GitHub token that can read the repo's Dependabot alerts (see [Required permission](#required-permission)) |
| Exit codes | 0 no open alerts, 1 at least one open alert, 2 could not check |
| Status | Active |

## The report-only posture

Two halves keep Dependabot to reporting:

- **Alerts are on.** `vrg-github-repo-config apply` enables
  vulnerability alerts on every repo, public or private, and the
  nightly config audit flags any repo where they are off.
- **Dependabot never opens a PR.** The repo setting for Dependabot
  security updates stays disabled, which the config audit also
  enforces. Version-update PRs are suppressed by the canonical
  `.github/dependabot.yml`, where every `updates` entry carries
  `open-pull-requests-limit: 0`. `vrg-validate` rejects any entry
  without it, and the nightly config audit's `local.dependabot` check
  flags a missing or non-canonical file. `vrg-dependabot-sync` writes
  the file for one repo, and `vrg-fleet-sync --target dependabot`
  sweeps it across the fleet. See the
  [GitHub Config Audit reference](config-audit.md) and the
  [CLI Tools Overview](cli-tools-overview.md#vrg-dependabot-sync).

## Where it runs

Every managed repo's nightly `ops.yml` calls the reusable
`vergil-project/vergil-actions/.github/workflows/ops-github-config.yml`
workflow. Its `dependabot-audit` job runs
`vrg-dependabot-audit --repo "${{ github.repository }}"`, so exit 1 or
exit 2 turns the scheduled run red, and GitHub's failed-workflow
notification is the alert.

The job cannot use `GITHUB_TOKEN`: a called workflow gets only what
the caller's `ops.yml` grants, which is `contents: read`. It mints a
token from the org's automation App (the `APP_CLIENT_ID` and
`APP_PRIVATE_KEY` secrets), scoped to the calling repo and carrying
only `vulnerability-alerts: read`. See
[Identity Architecture](../guides/identity-architecture.md#the-org-automation-apps).

The audit reads only alerts in the `open` state, across every page of
the API result, so a repo with hundreds of alerts is counted in full.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The repo has no open Dependabot alerts. |
| 1 | The repo has at least one open alert, at **any** severity. |
| 2 | The audit could not read the alerts. |

There is no severity threshold and no in-repo suppression. A low
severity alert fails the audit exactly as a critical one does.

Exit 2 is never a pass. The command prints one diagnostic line to
stderr, prefixed `vrg-dependabot-audit:`. The common causes are:

| Diagnostic | Cause |
|---|---|
| `Dependabot alerts are disabled for OWNER/REPO` | Alerts are off on the repo. Run `vrg-github-repo-config apply`. |
| `token lacks the 'Dependabot alerts: Read' permission for OWNER/REPO` | The token or GitHub App cannot read alerts (HTTP 403). |
| `repository OWNER/REPO not found or not accessible` | The repo does not exist or the token cannot see it (HTTP 404). |
| `the GitHub App has no installation for owner '…'` | No App installation for the repo's owner. |
| `could not read Dependabot alerts for OWNER/REPO: …` | Any other API failure; the first line of the error follows. |

## Output: public vs private repos

Every report starts with a totals line, then ends with a link to the
repo's Security tab:

```text
o/r: 2 open Dependabot alerts: 1 critical, 0 high, 0 medium, 1 low
Review or dismiss at https://github.com/o/r/security/dependabot
```

A severity GitHub adds beyond the four known ones is still counted,
and it is listed after `low`.

- **Private repos** also get one line per alert, ordered by severity
  and then package. Each line carries the severity, ecosystem, package,
  manifest path, GHSA ID, and the alert's URL.
- **Public repos** get the totals line and the link only. Package
  names, manifest paths, and GHSA IDs never appear, because the log of
  a public repo's workflow run is world-readable. An internal repo is
  treated like a public one.

If the repo's visibility cannot be resolved, the command warns on
stderr and uses the public, counts-only format. It never itemizes a
repo it has not confirmed is private.

## Clearing an alert

Dismissal on GitHub is the only escape hatch. Fix the dependency, or
dismiss the alert from the Security tab with a reason. Because the
audit reads only `open` alerts, a fixed or dismissed alert no longer
fails it. There is no file or setting in the repo that suppresses an
alert.

## Required permission

The token needs read access to Dependabot alerts:

- **GitHub App:** the **Dependabot alerts: Read** repository
  permission, and each installation must accept the updated
  permission.
- **Personal access token:** the fine-grained **Dependabot alerts:
  Read** permission, or the classic `security_events` scope (or
  `public_repo` for a public repo).

As with `vrg-github-repo-config`, when the command mints its own GitHub
App token it mints it for the audited repo's owner, not for the
current directory's remote.
