# Git Workflow

This is the single entry point for how we do git across every managed
repository. It covers the overall shape of the workflow, the two
enforcement layers that back it up, and the per-change cycle you walk
through from branching to finalization.

For per-tool detail, each `vrg-*` command has its own reference page.
For the rationale behind the worktree convention,
see [the worktree convention spec][worktree-spec].

## At a glance

```text
new issue  →  branch  →  commit  →  PR  →  (human review)  →  merge  →  finalize
                  │         │        │                                      │
                  ▼         ▼        ▼                                      ▼
            feature/<N>  vrg-commit  vrg-submit-pr                    vrg-finalize-pr
```

- One issue per branch. Branch name encodes the issue number.
- `vrg-commit` builds standards-compliant commit messages. **Raw
  `git commit` is blocked.**
- `vrg-submit-pr` creates the PR with issue linkage. **Raw
  `gh pr create` is blocked.**
- Merging is **manual**. Auto-merge is disabled org-wide as of
  2026-04-22.
- After merge, `vrg-finalize-pr` pulls develop, deletes the merged
  branch and its worktree, and prunes remote refs.

For parallel work on multiple issues, use the worktree convention
(below) — every session starts at the project root, each concurrent
agent gets its own worktree under `.worktrees/`.

## Branching model

Every repository's branching model is declared in `vergil.toml`
as `[project].branching-model`. Three models are supported:

| Model | Prefixes allowed | Typical use |
|---|---|---|
| `library-release` | `feature/*`, `bugfix/*`, `hotfix/*`, `chore/*`, `release/*` | Libraries / packages that cut versioned releases |
| `application-promotion` | `feature/*`, `bugfix/*`, `hotfix/*`, `chore/*`, `promotion/*` | Apps that promote through environments |
| `docs-single-branch` | `feature/*`, `bugfix/*`, `chore/*` | Docs-only repos |

Protected branches — `main`, `develop`, and (for `library-release`)
`release` — do **not** accept direct commits. All changes go
through PRs against the appropriate integration branch (typically
`develop`).

Work branches must include the issue number in the name:

```text
feature/42-add-caching
bugfix/101-fix-null-pointer
chore/256-bump-dependency-versions
```

Pattern: `^(feature|bugfix|hotfix|chore)/[0-9]+-[a-z0-9][a-z0-9.-]*$`.
`vrg-git` applies it when the branch is created (`worktree add -b`,
`checkout -b`, `switch -c`, `branch <name>`, and a `branch -c`/`-m` copy
or rename to an issue-branch name) and `vrg-commit` applies it again at
commit time. Listing, deleting and upstream forms of `vrg-git branch` are
not checked.

At creation, `vrg-git` also reads issue `<N>` from the current repo. It
refuses the branch when the issue is closed, or is an epic or archive
(an umbrella is never closed by a PR). It also refuses when the issue
cannot be read, so check `vrg-gh issue view <N>` works and retry.

The `release/*` and `promotion/*` prefixes are created by automated
workflows and are exempt from the issue-number rule.

`deps/update-<version>` is the branch `vrg-update-deps` (and the
`vrg-release` `update-deps` stage) creates for a dependency update. It
is not one of the branching-model prefixes, so `vrg-commit` refuses to
commit on it. That is deliberate: only the driver commits there, and it
runs `git commit` itself rather than going through `vrg-commit`. Any
manual change goes on a fix branch, described next.

