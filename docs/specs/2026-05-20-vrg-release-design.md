# vrg-release: Mechanized Release Workflow

**Date:** 2026-05-20
**Status:** Draft
**Target version:** vergil-tooling v2.1
**Related issues:** #918 (vrg-dependency-update extraction)

## Motivation

The release process is currently driven by an AI agent via the
`vergil:publish` skill. The agent sequences CLI tools, polls for async
artifacts, manages GitHub issues, and makes judgment calls when things
break. With the v2.1 VM architecture isolating agent credentials, the
release process must move to the host side as a fully mechanized,
human-invoked Python CLI tool.

The goal is a single command — `vrg-release` — that takes a repository
from its current state on develop through to a completed release on main,
with no human interaction required during the run. The human invokes it;
the tool does the rest or fails with diagnostics.

## Design Principles

**Fully non-interactive.** No prompts, no confirmation gates. The tool
runs start to finish. On failure, it stops immediately and reports
diagnostics to both stderr and the GitHub tracking issue.

**Stop and report — never stop and fix.** Every failure is surfaced, never
worked around. Manual workarounds mask tooling defects. The human triages
failures after the tool stops.

**No soft security gates.** The tool does not check whether it is running
on the host vs. inside the VM. The hard credential isolation in the VM
architecture is the security boundary. If the agent's credentials cannot
push, merge, or create releases, the tool fails naturally at the first
unauthorized operation.

**Trust the hard gates.** The existing retry layer in `lib/github.py`
handles transient GitHub API failures (502, 503, 504, 429, timeouts,
connection resets) with exponential backoff. Phase-level retry is not
needed — if a failure reaches the phase level, it is a real problem
requiring human triage.

## CLI Interface

```
vrg-release              # release current version (patch)
vrg-release minor        # bump to next minor, then release
vrg-release major        # bump to next major, then release
```

Entry point in `pyproject.toml`:

```toml
vrg-release = "vergil_tooling.bin.vrg_release:main"
```

Exit codes:
- `0` — release completed successfully
- `1` — failure (diagnostics on stderr and tracking issue)

## Architecture

### Approach: Functions with Shared Context

Each phase is a standalone function in its own module under
`lib/release/`. A thin orchestrator calls them sequentially, passing a
shared `ReleaseContext` dataclass that accumulates state as the workflow
progresses.

The orchestrator catches `ReleaseError` from any phase, posts failure
diagnostics to the tracking issue, and re-raises to terminate the
process.

### Module Layout

```
src/vergil_tooling/
  bin/
    vrg_release.py            # CLI entry point (parse args, call orchestrator)
  lib/
    release/
      __init__.py
      context.py              # ReleaseContext dataclass
      orchestrator.py         # Sequential phase runner
      preflight.py            # Preflight checks and version override
      prepare.py              # Issue creation, branch, changelog, PR
      merge.py                # Wait-poll-merge logic (shared by Phases 2 and 3)
      bump.py                 # Bump PR polling, linkage verification
      confirm.py              # Workflow watching, artifact verification
      finalize.py             # Close tracking issue, call vrg-finalize-repo
      tracking.py             # Issue creation, commenting, phase markers
      handoff.py              # Consumer-refresh display
```

### Existing Module Conflict

`lib/release.py` currently contains `is_release_branch()`. This file
must be renamed or its contents relocated when creating the
`lib/release/` package. Options: move to `lib/release/branches.py` or
fold into `lib/release/__init__.py`. Callers in `vrg_merge_when_green.py`
are retired (see below), so the only remaining consumers need to be
identified and updated.

### Retired Entry Points

These standalone CLI tools are absorbed into `vrg-release` and removed
from `pyproject.toml`:

- **`vrg-prepare-release`** — logic moves to `prepare.py`. The ecosystem
  detection, precondition checks, branch creation, changelog generation,
  and PR creation all become internal functions.
- **`vrg-merge-when-green`** — wait-poll-merge logic moves to `merge.py`.
  The `release/*` branch restriction is eliminated since `vrg-release`
  itself is the trust boundary.

### Kept Entry Points

