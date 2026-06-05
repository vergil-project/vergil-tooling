# vrg-submit-pr / vrg-finalize-pr interface upgrade — design

- **Issue:** vergil-project/vergil-tooling#1423
- **Date:** 2026-06-05
- **Status:** Approved design, pending implementation plan

## Context and goal

`vrg-submit-pr` and `vrg-finalize-pr` are the two highest-frequency
human commands in the 2.1 workflow. Both currently impose
location-sensitivity costs on the human:

- `vrg-submit-pr` must be run from inside the issue worktree. Reaching
  the worktree relies on shell tab completion, and with two or more
  worktrees pending, double-tab disambiguation can silently select the
  wrong one. This has caused wrong-PR submissions in practice (twice),
  requiring manual backout.
- `vrg-finalize-pr` (which, as of commit `711bb08e`, already performs
  the merge itself with a pre-merge provenance check) fails fast when
  CI checks are still running, forcing the human to poll manually
  before re-running it.

Target workflow after this change, all from the repo root:

1. `vrg-submit-pr` ↵ — tool finds the submittable worktree (or asks
   which), shows the preview, submits, emits the `/vergil:pr-watch`
   handoff lines.
2. Human pastes the handoff lines into the USER and AUDIT agent
   sessions.
3. `vrg-finalize-pr` ↵ — tool infers the PR, confirms, then waits for
   green and goes to completion (merge + cleanup) unattended, unless a
   check fails or an agent needs attention.

## Prior art already in the tree

This design reuses, rather than reinvents:

- Merge + provenance check: `bin/vrg_finalize_pr.py`
  (`_finalize_specific_pr()`), `lib/pr_provenance.py`.
- Wait-for-green: `github.wait_for_checks()`,
  `github.merge_state_status()`, `github.update_branch()`,
  `github.failed_check_names()` in `lib/github.py`; orchestrated copy
  in `lib/release/merge.py` (`wait_and_merge()`, hardcoded
  `--merge` strategy).
