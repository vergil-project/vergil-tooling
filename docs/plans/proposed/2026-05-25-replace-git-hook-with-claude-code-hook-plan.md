# Implementation Plan: Replace Git Hook with Claude Code Hook

**Date:** 2026-05-25
**Spec:** `docs/specs/2026-05-25-replace-git-hook-with-claude-code-hook-design.md`
**Issue:** #1135, #724
**Scope:** vergil-tooling only (step 1 of the rollout)

## Overview

This plan covers the vergil-tooling changes only. Plugin and
consuming-repo changes are separate follow-up work after this ships
and is installable.

## Steps

### Step 1: Add `vrg-hook-guard` console script

Create `src/vergil_tooling/bin/vrg_hook_guard.py`.

**Behavior:**

1. Read JSON from stdin.
2. Extract `cwd` from `tool_input.cwd` or top-level `cwd`.
3. Walk `cwd` upward looking for `vergil.toml`. If not found, exit 0.
4. Extract `tool_input.command`.
5. Check for raw `git` or `gh` using a regex that:
   - Matches `git` or `gh` as a standalone command word
   - Excludes `vrg-git` and `vrg-gh` (negative lookbehind or
     two-step check)
   - Catches: direct invocation, chained (`&& ; ||`), env-var
     prefixed, `env`/`command` wrappers, subshells (`bash -c`,
     `sh -c`), parenthesized, backtick/`$()` forms
6. If `git` matched: output deny JSON directing to `vrg-git`.
7. If `gh` matched: output deny JSON directing to `vrg-gh`.
8. Otherwise: exit 0.

**Entry point:** `main()` reads stdin, runs the check, writes stdout.

**Files:**
- Create: `src/vergil_tooling/bin/vrg_hook_guard.py`
- Create: `tests/vergil_tooling/test_vrg_hook_guard.py`

**Tests (red/green):**

Write tests first covering:
- Unmanaged repo (no `vergil.toml`) → exit 0, no output
- Direct `git commit -m "foo"` → deny
- Direct `gh pr create` → deny
- `vrg-git commit` → allow (must not match)
- `vrg-gh pr create` → allow (must not match)
- `cd /path && git push` → deny
- `VAR=1 git commit` → deny
- `env git commit` → deny
- `bash -c "git commit"` → deny
- `(git reset --hard)` → deny
- `$(gh api ...)` → deny
- `ls -la` → allow
- `python script.py` → allow
- Empty command → allow
- Missing `tool_input` → exit 0 gracefully

### Step 2: Register entry point in `pyproject.toml`

Add to `[project.scripts]`:

```
vrg-hook-guard = "vergil_tooling.bin.vrg_hook_guard:main"
```

**Files:**
- Edit: `pyproject.toml` (line ~25, alphabetical order)

### Step 3: Add shell shim and hook wiring

Create `.claude/hooks/guard.sh` — the thin shim that checks for
`vrg-hook-guard` on PATH, execs it if found, falls back to
jq-based git/gh detection if not.

Update `.claude/settings.json` to:
- Remove all deny rules (`Bash(git *)`, etc.)
- Add hooks section referencing the shim

**Files:**
- Create: `.claude/hooks/guard.sh` (make executable)
- Edit: `.claude/settings.json`

**Resulting `.claude/settings.json`:**

```json
{
  "permissions": {
    "allow": [
      "Bash(vrg-*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/guard.sh"
          }
        ]
      }
    ]
  },
  "extraKnownMarketplaces": {
    "vergil-marketplace": {
      "source": {
        "source": "github",
        "repo": "vergil-project/vergil-claude-plugin"
      }
    }
  },
  "enabledPlugins": {
    "vergil@vergil-marketplace": true
  }
}
```

### Step 4: Remove `VRG_COMMIT_CONTEXT` from `git.py`

Remove the env-var gate from `src/vergil_tooling/lib/git.py`:

- Delete `_GATE_ENV_VAR` and `_GATE_ENABLED_VALUE` constants
  (lines 14-15).
- Remove the `if args and args[0] == "commit"` branch and the
  `env` variable setup (lines 27-29). The `run()` function
  should pass `env=None` unconditionally for the subprocess call.
- Remove the comment block explaining the contract (lines 10-13).
- Update any tests that assert on `VRG_COMMIT_CONTEXT` behavior.

**Files:**
- Edit: `src/vergil_tooling/lib/git.py`
- Edit: tests if any reference the gate

### Step 5: Remove `_ALLOWED_EXACT` for `core.hooksPath`

