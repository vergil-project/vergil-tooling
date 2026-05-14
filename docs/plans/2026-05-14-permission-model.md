# Claude Code Permission Model — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate from `bypassPermissions` mode to a defense-in-depth
permission model where agents operate through VRG wrapper tools with
subcommand allowlists, Claude Code's permission layer controls what
reaches the shell, and existing plugin hooks serve as a backstop.

**Architecture:** Seven-step rollout — build wrappers, update hooks,
update CLAUDE.md guidance, deploy project settings to consuming repos,
deploy vergil-tooling's own permission config, switch the global
default mode, then observe and refine.

**Tech Stack:** Python CLI tools (click), Claude Code settings (JSON),
vergil plugin hooks (JSON + bash), CLAUDE.md (markdown)

**Spec:** `docs/specs/2026-05-14-permission-model-design.md`

---

## Phase 1: Build the Wrappers

The wrappers are the foundation — everything else depends on agents
having `vrg-git` and `vrg-gh` available before the deny rules go live.

### Task 1: `vrg-git` Wrapper

**Files:**
`src/vergil_tooling/cli/vrg_git.py`,
`tests/test_vrg_git.py`,
`pyproject.toml` (console script entry)

Build the `vrg-git` CLI tool per the spec's Section 2.

- [ ] Create `src/vergil_tooling/cli/vrg_git.py` — Python CLI that
      accepts arbitrary arguments after `vrg-git`, validates the first
      argument (subcommand) against the allowlist, checks remaining
      arguments against the per-subcommand denied-flags list, then
      executes `git` via `subprocess.run` with `shell=False`
- [ ] Implement the subcommand allowlist from the spec: `status`,
      `log`, `diff`, `show`, `branch`, `ls-remote`, `rev-parse`,
      `worktree add`, `worktree list`, `worktree remove`, `add`,
      `push`, `fetch`, `pull`, `checkout`, `switch`, `stash`, `merge`,
      `cherry-pick`, `rebase`
- [ ] Implement flag deny lists per-subcommand:
      - `branch`: deny `-D`, `--force`
      - `push`: deny `--force`, `--force-with-lease`, `-f`
      - `checkout`: deny `-- .`, `-- *` (broad restore patterns;
        specific-file `-- path/to/file` is allowed)
      - `rebase`: deny `-i`, `--interactive`
- [ ] Implement the explicit deny list: `commit`, `reset`, `clean`,
      `config`, `remote`, `reflog`, `gc`, `prune`, `filter-branch`,
      `replace` — with clear error messages naming the denied
      subcommand and suggesting the VRG alternative where one exists
      (e.g., "use vrg-commit instead of git commit")
- [ ] Handle compound subcommands: `worktree add`, `worktree list`,
      `worktree remove` — validate the first two arguments as a pair
- [ ] Reject unrecognized subcommands with a clear error message
- [ ] Add invocation logging: command, arguments, allowed/denied,
      timestamp — append to a local log file
      (`~/.local/share/vergil/vrg-git.log` or similar)
- [ ] Add `vrg-git` console script entry point in `pyproject.toml`
- [ ] Write tests:
      - Each allowed subcommand passes validation
      - Each denied subcommand is rejected with appropriate message
      - Each denied flag on an allowed subcommand is rejected
      - Unrecognized subcommand is rejected
      - Arguments are passed through to `git` without shell expansion
      - Logging produces entries on both allow and deny

### Task 2: `vrg-gh` Wrapper

**Files:**
`src/vergil_tooling/cli/vrg_gh.py`,
`tests/test_vrg_gh.py`,
`pyproject.toml` (console script entry)

Build the `vrg-gh` CLI tool per the spec's Section 3.

- [ ] Create `src/vergil_tooling/cli/vrg_gh.py` — same architecture
      as `vrg-git`: validate the two-level subcommand pair (e.g.,
      `issue view`, `pr checks`), then execute `gh` via
      `subprocess.run` with `shell=False`
- [ ] Implement the subcommand allowlist from the spec:
      - `issue`: `view`, `create`, `close`, `edit`, `list`, `comment`
      - `pr`: `view`, `checks`, `list`, `diff`, `comment`, `edit`
      - `run`: `list`, `view`, `watch`
      - `repo`: `view`
      - `label`: `list`, `create`
- [ ] Implement the explicit deny list:
      - `pr merge`, `pr review --approve`, `pr close`, `pr create`
      - `repo edit`, `repo create`, `repo delete`
      - `api` (entire subcommand tree)
      - `auth` (entire subcommand tree)
- [ ] For `pr review`: allow the subcommand but deny `--approve`
      flag specifically (agents can comment on PRs but not approve)
- [ ] Reject unrecognized top-level subcommands
- [ ] Reject unrecognized second-level subcommands under a known
      top-level