- Worktree-to-branch mapping: private `_worktree_for_branch()` in
  `bin/vrg_finalize_pr.py` (canonical `.worktrees/` constraint per
  issue #315).
- Interactive prompts: `prompt_choice()` / `prompt_yes_no()` in
  `lib/repo_init.py`.

## CLI behavior

### vrg-submit-pr (template mode, no arguments)

1. **Location resolution.**
   - CWD inside a `.worktrees/` worktree → behave exactly as today.
   - CWD at the repo root (main worktree) → scan `.worktrees/*/` for
     worktrees containing `.vergil/pr-template.yml` ("template-ready"
     candidates):
     - **One ready** → announce it (worktree name, issue number, title
       from the template), `os.chdir()` into it, continue with the
       existing preview + `Submit this PR? [y/N]` flow. No additional
       prompt: the existing confirmation already displays the issue
       and title.
     - **Multiple ready** → numbered menu, one line per candidate
       (worktree name, issue number, title). Selection → chdir →
       existing flow.
     - **None ready** → error listing existing worktrees and why each
       was skipped (no `.vergil/pr-template.yml` → not ready).
   - Anywhere else → error with guidance, as today.
2. `os.chdir()` affects only the tool's process; the invoking shell
   stays at the repo root.
3. CLI mode (`--issue` / `--summary` / `--title`) is unchanged and
   still requires running from inside the worktree.

### vrg-finalize-pr

Two entry modes:

- **Explicit PR** (`vrg-finalize-pr <pr-url-or-number>`): the URL is
  sitting in front of the human (printed by `vrg-submit-pr`), so
  passing it is itself the confirmation. No worktree inference, no
  prompt. Proceed directly to the merge path.
- **No arguments** (inference mode): scan `.worktrees/` worktrees and
  map each branch to its open PR (`gh pr list --head <branch>`,
  exposed as `github.pr_for_branch()`). Worktrees whose branch has no
  open PR are excluded. The tool **always confirms before acting** in
  this mode:
  - **One candidate** → `Finalize PR #N (issue #M: <title>)? [y/N]`.
  - **Multiple candidates** → numbered menu, then confirm the choice.
  - **None** → `No open PRs found in worktrees. Run cleanup only
    (switch to <target>, pull, prune branches/worktrees)? [y/N]` —
    cleanup-only is never entered silently.
  - Declining any prompt exits 0 without acting.

Merge path (both modes): provenance check (existing, runs first so
violations surface before any waiting) → wait-and-merge loop (below)
→ existing cleanup (checkout target, ff pull, delete merged branches
and their worktrees, prune remotes, post-finalization validation, CD
workflow check).

Existing flags (`--strategy`, `--target-branch`,
`--allow-provenance-violation`, `--dry-run`) are unchanged.
`--dry-run` skips waiting and prints what it would wait on. The
existing must-run-from-main-worktree guard stays.

### Wait-and-merge loop (BEHIND-first ordering)

A stale check run is detectable up front: if the branch is BEHIND the
target, the current CI run's outcome is irrelevant because
update-branch cancels it and starts a new one. Therefore the BEHIND
check runs **first**, on entry and after every wait — never after
letting a doomed run finish:

```text
attempts = 0
loop:
  if merge_state == BEHIND:
      if attempts == 3: abort (merge-train guard)
      update-branch; attempts += 1
      wait for the new check run to register
      continue
  wait for checks (progress feedback via `gh pr checks --watch`)
  if any check failed → abort, printing failed_check_names() + PR URL
  if merge_state == BEHIND:   # something merged while we waited
      continue                # → immediate update at loop top
  break
merge (caller-selected strategy)
```

## Architecture

### New: `lib/worktrees.py`

Single home for canonical-worktree logic:

- `Worktree` dataclass: `path`, `branch`.
- `list_worktrees(repo_root) -> list[Worktree]` — parses
  `git worktree list --porcelain`, filtered to the canonical
  `.worktrees/` container (preserving the issue #315 constraint that
  user-created worktrees elsewhere are ignored).
- `worktree_for_branch(branch, repo_root) -> Path | None` — relocated
  from `bin/vrg_finalize_pr.py:_worktree_for_branch()`; finalize-pr's
  cleanup imports it from here.
- `select_worktree(candidates, *, purpose) -> Worktree` — the
  one/many/none decision wrapper built on `repo_init.prompt_choice()`.

### New: `lib/pr_merge.py`

- `wait_and_merge(pr, *, strategy, verbose) -> None` — the
  BEHIND-first loop, assembled from existing `lib/github.py`
  primitives (`merge_state_status`, `wait_for_checks`,
  `update_branch`, `failed_check_names`, `merge`).
- `lib/release/merge.py` becomes a thin call into this with
  `strategy="merge"` — release keeps its public interface, loses its
  private copy of the loop, and inherits the BEHIND-first ordering.

### Modified: `bin/vrg_submit_pr.py`

Location-resolution preamble in template mode only: resolve worktree →
`os.chdir()` → existing flow untouched. The candidate scan reads
`issue`/`title` from each worktree's `.vergil/pr-template.yml` via the
existing `pr_template.read_template()`.

### Modified: `bin/vrg_finalize_pr.py`

- PR inference via new helper `github.pr_for_branch(branch) ->
  dict | None`.
- Confirmation prompts per the behavior section.
- `_finalize_specific_pr()` swaps its direct `github.merge()` call for
  `pr_merge.wait_and_merge()`.
- Cleanup phase unchanged.

### Explicitly not changing

No new console scripts; no changes to `vrg-pr-await` (keeps its
standalone role), the pr-template schema, or the pr-watch handoff. A
`vrg-pr <subcommand>` umbrella redesign was considered and dismissed:
it is a breaking rename of the two highest-frequency commands for zero
behavioral gain.

## Error handling and edge cases

- **Worktree without a template (submit-pr scan):** not a candidate;
  named in the zero-candidates error with the reason.
- **Worktree whose branch has no open PR (finalize inference):**
  excluded from candidates. A worktree whose PR was already merged but
  not yet cleaned up lands in the confirmed cleanup-only path —
  cleanup is exactly what it needs.
- **Multiple open PRs for one branch:** GitHub permits one open PR per
  head/base pair, so `pr_for_branch()` taking the first result is
  safe.
- **Check failure during wait:** abort with `failed_check_names()`
  output and the PR URL; no merge, no cleanup; nonzero exit.
  Finalize is idempotent up to the merge — re-running after the agents
  fix CI is the recovery path.
- **Ctrl-C during wait:** nothing destructive has happened
  (update-branch excepted, which is harmless); re-run resumes cleanly.
- **update-branch failure (e.g., conflict with target):** abort with
  the API error and instructions to resolve in the worktree; conflicts
  need a human or agent, not a retry loop.
- **Merge-train guard:** at most 3 update-branch attempts per
  invocation; abort with status on exhaustion.
- **Provenance violations:** checked before any waiting, so they
  surface immediately rather than after minutes of green CI.
- **No silent failures:** every skip and exclusion prints its reason.

## Testing

Existing layout (`tests/` mirroring `bin/` and `lib/`), subprocess
boundaries mocked:

- **`lib/worktrees.py`:** canned `git worktree list --porcelain`
  output — discovery, canonical-container filtering (non-canonical
  worktree ignored), branch mapping, selection one/many/none with
  mocked `prompt_choice`.
- **`lib/pr_merge.py`:** loop state table — green-first-try;
  BEHIND-on-entry → update → green; BEHIND-after-wait → update →
  green; check failure → abort; update-branch failure → abort;
  attempt-cap exhaustion. All `github.*` calls mocked.
- **`bin/vrg_submit_pr.py`:** location matrix — in-worktree
  (unchanged), root + one ready, root + multiple ready (menu),
  root + none ready (error names skip reasons), CLI mode untouched.
- **`bin/vrg_finalize_pr.py`:** inference matrix — explicit arg (no
  prompt), one candidate (confirm), multiple (menu + confirm), none
  (cleanup confirm), decline → exit 0 without action.
- **Release regression:** existing release-module merge tests pass
  against the relocated helper.
- Full validation: `vrg-container-run -- uv run vrg-validate`.

## Deferred / out of scope

- **Progress-framework integration:** the wait loop is a natural fit
  for the new progress framework (#1419); retrofit once this feature
  and the framework are both stable.
- **Issue closing:** stays a manual human action per issue #1423's
  out-of-scope note.
- **Submit-pr worktree argument:** an optional argument naming the
  worktree was considered and deferred (YAGNI) — the no-arg flow plus
  the menu covers the observed usage.
