# vrg-finalize-pr Progress-Framework Adoption Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give standalone `vrg-finalize-pr` runs the stage-aware live progress display while the nested `vrg-release` invocation explicitly opts into plain rendering.

**Architecture:** Split `main()` into a pre-pipeline phase (arg parsing, main-worktree guard, interactive PR inference — needs the real TTY) and five stage functions executed via `progress.run_pipeline`. Stage failure is signaled by raising `FinalizeAbort`; `fail_defer` on validation and cd-check preserves the current "validation fails but cd-check still runs, exit 1" semantics. `vrg-release` adds `--output-format plain` to its `--cleanup-only` invocation.

**Tech Stack:** Python 3.12+, `vergil_tooling.lib.progress` (Stage / run_pipeline / run / add_progress_args), pytest with `unittest.mock.patch`.

**Spec:** `docs/specs/2026-06-06-finalize-pr-progress-adoption-design.md` (issue #1479)

**Working context:** All file paths are relative to the worktree
`.worktrees/issue-1479-finalize-progress/` on branch
`feature/1479-finalize-progress`. Run every command from inside the
worktree (`vrg-container-run` mounts the current directory). Use
`vrg-git` / `vrg-commit`, never raw `git`.

**Test command (inner loop):**
`vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -q`
**Full validation (per-task gate):**
`vrg-container-run -- uv run vrg-validate`

---

## Background for the implementer

- `src/vergil_tooling/lib/progress.py` is the framework. Key API:
  - `Stage(name, fn, mode)` — `fn(ctx)` raises to fail; `mode` is
    `"fail_fast"` (stop pipeline) or `"fail_defer"` (record failure,
    keep going, exit 1 at the end).
  - `run_pipeline(ctx, stages, *, command, label, args, repo_root)` —
    returns 0/1/130. Redirects `sys.stdout`/`sys.stderr` during stage
    execution, so existing `print()` calls (including
    `file=sys.stderr`) inside stage code automatically reach the
    renderer and the run log. **Consequence:** messages that used to
    appear on stderr now reach stdout via the renderer — several test
    assertions move from `.err` to `.out`.
  - `run(cmd, *, stdin=...)` — stream a child through the active
    session. Raises `CalledProcessError` on nonzero exit.
  - `add_progress_args(parser, stages)` — adds `--output-format` /
    `--output-window`; pass `()` for stages (we define no skip flags).
  - `run_pipeline` creates a `RunLog` at `repo_root / ".vergil"` —
    tests that patch `git.repo_root` to the non-writable `Path("/repo")`
    must switch to `tmp_path`.
- `src/vergil_tooling/bin/vrg_finalize_pr.py` is currently a monolithic
  `main()` (~190 lines) plus helpers. Helpers stay as-is:
  `_run`, `_worktree_is_dirty`, `_delete_branch_and_worktree`,
  `_infer_pr`, `_check_cd_workflow_status`, `_ETERNAL_BY_MODEL`.
- Mock-patching note used throughout the tests: `patch(_MOD + ".git.run")`
  patches the attribute on the shared `lib.git` module object, so it is
  global for the test's duration. The same applies to
  `patch(_MOD + ".progress.run")`.

---

### Task 1: FinalizeContext, FinalizeAbort, provenance + merge stages

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py`
- Test: `tests/vergil_tooling/test_vrg_finalize_pr.py`

New stage functions live alongside the existing `_finalize_specific_pr`
(deleted in Task 3). Nothing is rewired yet — `main()` is untouched and
every existing test keeps passing.

- [ ] **Step 1: Write the failing tests**

Append to `tests/vergil_tooling/test_vrg_finalize_pr.py` (after the
existing `# -- engine swap + explicit-target cleanup (issue #1423)`
section, which defines `_CLEAN_PROVENANCE`):

```python
# -- stage functions (issue #1479) ---------------------------------------------


def _stage_ctx(argv: list[str], root: Path | None = None) -> FinalizeContext:
    return FinalizeContext(args=parse_args(argv), root=root or Path("/repo"))


def test_stage_provenance_clean_passes() -> None:
    ctx = _stage_ctx(["123"])
    with patch(_MOD + ".pr_provenance.check_pr", return_value=_clean()) as mock_check:
        _stage_provenance(ctx)
    mock_check.assert_called_once_with("123")


def test_stage_provenance_violation_raises(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_with_violation()),
        pytest.raises(FinalizeAbort, match="provenance"),
    ):
        _stage_provenance(ctx)
    err = capsys.readouterr().err
    assert "provenance violation" in err.lower()
    assert "closed" in err


def test_stage_provenance_override_passes() -> None:
    ctx = _stage_ctx(["123", "--allow-provenance-violation"])
    with patch(_MOD + ".pr_provenance.check_pr", return_value=_with_violation()):
        _stage_provenance(ctx)  # must not raise


def test_stage_provenance_advisory_surfaced(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _stage_ctx(["123"])
    with patch(_MOD + ".pr_provenance.check_pr", return_value=_with_advisory()):
        _stage_provenance(ctx)
    assert "advisory" in capsys.readouterr().err.lower()


def test_stage_merge_uses_wait_and_merge() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="feature/42-x"),
    ):
        _stage_merge(ctx)
    engine.assert_called_once_with("123", strategy="squash")
    assert ctx.merged_branch == "feature/42-x"


def test_stage_merge_abort_raises() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbortError("is a draft")),
        pytest.raises(FinalizeAbort, match="is a draft"),
    ):
        _stage_merge(ctx)


def test_stage_merge_already_merged_skips_engine() -> None:
    ctx = _stage_ctx(["123"])
    with (
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="feature/42-x"),
    ):
        _stage_merge(ctx)
    engine.assert_not_called()
    assert ctx.merged_branch == "feature/42-x"


def test_stage_merge_dry_run_skips_engine() -> None:
    ctx = _stage_ctx(["123", "--dry-run"])
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".github.head_ref", return_value="feature/42-x"),
    ):
        _stage_merge(ctx)
    engine.assert_not_called()
    assert ctx.merged_branch == "feature/42-x"
```

