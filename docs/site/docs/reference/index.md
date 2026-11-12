# Script Reference

Vergil-tooling provides Python CLI tools installed as `vrg-*` console
scripts, plus git hooks. For the full audit of every tool's runtime
preconditions, host-vs-container classification, and failure modes,
see the [CLI Tools Overview](cli-tools-overview.md). For how the
package itself is laid out, see [Package Architecture](package-architecture.md).

## Host tools

Run on the developer's machine. Installed via `uv tool install` or
the dev-tree override venv.

| Tool | Purpose |
| ---- | ------- |
| [vrg-commit](dev/commit.md) | Standards-compliant commit wrapper |
| [vrg-reword](cli-tools-overview.md#vrg-reword) | Reword a branch-local commit message (agent-safe) |
| [vrg-git](cli-tools-overview.md#vrg-git) | Policy-enforcing `git` wrapper for agent sessions |
| [vrg-gh](cli-tools-overview.md#vrg-gh) | Identity-aware `gh` wrapper with a per-identity allowlist |
| [vrg-whoami](cli-tools-overview.md#vrg-whoami) | Print the resolved identity role or platform |
| [vrg-hook-guard](cli-tools-overview.md#vrg-hook-guard) | Claude Code hook that blocks raw `git` and `gh` |
| [vrg-trusted-authors](cli-tools-overview.md#vrg-trusted-authors) | Print the GitHub logins whose issues and comments automation may trust |
| [vrg-resolve-tracking-issue](cli-tools-overview.md#vrg-resolve-tracking-issue) | Print a merge commit's tracking issue |
| [vrg-vm](cli-tools-overview.md#vrg-vm) | Manage the identity agent VMs |
| [vrg-vm-resolve-session](cli-tools-overview.md#vrg-vm-resolve-session) | Pick and start a Claude Code session inside an identity VM (run by `vrg-vm session`) |
| [vrg-submit-pr](dev/submit-pr.md) | Standards-compliant PR submission wrapper |
| [vrg-pr-workflow](cli-tools-overview.md#vrg-pr-workflow) | Record and manage the PR handoff state that `vrg-submit-pr` reads |
| [vrg-pr-await](cli-tools-overview.md#vrg-pr-await) | Block until a PR's checks or reviews settle, then print its state |
| [vrg-await](cli-tools-overview.md#vrg-await) | Block until a `.vergil/` channel file appears or changes |
| [vrg-audit-approve](cli-tools-overview.md#vrg-audit-approve) | Post the `vergil-audit/approved` check-run on a PR's head commit |
| [vrg-release](dev/release.md) | Run the full release workflow, ending with the dependency update |
| [vrg-promote](cli-tools-overview.md#vrg-promote) | Move the rolling `vX.Y` tag to a release (human-only) |
| [vrg-version](cli-tools-overview.md#vrg-version) | Print or bump the repo's version |
| [vrg-update-deps](dev/update-deps.md) | Update dependencies within their declared bounds and merge the result |
| [vrg-finalize-pr](dev/finalize-pr.md) | Merge a PR and run post-merge cleanup |
| [vrg-ruling](dev/ruling.md) | The agent's side of an authenticated ruling: post a request, draft the reply, read it, post the response; also draft a scope change |
| [vrg-ruling-reply](dev/ruling-reply.md) | Post the human's reply to a ruling request, as the human (human-only) |
| [vrg-scope-change](dev/scope-change.md) | Post a scope change on a task issue, as the human (human-only) |
| [vrg-merge-review-request](cli-tools-overview.md#vrg-merge-review-request) | Build a merge-review request from a clean, fully validated branch |
| [vrg-merge-review-check](cli-tools-overview.md#vrg-merge-review-check) | Check a stored merge-review result against the request it answers |
| [vrg-change-class](cli-tools-overview.md#vrg-change-class) | Print a branch's merge-review change class: `docs-only` or `code` |
| [vrg-ensure-label](cli-tools-overview.md#vrg-ensure-label) | Ensure GitHub labels exist |
| [vrg-github-repo-config](config-audit.md) | Audit/reconcile a repo against canonical config |
| [vrg-dependabot-audit](dependabot-audit.md) | Fail on any open Dependabot alert |
| [vrg-github-repo-init](cli-tools-overview.md#vrg-github-repo-init) | Bootstrap a new managed repository |
| [vrg-gitignore-sync](cli-tools-overview.md#vrg-gitignore-sync) | Check or write a repo's vergil-managed `.gitignore` block |
| [vrg-dependabot-sync](cli-tools-overview.md#vrg-dependabot-sync) | Check or write a repo's canonical report-only `dependabot.yml` |
| [vrg-claude-settings-sync](cli-tools-overview.md#vrg-claude-settings-sync) | Check or write a repo's managed `.claude/settings.json` |
| [vrg-claude-md-sync](cli-tools-overview.md#vrg-claude-md-sync) | Check or write the template region of a repo's `CLAUDE.md` |
| [vrg-hook-guard-shim-sync](cli-tools-overview.md#vrg-hook-guard-shim-sync) | Check or write a repo's managed `.claude/hooks/guard.sh` |
| [vrg-fleet-sync](cli-tools-overview.md#vrg-fleet-sync) | Propagate a managed file across the fleet, one PR per drifted repo |
| [vrg-ci-sync](cli-tools-overview.md#vrg-ci-sync) | Fleet sweep that strips retired inputs from each repo's `ci.yml` |
| [vrg-upstream-refresh](cli-tools-overview.md#vrg-upstream-refresh) | Run the repo's upstream-refresh hook and open, refresh or close its PR |
| [vrg-branch-sweep](dev/branch-sweep.md) | Report, or with `--apply` delete, a repo's finished remote branches |
| [vrg-repo-status](cli-tools-overview.md#vrg-repo-status) | Show every worktree in the current repo, grouped by kind |
| [vrg-container-run](cli-tools-overview.md#vrg-container-run) | Run commands inside a dev container |
| [vrg-container-docs](cli-tools-overview.md#vrg-container-docs) | Preview/build MkDocs in a dev container |
| [vrg-container-build-command](cli-tools-overview.md#vrg-container-build-command) | Print the declared `[container].build-command` (CI consumption) |
| [vrg-container-system-packages](cli-tools-overview.md#vrg-container-system-packages) | Print the declared `[container].system-packages` (CI consumption) |
| [vrg-container-cache](cli-tools-overview.md#vrg-container-cache) | Manage the per-branch cached dev image |
| [vrg-scorecard](cli-tools-overview.md#vrg-scorecard) | Run OpenSSF Scorecard in a dev container with a GitHub App token |
| [vrg-generate-commands](cli-tools-overview.md#vrg-generate-commands) | Generate MQSC command methods |
| [vrg-host](dev/host.md) | Report, upgrade and pin the macOS host dependencies |
| [vrg-issue-create](cli-tools-overview.md#vrg-issue-create) | Create an issue and link it under an epic in one step |
| [vrg-epic-create](cli-tools-overview.md#vrg-epic-create) | Create a top-level epic in its home repo |
| [vrg-epic-docs](cli-tools-overview.md#vrg-epic-docs) | Write an epic's Documents section |
| [vrg-epic-link](cli-tools-overview.md#vrg-epic-link) | Link a task under an epic as a native sub-issue |
| [vrg-epic-move](cli-tools-overview.md#vrg-epic-move) | Move a task under a different epic |
| [vrg-epic-rollup](cli-tools-overview.md#vrg-epic-rollup) | Close a finite epic once all its tasks are closed |
| [vrg-adhoc-epic](cli-tools-overview.md#vrg-adhoc-epic) | Ensure, archive or normalize a repo's ad-hoc epic |
| [vrg-epic-audit](cli-tools-overview.md#vrg-epic-audit) | Report epic/task drift and invariant violations for an org |
| [vrg-intake-create](cli-tools-overview.md#vrg-intake-create) | Capture an uncurated item into the intake queue |
| [vrg-intake-route](cli-tools-overview.md#vrg-intake-route) | Route an item between the intake, planning, ad-hoc and deferred epics |
| [vrg-security-record](dev/security-record.md) | Record a security finding as a private draft advisory with a neutral intake issue |
| [vrg-queue](cli-tools-overview.md#vrg-queue) | Read-only view of a standing queue |
| [vrg-roadmap](cli-tools-overview.md#vrg-roadmap) | Print the generated project roadmap |
| [vrg-activity-log](cli-tools-overview.md#vrg-activity-log) | Print the project activity log of recently closed issues |

## Container tools

Run inside dev containers launched by `vrg-container-run`.

| Tool | Purpose |
| ---- | ------- |
| [vrg-validate](cli-tools-overview.md#vrg-validate) | Unified validation driver (common + language-specific checks) |
| [repo-profile](lint/repo-profile.md) | Repository profile attribute validation (a `vrg-validate` common check, not a command) |
| [vrg-package](cli-tools-overview.md#vrg-package) | Build, install-test and index binary OS packages from `[package]` |
| [vrg-ci-resolve](cli-tools-overview.md#vrg-ci-resolve) | Resolve the repo's CI versions from the published version window |
| [vrg-sarif-filter](cli-tools-overview.md#vrg-sarif-filter) | Drop accepted-suppressed results from a SARIF file before upload |
| [vrg-sarif-evaluate](cli-tools-overview.md#vrg-sarif-evaluate) | Report a SARIF file's findings at the chosen severities |
| [vrg-semgrep-scan](cli-tools-overview.md#vrg-semgrep-scan) | Run a semgrep scan with language-resolved rulesets and write SARIF |
| [vrg-ecosystem-resolve](cli-tools-overview.md#vrg-ecosystem-resolve) | Resolve a language's package build and publish commands |
| [vrg-release-validate-inputs](cli-tools-overview.md#vrg-release-validate-inputs) | Validate release workflow inputs |
| [vrg-version-divergence](cli-tools-overview.md#vrg-version-divergence) | Fail when the head branch's version was not bumped |
| [vrg-freeze-refs](cli-tools-overview.md#vrg-freeze-refs) | Pin internal action references in workflow YAML to a release tag |
| [vrg-ci-evidence](cli-tools-overview.md#vrg-ci-evidence) | Harvest and bundle a release's CI evidence |
| [vrg-docs-stage](cli-tools-overview.md#vrg-docs-stage) | Stage the changelog and release notes into the docs build |
| [vrg-docs-patch-nav](cli-tools-overview.md#vrg-docs-patch-nav) | Add the release version pages to the `mkdocs.yml` nav |
| [Markdown validation](lint/markdown-standards.md) | Markdownlint with bundled canonical config |

## Claude Code Hooks

| Hook | Purpose |
| ---- | ------- |
| [Claude Code Hook Guard](hooks/pre-commit.md) | `PreToolUse` guard that blocks raw `git` and `gh`; also documents the `vrg-commit` branch checks |
