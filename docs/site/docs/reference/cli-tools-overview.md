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
Freeze gate: refuses to add a commit on a branch that has been reported ready
(#1719); `vrg-pr-workflow unfreeze` is the way back (see the freeze in
[`vrg-pr-workflow`](#vrg-pr-workflow)).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_commit` |
| Args | `--type`, `--scope`, `--message` (all three required unless `--fixup`), `--body`, `--fixup COMMIT` (a `fixup!` commit of a branch-local commit; refused with the message flags), `--allow-empty` |
| Preconditions | Git repo, staged changes, not detached HEAD, not on protected branch, branch prefix matches branching model, issue number in branch name, not main worktree when `.worktrees/` present, branch not frozen (or unfrozen) |
| Failure mode | `SystemExit` with diagnostic on stderr for each check |
| Exit codes | 0 success, 1 rejection or error |
| Status | Active |

### vrg-reword

Reword a branch-local commit's message through a scripted, non-interactive
rebase, re-stamping the new message through the same standards path as
`vrg-commit`. It is the agent-safe way to correct a commit message on the
current branch: `vrg-git commit` is denied, `vrg-commit` only creates new
commits, and interactive rebase is allowed only as `rebase -i --autosquash`,
which cannot reword. It is bounded:

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
`symbolic-ref`, denied subcommands (`commit`, `config` and others; use
`vrg-commit` for commits), a flag deny-list (`push --force`, `branch -D`,
`rebase -i` without `--autosquash`), the worktree convention, the
`<type>/<N>-<slug>` branch form for an open, non-epic issue, and the
report-ready freeze. It protects origin, not the local checkout: `reset`,
`clean`, a pathspec `checkout` and `rebase -i --autosquash` run only on a
branch that is neither protected nor frozen, and fail closed when the
PR-workflow state cannot be read. Remote operations run with the GitHub App installation
token for the resolved identity (see [`vrg-whoami`](#vrg-whoami)). It is not
a drop-in replacement for `git`; `vrg-git --help` lists the full policy.

The global `-C <path>` option (repeatable, as in git) changes directory
before any check runs, so every check, and the org of the identity token,
applies to the `-C` repo exactly as after a `cd`. No other global git option
is accepted.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_git` |
| Args | `[-C <path>]... <subcommand> [args...]`, forwarded to `git` |
| Preconditions | Git on PATH |
| Failure mode | A refused command prints the reason on stderr |
| Exit codes | 0 success; 1 refusal; 2 no subcommand or a `-C` with no path; 128 a `-C` path that cannot be entered; otherwise git's exit code |
| Status | Active |

### vrg-gh

A safe, identity-aware `gh` wrapper for agent sessions. It forwards to `gh`
only after enforcing a per-identity subcommand allowlist, injecting the GitHub
App installation token for the target owner, read from every form the command
names it in (`-R`/`--repo`, `--owner`, a positional repo, owner or URL, a
search qualifier, an `api` endpoint), and refusing a command that names more
than one owner (see
[Credential management](../guides/credential-management.md)). Raw `gh issue create`, `gh auth`,
`pr close`, `repo edit/create/delete` and `workflow run` are denied outright;
`gh api` is full for a human, read-only `GET` for the audit identity, and
denied for the user identity. Its own `run job-log --repo <owner>/<repo> --job
<job-id>` prints one Actions job's log from the read-only per-job log endpoint,
for every identity, with terminal control sequences stripped (see
[`run job-log`](../guides/permission-model.md#run-job-log)). `vrg-gh --help`
lists the allowlist.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_gh` |
| Args | `<subcommand> <action> [args...]`, forwarded to `gh` |
| Preconditions | `gh` on PATH; identity resolved via `vrg-whoami` |
| Failure mode | A refused command prints the reason on stderr |
| Exit codes | 0 success; 1 refusal; 2 no subcommand or bad `run job-log` arguments; otherwise gh's exit code |
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
Every mode driven by a ready-state opens the PR only at the `head_sha` the
latest `report-ready` recorded and refuses rather than pick another head
(#3815): a local worktree's HEAD must be that commit, a named branch reopened by `unfreeze` and not
re-reported is refused, a local ready-state and relay ref that disagree are
refused, and origin's tip is checked against the recorded head right before
`gh pr create`.
A ready-state that hands off a batch (several issues, epic
vergil-project/.github#1019) runs the per-issue refusals on every issue and
writes one `Closes` line per issue. It re-checks the batch hard rules and the
one-commit-per-issue invariant against a freshly fetched target, at the head
the PR opens at, before any push or `gh pr create`. CLI mode stays
single-issue.
A single-issue PR whose issue is an epic's documentation task also closes the
epic's planning issue, on a `- Planning issue: Closes <ref>` line of its own;
when it cannot, it says why (see
[the planning issue of a docs task](dev/submit-pr.md#the-planning-issue-of-a-docs-task)).
It refuses an issue whose native parent is an intake, planning or deferred
epic (see [`vrg-pr-workflow`](#vrg-pr-workflow)). Human-run: agent
identities are refused, and agents hand off with
`vrg-pr-workflow report-ready` instead.

Every mode takes one path (epic vergil-project/.github#1218): one
table of the selection (no confirmation prompt and no PR-body preview),
then for each PR the push, `gh pr create`,
the provenance check (a violation always fails that PR's submit), and GitHub
auto-merge turned on, pinned to the pushed head (squash for a single-issue PR, rebase for a batch PR; a PR that can
already merge is merged directly). PRs open against `develop` only, and
submit never rebases a branch. After the table the PRs open as a
progress rollup, one stage per PR named after its branch, like
`vrg-release`'s: each ends on one ✓/✗ line with its outcome, and the detail
goes to `.vergil/vrg-submit-pr-<stamp>.log`. A PR whose submit fails is
named in the summary and the others go on. Then one `vrg-finalize-pr <pr>,<pr>,…`
always watches every opened PR merge and settles `develop` once.
`--finalize`, `--no-finalize`, `--install`, `--no-install` and `--yes` are
removed and refused by name. See [vrg-submit-pr](dev/submit-pr.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_submit_pr` |
| Args | positional `branches` (relay mode); or `--issue` (required), `--summary` (required), `--linkage` (default: Ref; `vrg-submit-pr` auto-selects `Closes` for a managed task — `Fixes`/`Resolves` are banned), `--notes`, `--title`; `--all`/`--select` (from the repo root), `--base` (`develop` only), `--release`, `--dry-run`, `--output-window`, `--output-format`. `--finalize`, `--no-finalize`, `--install`, `--no-install`, `--allow-provenance-violation` and `--yes` are removed and are an error |
| Preconditions | Git repo, `gh` CLI on PATH; relay mode: named branches on origin with a resolvable ready-state and matching `head_sha` |
| Failure mode | A PR whose push, `gh pr create`, provenance check or auto-merge fails is named in the summary and the rest go on; loud refusal, with nothing opened, on a missing ready-state, a state that is not ready, any head other than the recorded `head_sha`, a base other than `develop`, or a removed flag |
| Exit codes | finalize's: 0 full success, 3 not every PR merged (at least 3 when a submit failed), 4 develop did not validate, 5 the release failed; 1 a usage error, a refusal, or a tool error from finalize |
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
commits, commit rewrites and pushes, and `vrg-git` refuses its local rewrites
(`reset`, `clean`, a pathspec `checkout`, `rebase -i --autosquash`), because a task is exactly one PR and more work is a new
follow-up issue (epic #146). The freeze lasts until `vrg-submit-pr` marks the
state submitted; from then on the open PR's branch takes fixes, rebases and
lease pushes again (the `pr-watch` skill). `report-ready` pushes the branch itself, to
`origin/<branch>` with upstream set, **before** recording anything (#3602),
so no separate push is needed; a branch already on origin at that commit is a
no-op push. When the delta touches `.github/workflows/**` GitHub refuses the
agent push: `report-ready` reports that as expected, records the state anyway,
and the human's `vrg-submit-pr` pushes the branch. A stale branch, whose own
commits change no workflow file but whose tree carries an outdated one, gets
the same refusal: `report-ready` tells it apart as `vrg-git push` does and
fails with the rebase steps, recording nothing (#4077). Any other push failure
fails `report-ready` with no local state and no relay ref written. A branch
with no commits ahead of `--base` is refused before any push, with nothing
recorded and no freeze (#3870). A re-run on
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
cannot be unfrozen. `unfreeze` first overwrites the relay ref with the
reopened state, so no `vrg-submit-pr` can still read the branch as ready at
its old head, and fails with nothing changed if that push fails (#3815).
`status` prints the current state. `report-ready` (like
`vrg-submit-pr`) refuses an issue whose native parent is an intake, planning
or deferred epic: a queue item is not buildable until it is routed with
`epic-review intake`, expanded with `epic-create`, or revived with
`epic-review deferred` (epics vergil-project/.github#176 and #508).
Local-settings guard: `report-ready` refuses an agent (warns a human), before
pushing or recording anything, while a loadable `.claude/settings.local.json`
is not absent or `{}` (epic vergil-project/.github#558).
A batch PR (epic vergil-project/.github#1019) repeats `--issue`, in commit
order. Every issue runs the per-issue refusals, then the batch hard rules and
the one-commit-per-issue check run before any push; either failing records
nothing. The state records the list as `issues` (its first entry is also
`issue`), in the local file and the relay ref alike, and a re-run must name the
same list in the same order. Once the state is recorded, the members' batch
markers are removed and `report-ready` prints
`{"ok": true, "status": "ready", "issues": […]}`. The printed `issues` are this
repo's `#N` refs, the form `batch-start` and `batch-show` print, whatever form
`--issue` took; the recorded `issues` keep each value as given (`N`, or
`owner/repo#N`), the form `vrg-submit-pr` reads. A single `--issue` records no
`issues` key, prints none, and behaves exactly as before.

When the single `--issue` is an epic's documentation task, the response also
previews what its PR does with the epic's planning issue, as `planning_close`:
`{"closes": "<ref>"}` when the PR will close it,
`{"planning": "<ref>", "skipped": "<why>"}` when it will not, or
`{"unknown": "<why>"}` when the check could not be read. `vrg-submit-pr` makes
the check again when it builds the body (see
[the planning issue of a docs task](dev/submit-pr.md#the-planning-issue-of-a-docs-task)).
Any other issue, and a batch, prints no `planning_close`.

The batch hard rules are: 2 to 8 issues, none listed twice, every one in
this repo, a task (not an epic, archive or queue item), not operational, not
directly Blocked-by an open deployment task and opened by a trusted author,
and all with the same parent epic (a finite epic or the repo's ad-hoc epic).
The one-commit-per-issue check holds the branch, from its base, to exactly
one commit per issue in the listed order, each subject ending in a space and
its issue's `(#N)`, with no merge commit and no unfolded `fixup!` commit.
Both fail closed: a read that fails refuses.

The batch marker (`build/.vergil-state/issue-implement/<N>/batch` in the main
clone, one per member, holding the ordered list and the batch's fixed base,
`base_sha`) is managed by three subcommands, run from the main clone. Each
takes `--issue` as `N`, `#N` or `owner/repo#N` for this clone's repo:

- `batch-start --issue <N> --issue <M> …` runs the hard rules, then writes
  every member's marker with the `--base` commit as `base_sha`, printing
  `{"ok": true, "resumed": <bool>, "issues": […], "base_sha": "<sha>"}`. A
  refusal writes nothing. The same list over existing markers is a resume
  that changes nothing; any other existing marker is refused.
- `batch-show --issue <N>` prints the issue's batch as one JSON object,
  `{"issues": ["#10", "#11"], "base_sha": "<sha>"}`, and exits 0, exits 1 with nothing on stdout when the issue is in no batch, and
  exits 2 on any other failure.
- `batch-extract --issue <N>` rewrites every other member's marker without
  the issue (removing the last one's when fewer than two remain) and removes
  the issue's whole `issue-implement/<N>/` directory, its `merge-review/`
  state included. It refuses while a `security-*.md` details file is under
  that directory.

`batch-check --pr <url> [--head <sha>]` checks a submitted batch PR before a
push to it, from a clone of the PR's repo (pr-watch runs it on the head it is
about to push). It reads the PR's body and base. A PR whose body has one
`Closes` line or none is not a batch PR and passes. A batch PR's `Closes` refs
must be exactly the issues GitHub lists as its closing references, and its
branch must hold one commit per issue, in the body's order, from the base as
fetched now to `--head` (default: the PR's head, fetched by its SHA so the
branch's remote-tracking ref never moves). The hard rules are not run. It
writes nothing and makes no freeze check, so it runs on a frozen branch. It
exits 0 when the PR passes or is not a batch PR, and 1 with the rule's message
otherwise.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_pr_workflow` |
| Args | `report-ready` (`--issue`, repeatable for a batch, `--title`, `--summary`, `--notes` required; `--linkage` default Ref), `unfreeze`, `status`, `batch-start`, `batch-extract`, `batch-show`, `batch-check` (`--pr` required, `--head`); global `--base` (default: origin/develop) |
| Preconditions | Git repo; run from the worktree whose branch is being staged |
| Failure mode | `WorkflowError` on stderr (e.g. unfreeze after submit, nothing to unfreeze, or a relay ref unfreeze could not update) |
| Exit codes | 0 success, 1 error |
| Status | Active |

### vrg-pr-await

Block until a pull request submitted with auto-merge on merges, or can no
longer merge as it stands, then print the outcome as JSON. Used by the
`pr-watch` skill. A PR that merged before the wait started counts as merged.
A new review or a new commit does not end the wait: a review after submit
never holds the merge, and auto-merge stays pinned to the head submit pushed.

On exit 0 or 1 it prints one JSON object with `pr`, `outcome` (`merged` or
`cannot_merge`), `reason` (`null` when merged), `detail`, `head_sha`,
`merge_commit` (`null` unless merged), `merge_state_status`, `failed_checks`
and `orphaned_checks`. On exit 1 the reason is also printed on stderr. The
reasons are:

- `check_failed`: a required check concluded in failure; `failed_checks`
  names it. A failed or cancelled check-run of a workflow run that a newer
  run of the same workflow superseded on the PR's head does not count.
- `check_cancelled`: a required check was cancelled, and no newer run of its
  workflow replaced the cancelled run within two minutes; `failed_checks`
  names it.
- `conflict`: the PR has a merge conflict with its base.
- `auto_merge_off`: auto-merge is off, at the start or after a push.
- `closed`: the PR was closed without merging.
- `orphaned_check`: every pending check is stuck after its workflow run
  completed, for longer than `$VRG_ORPHAN_GRACE` (default 300s);
  `orphaned_checks` names them.
- `timed_out`: the deadline passed with the PR still pending. The deadline is
  `$VRG_FINALIZE_DEADLINE` seconds, default 7200, as for `vrg-finalize-pr`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_pr_await` |
| Args | `pr` (positional, URL or number). `--since-sha` and `--since-reviews` are removed and are an error |
| Preconditions | GitHub App credentials for the PR's org |
| Failure mode | Exit 1 only when the PR cannot merge. A malformed `$VRG_FINALIZE_DEADLINE` or `$VRG_ORPHAN_GRACE` (checked before any waiting), a GitHub API or permission failure, an unreadable PR or bad arguments exit 2 with a message on stderr and nothing on stdout |
| Exit codes | 0 merged, 1 cannot merge, 2 any other error |
| Status | Active |

### vrg-pr-review-comments

List a pull request's inline (line-level) review comments. It reads
`GET repos/{owner}/{repo}/pulls/{n}/comments`, every page, through
`lib/github.py`'s paginated read helper, with the App token minted for the
`--repo` owner. `vrg-gh pr view --json reviews,comments` returns only review
bodies and conversation comments, and `vrg-gh` refuses `gh api` for the user
identity, so this is the user agent's way to read them. It is strictly
read-only and widens no `vrg-gh` allowlist. The plugin's
`epic-retrospective` sweep consumes it.

Without `--json` it prints one block per comment (`<path>:<line> by
<author> at <created_at> (comment <id>, review <id>, reply to <id>)`, the
comment URL, then the body indented; `:<line>` is left out when `line` is
`null`), or one line saying the PR has none.
`--json` prints a JSON array of objects with the keys `id`, `author`,
`path`, `line`, `review_id`, `in_reply_to_id`, `created_at`, `body` and
`url`, in API order. `line` is `null` for a comment not on a current line of
the diff (a file-level comment, or an outdated one on a line the PR no longer
has), `author` is `null` for a deleted account,
`review_id` is `null` when GitHub gives none, and `in_reply_to_id` is
`null` for a comment that starts a thread.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_pr_review_comments` |
| Args | `--repo <owner>/<repo>`, `--pr N`, `--json` |
| Preconditions | GitHub App credentials for the `--repo` owner |
| Failure mode | A GitHub API failure, or a response that is not a list of well-formed comments, prints `ERROR:` on stderr and exits 1; it never prints a partial or empty list in its place |
| Exit codes | 0 success, 1 GitHub API failure or malformed response, 2 bad arguments |
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
bump, rolling-tag promotion, the delivery of failed CD publish jobs
(`cd-publish`, which a `--resume` re-runs), the package-index wait,
cleanup, the `update-deps` stage, which runs the dependency update on the
new `develop` version, the install, which runs the `[publish].install`
commands once `promote` has moved `vX.Y` to the release, and finally the
close of the tracking issue, which any failed stage holds back, so a
`--resume` can finish the release. On a fresh start, preflight refuses while any
`release: X.Y.Z` tracking issue is open, naming its recovery, and while an open PR
carries the `dependency-update` label, after first closing any whose fix
task completed. Preflight then prints the release's contents: the
pending-release table and the issues its `feature/` PRs close by a
trusted closing reference. The tracking issue's body carries those
contents and its state block. The issue is locked, and its state is
trusted only while every identity that authored, edited, closed or
reopened it is a `human` login of `vrg-trusted-authors`. Human-only. See the [vrg-release reference](dev/release.md)
and the [Releasing guide](../guides/releasing.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_release` |
| Args | `minor`/`major` (optional positional), `--without-deps`, `--allow-pending-deps`, `--resume [X.Y.Z]`, `--skip-audit`, `--output-window`, `--output-format`. `--no-promote`, `--no-install` and `--install` are removed and are an error |
| Preconditions | Human identity; on `develop`, clean working tree, local develop matches `origin/develop`; `gh` authenticated and a reachable container runtime (docker or nerdctl) for `vrg-container-run`, which runs `git-cliff` |
| Failure mode | Fail-fast stages stop the run; deferred stages (including `update-deps`) record the failure, let later stages run, and are summarized at the end |
| Exit codes | 0 success, 1 any failure or refusal, 130 interrupted |
| Status | Active |

### vrg-promote

Move the rolling `vX.Y` tag to release `vX.Y.Z`. It is human-only: it
refuses any agent identity, failing closed on any agent signal
(`is_agent() or any_agent_signal()`), once its arguments are parsed and
before any other output or git call, so `--help` still prints the usage.
`vrg-release`'s promote stage uses the same library and the same refusal.
The refusal stops an agent from running it by mistake; it is not a security
boundary, because the identity signals come from the caller's environment.
The `Tag protection` ruleset on GitHub, which only the repository Admin
role and the org automation App bypass, is what enforces who can move the
tag.

The target must be an existing `vX.Y.Z` tag on origin whose commit is
merged work and that has a published (non-draft) GitHub Release; any other
target is refused. Merged work is a commit on origin's `main`, or that
release's freeze commit, the shape vergil-actions' CD tags: a commit with
exactly one parent, that parent on origin's `main`, the subject
`chore(release): freeze internal refs to vX.Y.Z`, and a change from the
parent in which every file is a regular file whose new content is exactly
what `vrg-freeze-refs` makes of the old one for origin's repo and that tag,
with no file added, deleted, renamed or re-moded. A refusal names the rule
that failed. origin's push URL must name the same GitHub repo as
its fetch URL. Every run, `--dry-run` included, first runs
`git fetch --tags --force origin +refs/heads/main:refs/remotes/origin/main`,
so the checks run against origin's current state. That fetch overwrites
local tags with origin's.

It prints the origin repo and the tag move, for example:

```text
origin: vergil-project/vergil-claude-plugin
v2.1 1a2b3c4d5e6f -> v2.1.50 fc17882a9b0c
```

`--dry-run` prints the same lines, after the same target check, and stops
(with `--yes` too). `--yes` prints them and moves the tag with no prompt, so
it needs no terminal and can run from a script. Otherwise it prints them to
stderr, asks `Move vX.Y on <repo>? [y/N]` there, and moves the tag only on
`y` or `yes`; the move and the prompt go to stderr so a redirected stdout
cannot hide them. It refuses to prompt unless both stdin and stderr are
terminals: with no terminal to confirm on, it refuses and names `--yes`.
Before it pushes it re-resolves the move and refuses if anything changed
since it was confirmed, and resolves the local release tag once and checks
that it is the confirmed commit. It then pushes that tag object, by its id,
to the rolling tag's ref
as a compare-and-swap (`--force-with-lease` on the rolling tag's value on
origin, empty when the tag is new), so it overwrites exactly the tag that
was shown, or is refused. The local rolling tag is updated only after origin
accepts the move. When the rolling tag already points at the release, it
prints only the "already promoted" line, asks nothing and pushes nothing.

When `vrg-release`'s promote stage calls it, the GitHub Release read retries
a 404 for a short budget, since that Release was just published. A version
given to `vrg-promote` is read once, so a missing Release is refused at once.
The read names `github.com/<owner>/<repo>`, the host origin was checked
against. A missing GitHub token, or GitHub App credentials that cannot give
an installation token, print the same guided message as other `vrg-*`
commands; any other failed GitHub or git call prints `Error: …`. If a push
fails, it reads origin's rolling tag back: when origin holds the release (an
earlier attempt landed before the connection dropped), the move is reported
as done.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_promote` |
| Args | `version` (positional, default: the `VERSION` file), `--dry-run`, `--yes` |
| Preconditions | Human identity; run in the repo's checkout; a terminal to confirm on, unless `--yes` is given; push access to origin |
| Failure mode | Exit 1 with a message on stderr. An agent identity, a run with no terminal and no `--yes`, a declined or unanswered confirmation, and a missing `VERSION` file each print their own `vrg-promote:` or plain message; a refused target, a move that changed since it was confirmed, an unreadable `VERSION` file, and a failed git or GitHub call print `Error: …`; a missing GitHub token or unusable GitHub App credentials print the guided `vrg-promote:` message |
| Exit codes | 0 success (including "already promoted" and `--dry-run`), 1 refusal or error |
| Status | Active |

To roll a release back with `vrg-promote`, follow the
[rollback runbook](../guides/releasing.md#rolling-back-a-release).

### vrg-version

Print or bump the repo's version. `show` prints the current version
(`--major-minor` for `X.Y` only, `--ref` to read it from a git ref); `bump`
increments `patch` (default), `minor` or `major` and prints the new version.
For a Python or Rust repo, `bump` then refreshes the lockfile (`uv lock`,
`cargo update --workspace`) in the dev container, through
`vrg-container-run`, so no host toolchain is needed. A bump is all or
nothing: when any step fails, the lockfile refresh included, `bump` puts
`VERSION`, the language manifest, the secondary version files and the
lockfile back as they were, says so on stderr and exits non-zero. Used by
the CD workflows to read the release version.

In a TypeScript repo the version is also kept in `package.json`. `show`
fails, naming the fix, when `package.json` is missing, has no `version`, or
holds another version than `VERSION`; `vrg-validate`'s TypeScript pre-gate
makes the same check on the PR. `bump` rewrites only the top-level `version`
of `package.json` and the two root version fields of `package-lock.json`
(its top-level `version` and `packages[""].version`), keeping every other
byte. It runs no npm, so no script runs and no dependency moves. Before
writing any file, `bump` fails, naming the file and the fix, when either
file is missing, `package.json` does not hold the current version, or the
lockfile lacks a root version field; a file it cannot write fails the bump.

In a Rust repo the version is read from `[package].version` when it is a
string, and from `[workspace.package].version` in a virtual workspace or
when `[package]` inherits it (`version.workspace = true`). `bump` rewrites
the key in that table only, never a `[workspace.dependencies]` entry, and
members that inherit the version follow it. A `Cargo.toml` with neither
version fails, naming both tables. Before writing any file, `bump` also
fails, naming each entry, when the new version would not satisfy a
versioned path dependency on a bumped workspace member (for example `0.1.3`
to `0.2.0` under `^0.1.3`).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_version` |
| Args | `show [--major-minor] [--ref REF]`, `bump [{patch,minor,major}]` |
| Preconditions | Run from the repo root; `bump` in a Python or Rust repo needs a running container runtime (a TypeScript repo does not) |
| Failure mode | An unreadable version raises an uncaught error; for a TypeScript repo's `package.json` or `package-lock.json`, its message names the fix |
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
applicable updaters (`python`, `cargo`, `npm`, `vergil`, `images`, then the repo
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

Run the repo's `[upstream-refresh].hook` in a fresh clone of
`develop`, check the patch against the repo's path fence
(`[upstream-refresh].paths`), and open an
`automation/upstream-refresh/<run-id>` PR that merges itself once CI
passes. It runs in two halves split by the key boundary: `prepare`
runs the hook with no credentials and writes a patch; `publish` holds
the automation App's key, applies the patch on the recorded `develop`
commit in its own clone, checks the fence, commits as the App's bot,
and waits until the PR merges, renewing and revoking its token as it
goes. A no-change run still closes an earlier run's open PR. With no
mode, both halves run in one process for a human's local run.
`--dry-run` runs prepare and the fence and pushes nothing. It never
touches the caller's checkout, and never runs dependency updaters,
validation or a release. See the
[upstream-refresh reference](upstream-refresh-config.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_upstream_refresh` |
| Args | `prepare --out DIR`, `publish --from DIR`, or no mode (prepare and publish in one process); `--repo OWNER/NAME` (default: `$GITHUB_REPOSITORY`, then origin); `--dry-run`, with `--rules-from PATH` to read the fence rules from a branch's `vergil.toml` |
| Preconditions | `[upstream-refresh]` in `vergil.toml` on `develop`. Agent identities may run `--dry-run` only. Under GitHub Actions: publish needs `VRG_AUTOMATION_APP`, `VRG_AUTOMATION_CLIENT_ID` and `VRG_AUTOMATION_PRIVATE_KEY`; prepare refuses them; the combined mode is refused |
| Failure mode | A hook failure, untracked files, a patch that does not apply, a recorded commit no longer on `develop`, or a fence violation stops the run before any push, naming the cause on stderr |
| Exit codes | 0 merged or nothing to change, 1 failure or refusal (usage errors included), 2 PR opened but not merged (PR and reason named), 130/143 interrupted |
| Status | Active |

### vrg-finalize-pr

Watch a batch of auto-merging pull requests, then settle `develop` once
(epic vergil-project/.github#1218). It never merges a PR: `vrg-submit-pr`
turned auto-merge on, and GitHub merges each PR once its required checks
pass. The watch follows every listed PR until it has merged or failed (CI,
a conflict, closed, auto-merge off, provenance, not a develop PR), or the
batch deadline (`$VRG_FINALIZE_DEADLINE`, default 7200s) passes. If any PR
merged, the settle phase closes any issue GitHub left open, then, under the
checkout lock, switches to the target branch, fast-forwards it, deletes the
merged PRs' branches and worktrees and other merged local branches, deletes
each merged branch's PR-workflow relay ref
(`refs/vergil/pr-workflow/<branch>`) and sweeps any orphaned relay ref
whose branch is gone (issue #2369), prunes remotes, provisions the dev
image and runs one validate. It then reports the synced head's CD run
without waiting (`--wait-cd` waits), lists every PR as merged or not
merged with the next step, and, with `--release`, chains into
`vrg-release` on full success. `--cleanup-only` runs the settle phase with
no PRs. It also removes each merged branch's
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
A batch PR (more than one `Closes` line, one commit per issue, epic
vergil-project/.github#1019) is watched like any other: `vrg-submit-pr`
turned its auto-merge on with the rebase method, so `develop` gets one
commit and one changelog entry per issue. See
[vrg-finalize-pr](dev/finalize-pr.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_finalize_pr` |
| Args | `PR` (a number or URL, or a comma list of them; optional), `--all`, `--cleanup-only`, `--target-branch` (default: develop), `--wait-cd`, `--release`, `--clean-dirty`, `--dry-run`, `--yes`. `--strategy`, `--no-wait-cd`, `--skip-post-checks`, `--install`, `--no-install` and `--allow-provenance-violation` are removed and are an error |
| Preconditions | Must run from the main worktree, `vrg-container-run` on PATH |
| Failure mode | An unmerged PR exits 3 and a develop that did not validate exits 4; a watch that cannot read a PR, a failed close of an issue, a failed sync or cleanup, or a dirty working tree exits 1 |
| Exit codes | 0 full success, 1 a tool or usage error, 3 develop healthy but not every PR merged (or one merged with a provenance violation), 4 develop did not validate (with `--wait-cd`, or CD did not succeed), 5 the chained release failed |
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
`gh issue create`. `--epic` takes `adhoc`, `intake`, `planning`, `hoshin`
(the org's hoshin epic, always in `<org>/.github`), `#N` (read against the
target repo's epic home) or `owner/repo#N`. `--kind` stamps an
operational (`validation`, `deployment`) or `retrospective` label and body
scaffold, and `--blocked-by` stamps a `Blocked-by:` reference. With
`--kind validation` or `--kind deployment`, `--body-file` fills the scaffold
from a file's `## Intro`, `## Preconditions`, `## Procedure` and
`## Acceptance` sections (all required) and, for deployment only, an optional
`## Human-run steps`; the scaffold keeps its frame. `--kind retrospective`
refuses `--body-file`. If the link
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
The sanctioned path for creating epics, used by the `epic-create` skill.

The epic also gets its **category label**, derived from its other labels:
`epic-management` for an intake, planning, deferred or hoshin epic (`--label
intake` and so on), `epic-development` for a plain epic, and nothing more for
an ad-hoc epic, whose `ad-hoc` label is its category label. Every label is
created in the home from the label registry before the epic is. It refuses,
creating nothing, a `--label epic-development` or `--label epic-management`
(the category label is never given by the caller) and labels that mark two
categories at once (for example `--label ad-hoc --label proxy`). The category
labels are for display only: no tool reads them to decide behaviour.

`--proxy-for OWNER/REPO#N --repo OWNER/NAME` instead ensures the **proxy
epic** for a development epic in another org (its primary): an epic in the
target's `<owner>/.github`, titled `Epic (proxy): OWNER/REPO#N — <primary
title without "Epic: ">` and labelled `epic` + `proxy`, whose body carries the
line `Primary: OWNER/REPO#N`. It then posts a comment on the primary carrying
`Proxy: <owner>/.github#<N>`, alone on its line. An open proxy for the primary
is reused and a closed one is reopened (it prints which). A reused or reopened
proxy whose title differs from the derived one is retitled, so a re-run brings
an older proxy up to date; a proxy is found by its `Primary:` line and labels,
never its title. The comment is posted only when no trusted comment on the
primary already names the proxy, so a re-run on an up-to-date proxy writes
nothing. It refuses, before any write, a primary in the
target's own org, a primary that is not an open development epic (a standing
epic, a proxy, an archive or a closed epic), and a private primary repo,
target or `.github`: a proxy exists only between public repos, and the task
must not be filed in an ad-hoc epic instead. It reads and writes each org
under that org's App token. The title and body are derived, so it takes no
`--title`, `--body`, `--body-file`, `--label` or `--assignee`.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_create` |
| Args | `--title` (required unless `--proxy-for`), `--body` / `--body-file`, `--repo OWNER/NAME`, `--label` (repeatable), `--assignee` (repeatable), `--proxy-for OWNER/REPO#N` (needs `--repo`) |
| Preconditions | GitHub App credentials for the target org (with `--proxy-for`, for both orgs) |
| Failure mode | Error on stderr; a refused label set writes nothing, a failed label create creates no epic, and a `--proxy-for` refusal or link error writes nothing |
| Exit codes | 0 created (or, with `--proxy-for`, ensured), 1 error or refusal |
| Status | Active |

### vrg-epic-docs

Write an epic's **Documents** section, the marked block at the top of its body
that links the epic's spec, plan and retrospective or notes why one does not
exist (see
[The Documents section](../standards/github-issues.md#the-documents-section)
for the format and the entry rules). It has two subcommands.

`set --epic OWNER/REPO#N` adds the section, or updates it in place;
an entry not named keeps its value, and a new section starts all `pending`.
The issue must carry the `epic` label. A `PATH` is in the epic's home repo,
or `<repo>:<path>` for another repo of the same org (an
`<owner>/<repo>:<path>` value naming the epic's owner is refused); the
file's existence is not checked. A path is repo-relative, with no leading
`/`, whitespace or backtick, and a repo name uses only letters, digits, `.`,
`-` and `_`. Hand-written document lines elsewhere in the body are removed,
and a body that is already correct is not edited. The skills run it when an
epic is created and at each docs PR handoff.

`backfill (--org ORG | --repo OWNER/REPO)` writes the section on every epic
of an epic home, open and closed: `--org` works on the org's `.github`, and
`--repo` on a private repo that keeps its own epics. It skips the standing
epics (ad hoc, intake, planning, deferred, hoshin, the retired `standing`
label, and titles starting `Epic (standing):`), proxy epics and `[archived]`
epics. For each epic it
looks for the documents, in order:

1. the directory `epics/<N>-*/` in the home repo's default branch;
2. otherwise, an `epics/<dir>/` the body mentions that exists;
3. otherwise, `docs/specs/`, `docs/plans/`, `docs/superpowers/specs/` or
   `docs/superpowers/plans/` paths the body mentions, in the home repo or
   another repo of the same org (`<repo>/docs/…` or `<owner>/<repo>/docs/…`),
   that exist. A file under a `plans/` directory is the plan, any other the
   spec.

In a directory, `spec.md`, `plan.md` and `retrospective.md` are the
documents; the `epics/archive/` directory is never read. A link already in
the section is kept, so only entries that are still notes are decided, by
the entry rules. Two candidate directories that each hold a document still
to be decided, or two candidate specs or plans, make the epic **need a
decision**: it is named and left unchanged, for a human to settle with
`set`. Hand-written `Documents:`, `Spec:`, `Plan:` and `Retrospective:`
lines (and a bracketed note line after one) are removed. Without `--apply`
it is a dry run: per epic it prints whether it would add, change or leave
the section, the section itself and each line it would remove, then a
summary count. With `--apply` each body is read again before its documents
are looked for, so an edit made since the listing is kept; an edit made
while that epic is being worked on is not. A failed epic is named and the
run carries on.

Both subcommands run every read and write under the epic owner's App token.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_docs` (format, parser and discovery: `lib/epic_docs.py`) |
| Args | `set`: `--epic` (required), `--spec PATH`, `--plan PATH`, `--retrospective PATH`; `backfill`: `--org ORG` or `--repo OWNER/REPO` (one required), `--apply` |
| Preconditions | GitHub App credentials for the epic's org |
| Failure mode | Error on stderr; for `set`, an issue that is not an epic, a bad `PATH`, or a malformed section (half a section, a bad entry) is refused with nothing edited. For `backfill`, a failed listing edits nothing; an epic that needs a decision or fails is named and left unchanged while the rest run |
| Exit codes | 0 written or already correct, or (`backfill`) no epic needs a decision or failed, including a dry run with changes still to make; 1 error, or any epic that needs a decision or failed; 2 bad arguments |
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
`adhoc`, `intake`, `planning`, `hoshin`, `#N` or `owner/repo#N`, read against
the task's repo. Moving a task already under the target is a no-op. The deferred
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

### vrg-epic-rollup

Close a just-closed task's parent epic when the epic is finite and all its
tasks are closed. A no-op for any other issue (a standing epic such as
`ad-hoc` never auto-closes, and a
[proxy epic](../standards/github-issues.md#proxy-epics) stays open until its
primary closes), so it is safe on every issue close. Run by the `epic-rollup`
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
| Preconditions | GitHub App credentials for the target org; `--all-in` also needs an identity that sees every repo in ORG |
| Failure mode | Error on stderr; `--all-in` refuses before any read when the identity may not see every repo in ORG |
| Exit codes | 0 success, 1 error |
| Status | Active |

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
deferral or an estimated epic size — so no review skill hand-rolls them. Routes:

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
  named epic. It refuses the hoshin epic: `--epic hoshin` before anything
  resolves (so nothing is created), and a ref that names a hoshin epic. A planning item's `in-progress` label is removed from the moved
  issue once the move has settled; a recreated issue never carries it. A
  one-off also ends the item's estimated size, from intake, planning or
  deferred alike: a recreated issue never carries an `epic-size:*` label, and
  a transferred or same-repo issue has every one removed once the move has
  settled. When the item carried a size (an `epic-size:*` label, or any
  `Sized …` comment), the transferred or same-repo issue then gets the
  marker `Size cleared <YYYY-MM-DD>: moved out of planning as a one-off.`,
  so the sizing comments it carries no longer count. This needs no trusted
  authors configured. If a step fails after the move, the command fails
  loudly, naming the moved item and the by-hand fix for each step not done.
- `planning` — reparent the item onto the planning epic of the same queue
  home. From an ad-hoc epic it takes a task found too big for one PR straight
  to planning, with no stop in the deferred queue; like `defer`, it is
  refused while an open PR would close the item. It also takes a **follow-on brainstorm** out of a development epic: a
  task titled `Follow-on brainstorm: …` whose native parent is a development
  epic. A brainstorm never gates its epic's work, so moving it suspends
  nothing. The parent is replaced in one step, then the hand-off is recorded
  with a comment on the epic, for the retrospective's §5, and one on the item:

  ```text
  Follow-on brainstorm handed to planning: <item-ref> (now under <planning-epic-ref>)
  Handed to planning from: <epic-ref>
  ```

  If a record fails after the move, the command fails loudly and prints every
  comment still to post by hand.

  `planning [--tasks N [--confidence high|med|low] --reason TEXT]` also sizes
  the item once it is in planning, as `size` does, and prints the `Sized`
  line after the `Routed` one. `--tasks` and `--reason` are given together
  or not at all, and `--confidence` needs `--tasks`; a bad sizing is refused
  before the move. A sizing that fails after the move fails loudly, naming the
  item (now in planning) and the `size` command that finishes it. Without a
  sizing the item keeps any size it already carries.
- `split --into REF [--into REF …] --reason TEXT` — comment links to the
  replacement tasks (already created with `vrg-issue-create`) and close.
- `size --tasks N [--confidence high|med|low] --reason TEXT` — record a
  planning or deferred item's estimated epic size. The size is derived from
  the task count (S 1–9, M 10–19, L 20–39, XL 40+); `--confidence` defaults
  to `med` and `--reason` is one line. It posts the dated sizing comment
  first, as the record:

  ```text
  Sized <YYYY-MM-DD>: epic-size <X>, about <N> tasks.
  Epic-size: <X>
  Estimated-tasks: <N>
  Confidence: <high|med|low>
  Reason: <one line>
  ```

  then, in one edit, adds the `epic-size:<X>` label and removes any other
  `epic-size:*` label. It neither moves nor closes the item, and prints
  `Sized <ref>: epic-size X, about N tasks.`. If the label cannot be set
  after the comment posted, it fails loudly with the command that finishes
  it; re-running the same `size` is safe.

Which routes an item allows depends on the container epic it is in:

| Item is in | Allowed routes |
|---|---|
| intake | `drop`, `defer`, `one-off`, `planning`, `split` |
| planning | `defer`, `drop`, `one-off` — each refused while any development epic of the item is open; `size` |
| ad hoc | `defer`, `planning` — each refused while an open PR would close the item |
| deferred | `defer` (refresh), `intake`, `planning`, `one-off`, `drop`, `split`, `size` |
| no parent | `planning` (the `epic-create` free-text path) |
| a development epic | `planning` for a follow-on brainstorm only; any other task is refused, naming the parent |
| anything else | none — refused, naming the parent |

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_intake_route` (mechanics and route table: `lib/intake_route.py`) |
| Args | `<issue-ref>` (`owner/repo#N`, or `#N` in the current repo), then a route: `drop`, `defer`, `intake`, `one-off`, `planning`, `split`, `size` |
| Preconditions | The item is a child of a container epic that allows the route (`planning` also accepts an unparented issue and a follow-on brainstorm under a development epic); GitHub App credentials for the item's org |
| Failure mode | A refused route — not allowed from the item's container, a development-epic task other than a follow-on brainstorm sent to planning, a planning item while any of its development epics is open, an ad-hoc item with an open closing PR, a cross-org target, or a private item headed to a public repo or epic — exits 1 with the reason on stderr, as does a bad sizing (a task count below 1, an empty or multi-line reason, `--tasks` or `--reason` alone on `planning`). A route half-applied by a failure raises with the state it left and how to fix it |
| Exit codes | 0 routed, 1 refused or error, 2 bad arguments (including a missing `--revisit-when`, a malformed `--date`, a non-integer `--tasks` or an unknown `--confidence`) |
| Status | Active |

A `security` item keeps its advisory links. Every retarget moves the
advisory from the item and keeps every other issue it tracks. A recreate
retargets every advisory the item names before the original closes, and a
transfer retargets every one after the move; on a failure, either names
every advisory still not retargeted, with a `retarget --from` command for
each. A split retargets an item's one advisory
before the original closes. A split of an item naming several advisories
is refused before any change, because which advisory goes to which task
can't be inferred:

```text
vrg-intake-route: <owner>/<repo>#<N> names several advisories (<GHSA-id>, <GHSA-id>); retarget each by hand to the tasks that fix it, keeping every other issue it tracks (vrg-security-record retarget <GHSA-id> --from '<owner>/<repo>#<N>' --to <task> [--to …], then vrg-security-record retarget <GHSA-id> --from '<owner>/<repo>#<N>' --to <task> [--to …]), then split
```

See [Security Records](../guides/security-records.md#when-the-issue-moves).

### vrg-security-record

Record a security finding as a private draft advisory in `<org>/.github`
paired with a neutral `security`-labelled intake issue (epic
vergil-project/.github#517). `create` makes the pair (or attaches an
advisory to an existing issue, or resumes an unfinished pair), `list` and
`show` read records, `retarget` moves an advisory's tracking from an issue
it tracks (`--from`) to new issues, refusing to stop tracking an open
issue unless `--drop` names it, and moves the advisory to another org's
`.github` when every `--to` is there (a copy with a `Moved from:` line;
the original closed with `Moved to:`), `sync` sets each paired advisory's state from its issues and
reports broken pairs (`--dry-run` previews it), and `close` closes a draft
advisory whose issues are all closed, posting the GHSA ID and a required
`--reason` on each of them first, and `note` appends a dated private note
from a file to an advisory's description (append-only). Advisory text is printed only by `show` and `list --details`. See
[vrg-security-record](dev/security-record.md) and
[Security Records](../guides/security-records.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_security_record` (library: `lib/security_records.py`) |
| Args | `create --repo --title --body-file --details-file --severity [--summary] [--issue] [--resume]`; `list [--repo] [--state draft\|closed\|all] [--details]`; `show GHSA-ID\|OWNER/REPO#N`; `retarget GHSA-ID --from REF --to REF [--to REF …] [--drop REF …]`; `sync [--dry-run]`; `close GHSA-ID --reason TEXT`; `note GHSA-ID --file FILE` |
| Preconditions | The org's `.github` is public; Repository security advisories: Read and write on the identity's App (or a human `gh` login with access), in both orgs for a cross-org `retarget` |
| Failure mode | A partial pair exits 1 naming what exists and the exact `--resume` command that finishes it; a private `.github`, a duplicate draft, or `--details` under `VRG_EPIC_SWEEP` is refused; `retarget` refuses, before any write, a `--from` the advisory does not track and dropping an open tracked issue, printing the command that would succeed, and `--to` refs spanning orgs, naming the two ways out; a cross-org `retarget` that fails partway is finished by the identical command; `create` refuses an advisory that already tracks another issue, and `create` and `note` refuse caller text holding a `Moved from:` or `Moved to:` line; `sync` exits 1 on a broken pair after syncing the rest |
| Exit codes | 0 success, 1 refused or error, 2 bad arguments |
| Status | Active |

### vrg-queue

Read-only view of a standing queue. `vrg-queue intake` lists the open items
under the intake epic, oldest first. `vrg-queue planning` lists the open items
under the planning epic with their state: **waiting** (no `in-progress`
label), **in-progress** (labelled, with its latest progress note), or
**ready-to-close** (labelled, the `Epic:` / `Docs task:` / `Tasks filed: yes`
markers present, and the docs task closed). A planning item that any security
advisory still tracks, named in its body or not, or whose body names (with
the `security` label on) an advisory that cannot be read, is held at
**in-progress** until every such
advisory is retargeted to the task or tasks that fix it, with a note that
counts them and names them by GHSA ID only (`held: <k> advisory still tracks
this planning issue (<GHSA-id>) — …` for one, `held: <k> advisories still
track this planning issue (<GHSA-id>, …) — …` for more, and
`… cannot be read …` for an unreadable one; see
[Security Records](../guides/security-records.md#security-items-in-planning)).
`vrg-queue deferred` lists the
open items under the deferred epic, all **waiting**, least recently deferred
first; each item's note is its latest deferral
(`Deferred <day>: <reason> | Revisit when: <conditions>`), and an item with no
deferral comment sorts first with an empty note. A bare `vrg-queue` lists all
three queues in that order (intake, planning, deferred). Used by the
collect step of `epic-review` and by `epic-create`'s no-argument summary.

Each planning and deferred line shows the item's **epic size** after its age:
`M/14` (the size and the estimated task count), `?` when unsized, or `!` when
its sizing is in error: its `epic-size:*` label and its latest trusted sizing
comment disagree, one is missing, it has several size labels, or the comment
is malformed. Only comments by trusted authors count. For an item in error, a
held or deferral note is kept with `| size: <error>` appended; otherwise
`size: <error>` replaces its latest-comment note. `--json` adds `epic_size`,
`estimated_tasks`, `confidence` and `size_error` (each `null` when unset) to
every planning and deferred item; intake items are unchanged. For the
planning view only, `--sort age|size|tasks` orders the queue (`age`, oldest
first, is the default; `size` is S, M, L, XL, then unsized, then items in
error, oldest first within each; `tasks` is by estimated task count, then
unsized, then in error), and `--size S,M` filters it to those sizes
(`unsized` selects the unsized). An item in error is always listed, whatever
the filter, and a filtered header reads `planning queue for <name> in <home>:
<N> open item(s), <M> shown`.

`vrg-queue adhoc` lists the open tasks under every live ad-hoc epic
(`Epic (ad hoc): <repo>`, labelled `epic` + `ad-hoc`) in the home, grouped by
repo and oldest first within each. Each line is
`<ref>  <age>  <state>  <kind>  <title>`, followed by an em dash and
`<disposition>: <marks>` when the task is in flight, and by an em dash and
`blocked by <refs>` when a blocker is open. The kind is `validation` or
`deployment` from the task's label, else `task`. The state is **in-flight**
when any in-flight mark holds (it outranks blocked), else **blocked** while any
`Blocked-by:` dependency is open, else **ready**; only the open blockers are
listed.

A task's **marks**, in this order, are: `open-pr` (an open PR closes it, or has
a `feature/<N>-` head branch); `report-ready` (a relay ref
`refs/vergil/pr-workflow/<branch>` whose payload has status `ready` and names
it; a batch's payload names every member); `branch` (a `feature/<N>-` branch on
the remote); `worktree` (a `feature/<N>-` branch or worktree in the repo's main
clone); `merge-review` (`build/.vergil-state/issue-implement/<N>/merge-review`
in the main clone); and `batch` (the batch marker
`build/.vergil-state/issue-implement/<N>/batch` in the main clone, which marks a
batch member with no branch of its own). A branch matches only by the exact
prefix `feature/<N>-`. The **disposition** is **handed-off** when there is an
open PR or a ready record, whatever local marks remain; otherwise **resume**
when there is a worktree, a merge-review directory or a batch marker; otherwise
**handed-off** when only the remote branch remains. The relay payloads are read
by SHA through the GitHub Git Data API, with no fetch. The local marks come from
each repo's main clone, found under the repo roots by its `origin`: `--root
DIR` (repeatable), or the platform default (`/vergil/projects` on a cloud VM,
the projects mount on a Lima VM, none on a physical host). Every repo with open
ad-hoc tasks must have exactly one clone there: a missing clone is fatal, never
skipped or assumed clean. The epics come from one label search; an archive carrying the same
labels (`Archive (ad hoc): …`, or the legacy `Epic (ad hoc): <repo> — <quarter>`
form) is skipped. With `--repo`, a public repo narrows the list to its own
ad-hoc epic in the org's `.github`, and a repo with none gives an empty list;
a self-homed private repo selects its own home, as for the other queues. It is
not a review queue, so a bare `vrg-queue` never shows it. It is the collect
step of `epic-implement adhoc`, as the other queues are of `epic-review`.

`vrg-queue hoshin` lists the open tasks under the org's hoshin epic
(`Epic (hoshin): <org>`, labelled `epic` + `hoshin`), the `Logbook:` tasks
`/vergil:hoshin` files, with the same lines, kinds, states, marks,
dispositions, order, `--json` output and `--root` as `vrg-queue adhoc` (the
two share one body). Its home is always the org's `.github`, never a self-homed
private repo, and the epic is looked up, never created: an org with none gives
an empty list. With `--repo`, only the epic's tasks in that repo are listed.
It is not a review queue, so a bare `vrg-queue` never shows it. It is the
listing for `/vergil:hoshin`'s stale-task check.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_queue` (views: `lib/queue_view.py`) |
| Args | `{intake,planning,deferred,adhoc,hoshin}` (optional; default: intake, planning and deferred); `--repo OWNER/NAME` (that repo's queue home, for `adhoc` on a public repo its own ad-hoc epic, and for `hoshin` the org's hoshin epic narrowed to the repo's tasks; default: the current org's `.github` queues); `--root DIR` (`adhoc` and `hoshin` only, repeatable: where to find each repo's main clone; default: the platform's repo roots); `--sort age\|size\|tasks` and `--size LIST` (`planning` only; refused with any other queue or with none); `--json` (a list of items; with no queue named, an object keyed by queue, `null` for a missing one) |
| Preconditions | GitHub App credentials for the target org. The planning and deferred views read the configured trusted authors (the VM's `trusted-authors.toml`, else the host's `identities.toml` stanza). The planning view also reads the org's advisories when its `.github` is public, or when a planning item has an advisory line (then a `.github` that is not public is refused): the identity needs Repository security advisories access |
| Failure mode | A named queue whose home has no live queue epic exits 1 naming the missing epic; with no queue named, a missing queue prints `<queue> queue for <name> in <home>: no queue yet` and the run exits 0. A queue that cannot be read (a failed GitHub call, including the advisory read; a `.github` that is not public; an advisory line naming another org; a malformed response; trusted authors not configured or invalid) is named on stderr as `vrg-queue: <queue> queue for <name> in <home> could not be read: <reason>` and the run exits 1; with no queue named, a text run still lists the other queues and prints `<queue> queue for <name> in <home>: not shown (failed; see stderr)` in its place, and a `--json` run prints nothing on stdout. For `adhoc`, a failed read of the epics, a task's sub-issues, a task, its blockers or a blocker's state, an epic search that reaches its listing limit, two live ad-hoc epics for one repo, a failed read of any in-flight source (the open PRs, the remote refs, a relay payload, a clone's branches or remotes), a malformed relay payload, repo roots that cannot be determined (no `--root` on a physical host) or do not exist, or a repo with open ad-hoc tasks and no clone, or two, under the repo roots is reported the same way (`adhoc queue for <name> in <home> could not be read`) and exits 1; no ad-hoc epic is an empty list and exit 0. For `hoshin`, the same task, blocker and in-flight failures, a hoshin epic search that reaches its listing limit, or two live hoshin epics, are reported as `hoshin queue for <name> in <home> could not be read`; no hoshin epic is an empty list and exit 0. When every queue was read but an item's sizing is in error, the full listing is printed (`--json` included), each such item is named on stderr as `vrg-queue: <ref>: <error>`, and the run exits 2. `--sort` or `--size` with a queue other than `planning`, or with none, and an unknown `--size` token exit 1. Nothing is created |
| Exit codes | 0 success; 2 every queue read, one or more items' sizing in error; 1 a queue could not be read, or bad arguments (every usage error exits 1, never 2). 1 wins over 2 |
| Status | Active |

### vrg-roadmap

Print the generated project roadmap: the open epics in a repo's resolved
epic home, apart from the standing and proxy epics. The docs-refresh workflow
publishes the same rendering.

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

### vrg-ruling

The agent's side of an authenticated ruling on a task issue (epic
vergil-project/.github#889): one round is the agent's request, the human's
reply and the agent's response, each a comment posted exactly once.
`request` posts a request from an items file and prints its comment ID,
refusing while the issue has an open exchange and naming that request.
`read` runs every turn check on the reply to a request and prints the reply
on stdout, with its comment ID on stderr; with `--json` a valid reply prints
`{"request": <ID>, "reply": <ID>, "body": "<text>"}` instead, and every other
outcome leaves stdout empty, so the exit code is the signal. `draft` builds the human's reply
from a reply items file, pushes it with git plumbing to the reserved ref
`refs/vergil/ruling-draft/<issue>/<request ID>` on the issue's repo, and
prints the human's `vrg-ruling-reply` command; it runs only in a clone
whose `origin` is the issue's repo, refuses once the request has a reply,
and posts nothing. `respond` re-runs `read`'s checks and posts the
response, which records the request and reply comment IDs; a re-run that
finds its own response already landed reports it and posts nothing. Either
way `respond` then deletes any leftover draft ref. `draft-scope` pushes a
scope change drafted for the human, the text alone, to
`refs/vergil/scope-draft/<issue>` and prints the human's `vrg-scope-change`
command; like `draft`, it runs only in a clone whose `origin` is the
issue's repo and posts nothing. A security item carries
only its ID: the fixed text `security finding, details given privately` in
a request, a decision
(`fix`, `file`, `defer` or `wont-fix`) in a draft, and an outcome
(`fixed`, `filed`, `deferred` or `wont-fix`) in a response. All text is
printable ASCII plus newline. See the
[vrg-ruling reference](dev/ruling.md) and the
[Human Replies guide](../guides/human-replies.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ruling` (library: `lib/rulings/`) |
| Args | `request --issue 'OWNER/REPO#N' --items FILE`; `read --issue 'OWNER/REPO#N' --request ID [--json]`; `draft --issue 'OWNER/REPO#N' --request ID --items FILE`; `respond --issue 'OWNER/REPO#N' --request ID --reply ID --items FILE`; `draft-scope --issue 'OWNER/REPO#N' --text-file FILE` |
| Preconditions | The `user` identity mode; trusted authors list exactly one human login, and `<human>-vergil-user` is on the `agents` list; for `draft` and `draft-scope`, a clone whose `origin` is the issue's repo |
| Failure mode | A refusal or error prints the reason on stderr and exits 1; a fatal turn check names the check and the comment and exits 4. A failed post is never retried: the issue is re-read, and the tool reports the landed comment, says it did not land, or says it cannot tell |
| Exit codes | 0 success (or a valid reply), 1 refused or error, 2 bad arguments, 3 no reply yet, 4 fatal check |
| Status | Active |

### vrg-ruling-reply

The human's side of an authenticated ruling: it posts the human's reply to
the agent's request, as the human. It is a human-only command, run in the
human's own shell on the host, logged in to `gh` as themselves, the way
`vrg-submit-pr` is run; never through `!` in an agent session, whose shell
carries the agent's identity. It refuses in an agent identity mode, without
an interactive terminal, and unless `gh api user` reports the one human
login of the trusted-authors configuration. It refuses unless the request is
the issue's open request and has no reply yet. It reads the reply once, from
the agent's draft (the request's draft ref, pushed by `vrg-ruling draft`) or
from `--response-text`, refuses anything but printable ASCII plus newline,
and prints the reply exactly as it will be posted with its byte length and
SHA-256. On confirmation it reads the issue again, refuses if its trusted
comments changed while the prompt waited, posts exactly the bytes it showed,
once, and deletes the request's draft ref. A draft pushed again while the
prompt waits is never what is posted. Declining exits 1 and keeps the draft.
See the [vrg-ruling-reply reference](dev/ruling-reply.md) and the
[Human Replies guide](../guides/human-replies.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ruling_reply` (library: `lib/rulings/`) |
| Args | `--issue 'OWNER/REPO#N' --request ID [--response-text TEXT]` |
| Preconditions | The human's own shell (not an agent identity), an interactive terminal, `gh` logged in as the human login; trusted authors list exactly one human login, and `<human>-vergil-user` is on the `agents` list |
| Failure mode | A refusal, a declined prompt or an error prints the reason on stderr and exits 1, posting nothing; a fatal turn check names the check and the comment and exits 4. A failed post is never retried: the issue is re-read, and the tool reports the landed comment, says it did not land, or says it cannot tell |
| Exit codes | 0 posted, 1 refused, declined or error, 2 bad arguments, 4 fatal check |
| Status | Active |

### vrg-scope-change

Posts a scope change on a task issue (an addition to the task's scope, a
narrowing of it, or a ruling on an item awaiting one) as the human, so merge
review can count it as the human's. It is a human-only command, run in the
human's own shell on the host, like `vrg-ruling-reply`, and refuses in an
agent identity mode, without an interactive terminal, and unless `gh api
user` reports the one human login of the trusted-authors configuration. It
refuses while the issue has an open ruling exchange. It reads the text once,
from the agent's draft (`refs/vergil/scope-draft/<issue>`, pushed by
`vrg-ruling draft-scope`) or from `--text`, refuses anything but printable
ASCII plus newline, and renders the comment as the header line
`Scope change`, a blank line and the text. That header is how a scope
change is recognised, together with the human login as its author. If the
human has already posted exactly that comment, it prints its ID and posts
nothing. Otherwise it prints the comment with its byte length and SHA-256,
and on confirmation re-reads the issue, refuses if its trusted comments
changed while the prompt waited, posts exactly the bytes it showed, once,
prints the comment ID and deletes the draft ref. See the
[vrg-scope-change reference](dev/scope-change.md) and the
[Human Replies guide](../guides/human-replies.md#scope-changes).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_scope_change` (library: `lib/rulings/`) |
| Args | `--issue 'OWNER/REPO#N' [--text TEXT]` |
| Preconditions | The human's own shell (not an agent identity), an interactive terminal, `gh` logged in as the human login; trusted authors list exactly one human login, and `<human>-vergil-user` is on the `agents` list; no open ruling exchange on the issue |
| Failure mode | A refusal, a declined prompt or an error prints the reason on stderr and exits 1, posting nothing. A failed post is never retried: the issue is re-read, and the tool reports the landed comment, says it did not land, or says it cannot tell; a re-run finds a landed comment and posts nothing |
| Exit codes | 0 posted or already posted, 1 refused, declined or error, 2 bad arguments |
| Status | Active |

### vrg-merge-review-check

The merge-review result check (epic vergil-project/.github#881): it checks
a reviewer's stored reply against the request it answers, by the rules of
the [merge-review protocol](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/merge-review-protocol.md).
It first reads the capture record the plugin's capture hook wrote beside the
result, `capture-<step>.json` in the result's directory (`<step>` from the
request), and refuses a result with no record, a malformed record, one for
another step or request, or one whose `result_sha256` is not the SHA-256 of
the result file's bytes, so a result edited after the capture is refused.
The reply must open, after optional whitespace, with exactly one
`merge-review-result` fenced block holding one JSON object, with no second
such block anywhere. The block must carry exactly the schema's fields at
every level, copy the request's `step` and `request_id`, use only the
defined questions, kinds and statuses, give each finding an ID with its
step's prefix, keep every summary and location to one line of printable
ASCII (a summary at most 300 characters, none on a security finding), raise
no `production-safety` finding on a `docs-only` change, and, in a check,
raise no `pre-existing` finding and give exactly one `fix_status` per ID the
request listed. A summary or location that carries the vrg-ruling marker
prefix is refused too, so it can go into a `vrg-ruling` item unchanged. On
success it prints `{"step", "counts", "security", "fix_status"}` as JSON on
stdout. A refusal names the rule, the finding by ID (or by position when
the ID is bad) and, for a bad character, its code point and offset; it
never repeats the refused value. It reads the two files and the capture
record, and writes nothing.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_merge_review_check` (library: `lib/merge_review/`) |
| Args | `--request FILE --result FILE` |
| Preconditions | The capture record `capture-<step>.json` beside the result; no identity mode, network or git access |
| Failure mode | `ERROR: <rule>` on stderr and exit 1, with nothing on stdout |
| Exit codes | 0 the result passed, 1 it failed or a file could not be read, 2 bad arguments |
| Status | Active |

### vrg-change-class

The merge-review change classifier (epic vergil-project/.github#881): it
prints `docs-only` or `code` for the files a branch changed since its merge
base with `--base` (`git diff --name-status -M <base>...<head>`), so commits
added to the base after the branch was cut never count. A change is
`docs-only` only when every changed file is an addition or an edit of a
path on the documentation allow-list; any deletion, rename or type change,
any change to `vergil.toml`, and an empty change are `code`. The allow-list
is `README.md` and `CHANGELOG.md`, plus the repository's own paths from
`vergil.toml` as committed at the merge base (never the working tree's
copy):

```toml
[merge-review]
docs-paths = ["docs/guides/", "NOTICE.md"]
```

An entry ending in `/` covers everything below it; any other entry is that
one file. Markdown is not documentation by its extension: list only paths
that hold nothing that controls behaviour. The command is for humans and
debugging; the merge-review skill gets the class through the request
builder, which runs the same classifier. It reads the repository and writes
nothing. See the [Merge Review guide](../guides/merge-review.md) for how the
class and the three merge-review commands fit together.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_change_class` (library: `lib/merge_review/change_class.py`) |
| Args | `--base REF [--head REF]` (`--head` defaults to `HEAD`) |
| Preconditions | Run inside the repository, whose merge base has a valid `vergil.toml` |
| Failure mode | `ERROR: <message>` on stderr and exit 1 (an unknown ref, no merge base, a merge base with no `vergil.toml` or an invalid one) |
| Exit codes | 0 the class was printed, 1 error, 2 bad arguments |
| Status | Active |

### vrg-merge-review-request

The merge-review request builder (epic vergil-project/.github#881): it
writes a review or check request, by the
[merge-review protocol](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/merge-review-protocol.md),
into the task's private directory,
`build/.vergil-state/issue-implement/<N>/merge-review/` in the main clone,
and prints the request's path. It writes `request-<step>.json`,
`diff-<step>.patch` (`git diff <base_sha> <head_sha>`, where `base_sha` is
the merge base of `--base` and `HEAD`) and, for a check,
`fix-<step>.patch` (`git diff <fix_base_sha> <head_sha>`). Before it reads
or writes anything, with or without `--ensure-dir`, it refuses unless the
current clone's `origin` is the `--issue` repository (every push URL names
it on github.com), naming both repositories. It refuses,
naming the reason, unless the worktree is clean and the last `vrg-validate`
run (its record, `.vergil/vrg-validate-last.json`) was a full run that
started clean at `HEAD`, exited 0 and passed every stage; the passed stages
become `validate_stages`. The change class is
[`vrg-change-class`](#vrg-change-class)'s, with the allow-list committed at
the merge base.

It reads the body of the `--issue` itself, as `issue_body`, and derives
`epic_open_tasks` from the issue's native parent: when the parent carries
`epic` and not `ad-hoc`, its open sub-issues, every page, as
`{"ref", "title"}`, without the issue itself; otherwise `[]`. When the
parent is a proxy epic (it also carries `proxy`), the open tasks are
instead those of the proxy's primary, its own and every proxy's, as
[`vrg-epic-audit --epic`](#vrg-epic-audit) lists them in its `children`,
again without the issue itself. It refuses when the issue, its parent's
labels or the sub-issues cannot be read, and, for a proxy, when its
`Primary:` line cannot be resolved, the primary has no trusted `Proxy:`
comment naming it back, or any of the primary's proxy links is broken.

It reads the comments of the `--issue` itself, every page, and keeps only
those posted by the one human login of the trusted-authors configuration
(see [`vrg-trusted-authors`](#vrg-trusted-authors)), oldest first, as
`issue_comments`, each `{"author", "created_at", "body"}`. Agent and
third-party comments are dropped. A human comment that has been edited is
left out and counted in `issue_comments_excluded_edited`. It refuses when
the configuration lists no human login or several, when the comments
cannot be read, and when a kept comment's `created_at` is not an ISO 8601
timestamp with a time zone, naming the comment by ID and never echoing a
value.

A check takes `--fix-base` (the `HEAD` the previous step judged, refused
unless it is the `head_sha` of that step's stored request), one
`--earlier-result` per earlier step in step order, each checked against
that step's stored request, `--fix-id` for each earlier ID asked to change
(it becomes `earlier_finding_ids`) and `--ruled-id` for each earlier defect
the human excused (not sent to the reviewer). It refuses an ID no earlier
result raised, an ID given to both, a check with no `--fix-id`, and an
earlier defect named by neither unless a later result marks it `fixed`.

For a fix to an open PR, `--pr` names the PR by number or URL. The PR must
be in the `--issue` repository, open, and headed by the current branch.
When its body has more than one `Closes` line it is a batch PR: the
`--issue` must be one of them, and every step's request adds
`batch_issues` (each other issue's ref, body and human comments, read as
for the `--issue`), `batch_commits` (every commit under review as
`{"sha", "subject", "issue"}`, its issue the one its subject's trailing
`(#N)` names) and `batch_commits_path` (`commits-<step>.patch`, each
commit with its own patch), and leaves the batch's issues out of
`epic_open_tasks`. It refuses a merge commit, a commit naming no issue of the
PR, an issue with no commit, and a check whose batch issues differ from its
previous step's. A single-issue PR must link the `--issue`, and its request
is the one built without `--pr`. See the protocol's
[A batch PR](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/merge-review-protocol.md#a-batch-pr).

Before writing, and on its own with `--ensure-dir`, it checks the private
directory level by level: `build` must be a real directory (it is created
when absent), and each level from `.vergil-state` down to `merge-review`
must be a real directory, not a symlink, with mode 700; an absent level is
created mode 700. The caller runs `--ensure-dir` before every write it
makes there, and writes only to the path it prints.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_merge_review_request` (library: `lib/merge_review/request.py`, `lib/merge_review/open_pr.py`, `lib/merge_review/state.py`, `lib/build_dir.py`, `lib/validate_record.py`, `lib/rulings/comments.py`, `lib/rulings/identity.py`) |
| Args | `--issue OWNER/NAME#N --base REF --step {review,check,check-<k>} [--design-source FILE]... [--pr N\|URL] [--fix-base SHA --earlier-result FILE... --fix-id ID... [--ruled-id ID]...]`, or `--issue OWNER/NAME#N --ensure-dir` |
| Preconditions | Run inside the task's worktree after `vrg-container-run -- vrg-validate` passed at `HEAD`; trusted authors are configured with exactly one human login, and GitHub is reachable to read the issue, its comments and its epic's sub-issues (and, with `--pr`, the PR and each of its issues) |
| Failure mode | `ERROR: <reason>` on stderr, nothing on stdout |
| Exit codes | 0 the path was printed, 1 a refusal or error, 2 bad arguments, 3 a check's `--fix-base` is not an ancestor of `HEAD` (the branch was rebased) |
| Status | Active |

### vrg-epic-audit

Report epic/task drift and invariant violations for an org, as Markdown on
stdout. It is read-only by default. **Drift** is a task still open although
a merged PR that links it landed within the last `--window-days` days (a
body's one linkage line, or every `Closes` line of a body with several, so
each issue of a batch PR is checked), and
an open finite epic whose children are all closed (a standing or
[proxy epic](../standards/github-issues.md#proxy-epics) never is). The **invariant** checks
are the [compliance invariants](../standards/github-issues.md#compliance-invariants):
epics outside `.github`, stray `.github` issues, a closed epic with an open
child, orphans, retired kind labels, body-only `Parent:` links, self-homed
queues in a now-public repo, private work under a public epic, and a
misplaced archive (one with a parent, one without its `archive` label, or one
labelled `archive` whose title is not a stamped archive title). It also
lists open epics with operational tasks still pending, and closed
operational tasks with no `Outcome: SUCCESS`.

A **Proxy link drift** section lists each open proxy epic in the home whose
`Primary:` line is broken or whose primary carries no matching trusted
`Proxy:` comment. It is report-only, and runs only without `--close`: it reads
the trusted authors, which the scheduled sweep has no configuration for.

An **Ad-hoc tasks that look misplaced** section lists each open task under a
live ad-hoc epic whose title starts `Logbook:` (it belongs under the hoshin
epic), or whose body names an `epic`-labelled issue in another org (it belongs
under that org's proxy), each with its reason. A cross-org issue the body
names that cannot be read (private, deleted, or in an org the App cannot
reach) is listed as a reason of its own. A task drops out of the report once
a comment by a trusted author carries the line `Standalone-confirmed`, alone
on its line, recording the human's ruling that it is standalone. The report
never refuses, moves, closes or labels anything, does not change the exit
status, and, like the proxy link drift section, runs only without `--close`.

An **Epic documents** section checks the
[Documents section](../standards/github-issues.md#the-documents-section) of
every epic in the home, open and closed, apart from the standing, proxy and
`[archived]` epics. Each epic needs the section, well formed, with its three
entries. On a closed epic, no entry may read `pending` and every link must
name a file on its repo's default branch (a link to a directory, another
branch, another org or a missing repo is reported too). An open epic's links
are not checked. The findings are report-only: they do not change the exit
status, and `vrg-epic-docs` settles them. The check runs only without
`--close`, so the scheduled sweep does not report it. The audit lists the
home's epics once, under the home owner's App token, for this check and the
closed-epic-with-open-child check.

The report previews, without
writing, the proxies whose primary has closed (the ones `--close` would close
and the ones it would leave open), the ad-hoc and queue-epic drains that
`--close` would run, and the
security-records pass of `vrg-security-record sync` (`--close` does not run
it).

The org comes from the current repo's `origin` remote (there is no `--org`
flag). `--repo OWNER/NAME` audits that repo's resolved epic home instead of
the org's `.github`, so a private repo that self-homes its epics can be
audited; the home-scoped checks (epic drift, stray issues, operation-pending
epics, and the `--close` drift closes) then target it.

Every read runs under the audited org's token, wherever the command is run
from. Before any check, the audit confirms that this token sees every repo in
the org: an App token must belong to an installation on that org with access
to all repositories, and a human's own `gh` login must be an owner of the org
(or the org must be that login's own account). If it does not, the audit
names the gap on stderr. The read-only report then opens with an
**Incomplete coverage** section and exits 1, and `--close` is refused before
any write. A full preview therefore needs the org's App installation with
access to all repositories, or an org owner's own login.

`--epic REF` is a separate read-only mode: it prints one epic's children as
JSON (`{"epic": <slug>, "children": [{repo, number, title, state}, …]}`) and
skips the drift audit. A bare `#N` resolves against the current repo's epic
home. The children include the tasks of every
[proxy epic](../standards/github-issues.md#proxy-epics) the epic's trusted
`Proxy:` comments name, each with its repo, beside the epic's own. On a proxy
the JSON gains a `"primary": <slug>` key naming its primary; on any other epic
the key is absent. `--epic` also checks the links: on a primary, every proxy
it names must exist, be a proxy and carry a `Primary:` line naming it; on a
proxy, its primary must carry the matching `Proxy:` comment. A link that
disagrees, cannot be parsed or cannot be read exits 1, with the problems on
stderr and no JSON, so the retrospective gate stops on it.

`--close` applies the fixes, in order:

1. Close the drifted tasks and rolled-up epics. Then close each open proxy
   epic in the home whose primary has closed and which has no open task; a
   proxy that still has an open task is reported and left open, never closed
   silently. Then reopen each epic closed while a child was still open. Each
   close and reopen carries an explanatory comment.
2. Drain the closed children of the ad-hoc, intake, planning, deferred and
   hoshin epics into per-quarter archives, and close past archives (see
   [Ad-hoc epic archiving](../standards/github-issues.md#ad-hoc-epic-archiving)).
3. Run the [orphan pass](../standards/github-issues.md#the-orphan-pass-auto-heal):
   nativize body-only `Parent:` links, strip the retired kind labels, move a
   now-public repo's queue items under `.github`, and attach every orphan to
   its home's intake epic.

`--close` makes no security-advisory call: it neither syncs advisory states
nor checks advisory/issue pairs. That is `vrg-security-record sync`, run by
the human or the user agent (see
[Security Records](../guides/security-records.md#managing-a-record-through-its-issue)).

`--close` is a human action, or the scheduled `ops-epic-sweep` workflow,
which sets `VRG_EPIC_SWEEP=1`. Other invariant violations stay report-only.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_epic_audit` (checks and passes: `lib/epic_audit.py`; security pass: `lib/security_records.py`) |
| Args | `--window-days N` (positive; default 30), `--repo OWNER/NAME`, `--epic REF`, `--close` |
| Preconditions | Run inside a repo in the target org; GitHub App credentials for that org. `--close` needs a human identity or `VRG_EPIC_SWEEP=1`. The read-only report's security-records preview needs Repository security advisories: Read; `--close` needs no advisories access |
| Failure mode | `--close` in an agent session is refused before any network call; a `--repo` without `/`, an org that cannot be detected, or an org with no `.github` home exits 1. A token that cannot see every repo in the org exits 1: the report is marked partial and `--close` is refused before any write. A full intake epic stops the orphan pass, listing what it did not attach. A failed security-records preview exits 1 after the rest of the report. A proxy whose `Primary:` link cannot be parsed or read is named on stderr and in the report, every other pass still runs, and the run then exits 1, with or without `--close`. With `--epic`, a ref that is not an epic, or proxy link drift, exits 1 |
| Exit codes | 0 success (the read-only report exits 0 even when it finds drift), 1 refused, incomplete coverage, a pass failed, or error, 2 bad arguments |
| Status | Active |

### vrg-repo-status

Show every worktree git knows about in the current repo, grouped by
kind, so nothing with code on disk goes unseen (issue #3386), then the
open PRs on `origin` that no worktree shows (issue #4581), then what the
integration branch holds that `main` does not. Read-only — it never
fetches or writes a ref, cleanup stays `vrg-finalize-pr`'s job, and
finalize and sweep still act only on development worktrees. Local
branches with no worktree are not shown. The sections, in order:

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
5. **Open PRs on origin with no local worktree** — every open PR whose
   head branch no linked worktree holds, described below. Always shown.
6. **Pending release** — every change on the integration branch that is
   not yet on `main`, described below. Shown only in a repo that
   publishes.

Empty worktree sections are omitted; the summary line counts every
worktree section, including the empty ones. A branch that was deleted on its remote is
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

LAST COMMIT is the age of the worktree's HEAD commit. In the worktree
sections, a worktree whose branch has no commits beyond the target branch
(AHEAD 0) shows `-` there instead: its HEAD is still the commit it was
cut from, whose age says nothing about the work on it (issue #3828).
LAST MODIFIED is still shown. A detached worktree whose branch cannot be
recovered also shows AHEAD 0, because there is no branch to count, and
keeps its LAST COMMIT.

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

**Open PRs on origin with no local worktree.** The section follows the
worktree sections. Many PRs never touch the local checkout: those that
automation jobs open (lifecycle, repo-config sync, dependency updates) and
those prepared on another machine, such as a cloud VM. They are read from
GitHub in one query per 100 open PRs. A PR is left out when a linked
worktree already shows it: its PR number is in a worktree row, or its
head branch is a linked worktree's branch. A PR from a fork is never
matched by branch name, since a fork's branch can share a local name; its
BRANCH shows as `<owner>:<branch>`. A VM that shares the file system
already shows its PRs through its worktrees.

```text
Open PRs on origin with no local worktree
PR    BRANCH                    BASE     TYPE       AUTHOR                     CI       OPENED  TITLE
#935  lifecycle/rust-add-1.100  develop  lifecycle  vergil-project-automation  success  6h ago  feat(versions): add rust 1.100
#936  lifecycle/rust-drop-1.98  develop  lifecycle  vergil-project-automation  pending  6h ago  feat(versions): drop rust 1.98 (EOL 2026-10-01)
2 open PRs with no local worktree: 0 automation PRs, 2 lifecycle PRs, 0 dependency updates, 0 release PRs, 0 other PRs.
```

- **Rows** come in PR-number order. TYPE is as in Pending release
  below, except that a `lifecycle/*` head branch (a language version
  added or dropped) shows `lifecycle` first. AUTHOR is the PR author's
  login (`ghost` for a deleted account). CI is the head commit's
  combined check state (`success`, `failure`, `pending`, `error`,
  `expected`), or `none` when it has no checks. OPENED is the PR's age.
  A draft's TITLE starts with `[draft]`; a TITLE over 72 characters is
  cut, ending in `…`.
- **The summary line** counts the rows: automation PRs, lifecycle PRs,
  dependency updates, release PRs and other PRs, every bucket listed.
- **None left** prints `None: <n> open PRs on origin, each with a local
  worktree.`, or `None: origin has no open PRs.`, in place of the table.
- **A lookup that fails or is incomplete** (an API error, a missing
  token, a non-GitHub `origin`, a list that changed between pages, a
  truncated label list) is an error, never "no open PRs" and never a
  partial list: the section prints its heading and
  `ERROR: open PRs: <reason>` alone. The rest of the output still
  prints, then the command exits 1. The reason never carries URL
  credentials.

**Pending release.** The section follows the open-PR section and comes
before the summary lines. It is shown only when `vergil.toml` sets
`[publish].release = true`; with the flag false or absent, nothing is
printed for it, and without a `vergil.toml` a note says it was skipped.
In a publishing repo it is always shown, even when nothing is pending.
It lists every change on the integration branch that is not on `main`,
read from the remote without fetching: the current refs and tags, the
first-parent commits from the GitHub compare API, and each commit's
merged PR into the integration branch, by commit SHA (so a batch PR
rebase-merged as several commits is one row).

```text
Pending release (develop → main): latest release v2.1.303, main moved 4h ago; rolling tag v2.1 → v2.1.303
PR     BRANCH                      TYPE     MERGED  TITLE
#4251  release/post-2.1.303        release  4h ago  chore(release): back-merge 2.1.303 and bump to 2.1.304
#4258  deps/update-2.1.304         deps     4h ago  chore(deps): dependency update for 2.1.304
#4252  feature/4249-refuse-ubuntu  feat     1h ago  feat(validate): refuse ubuntu-latest runner labels
3 changes pending release: 1 dependency update, 1 release back-merge, 0 automation PRs, 1 other PR.
```

- **Release line.** It follows the section heading on the same line,
  after a colon. The latest release is the highest `vX.Y.Z` tag on
  the remote (`no release yet` without one), with how long ago `main`
  last moved. The rolling tag is the highest `vX.Y` tag, shown with the
  `vX.Y.Z` tag on the same commit, or its short SHA when that commit has
  none, or `no rolling tag`. A rolling tag behind the latest release is
  shown as `(lags vX.Y.Z)`: information, not a problem, since it can be
  held back deliberately. Rolling major `vX` tags and `develop-v*` tags
  are ignored.
- **Rows** come in merge order, oldest first. TYPE is the PR title's
  conventional-commit type (`-` when it has none), except that a
  release back-merge (a `release/post-*` head branch) shows `release`,
  an automation PR (an `automation/*` head branch, such as
  `automation/repo-config-sync/<run-id>`) shows `automation`, and a
  dependency update (a `deps/update-*` head branch or a
  `dependency-update` label) shows `deps`. The head branch decides
  before the label, so an automation PR is never counted as a
  dependency update or a development PR. MERGED is the PR's
  merge time. A TITLE over 72 characters is cut, ending in `…`.
- **A commit with no PR** shows `PROBLEM` in the PR column and `-` for
  BRANCH and TYPE, with its commit date and subject, and a
  `PROBLEM: <sha> reached develop without a PR` note after the table.
- **Nothing pending** prints `Nothing pending release: develop and main
  are level.` in place of the table.
- **The summary line** counts the changes: dependency updates, release
  back-merges, automation PRs and other PRs, every bucket listed, plus
  `<n> PROBLEM: commits with no PR` only when there are some.
- **A lookup that fails or is incomplete** (an API error, a missing
  branch, a truncated answer) is an error, never a partial list: the
  section prints its heading, with no release line, and
  `ERROR: pending release: <reason>`
  alone. The rest of the output still prints, then the command exits 1.
  The reason never carries URL credentials.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_repo_status` (open PRs: `lib/open_prs.py`; pending release: `lib/pending_release.py`) |
| Args | `--target-branch` (default: develop) — the branch AHEAD is measured against |
| Preconditions | Git repo; `gh` CLI on PATH for PR state; for the open-PR and Pending release sections, a github.com `origin` and GitHub credentials |
| Failure mode | None for classification — a failed PR lookup is reported as an `unknown` row and a failed branch-gone check as a note, never a crash. A malformed `vergil.toml` exits 1 with the reason. A failed or incomplete open-PR or pending-release lookup prints `ERROR:` in its section and exits 1 after the rest of the output |
| Exit codes | 0 success, 1 malformed `vergil.toml`, or an open-PR or pending-release lookup error |
| Status | Active |

### vrg-whoami

Print the resolved Vergil identity role, from the authoritative resolver
in `lib/identity_mode.py`. Ask `vrg-whoami`; never infer identity from
`VRG_IDENTITY_MODE` alone. That variable is only the first of five
fallback signals (environment variable, mode file, App key, App ID, then
the `human` default), and an unset value means "fall through", not
"human". See [Account setup](../guides/account-setup.md).

The `automation` role is checked before those signals, and only when
`VRG_AUTOMATION_APP` is set. That variable names the org automation App,
and the token must then verify as that App's installation token (GraphQL
`viewer` login `<slug>[bot]`). A different login, a failed lookup, an empty
variable, or an agent signal alongside it is an error. No
`VRG_IDENTITY_MODE` value or mode file selects `automation`. Without the
variable, resolution is unchanged and makes no network request.

- `vrg-whoami` / `--mode`: the role as one token (`human`, `user`,
  `audit` or `automation`), for scripting, for example
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
| Preconditions | None (with `VRG_AUTOMATION_APP` set, a GitHub token) |
| Failure mode | Disagreement is a warning on stderr, never an error; a failed automation verification exits 1 with the reason |
| Exit codes | 0 success, 1 automation verification failed |
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

In single-label mode, `vrg-ensure-label --repo <owner>/<repo> --label
<name>` with neither `--color` nor `--description` creates or updates
that one label from its registry definition, and touches no other
label. A name not in the registry fails, naming it, and creates
nothing. Passing `--color` or `--description` creates the label from
the flags given instead, without reading the registry.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ensure_label` |
| Args | `--repo`, `--label`, `--color`, `--description`, `--sync`, `--owner`, `--project` |
| Preconditions | `gh` CLI on PATH |
| Failure mode | argparse validation for incompatible flag combinations; an unregistered `--label` with neither `--color` nor `--description` is refused on stderr; subprocess error from `gh` |
| Exit codes | 0 success, 1 unregistered label or no App installation for the owner, 2 invalid flag combination |
| Status | Active |

### vrg-github-repo-config

Audit a managed repo against the canonical Vergil configuration. In
`update` mode it also reconciles the GitHub half and proposes the
managed-file fixes as one sync PR, watched until it merges; `apply`, which
reconciles only the GitHub half, is deprecated. Combines managed-file
checks, run in a fresh shallow clone of the repo's `develop` (`vergil.toml`,
`CLAUDE.md`, `.claude/settings.json`, hook-guard
shim, workflow pins, `.gitignore` managed fence, canonical
`dependabot.yml`, `ops.yml` wiring)
with GitHub-API checks (repo settings, rulesets, required-check set).
`update` runs nightly, as the org automation App, from each repo's
`ops.yml` via the reusable `ops-github-config.yml` workflow; drift it
cannot fix turns the run red. `update` refuses the agent identity modes.
See the [GitHub Config Audit reference](config-audit.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_github_repo_config` |
| Args | `audit` \| `diff` \| `update` \| `apply` (deprecated); `--repo OWNER/REPO`, `--config PATH`; `--in-tree-templates` (`audit` only, vergil-tooling only) |
| Preconditions | Local checks clone the repo's `develop` over HTTPS (git's credential helper must reach it); GitHub checks require `gh` credentials; `update` needs the `human` or `automation` identity mode |
| Failure mode | `DiffItem` list on drift; clean diagnostic on unreachable GitHub state |
| Exit codes | 0 compliant (for `diff`: whether or not it finds drift; for `update`: converged and in sync, or the sync PR opened this run merged), 1 non-compliant (drift; for `update`, drift it could not fix), 2 could not complete, including a setting not checked (for `update`, also an agent identity or a sync PR that did not merge) |
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

For a TypeScript repo, the config-files step also writes the language's
managed files (`tsconfig.vergil.json`, `eslint.vergil.config.mjs`,
`.prettierrc.json`) and a minimal `package.json` holding only `name`
(the repo name), `version` (the `VERSION` it writes) and
`"private": true`, so the new repo passes the `package.json` version
cross-check from its first commit. It never overwrites an existing
`package.json`, which is the repo's, not a managed file (see
[A new repository's `package.json`](../standards/development/typescript/overview.md#a-new-repositorys-packagejson)).

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

**Integration tests are refused.** `[ci].integration-tests = true` would
make the CI-gates rulesets require `test / integration / <version>`,
which no vergil-actions CI job produces yet
([#2721](https://github.com/vergil-project/vergil-tooling/issues/2721)),
so the repo could never merge a PR. The wizard refuses the setting as
early as it can name it: `--integration-tests` fails at argument
parsing, before step 1 creates the repository, and a "yes" to the
prompt (or an adopted `vergil.toml` that sets it) stops the run in
step 3, before `vergil.toml` is written and before step 8 makes the
first push. A resumed run whose committed `vergil.toml` sets it stops
before the remaining steps. The config loader still accepts the key,
so other tools keep working in a repo that sets it.
`--no-integration-tests` turns off an adopted repo's setting.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_github_repo_init` |
| Args | `org/name` positional; `--adopt`, `--non-interactive`, `--target-dir`, and per-field overrides (`--language`, `--repository-type`, …) |
| Preconditions | `gh` and `git` on PATH; run from the org-layout parent (or pass `--target-dir`) |
| Failure mode | Loud refusal on a foreign-repo CWD, an occupied clone path, missing required non-interactive values, or `[ci].integration-tests = true` |
| Exit codes | 0 success, 1 error, 2 a refused argument (including `--integration-tests`) |
| Status | Active |

### vrg-container-run

Run arbitrary commands inside a dev container. Auto-detects the
project language to select the Docker image; falls back to
`dev-base:latest`. Uses `execvp` to replace the process.
Local-settings guard: on the host, before any container starts, it refuses an
agent (warns a human) while a loadable `.claude/settings.local.json` is not
absent or `{}` (epic vergil-project/.github#558).
The container mounts and runs in the root of the repo `git` resolves from the
current directory, not the current directory itself. When the two differ (a
subdirectory, or a copied tree with no `.git` of its own inside another repo),
it prints a warning on stderr naming both paths and carries on (issue #4594).
Every launch bind-mounts the per-user `~/.cache/vergil/trivy` directory
(under `$XDG_CACHE_HOME` when set), creating it if missing, where
`vrg-validate`'s trivy stage caches the vulnerability database (see
[Container Config](container-config.md#variables-vrg-container-run-always-sets)).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_container_run` |
| Args | `[--prefix <dev\|prod>] [--] <command> [args...]` (manual parsing, `--` separator) |
| Preconditions | Git repo, `GH_TOKEN` set, Docker daemon running |
| Failure mode | Explicit error message for missing `GH_TOKEN`; `assert_docker_available()` exits with message for Docker; `git.repo_root()` raises on non-git directory; in a cpp repo, an unreadable or malformed ConanCenter audit token source exits 1 naming the file (see [Container Config](container-config.md#variables-vrg-container-run-always-sets)) |
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
committed copies (`vrg-git rm --cached <path>`). The managed
`.gitignore` fence itself is fixed on `develop`, not in your checkout:
[`vrg-github-repo-config update`](config-audit.md#the-update-command),
run by the human or by the nightly ops job, corrects it through a sync
PR. Once that PR has merged, bring your branch up to date with
`develop`.

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

A cached image is named
`<base image>:<base tag>--<branch>--<repository>.<hash>`, for example
`ghcr.io/vergil-project/prod-base:latest--develop--vergil-tooling.2fb5421a`.
The branch is sanitized (`/` becomes `-`). The repository is the directory
name of the repository's clone, the same from each of its worktrees. The hash
is the cache key: the cache-sensitive files, the base image's digest, the
build environment and the vergil-tooling release the image installs. Naming
the repository gives each repository its own cached image, so two
repositories on the same base image and branch no longer replace each
other's. `build`, `clean`, `status` and `vrg-container-run` only ever find
or remove the current repository's image, and so does `vrg-finalize-pr` when
it cleans a branch's images (issue #4162).

#### How the cache tracks vergil-tooling releases

The base images carry no vergil-tooling: the cache build installs the
release that `vergil.toml`'s `[dependencies].vergil` pin names (or
`VRG_DOCKER_INSTALL_TAG`, when set). A line pin such as `v2.1` floats with
its rolling tag, so every `build`, `status` and `vrg-container-run` resolves
it to the release that tag names before it looks at the cache. It resolves
the line once, with one `git ls-remote` of the vergil-tooling repository,
reading the tag `v2.1` itself, peeled to its commit, and taking the one
`vX.Y.Z` tag on that commit and its SHA; it never simply takes the newest
`vX.Y.Z`, and it reads no package index. A line whose rolling tag is
missing, or whose commit carries no `vX.Y.Z` tag, is an error.
That release, such as `v2.1.320`, and its SHA are both part of the cache
key, the build installs exactly that commit, constrained by the commit's own
`uv.lock`, and the image records the release. So when `promote` moves the
line to a release, or `vrg-promote` demotes it to an older one, the next
run rebuilds each branch's cache with it, and `status`
reports the old cache `stale`, naming the release it holds and the one the
pin installs now (issue #4550).

An exact pin (`v2.1.320`) is its own release and needs no lookup: its cache
key is the tag alone, which tag protection keeps immutable, and the build
resolves its SHA. So an exact pin finds its cached image with no network
call. A dev ref (`develop`, a branch) is installed from git and is not
resolved: a cache built from a dev ref is not rebuilt when that ref moves.

A line pin is resolved on every run, never cached, like the base image pull
and the version window. When it cannot be resolved (offline, or GitHub
cannot be reached), or the line has no release tag, `build`, `status` and
`vrg-container-run` fail with the cause and exit 1. They never fall back to
the release the cached image holds. To work without the network, pin an
exact release with `VRG_DOCKER_INSTALL_TAG`: its cache needs one build with
the network, and is then found offline. An image built under the line pin
is not reused, since the line pin's key also holds the commit. The error
names the release this branch's cached image holds, so you can pin it once
the network is back.
`VRG_ALLOW_STALE_BASE`, which lets a run use the local base image when its
pull fails, does not cover this lookup.

Images from earlier vergil-tooling releases are named
`<base image>:<base tag>--<branch>--<hash>`, with no repository. No command
uses them any more: each repository builds a new image the first time it
runs, and the old ones are left for `prune` to remove (see below).
`clean-all` removes both kinds.

`prune` covers every cached per-branch image in the local image store,
whichever repo built it. It is a **dry run by default**: it prints the repo
set and a keep or remove verdict, with the reason, for each image, and
removes nothing until re-run with `--apply` (issue #3481). An image in the
old format, with no repository, is removed whatever its branch, unless a
container uses it. For the rest, the tag names the repository only by its
directory name and the branch only in sanitized form, so the rule leans
toward keeping. An image is kept when:

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
vergil-tooling, copies the host's `~/.claude` config in and refreshes and
verifies the plugins in a running VM in place; `--all` updates every
configured identity's boxes, and one box's failure does not stop the rest.
`vrg-vm` is a human tool: it runs the VMs the agents work inside, so every
subcommand refuses an agent identity before doing anything (`--help` still
works). VMs install vergil-tooling with uv, one resolved release commit
constrained by its `uv.lock`, and `vrg-vm update` removes any vergil
package an older VM still carries;
`vrg-vm update --tag <ref>` with a non-release ref installs a dev copy
instead and flags it with a `DEV tooling` banner, and a plain
`vrg-vm update` returns the VM to its release. See
[VM Spec](vm-spec.md#vergil-tooling-inside-the-vm).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_vm` |
| Args | Global `--identity` (default: the configured default identity), `--config` (path to `identities.toml`); then a subcommand with its own flags (`vrg-vm <subcommand> --help`) |
| Preconditions | Run by the human (any agent identity is refused, exit 1); host with Lima (local VMs) or the off-platform backend configured; `identities.toml`; a valid host `~/.claude/settings.json` for `start`, `rebuild`, `session`, `update` and an off-platform `create` |
| Failure mode | Error on stderr; an agent identity is refused before any work; a missing or invalid host `~/.claude/settings.json` is refused, naming the file, before any VM is touched; `update --all` attempts every box and reports each failure |
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
`gh`, `git`, `bash`, `gcloud`, Lima, Docker Desktop, OpenTofu,
vergil-tooling, Claude Code, macOS, the Xcode Command Line Tools) declared in
the packaged, tooling-owned manifest
`src/vergil_tooling/data/host_dependencies.toml` (never
repo-overridable). `status` (the default) reports installed-vs-latest
drift, auxiliary-check health and pins; `status --fix` first runs the
remediations of unsatisfied checks. `upgrade` upgrades only `auto`
dependencies and holds actively pinned ones unless `--force`. `pin` /
`unpin` record holds in the user's `~/.config/vergil/host-pins.toml`
under the epic #155 pin-as-tech-debt doctrine. `plugins` refreshes the
host's Claude Code plugins from `~/.claude/settings.json` and verifies that
each declared marketplace is registered at its declared repo and ref (the
same refresh `vrg-vm update` runs in each VM); it refuses an agent identity.
Runs unprivileged and refuses `sudo`/`doas`/`su`/`pkexec`. See the
[vrg-host reference](dev/host.md).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_host` |
| Args | `status [--fix]` (default); `upgrade <dep>…` or `upgrade --all`, each with optional `--force`; `pin <dep> --constraint --inducing-release --reason [--tracking-issue] [--deterministic]`; `unpin <dep>`; `plugins` |
| Preconditions | macOS host with each dependency installed by its documented method (Homebrew for the `brew-formula` and `brew-cask` entries, including `uv`); `plugins` also needs the `claude` CLI and a valid `~/.claude/settings.json` |
| Failure mode | Per-dependency `probe-error` rows (never fatal to the report); loud error on an unreadable manifest or pins file; refused pins and upgrades print the reason; `plugins` names each failed step and each marketplace that is not at its declared repo and ref, and a missing, unreadable or invalid `~/.claude/settings.json` is a failure |
| Exit codes | `status`: 0 OK, 1 warnings, 2 errors; `upgrade`: 0 done, 1 a named dependency refused, 2 failure; `pin`/`unpin`: 0 success, 1 refused; `plugins`: 0 refreshed and verified, 1 refused (agent identity), 2 failure |
| Status | Active |

### vrg-tooling-install

Install a vergil-tooling release on this host with uv, locked to that
release's `uv.lock`. It takes an exact release `vX.Y.Z` or a line `vX.Y`. A
line is first resolved, with one `git ls-remote`, through its rolling tag
`vX.Y` to the one `vX.Y.Z` tag on the commit that tag names (the resolver
the dev-container cache and `vrg-vm` use), and that exact release is what it
installs and reports. In a temporary directory it shallow-fetches the tag over
git, reads the commit the tag names, exports that commit's `uv.lock` as
constraints (refusing an empty export or one naming a non-registry package),
and runs `uv tool install --force --no-config --python 3.15 --managed-python`
of that commit under them, with `UV_PYTHON`, `UV_PYTHON_PREFERENCE` and
`UV_NO_MANAGED_PYTHON` cleared. vergil-tooling's release runs it on the Mac
through its `[publish].install`; any other host runs it to upgrade. A
fresh host, or one whose installed copy fails, installs by hand (see
[Getting Started](../getting-started.md)).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_tooling_install` |
| Args | `vX.Y` (a release line) or `vX.Y.Z` (an exact release tag) |
| Preconditions | `git` and `uv` on PATH; network access to github.com |
| Failure mode | Names the failed step (line resolution, fetch, checkout, lock export, constraints check or install) and its error output on stderr; the temporary directory is removed either way |
| Exit codes | 0 installed; 1 a step failed, the line did not resolve, or the argument is neither `vX.Y` nor `vX.Y.Z`; 2 usage error |
| Status | Active |

### vrg-generate-commands

Generate MQSC command methods for all language ports (Python,
Go, Rust) from `mapping-data.json`. Updates target files
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
language's install, lint, typecheck, build, test and audit commands
(build only for a language that turns it on: Rust and TypeScript), the
semgrep scan of PR CI's `security / semgrep` gate, the Trivy
vulnerability scan of its `security / trivy` gate, the strict mkdocs
build of its `docs / docs` gate (a notice and a pass when there is no
`docs/site/mkdocs.yml`), and last the repository's custom validator (`scripts/bin/validate-custom` or
`vrg-validate-custom` on `PATH`) when it has one. `--check` runs one
stage. `--check custom` runs the install stage (when the language has
one) and then the custom stage; with no custom validator it prints one
"no custom validator" line and exits 0, and a validator that is present
but not an executable regular file fails. `--check build` for a
language with no build check prints one "nothing to run" line and exits
0. A `--check` of a language stage in a repo with no language declared
also exits 0, with a line naming the repo root it resolved, since PR CI
runs `--check lint` and `--check typecheck` in such repos too.
PR CI runs each stage through
the `vergil-actions` reusable workflows, the custom stage as
`quality / custom`. See the [Validation Matrix](../guides/validation-matrix.md)
for every check and where it runs. Every pipeline run, pass or fail,
writes `.vergil/vrg-validate-last.json`: the commit at the start, the exit
code, whether the run was full (no `--check`) and the tree clean at the
start, and each stage's status. `vrg-merge-review-request` reads it.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_validate` |
| Args | `--check {common,lint,typecheck,build,test,audit,semgrep,trivy,docs,custom}`, `--all-versions` (Python only; alone or with `--check test`) |
| Preconditions | Inside a dev container (run it as `vrg-container-run -- vrg-validate`) |
| Failure mode | Runs every stage after install and reports all failures in one run; install fails fast |
| Exit codes | 0 all passed, 1 a stage failed or the config is invalid, 2 usage error (for example `--check custom --all-versions`) |
| Status | Active |

### validate_common (internal)

Not a command on `PATH`: the module `vrg-validate` runs as its common
stage. Shared validation checks for all repos: the
[repo-profile](lint/repo-profile.md) check (`vergil.toml` and the
`README.md` structure), markdownlint on published markdown
(`docs/site/**/*.md`, `README.md` and `epics/**/*.md`) using the bundled canonical config,
the docs-links guard (over the same markdown less `epics/archive/`, a link
to a mike-versioned Vergil docs site must carry a `latest`, `dev` or
version-number segment after the site name, and a link to a site the org
does not publish fails), shellcheck on
`scripts/` and every `*.sh` file in the repository, yamllint on `.github/`
and `docs/` YAML files, the
dependabot-limit guard (when `.github/dependabot.yml` exists, every
`updates` entry must carry `open-pull-requests-limit: 0`, so Dependabot
can never open a PR), hadolint
on `Dockerfile*`, actionlint on `.github/workflows/`, the runner-labels
guard (no workflow value may be `ubuntu-latest`; pin runners to
`ubuntu-24.04`), and ansible-lint (using the bundled canonical config) when the repo
carries Ansible content.

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.validate_common` |
| Args | None |
| Preconditions | Git repo, `shellcheck` and `yamllint` on PATH |
| Failure mode | Propagates exit codes from each tool |
| Exit codes | 0 all passed, non-zero on first failure |
| Status | Active (called internally by `vrg-validate`) |

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
  `primary-version`, `primary-container-tag`, `container-tags`,
  `container-suffixes` and `build-enabled`.
- `--format github-output` — `versions`, `primary-version`,
  `primary-container-tag` and `build-enabled` as `key=value` lines for
  `$GITHUB_OUTPUT`.
- `build-enabled` is `"true"` when the repo's language has a build
  check (`vrg-validate --check build` has a command to run), else
  `"false"`. CI reads it to decide whether to run its build job.
- `--repo-root PATH` — the directory holding `vergil.toml` (default:
  the current directory).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_ci_resolve` |
| Args | `--format {json,github-output}`, `--repo-root PATH` |
| Preconditions | `vergil.toml` in the repo root; network access to the published window, or `VRG_VERSION_WINDOW` |
| Failure mode | `vrg-ci-resolve: error:` on stderr for a missing or invalid `vergil.toml`, an unreadable or malformed window, or a resolution error (a pin outside the window, an app pin of more than one version, a C++ repo, whose window has more than one family, with no pin) |
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

### vrg-npm-audit-gate

Give the TypeScript audit gate's verdict on an `npm audit --json` report:
drop the advisories a live entry of the repository's
`.npm-audit-exceptions` covers, list them, and fail on any other advisory
at high or critical. The TypeScript AUDIT check runs it after `npm audit`.
See
[TypeScript audit exceptions](../guides/validation-matrix.md#typescript-audit-exceptions).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_npm_audit_gate` |
| Args | `REPORT` (positional), `--npm-exit STATUS` (required: the status of the `npm audit --audit-level=high` run), `--exceptions PATH` (default `.npm-audit-exceptions`) |
| Preconditions | `REPORT` is an `npm audit --json` report (version 2); a missing exceptions file means none |
| Failure mode | Error on stderr for each advisory at high or critical with no live exception, each expired or malformed exception, an unreadable or error report, or an npm status that disagrees with the report; a warning for each exception that matches no advisory |
| Exit codes | 0 pass, 1 gate failure or error |
| Status | Active |

### vrg-npm-license-gate

Gate the licenses of every package in a TypeScript repository's
`package-lock.json`, dev dependencies included, against the TypeScript
license allowlist, honoring the live entries of the repository's
`.npm-license-exceptions`, and write the `licenses.json` evidence. The
TypeScript AUDIT check runs it after the npm audit, as
`vrg-npm-license-gate --output licenses.json`. See
[TypeScript license exceptions](../guides/validation-matrix.md#typescript-license-exceptions).

| Attribute | Value |
|---|---|
| Source | `vergil_tooling.bin.vrg_npm_license_gate` |
| Args | `--lockfile PATH` (default `package-lock.json`), `--output PATH` (default `licenses.json`) |
| Preconditions | The lockfile is an npm v2 or v3 lockfile; `.npm-license-exceptions` is read from the lockfile's directory, and a missing one means none |
| Failure mode | Error on stderr for each package whose license is disallowed, unparseable or missing with no live exception, naming its version and license value; for each expired or malformed exception; and for a missing, unreadable or pre-v2 lockfile or an entry with no `version`, naming the file and `npm install --package-lock-only`. A warning for each exception that covers no failing package. The report is written whenever the lockfile is read, judged against no exceptions when the exceptions file is bad |
| Exit codes | 0 pass, 1 gate failure or error |
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
| Failure mode | Exit 2 when semgrep itself fails, after printing semgrep's captured stdout and stderr; the error names the last non-empty stderr line (or stdout line) |
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
- 2: infrastructure error (used by the `vrg-validate` repo-profile
  check when `vergil.toml` is missing)

The 1-vs-2 distinction is not universal. Tools added before the
convention was established use 1 for all errors.