- **`vrg-finalize-repo`** — remains standalone. It is a general-purpose
  housekeeping tool used after any PR merge, not just releases.
  `vrg-release` calls it as a subprocess in the finalize phase.

## ReleaseContext Dataclass

```python
@dataclass
class ReleaseContext:
    # -- Set during preflight --
    repo: str                            # owner/repo
    version: str                         # e.g., "2.1.0"
    repo_root: Path                      # absolute path
    version_override: str | None         # "minor", "major", or None

    # -- Set during prepare phase --
    issue_number: int | None = None      # tracking issue number
    issue_url: str | None = None         # full URL
    release_branch: str | None = None    # e.g., "release/2.1.0"
    release_pr_url: str | None = None    # PR to main

    # -- Set during merge-release phase --
    release_merge_sha: str | None = None

    # -- Set during merge-bump phase --
    bump_pr_url: str | None = None
    next_version: str | None = None      # e.g., "2.1.1"

    # -- Set during confirm-publish phase --
    publish_run_id: str | None = None
    publish_run_url: str | None = None
    docs_run_id: str | None = None
    docs_run_url: str | None = None
    tag: str | None = None               # e.g., "v2.1.0"
    develop_tag: str | None = None       # e.g., "develop-v2.1.0"
    release_url: str | None = None       # GitHub Release URL
```

Fields start as `None` and are populated as each phase completes. A phase
can assert its preconditions by checking that earlier fields are set.

The context contains everything needed to write the final tracking issue
summary — all PR URLs, run URLs, tags, and versions.

For future resumability, a `load_from_issue()` classmethod could
reconstruct this dataclass from tracking issue comments by parsing
phase-marker strings.

## Orchestrator

```python
def run_release(ctx: ReleaseContext) -> None:
    phases = [
        ("preflight",        preflight),
        ("prepare",          prepare),
        ("merge-release",    merge_release),
        ("merge-bump",       merge_bump),
        ("confirm-publish",  confirm_publish),
        ("close-finalize",   close_and_finalize),
        ("consumer-refresh", consumer_refresh),
    ]

    for phase_name, phase_fn in phases:
        try:
            phase_fn(ctx)
            comment_phase_complete(ctx, phase_name)
        except ReleaseError as exc:
            comment_phase_failed(ctx, phase_name, exc)
            raise
```

### ReleaseError

Custom exception carrying structured diagnostics: phase name, the
command that failed, stderr/stdout, and relevant context (PR URL, branch
name, etc.). The orchestrator catches it, posts diagnostics to the
tracking issue, then re-raises to terminate.

### Phase Markers

`comment_phase_complete()` writes a comment to the tracking issue with:
- A machine-parseable HTML comment:
  `<!-- vrg-release:<phase-name>:complete -->`
- Human-readable details: PR URLs, merge SHAs, workflow run URLs, etc.

`comment_phase_failed()` writes a comment with:
- A machine-parseable HTML comment:
  `<!-- vrg-release:<phase-name>:failed -->`
- Full diagnostics: error message, command, relevant URLs

These markers enable future resumability — a function could scan issue
comments and reconstruct the `ReleaseContext` from completed phases.

## Phase Details

### Preflight

1. Read `vergil.toml` and extract `repository_type`, `branching_model`.
2. Reject if `repository_type` is not `library` or `tooling`.
3. Confirm on `develop` branch with clean working tree and
   `develop == origin/develop` (fetch first).
4. Verify `gh` authentication via a lightweight command
   (e.g., `gh repo view --json name`).
5. Run `vrg-github-repo-config audit --repo <owner/repo>`. Non-zero
   exit is a hard stop (non-interactive — no override prompt).
6. Extract version from project manifest using the same detection
   priority as the current `vrg-prepare-release`: pyproject.toml,
   pom.xml, version.go, version.rb, Cargo.toml, plugin.json, VERSION
   file.
7. Compare version against the latest `v*` tag. If it matches an
   existing tag, abort — the post-publish version bump did not run.
8. If `version_override` is `minor` or `major`: bump the version in
   the manifest, regenerate the lockfile if needed (e.g., `uv lock`
   for Python), commit locally on develop. Do not push.
