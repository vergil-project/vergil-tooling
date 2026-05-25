# Git Worktree Convention for Parallel AI Agent Development

**Status:** Reviewed — `paad:pushback` complete; ready for `paad:alignment` and implementation planning
**Issue:** [#258](https://github.com/wphillipmoore/standard-tooling/issues/258)
**Related:** [#129](https://github.com/wphillipmoore/ai-research-methodology/issues/129) (PAAD integration), ai-research-methodology #135 (first PAAD pilot)
**Author:** wphillipmoore
**Last updated:** 2026-04-22
**Pushback review:** [`paad/pushback-reviews/2026-04-22-worktree-convention-pushback.md`](../../paad/pushback-reviews/2026-04-22-worktree-convention-pushback.md)

## Purpose

Define a repository-agnostic convention for running multiple AI coding
agents against the same project in parallel, without their working
trees colliding and without losing Claude Code's shared project memory.

The convention should be adopted by every actively developed repository
in the fleet (standard-tooling, ai-research-methodology,
the-infrastructure-mindset, mq-rest-admin-*, standards-and-conventions,
etc.). Adoption is per-repo — each repo gets its own CLAUDE.md entry
and `.gitignore` line. There is no automatic inheritance mechanism;
the canonical write-up in `standards-and-conventions` is a
human-readable reference, not a dependency.

## Problem statement

Claude Code derives the on-disk memory path from the working directory
at session start (path with separators replaced, used as a slug). Two
consequences follow:

- **Identical CWD across sessions → shared memory.** Every agent sees
  the same CLAUDE.md, the same feedback memories, the same project
  memories.
- **Different CWD → different memory silos.** An agent started in
  `~/dev/github/foo-2/` has no access to what an agent in
  `~/dev/github/foo/` has learned, and vice versa.

Running two agents in a single working tree produces the failure we
hit on 2026-04-22: one agent's uncommitted file moves broke a test the
other agent was validating. The collision is structural, not a
coordination lapse — nothing prevents two processes from editing the
same file at the same time.

The naive alternatives each break one of the two goals:

| Approach | Isolation | Shared memory | Verdict |
|----------|-----------|---------------|---------|
| Sibling clones (`foo/`, `foo-2/`) | Yes | No (different CWDs) | Loses shared memory |
| Devcontainers / containers | Yes | Yes (mount-dependent) | Heavyweight for docs/methodology work |
| Single tree, coordinate by hand | No | Yes | Original failure mode |
| **Git worktrees under project root** | **Yes** | **Yes** | **Proposed** |

## Proposed design

Use git worktrees rooted under the project's own checkout, and require
that every Claude Code session start at the project root regardless of
which worktree it will operate in.

### Structure

```text
~/dev/github/<project>/            ← sessions ALWAYS start here
  .git/                            ← main repo .git
  CLAUDE.md, src/, docs/, …        ← main worktree (usually `develop`)
  .worktrees/                      ← container for parallel worktrees
    issue-258-worktrees/           ← worktree on feature/258-worktrees
    issue-93-rerun/                ← worktree on feature/93-rerun
    …
```

- `.worktrees/` sits inside the project root. This is deliberate —
  placing it anywhere else would change the CWD slug for parallel
  sessions and break memory sharing.
- Each child of `.worktrees/` is a git worktree on its own branch.
  The directory name encodes the issue number and a short slug so
  humans and agents can disambiguate at a glance.
- `.worktrees/` is gitignored. It is a local working convention, never
  committed.

### Rules

1. **Sessions always start at the project root.** `cd
   ~/dev/github/<project> && claude` — never `cd
   ~/dev/github/<project>/.worktrees/<name> && claude`. This keeps
   the memory-path slug stable and shared.
2. **Each parallel agent is assigned exactly one worktree path.** The
   session prompt tells the agent which worktree it owns (see
   [Agent prompt contract](#agent-prompt-contract) below). Two
   sub-rules govern path usage inside the agent's session:
   - **For Read / Edit / Write tools:** always use the worktree's
     absolute path. This is already required by those tools, but the
     value must be the worktree path, not the main tree's path.
   - **For Bash commands that touch files** (`grep`, `cat`, `pytest
     tests/foo.py`, etc.): `cd` into the worktree first, or use
     absolute paths. Relative paths resolve against the Bash
     invocation's CWD — if the agent never `cd`-ed, that's the
     project root, not the worktree.
3. **The main worktree is read-only.** All edits flow through a
   worktree on a feature branch. This is the logical endpoint of the
   standing "no direct commits to develop" policy — if direct commits
   to develop were already forbidden, then by extension edits at the
   main worktree root have no legitimate destination. The same git
   hook that refuses direct pushes to develop is the natural place
   to refuse commits originating outside `.worktrees/*` (see
   [Enforcement and follow-up work](#enforcement-and-follow-up-work)).
4. **One worktree per issue.** Do not stack multiple in-flight issues
   in one worktree. When a worktree branch lands, remove the worktree
   before starting the next.
5. **Worktree naming: `issue-<N>-<short-slug>`.** `<N>` is the GitHub
   issue number; `<short-slug>` is 2–4 kebab-case tokens describing
   the work. Shared format lets both humans and scripts match worktree
   to issue.

### Agent prompt contract

The convention depends on each parallel agent being told, up front,
which worktree it owns and how to behave inside it. A prompt that
omits any of the fields below leaves the agent's behavior undefined;
in the worst case, the agent defaults to the project root and
reproduces the original collision.

**Canonical prompt template** (fill in the placeholders when launching
a parallel-agent session):

```text
You are working on issue #<N>: <issue title>.

Your worktree is: <absolute-path-to-project-root>/.worktrees/issue-<N>-<slug>/
Your branch is:   feature/<N>-<slug>

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

All fields are required. The template is intended as a copy-paste
starting point; an operator launching multiple parallel agents fills
in the placeholders for each one. A future `vrg-worktree-prompt` helper
could emit this text given `<issue-number>` and `<slug>` — see
[Enforcement and follow-up work](#enforcement-and-follow-up-work).

### Onboarding a new worktree

When starting work on issue `N`:

```bash
cd ~/dev/github/<project>                      # always from root
git fetch origin
git worktree add .worktrees/issue-N-<slug> -b feature/N-<slug> origin/develop
```

Then launch a Claude Code session from the project root and give the
agent the prompt described in
[Agent prompt contract](#agent-prompt-contract).

**Stacked worktrees (exception).** For the occasional case where work
must stack on an unmerged parent branch, substitute the parent for
`origin/develop` in the worktree-add command (e.g., `git worktree add
.worktrees/issue-B-<slug> -b feature/B-<slug> feature/A-<parent>`).
The author owns rebasing as the parent evolves. Stacking is
discouraged as a habit — prefer to defer dependent work or integrate
it into the parent issue; the mechanism exists only for the rare case
where neither is viable.

### Cleanup

Post-merge cleanup in this fleet runs through `vrg-finalize-repo`.
Worktree removal should live there too — the canonical flow becomes:

```bash
cd ~/dev/github/<project>
vrg-finalize-repo                              # handles branch delete,
                                               # remote prune, and
                                               # worktree removal for
                                               # the finalized branch
```

Extending `vrg-finalize-repo` to be worktree-aware is tracked as
follow-up work (see
[Enforcement and follow-up work](#enforcement-and-follow-up-work)).
Until that lands, the interim procedure is raw git:

```bash
cd ~/dev/github/<project>
git worktree remove .worktrees/issue-N-<slug>
git branch -D feature/N-<slug>           # local branch, already merged upstream
git fetch --prune                        # drop deleted remote tracking refs
```

If the worktree directory lingers after a crash or force-removed
branch, `git worktree prune` cleans up stale metadata. A separate
`vrg-worktree-prune` for abandoned branches that never merged is not
part of this spec — add it only if the abandoned-branch case proves
to need tooling in practice.

### Failure modes

| Failure | Trigger | Mitigation |
|---------|---------|------------|
| Agent runs git at the project root, not in its worktree | Agent forgets to `cd`; commits land on main tree's current branch | Prompt contract + CLAUDE.md convention; git hook that refuses commits from outside `.worktrees/*` (shared backstop with rule 3) |
| Edit at the main tree root | Rule 3 ignored, or a human edit slipped in | Standing "no direct commits to develop" policy forbids this at the commit level; same git hook catches it |
| Memory silo from starting session in the wrong directory | Session started inside `.worktrees/<name>/` instead of the root | Documentation + muscle memory; a `claude` wrapper script could refuse to launch unless CWD is a project root |
| Agent writes to a sibling worktree or the main tree (intentional or mis-prompted) | Prompt contract ignored or misread | Git hook (same backstop) catches any such write that actually gets committed; see [Trust model](#trust-model) for the limits of isolation |
| Stale worktrees accumulate after branch land | Author forgets the cleanup step | `vrg-finalize-repo` will remove the worktree as part of its post-merge flow once extended |
| Worktree branch gets into weird state on rebase of main | Shared history diverges during long-lived worktree | Normal git hygiene: rebase the worktree's branch onto updated `develop` periodically; no special convention needed |

### Trust model

What the convention buys, and what it does not:

- **Git-level isolation (structural).** Each worktree has its own
  HEAD and index. A commit in one worktree cannot touch another
  worktree's branch state. This is the guarantee that prevents the
  2026-04-22 collision.
- **Accidental-path-resolution isolation (structural, via rule 2).**
  Agents using absolute worktree paths and `cd`-ed Bash cannot
  accidentally write to the wrong tree.
- **Filesystem sandbox (not provided).** Nothing prevents an agent
  from writing outside its worktree if its prompt is misread or
  ignored. The `Write` tool does not check that its target is within
  the assigned worktree.

The backstop for the filesystem-sandbox gap is the enforcement
tooling described in
[Enforcement and follow-up work](#enforcement-and-follow-up-work). A
git hook that refuses commits from outside `.worktrees/*` catches any
out-of-worktree write that actually gets committed. Uncommitted
out-of-worktree edits remain a residual risk, caught (if at all) by
human review.

A true filesystem sandbox — devcontainers that mount only the
assigned worktree read-write with everything else read-only — would
close this gap but would also interact with the "sessions start at
the root" memory-path requirement and is a larger scope expansion.
Not proposed here.

### Memory-path implications

This is the load-bearing constraint for the whole design.

Claude Code's memory path for a session is derived from the session's
starting CWD. For `~/dev/github/foo/`, the slug is
`-Users-pmoore-dev-github-foo` (or similar, platform-dependent). Two
sessions share memory if and only if their derivation produces the
same slug.

- Starting a session at `~/dev/github/foo/` and then `cd`-ing to
  `~/dev/github/foo/.worktrees/issue-258/` **does not** change the
  slug. The session already registered its memory path; subsequent
  `cd` calls are cosmetic for that session.
- Starting a session directly at
  `~/dev/github/foo/.worktrees/issue-258/` **does** produce a
  different slug and therefore a separate memory silo. This is the
  failure mode rule #1 prevents.
- Sibling clones (`~/dev/github/foo-2/`) produce a different slug
  even though the repo content is identical — this is why sibling
  clones cannot share memory.

The convention only works because git worktrees live *inside* the
project root directory. Any future change that moves `.worktrees/`
outside the root (e.g., a global `~/.worktrees/<project>/<branch>`
layout) would re-introduce the memory split.

### Rollout plan

The convention is a development methodology, not a feature of any
single repo. Rollout sequencing (tooling-first, then pilot, then
cascade, then canonicalize):

1. **standard-tooling** (this repo) — `.gitignore` entry for
   `.worktrees/`; CLAUDE.md section describing the convention and
   the Agent prompt contract. This lands first so the tooling repo
   itself carries the canonical text before cascading.
2. **Pilot in an active-parallel repo** — `the-infrastructure-mindset`
   or `ai-research-methodology`, whichever has imminent parallel
   work. Apply the convention end-to-end (launch parallel agents,
   observe behavior, iterate on the prompt template or rules if the
   pilot surfaces gaps). Pilot output feeds back into the spec.
3. **Fleet cascade** — apply the `.gitignore` + CLAUDE.md change to
   the remaining active consuming repos in order of likelihood of
   needing parallel agents: other of the two active-parallel repos,
   `mq-rest-admin-python` (most-active Python repo), then the rest
   of `mq-rest-admin-*`, `standard-actions`, `standard-tooling-docker`,
   `standard-tooling-plugin`, `mempalace`.
4. **Canonicalize in `standards-and-conventions`** — add the
   generalized write-up to the standards repo so future repos have a
   reference. This lands **last**, reflecting what the pilot + cascade
   actually validated. No automatic inheritance is implied — each
   repo adopts by adding its own CLAUDE.md entry.
5. **Agent-facing feedback memory (per-repo, where the repo permits
   it)** — capture the rule "when told to work on issue N, your
   worktree is `.worktrees/issue-N-<slug>/`; do git ops there, use
   absolute paths or cd for Bash" as a feedback memory, so agents
   onboard without re-reading CLAUDE.md each session. See
   [Adjacent work surfaced during pushback](#adjacent-work-surfaced-during-pushback)
   — this depends on lifting the MEMORY.md ban in repos where it
   still exists.

Each repo's rollout is a small PR (`.gitignore` line +
CLAUDE.md paragraph); the cascade is wide but shallow.

### Enforcement and follow-up work

Enforcement is a safety net, not the primary mechanism. The primary
mechanism is telling the agent correctly up front via the Agent prompt
contract. Enforcement backstops prompt drift, prompt omission, or
agent mis-reads.

Candidate enforcement mechanisms (to be designed as follow-up work,
tracked separately):

- **Git hook.** A pre-commit (or pre-push) hook that:
  - Refuses direct commits to `develop` (already a standing policy —
    this spec piggybacks rather than adding).
  - Refuses commits originating from the project root when
    `.worktrees/*` is non-empty (or, more simply, refuses all commits
    from the project root, since rule 3 says the main tree is
    read-only).
- **`vrg-finalize-repo` extension.** Worktree removal when finalizing
  the branch for that worktree. Covers the cleanup failure mode
  structurally.
- **`vrg-worktree-prompt` helper.** Emits the canonical prompt
  template given an issue number and slug, so operators don't
  hand-author the prompt each time.
- **Claude Code plugin.** Hooks at session start (or on work-item
  assignment) that detect the intent to work on issue N and produce
  the worktree + prompt automatically. Open-ended — may or may not be
  worth building; depends on how often parallel-agent launches are
  manual.

**Design principle for all of these:** the convention works without
them. They raise the floor on consistency; they do not gate adoption.

## Out of scope

- **Automated worktree provisioning for the initial adoption** (e.g.,
  `vrg-worktree-add <issue-number>`). Tracked as follow-up.
- **Devcontainer integration.** Worktrees and devcontainers are
  orthogonal; a worktree can run inside or outside a devcontainer.
  This spec does not prescribe.
- **Multi-repo worktrees.** Worktrees that span repositories
  (e.g., a coordinated change across standard-tooling and
  ai-research-methodology) are not addressed here; each repo's
  worktree is independent.
- **Concurrent editing of the same file across worktrees.** Two
  worktrees editing the same file on different branches is a
  merge-conflict problem, not an isolation problem, and is already
  handled by git's normal flow.
- **Filesystem sandboxing** (devcontainer-based worktree isolation).
  See [Trust model](#trust-model). Larger scope; not proposed here.

## Adjacent work surfaced during pushback

These are not part of this spec's implementation, but the pushback
review surfaced them as coupled items that should be addressed
separately:

- **Remove the MEMORY.md ban in this repo's CLAUDE.md.** The ban was
  added to solve a polyglot-convergence problem (getting consistent
  agent behavior across five per-language repos via memory). The
  author has since found feedback memory to be high-value in active
  repositories (notably `the-infrastructure-mindset`). The ban is an
  anachronism and should be removed here and anywhere else it lives.
  It blocks rollout step 5 above.
- **Deprecate the "include this doc and remember it" pattern
  fleet-wide.** The include/reference mechanism referenced by older
  specs (e.g., "new repos pick it up via standards-and-conventions")
  did not work in practice — context overload. Replace with explicit
  per-repo CLAUDE.md entries. This is the model this spec already
  assumes.

## Acceptance criteria (from #258)

- [x] Design spec authored covering structure, rules, onboarding,
  cleanup, failure modes, memory-path implications, cross-repo
  propagation
- [x] `paad:pushback` run on the spec; findings addressed (this
  revision) or explicitly deferred to follow-up
- [ ] `paad:alignment` run between spec and implementation plan
- [ ] This repo: `.gitignore` + CLAUDE.md updated
- [ ] Pilot in `the-infrastructure-mindset` or `ai-research-methodology`
- [ ] Fleet cascade: other active-parallel repo, then
  `mq-rest-admin-*`, `standard-actions`, `standard-tooling-docker`,
  `standard-tooling-plugin`, `mempalace`
- [ ] `standards-and-conventions`: canonical convention doc (lands
  last, post-pilot)
- [ ] Agent-facing feedback memory captured per-repo (blocked until
  the MEMORY.md ban is lifted where it still exists)
- [ ] Retrospective notes added to ai-research-methodology #129