Extend the import block at the top of the test file:

```python
from vergil_tooling.bin.vrg_finalize_pr import (
    FinalizeAbort,
    FinalizeContext,
    _check_cd_workflow_status,
    _finalize_specific_pr,
    _stage_merge,
    _stage_provenance,
    _worktree_is_dirty,
    main,
    parse_args,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -q`
Expected: ImportError — `FinalizeAbort` (et al.) not defined.

- [ ] **Step 3: Implement the context, exception, and two stage functions**

In `src/vergil_tooling/bin/vrg_finalize_pr.py`, extend the imports:

```python
from dataclasses import dataclass, field
```

After `_ETERNAL_BY_MODEL`, add:

```python
class FinalizeAbort(Exception):
    """Stage failure with a human-readable reason.

    Raised by stage functions to mark the stage failed; run_pipeline
    records the message in the stage result and the final summary.
    """


@dataclass
class FinalizeContext:
    """State threaded through the pipeline stages."""

    args: argparse.Namespace
    root: Path
    merged_branch: str | None = None
    deleted: list[str] = field(default_factory=list)
```

After `_check_cd_workflow_status`, add the two stage functions (bodies
lifted from `_finalize_specific_pr`, with `return 1` replaced by
`raise FinalizeAbort`):

```python
def _stage_provenance(ctx: FinalizeContext) -> None:
    """Pre-merge provenance check (issue #1289)."""
    args = ctx.args
    print(f"Checking provenance for PR {args.pr}...")
    result = pr_provenance.check_pr(args.pr)

    for adv in result.advisories:
        print(
            f"  ADVISORY: {adv.login} ({adv.role.value}) performed '{adv.action}' "
            "— permitted but advisory.",
            file=sys.stderr,
        )

    if result.violations:
        print(f"ERROR: PR {args.pr} has provenance violations:", file=sys.stderr)
        for v in result.violations:
            print(
                f"  {v.login} ({v.role.value}) performed forbidden action '{v.action}'.",
                file=sys.stderr,
            )
        if not args.allow_provenance_violation:
            msg = (
                "provenance violations found — re-run with "
                "--allow-provenance-violation to override consciously"
            )
            raise FinalizeAbort(msg)
        print(
            "  Overriding provenance violations per --allow-provenance-violation.",
            file=sys.stderr,
        )


def _stage_merge(ctx: FinalizeContext) -> None:
    """Merge the PR (or confirm it is already merged) and record its branch."""
    args = ctx.args
    if github.pr_state(args.pr) == "MERGED":
        print(f"PR {args.pr} already merged.")
    elif args.dry_run:
        print(f"  [dry-run] wait for green, then merge PR {args.pr} (--{args.strategy})")
    else:
        try:
            pr_merge.wait_and_merge(args.pr, strategy=args.strategy)
        except pr_merge.MergeAbortError as exc:
            raise FinalizeAbort(str(exc)) from exc
    ctx.merged_branch = github.head_ref(args.pr)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -q`
Expected: all pass (new stage tests plus the untouched existing suite).

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1479-finalize-progress
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type refactor --scope finalize --message "extract provenance and merge stage functions" --body "First slice of the progress-framework adoption (spec: docs/specs/2026-06-06-finalize-pr-progress-adoption-design.md): FinalizeContext threads state between stages, FinalizeAbort signals stage failure by raising instead of returning codes. main() is not rewired yet.

Ref #1479"
```

---

### Task 2: cleanup, validation, cd-check stages and build_stages

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py`
- Test: `tests/vergil_tooling/test_vrg_finalize_pr.py`

The cleanup body is duplicated from `main()` for one commit; Task 3
deletes the original. The validation stage switches from bare
`subprocess.run` (inherited stdout — would corrupt a live display) to
`progress.run` (streams through the active session).

- [ ] **Step 1: Write the failing tests**

Append to the `# -- stage functions (issue #1479)` section:

```python
def test_build_stages_with_pr() -> None:
    names = [s.name for s in build_stages(include_pr=True)]
    assert names == ["provenance", "merge", "cleanup", "validation", "cd-check"]


def test_build_stages_without_pr() -> None:
    names = [s.name for s in build_stages(include_pr=False)]
    assert names == ["cleanup", "validation", "cd-check"]


def test_build_stages_failure_modes() -> None:
    modes = {s.name: s.mode for s in build_stages(include_pr=True)}
    assert modes["provenance"] == "fail_fast"
    assert modes["merge"] == "fail_fast"
    assert modes["cleanup"] == "fail_fast"
    # fail_defer preserves current semantics: a validation failure still
    # runs the cd-check, and either failure exits 1.
    assert modes["validation"] == "fail_defer"
    assert modes["cd-check"] == "fail_defer"


def test_stage_validation_streams_through_progress(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + ".progress.run", return_value=0) as run:
        _stage_validation(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-container-run", "--", "vrg-validate")


def test_stage_validation_uses_uv_for_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + ".progress.run", return_value=0) as run:
        _stage_validation(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-container-run", "--", "uv", "run", "vrg-validate")


def test_stage_validation_failure_raises(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    err = subprocess.CalledProcessError(1, ("vrg-container-run",))
    with (
        patch(_MOD + ".progress.run", side_effect=err),
        pytest.raises(FinalizeAbort, match="validation failed"),
    ):
        _stage_validation(ctx)


def test_stage_validation_dry_run_skips(tmp_path: Path) -> None:
    ctx = _stage_ctx(["--dry-run"], root=tmp_path)
    with patch(_MOD + ".progress.run") as run:
        _stage_validation(ctx)
    run.assert_not_called()


def test_stage_cd_check_passes_when_clean(tmp_path: Path) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with patch(_MOD + "._check_cd_workflow_status", return_value=None):
        _stage_cd_check(ctx)  # must not raise


def test_stage_cd_check_raises_on_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _stage_ctx([], root=tmp_path)
    with (
        patch(
            _MOD + "._check_cd_workflow_status",
            return_value="CD workflow run 999 on develop (deadbee) ended with 'failure'.",
        ),
        pytest.raises(FinalizeAbort, match="CD workflow"),
    ):
        _stage_cd_check(ctx)
    assert "CD workflow" in capsys.readouterr().err


def test_stage_cd_check_dry_run_skips(tmp_path: Path) -> None:
    ctx = _stage_ctx(["--dry-run"], root=tmp_path)
    with patch(_MOD + "._check_cd_workflow_status") as check:
        _stage_cd_check(ctx)
    check.assert_not_called()
```

Add `import subprocess` to the test module's imports if not already
present (the file currently imports only `CompletedProcess` from
subprocess — add the module import alongside it), and extend the
`vrg_finalize_pr` import block with `_stage_cd_check`,
`_stage_cleanup`, `_stage_validation`, `build_stages`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -q`
Expected: ImportError — `build_stages` (et al.) not defined.

- [ ] **Step 3: Implement the three stages and build_stages**

Extend the module imports in `src/vergil_tooling/bin/vrg_finalize_pr.py`:

```python
from vergil_tooling.lib import (
    config,
    git,
    github,
    pr_merge,
    pr_provenance,
    progress,
    worktrees,
)
from vergil_tooling.lib.progress import Stage
```

After `_stage_merge`, add:

```python
def _stage_cleanup(ctx: FinalizeContext) -> None:
    """Switch to the target branch, pull, and prune merged branches,
    worktrees, and remote-tracking references."""
    args = ctx.args
    root = ctx.root

    try:
        vergil_config = config.read_config(root)
        model = vergil_config.project.branching_model
    except FileNotFoundError:
        model = ""
    except config.ConfigError as exc:
        raise FinalizeAbort(str(exc)) from exc

    eternal = {"gh-pages"}
    if model in _ETERNAL_BY_MODEL:
        eternal.update(_ETERNAL_BY_MODEL[model])
    else:
        print("WARNING: branching_model not found; protecting develop and main.", file=sys.stderr)
        eternal.update(("develop", "main"))

    current = git.current_branch()
    if current != args.target_branch:
        print(f"Switching to {args.target_branch}...")
        _run(["checkout", args.target_branch], dry_run=args.dry_run)
    else:
        print(f"Already on {args.target_branch}.")

    print(f"Pulling latest from origin/{args.target_branch}...")
    _run(["fetch", "--tags", "--force", "origin", args.target_branch], dry_run=args.dry_run)
    _run(["pull", "--ff-only", "origin", args.target_branch], dry_run=args.dry_run)

    deleted = ctx.deleted

    # Explicit-target cleanup: the just-merged PR branch. The default
    # squash strategy rewrites history onto the target, so the branch is
    # never an ancestor and `git branch --merged` cannot see it — without
    # this step the flagship flow would merge and then silently fail to
    # clean up the very worktree it inferred the PR from.
    if ctx.merged_branch and ctx.merged_branch not in eternal:
        if git.read_output("branch", "--list", ctx.merged_branch):
            print(f"Cleaning up merged PR branch {ctx.merged_branch}...")
            if _delete_branch_and_worktree(ctx.merged_branch, root, dry_run=args.dry_run):
                deleted.append(ctx.merged_branch)
        else:
            print(f"  Merged PR branch {ctx.merged_branch} has no local branch — skipping.")

    # Ancestry sweep for stragglers. `git branch --merged` classifies a
    # branch as merged when its tip is an *ancestor* of the target —
    # which a branch just created from the target's tip satisfies
    # trivially. That is the normal starting state of every new issue
    # worktree, so an unguarded sweep races parallel agent sessions in
    # their creation-to-first-commit window and deletes their branch and
    # worktree out from under them. Two guards close the race
    # (issue #1445); both gate the worktree removal exactly as strictly
    # as the branch deletion, since they sit ahead of either action.
    print("Checking for merged local branches...")
    for branch in git.merged_branches(args.target_branch):
        if branch in eternal or branch in deleted:
            continue
        # Guard 1 — skip zero-commit branches. A tip equal to the
        # target's carries no merged work, so deleting it saves nothing;
        # it is also exactly what an in-flight branch looks like before
        # its first commit.
        if git.commit_sha(branch) == git.commit_sha(args.target_branch):
            print(
                f"  Skipping {branch}: tip matches {args.target_branch} "
                "(zero-commit branch, nothing to clean up)"
            )
            continue
        # Guard 2 — require merge evidence. Ancestry alone cannot
        # distinguish a merged branch from one created off an older
        # target tip; only sweep branches whose head has a closed or
        # merged PR. The just-merged PR branch is handled by the
        # explicit-target step above, which keeps its own behavior.
        if github.closed_pr_for_branch(branch) is None:
            print(f"  Skipping {branch}: no closed or merged PR for this branch")
            continue
        if _delete_branch_and_worktree(branch, root, dry_run=args.dry_run):
            deleted.append(branch)

    print("Pruning stale remote-tracking references...")
    if args.dry_run:
        print("  [dry-run] git remote prune origin")
    else:
        git.run("remote", "prune", "origin")

    # -- working-tree cleanliness gate (issue #472) ----------------------------
    if not args.dry_run:
        dirty = git.working_tree_status()
        if dirty:
            print(
                f"ERROR: {args.target_branch} working tree is not clean.",
                file=sys.stderr,
            )
            for line in dirty.splitlines():
                print(f"  {line}", file=sys.stderr)
            msg = (
                f"{args.target_branch} working tree is not clean — "
                "clean up these files before starting the next issue"
            )
            raise FinalizeAbort(msg)

    # Replaces the old end-of-main summary block: the framework owns the
    # run footer, so the branch/deleted/pruned details live here where
    # they still reach the renderer and the run log.
    print(
        f"Cleanup complete: branch {args.target_branch}; "
        f"deleted {' '.join(deleted) if deleted else '(none)'}; remotes pruned."
    )