9. Populate `ctx.repo`, `ctx.version`, `ctx.repo_root`.

### Phase 1 — Prepare

1. Create a GitHub issue titled `release: <version>` with a body
   summarizing the release (version, date, repo). This is the tracking
   issue for the entire release operation.
2. Populate `ctx.issue_number`, `ctx.issue_url`.
3. Create `release/<version>` branch from develop. Fail if the branch
   already exists locally or on origin.
4. Merge `origin/main` into the release branch with `-X ours` strategy
   to prefer the release branch content on conflicts.
5. Generate changelog via `git-cliff`:
   - `CHANGELOG.md` (full history)
   - `releases/v<version>.md` (release notes for this version)
   - Boundary tag: `develop-v<version>`
   - If no publishable changes are detected, abort.
6. Commit: `chore(release): prepare <version>`.
7. Push release branch to origin.
8. Create PR to `main` with `Ref #<issue_number>` in the body.
9. Populate `ctx.release_branch`, `ctx.release_pr_url`.

### Phase 2 — Merge Release PR

1. Poll for checks registered on the release PR (the window between
   push and GitHub registering checks).
2. Wait for all checks to pass
   (`gh pr checks <pr> --watch --fail-fast`).
3. If the PR branch is behind its base, update the branch via the
   GitHub API and re-poll. Up to 5 update attempts.
4. If merge conflicts are detected, raise `ReleaseError`.
5. Merge with merge-commit strategy.
6. Populate `ctx.release_merge_sha`.

### Phase 3 — Merge Bump PR

Merging the release PR triggers `publish.yml` on main asynchronously.
Early in that workflow, the `version-bump-pr` composite action creates
a `release/bump-version-<next>` PR to develop. This phase drives that
merge in parallel with the slower async publish work.

1. Poll for the bump PR:
   `gh pr list --head release/bump-version-<next> --json url --jq '.[0].url'`
   Retry at ~10-second intervals. Timeout after 5 minutes with
   `ReleaseError`.
2. Verify issue linkage in the bump PR body. Look for
   `Ref #N`, `Fixes #N`, `Closes #N`, or `Resolves #N`. If missing:
   - Write a corrected body to a temp file adding
     `Ref #<issue_number>`.
   - Update the PR: `gh pr edit <url> --body-file <file>`.
   - Push an empty commit on the bump branch to retrigger CI
     (editing the PR body alone does not retrigger the
     `pr-issue-linkage` check).
3. Wait for checks, handle behind-base updates (same pattern as
   Phase 2, up to 5 attempts).
4. Merge with merge-commit strategy.
5. Populate `ctx.bump_pr_url`, `ctx.next_version`.

### Phase 4 — Confirm Publish

Block until both asynchronous workflows triggered by the release merge
complete successfully:

1. Locate the `publish.yml` run on main:
   `gh run list --workflow publish.yml --branch main --limit 1 --json databaseId --jq '.[0].databaseId'`
2. Block on it: `gh run watch --exit-status <run-id>`.
3. Locate the docs workflow run on main (workflow name from
   `vergil.toml` or default `"Documentation"`).
4. Block on it: `gh run watch --exit-status <run-id>`.
5. Verify artifacts:
   - Git tag `v<version>` exists on main.
   - Develop boundary tag `develop-v<version>` exists.
   - GitHub Release created.
6. Populate `ctx.publish_run_id`, `ctx.publish_run_url`,
   `ctx.docs_run_id`, `ctx.docs_run_url`, `ctx.tag`,
   `ctx.develop_tag`, `ctx.release_url`.

### Phase 5 — Close and Finalize

1. Post a final summary comment on the tracking issue containing:
   - All PR URLs (release PR, bump PR)
   - Tags (`v<version>`, `develop-v<version>`)
   - GitHub Release URL
   - `publish.yml` and docs workflow run URLs
   - Any failures encountered during the run
   - All references as full URLs (not short `#N`)
2. Close the tracking issue. The historical record is sealed before
   finalization so that if `vrg-finalize-repo` errors out, the
   bookkeeping is still done.
3. Run `vrg-finalize-repo` as a subprocess. This switches to develop,
   pulls latest, deletes merged branches, prunes stale remotes, and
   runs post-finalization validation.
