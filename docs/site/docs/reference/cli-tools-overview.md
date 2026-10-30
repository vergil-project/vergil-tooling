# CLI Tools Overview

Every CLI command provided by this package, organized by runtime
context. Each entry documents the tool's purpose, where it runs,
what it assumes, and how it fails when those assumptions are violated.

## Host tools

Host tools run on the developer's machine (outside any container).
They drive git, `gh`, SSH, and Docker operations. Installed via
`uv tool install` or the dev-tree override venv.

### vrg-commit

Construct standards-compliant conventional commits (no AI co-author
trailer; see #3200). Performs five branch/context checks before committing
(detached HEAD, protected branch, branch prefix, issue number,
worktree convention).
Local-settings guard: refuses an agent (warns a human) while a loadable
`.claude/settings.local.json` is not absent or `{}` (epic vergil-project/.github#558).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_commit` |
| Args | `--type` (required), `--scope` (required), `--message` (required), `--body`, `--allow-empty` |
| Preconditions | Git repo, staged changes, not detached HEAD, not on protected branch, branch prefix matches branching model, issue number in branch name, not main worktree when `.worktrees/` present |
| Failure mode | `SystemExit` with diagnostic on stderr for each check |
| Exit codes | 0 success, 1 rejection or error |
| Status | Active |

### vrg-reword

Reword a branch-local commit's message through a scripted, non-interactive
rebase, re-stamping the new message through the same standards path as
`vrg-commit`. It is the agent-safe way to correct a commit message on the
current branch: `vrg-git commit` is denied, `vrg-commit` only creates new
commits, and raw interactive rebase is blocked repo-wide. It is bounded:

- **Branch-local only**: it refuses a commit already reachable from the
  base branch (`origin/develop`, or `origin/main` for a `release/*` branch)
  and refuses to run on `develop`, `main` or a `release/*` branch, so it
  never rewrites shared or merged history.
- **Own identity only**: it refuses a commit whose author email differs
  from the current git identity unless `--allow-foreign-author` is passed.
- **Force-with-lease**: when the branch is already on origin, the rewrite is
  pushed with `--force-with-lease`, never a bare `--force`. `--no-push`
  skips the push and prints the command to run.

Rewording a mid-chain commit re-applies the commits after it, which changes
their committer (the author is kept), so the tool first prints a note on
stderr naming the affected commits. A body carrying a GitHub auto-close keyword is refused,
as in `vrg-commit`.

It also refuses a branch that has been reported ready, because that branch is
frozen; `vrg-pr-workflow unfreeze` is the way back (see the freeze in
[`vrg-pr-workflow`](#vrg-pr-workflow)).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_reword` |
| Args | `sha` (positional, required), `--type` (required), `--scope` (required), `--message` (required), `--body`, `--allow-foreign-author`, `--no-push` |
| Preconditions | Git repo on a feature branch, clean working tree, the base branch resolvable, `user.email` set (unless `--allow-foreign-author`) |
| Failure mode | Each refusal prints the reason and a hint on stderr; a rebase that does not complete says to run `git rebase --abort`; a refused lease push says to fetch and review before retrying |
| Exit codes | 0 success, 1 refusal or error; otherwise the rebase's exit code |
| Status | Active |

### vrg-git

A safe git wrapper for agent sessions. It forwards to `git` only after
enforcing policy: a subcommand allowlist, read-only forms of `reflog` and
`symbolic-ref`, denied subcommands (`commit`, `config`, `reset`, `clean` and
others; use `vrg-commit` for commits), a flag deny-list (`push --force`,
`branch -D`, `rebase -i`), the worktree convention, the
`<type>/<N>-<slug>` branch form for an open, non-epic issue, and the
report-ready freeze. Remote operations run with the GitHub App installation
token for the resolved identity (see [`vrg-whoami`](#vrg-whoami)). It is not
a drop-in replacement for `git`; `vrg-git --help` lists the full policy.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_git` |
| Args | `<subcommand> [args...]`, forwarded to `git` |
| Preconditions | Git on PATH |
| Failure mode | A refused command prints the reason on stderr |
| Exit codes | 0 success; 1 refusal; 2 no subcommand; otherwise git's exit code |
| Status | Active |

### vrg-gh

A safe, identity-aware `gh` wrapper for agent sessions. It forwards to `gh`
only after enforcing a per-identity subcommand allowlist, injecting the GitHub
App installation token for the target owner. Raw `gh issue create`, `gh auth`,
`pr close`, `repo edit/create/delete` and `workflow run` are denied outright;
`gh api` is full for a human, read-only `GET` for the audit identity, and
denied for the user identity. `vrg-gh --help` lists the allowlist.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_gh` |
| Args | `<subcommand> <action> [args...]`, forwarded to `gh` |
| Preconditions | `gh` on PATH; identity resolved via `vrg-whoami` |
| Failure mode | A refused command prints the reason on stderr |
| Exit codes | 0 success; 1 refusal; 2 no subcommand; otherwise gh's exit code |
| Status | Active |

### vrg-submit-pr

Create standards-compliant pull requests. Template mode pushes the
current branch and opens a PR from `.vergil/pr-workflow.json`. **Relay
branch mode** (positional `<branch> [<branch> …]`) opens PRs
**worktree-free** for branches already on origin — the Mac side of the
cloud→Mac handoff: each branch's ready-state comes from a local
worktree's `pr-workflow.json` when present, else the relay ref
`refs/vergil/pr-workflow/<branch>`; origin's tip is checked against the
recorded `head_sha` and the PR opens with `--head` **without pushing**.
It refuses an issue whose native parent is an intake, planning or deferred
epic (see [`vrg-pr-workflow`](#vrg-pr-workflow)). Human-run: agent
identities are refused, and agents hand off with
`vrg-pr-workflow report-ready` instead. PRs merge manually.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_submit_pr` |
| Args | positional `branches` (relay mode); or `--issue` (required), `--summary` (required), `--linkage` (default: Ref; `vrg-submit-pr` auto-selects `Closes` for a managed task — `Fixes`/`Resolves` are banned), `--notes`, `--title`, `--dry-run` |
| Preconditions | Git repo, `gh` CLI on PATH; relay mode: named branches on origin with a resolvable ready-state and matching `head_sha` |
| Failure mode | Subprocess error from `git push` or `gh pr create`; relay mode: loud failure on a missing ready-state or `head_sha` drift |
| Exit codes | 0 success |
| Status | Active |

### vrg-pr-fix-body

Repair a PR's body by regenerating it from corrected fields through
the same validated builder as `vrg-submit-pr`, then push an empty
commit to re-trigger CI. The agent-safe fix path for body-level
standards failures during the pr-watch reconcile loop (denied to the
`audit` identity; the `user` identity may only fix its own PR).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_pr_fix_body` |
| Args | `pr` (positional, required), `--issue` (required), `--summary` (required), `--linkage` (default: Ref), `--notes`, `--dry-run`, `--no-retrigger` |
| Preconditions | Git repo, `gh` CLI on PATH, PR open, agent identities on the PR's head branch |
| Failure mode | `SystemExit`/diagnostic on identity, scope, or state rejection |
| Exit codes | 0 success, 1 rejection or error |
| Status | Active |

### vrg-pr-workflow

Record and manage the PR handoff state in `.vergil/pr-workflow.json` —
the oracle `vrg-submit-pr` reads. `report-ready` writes the PR metadata
(`--issue/--title/--summary/--notes`, optional `--linkage`) and marks the
branch **ready**; it is idempotent — re-running overwrites the recorded
prose (correcting the title/summary/notes is always allowed).
`report-ready` **always** also mirrors the ready-state onto the reserved
relay ref `refs/vergil/pr-workflow/<branch>` (unconditional — no config,
no off-platform detection), so a cloud VM's report-ready reaches the Mac
that later runs `vrg-submit-pr <branch>` even though the two never share a
disk. The relay push is a pure ref write built with git plumbing; it never
advances the branch, so it stays **freeze-neutral**. A push failure is
loud on stderr but never rolls back the durable local file. Because the
ref is world-readable on a public repo, keep secrets out of
`--title/--summary/--notes`. Reaching **ready** *freezes* the branch:
`vrg-commit`, `vrg-reword` and the `vrg-git` push path then refuse further
commits, commit rewrites and pushes, because a task is exactly one PR and more work is a new
follow-up issue (epic #146). `report-ready` pushes the branch itself, to
`origin/<branch>` with upstream set, **before** recording anything (#3602),
so no separate push is needed; a branch already on origin at that commit is a
no-op push. When the delta touches `.github/workflows/**` GitHub refuses the
agent push: `report-ready` reports that as expected, records the state anyway,
and the human's `vrg-submit-pr` pushes the branch. Any other push failure
fails `report-ready` with no local state and no relay ref written. A re-run on
a frozen branch whose HEAD has drifted past the reported commit is refused
before any push. After a deliberate `unfreeze`, a re-run pushes with
`--force-with-lease=refs/heads/<branch>:<recorded head_sha>`, so a rebased
branch replaces the reported commit with no manual force-push, while an origin
branch that moved anywhere else is refused with nothing recorded (#3783); a
first report, a frozen re-run, or a branch not yet on origin keeps the plain
push. An explicit `vrg-git push [-u] origin <branch>` also stays
allowed while HEAD is exactly the reported commit — it publishes, not
advances, the branch (issue #3071). `unfreeze` is the only sanctioned way to
reopen a still-unsubmitted branch — it drops the state back to
`implementing` while keeping the metadata; an already-submitted branch
cannot be unfrozen. `status` prints the current state. `report-ready` (like
`vrg-submit-pr`) refuses an issue whose native parent is an intake, planning
or deferred epic: a queue item is not buildable until intake review routes it,
`epic-create` expands it, or deferred review revives it (epics
vergil-project/.github#176 and #508).
Local-settings guard: `report-ready` refuses an agent (warns a human), before
pushing or recording anything, while a loadable `.claude/settings.local.json`
is not absent or `{}` (epic vergil-project/.github#558).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_pr_workflow` |
| Args | `report-ready` (`--issue`, `--title`, `--summary`, `--notes` required; `--linkage` default Ref), `unfreeze`, `status`; global `--base` (default: origin/develop) |
| Preconditions | Git repo; run from the worktree whose branch is being staged |
| Failure mode | `WorkflowError` on stderr (e.g. unfreeze after submit, or nothing to unfreeze) |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-pr-await

Block until a pull request settles, then print its state as JSON. It settles
when every check reaches a terminal conclusion, the head SHA moves, a new
review appears, or the PR is wedged by an orphaned check-run (grace period
`$VRG_ORPHAN_GRACE`, default 300s). `--since-sha` and `--since-reviews` carry
the caller's last-seen baseline. Used by the `pr-watch` skill; it blocks
indefinitely by design.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_pr_await` |
| Args | `pr` (positional, URL or number), `--since-sha`, `--since-reviews` |
| Preconditions | GitHub App credentials for the PR's org |
| Failure mode | Exits 1 with an error if the PR is already merged or the PR cannot be read |
| Exit codes | 0 settled, 1 error |
| Status | Active |

### vrg-wait-until-green

Block until a PR's required checks pass and its branch is up to date. When
the branch is behind its base it updates it and re-polls CI. The wait is
bounded by `$VRG_CHECKS_TIMEOUT` (default 1800s).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_wait_until_green` |
| Args | `pr` (positional, URL or number) |
| Preconditions | GitHub App credentials for the PR's org |
| Failure mode | Exits 1 on merge conflicts, failed checks (named on stderr), a branch still behind after repeated updates, or a merge state other than `CLEAN` |
| Exit codes | 0 every check passed and the PR is mergeable, 1 otherwise |
| Status | Active |

### vrg-await

Block until a `.vergil/` channel file appears or changes, then print its
SHA-256. Without `--since` it waits for the file to exist; with `--since` it
waits until the file's digest differs from the given one. It blocks
indefinitely by design.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_await` |
| Args | `path` (positional), `--since SHA256` |
| Preconditions | None |
| Failure mode | None; it waits until the condition holds |
| Exit codes | 0 |
| Status | Active |

### vrg-audit-approve

Post the `vergil-audit/approved` check-run on a PR's head commit, the check
that branch protection requires before a merge. Run by the audit identity;
it refuses the user identity. A new push invalidates the check until it is
posted again.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_audit_approve` |
| Args | `pr` (positional, URL or number), `--conclusion {success,failure,action_required,neutral}` (default `success`), `--summary` |
| Preconditions | Run in the PR's repo as the audit identity, with the audit App's credentials |
| Failure mode | Refuses (exit 1) under the user identity |
| Exit codes | 0 posted, 1 refused or error |
| Status | Active |

### vrg-release

Run the full release workflow from `develop` to `main`: tracking issue,
changelog, release PR and merge, CD confirmation, back-merge and version
bump, rolling-tag promotion, cleanup, consumer refresh, and finally the
`update-deps` stage, which runs the dependency update on the new
`develop` version. On a fresh start, preflight refuses while an open PR
carries the `dependency-update` label, after first closing any whose fix
task completed. Human-only. See the [vrg-release reference](dev/release.md)
and the [Releasing guide](../guides/releasing.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_release` |
| Args | `minor`/`major` (optional positional), `--no-promote`, `--without-deps`, `--allow-pending-deps`, `--install`, `--resume [X.Y.Z]`, `--skip-audit`, `--output-window`, `--output-format` |
| Preconditions | Human identity; on `develop`, clean working tree, local develop matches `origin/develop`; `gh` authenticated and `git-cliff` on PATH |
| Failure mode | Fail-fast stages stop the run; deferred stages (including `update-deps`) record the failure, let later stages run, and are summarized at the end |
| Exit codes | 0 success, 1 any failure or refusal, 130 interrupted |
| Status | Active |

### vrg-changelog

Generate `CHANGELOG.md` and the release notes (`releases/vX.Y.Z.md`) for the
version in `VERSION`, using `git-cliff`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_changelog` |
| Args | `--changelog-only` or `--notes-only` (default: both) |
| Preconditions | Run from the repo root; a `VERSION` file; `git-cliff` on PATH |
| Failure mode | Exits 1 when no `VERSION` file is found |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-promote

Move the rolling `vX.Y` tag to release `vX.Y.Z`. It is human-only: it
refuses any agent identity, failing closed on any agent signal
(`is_agent() or any_agent_signal()`), before any output or git call.
`vrg-release`'s promote stage uses the same library and the same refusal.

The target must be an existing `vX.Y.Z` tag on origin whose commit is on
origin's `main` and that has a published (non-draft) GitHub Release; any
other target is refused. origin's push URL must name the same GitHub repo as
its fetch URL. Every run, `--dry-run` included, first runs
`git fetch --tags --force origin +refs/heads/main:refs/remotes/origin/main`,
so the checks run against origin's current state; like the rollback
runbook's own fetch, this overwrites local tags with origin's.

It prints the origin repo and the tag move, for example:

```text
origin: vergil-project/vergil-claude-plugin
v2.1 1a2b3c4d5e6f -> v2.1.50 fc17882a9b0c
```

`--dry-run` prints the same lines, after the same target check, and stops.
Otherwise it asks `Move vX.Y on <repo>? [y/N]` on the terminal, and moves
the tag only on `y` or `yes`. With no terminal to confirm on, it refuses.
Before it pushes it re-resolves the move and refuses if anything changed
since it was confirmed, and checks that the local release tag is the
confirmed commit. It then pushes that release tag to the rolling tag's ref
as a compare-and-swap (`--force-with-lease` on the rolling tag's value on
origin, empty when the tag is new), so it overwrites exactly the tag that
was shown, or is refused. The local rolling tag is updated only after origin
accepts the move. When the rolling tag already points at the release, it
prints only the "already promoted" line, asks nothing and pushes nothing.

When `vrg-release`'s promote stage calls it, the GitHub Release read retries
a 404 for a short budget, since that Release was just published. A version
given to `vrg-promote` is read once, so a missing Release is refused at once.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_promote` |
| Args | `version` (positional, default: the `VERSION` file), `--dry-run` |
| Preconditions | Human identity; run in the repo's checkout; a terminal to confirm on; push access to origin |
| Failure mode | Exit 1 with a message on stderr. An agent identity, a run with no terminal, a declined or unanswered confirmation, and a missing `VERSION` file each print their own `vrg-promote:` or plain message; a refused target, a move that changed since it was confirmed, and a failed git or GitHub call print `Error: …` |
| Exit codes | 0 success (including "already promoted" and `--dry-run`), 1 refusal or error |
| Status | Active |

### vrg-version

Print or bump the repo's version. `show` prints the current version
(`--major-minor` for `X.Y` only, `--ref` to read it from a git ref); `bump`
increments `patch` (default), `minor` or `major` and prints the new version.
Used by the CD workflows to read the release version.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_version` |
| Args | `show [--major-minor] [--ref REF]`, `bump [{patch,minor,major}]` |
| Preconditions | Run from the repo root |
| Failure mode | An unreadable version raises an uncaught error |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-resolve-tracking-issue

Print the tracking issue number of the PR that produced a commit, read
from the PR body's `Ref #N` linkage. The PR is found from the commit
subject (the merge-commit `Merge pull request #N from …` pattern, then the
squash-merge `… (#N)` pattern), then from the GitHub API's
commit-to-pulls lookup; `--pr` skips the lookup. Used by the
`version-bump-pr` composite action in vergil-actions.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_resolve_tracking_issue` (patterns: `lib/linkage.py`) |
| Args | `--commit` (default: HEAD), `--pr N` |
| Preconditions | Git repo; `gh` authenticated for the current repo |
| Failure mode | `ERROR:` on stderr naming what was tried; a PR with no body or no `Ref #N` linkage is an error |
| Exit codes | 0 success (issue number on stdout), 1 no PR found or no linkage, 2 git or GitHub API failure |
| Status | Active |

### vrg-update-deps

Update the repo's dependencies to the newest versions their declared
bounds allow, in a `deps/update-<version>` managed worktree: run the
applicable updaters (`python`, `vergil`, `images`, then the repo
`hook`), validate once, open a `dependency-update`-labelled PR, and
merge it. A no-op run opens no PR. A validation or merge failure leaves
the PR open and files a fix task; an updater or hook failure opens no PR
but still files a fix task. Every run prints the held-back report.
`vrg-release` runs the same pipeline as its `update-deps` stage.
Human-only. See the [vrg-update-deps reference](dev/update-deps.md) and
the [dependency-update reference](dependency-update-config.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_update_deps` |
| Args | `--only NAME[,NAME]`, `--skip NAME[,NAME]`, `--vergil X.Y` (mutually exclusive), `--output-window`, `--output-format` |
| Preconditions | Human identity; on `develop`, clean working tree, local develop matches `origin/develop`; `gh` authenticated; no existing `deps/update-<version>` branch |
| Failure mode | Updater, validation and merge failures are recorded, the worktree is cleaned up, then the run exits non-zero |
| Exit codes | 0 success or no-op, 1 failure or refusal, 130 interrupted |
| Status | Active |

### vrg-upstream-refresh

Run the repo's `[upstream-refresh].hook` on a clean checkout of the
base branch, then open, refresh or close the single `upstream-refresh`
PR. A no-change run closes any stale refresh PR, or exits quietly when
there is none. It never merges, and never runs dependency updaters,
validation or a release. It runs from the scheduled
`ops-upstream-refresh` workflow under the App token, or by hand as a
human; it refuses agent identities. See the
[upstream-refresh reference](upstream-refresh-config.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_upstream_refresh` |
| Args | `--repo OWNER/NAME` (default: `$GITHUB_REPOSITORY`, then origin), `--base BRANCH` (default: the repo's default branch), `--dry-run` |
| Preconditions | Not an agent identity; `[upstream-refresh]` in `vergil.toml`; clean working tree with `HEAD` at `origin/<base>`; `gh` authenticated with contents and pull-requests write |
| Failure mode | A hook failure or untracked files left by the hook stop the run before any push; errors on stderr |
| Exit codes | 0 any outcome (`nothing`, `closed`, `opened`, `refreshed`, `unchanged`), 1 error or refusal |
| Status | Active |

### vrg-finalize-pr

Finalize a pull request. Given a PR, runs the provenance check, merges
it, then cleans up; with no PR, runs cleanup only. Cleanup switches to
the target branch, fast-forward pulls, deletes merged local branches
(auto-removing worktrees inside `.worktrees/` when necessary), deletes
each merged branch's PR-workflow relay ref
(`refs/vergil/pr-workflow/<branch>`) and sweeps any orphaned relay ref
whose branch is gone (issue #2369), prunes remotes, runs validation, and
checks the CD workflow status. It also removes each merged branch's
cached dev images and sweeps cached images whose branch is live in no
repo of the platform repo set, as `vrg-container-cache prune` decides it
(skipped on the physical host, which has no default repo set); a failed
image removal is reported as a warning, never a failed finalize
(issue #3566). A
merged/closed worktree the sweep cannot remove — a dirty tree, or a
reused branch name with unmerged commits (issue #1719) — is surfaced
**prominently after the pipeline** rather than buried in the stage log;
`--clean-dirty` opt-in clears one whose only dirt is untracked
build/validation output, after showing the paths and confirming
(issue #2348).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_finalize_pr` |
| Args | `PR` (optional), `--target-branch` (default: develop), `--strategy` (default: squash), `--allow-provenance-violation`, `--clean-dirty`, `--dry-run` |
| Preconditions | Must run from the main worktree, `vrg-container-run` on PATH |
| Failure mode | Provenance violations, a dirty working tree, validation failures, or a failed CD run all return exit 1 |
| Exit codes | 0 success, 1 provenance/worktree/validation/CD failure or unrecognized branching model |
| Status | Active |

### vrg-branch-sweep

Report, or with `--apply` delete, a repo's finished remote branches: the
newest PR is merged or closed, none is open, and the branch tip still equals
that PR's head. The safety net behind the delete-on-merge repo setting and
the delete-on-close rule (issue #3332). Never deletes the default branch,
`develop`, `main`, `gh-pages`, a protected branch (branch protection or any
ruleset rule), a branch with no PR, or a branch whose name was reused
(issue #1719). See [vrg-branch-sweep](dev/branch-sweep.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_branch_sweep` |
| Args | `--repo OWNER/NAME` (default: the current repo), `--apply` (default: dry run) |
| Preconditions | `gh` authenticated with read access (delete access for `--apply`) to the target repo |
| Failure mode | A per-branch `gh` error is reported on that branch's line, the rest are processed, then exit 1 |
| Exit codes | 0 success, 1 any branch error, listing failure or auth failure, 2 bad arguments |
| Status | Active |

### vrg-issue-create

Create an issue and link it under an epic (a native sub-issue) in one step.
It is the sanctioned issue-creation path: `vrg-gh` denies raw
`gh issue create`. `--epic` takes `adhoc`, `intake`, `planning`, `#N` (read
against the target repo's epic home) or `owner/repo#N`. `--kind` stamps an
operational (`validation`, `deployment`) or `retrospective` label and body
scaffold, and `--blocked-by` stamps a `Blocked-by:` reference. If the link
fails after creation, the created issue's URL is reported for recovery with
`vrg-epic-move`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_issue_create` |
| Args | `--epic` (required), `--title` (required), `--body` / `--body-file`, `--label` (repeatable), `--assignee` (repeatable), `--repo OWNER/NAME`, `--kind {task,validation,deployment,retrospective}`, `--blocked-by REF` (repeatable) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Refusals and a failed link are reported on stderr; a failed link names the created issue |
| Exit codes | 0 created and linked, 1 error |
| Status | Active |

### vrg-epic-create

Create a top-level epic (labelled `epic`, no parent) in its home repo:
`<org>/.github` for a public target, the target repo itself when private.
The sanctioned path for creating epics, used by the `epic-create` and
`migrate-repo` skills.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_create` |
| Args | `--title` (required), `--body` / `--body-file`, `--repo OWNER/NAME`, `--label` (repeatable), `--assignee` (repeatable) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Error on stderr |
| Exit codes | 0 created, 1 error |
| Status | Active |

### vrg-epic-link

Link a task under an epic as a native GitHub sub-issue, reopening a closed
epic first. Used when creating a task under an epic and to backfill
reflink-only children. A bare `#N` epic ref resolves to the task's epic home.
A public task may not link under a private epic.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_link` |
| Args | `--epic` (required), `--task` (required) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Refusals and API errors on stderr |
| Exit codes | 0 linked, 1 error |
| Status | Active |

### vrg-epic-move

Move a task under a different epic (unlink, then link). `--epic` accepts
`adhoc`, `intake`, `planning`, `#N` or `owner/repo#N`, read against the
task's repo. Moving a task already under the target is a no-op. The deferred
queue is entered and left only through `vrg-intake-route`, so this refuses
it.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_move` |
| Args | `--task` (required), `--epic` (required) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Every refusal runs before any write and is reported on stderr |
| Exit codes | 0 moved or already there, 1 error |
| Status | Active |

### vrg-epic-unlink

Remove a task's native sub-issue link to its epic. A task with no parent is
a no-op.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_unlink` |
| Args | `--task` (required) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Error on stderr |
| Exit codes | 0 unlinked or no parent, 1 error |
| Status | Active |

### vrg-epic-rollup

Close a just-closed task's parent epic when the epic is finite and all its
tasks are closed. A no-op for any other issue (an `ad-hoc` epic never
auto-closes), so it is safe on every issue close. Run by the `epic-rollup`
workflow on `issues.closed`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_rollup` |
| Args | `--task` (required) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Error on stderr |
| Exit codes | 0 success or no-op, 1 error |
| Status | Active |

### vrg-adhoc-epic

Manage a repo's ad-hoc epic, `Epic (ad hoc): <repo>`. `ensure` creates it if
missing (idempotent). `archive` drains closed children into per-quarter
`Archive (ad hoc): <repo> — YYYY-Qn` issues, and `normalize` converts legacy
quarter-named epics to that form; both are dry runs unless `--apply`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_adhoc_epic` |
| Args | `ensure [--repo]`; `archive --repo` or `archive --all-in ORG`, each with optional `--apply`; `normalize --all-in ORG [--apply]` |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Error on stderr |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-adhoc-migrate

One-shot migration (epic vergil-project/.github#85): relocate per-repo
standing epics into the org `.github` ad-hoc model, re-linking their open
children and closing the old epic. A dry run by default; `--apply` is a
human action, refused in agent sessions.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_adhoc_migrate` |
| Args | `--apply` |
| Preconditions | Run in a repo whose `origin` names the org; GitHub App credentials |
| Failure mode | Refuses `--apply` in agent sessions; errors on stderr |
| Exit codes | 0 success, 1 refusal or error |
| Status | Active (one-shot migration) |

### vrg-intake-create

Capture an uncurated item — a bug, an idea, an investigation, a one-line
"don't forget X" — into the intake queue (epic vergil-project/.github#176).
The issue is filed in the current repo's queue home (`<org>/.github` for a
public repo, the repo itself for a self-homed private one) and linked under
that home's `Epic (intake): <name>` as it is created, so there is no unlinked
path. It carries no kind label. The intake epic is resolved (created if
absent) *before* the issue is created; if linking the created issue fails, the
tool exits 1 naming its URL and the `vrg-epic-move` command that links it. Used
by the `intake-capture` skill. See
[Intake, planning and deferred queues](../standards/github-issues.md#intake-planning-and-deferred-queues).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_intake_create` |
| Args | `--title` (required), `--body` / `--body-file`, `--label` (repeatable), `--assignee` (repeatable), `--repo OWNER/NAME` (default: the current repo's queue home) |
| Preconditions | Run inside a repo in the target org, or pass `--repo`; GitHub App credentials for that org |
| Failure mode | An extra `--label` missing from the registry is refused before anything is created; a failed link after creation exits 1 naming the created issue |
| Exit codes | 0 created and linked, 1 error |
| Status | Active |

### vrg-intake-route

Route one item between the container epics — intake, planning, ad hoc and
deferred — applying one queue-review decision. Owns the routing mechanics —
transfer-or-recreate, reparent, re-link, close with a pointer, record a
deferral — so no review skill hand-rolls them. Routes:

- `drop --reason TEXT` — comment the reason, naming the queue it was dropped
  from (`Dropped in <queue> review: …`), and close (not planned).
- `defer --reason TEXT --revisit-when TEXT [--date YYYY-MM-DD]` — move the
  item into its queue home's deferred epic and post the deferral record:

  ```text
  Deferred <YYYY-MM-DD>: <reason>
  Revisit when: <conditions>
  Deferred from: <owner>/<repo>#<N>
  ```

  `--date` stamps an original deferral date instead of today. The item is
  linked before the record is posted, and a planning item's `in-progress`
  label is removed. On an item already deferred, `defer` is a **refresh**: a
  new record, no move, and no `Deferred from:` line. If the record fails after
  the move, the command fails loudly and prints the full intended comment to
  post by hand.
- `intake` — reparent a deferred item back onto its home's intake epic.
- `one-off --repo OWNER/REPO [--epic REF]` — move the item into the target
  repo (transfer, or recreate with a link back when GitHub refuses the
  transfer) and link it under that repo's ad-hoc epic (default `adhoc`) or the
  named epic.
- `planning` — reparent the item onto the planning epic of the same queue
  home.
- `split --into REF [--into REF …] --reason TEXT` — comment links to the
  replacement tasks (already created with `vrg-issue-create`) and close.

Which routes an item allows depends on the container epic it is in:

| Item is in | Allowed routes |
|---|---|
| intake | `drop`, `defer`, `one-off`, `planning`, `split` |
| planning | `defer`, `drop` — both refused once an `Epic:` marker is recorded |
| ad hoc | `defer` — refused while an open PR would close the item |
| deferred | `defer` (refresh), `intake`, `planning`, `one-off`, `drop`, `split` |
| no parent | `planning` (the `epic-create` free-text path) |
| a development epic, or anything else | none — refused, naming the parent |

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_intake_route` (mechanics and route table: `lib/intake_route.py`) |
| Args | `<issue-ref>` (`owner/repo#N`, or `#N` in the current repo), then a route: `drop`, `defer`, `intake`, `one-off`, `planning`, `split` |
| Preconditions | The item is a child of a container epic that allows the route (`planning` also accepts an unparented issue); GitHub App credentials for the item's org |
| Failure mode | A refused route — not allowed from the item's container, a development-epic task, a planning item with an `Epic:` marker, an ad-hoc item with an open closing PR, a cross-org target, or a private item headed to a public repo or epic — exits 1 with the reason on stderr. A route half-applied by a failure raises with the state it left and how to fix it |
| Exit codes | 0 routed, 1 refused or error, 2 bad arguments (including a missing `--revisit-when` or a malformed `--date`) |
| Status | Active |

A `security` item keeps its advisory links. A recreate retargets every
advisory the item names before the original closes, and a transfer
retargets every one after the move; on a failure, either names every
advisory still not retargeted. A split retargets an item's one advisory
before the original closes. A split of an item naming several advisories
is refused before any change, because which advisory goes to which task
can't be inferred:

```text
vrg-intake-route: <owner>/<repo>#<N> names several advisories (<GHSA-id>, <GHSA-id>); retarget each by hand with vrg-security-record retarget, then split
```

See [Security Records](../guides/security-records.md#when-the-issue-moves).

### vrg-security-record

Record a security finding as a private draft advisory in `<org>/.github`
paired with a neutral `security`-labelled intake issue (epic
vergil-project/.github#517). `create` makes the pair (or attaches an
advisory to an existing issue, or resumes an unfinished pair), `list` and
`show` read records, and `retarget` moves an advisory's tracking to new
issues. Advisory text is printed only by `show` and `list --details`. See
[vrg-security-record](dev/security-record.md) and
[Security Records](../guides/security-records.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_security_record` (library: `lib/security_records.py`) |
| Args | `create --repo --title --body-file --details-file --severity [--summary] [--issue] [--resume]`; `list [--repo] [--state draft\|closed\|all] [--details]`; `show GHSA-ID\|OWNER/REPO#N`; `retarget GHSA-ID --to REF [--to REF …]` |
| Preconditions | The org's `.github` is public; Repository security advisories: Read and write on the identity's App (or a human `gh` login with access) |
| Failure mode | A partial pair exits 1 naming what exists and the exact `--resume` command that finishes it; a private `.github`, a duplicate draft, or `--details` under `VRG_EPIC_SWEEP` is refused |
| Exit codes | 0 success, 1 refused or error, 2 bad arguments |
| Status | Active |

### vrg-queue

Read-only view of a standing queue. `vrg-queue intake` lists the open items
under the intake epic, oldest first. `vrg-queue planning` lists the open items
under the planning epic with their state: **waiting** (no `in-progress`
label), **in-progress** (labelled, with its latest progress note), or
**ready-to-close** (labelled, the `Epic:` / `Docs task:` / `Tasks filed: yes`
markers present, and the docs task closed). A planning item that any security
advisory it names still tracks is held at **in-progress** until every such
advisory is retargeted to the task or tasks that fix it, with a note that
counts them and names them by GHSA ID only (`held: <k> advisory still tracks
this planning issue (<GHSA-id>) — …` for one, `held: <k> advisories still
track this planning issue (<GHSA-id>, …) — …` for more; see
[Security Records](../guides/security-records.md#security-items-in-planning)).
`vrg-queue deferred` lists the
open items under the deferred epic, all **waiting**, least recently deferred
first; each item's note is its latest deferral
(`Deferred <day>: <reason> | Revisit when: <conditions>`), and an item with no
deferral comment sorts first with an empty note. A bare `vrg-queue` lists all
three queues in that order (intake, planning, deferred). Used by the
collect step of the three review skills (`intake-review`, `planning-review`,
`deferred-review`) and by `epic-create`'s no-argument summary.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_queue` (views: `lib/queue_view.py`) |
| Args | `{intake,planning,deferred}` (optional; default: all three); `--repo OWNER/NAME` (that repo's queue home; default: the current org's `.github` queues); `--json` (a list of items; with no queue named, an object keyed by queue, `null` for a missing one) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | A named queue whose home has no live queue epic exits 1 naming the missing epic; with no queue named, a missing queue prints `<queue> queue for <name> in <home>: no queue yet` and the run exits 0. Nothing is created |
| Exit codes | 0 success, 1 error, 2 bad arguments |
| Status | Active |

### vrg-roadmap

Print the generated project roadmap: the open epics in a repo's resolved
epic home. The docs-refresh workflow publishes the same rendering.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_roadmap` |
| Args | `--org ORG` or `--repo OWNER/NAME` (default: the current repo's owner) |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Error on stderr |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-activity-log

Print the project activity log: issues closed across the org in the last 30
days. `--repo` narrows it to one repo; `--out-dir` instead writes the
multi-page form for a docs site (`index.md`, `days/<date>.md`,
`repos/<name>.md`). The docs-refresh workflow publishes it.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_activity_log` |
| Args | `--org ORG`, then `--repo REPO` or `--out-dir DIR` |
| Preconditions | GitHub App credentials for the target org |
| Failure mode | Exits 1 when no App installation covers the org |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-trusted-authors

The one accessor for the trusted-authors configuration: the GitHub logins
whose issues and comments automation may trust. It reads
`~/.config/vergil/trusted-authors.toml` (delivered into agent VMs by `vrg-vm`)
if it exists, else the `[trusted_authors]` stanza of
`~/.config/vergil/identities.toml`, and prints `human: <login>` and
`agent: <login>`, one per line. Skills and doctrine name this command, never
the logins. See
[Trusted authors](../guides/account-setup.md#trusted-authors).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_trusted_authors` (config: `lib/trusted_authors.py`) |
| Args | `--json` (prints `{"human": [...], "agents": [...]}`) |
| Preconditions | The VM file or the host stanza exists |
| Failure mode | Nothing configured, or an invalid configuration, prints the error on stderr and exits 1; it never prints a default |
| Exit codes | 0 success, 1 not configured or invalid, 2 bad arguments |
| Status | Active |

### vrg-epic-audit

Report epic/task drift and invariant violations for an org, as Markdown on
stdout. It is read-only by default. **Drift** is a task still open although
the merged PR that `Ref`s it landed within the last `--window-days` days, and
an open finite epic whose children are all closed. The **invariant** checks
are the [compliance invariants](../standards/github-issues.md#compliance-invariants):
epics outside `.github`, stray `.github` issues, a closed epic with an open
child, orphans, retired kind labels, body-only `Parent:` links, self-homed
queues in a now-public repo, and private work under a public epic. It also
lists open epics with operational tasks still pending, and closed
operational tasks with no `Outcome: SUCCESS`. The report previews, without
writing, the ad-hoc and queue-epic drains and the security-records pass that
`--close` would run.

The org comes from the current repo's `origin` remote (there is no `--org`
flag). `--repo OWNER/NAME` audits that repo's resolved epic home instead of
the org's `.github`, so a private repo that self-homes its epics can be
audited; the home-scoped checks (epic drift, stray issues, operation-pending
epics, and the `--close` drift closes) then target it.
`--epic REF` is a separate read-only mode: it prints one epic's children as
JSON (`{"epic": <slug>, "children": [{repo, number, title, state}, …]}`) and
skips the drift audit. A bare `#N` resolves against the current repo's epic
home.

`--close` applies the fixes, in order:

1. Close the drifted tasks and rolled-up epics, and reopen each epic closed
   while a child was still open, with an explanatory comment on each.
2. Drain the closed children of the ad-hoc, intake, planning and deferred
   epics into per-quarter archives, and close past archives (see
   [Ad-hoc epic archiving](../standards/github-issues.md#ad-hoc-epic-archiving)).
3. Run the [orphan pass](../standards/github-issues.md#the-orphan-pass-auto-heal):
   nativize body-only `Parent:` links, strip the retired kind labels, move a
   now-public repo's queue items under `.github`, and attach every orphan to
   its home's intake epic.
4. Run the security-records pass, even when the orphan pass failed: each
   paired draft advisory in the org's `.github` is set to closed once every
   issue it tracks is closed, and back to draft while any is open; broken
   pairs are reported, not repaired. Its output names only GHSA IDs, issue
   refs and states, never advisory text (see
   [Security Records](../guides/security-records.md#managing-a-record-through-its-issue)).

`--close` is a human action, or the scheduled `ops-epic-sweep` workflow,
which sets `VRG_EPIC_SWEEP=1`. Other invariant violations stay report-only.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_audit` (checks and passes: `lib/epic_audit.py`; security pass: `lib/security_records.py`) |
| Args | `--window-days N` (positive; default 30), `--repo OWNER/NAME`, `--epic REF`, `--close` |
| Preconditions | Run inside a repo in the target org; GitHub App credentials for that org. `--close` needs a human identity or `VRG_EPIC_SWEEP=1`. The security-records pass needs Repository security advisories: Read and write |
| Failure mode | `--close` in an agent session is refused before any network call; a `--repo` without `/`, an org that cannot be detected, or an org with no `.github` home exits 1. A full intake epic stops the orphan pass, listing what it did not attach; a broken security pair or a failed security pass exits 1 after the rest of the run. With `--epic`, a ref that is not an epic exits 1 |
| Exit codes | 0 success (the read-only report exits 0 even when it finds drift), 1 refused, a pass failed, or error, 2 bad arguments |
| Status | Active |

### vrg-worktree-status

Show every worktree git knows about in the current repo, grouped by
kind, so nothing with code on disk goes unseen (issue #3386). Read-only
— cleanup stays `vrg-finalize-pr`'s job, and finalize and sweep still
act only on development worktrees. Local branches with no worktree are
not shown. The sections, in order:

1. **Main checkout** — always shown. Its STATUS is `ok`, or `PROBLEM:`
   followed by each issue found: not on the integration branch, dirty,
   or mid-rebase. The integration branch comes from the
   `branching-model` in `vergil.toml` (every current model integrates on
   `develop`); without a `vergil.toml` it defaults to `develop` and a
   note says so. A malformed `vergil.toml` is an error (exit 1).
2. **Development** — issue-branch worktrees under `.worktrees/`: a
   `feature/`, `bugfix/`, `hotfix/` or `chore/` branch carrying an issue
   number (`<type>/<N>-<slug>`), including the `chore/<N>-dep-update-*`
   fix branch `vrg-update-deps` asks for. An issue prefix without an
   issue number, or an unknown prefix, goes to Other / unknown.
   A mid-rebase worktree is placed by the branch recovered from the
   paused rebase or its `issue-<N>-<slug>` directory name.
3. **Release and dependency machinery** — `release/*`, `release/post-*`
   and `deps/update-*` worktrees. These are meant to be single-run, so
   each row shows age, PR state and dirty state and a leftover stands out.
4. **Other / unknown** — any other branch, a detached HEAD, or a
   worktree outside `.worktrees/` (shown by its full path).

Empty sections are omitted; the summary line counts every section,
including the empty ones. A branch that was deleted on its remote is
still listed, with `(branch gone)` after its name. That check queries
each remote once; if it fails, a note gives the reason.

The worktree sections share one table. PR state is queried from GitHub
per worktree; a failed lookup shows as `unknown` with the reason rather
than being silently downgraded. The development summary counts fall
into disjoint buckets — **active**, **needs-attention**, **stalled
(no-pr)**, and **cruft (removable)**. A finished-but-stuck worktree
(merged/closed but not removable: dirty, or a reused branch name whose
merged verdict was withheld, issue #1719) is counted under
**needs-attention** with an actionable note, never miscounted as active
behind a "0 cruft" message (issue #2347). A merged release or deps
worktree shows as `merged` in its own section but is not counted as
`vrg-finalize-pr` cruft.

```text
Main checkout (integration branch: develop)
CHECKOUT               BRANCH   DIRTY  LAST COMMIT  STATUS
mq-gateway-replay-lab  develop  -      23d ago      ok

Development
WORKTREE                      BRANCH                          PR  STATE  WORKFLOW  AHEAD  DIRTY  LAST COMMIT  LAST MODIFIED
issue-315-deps-security-bump  feature/315-deps-security-bump  -   no-pr  ready     1      -      1h ago       1h ago

Other / unknown
WORKTREE         BRANCH      PR  STATE     WORKFLOW  AHEAD  DIRTY  LAST COMMIT  LAST MODIFIED
tmp-wheel-build  (detached)  -   detached  -         0      -      82d ago      82d ago
  note: tmp-wheel-build (detached): detached HEAD — no branch checked out; check out a branch to resume work

3 worktrees: main checkout (ok), 1 development, 0 release and dependency machinery, 1 other / unknown.
Development: 0 active, 0 needs-attention, 1 stalled (no-pr), 0 cruft (removable). 1 PR prepared.
```

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_worktree_status` |
| Args | `--target-branch` (default: develop) — the branch AHEAD is measured against |
| Preconditions | Git repo; `gh` CLI on PATH for PR state |
| Failure mode | None for classification — a failed PR lookup is reported as an `unknown` row and a failed branch-gone check as a note, never a crash. A malformed `vergil.toml` exits 1 with the reason |
| Exit codes | 0 success, 1 malformed `vergil.toml` |
| Status | Active |

### vrg-whoami

Print the resolved Vergil identity role, from the authoritative resolver
in `lib/identity_mode.py`. Ask `vrg-whoami`; never infer identity from
`VRG_IDENTITY_MODE` alone. That variable is only the first of five
fallback signals (environment variable, mode file, App key, App ID, then
the `human` default), and an unset value means "fall through", not
"human". See [Account setup](../guides/account-setup.md).

- `vrg-whoami` / `--mode`: the role as one token (`human`, `user` or
  `audit`), for scripting, for example
  `export VRG_IDENTITY_MODE="$(vrg-whoami --mode)"`.
- `--platform`: the platform as one token (`physical-host`, `local-vm` or
  `cloud-vm`), a second axis independent of the role, resolved from
  empirical signals in `lib/platform_env.py`. It fails closed: an
  unconfirmed VM resolves to `cloud-vm`, never `physical-host`.
- `--explain`: the role, the signal it resolved from and every signal's
  state, then the platform and its signals. It warns on stderr when the
  identity signals disagree, and when the platform and the identity
  correlate poorly (expected: host with human, VM with an agent).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_whoami` |
| Args | `--mode`, `--platform`, `--explain` (mutually exclusive) |
| Preconditions | None |
| Failure mode | Disagreement is a warning on stderr, never an error |
| Exit codes | 0 |
| Status | Active |

### vrg-hook-guard

The Claude Code `PreToolUse` hook that blocks raw `git` and `gh` in agent
sessions and points the agent at `vrg-git` and `vrg-gh`. It reads the hook
JSON on stdin and prints a deny decision when a command invokes `git` or
`gh` at command position. Quoted spans are stripped before matching, and
the quoted argument of `bash -c` / `sh -c` is checked too. It is a no-op
outside a repo with a `vergil.toml`.

Two layers call it: each repo's `.claude/settings.json` wires
`.claude/hooks/guard.sh`, a shim that runs `vrg-hook-guard` when
vergil-tooling is installed and otherwise falls back to a `jq`-based hard
deny; and the vergil-claude-plugin provides the same guard through its
own hooks. See [Claude Code Hook Guard](hooks/pre-commit.md).

Inside a managed repository it fails closed: a call whose hook input it cannot
parse or check is refused (see
[Malformed input fails closed](hooks/pre-commit.md#malformed-input-fails-closed)).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_hook_guard` |
| Args | None (hook JSON on stdin) |
| Preconditions | Run by Claude Code as a `PreToolUse` hook |
| Failure mode | A deny decision as JSON on stdout for raw `git`/`gh`; in a managed repository, malformed input is refused with a message on stderr; anything else allows the command |
| Exit codes | 0 allow or deny decision, 2 malformed input refused (fails closed) |
| Status | Active |

### vrg-ensure-label

Ensure GitHub labels exist. Three modes: single-label (create/update
one label), sync (provision all labels from the canonical registry,
and delete the registry's `delete` list and the retired `triage`,
`idea` and `research` labels where present), and project
(discover repos via a GitHub Project and sync each). The
registry is `src/vergil_tooling/data/labels.json`; the labels it
defines are listed in
[GitHub Issue Standards](../standards/github-issues.md#labels).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ensure_label` |
| Args | `--repo`, `--label`, `--color`, `--description`, `--sync`, `--owner`, `--project` |
| Preconditions | `gh` CLI on PATH |
| Failure mode | argparse validation for incompatible flag combinations; subprocess error from `gh` |
| Exit codes | 0 success |
| Status | Active |

### vrg-github-repo-config

Audit a managed repo against the canonical Vergil configuration, and
in `apply` mode reconcile the GitHub half. Combines local filesystem
checks (`vergil.toml`, `CLAUDE.md`, `.claude/settings.json`, hook-guard
shim, workflow pins, `.gitignore` managed fence, canonical
`dependabot.yml`, `ops.yml` wiring)
with GitHub-API checks (repo settings, rulesets, required-check set).
Runs nightly from each repo's `ops.yml` via the reusable
`ops-github-config.yml` workflow, where drift is fatal. See the
[GitHub Config Audit reference](config-audit.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_github_repo_config` |
| Args | `audit` \| `diff` \| `apply`; `--repo OWNER/REPO`, `--config PATH` |
| Preconditions | Local checks require the repo's own checkout as CWD; GitHub checks require `gh` credentials |
| Failure mode | `DiffItem` list on drift; clean diagnostic on unreachable GitHub state |
| Exit codes | 0 compliant, 1 non-compliant (drift), 2 audit could not complete |
| Status | Active |

### vrg-dependabot-audit

Fail on any open Dependabot alert, at any severity: the gate for the
fleet's report-only Dependabot posture. Private repos get an itemized
report; public repos (and any repo whose visibility cannot be resolved)
get counts by severity and the Security-tab link only. See the
[Dependabot Alert Audit reference](dependabot-audit.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_dependabot_audit` |
| Args | `--repo OWNER/REPO` (required) |
| Preconditions | A token with "Dependabot alerts: Read" on the repo; alerts enabled on the repo |
| Failure mode | One `vrg-dependabot-audit:` diagnostic on stderr when the alerts cannot be read |
| Exit codes | 0 no open alerts, 1 at least one open alert, 2 could not check |
| Status | Active |

### vrg-github-repo-init

Interactive (or `--non-interactive`) wizard that bootstraps a new
managed repository: creates and clones the repo, generates
`vergil.toml`, scaffolds config files (`CLAUDE.md`, hook-guard shim,
the managed `.gitignore` fence, the canonical report-only
`.github/dependabot.yml`), scaffolds the language skeleton
(born-green — see below), generates
CI/CD/epic-rollup/`ops.yml` workflows, the docs site, branch structure,
GitHub config, and Pages. The scaffolded `base + <language>`
`.gitignore` fence, canonical `dependabot.yml` and staggered-cron
`ops.yml` make a new repo pass the config audit's checks for those
files from day one.

The vergil series a new repo uses comes from the installed
vergil-tooling: `[dependencies].vergil` defaults to its `vX.Y`, and the
generated workflows pin the vergil-actions reusable workflows to that
series. A new repo therefore starts on the series that created it.

**Born-green language skeleton.** A dedicated scaffolding step (step 5,
`lib/lang_scaffold.py`) renders the language's packaged skeleton
templates (`src/vergil_tooling/data/skeletons/<lang>/`) into the fresh
checkout, stamping only files that are missing so it never clobbers
existing content. For a language that resolves a committed lockfile
(cpp), it then runs that language's lock command
(`conan lock create . -s build_type=Debug`) and one full `vrg-validate`
inside the dev container, so the new repo is *born green* — it passes
validation from its first commit. The container is a fail-fast
precondition for a lock-resolving language: if no runtime is available
the step refuses **before writing anything**, so such a repo is born
green or not born at all, never half-created. The step writes no commit
of its own; its host-rendered skeleton and container-resolved lock ride
into the next step's commit. Because the container warmup for cpp
requires a `conan.lock` and one does not yet exist during this window,
the warmup deliberately **skips** until the scaffold's
`conan lock create` produces the lock — the skeleton resolves in an
unwarmed-but-usable container, and the warmup runs normally on every
later build once the lock is committed
([epic vergil-project/.github#342](https://github.com/vergil-project/.github/issues/342)).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_github_repo_init` |
| Args | `org/name` positional; `--adopt`, `--non-interactive`, `--target-dir`, and per-field overrides (`--language`, `--repository-type`, …) |
| Preconditions | `gh` and `git` on PATH; run from the org-layout parent (or pass `--target-dir`) |
| Failure mode | Loud refusal on a foreign-repo CWD, an occupied clone path, or missing required non-interactive values |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-gitignore-sync

Single-repo applicator for the vergil-managed `.gitignore` block. Reads
`<repo>/.gitignore`, resolves the fence language from
`[project].primary-language` (normalized to base-only for any language
without a managed fragment), and either checks (default) or writes the
composed `base + <language>` managed block through `lib/gitignore.py` —
the same module the config audit renders through, so applicator and
audit cannot diverge. It owns **only** the file change: it has no git or
PR knowledge. `--write` rewrites the fenced block, moves it to the top of
the file, and drops any exact repeat of one of the fence's own patterns
outside it (the fence is the single source of truth), leaving genuinely
repo-local lines untouched and in order. `--check` reports drift exactly
when `--write` would change the file, so `--write` then `--check` always
passes (#3568). See the
[GitHub Config Audit reference](config-audit.md#the-composed-managed-fence).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_gitignore_sync` |
| Args | `--repo PATH` (default: CWD); mutually exclusive `--check` (default) \| `--write` |
| Preconditions | A repo whose language resolves from `[project].primary-language` (no `vergil.toml` or no declared language → base-only; a `vergil.toml` that fails to load is an error, exit 2) |
| Failure mode | `--check` prints each drift reason to stderr; `--write` succeeds whenever the language resolves (prints `already in sync` or the synced path) |
| Exit codes | 0 ok, 1 `--check` drift, 2 usage error or a `vergil.toml` that fails to load (the config error is printed to stderr; nothing is written) |
| Status | Active |

### vrg-dependabot-sync

Single-repo applicator for the canonical **report-only**
`.github/dependabot.yml` (epic vergil-project/.github#420). Resolves the
repo's `[project].primary-language` and either checks (default) or writes
the render from `lib/dependabot.py`: a `github-actions` entry plus the
language's ecosystem (`python` → `uv`, `go` → `gomod`, `rust` → `cargo`,
`java` → `maven`, `typescript` → `npm`; any other language gets
`github-actions` only), every entry at `open-pull-requests-limit: 0` so
Dependabot never opens a PR. The file is **exact match**: `--write`
replaces local additions. It owns **only** the file change and has no git
or PR knowledge.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_dependabot_sync` |
| Args | `--repo PATH` (default: CWD); mutually exclusive `--check` (default) \| `--write` |
| Preconditions | A repo whose language resolves from `[project].primary-language` (no `vergil.toml` or no declared language → `github-actions` only; a `vergil.toml` that fails to load is an error, exit 2) |
| Failure mode | `--check` prints `missing .github/dependabot.yml` or `differs from the canonical render` to stderr; `--write` succeeds whenever the language resolves (prints `already in sync` or `wrote .github/dependabot.yml (<lang or none>)`) |
| Exit codes | 0 ok, 1 `--check` drift, 2 usage error or a `vergil.toml` that fails to load (the config error is printed to stderr; nothing is written) |
| Status | Active |

### vrg-fleet-sync

Propagate a vergil-managed file across a fleet of repositories, opening
**one PR per repo whose file drifts**. `--target` picks the file:
`gitignore` (default) syncs the managed `.gitignore` block via
`vrg-gitignore-sync --write`; `dependabot` writes the report-only
`.github/dependabot.yml` via `vrg-dependabot-sync --write` (branch slug
`dependabot-config`, filed under epic vergil-project/.github#420). A
target's epic links only repos in the epic's own org: a repo in another
org (read from the clone with `gh repo view`) is filed under its own
ad-hoc epic, and the issue body still names the target's epic, because
`vrg-issue-create` refuses a cross-org link. It
drives the generic fleet-sweep driver (`lib/fleet_sweep.py`): per repo it
creates a worktree, shells out to the target's applicator, commits,
and runs `report-ready`. Like every agent-run task it **never submits or
merges** — it stops at `report-ready`, leaving submission and merge to the
human. `--dry-run` reports the verdict a live run would reach per repo
(would change, naming the epic it would be filed under, or skip) from a
disposable probe worktree, without opening an issue, branch, commit, or
PR. Each target previews with its applicator's `--check`
(`vrg-gitignore-sync --check` or `vrg-dependabot-sync --check`), whose
drift verdict is exactly the change `--write` would make.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_fleet_sync` |
| Args | `--repos PATH [PATH …]` (required, local clone paths); `--target {dependabot,gitignore}` (default `gitignore`); `--dry-run` |
| Preconditions | The target's applicator (`vrg-gitignore-sync` or `vrg-dependabot-sync`) on PATH; each `--repos` path a local clone; git/`gh` credentials for the sweep |
| Failure mode | Any repo that errors is recorded and reported in the per-repo status lines |
| Exit codes | 0 all repos ok, 1 one or more repos errored |
| Status | Active |

### vrg-ci-sync

Fleet sweep that strips the retired `versions:` / `container-tag:` inputs
from each repo's `.github/workflows/ci.yml`, opening one PR per repo whose
file drifts. It stops at `report-ready` and never submits or merges.
`--dry-run` reports each repo's verdict without opening anything. A repo
whose `ci.yml` cannot be understood is left unchanged and listed in the
report.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ci_sync` |
| Args | `--repos PATH...` (required, local clones), `--dry-run` |
| Preconditions | Local clones of the target repos; GitHub App credentials |
| Failure mode | Per-repo errors are reported in the summary |
| Exit codes | 0 success, 1 any repo errored |
| Status | Active |

### vrg-xdist-sync

Fleet sweep that adds `pytest-xdist` to each Python repo's dev dependencies,
opening one PR per repo that needs it. It stops at `report-ready` and never
submits or merges. A repo it cannot safely change is reported and gets a
follow-up issue; a non-Python repo is skipped. `--dry-run` reports each
repo's verdict without opening anything.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_xdist_sync` |
| Args | `--repos PATH...` (required, local clones), `--dry-run` |
| Preconditions | Local clones of the target repos; `uv` on PATH; GitHub App credentials |
| Failure mode | Per-repo errors are reported in the summary |
| Exit codes | 0 success, 1 any repo errored |
| Status | Active |

### vrg-container-run

Run arbitrary commands inside a dev container. Auto-detects the
project language to select the Docker image; falls back to
`dev-base:latest`. Uses `execvp` to replace the process.
Local-settings guard: on the host, before any container starts, it refuses an
agent (warns a human) while a loadable `.claude/settings.local.json` is not
absent or `{}` (epic vergil-project/.github#558).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_container_run` |
| Args | `[--prefix <dev\|prod>] [--] <command> [args...]` (manual parsing, `--` separator) |
| Preconditions | Git repo, `GH_TOKEN` set, Docker daemon running |
| Failure mode | Explicit error message for missing `GH_TOKEN`; `assert_docker_available()` exits with message for Docker; `git.repo_root()` raises on non-git directory; in a cpp repo, an unreadable or malformed ConanCenter audit token source exits 1 naming the file (see [Container Config](container-config.md#variables-vrg-container-run-always-sets)) |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### vrg-container-test

Run a repository's test suite inside a dev container. The language comes
from `[project].primary-language` in `vergil.toml`, falling back to
package-manager markers (`pyproject.toml`, `go.mod`, `Cargo.toml`,
`pom.xml`/`mvnw`, …) when the repo asserts none; it selects the image and
the test command. Consuming repos call it directly. Environment overrides:

- `DOCKER_DEV_IMAGE`: the container image.
- `DOCKER_TEST_CMD`: the test command.
- `DOCKER_NETWORK`: a Docker network to join, for example for
  integration tests.

Env-var passthrough is set per repo with `[container].env-prefixes`; see
[Container Config](container-config.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_container_test` |
| Args | None |
| Preconditions | Git repo, Docker daemon running, language detection or `DOCKER_DEV_IMAGE` + `DOCKER_TEST_CMD` |
| Failure mode | Explicit error for undetected language and unavailable Docker; `git.repo_root()` raises on non-git directory |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### vrg-container-docs

Preview or build MkDocs documentation inside a dev container.
Supports `serve` (live-reload) and `build` subcommands. For Python
repos, wraps with `uv sync --group docs` and sets
`UV_PYTHON_PREFERENCE=only-system`, so uv uses the docs image's own Python
(as CI does) whatever the repo's `python-preference`, and fails loudly if
that Python does not satisfy `requires-python`. Always sets
`NO_MKDOCS_2_WARNING=1` in the container, so local builds suppress the
mkdocs-material MkDocs 2.0 warning banner the same way CI does.

Before mkdocs runs, it stages the docs sources on the host the same way
CI's docs job does (`actions/shared/docs/stage` in vergil-actions), so a
local strict build passes or fails where CI's does:

1. `vrg-docs-stage` copies `CHANGELOG.md` to `<config dir>/docs/changelog.md`
   and `releases/*.md` to `<config dir>/docs/releases/`, and writes the
   release index.
2. `vrg-docs-patch-nav` patches the release versions into the nav of a
   copy of the config, `mkdocs.staged.yml`, written next to `mkdocs.yml`.
   The tracked `mkdocs.yml` is never modified. mkdocs runs with
   `-f <config dir>/mkdocs.staged.yml`.

Staging needs authenticated `gh` (or an App token in agent sessions)
whenever the repo has release notes, because each release page links to
its CI-evidence bundle. Without auth the command fails; it never skips
staging.

Staging must not dirty the working tree, so every generated path has to
be gitignored. The managed base gitignore covers the default layout
(`docs/site/docs/changelog.md`, `docs/site/docs/releases/`,
`mkdocs.staged.yml`). If a generated path is not ignored, including a
staged copy that the repo has committed, the command refuses before
writing anything and lists the paths. To fix it, stop tracking any
committed copies (`vrg-git rm --cached <path>`), then run
`vrg-gitignore-sync`.

`serve` stages once, at startup. Edits to the docs pages reload as usual,
but edits to `mkdocs.yml`, `CHANGELOG.md` or `releases/` take effect only
after a restart.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_container_docs` |
| Args | `<serve\|build> [mkdocs args...]` (manual parsing) |
| Preconditions | Git repo, Docker daemon, mkdocs config at `MKDOCS_CONFIG` (default `docs/site/mkdocs.yml`), generated docs paths gitignored, `gh` auth when the repo has release notes |
| Failure mode | Usage message on missing/unknown subcommand; explicit error for a missing config, un-ignored generated paths, or a staging failure (including missing `gh` auth) |
| Exit codes | 0 (help), 1 error; command exit code after `execvp` |
| Status | Active |

### vrg-container-build-command

Print a repo's declared `[container].build-command` verbatim, for CI
consumption. The CI test jobs call `--script` to obtain the exact
provisioning command to run per job — the same command the local cache
build bakes into the per-branch image — so the two paths run one
declaration. Prints nothing when the repo declares no `build-command`.
See [Container Config Reference → `build-command`](container-config.md#build-command).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_container_build_command` |
| Args | `--script` (explicit CI intent; identical output), `--repo-root` (default: CWD) |
| Preconditions | A readable `vergil.toml` at the repo root (absent/empty key prints nothing) |
| Failure mode | argparse error on unknown flags; no failure for an unset key |
| Exit codes | 0 always |
| Status | Active |

### vrg-container-system-packages

Print a repo's declared `[container].system-packages`, for CI
consumption. Default mode prints the package names one per line;
`--install-script` prints the exact apt install snippet — the single
speller shared with the local cache build — which the CI test jobs run.
See [Container Config Reference → `system-packages`](container-config.md#system-packages).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_container_system_packages` |
| Args | `--install-script` (apt snippet instead of the name list), `--repo-root` (default: CWD) |
| Preconditions | A readable `vergil.toml` at the repo root (absent/empty key prints nothing) |
| Failure mode | argparse error on unknown flags; no failure for an unset key |
| Exit codes | 0 always |
| Status | Active |

### vrg-container-cache

Manage the per-branch cached container image (base image plus
vergil-tooling, `system-packages`, and the `build-command` artifact) that
`vrg-container-run` builds over. Subcommands: `build` (build/refresh the
cached image for the current branch), `clean` (remove it), `status` (show
cache state and the expected tag), `clean-all` (remove every image it
manages), and `prune` (remove the cached images of finished branches).

`prune` covers every cached per-branch image in the local image store,
whichever repo built it. It is a **dry run by default**: it prints the repo
set and a keep or remove verdict, with the reason, for each image, and
removes nothing until re-run with `--apply` (issue #3481). The image tag
records the branch (sanitized: `/` becomes `-`) but not the repository, so
the rule leans toward keeping. An image is kept when:

- its branch is protected (`develop`, `main`, `release`);
- a container, running or stopped, uses it;
- its branch matches a live branch in **any** repo of the repo set. A
  branch is live when it is a local branch (which covers every worktree)
  or exists on the repo's `origin`.

Only an image that matches none of these is removed. Origin is read with
`git ls-remote`, so a branch deleted on GitHub counts as gone. When a
repo's origin cannot be read (typically a private repo the agent's App
cannot see), that repo's local `refs/remotes/origin/*` stand in, with a
warning naming the repo; stale tracking refs can only keep extra images,
never remove live ones. Any other failure to read the repo set, the images,
the containers or a repo's branches stops the run before anything is
removed. A failed image removal is reported with the runtime's error, the
remaining removals still run, and the command exits 1.

The repo set comes from `--root DIR` (repeatable; a projects directory, a
directory of repos, or a single repo, searched down to `<org>/<repo>`),
which replaces the platform default: `/vergil/projects` on a cloud VM, and
on a Lima VM the projects mount, found two levels above the current repo's
main worktree and accepted only if it is a mount point. The physical host
has no default, so `--root` is required there. A root that does not exist,
or roots that hold no repositories, are an error.

`vrg-finalize-pr` runs the same sweep (in effect `prune --apply` against
the platform's default repo set) after removing the merged branch's own
cached images. It runs on agent VMs and is skipped on the physical Mac
host, which has no default repo set; there finalize prints the
`vrg-container-cache prune --root DIR` command to run by hand. A failed
sweep or removal is a finalize warning, never a failure (issue #3566).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_container_cache` |
| Args | `build` \| `clean` \| `status` \| `clean-all` \| `prune [--apply] [--root DIR]...` |
| Preconditions | A container runtime (`nerdctl` or `docker`) on PATH for every subcommand; a git repo for `build`/`clean`/`status`; for `prune`, a repo set (`--root DIR` on the physical host) |
| Failure mode | Runtime-unavailable error; propagates the runtime's build failures; every failed image removal is reported with the runtime's error while the rest still run. `prune` refuses, before removing anything, a missing or empty repo set, or images, containers or branches it cannot read |
| Exit codes | 0 success (including a `prune` dry run); 1 no subcommand, no runtime, a failed removal, or a `prune` refusal; non-zero on build failure |
| Status | Active |

### vrg-scorecard

Run OpenSSF Scorecard inside a dev container with a GitHub App token
injected as `GH_TOKEN`. Every argument except `--prefix` is passed through
to `scorecard`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_scorecard` |
| Args | `--prefix {dev,prod}` (default `prod`), then `scorecard` arguments |
| Preconditions | Git repo; a container runtime; GitHub App credentials |
| Failure mode | Error on stderr for a bad `--prefix` or missing credentials |
| Exit codes | 1 on those errors; otherwise `scorecard`'s exit code |
| Status | Active |

### vrg-vm

Create, start, stop, update, rebuild and enter the identity agent VMs,
Lima on the Mac or off-platform (cloud). Subcommands: `create`, `start`,
`stop`, `restart`, `update`, `destroy`, `destroy-volume`, `rebuild`,
`list`, `volumes`, `session` (launch a Claude session in the VM) and `cp`
(copy files between the host and a VM). `vrg-vm update` reinstalls
vergil-tooling and refreshes the plugins in a running VM in place; `--all`
updates every configured identity's boxes. VMs install vergil-tooling as
the signed package; `vrg-vm update --tag <ref>` with a non-release ref
installs a dev copy instead and flags it with a `DEV tooling` banner, and
a plain `vrg-vm update` returns the VM to the package. See
[VM Spec](vm-spec.md#vergil-tooling-inside-the-vm).

**The `--plugin-dir` override.** `vrg-vm session` can launch Claude with
plugin trees loaded from disk instead of the installed plugins, for running
unreleased plugin work (for example a paad checkout's `preview/paad`) across
the fleet. List the trees in `~/.config/vergil/plugin-dirs.toml` on the host,
a personal file you create by hand:

```toml
plugin_dirs = ["/Users/me/dev/projects/me/paad-fleet/preview/paad"]
```

`plugin_dirs` is the only key, and each entry must be an absolute path. For
each entry the session adds `--plugin-dir <path>` to the `claude` launch,
after `--model` and before any `-- claude …` arguments; a raw `-- <command>`
override is unaffected. The file is read when every session starts, so a
change applies to the next session.

- **Lima only.** A Lima VM sees the Mac's projects tree at the same path, so
  the path is checked inside the guest: a directory missing there fails the
  launch with `ERROR: plugin directory not found in the guest: <path>`.
  Listing sessions (`vrg-vm session <repo>` with no `--label`, `--resume`
  or `--fresh`) starts no Claude, so it skips the check. A cloud box has
  no host mount, so a cloud session launches with the installed plugins and
  prints a notice that the override is Lima only.
- **Loud on error.** An unreadable or malformed file (not UTF-8, invalid
  TOML, an unknown key, a value that is not a list of strings, or a
  relative entry) fails every session
  launch, Lima or cloud, with an error naming the file.
- **Turning it off.** Delete the file, or set `plugin_dirs = []`; sessions
  then use the installed plugins again.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_vm` |
| Args | Global `--identity` (default: the configured default identity), `--config` (path to `identities.toml`); then a subcommand with its own flags (`vrg-vm <subcommand> --help`) |
| Preconditions | Host with Lima (local VMs) or the off-platform backend configured; `identities.toml` |
| Failure mode | Error on stderr; `update --all` attempts every box and reports each failure |
| Exit codes | 0 success, non-zero on failure |
| Status | Active |

### vrg-vm-resolve-session

The in-VM half of `vrg-vm session`. It runs inside the identity VM, reads
the VM-local session roster and transcripts, picks the session slot for an
identity and workspace path, and execs Claude Code. With `--list-json` it
prints every named session's state for `vrg-vm list --sessions`. Not run by
hand.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_vm_resolve_session` |
| Args | `--identity`, `--path`, `--fresh`, `--resume-name`, `--label`, `--instance`, `--list-json`, then extra arguments |
| Preconditions | Inside an identity VM with vergil-tooling installed |
| Failure mode | Error on stderr |
| Exit codes | 1 on error; otherwise it execs Claude Code |
| Status | Active |

### vrg-host

Observe and manage the Vergil-critical macOS host dependencies (`uv`,
`gh`, `git`, `bash`, `gcloud`, Lima, Docker Desktop, OpenTofu, `git-cliff`,
vergil-tooling, Claude Code, macOS, the Xcode Command Line Tools) declared in
the packaged, tooling-owned manifest
`src/vergil_tooling/data/host_dependencies.toml` (never
repo-overridable). `status` (the default) reports installed-vs-latest
drift, auxiliary-check health and pins; `status --fix` first runs the
remediations of unsatisfied checks. `upgrade` upgrades only `auto`
dependencies and holds actively pinned ones unless `--force`. `pin` /
`unpin` record holds in the user's `~/.config/vergil/host-pins.toml`
under the epic #155 pin-as-tech-debt doctrine. Runs unprivileged and
refuses `sudo`/`doas`/`su`/`pkexec`. See the
[vrg-host reference](dev/host.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_host` |
| Args | `status [--fix]` (default); `upgrade <dep>…` or `upgrade --all`, each with optional `--force`; `pin <dep> --constraint --inducing-release --reason [--tracking-issue] [--deterministic]`; `unpin <dep>` |
| Preconditions | macOS host with each dependency installed by its documented method (Homebrew for the `brew-formula` and `brew-cask` entries, including `uv`) |
| Failure mode | Per-dependency `probe-error` rows (never fatal to the report); loud error on an unreadable manifest or pins file; refused pins and upgrades print the reason |
| Exit codes | `status`: 0 OK, 1 warnings, 2 errors; `upgrade`: 0 done, 1 a named dependency refused, 2 failure; `pin`/`unpin`: 0 success, 1 refused |
| Status | Active |

### vrg-generate-commands

Generate MQSC command methods for all language ports (Python,
Java, Go, Rust) from `mapping-data.json`. Updates target files
between `BEGIN/END GENERATED MQSC METHODS` markers.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_generate_commands` |
| Args | `--language` (required), `--mapping-data` (required), `--target`, `--mapping-pages-dir`, `--check` |
| Preconditions | `mapping-data.json` file exists at the given path |
| Failure mode | Explicit error for missing mapping data file |
| Exit codes | 0 success, 1 error or `--check` mismatch |
| Status | Active |

## Container tools

Container tools run inside dev containers launched by `vrg-container-run`.
They assume language toolchain dependencies (ruff, mypy, shellcheck,
markdownlint, yamllint) are available on PATH.

### vrg-validate

The unified validation driver. It reads `[project].primary-language`
from `vergil.toml` and runs the stages in order: common checks, the
language's install, lint, typecheck, test and audit commands, and last
the repository's custom validator (`scripts/bin/validate-custom` or
`vrg-validate-custom` on `PATH`) when it has one. `--check` runs one
stage. `--check custom` runs the install stage (when the language has
one) and then the custom stage; with no custom validator it prints one
"no custom validator" line and exits 0, and a validator that is present
but not an executable regular file fails. PR CI runs each stage through
the `vergil-actions` reusable workflows, the custom stage as
`quality / custom`. See the [Validation Matrix](../guides/validation-matrix.md)
for every check and where it runs.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_validate` |
| Args | `--check {common,lint,typecheck,test,audit,custom}`, `--all-versions` (Python only; alone or with `--check test`) |
| Preconditions | Inside a dev container (run it as `vrg-container-run -- vrg-validate`) |
| Failure mode | Runs every stage after install and reports all failures in one run; install fails fast |
| Exit codes | 0 all passed, 1 a stage failed or the config is invalid, 2 usage error (for example `--check custom --all-versions`) |
| Status | Active |

### validate_common (internal)

Not a command on `PATH`: the module `vrg-validate` runs as its common
stage. Shared validation checks for all repos: repository profile
validation, markdownlint on published markdown (`docs/site/**/*.md`
and `README.md`) using the bundled canonical config, shellcheck on
`scripts/`, yamllint on `.github/` and `docs/` YAML files, the
dependabot-limit guard (when `.github/dependabot.yml` exists, every
`updates` entry must carry `open-pull-requests-limit: 0`, so Dependabot
can never open a PR), hadolint
on `Dockerfile*`, actionlint on `.github/workflows/`, and
ansible-lint (using the bundled canonical config) when the repo
carries Ansible content.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.validate_common` |
| Args | None |
| Preconditions | Git repo, `shellcheck` and `yamllint` on PATH |
| Failure mode | Propagates exit codes from each tool |
| Exit codes | 0 all passed, non-zero on first failure |
| Status | Active (called internally by `vrg-validate`) |

### vrg-repo-profile

Validate the repository configuration in `vergil.toml` (required
keys, enum values, dependencies) and the `README.md` structure
(exactly one H1, a Table of Contents, no heading-level skips). Run by
`vrg-validate` as a common check. See the
[repo-profile reference](lint/repo-profile.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_repo_profile` |
| Args | None |
| Preconditions | Run from the repository root; `vergil.toml` exists |
| Failure mode | Exit 2 if `vergil.toml` is not found; exit 1 for an invalid `vergil.toml` or a `README.md` structure error |
| Exit codes | 0 valid, 1 invalid, 2 `vergil.toml` not found |
| Status | Active |

### vrg-ci-resolve

Resolve the repo's CI versions from the published version window, with
the same resolver `vrg-validate` and the dev container use. The CI
setup step of the `vergil-actions` reusable workflows runs it to build
the version matrix and pick the primary-version container. The window
is read at the repo's `[dependencies].vergil` ref; set
`VRG_VERSION_WINDOW=<path>` to resolve against a local `window.json`.
A repo with no language resolves to `latest`. See
[Version resolution](../guides/ci-architecture.md#version-resolution).

- `--format json` (default) — one JSON object with `versions`,
  `primary-version`, `primary-container-tag`, `container-tags` and
  `container-suffixes`.
- `--format github-output` — `versions`, `primary-version` and
  `primary-container-tag` as `key=value` lines for `$GITHUB_OUTPUT`.
- `--repo-root PATH` — the directory holding `vergil.toml` (default:
  the current directory).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ci_resolve` |
| Args | `--format {json,github-output}`, `--repo-root PATH` |
| Preconditions | `vergil.toml` in the repo root; network access to the published window, or `VRG_VERSION_WINDOW` |
| Failure mode | `vrg-ci-resolve: error:` on stderr for a missing or invalid `vergil.toml`, an unreadable or malformed window, or a resolution error (a pin outside the window, an app pin of more than one version, a C++ or TypeScript repo with no pin) |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-package

Build, install-test and index binary OS packages (`.deb`/`.rpm`) from
the `[package]` section of `vergil.toml` in the current directory. Run
by the `ci-package`, `cd-release` and `publish-index` reusable
workflows; `matrix` is also useful locally. See the
[package config reference](package-config.md).

- `matrix` — resolve `[package]` into build and test cells and print
  them as JSON (read-only). `--tier {full,reduced}` (default `full`),
  `--github-output` (append `enabled`/`build`/`test`/`tier` to
  `$GITHUB_OUTPUT`), `--manifest PATH` (also write the release artifact
  manifest). A repo without `[package]` reports `enabled: false`. Each
  `python` package resolves its runtime here, once, and records it as
  `runtime` on every build cell of that package and in the manifest's
  `runtimes` map. With several `[[package]]` entries, every cell id is
  qualified as `<name>--<id>`.
- `build` — build every format of one build cell: `--cell` (required),
  `--version` (required), `--out` (default `dist/packages`),
  `--staging` (default `.vergil/package-staging`, wiped first),
  `--runtime` (the cell's recorded runtime; when empty or absent, the
  build resolves the runtime itself). Prints the runtime it chose.
  Needs `nfpm` on `PATH`.
- `check-runtime` — refuse (exit 1) when any `.deb`/`.rpm` of a
  `python` package in `--artifacts DIR` does not depend on exactly the
  runtime that `--manifest PATH` records for that package. Read-only.
  See [one runtime per package](package-config.md#one-runtime-per-package).
- `check-unpublished` — refuse (exit 1) when a release at `--version`
  (required) would publish a package with the same name, full version,
  architecture and format as one an earlier stable release of `--repo`
  (default `$GITHUB_REPOSITORY`) already published. Read-only; needs
  `gh`. This is the CD backstop for the same check `vrg-release` runs in
  preflight. See
  [the re-publish check](package-config.md#the-re-publish-check).
- `install-test` — in a clean container, install one test cell's
  artifact, verify shipped systemd units, run the smoke command and
  shim checks under a sanitized environment, remove the package and
  check for residue: `--cell`, `--artifacts DIR`, `--report PATH` (all
  required).
- `index` — build the signed apt/dnf repository site: `--config`
  (`packages.toml`), `--keys DIR`, `--out DIR` (all required), `--work`
  (default `.vergil/index-work`), `--base-url`. Needs
  `PACKAGE_SIGNING_KEY` and `PACKAGE_SIGNING_PASSPHRASE`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_package` |
| Args | Subcommand (`matrix`, `build`, `check-runtime`, `check-unpublished`, `install-test`, `index`) with the flags above |
| Preconditions | `vergil.toml` in the current directory (`index`: a `packages.toml`) |
| Failure mode | `ERROR:` diagnostic on stderr for config, package, or missing-file errors |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-sarif-filter

Write a copy of a SARIF file with every accepted-suppressed result
removed. The CI security composites upload the filtered copy to GitHub
code scanning, while the CI-evidence bundle keeps the unfiltered
original. See
[Suppressing a reviewed finding in source](../guides/ci-architecture.md#suppressing-a-reviewed-finding-in-source).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_sarif_filter` |
| Args | `INPUT` `OUTPUT` (positional; may be the same path) |
| Preconditions | `INPUT` is a readable SARIF file |
| Failure mode | Error on stderr when the input cannot be read or the output cannot be written |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-sarif-evaluate

Evaluate a SARIF file, or a directory of `.sarif` files, and report every
finding at the chosen severities as an error annotation.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_sarif_evaluate` |
| Args | `path` (positional), `--severity LEVEL...` (default `warning error`) |
| Preconditions | None |
| Failure mode | Exit 2 when the path does not exist |
| Exit codes | 0 no findings, 1 findings, 2 path not found |
| Status | Active |

### vrg-semgrep-scan

Run a semgrep scan with rulesets resolved for the language (plus Dockerfile
and workflow rulesets on request), write SARIF, and report findings. Used by
the CI security workflow.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_semgrep_scan` |
| Args | `--language` (required), `--target-dir`, `--output`, `--extra-config`, `--exclude-rule` (repeatable), `--has-dockerfiles`, `--has-workflows` |
| Preconditions | `semgrep` on PATH |
| Failure mode | Exit 2 when semgrep itself fails |
| Exit codes | 0 no findings, 1 findings, 2 scan failed |
| Status | Active |

### vrg-trivy-scan

Run a Trivy vulnerability scan of a filesystem or image, writing SARIF and
table output, report findings, and optionally write a CycloneDX SBOM.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_trivy_scan` |
| Args | `--type {filesystem,image}` (required), `--target` (required), `--output-dir` (required), `--trivyignore`, `--sbom`, `--severity` (default `MEDIUM,HIGH,CRITICAL`) |
| Preconditions | `trivy` on PATH |
| Failure mode | Exit 2 when trivy itself fails |
| Exit codes | 0 no findings, 1 findings, 2 scan failed |
| Status | Active |

### vrg-ecosystem-resolve

Resolve a language's package build command, publish command and credential
secret name. In CI it writes them as step outputs; otherwise it prints them.
Used by the registry-publish action.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ecosystem_resolve` |
| Args | `language` (positional) |
| Preconditions | None |
| Failure mode | Exit 1 for an unsupported language, naming the supported ones |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-resolve-tooling-version

Resolve the vergil-tooling install tag from `vergil.toml` and write it as
the `vergil_version` output.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_resolve_tooling_version` |
| Args | `--repo-root PATH` (default: the current directory) |
| Preconditions | `vergil.toml` in the repo root |
| Failure mode | Exit 1 for a missing or invalid `vergil.toml` |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-release-validate-inputs

Validate release workflow inputs: the language is supported,
`--registry-publish` is compatible with it, and `--container-tag` has an
image tag's shape. Used by the release validate-inputs action.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_release_validate_inputs` |
| Args | `--language`, `--container-tag`, `--registry-publish` |
| Preconditions | None |
| Failure mode | Each invalid input is reported as an error |
| Exit codes | 0 valid, 1 invalid |
| Status | Active |

### vrg-version-divergence

Compare the head branch's version with the main branch's and fail when it
was not bumped. Writes `status`, `head_version` and `main_version` outputs.
Omit the main version for a first release.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_version_divergence` |
| Args | `head_version` (positional), `main_version` (positional, optional) |
| Preconditions | None |
| Failure mode | Exit 1 when the versions are equal, 2 when the head version is empty |
| Exit codes | 0 bumped, 1 not bumped, 2 empty head version |
| Status | Active |

### vrg-freeze-refs

Rewrite internal action references in workflow YAML to a release tag, then
fail on any reference left unfrozen. Run at release time to pin internal
references to the release tag.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_freeze_refs` |
| Args | `--owner-repo` (required), `--tag` (required), `--scan-dirs DIR...` (default `.github/workflows actions`) |
| Preconditions | None |
| Failure mode | Each unfrozen reference is reported as an error |
| Exit codes | 0 success, 1 unfrozen references remain |
| Status | Active |

### vrg-ci-evidence

Harvest and bundle a release's CI evidence. `harvest` finds the CI run for
the release merge commit, downloads and validates its evidence into a
staging directory; `assemble` builds the manifest and tarball from that
staging directory with no network access; `bundle` does both. Used by the
release workflow.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ci_evidence` |
| Args | `harvest --repo --merge-sha --out-dir [--run-grace-secs]`; `assemble --staging --version --generated-at --out-dir [--sbom-file]`; `bundle --repo --version --merge-sha --out-dir [--sbom-file]` |
| Preconditions | GitHub App credentials for the target org (`harvest`, `bundle`) |
| Failure mode | A single-line error on stderr, never a traceback |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-docs-stage

Stage the changelog and release notes into the docs build directory,
linking each release page to its CI-evidence bundle. Used by the docs-stage
action.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_docs_stage` |
| Args | `--docs-dir` (required), `--releases-dir`, `--changelog`, `--repo OWNER/NAME` |
| Preconditions | The docs directory exists; GitHub credentials for evidence linking |
| Failure mode | Exit 1 when the docs directory is missing or `gh` is not authenticated |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-docs-patch-nav

Add the release version pages to the `mkdocs.yml` nav. Used by the
docs-stage action.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_docs_patch_nav` |
| Args | `--mkdocs-yml` (required), `--releases-dir` (required) |
| Preconditions | `mkdocs.yml` and the releases directory exist |
| Failure mode | Exit 1 when either path is missing |
| Exit codes | 0 success, 1 error |
| Status | Active |

## Removed in this audit

### st-list-project-repos (removed)

Entry point declared in `pyproject.toml` but the source module
`vergil_tooling.bin.list_project_repos` did not exist. Would crash
on import with `ModuleNotFoundError`. The underlying function
(`list_project_repos`) lives in `vergil_tooling.lib.github` and is
consumed by `vrg-ensure-label --owner/--project` mode.

### st-set-project-field (removed)

Entry point declared in `pyproject.toml` but the source module
`vergil_tooling.bin.set_project_field` did not exist. Would crash
on import with `ModuleNotFoundError`.

## Audit notes

### Precondition consistency

Most tools check preconditions explicitly and fail with clear
messages. Notable gaps:

- `vrg-submit-pr` does not validate that `gh` is on PATH before
  attempting `git push` and `gh pr create`. Failure surfaces as a
  subprocess error rather than a clear precondition message.
- `vrg-container-run` checks `GH_TOKEN` explicitly; `vrg-container-test`
  does not (it will fail inside the container when `gh` commands
  run without a token, but the error is less clear).

### Args style

Most tools use `argparse`. Two exceptions:

- `vrg-container-run` parses `sys.argv` manually with a `--` separator.
- `vrg-container-docs` parses `sys.argv` manually with subcommands.

Both are intentional: `vrg-container-run` passes everything after `--`
through to `docker run`, and `vrg-container-docs` has a simpler
interface than argparse would provide. No alignment needed.

### Exit code contract

- 0: success
- 1: check failure, validation error, or precondition violation
- 2: infrastructure error (used by `vrg-repo-profile`)

The 1-vs-2 distinction is not universal. Tools added before the
convention was established use 1 for all errors.