def _stage_validation(ctx: FinalizeContext) -> None:
    """Run canonical validation to catch problems on the target branch
    before the next PR is created.

    Streams through ``progress.run`` — a bare ``subprocess.run`` with
    inherited stdout would write raw lines under the live display and
    strand stale frames (issue #1470).
    """
    args = ctx.args
    if args.dry_run:
        print("  [dry-run] vrg-container-run -- [uv run] vrg-validate")
        return
    print("Running post-finalization validation via vrg-container-run...")
    if (ctx.root / "pyproject.toml").is_file():
        cmd: tuple[str, ...] = ("vrg-container-run", "--", "uv", "run", "vrg-validate")
    else:
        cmd = ("vrg-container-run", "--", "vrg-validate")
    try:
        progress.run(cmd)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"post-finalization validation failed (exit {exc.returncode}) — "
            f"fix {args.target_branch} before creating the next PR"
        )
        raise FinalizeAbort(msg) from exc


def _stage_cd_check(ctx: FinalizeContext) -> None:
    """Docs-publish sanity check (issue #303). CD is async relative to the
    merge that triggers it, so a failure doesn't block any PR — but it
    means the site or release artifacts may be stale."""
    args = ctx.args
    if args.dry_run:
        print("  [dry-run] check most recent CD workflow run")
        return
    failure = _check_cd_workflow_status(args.target_branch)
    if failure is None:
        return
    print("ERROR: most recent CD workflow run did not succeed.", file=sys.stderr)
    print(f"  {failure}", file=sys.stderr)
    print(
        "  CD workflow is async — investigate before the next merge so",
        file=sys.stderr,
    )
    print("  the site doesn't drift further from develop.", file=sys.stderr)
    raise FinalizeAbort("most recent CD workflow run did not succeed")