When a dependency update fails, its fix task's branch follows the
issue-number rule but starts from the failed update:
`chore/<fix-issue>-dep-update-<version>`, cut from
`origin/deps/update-<version>`. See
[When the dependency update fails](releasing.md#when-the-dependency-update-fails).

## Two enforcement layers

The branching, commit, and PR rules are enforced by two distinct
mechanisms. Both exist; they complement each other.

| Layer | Where it runs | Catches |
|---|---|---|
| **Claude Code hook guard** | `.claude/hooks/guard.sh` checked into each repo; wired via `.claude/settings.json` as a `PreToolUse` hook on the `Bash` matcher. Delegates to `vrg-hook-guard` (regex-based detection of raw `git`/`gh` commands). The five branch/context checks live in `vrg-commit` itself. | Raw `git` commands blocked • raw `gh` commands blocked • `vrg-commit` additionally checks: detached HEAD, protected branches, branch prefix, issue number |
| **Plugin PreToolUse hooks** | Delivered by the [`vergil-claude-plugin`](https://github.com/vergil-project/vergil-claude-plugin). Fires on Claude Code's `Bash`/`Write`/`Edit` tool invocations. | Raw `git commit` (forces `vrg-commit`) • Raw `gh pr create` (forces `vrg-submit-pr`) • commits originating from outside `.worktrees/*` on repos that have adopted the worktree convention • heredoc syntax in CLI args • associative-array bashisms |

**Why both?** The hook guard catches all raw `git`/`gh` commands
at the Claude Code tool level. The plugin catches additional
patterns (heredocs, worktree convention enforcement) that the
hook guard does not cover. Together they close the loop.

For the hook guard detail, see the
[Claude Code Hook Guard reference](../reference/hooks/pre-commit.md).
For every check and where it runs, see the
[Validation Matrix](validation-matrix.md).
For the plugin hook detail, see
[vergil-claude-plugin/docs → Hooks](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/hooks/index.md).

## Developing a change

Every change follows this path, whether or not other work is running
in parallel.

### 1. Create a worktree on a new branch

From the project root:

```bash
vrg-git fetch origin
vrg-git worktree add -b feature/42-add-caching \
  .worktrees/issue-42-add-caching origin/develop
cd .worktrees/issue-42-add-caching
```

`vrg-git` checks the branch name and the worktree directory when it
creates them, so name both correctly now. The directory must be
`.worktrees/issue-<N>-<slug>`, matching the branch
`<type>/<N>-<slug>`. See [Parallel work with worktrees](#parallel-work-with-worktrees)
for why every change gets its own worktree.

### 2. Commit with `vrg-commit`

```bash
vrg-commit \
  --type feat \
  --scope cache \
  --message "cache computed results" \
  --body "Adds an LRU cache to the computation pipeline..."
```

`vrg-commit`:

- Validates the commit type against conventional-commit standards
  (`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`,
  `build`).
- Adds no AI `Co-Authored-By` trailer: AI co-author attribution is
  not used, and Claude Code's own attribution is turned off in
  `.claude/settings.json` (#3200).
- Invokes `git commit` under the hood after all checks pass.

**Multi-line bodies go in a file**, not a heredoc. The plugin blocks
heredocs in CLI args because they cause escaping bugs:

```bash
vrg-commit --type feat --scope cache --message "…" --body "$(cat /tmp/body.txt)"
```

See the [vrg-commit reference](../reference/dev/commit.md) for the
full flag list.

### 3. Submit the PR with `vrg-submit-pr`

```bash
vrg-submit-pr \
  --issue 42 \
  --summary "Add LRU cache to pipeline" \
  --notes "$(cat /tmp/pr-notes.txt)"
```

`vrg-submit-pr`:

- Pushes the current branch to `origin`.
- Constructs a standards-compliant PR body with issue linkage,
  auto-selecting `Closes #42` for a managed task (so it closes on
  merge) or `Ref #42` for a legacy issue. `Fixes`/`Resolves` are
  not accepted.
- Creates the PR via `gh pr create` under the hood.

See the [vrg-submit-pr reference](../reference/dev/submit-pr.md).

!!! note "Agent handoff and the post-report-ready freeze"
    In an agent session, submission is a **human** action. The agent
    runs `vrg-pr-workflow report-ready`, which pushes the branch to origin
    (setting upstream) and then records the PR metadata in
    `.vergil/pr-workflow.json`, and stops; the human runs `vrg-submit-pr`.
    No separate push is needed. When the delta touches
    `.github/workflows/**` GitHub refuses the agent push; `report-ready`
    says so, records the state anyway, and the human's `vrg-submit-pr`
    pushes the branch instead. Any other push failure fails `report-ready`
    with nothing recorded. Recording ready **freezes the branch**: `vrg-commit`
    and the `vrg-git` push path refuse further commits/pushes on it,
    because a task is exactly one PR and more work is a new follow-up
    issue. One push stays allowed: an explicit push of the branch while
    HEAD is still exactly the reported commit, which publishes it without
    advancing it. Correcting the PR prose (re-running `report-ready`) is still
    allowed — its push is a no-op while the branch is on origin at the
    reported commit, and it refuses a HEAD that has drifted past that
    commit; deliberately reopening the branch needs
    `vrg-pr-workflow unfreeze`. See the tool's entry in the
    [CLI Tools Overview](../reference/cli-tools-overview.md#vrg-pr-workflow).

!!! note "Cloud (off-platform) agents: same handoff, over GitHub"
    A cloud x86 VM shares no disk with the Mac, so `report-ready`
    **always** also mirrors the ready-state onto the reserved relay ref
    `refs/vergil/pr-workflow/<branch>`. A cloud agent therefore does
    PR-development end-to-end — implement, push the branch, `report-ready`
    — exactly like a Lima agent, and the human submits from the Mac
    worktree-free with `vrg-submit-pr <branch> [<branch> …]` (see the
    [reference](../reference/dev/submit-pr.md#relay-branch-mode-cloud-to-mac-handoff)).
    The relay ref is world-readable on a public repo, so keep secrets out
    of the `report-ready` title/summary/notes.

### 4. Wait for review, merge manually

Once CI is green and a reviewer approves, merge through the GitHub
UI (squash merge for feature PRs). No automated merge will happen.

### 5. Finalize with `vrg-finalize-pr`

```bash
vrg-finalize-pr            # cleanup only, after merging in the UI
vrg-finalize-pr <PR>       # provenance check, merge, then cleanup
```

This:

- Switches back to the integration branch (`develop`).
- Pulls the latest.
- Deletes the merged local feature branch and removes its worktree,
  along with the branch's PR-workflow relay ref
  (`refs/vergil/pr-workflow/<branch>`).
- Prunes stale remote-tracking refs and sweeps any orphaned relay ref
  whose branch is gone.
- Runs post-merge validation via `vrg-container-run` to catch any
  regression introduced by the merge.

Run this immediately after the PR merges. A Stop hook in the plugin
will remind you if you try to end the session with an unsubmitted
or unfinalized PR still dangling.

A merged worktree the sweep cannot remove (a dirty tree, or a reused
branch name with unmerged commits) is not deleted silently —
`vrg-finalize-pr` surfaces it **prominently after the pipeline** so it
is impossible to miss, and `vrg-worktree-status` counts it under
**needs-attention** rather than hiding it as active. When the only dirt
is un-gitignored build/validation output, `--clean-dirty` clears it
after confirmation; better still, gitignore that output so the worktree
sweeps automatically.

See the [vrg-finalize-pr reference](../reference/dev/finalize-pr.md).

## Parallel work with worktrees

The main worktree at the project root stays on `develop` and is
read-only. Every change happens in its own worktree under
`.worktrees/`, on its own branch. This lets several agents work in the
same repository at once, and lets you start a second issue before the
first is merged.

For the full spec (rationale, trust model, failure modes, memory-path
implications), see
[the worktree convention][worktree-spec].
This section is the user-level "how to."

### What enforces it

Once `.worktrees/` exists in the project root:

- `vrg-git checkout` and `vrg-git switch` refuse to move the main
  worktree onto a work branch.
- `vrg-commit` refuses a work-branch commit made from the main
  worktree.
- `vrg-git worktree add` refuses a worktree outside `.worktrees/`, or
  one whose directory name does not match its branch.

`vrg-github-repo-init` adds `.worktrees/` to the managed `.gitignore`
block, so worktrees never show up as untracked files.

### Creating a worktree for an issue

From the project root:

```bash
vrg-git fetch origin
vrg-git worktree add -b feature/42-add-caching \
  .worktrees/issue-42-add-caching origin/develop
```

Directory layout:

```text
<project-root>/                           ← session starts here
  .git/
  CLAUDE.md, src/, docs/, …               ← main worktree (develop)
  .worktrees/                             ← gitignored, local-only
    issue-42-add-caching/                 ← your worktree, on feature/42-…
    issue-101-fix-npe/                    ← another agent's worktree
```

### Launching a session with a worktree assignment

**Always start the Claude Code session at the project root**, not
inside the worktree. The session's initial CWD determines the memory
slug; starting inside a worktree creates a separate memory silo and
loses shared project context.

Then give the agent the canonical prompt. The template is the
[Agent prompt contract](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md#agent-prompt-contract)
in the worktree convention, which each repo's CLAUDE.md links to;
filled in, it reads:

```text
You are working on issue #42: Add LRU cache to pipeline.

Your worktree is: <project-root>/.worktrees/issue-42-add-caching/
Your branch is:   feature/42-add-caching

Rules for this session:
- Do all git operations from inside your worktree:
    cd <absolute-worktree-path> && vrg-git <command>
- For Read / Edit / Write tools, use the absolute worktree path.
- For Bash commands that touch files, cd into the worktree first
  or use absolute paths.
- Do not edit files at the project root. The main worktree is
  read-only — all changes flow through your worktree on your
  feature branch.
- When you need to run validation, run it from inside your worktree
  (vrg-container-run mounts the current directory).
```

### Cleanup after merge

`vrg-finalize-pr` removes the finalized branch's worktree when it lives
under the canonical `.worktrees/` directory, so cleanup is normally
automatic. To remove one by hand, from the project root:

```bash
vrg-git worktree remove .worktrees/issue-42-add-caching
vrg-git branch -D feature/42-add-caching   # usually already deleted
vrg-git fetch --prune                      # usually already pruned
```

`vrg-git branch -D` deletes a branch only when its upstream is gone or
is an integration branch, and never deletes a protected branch.

## Releasing

Release flow is covered in its own guide:
[Releasing](releasing.md).

High-level summary:

1. Accumulate feature PRs on `develop`.
2. Run `vrg-release` from the root checkout. It creates the tracking
   issue and the `release/<version>` branch, generates the changelog,
   merges the release PR to `main`, waits for CD to tag and publish,
   merges the version bump back to `develop`, and cleans up.
3. As its last step, `vrg-release` runs the dependency update on the
   new `develop` version and opens a `dependency-update` PR when
   anything changed. If its validation or merge fails, the PR stays open
with a fix task, and the next
   release refuses to start until it is dealt with.

## Adoption for a new repo

`vrg-github-repo-init` creates a new managed repository, or adopts
an existing one with `--adopt`, and writes every managed file in its
canonical form: `vergil.toml`, `CLAUDE.md` (the packaged template
region, including its "Worktrees" section), `.claude/settings.json`,
`.claude/hooks/guard.sh`, the managed `.gitignore` block (including
`.worktrees/`) and the GitHub workflows. Two guides cover it:

- **[Getting Started](../getting-started.md)** — the quickstart.
- **[Consuming Repo Setup](consuming-repo-setup.md)** — full
  walkthrough with rationale, CI configuration, plugin details,
  and troubleshooting.

## Troubleshooting

### A hook blocked me. Which one, and why?

The hook that fired will print a reason. Common signals:

| Message fragment | Source | What to do |
|---|---|---|
| `"Raw git commit is blocked. Use vrg-commit"` | plugin | Use `vrg-commit` with the appropriate flags |
| `"Raw gh pr create is blocked. Use vrg-submit-pr"` | plugin | Use `vrg-submit-pr` |
| `"Heredoc syntax (<<EOF) is blocked"` | plugin | Write your multi-line content to a `/tmp/…` file, pass it via `$(cat <file>)` or `--body-file` |
| `"Commits must originate from inside .worktrees/<name>/"` | plugin (on repos that have adopted the worktree convention) | Create a worktree for your work and `cd` into it |
| `"Commits on protected branch \"develop\" are blocked"` | plugin or vrg-commit | Create a feature branch with the issue number in its name |
| `"direct commits to protected branches are forbidden"` | vrg-commit | Same — you're on `develop`/`release`/`main` directly |
| `"branch name must use {prefixes}"` | vrg-commit | Rename the branch to `feature/42-<slug>` or similar |
| `"branch name must include a repo issue number"` | vrg-commit | Add the issue number to the branch name |
| `"detached HEAD is not allowed for commits"` | vrg-commit | Create a named branch before committing |

### My plugin cache is stale

If you're running an old version of a hook, refresh and verify the plugin
on the host and every agent VM with `vrg-host plugins` and
`vrg-vm update --all`, in a human shell, then restart Claude Code (see
[Switching an existing machine](consuming-repo-setup.md#switching-an-existing-machine)
and
[Pick the move up on each machine](releasing.md#pick-the-move-up-on-each-machine)).
They verify only the marketplace repo and ref, which is `v2.1` before and
after a release, so confirm the installed version on each machine with
`claude plugin list --json`. The plugin README's
[Update section](https://github.com/vergil-project/vergil-claude-plugin#update)
describes the same pick-up of a release or a rollback, and its
[Verify the update](https://github.com/vergil-project/vergil-claude-plugin#verify-the-update)
subsection gives that check. Do not use the in-session
`/plugin marketplace update` and `/reload-plugins` pair instead: it does not
switch a changed `ref` and checks nothing.

## Related

- [Validation Matrix](validation-matrix.md)
  — every check and where it runs
- [Claude Code Hook Guard](../reference/hooks/pre-commit.md)
  — hook guard and `vrg-commit` check reference
- [Worktree convention spec][worktree-spec]
  — rationale, failure modes, trust model
- [Releasing](releasing.md) — release workflow detail
- [vergil-claude-plugin — Hooks][plugin-hooks]
  — plugin hook reference

[worktree-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md
[plugin-hooks]: https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/hooks/index.md