- [ ] Add invocation logging (same format as `vrg-git`)
- [ ] Add `vrg-gh` console script entry point in `pyproject.toml`
- [ ] Write tests:
      - Each allowed subcommand pair passes validation
      - Each denied subcommand pair is rejected
      - `api` and `auth` are rejected at the top level
      - `pr review` without `--approve` is allowed
      - `pr review --approve` is rejected
      - Arguments are passed through without shell expansion
      - Logging produces entries on both allow and deny

### Task 3: Shared Infrastructure

**Files:**
`src/vergil_tooling/lib/wrapper.py` (new, if warranted)

Evaluate during Task 1/2 implementation whether shared code between
the two wrappers warrants extraction. Both wrappers share: argument
parsing pattern (allowlist lookup + flag deny), logging format,
error message format, `subprocess.run` invocation.

- [ ] If the two implementations share substantial code, extract
      shared logic into `src/vergil_tooling/lib/wrapper.py`
- [ ] If not, skip — prefer duplication over premature abstraction

### Task 4: Verify `Bash(git *)` Pattern Matching

**Files:** None (manual testing)

The spec identifies a critical implementation risk: the `Bash(git *)`
deny pattern must not match `vrg-git *` via substring matching.

- [ ] Before any permission config is deployed, test Claude Code's
      matching behavior:
      1. Set `deny: ["Bash(git *)"]` in a test settings file
      2. Attempt `vrg-git status` — must NOT be denied
      3. Attempt `git status` — must be denied
      4. Attempt `echo "git status"` — document behavior
      5. Attempt `vrg-git status && git push` — document behavior
         (compound command splitting per anthropics/claude-code#28784)
- [ ] Document results in the spec or as a comment on issue #754
- [ ] If `vrg-git` is matched by `Bash(git *)`: do not deploy the
      global deny rules. Instead, move the git/gh deny enforcement
      entirely to the hook layer and document the limitation. The
      rest of the plan still proceeds — only the enforcement layer
      for the deny changes.

### Task 5: Validation and Release

**Files:** `pyproject.toml` (version bump if needed)

- [ ] Run `vrg-docker-run -- uv run vrg-validate` — full pipeline
      must pass with the new wrappers and tests
- [ ] Verify `uv tool install` installs both `vrg-git` and `vrg-gh`
      as available console scripts
- [ ] Commit and PR for the wrapper code (Tasks 1-3)

---

## Phase 2: Update Supporting Infrastructure

With the wrappers available, update the guidance and hook layers to
point agents toward them.

### Task 6: Update Vergil Plugin Hook Messages

**Files:** vergil-claude-plugin hooks (separate repo)

Existing hooks that block raw `git` and `gh` usage should update
their error messages to point agents to the wrappers.

- [ ] `block-raw-git-commit`: update message to reference `vrg-commit`
      (already does) and note that all `git` operations should use
      `vrg-git`
- [ ] `block-raw-gh-pr-create`: update message to reference
      `vrg-submit-pr` (already does) and note that all `gh` operations
      should use `vrg-gh`
- [ ] `block-agent-merge`: update message to note that `vrg-gh`
      rejects `pr merge` as well
- [ ] `block-github-contents-api`: update message to note that
      `vrg-gh` denies `gh api` entirely
- [ ] No hooks are removed — they remain as Layer 3 backstops
- [ ] Commit and PR for hook message updates

### Task 7: Update CLAUDE.md Across All Repos

**Files:** `CLAUDE.md` in vergil-tooling, vergil-actions,
vergil-docker, vergil-claude-plugin

Add a section to each repo's CLAUDE.md instructing agents to use
the wrappers.

- [ ] Add a "Shell command policy" section (or equivalent) to each
      CLAUDE.md:
      - Use `vrg-git` instead of `git` for all git operations
      - Use `vrg-gh` instead of `gh` for all GitHub CLI operations
      - These are not optional preferences — raw `git` and `gh` are
        denied by the permission model
      - If a command is not available through the wrappers, explain
        the situation to the human who can run it via `! <command>`
- [ ] Verify the wording is consistent across all four repos
- [ ] One PR per repo (can run in parallel)

---

## Phase 3: Deploy Permission Configuration

Settings files are deployed to repos and the global mode is switched.
This is the "flip the switch" phase — everything before this was
preparation.

### Task 8: Deploy Project Settings to Consuming Repos

**Files:** `.claude/settings.json` in vergil-actions, vergil-docker,
vergil-claude-plugin

Each consuming repo gets the project-level `Bash(vrg-*)` allowlist.

- [ ] Update `.claude/settings.json` in vergil-actions to add:
      ```json
      {
        "permissions": {
          "allow": ["Bash(vrg-*)"]
        }
      }
      ```
      (Merged with existing plugin marketplace config)
- [ ] Same for vergil-docker
- [ ] Same for vergil-claude-plugin
- [ ] Document the `.claude/settings.local.json` template for phase 1
      read-only exceptions in CONTRIBUTING.md or developer docs — this
      is gitignored and per-developer, so it cannot be committed
- [ ] One PR per repo (can run in parallel)

### Task 9: Deploy Permission Configuration for vergil-tooling

**Files:**
`.claude/settings.json` (project-level),
`~/.claude/settings.json` (global, manual),
`~/.claude/settings.local.json` (global local, manual)

Deploy the full permission stack for vergil-tooling itself.

- [ ] Update `.claude/settings.json` (project-level) to add the
      `Bash(vrg-*)` allowlist (merged with existing plugin config)
- [ ] Prepare but do not yet apply the global settings changes:
      ```json
      {
        "permissions": {
          "defaultMode": "acceptEdits",
          "deny": ["Bash(git *)", "Bash(gh *)"]
        }
      }
      ```
      These are applied in Task 10, not here — the project-level
      changes can be committed and merged first.
- [ ] Prepare the `.claude/settings.local.json` template with the
      phase 1 read-only exceptions (from the spec's Section 4)
- [ ] Commit and PR for the project-level settings change

### Task 10: Switch Global Default Mode

**Files:** `~/.claude/settings.json` (manual, not committed anywhere)

This is the migration moment. All wrappers, hooks, CLAUDE.md updates,
and project settings must be deployed and merged before this step.

- [ ] Confirm prerequisites:
      - `vrg-git` and `vrg-gh` are installed and working
      - All four repos have updated CLAUDE.md
      - All four repos have project-level `Bash(vrg-*)` allowlist
      - Task 4 (pattern matching verification) is complete and results
        are documented
- [ ] Apply global `~/.claude/settings.json`:
      ```json
      {
        "permissions": {
          "defaultMode": "acceptEdits",
          "deny": ["Bash(git *)", "Bash(gh *)"]
        }
      }
      ```
      (If Task 4 showed substring matching, omit the deny rules and
      rely on hook enforcement instead)
- [ ] Apply `~/.claude/settings.local.json` with the phase 1
      read-only exceptions
- [ ] Remove `skipDangerousModePermissionPrompt` from global settings
      (no longer needed when not in bypass mode)

---

## Phase 4: Observe and Refine

### Task 11: One-Week Observation Period

**Files:** None (operational)

Run the new permission model for one week of normal development
across all four repos. Collect data on prompt frequency, wrapper
usage, and friction points.

- [ ] Monitor: which commands trigger prompts most frequently?
      Candidates for new VRG tools or allowlist additions.
- [ ] Monitor: which wrapper subcommands are agents using most?
      Validates the allowlist design.
- [ ] Monitor: are agents finding workarounds or getting stuck?
      Indicates missing allowlist entries or overly restrictive
      wrappers.
- [ ] Review `vrg-git` and `vrg-gh` logs for patterns
- [ ] After one week: update issue #754 with findings and open
      follow-up issues for any phase 2 work identified
- [ ] If the prompt volume is unacceptably high, adjust the
      `.claude/settings.local.json` read-only exceptions. If specific
      `vrg-git` or `vrg-gh` subcommands are missing, add them to the
      wrappers.

---

## Implementation Notes

### Task Dependencies

```text
Task 1 (vrg-git) ──┐
Task 2 (vrg-gh) ───┤
Task 3 (shared) ───┼── Task 4 (pattern verify) ── Task 5 (validate/PR)
                   │
                   ├── Task 6 (hook messages)  ─┐
                   ├── Task 7 (CLAUDE.md × 4) ──┤
                   │                            ├── Task 10 (switch mode)
                   ├── Task 8 (consuming repos) ┤
                   └── Task 9 (vergil-tooling) ─┘
                                                │
                                           Task 11 (observe)
```

Tasks 1-3 can proceed in parallel. Task 4 can begin as soon as
Task 1 produces a working `vrg-git`. Tasks 6, 7, 8, 9 can proceed
in parallel after Task 5 merges. Task 10 is the gating step — all
prerequisites must be merged. Task 11 follows Task 10.

### Scope Boundaries

- **In scope:** wrapper tools, permission config, CLAUDE.md updates,
  hook message updates, pattern matching verification.
- **Out of scope:** credential management (covered by #717, #764,
  #765), unified `vrg` CLI (phase 2/3), `vrg-search` or other
  read-only tool replacements (phase 2), Windows/Linux credential
  backends (#764, #765).

### Risk Mitigations

- **Task 4 is a gate.** If `Bash(git *)` matches `vrg-git`, the
  deny rules cannot go in Claude Code settings. The plan has an
  explicit fallback: hook-only enforcement for the deny layer.
- **Phase 1 read-only exceptions are temporary.** They live in
  gitignored local settings so they can be tightened per-developer
  without requiring a PR.
- **The switch (Task 10) is reversible.** If the prompt volume is
  unmanageable, reverting `~/.claude/settings.json` to
  `bypassPermissions` restores the prior behavior. The wrappers and
  hooks remain as improvements regardless.
