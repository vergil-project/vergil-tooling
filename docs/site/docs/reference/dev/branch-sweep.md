# vrg-branch-sweep

**Installed as:** `vrg-branch-sweep` (Python console script)

**Source:** `src/vergil_tooling/bin/vrg_branch_sweep.py` (rules in
`src/vergil_tooling/lib/branch_sweep.py`)

Finds the branches on a GitHub repo whose pull-request work is finished
and, with `--apply`, deletes them.

Finished branches are normally removed automatically, by two mechanisms:

- the **delete-on-merge** repo setting (*Automatically delete head
  branches*), which removes a branch when its PR merges;
- the **delete-on-close** rule (issue #3332), which removes a branch when
  its PR is closed without merging. GitHub never does this on its own.

`vrg-branch-sweep` is the **safety net** behind both. GitHub sometimes
skips its after-merge processing, and a merge can then fire no push event
and leave its branch behind. A PR can also be closed before the
delete-on-close rule existed, or outside it. Leftover branches are not
harmless: tools that treat a branch's existence as "work in flight" stall
on them, and they hide the dropped events that should get someone's
attention (issue #3333).

## Usage

```text
vrg-branch-sweep [--repo OWNER/NAME] [--apply]
```

- `--repo OWNER/NAME`: the repo to sweep. Defaults to the current
  directory's repo. Every lookup goes through the GitHub API, so the target
  does not need to be checked out.
- `--apply`: delete the finished branches. Without it the command is a
  **dry run**: it reports what it would delete and changes nothing.

## What counts as finished

A branch is finished, and deleted under `--apply`, when all of these hold:

1. it has at least one PR from the same repo (fork PRs that share the
   branch name are ignored);
2. none of its PRs is open;
3. its newest PR (the highest number) is merged or closed;
4. its current tip equals that PR's `headRefOid`.

When the tip differs, the branch name was reused after the PR finished
(issue #1719) and holds new work, so it is kept.

## What it never deletes

- The repo's default branch, `develop`, `main` and `gh-pages`.
- A protected branch. A branch counts as protected when the branches API
  reports `protected`, which covers classic branch protection and rulesets.
  Before deleting, the sweep also asks the rules-for-a-branch endpoint
  (`GET /repos/{owner}/{repo}/rules/branches/{branch}`) and keeps the
  branch if **any** active ruleset rule applies to it.
- A branch with **no PR**. An agent pushes its branch before
  `vrg-pr-workflow report-ready`, and that branch has no PR until the human
  runs `vrg-submit-pr`.

Under `--apply`, the tip is read again just before each delete. If the
branch moved after the listing, it is kept.

## Output

One line per branch, sorted by name: the branch, its newest PR, that PR's
state, and the action with its reason. A summary line follows.

```text
Sweeping finished branches in acme/widgets (dry run)
develop            -       -       kept (default branch)
feature/12-done    #40     MERGED  would delete (PR #40 merged)
feature/13-wip     #41     OPEN    kept (open PR #41)
feature/14-new     -       -       kept (no PR)
feature/9-reused   #30     MERGED  kept (reused branch name: tip 1a2b3c4d5e6f is not PR #30's head 9f8e7d6c5b4a)
release/2.0.55     #31     CLOSED  would delete (PR #31 closed)

acme/widgets: 6 branches, 2 would delete, 4 kept, 0 errors (dry run; pass --apply to delete)
```

Actions are `kept`, `would delete` (dry run), `deleted` (`--apply`) and
`error`.

## Failure handling

No branch is skipped quietly. When a `gh` call fails for one branch, the
command reports an `error` line with the cause, finishes the remaining
branches, and then exits 1. A failure to read the repo's default branch or
branch list stops the sweep at once with exit 1, since nothing can be
decided without them. Missing or invalid GitHub credentials end the run
with the standard authentication message.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every branch was processed without error |
| 1 | At least one branch failed, the branch list could not be read, or authentication failed |
| 2 | Invalid arguments |
