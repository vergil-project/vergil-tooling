# Repository Configuration Audit Design

**Issue:** #822
**Date:** 2026-05-18
**Status:** Draft

## Problem

The rules required for vergil-tooling to function correctly in a
consuming repository — use `vrg-git` not `git`, use
`vrg-container-run -- vrg-validate` not individual linters, use the
vergil memory management skills — only exist as prose in
vergil-tooling's own CLAUDE.md. Consuming repos either have stale
partial copies, copies with drift (wrong skill namespace, extra
`uv run` prefix), or nothing at all.

When a consuming repo lacks these rules:

- Agents use raw `git`/`gh`, hit deny rules, and work around them
- Agents run `pip install` instead of `uv`
- Agents run individual linters instead of `vrg-validate`
- Agents create worktrees with wrong naming or structure
- Agents write to memory without human approval, causing
  behavioral drift across repositories

The CLAUDE.md rules are advisory — they do not enforce anything.
But without them, agents do not know what the wrapper scripts are
or why raw commands are denied, which increases workaround
attempts. The `.claude/settings.json` deny rules are the
enforcement layer, but agents work around those roughly half the
time. Both layers are needed because each catches what the other
misses.

There is currently no way to audit whether a consuming repo has the
required configuration in place. The existing `vrg-github-config`
tool only checks GitHub API settings (branch protection, repo
settings, etc.) and has no awareness of local filesystem
configuration.

## Strategic Context

This work is part of the interim architecture described in the
permission model design (`2026-05-14-permission-model-design.md`).
The long-term target is full containerization with isolated
credentials and a locked-down command namespace where agents can
only run wrapper scripts. Until that architecture is in place,
CLAUDE.md rules, settings.json deny rules, and wrapper scripts
form a layered defense:

1. **CLAUDE.md** — advisory guidance ("please do the right thing")
2. **`.claude/settings.json` deny rules** — enforcement that
   agents work around roughly half the time
3. **Wrapper scripts (`vrg-git`, `vrg-gh`, etc.)** — actual
   enforcement, the only layer that reliably works

All three layers are required. This spec addresses auditing layers
1 and 2 at the local filesystem level.

## Design

### 1. Shared CLAUDE.md Template

A single file containing the exact text that every consuming
repository must include verbatim in its CLAUDE.md. This is not a
generator or a set of patterns — it is a literal block of text
that the audit tool searches for as a contiguous substring.

**Location:** `src/vergil_tooling/data/claude_md_consumer.md`

**Content (four sections):**

#### Section 1 — Memory management

```markdown
## Memory management

Memory is allowed with human approval. The authoritative policy is in
the user's global `~/.claude/CLAUDE.md` — agents must propose memory
writes and suggest a destination (repo memory, global CLAUDE.md, or
plugin/skill issue) before writing. See that file for the full
workflow.

Available skills:
- `/vergil:memory-init` — set up or update the policy header
  in a project's `MEMORY.md`.
- `/vergil:memory-audit` — structured collaborative review
  of memory files.
```

#### Section 2 — Parallel AI agent development

````markdown
## Parallel AI agent development