In `src/vergil_tooling/bin/vrg_git.py`, remove the exact-match
allowance for `git config core.hooksPath .githooks` (lines 39-41)
since there are no git hooks to configure.

Also remove the check at lines 162-165 that tests against
`_ALLOWED_EXACT`, and the `_ALLOWED_EXACT` constant itself.

**Files:**
- Edit: `src/vergil_tooling/bin/vrg_git.py`
- Update tests if any reference the exact-match path

### Step 6: Add worktree-convention check to `vrg-git` (#724)

Add a check to `vrg-git` for `checkout` and `switch` subcommands.
This goes in `main()` after the denied-subcommand check but before
the subprocess call, only for `checkout` and `switch`.

**Logic:**

1. Detect if the current directory is the main worktree: check that
   `.git` is a directory (not a file — worktrees have a `.git` file
   pointing to the main repo's `.git/worktrees/<name>`).
2. If in the main worktree and `.worktrees/` directory exists
   (worktree convention is active):
   - Parse the target branch from args. For `checkout`, it's the
     first non-flag arg after any `--`. For `switch`, it's the
     first non-flag arg or the arg after `-c`/`-C`.
   - If the target is not the default branch (`develop` or `main`),
     reject with: "Branch switches in the main worktree are blocked.
     Use a worktree under .worktrees/ instead."
   - If no target branch is found (e.g., `checkout -- file.txt`),
     allow — this is a file checkout, not a branch switch.
3. If not in the main worktree, or `.worktrees/` doesn't exist:
   allow unconditionally.

**Files:**
- Edit: `src/vergil_tooling/bin/vrg_git.py`
- Create or extend: `tests/vergil_tooling/test_vrg_git.py`

**Tests:**
- Main worktree + `.worktrees/` exists + checkout feature branch → deny
- Main worktree + `.worktrees/` exists + checkout develop → allow
- Main worktree + `.worktrees/` exists + checkout -- file.txt → allow
- Inside `.worktrees/` + checkout feature branch → allow
- Main worktree + no `.worktrees/` dir → allow (convention not active)
- Same checks for `switch` subcommand

### Step 7: Delete `.githooks/pre-commit`

Remove the git hook file.

**Files:**
- Delete: `.githooks/pre-commit`
- Delete: `.githooks/` directory if empty after removal

### Step 8: Update documentation

Update all references to the removed hook, deny rules, and
`VRG_COMMIT_CONTEXT`:

**CLAUDE.md** — remove or rewrite:
- Line 147: `git config core.hooksPath .githooks` from setup
  instructions
- Lines 161-162: reference to `.githooks` pre-commit gate in
  Tier 1 description
- Lines 238-245: entire "Git Hooks (`.githooks/`)" section
- Lines 264-269: `git config core.hooksPath .githooks` in
  consumption model
- Line ~130: "Shell command policy" section — remove the note that
  "Raw `git` and `gh` are denied by the permission model" and
  replace with a note that they are blocked by the
  `vrg-hook-guard` Claude Code hook

**Other docs:**
- `docs/specs/host-level-tool.md` — remove git hook section
- `README.md` — remove any hook setup references
- Any other files referencing `.githooks`, `VRG_COMMIT_CONTEXT`, or
  `core.hooksPath`

**Files:**
- Edit: `CLAUDE.md`
- Edit: `docs/specs/host-level-tool.md`
- Edit: `README.md` (if applicable)
- Grep-and-fix any remaining references

### Step 9: Run validation

```bash
vrg-container-run -- uv run vrg-validate
```

Fix any lint, typecheck, or test failures.

### Step 10: Commit and submit PR

Commit with a message covering both issues (#1135 and #724).
Submit PR via `vrg-submit-pr`.

## Dependency graph

```text
Step 1 (vrg-hook-guard)
  ↓
Step 2 (pyproject.toml entry point)
  ↓
Step 3 (shim + settings.json wiring)
  ↓
Step 4 (remove VRG_COMMIT_CONTEXT) ←─ independent of 3
  ↓
Step 5 (remove _ALLOWED_EXACT) ←─ independent of 4
  ↓
Step 6 (worktree check in vrg-git) ←─ independent of 4, 5
  ↓
Step 7 (delete .githooks/pre-commit) ←─ after 4 (no consumers left)
  ↓
Step 8 (update docs) ←─ after all code changes
  ↓
Step 9 (validate)
  ↓
Step 10 (commit + PR)
```

Steps 4, 5, and 6 are independent of each other and can be done in
any order after step 3. Step 7 depends on step 4 (removing the last
consumer of the gate). Step 8 should be last before validation so
all code changes are settled.
