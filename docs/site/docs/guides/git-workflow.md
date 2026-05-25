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
            feature/<N>  vrg-commit  vrg-submit-pr                    vrg-finalize-repo
```

- One issue per branch. Branch name encodes the issue number.
- `vrg-commit` builds standards-compliant commit messages. **Raw
  `git commit` is blocked.**
- `vrg-submit-pr` creates the PR with issue linkage. **Raw
  `gh pr create` is blocked.**
- Merging is **manual**. Auto-merge is disabled org-wide as of
  2026-04-22.
- After merge, `vrg-finalize-repo` pulls develop, deletes the merged
  branch, and prunes remote refs.

For parallel work on multiple issues, use the worktree convention
(below) — every session starts at the project root, each concurrent
agent gets its own worktree under `.worktrees/`.

## Branching model

Every repository's branching model is declared in
`docs/repository-standards.md` under the `branching_model`
attribute. Three models are supported:

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

Pattern: `^(feature|bugfix|hotfix|chore)/[0-9]+-[a-z0-9][a-z0-9-]*$`.

The `release/*` and `promotion/*` prefixes are created by automated
workflows and are exempt from the issue-number rule.

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

For the hook guard detail, see
[Git Hooks and Validation][hooks-doc].
For the plugin hook detail, see
[vergil-claude-plugin/docs → Hooks](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/hooks/index.md).

## Developing a change

The common path, for work that isn't running in parallel:

### 1. Branch from the integration branch

```bash
git fetch origin develop
git checkout -b feature/42-add-caching origin/develop
```

`vrg-commit` enforces the prefix and issue-number rule when
you try to commit, so name the branch correctly now.

### 2. Commit with `vrg-commit`

```bash
vrg-commit \
  --type feat \
  --scope cache \
  --message "cache computed results" \
  --body "Adds an LRU cache to the computation pipeline..." \
  --agent claude
```

`vrg-commit`:

- Validates the commit type against conventional-commit standards
  (`feat`, `fix`, `docs`, `style`, `refactor`, `test`, `chore`, `ci`,
  `build`).
- Resolves the AI agent's `Co-Authored-By` trailer from
  `docs/repository-standards.md`.
- Invokes `git commit` under the hood after all checks pass.

**Multi-line bodies go in a file**, not a heredoc. The plugin blocks
heredocs in CLI args because they cause escaping bugs:

```bash
vrg-commit --type feat --message "…" --body "$(cat /tmp/body.txt)" --agent claude
```

See the [vrg-commit reference](../reference/dev/commit.md) for the
full flag list.

### 3. Submit the PR with `vrg-submit-pr`

```bash
vrg-submit-pr \
  --issue 42 \
  --summary "Add LRU cache to pipeline" \
  --linkage Fixes \
  --notes "$(cat /tmp/pr-notes.txt)"
```

`vrg-submit-pr`:

- Pushes the current branch to `origin`.
- Constructs a standards-compliant PR body with issue linkage
  (`Ref #42`). Auto-close keywords (`Fixes`, `Closes`,
  `Resolves`) are not accepted.
- Creates the PR via `gh pr create` under the hood.

!!! note "Auto-merge is disabled"
    As of 2026-04-22 all managed repos have `allow_auto_merge=false`.
    `vrg-submit-pr` still tries to enable auto-merge after PR creation
    and will print a non-fatal `CalledProcessError` traceback — the
    PR itself is created successfully and simply waits for a human
    merge. Removing that call is tracked in
    [vergil-tooling#268](https://github.com/vergil-project/vergil-tooling/issues/268).

See the [vrg-submit-pr reference](../reference/dev/submit-pr.md).

### 4. Wait for review, merge manually

Once CI is green and a reviewer approves, merge through the GitHub
UI (squash merge for feature PRs). No automated merge will happen.

### 5. Finalize with `vrg-finalize-repo`

```bash
vrg-finalize-repo
```

This:

- Switches back to the integration branch (`develop`).
- Pulls the latest.
- Deletes the merged local feature branch.
- Prunes stale remote-tracking refs.
- Runs post-merge validation via `vrg-container-run` to catch any
  regression introduced by the merge.

Run this immediately after the PR merges. A Stop hook in the plugin
will remind you if you try to end the session with an unsubmitted
or unfinalized PR still dangling.

See the [vrg-finalize-repo reference](../reference/dev/finalize-repo.md).

## Parallel work with worktrees

Use the worktree convention when running **more than one agent in
the same repo at the same time**, or when you want to start a second
issue before the first is merged.

For the full spec (rationale, trust model, failure modes, memory-path
implications), see
[the worktree convention][worktree-spec].
This section is the user-level "how to."

### When a worktree is required

- Two or more Claude Code sessions are going to work concurrently
  on the same repo.
- The main working tree at the project root is serving as the
  "canonical state" for one agent while another starts work.

### When a worktree is optional but recommended

- Any feature branch, even for solo sequential work. The convention
  keeps the main tree clean; once the plugin-level CWD check is
  active (release tracked in
  [vergil-claude-plugin#46](https://github.com/vergil-project/vergil-claude-plugin/issues/46))
  committing from the main tree will be blocked.

### Creating a worktree for an issue

From the project root:

```bash
git fetch origin develop
git worktree add \
  .worktrees/issue-42-add-caching \
  -b feature/42-add-caching \
  origin/develop
```

Directory layout:

```text
~/dev/github/<repo>/                      ← session starts here
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

Then give the agent the canonical prompt (a template lives in each
repo's CLAUDE.md under "Parallel AI agent development → Agent prompt
contract"):

```text
You are working on issue #42: Add LRU cache to pipeline.

Your worktree is: /Users/pmoore/dev/github/<repo>/.worktrees/issue-42-add-caching/
Your branch is:   feature/42-add-caching

Rules for this session:
- Do all git operations from inside your worktree:
    cd <absolute-worktree-path> && git <command>
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

`vrg-finalize-repo` will handle worktree removal once
[vergil-tooling#260](https://github.com/vergil-project/vergil-tooling/issues/260)
lands. Until then, do it by hand alongside the usual finalization:

```bash
cd ~/dev/github/<repo>
git worktree remove .worktrees/issue-42-add-caching
git branch -D feature/42-add-caching    # usually already deleted
git fetch --prune                        # usually already pruned
```

## Releasing

Release flow is covered in its own guide:
[Releasing](releasing.md).

High-level summary:

1. Accumulate feature PRs on `develop`.
2. Run `vrg-prepare-release --issue <N>` to cut a `release/<version>`
   branch, generate the changelog, and open a PR to `main`.
3. Merge the release PR manually. CI auto-tags, creates the GitHub
   Release, publishes the package artifact, and opens a
   version-bump PR back to `develop`.
4. `vrg-finalize-repo` as usual.

## Adoption for a new repo

Onboarding a brand-new repository has two entry points:

- **[Getting Started](../getting-started.md)** — five-to-ten minute
  quickstart.
- **[Consuming Repo Setup](consuming-repo-setup.md)** — full
  walkthrough with rationale, CI configuration, plugin nuances,
  and troubleshooting.

At a minimum, a new repo needs:

1. `docs/repository-standards.md` with the six required attributes
   (see the existing setup guide).
2. `.claude/hooks/guard.sh` and `.claude/settings.json` wiring
   the Claude Code hook guard to block raw `git`/`gh` commands.
3. `.claude/settings.json` also enabling the `vergil-tooling`
   plugin so the plugin hooks fire in Claude Code sessions.
4. `.worktrees/` in `.gitignore` and a Parallel-AI-agent-development
   section in CLAUDE.md so the worktree convention applies.

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

Until plugin release automation is fully stood up
([vergil-claude-plugin#46](https://github.com/vergil-project/vergil-claude-plugin/issues/46)),
the plugin is consumed from a local checkout and its cache under
`~/.claude/plugins/cache/vergil-tooling-marketplace/`. If you're
running an old version of a hook:

```bash
git -C ~/.claude/plugins/marketplaces/vergil-tooling-marketplace pull
# then restart Claude Code
```

### `vrg-submit-pr` threw a CalledProcessError on auto-merge

Expected since 2026-04-22 — auto-merge is disabled org-wide. The
PR itself was created successfully. See
[vergil-tooling#268](https://github.com/vergil-project/vergil-tooling/issues/268)
for the planned fix.

### Validator-not-on-PATH errors during PostToolUse

The plugin's `validate-on-edit` hook expects those validators on
PATH. Some are only installed inside the dev container image, not on
the host. Hook-level PATH discovery is tracked in
[vergil-tooling#265](https://github.com/vergil-project/vergil-tooling/issues/265).
Workaround: run the validator explicitly via `vrg-container-run` to
confirm the file is clean; then proceed.

## Related

- [Git Hooks and Validation][hooks-doc]
  — pre-commit hook + validator reference
- [Worktree convention spec][worktree-spec]
  — rationale, failure modes, trust model
- [Releasing](releasing.md) — release workflow detail
- [vergil-claude-plugin — Hooks][plugin-hooks]
  — plugin hook reference

[hooks-doc]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/git-hooks-and-validation.md
[worktree-spec]: https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md
[plugin-hooks]: https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/hooks/index.md