This repository supports running multiple Claude Code agents in parallel via
git worktrees. The convention keeps parallel agents' working trees isolated
while preserving shared project memory (which Claude Code derives from the
session's starting CWD).

**Canonical spec:**
[`vergil-tooling/docs/specs/worktree-convention.md`](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/worktree-convention.md)
— full rationale, trust model, failure modes, and memory-path implications.
The canonical text lives in `vergil-tooling`; this section is the local
on-ramp.

### Structure

```text
<project-root>/                              ← sessions ALWAYS start here
  .git/
  CLAUDE.md, …                               ← main worktree (usually `develop`)
  .worktrees/                                ← container for parallel worktrees
    issue-<N>-<short-slug>/                  ← worktree on feature/<N>-<short-slug>
    …
```

### Rules

1. **Sessions always start at the project root.**
   Never start Claude from inside `.worktrees/<name>/`. This keeps the
   memory-path slug stable and shared.
2. **Each parallel agent is assigned exactly one worktree.** The session
   prompt names the worktree (see Agent prompt contract below).
   - For Read / Edit / Write tools: use the worktree's absolute path.
   - For Bash commands that touch files: `cd` into the worktree first,
     or use absolute paths.
3. **The main worktree is read-only.** All edits flow through a worktree
   on a feature branch — the logical endpoint of the standing
   "no direct commits to develop" policy.
4. **One worktree per issue.** Don't stack in-flight issues. When a
   branch lands, remove the worktree before starting the next.
5. **Naming: `issue-<N>-<short-slug>`.** `<N>` is the GitHub issue
   number; `<short-slug>` is 2–4 kebab-case tokens.

### Agent prompt contract

When launching a parallel-agent session, use this template (fill in the
placeholders):

```text
You are working on issue #<N>: <issue title>.

Your worktree is: <project-root>/.worktrees/issue-<N>-<slug>/
Your branch is:   feature/<N>-<slug>

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

All fields are required.
````

#### Section 3 — Shell command policy

```markdown
## Shell command policy

Use `vrg-git` instead of `git` for all git operations. Use `vrg-gh`
instead of `gh` for all GitHub CLI operations. These wrappers enforce
subcommand allowlists, flag deny lists, credential selection, and
audit logging.

Raw `git` and `gh` are denied by the permission model. If a command
is not available through the wrappers, explain the situation to the
human who can run it directly via `! <command>` in the prompt.
```

#### Section 4 — Validation

````markdown
## Validation

```bash
vrg-container-run -- vrg-validate
```

This is the **only** validation command. Do not run individual linters,
formatters, or other tools outside of `vrg-validate`. If a tool is not
invoked by `vrg-validate`, it is not part of the validation pipeline.
````

#### Template properties

- The four sections appear in this order as one contiguous block
- Consuming repos include this block verbatim in their CLAUDE.md
- Repos may have additional content before and after the block
  (project overview, architecture, development commands, etc.)
- The block should appear near the top of the file, after any
  introductory line

#### vergil-tooling exception

vergil-tooling includes the standard block verbatim. Immediately
after the validation section, it appends an override:

> **Note:** This repository uses
> `vrg-container-run -- uv run vrg-validate` because it runs its own
> unreleased code rather than the pre-installed version.

The audit tool does not need special-case logic — it checks for the
substring and finds it regardless of what follows.

### 2. Rename `vrg-github-config` → `vrg-github-repo-config`

`vrg-github-config` is renamed to `vrg-github-repo-config`. The
old command is removed entirely — no deprecation shim, no backward
compatibility. All references across all repos are updated as part
of the rollout sweep.

The `--owner`/`--project` flags for project-wide scanning are
dropped from this tool. Project-level auditing (including running
repo config on each constituent repo) moves to a new
`vrg-github-project-config` tool, tracked as a separate issue.

The new tool combines local filesystem checks with the existing
GitHub API configuration checks.

**CLI interface:**

```
vrg-github-repo-config audit [--repo OWNER/REPO] [--config PATH]
vrg-github-repo-config diff  [--repo OWNER/REPO] [--config PATH]
vrg-github-repo-config apply [--repo OWNER/REPO] [--config PATH]
```

- `--repo OWNER/REPO` — target a specific GitHub repo. Default:
  inferred from the current directory's git remote.
- `--config PATH` — override the `vergil.toml` location (carried
  forward from `vrg-github-config`; required for bootstrapping
  scenarios where you modify the config and then verify it).

Both local filesystem checks and GitHub API checks always run.

**Modes:**

| Mode | Local checks | GitHub checks | Exit code |
|------|-------------|---------------|-----------|
| `audit` | yes (read-only) | yes | 0 = compliant, 1 = non-compliant |
| `diff` | yes (read-only) | yes | always 0 |
| `apply` | yes (read-only) | yes (applies fixes) | 0 = all applied, 1 = local issues remain |

Local filesystem checks are always read-only — the tool never
writes to local files. In `apply` mode, GitHub API settings are
fixed programmatically (existing behavior), but local issues are
only reported. Correcting a drifted CLAUDE.md or `.claude/settings.json`
requires human judgment that the Python code cannot provide.

Local checks produce three possible outcomes per file:
- **correct** — pass
- **wrong** (exists but fails audit) — fail, report the diff
- **missing** — fail, report as absent

Bootstrapping missing files (creating a CLAUDE.md from the
template, seeding a default `settings.json`) belongs in a separate
init tool, not in this audit tool. An audit tool that writes files
as a side effect would violate the branch model when agents run it
at workflow start on the develop branch.

### 3. Local Filesystem Checks

All checks are pure filesystem operations — no API calls, no
network access. They reuse the existing `ConfigDiff`/`DiffItem`
types from `github_config.py` for unified output.

**Implementation:** `src/vergil_tooling/lib/repo_config.py`

**Public entry point:**

```python
def audit_local_config(repo_root: Path) -> ConfigDiff:
```

**Checks:**

| Check | Field prefix | Pass condition |
|-------|-------------|----------------|
| `vergil.toml` | `local.vergil_toml` | File exists, valid TOML, required fields present (delegates to existing `read_config()`) |
| `.githooks/pre-commit` | `local.githooks_pre_commit` | File exists |
| `CLAUDE.md` template | `local.claude_md` | File exists and contains the template block as a verbatim contiguous substring |
| `.claude/settings.json` | `local.claude_settings` | File exists, valid JSON object |
| Marketplace config | `local.claude_settings.marketplace` | `extraKnownMarketplaces.vergil-marketplace` configured with correct source repo |
| Plugin enabled | `local.claude_settings.plugin` | `enabledPlugins.vergil@vergil-marketplace` is `true` |
| Deny rules | `local.claude_settings.deny_rules` | `permissions.deny` contains at minimum `Bash(git *)`, `Bash(*/git *)`, `Bash(gh *)`, and `Bash(*/gh *)` |

**CLAUDE.md matching algorithm:**

1. Read the template from package data
   (`importlib.resources` or `Path(__file__).parent`)
2. Read the repo's CLAUDE.md
3. Check whether the template text appears as a substring of the
   CLAUDE.md content
4. If not found: report `local.claude_md` as non-compliant

The match is exact. No normalization, no pattern matching, no
section-by-section comparison. If the template is not present
verbatim, the check fails.

### 4. Output Format

Local check results print before GitHub check results:

```
  local: compliant
  owner/repo: compliant
```

Or:

```
  local: NON-COMPLIANT (3 issues)
    local.vergil_toml: expected='present', actual='missing'
    local.claude_md: expected='template present', actual='not found'
    local.claude_settings.deny_rules: expected='...', actual='missing: ...'
  owner/repo: NON-COMPLIANT (1 issue)
    repo_settings.allow_auto_merge: expected=False, actual=True
```

### 5. Files Touched

| File | Action |
|------|--------|
| `src/vergil_tooling/data/claude_md_consumer.md` | Create — the template |
| `src/vergil_tooling/lib/repo_config.py` | Create — local audit checks |
| `src/vergil_tooling/bin/vrg_github_repo_config.py` | Create — new CLI entry point |
| `src/vergil_tooling/bin/vrg_github_config.py` | Delete |
| `tests/vergil_tooling/test_repo_config.py` | Create — lib tests |
| `tests/vergil_tooling/test_vrg_github_repo_config.py` | Create — CLI tests |
| `tests/vergil_tooling/test_vrg_github_config.py` | Delete |
| `pyproject.toml` | Edit — replace entry point, add package-data glob |
| `CLAUDE.md` | Edit — insert template block + vergil-tooling override |

`src/vergil_tooling/lib/github_config.py` is unchanged — its types
are imported by the new code.

### 6. Rollout

**Phase 1 (this issue):** Build the template, audit library, and
renamed CLI tool. Update vergil-tooling's own CLAUDE.md. Validate
with `vrg-container-run -- uv run vrg-validate`.

**Phase 2 (separate issues):** Sweep all consuming repos:
- Update CLAUDE.md to include the template block verbatim
- Update `.claude/settings.json` where needed
- Replace all references to `vrg-github-config` with
  `vrg-github-repo-config`
- Run `vrg-github-repo-config audit` to verify compliance

**Repos to sweep:**
- vergil-actions
- vergil-claude-plugin
- vergil-docker
- diogenes
- mq-rest-admin-python
- mq-rest-admin-go
- mq-rest-admin-java
- mq-rest-admin-rust
- mq-rest-admin-ruby
- mq-rest-admin-common
- mq-rest-admin-dev-environment
- the-infrastructure-mindset

## Known Drift (discovered during brainstorming)

Issues that the audit tool will catch on its first sweep:

| Repo | Issue |
|------|-------|
| diogenes | Validation command includes unnecessary `uv run` prefix |
| vergil-actions | Skill namespace uses `vergil-tooling:` instead of `vergil:` |
| vergil-claude-plugin | Skill namespace uses `vergil-tooling:` instead of `vergil:` |
| the-infrastructure-mindset | No CLAUDE.md, no vergil.toml, no .githooks — zero configuration |

## Future Work

- **`vrg-github-project-config`** — a separate tool for
  project-level auditing: project settings, plus running
  `vrg-github-repo-config audit` on each constituent repo.
  Tracked as a separate issue.
- **Repository init tool** — bootstraps missing local files
  (CLAUDE.md from template, default `.claude/settings.json`,
  `vergil.toml`, `.githooks/pre-commit`) when onboarding a new
  repo. Out of scope for this audit tool, which is read-only.

## Open Questions

None. Design questions resolved during brainstorming and pushback
review (2026-05-18).