4. Every error and warning from `vrg-finalize-repo` is surfaced. There
   is no such thing as a pre-existing or ignorable error.

### Phase 6 — Consumer Refresh

1. Read `[publish] consumer-refresh` from `vergil.toml`.
2. Template `<VERSION>` with `ctx.version` (simple string replacement).
3. Print the result to stdout.
4. If `consumer-refresh` is not configured, print a message stating
   that no consumer-refresh sequence is configured for this repository.

## Failure Model

**No retry at the phase level.** Transient GitHub API failures are
retried at the call level in `lib/github.py` (exponential backoff, up to
4 retries). If a failure reaches a phase function, it is a real problem.

**No resumability in v2.1.** On failure, the tool stops and the human
triages. Recovery is typically either "fix the underlying issue and
re-run" or "abandon this release, bump to the next patch level, start
over."

**Duplicate tracking issue guard.** Before creating a new tracking
issue, preflight checks for an existing open issue titled
`release: <version>`. If one exists, `vrg-release` aborts with a
message identifying the existing issue. The human must either close
the stale issue (abandoned release) or investigate before re-running.
This prevents duplicate tracking issues from cluttering the release
history and signals that a previous attempt needs triage.

**Future resumability path.** The tracking issue architecture supports
resumability naturally. Each phase writes a machine-parseable completion
marker as an HTML comment. A future `load_from_issue()` function could
reconstruct the `ReleaseContext` from these markers and skip completed
phases. This is deferred to a future version informed by real operational
experience with the mechanized tool.

**Abandoned release cleanup.** When a release is abandoned (human decides
to skip to the next patch level), artifacts from the failed attempt
(branches, PRs, partial tags) may remain. Cleanup of abandoned releases
is a separate concern — an audit tool can be built later based on
operational experience.

## Tracking Issue as State

The GitHub tracking issue serves three purposes:

1. **Operational log.** Each phase completion and failure is documented
   with full details as it happens.
2. **Audit trail.** The closed issue is the permanent record of the
   release operation — all PR URLs, workflow runs, tags, and any
   incidents.
3. **State mechanism.** Phase-marker HTML comments
   (`<!-- vrg-release:<phase>:complete -->`) enable future resumability
   without a separate state file. The issue is the single source of
   truth for how far the release progressed.

## Docs-Only Mode

Removed. There is no separate mode for documentation-only changes. A
release is a release regardless of what changed. If only documentation
was modified, the release is smaller, but every phase still runs with
full rigor.

## Dependency Updates

Extracted from the release workflow entirely. Dependency updates are a
development activity, not a release activity. They are tracked as a
separate command (`vrg-dependency-update`) in issue #918.

The human workflow becomes:
1. `vrg-release` — release the current version
2. (when appropriate) `vrg-dependency-update` — refresh dependencies
   on develop

## Version Override

When `minor` or `major` is specified:

1. The preflight phase computes the target version by incrementing
   the minor or major component (resetting lower components to zero).
2. Updates the version at the source of truth in the project manifest.
3. Regenerates the lockfile if applicable (e.g., `uv lock` for Python).
4. Commits locally on develop. Does not push to origin.
5. The release proceeds with the new version.

If the release fails after the local version bump, `origin/develop` is
untouched. The bump reaches origin only through the normal mechanism
(the bump PR in Phase 3).

## Commit Conventions

All commits produced by `vrg-release` use the `chore(release):` scope.
The `VRG_COMMIT_CONTEXT=1` environment variable is set automatically by
`lib/git.py` when the first argument is `commit`, satisfying the
pre-commit gate.

## Host vs Container Commands

`vrg-release` is a host-side tool. All git, gh, and vrg-* commands run
directly on the host. The only container invocation is validation via
`vrg-finalize-repo`, which internally calls
`vrg-docker-run -- [uv run] vrg-validate`.

## Consumer-Refresh Templating

The `[publish] consumer-refresh` value in `vergil.toml` supports a
`<VERSION>` placeholder that is replaced with the released version at
display time. No other placeholders are defined in v2.1. The value is
displayed verbatim after substitution — never executed.