def build_stages(*, include_pr: bool) -> tuple[Stage, ...]:
    """Assemble the pipeline for the resolved mode.

    provenance/merge run only when a PR was given or inferred; cleanup,
    validation, and cd-check always run. validation and cd-check are
    fail_defer so a validation failure still surfaces the CD status —
    matching the pre-pipeline control flow.
    """
    common = (
        Stage("cleanup", _stage_cleanup, "fail_fast"),
        Stage("validation", _stage_validation, "fail_defer"),
        Stage("cd-check", _stage_cd_check, "fail_defer"),
    )
    if not include_pr:
        return common
    return (
        Stage("provenance", _stage_provenance, "fail_fast"),
        Stage("merge", _stage_merge, "fail_fast"),
        *common,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1479-finalize-progress
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type refactor --scope finalize --message "add cleanup, validation, and cd-check stages with build_stages" --body "Validation now streams through progress.run instead of a bare subprocess.run with inherited stdout, which would corrupt a live display (issue #1470 failure mode). The cleanup body is temporarily duplicated from main(); the next commit rewires main() through run_pipeline and deletes the original.

Ref #1479"
```

---

### Task 3: Rewire main() through run_pipeline; adapt the test suite

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py`
- Test: `tests/vergil_tooling/test_vrg_finalize_pr.py`

This is the behavior-parity switchover. Three systematic test
consequences (explained in Background): `run_pipeline` writes a RunLog
under `repo_root / ".vergil"` (so `Path("/repo")` patches become
`tmp_path`); stage-internal stderr prints now reach stdout via the
renderer (so `.err` assertions on in-stage messages become `.out`); and
`_finalize_specific_pr` disappears (tests patch the stage functions or
their internals instead).

- [ ] **Step 1: Rewrite parse_args docstring-adjacent wiring and main()**

In `src/vergil_tooling/bin/vrg_finalize_pr.py`:

(a) At the end of `parse_args`'s argument definitions, before
`return parser.parse_args(argv)`, add:

```python
    progress.add_progress_args(parser, ())
```

(b) Replace the entire `main()` body with:

```python
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not git.is_main_worktree():
        main_root = git.main_worktree_root()
        print(
            f"ERROR: vrg-finalize-pr must be run from the main worktree at {main_root},\n"
            "  not from a secondary worktree. The script removes worktrees during cleanup\n"
            "  and cannot safely do so when the calling shell's CWD is inside one.",
            file=sys.stderr,
        )
        return 1

    root = git.repo_root()

    # --cleanup-only is the scriptable release path: no inference, no
    # prompts, no stdin reads — args.pr stays None and only the
    # cleanup/validation/cd-check stages run (issue #1448).
    if args.pr is None and not args.cleanup_only:
        try:
            args.pr = _infer_pr(root, args.target_branch)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raise

    # Inference and its prompts above need the real TTY; everything
    # below runs under the progress pipeline, which owns stdout/stderr
    # (issue #1479).
    ctx = FinalizeContext(args=args, root=root)
    return progress.run_pipeline(
        ctx,
        build_stages(include_pr=args.pr is not None),
        command="vrg-finalize-pr",
        label="vrg-finalize-pr",
        args=args,
        repo_root=root,
    )
```

(c) Delete `_finalize_specific_pr` entirely.

(d) Update the module docstring: keep the three-modes block, replace the
trailing paragraph with:

```python
"""Finalize a pull request: provenance check, merge, and cleanup.

Three modes:

- ``vrg-finalize-pr <PR>`` — run the pre-merge provenance check, merge
  the PR (or confirm it is already merged), then run the cleanup below.
  This replaces the manual web merge + post-merge repo cleanup.
- ``vrg-finalize-pr`` (no PR) — interactive: infer which PR to finalize
  from open PRs in ``.worktrees/`` worktrees, confirm via prompts, then
  run the cleanup. Requires a real terminal on both stdin and stdout.
- ``vrg-finalize-pr --cleanup-only`` — non-interactive release path:
  skip inference and merge entirely, never read stdin, and run only the
  cleanup: switch to the target branch, fast-forward pull, delete
  merged local branches, and prune stale remote-tracking references.
  This is what ``vrg-release`` invokes (issue #1448).

Output renders through the stage-aware progress framework
(issue #1479): standalone TTY runs get the live display with collapsed
per-stage status lines and a run log at
``.vergil/vrg-finalize-pr-<stamp>.log``; piped runs fall back to the
plain renderer. ``vrg-release`` passes ``--output-format plain``
explicitly because two live displays cannot nest (issue #1470).

After cleanup succeeds, validation runs in the dev container, then the
most recent CD workflow run on the target branch is checked and the
command fails if it did not succeed (issue #303 — docs publish is async
and used to fail silently).
"""
```

(e) Imports: `json` and `subprocess` are still used
(`_check_cd_workflow_status`, `_worktree_is_dirty`, `_stage_validation`);
leave them. Remove nothing else.

- [ ] **Step 2: Adapt the test suite**

All edits in `tests/vergil_tooling/test_vrg_finalize_pr.py`.

**(a) Imports:** remove `_finalize_specific_pr` from the import block
(deleted in Step 1c).

**(b) New autouse fixture** after `_clean_working_tree`:

```python
@pytest.fixture(autouse=True)
def _validation_passes() -> Iterator[None]:
    """Default: the post-finalization validation child succeeds.

    The validation stage streams through progress.run (issue #1479);
    tests that assert validation behavior re-patch it directly — the
    innermost patch wins.
    """
    with patch(_MOD + ".progress.run", return_value=0):
        yield
```

**(c) Delete the `_validation_ok` helper** and remove every
`patch(_MOD + ".subprocess.run", return_value=_validation_ok())` /
`patch("vergil_tooling.bin.vrg_finalize_pr.subprocess.run", ...)`
context line whose only purpose was validation, plus the
`patch(_MOD + ".subprocess.run") as mock_sub` +
`mock_sub.return_value.returncode = 0` pairs in the PR-path tests.
Affected tests: `test_main_library_release`,
`test_main_already_on_target`, `test_main_no_profile`,
`test_main_application_promotion`, `test_main_docs_single_branch`,
`test_main_no_deleted_branches`,
`test_main_removes_worktree_before_deleting_branch`,
`test_main_skips_worktree_remove_when_branch_not_in_worktree`,
`test_main_skips_dirty_worktree`,
`test_main_cleans_docker_cache_on_branch_delete`,
`test_main_returns_one_on_docs_failure`,
`test_pr_arg_runs_provenance_then_merges`,
`test_provenance_violation_override_merges`,
`test_advisory_surfaced_and_merge_proceeds`,
`test_already_merged_skips_merge`, `test_no_pr_arg_is_cleanup_only`,
`test_sweep_skips_zero_commit_branch`,
`test_sweep_skips_branch_without_merge_evidence`,
`test_sweep_deletes_straggler_with_commits_and_merge_evidence`,
`test_explicit_target_cleanup_*` (all five).
Keep `subprocess.run` patches that serve `_check_cd_workflow_status` or
`_worktree_is_dirty` direct tests — those functions are unchanged.

**(d) Validation-failure test** — replace the subprocess patch:

```python
def test_main_validation_fails(tmp_path: Path) -> None:
    _make_profile(tmp_path, "library-release")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(
            _MOD + ".progress.run",
            side_effect=subprocess.CalledProcessError(1, ("vrg-container-run",)),
        ),
        patch(_MOD + "._check_cd_workflow_status", return_value=None) as cd_check,
    ):
        result = main([])
    assert result == 1
    # fail_defer: a validation failure must not skip the CD check.
    cd_check.assert_called_once()
```

**(e) Convert the two command-construction tests** to direct stage tests
(they were added as `_stage_validation` tests in Task 2 —
`test_stage_validation_streams_through_progress` and
`test_stage_validation_uses_uv_for_python`); delete the old
`test_main_calls_docker_run` and
`test_main_container_run_uses_uv_for_python`.

**(f) stderr → stdout assertion moves** (in-stage messages now flow
through the renderer to stdout):

- `test_provenance_violation_aborts_without_merge`: change
  `err = capsys.readouterr().err` to `out = capsys.readouterr().out`
  and assert against `out`. Also add
  `patch(_MOD + ".github.head_ref", return_value="feature/42-x")` is
  NOT needed (fail_fast stops before merge); but the test must patch
  `git.repo_root` to `tmp_path` (it already does).
- `test_advisory_surfaced_and_merge_proceeds`: `"advisory" in
  capsys.readouterr().out.lower()`.
- `test_main_fails_on_dirty_working_tree`: assert the three substrings
  against `.out` instead of `.err`.
- `test_main_returns_one_on_docs_failure`: assert `"CD workflow" in
  capsys.readouterr().out`.
- `test_main_skips_dirty_worktree` and the sweep-guard tests already
  assert `.out` — unchanged.
- `test_main_rejects_secondary_worktree` stays `.err` (pre-pipeline).

**(g) `Path("/repo")` → `tmp_path`** (RunLog needs a writable
repo_root). Change `_cleanup_path_mocks` to take the path:

```python
@contextmanager
def _cleanup_path_mocks(root: Path) -> Iterator[None]:
    """Neutralize the post-merge cleanup path for inference-focused tests.

    Keeps main() off real git/config/gh calls after the part under test.
    """
    with (
        patch(_MOD + ".git.repo_root", return_value=root),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        yield
```

Each caller gains a `tmp_path: Path` parameter and passes it:
`_cleanup_path_mocks(tmp_path)`. Callers:
`test_no_arg_single_candidate_confirms_and_finalizes`,
`test_no_arg_multiple_candidates_menu_then_confirm`,
`test_no_arg_excluded_worktree_prints_reason`.

The other `Path("/repo")` users switch their
`patch(_MOD + ".git.repo_root", return_value=Path("/repo"))` line to
`return_value=tmp_path` and gain the `tmp_path: Path` parameter:
`test_explicit_pr_skips_inference_and_prompts`,
`test_cleanup_only_skips_inference_and_prompts`,
`test_explicit_target_cleanup_deletes_merged_pr_branch`,
`test_explicit_target_cleanup_respects_eternal_branches`,
`test_explicit_target_cleanup_skips_missing_local_branch`,
`test_explicit_target_cleanup_skips_dirty_worktree`,
`test_explicit_target_cleanup_bypasses_sweep_guards`.

**(h) `_finalize_specific_pr` patch replacements:**

*Inference tests* — patch the provenance stage as the observation
point (run_pipeline resolves `_stage_provenance` through the module
global at `build_stages` call time, so the patch takes effect) **and
stub `_stage_merge`** — the real merge stage calls `github.pr_state`
first thing, which the old `_finalize_specific_pr` mock used to
intercept; without the stub these tests would make live `gh` calls.
The provenance stage receives the `FinalizeContext`, so the PR
assertion reads `ctx.args.pr`:

```python
def test_no_arg_single_candidate_confirms_and_finalizes(tmp_path: Path) -> None:
    with (
        _cleanup_path_mocks(tmp_path),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + ".prompt_yes_no", return_value=True) as confirm,
        patch(_MOD + "._stage_provenance") as prov,
        patch(_MOD + "._stage_merge"),
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    assert "PR #7" in confirm.call_args[0][0]
    assert prov.call_args[0][0].args.pr == "https://github.com/o/r/pull/7"
```

(With `_stage_merge` stubbed, `ctx.merged_branch` stays None and the
cleanup stage's explicit-target step is skipped — these tests assert
inference behavior, not cleanup.)

Apply the same substitution (`_finalize_specific_pr` → `_stage_provenance`
plus the `_stage_merge` stub, assertion path `.args.pr`) to
`test_no_arg_multiple_candidates_menu_then_confirm` (asserts pull/8) and
`test_no_arg_excluded_worktree_prints_reason` (no call_args assertion —
just swap the patch target and add the stub).

`test_no_arg_single_candidate_decline_exits_zero_without_action`: swap
`patch(_MOD + "._finalize_specific_pr") as fin` for
`patch(_MOD + "._stage_provenance") as fin`; assertions unchanged
(decline exits pre-pipeline, so no RunLog and no tmp_path needed).

`test_explicit_pr_skips_inference_and_prompts`: swap the patch target to
`_stage_provenance`, keep `fin.assert_called_once()`, and add
`patch(_MOD + ".github.pr_state", return_value="MERGED")` so the real
`_stage_merge` takes the already-merged short-circuit instead of making
a live `gh` call (it already patches `github.head_ref`).

`test_cleanup_only_skips_inference_and_prompts`: swap
`patch(_MOD + "._finalize_specific_pr") as fin` for
`patch(_MOD + "._stage_provenance") as fin`; `fin.assert_not_called()`
still holds (no PR → stages not included).

*Explicit-target cleanup tests* (the five `test_explicit_target_cleanup_*`)
— replace `patch(_MOD + "._finalize_specific_pr", return_value=0)` with
two patches that let the real stages run and take the already-merged
short-circuit:

```python
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
```

(`github.head_ref` is already patched in each, which is how
`ctx.merged_branch` gets set.)

**(i) Direct `_finalize_specific_pr` tests** — delete
`test_finalize_specific_pr_uses_wait_and_merge`,
`test_finalize_specific_pr_merge_abort_returns_error`, and
`test_finalize_specific_pr_already_merged_skips_engine`; their coverage
moved to the Task 1 stage tests (`test_stage_merge_uses_wait_and_merge`,
`test_stage_merge_abort_raises`,
`test_stage_merge_already_merged_skips_engine`).

**(j) Merge-abort exit code at main level** — add (in the
`# -- merge + provenance` section) to pin the rc-1 path end to end:

```python
def test_merge_abort_returns_one_from_main(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.pr_merge.wait_and_merge", side_effect=MergeAbortError("is a draft")),
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.git.current_branch") as branch,
    ):
        result = main(["42"])
    assert result == 1
    branch.assert_not_called()  # fail_fast: cleanup never starts
    assert "is a draft" in capsys.readouterr().out
```

- [ ] **Step 3: Run the module's tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -q`
Expected: all pass. Triage any failure against the parity table in the
spec before changing production code — the intent is that only test
plumbing changes, not behavior.

- [ ] **Step 4: Run full validation**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: green (this also catches lint/typecheck issues in the new
code and any other test module touching `vrg_finalize_pr`).

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1479-finalize-progress
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type feat --scope finalize --message "render vrg-finalize-pr through the progress pipeline" --body "main() splits into a pre-pipeline phase (worktree guard, interactive PR inference — the prompts need the real TTY) and a run_pipeline execution of the provenance/merge/cleanup/validation/cd-check stages. Standalone TTY runs get the live display and a .vergil run log; piped runs fall back to the plain renderer. Exit codes preserved (0/1, plus the framework's 130 on SIGINT); fail_defer on validation and cd-check keeps the validation-failure-still-checks-CD behavior.

Ref #1479"
```

---

### Task 4: vrg-release passes --output-format plain explicitly

**Files:**
- Modify: `src/vergil_tooling/lib/release/finalize.py:33`
- Test: `tests/vergil_tooling/test_release_finalize.py`

- [ ] **Step 1: Update the failing test**

In `tests/vergil_tooling/test_release_finalize.py`, replace
`test_close_and_finalize_streams_through_progress`:

```python
def test_close_and_finalize_streams_through_progress() -> None:
    """Issue #1470: the cleanup child must not inherit the TTY — raw writes
    under the live display strand stale frames on screen. Its output streams
    through the progress session (live display + run log) instead; stdin is
    closed so the child can never block on a terminal read (issue #1448).
    --output-format plain states the rendering contract explicitly: the
    child is itself progress-aware (issue #1479) and two live displays
    cannot nest."""
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(_MOD + ".progress.run", return_value=0) as run,
    ):
        close_and_finalize(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-finalize-pr", "--cleanup-only", "--output-format", "plain")
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_release_finalize.py -q`
Expected: FAIL — command tuple lacks the two new arguments.

- [ ] **Step 3: Implement**

In `src/vergil_tooling/lib/release/finalize.py`, update the invocation
and its comment:

```python
    print("Running vrg-finalize-pr...")
    # --cleanup-only is the non-interactive release path: no PR
    # inference, no prompts (issue #1448). Output streams through the
    # progress session so the live display stays intact and the run log
    # captures the cleanup narration (issue #1470) — the child must not
    # inherit the TTY: raw writes under the live display strand stale
    # frames on screen. The child is itself progress-aware (issue #1479);
    # --output-format plain states the rendering contract explicitly
    # because two live displays cannot nest (TTY auto-detection on the
    # piped stdout is the backstop). stdin is closed so the child can
    # never block on a terminal read. Captured stderr rides on
    # CalledProcessError for ReleaseError.detail; the streamed lines
    # mean warnings are never silently swallowed.
    try:
        progress.run(
            ("vrg-finalize-pr", "--cleanup-only", "--output-format", "plain"),  # noqa: S607
            stdin=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="close-finalize",
            command="vrg-finalize-pr --cleanup-only --output-format plain",
            message="vrg-finalize-pr failed.",
            detail=exc.stderr,
        ) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_release_finalize.py -q`
Expected: all pass (the `ReleaseError` test matches on
`"vrg-finalize-pr"`, which still holds).

- [ ] **Step 5: Commit**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1479-finalize-progress
vrg-git add src/vergil_tooling/lib/release/finalize.py tests/vergil_tooling/test_release_finalize.py
vrg-commit --type feat --scope release --message "pass --output-format plain to the nested vrg-finalize-pr" --body "vrg-finalize-pr is now progress-aware; the close-finalize call site states its rendering contract explicitly rather than depending on how the child is spawned. TTY auto-detection on the piped stdout remains the backstop.

Ref #1479"
```

---

### Task 5: Final validation, smoke check, and PR handoff

**Files:**
- Create: `.vergil/pr-template.yml`

- [ ] **Step 1: Full validation**

Run: `cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1479-finalize-progress && vrg-container-run -- uv run vrg-validate`
Expected: green across lint, typecheck, tests, audit, common checks.

- [ ] **Step 2: Dry-run smoke check**

Run (from the worktree):
`UV_PROJECT_ENVIRONMENT=.venv-host uv run vrg-finalize-pr --cleanup-only --dry-run --output-format plain`
Expected: plain-rendered stage narration
(`→ cleanup  starting...` … `✓ vrg-finalize-pr complete`), exit 0, and a
new `.vergil/vrg-finalize-pr-*.log`. If the host venv is unavailable,
note it and rely on the suite — do not improvise another install path.
Delete any `.vergil/*.log` files created by the smoke check before
committing (they are scratch, and `.vergil` logs are not tracked).

- [ ] **Step 3: Write the PR handoff template**

Write `.vergil/pr-template.yml` in the worktree:

```yaml
issue: 1479
title: "feat(finalize): adopt the progress framework in vrg-finalize-pr"
linkage: Ref
summary: |
  Adopts the stage-aware progress framework in vrg-finalize-pr per the
  approved design (docs/specs/2026-06-06-finalize-pr-progress-adoption-design.md).

  - main() splits into a pre-pipeline phase (worktree guard, interactive
    PR inference on the real TTY) and a run_pipeline execution of five
    stages: provenance, merge, cleanup, validation, cd-check.
  - Standalone TTY runs get the live display, collapsed per-stage status
    lines, and a .vergil/vrg-finalize-pr-*.log run log; piped runs fall
    back to the plain renderer.
  - vrg-release passes --output-format plain explicitly on the
    --cleanup-only invocation — two live displays cannot nest
    (issue #1470); TTY auto-detection is the backstop.
  - Behavior parity: exit codes preserved (0/1, plus the framework's
    130 on SIGINT); fail_defer on validation/cd-check keeps the
    "validation failure still checks CD, exit 1" flow; validation now
    streams through progress.run instead of inheriting stdout.
notes: |
  The old end-of-run summary block (Branch/Deleted/Remotes) is
  superseded by the framework footer; the details moved to the end of
  the cleanup stage narration.
```

- [ ] **Step 4: Stop — human submits**

Do **not** run `vrg-submit-pr`. Report completion; the human runs
`vrg-submit-pr`, merges, and finalizes.

---

## Self-review notes (spec → plan)

- Spec "two-phase main" → Task 3. Stage table → Tasks 1–2 (modes match:
  fail_fast × 3, fail_defer × 2). Nested invocation flag → Task 4.
  Summary-output supersession → Task 2 (`_stage_cleanup` final print).
  Logging/exit-code/dry-run parity → Task 3 step 2(d)/(j) tests.
  Testing section → Tasks 1–4 test work; release-finalize assertion →
  Task 4 step 1.
- Type consistency: `FinalizeContext(args, root, merged_branch, deleted)`
  used identically in Tasks 1–3; `build_stages(*, include_pr: bool)`
  defined in Task 2, called in Task 3; `FinalizeAbort` raised in all
  five stages.
