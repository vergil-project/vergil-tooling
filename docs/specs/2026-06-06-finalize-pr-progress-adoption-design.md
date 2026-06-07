# vrg-finalize-pr progress-framework adoption — design

- **Issue**: vergil-project/vergil-tooling#1479
- **Date**: 2026-06-06
- **Status**: approved
- **Depends on**: the stage-aware progress framework
  (`docs/specs/2026-06-05-progress-framework-design.md`, #1456)

## Problem

`vrg-finalize-pr` produces flat `print()` output. Standalone runs on a
TTY get no live feedback during the two long-running steps — the
wait-for-green merge poll (`pr_merge.wait_and_merge`) and the container
validation run (`vrg-container-run -- uv run vrg-validate`).

The command cannot simply inherit the rich treatment from its caller:
when `vrg-release`'s close-finalize stage runs
`vrg-finalize-pr --cleanup-only`, two `rich.Live` displays cannot nest.
Issue #1470 established the failure mode (raw child writes under a Live
display strand stale frames) and #1471 fixed it by routing the child
through `progress.run()`, leaving the child plain.

This design adopts the progress framework in `vrg-finalize-pr` itself,
so standalone runs get the live display while the nested invocation
explicitly opts into plain rendering.

## Design

### Two-phase `main()`

`main()` splits into a pre-pipeline phase and a staged pipeline.

**Pre-pipeline** (real TTY, plain output, unchanged behavior):

1. Arg parsing — gains `--output-format` and `--output-window` via
   `progress.add_progress_args(parser, ())` (no skip flags).
2. Main-worktree guard (`git.is_main_worktree()`), which must error
   plainly before any live display starts.
3. Interactive PR inference (`_infer_pr` and its prompts). Prompts read
   stdin and must run before `run_pipeline` redirects stdout/stderr.
   Decline still exits 0 plainly.

**Pipeline** — stages built dynamically from the resolved mode, then
executed via `progress.run_pipeline(ctx, stages, command="vrg-finalize-pr",
label="finalize-pr", args=args, repo_root=root)`:

| Stage      | Included when | Mode         | Contents |
|------------|---------------|--------------|----------|
| provenance | PR resolved   | `fail_fast`  | `pr_provenance.check_pr`, advisory printing, violation gate honoring `--allow-provenance-violation` |
| merge      | PR resolved   | `fail_fast`  | already-merged short-circuit or `pr_merge.wait_and_merge` |
| cleanup    | always        | `fail_fast`  | config read (eternal branches), branch switch, fetch/pull, explicit-target branch+worktree deletion, ancestry sweep, remote prune, working-tree cleanliness gate (#472) |
| validation | always        | `fail_defer` | `vrg-container-run -- [uv run] vrg-validate` via `progress.run()` |
| cd-check   | always        | `fail_defer` | `_check_cd_workflow_status` (#303); a failed CD run raises so the stage records as failed |

`fail_defer` on validation and cd-check preserves current semantics: a
validation failure today still runs the CD check and the command exits
1; a deferred failed stage does exactly that under `run_pipeline`.

Stage functions raise on failure (the framework's failure signal)
instead of returning codes. Existing `print()` narration inside stage
code is kept as-is — `run_pipeline` redirects `sys.stdout`/`sys.stderr`
through `_EmitWriter`, so every line reaches the renderer and the run
log without call-site changes.

### Stage context

A small dataclass threads state between stages:

```python
@dataclass
class FinalizeContext:
    args: argparse.Namespace
    root: Path
    merged_branch: str | None = None
    deleted: list[str] = field(default_factory=list)
```

`merge` records `merged_branch` (resolved via `github.head_ref` when a
PR is given); `cleanup` consumes it and accumulates `deleted` for the
summary.

### Nested invocation from vrg-release

`lib/release/finalize.py` adds the explicit rendering contract:

```python
progress.run(
    ("vrg-finalize-pr", "--cleanup-only", "--output-format", "plain"),
    stdin=subprocess.DEVNULL,
)
```

The call site states its requirement rather than depending on how the
child is spawned. TTY auto-detection remains the backstop: the child's
stdout is a pipe under `progress.run`, so `detect_format()` would pick
`PlainRenderer` even without the flag.

### Summary output

The manual summary block (`Finalization complete.` / `Branch:` /
`Deleted:` / `Remotes: pruned`) is superseded by the framework's
`build_summary` footer. The branch/deleted/pruned details move to the
end of the cleanup stage's narration, so they remain in the run log and
the rolling window without duplicating the framework summary.

### Logging

`run_pipeline` gives `vrg-finalize-pr` its own
`.vergil/vrg-finalize-pr-YYYYMMDD-HHMMSS.log`, written in standalone
and nested runs alike. In nested runs this duplicates lines also
captured in vrg-release's log; that is intentional — the finalize log
exists independently of the caller, and both prune at `LOG_RETAIN`.

### Exit codes and dry-run

- 0 / 1 semantics preserved exactly; `run_pipeline` additionally
  standardizes 130 on SIGINT (new, intentional).
- `--dry-run` is unchanged: stages run and print `[dry-run]` lines,
  which flow through the renderer like any other narration.

## Alternatives considered

- **Single-stage wrapper** (whole `main()` body as one stage): minimal
  restructuring, but no per-phase collapse and a single opaque spinner
  for a five-minute run. Rejected for poor observability.
- **Auto-detection only** (no vrg-release change): works today, but the
  suppression would be implicit — a future change to how the child is
  spawned could silently reintroduce nested rich output. Rejected in
  favor of the explicit flag with auto-detection as backstop.

## Testing

- Stage functions become named, individually testable units; existing
  `vrg-finalize-pr` tests adapt to call them directly.
- New tests: stage-list construction per mode (explicit PR, inferred
  PR, no-PR cleanup confirm, `--cleanup-only`), and failure-mode
  mapping (provenance violation → fail_fast abort; validation failure →
  deferred, cd-check still runs, exit 1).
- `test_release_finalize` asserts the `--output-format plain` argument
  in the nested invocation.
- Full validation: `vrg-container-run -- uv run vrg-validate`.
