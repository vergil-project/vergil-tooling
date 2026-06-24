# vrg-worktree-status: LAST COMMIT and LAST MODIFIED age columns

- **Issue:** vergil-project/vergil-tooling#1856
- **Status:** Design (approved for planning)
- **Date:** 2026-06-24

## Goal

Give `vrg-worktree-status` two new columns answering "how old / how
active is this worktree," both rendered as relative age (`3d ago`,
`2h ago`):

- **LAST COMMIT** — committer date of the branch tip.
- **LAST MODIFIED** — newest filesystem mtime across the worktree's
  files. This catches uncommitted edits that have no commit, so a branch
  with an old last-commit but recent edits reads as *live*, not cruft.

The two timestamps together make staleness legible: an old LAST COMMIT
with a recent LAST MODIFIED means active uncommitted work; both old means
the worktree is genuinely stale.

## Background

`vrg-worktree-status` (`bin/vrg_worktree_status.py`) prints a table —
`WORKTREE  BRANCH  PR  STATE  WORKFLOW  AHEAD  DIRTY` — one row per
canonical `.worktrees/` worktree, built from `WorktreeStatus`
(`lib/worktrees.py`). It is read-only observability: it distinguishes
live work from removable cruft via PR/lifecycle state, but conveys no
sense of *age*. These columns add that dimension.

`WorktreeStatus` is also consumed by the `vrg-finalize-pr` straggler
sweep, which calls `classify_worktree` directly. The new fields must not
disturb that path.

A relative-age formatter already exists at `vrg_vm._format_age`
(`bin/vrg_vm.py`): `<1 day → "Nh"`, else `"Nd"`. This design mirrors its
logic (with an `" ago"` suffix) rather than reinventing it.

## Design

The work splits along the file's existing data/presentation seam.

### Data layer — `lib/worktrees.py`

Add two epoch-seconds fields to `WorktreeStatus`, both `float | None`,
**defaulted to `None`** so `classify_worktree` callers (the finalize
sweep) are unaffected — exactly the pattern the existing
`workflow_status` / `pr_prepared` fields already follow:

```python
last_commit_ts: float | None = None
last_modified_ts: float | None = None
```

`gather_worktree_status` populates both, but only when called with
`with_freshness=True` — the display-only concern of `vrg-worktree-status`.
The `vrg-finalize-pr` straggler sweep shares this function and leaves the
default `False`, so it neither computes nor pays for the timestamps:

- **`last_commit_ts`** — a new `git.committer_timestamp(path)` helper
  running `git -C <path> log -1 --format=%ct HEAD` and parsing the epoch
  integer. Reads `HEAD` (a canonical worktree always has its branch
  checked out, so `HEAD` is the branch tip); a git failure propagates
  rather than being collapsed to `None`, consistent with the
  no-silent-failures rule.

- **`last_modified_ts`** — a new private `_newest_mtime(path)` that takes
  the maximum `st_mtime` over the union of:
  - `git -C <path> ls-files` (tracked files), and
  - `git -C <path> ls-files --others --exclude-standard`
    (untracked-but-not-ignored files).

  Using `ls-files` means the walk **respects `.gitignore`**, so `.venv`,
  `node_modules`, and build artifacts are skipped — both correct (they
  are not "your work") and fast. Returns `None` when the set is empty.

### Presentation layer — `bin/vrg_worktree_status.py`

- Compute `now` **once** in `main()` via
  `datetime.datetime.now(tz=datetime.UTC).timestamp()` (the established
  pattern from `vrg_vm`), and thread it into row rendering.
- Add a module-level `_format_age(ts: float | None, now: float) -> str`
  mirroring `vrg_vm._format_age`, but appending `" ago"` and returning
  `"-"` for `None`:
  - `None → "-"`
  - `< 1 day → "{int(hours)}h ago"`
  - `>= 1 day → "{int(days)}d ago"`
- **Append** the two columns at the end of `_COLUMNS` and `_row`:

  ```text
  WORKTREE  BRANCH  PR  STATE  WORKFLOW  AHEAD  DIRTY  LAST COMMIT  LAST MODIFIED
  ```

  Placement rationale: the existing columns are all PR-lifecycle signals
  that read as a group; the timestamps are a separate freshness concern,
  so they go at the end rather than splitting the lifecycle group.
  Appending also minimizes the textual merge surface with the in-flight
  `issue-1855-extip-column` work, which also edits `_COLUMNS` / `_row`.

The `_render_table` width logic is column-count-agnostic and needs no
change.

### Example output

```text
WORKTREE          BRANCH                          PR    STATE    ...  LAST COMMIT  LAST MODIFIED
issue-1840-foo    feature/1840-foo                #200  open-pr  ...  3d ago       2h ago
issue-1799-bar    feature/1799-bar                -     no-pr    ...  21d ago      21d ago
issue-1856-baz    feature/1856-...                -     draft    ...  1h ago       1h ago
```

## Error handling (no silent failures)

- A per-file `FileNotFoundError` during the mtime walk (a file vanishing
  mid-walk — a benign race) is **skipped**, not treated as an error.
- A genuine `git ls-files` / `git log` failure is left to **raise**
  (fail loud), consistent with the repo's no-silent-failures rule. In
  practice these cannot fail here without the earlier `dirty` / `ahead`
  git calls in the same function having already failed.
- "No data" — an unborn branch or an empty worktree — renders `"-"`,
  which is visually distinct from an error (errors raise).

No new error/`detail` field is introduced; the existing `detail` and
`workflow_error` note channels are untouched.

## Edge cases

- **Checkout sets mtimes.** `git checkout` writes working-tree files at
  worktree creation, so a never-touched worktree's LAST MODIFIED starts
  at its creation time and ages naturally from there. Accepted — fine for
  the hygiene use case.
- **Commit can be newer than modified.** Committing does not rewrite
  working-tree files, so `last_commit_ts` (committer "now") can be
  slightly newer than `last_modified_ts`. Both are shown independently;
  the meaningful "old commit + recent edit" signal is unaffected.

## Cost

Two extra local git calls plus one stat-walk per worktree. Negligible
next to the existing per-worktree GitHub API call, which already
dominates the command's runtime.

## Testing

- **`_format_age`** (`test_vrg_worktree_status.py`): the `<1d → "Nh ago"`
  / `>=1d → "Nd ago"` boundary, and `None → "-"`.
- **`_newest_mtime`** (`test_worktrees.py`): a temp worktree containing a
  tracked file, an untracked-non-ignored file, and a gitignored file —
  assert the ignored file is excluded and the newest eligible file's
  mtime wins; the empty case returns `None`.
- **Row rendering** (`test_vrg_worktree_status.py`): the two new cells
  appear in the correct trailing positions for both populated and
  `None` timestamps.

## Out of scope (YAGNI)

- Sorting by age or any `--sort` flag.
- Configurable / absolute date formats.
- Refactoring the duplicate `_format_age` in `vrg_vm.py` into a shared
  lib (noted as minor existing duplication; an unrelated tool, left
  untouched).

## Coordination note

`issue-1855-extip-column` is concurrently adding a column to the same
`_COLUMNS` / `_row` definitions. Whichever branch lands second will hit a
trivial textual merge conflict there — flagged, not a blocker.
