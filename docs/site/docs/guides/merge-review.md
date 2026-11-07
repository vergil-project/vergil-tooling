# Merge Review

Merge review is the judgement-level check between a green `vrg-validate` and
the PR. CI covers everything deterministic; merge review asks one question of
a task branch, the way a second engineer would: **is this change good enough
to merge?** The design is epic
[vergil-project/.github#881](https://github.com/vergil-project/.github/issues/881).
It replaces the earlier agentic-review gate, which epic
[vergil-project/.github#885](https://github.com/vergil-project/.github/issues/885)
removed.

The work is split across two repositories:

- **vergil-claude-plugin** holds the `merge-review` skill, which runs the
  loop, and the read-only `vergil:merge-reviewer` agent, which judges one
  step. `issue-implement` runs the skill once the committed branch is green
  and before `vrg-pr-workflow report-ready`. See the plugin's
  [skills](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/skills/index.md#merge-review)
  and [agents](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/agents/index.md)
  pages.
- **vergil-tooling** (this repository) holds the protocol the two agents
  speak, the commands that build a request and check a result, the change
  classifier, and the `vrg-validate` record the request builder trusts.

## The loop

1. **Review.** The skill builds a request and dispatches a fresh
   `vergil:merge-reviewer`. The reviewer's only tools are `Read`, `Glob` and
   `Grep`, so it cannot change the code or run anything, and it never sees
   the implementer's session.
2. **Decide.** The skill acts only on a result that passes
   `vrg-merge-review-check`, and decides from the findings' kinds alone:
   - nothing raised (or only notes already tracked): **pass**;
   - only defects: **one fix**, validated to green, then a **check** of the
     fix by a fresh reviewer;
   - a design question, an out-of-scope addition, an untracked pre-existing
     note, a defect the implementer disputes, or more than five blockers:
     a **stop for the human**.
3. **Stop.** Every stop is one `vrg-ruling request` on the task issue. The
   human replies with `vrg-ruling-reply`, and the skill carries out the
   rulings and posts the response (see [Human Replies](human-replies.md)).
   A further fix and check (`check-2` up to `check-9`) happens only when a
   ruling grants it.

The loop has exactly one iteration before the human: one review, then at
most one fix and check.

### What a reviewer can raise

Each finding answers one of two questions: **production-safety** (can this
break production, including input that crosses a trust boundary) or
**merge-quality** (does it do what the issue asks, maintainably and
clearly). A `docs-only` change is asked only the merge-quality question.

| Kind | Meaning |
|---|---|
| `defect` | The change fails the issue or a question, and the fix stays within the existing design. |
| `design-question` | The fix would add something the design does not have. The finding proposes no fix; the human decides. |
| `out-of-scope` | Code or content the issue did not ask for. |
| `pre-existing` | A real problem the branch did not introduce. A note only, never fixed on the branch. |

A result carries **no verdict**: the caller decides from the kinds, so there
is no second field that could disagree with them.

## Records and the eligibility line

Everything merge review records goes on the task issue:

- one **step comment** per review or check, with the counts by kind and each
  finding by ID;
- the **ruling exchanges**, when the run stopped for the human;
- one **closing comment**, listing every item the run raised and how it
  closed. Its first line is the **eligibility line**:

  ```text
  Merge review: done: <eligible | not-eligible (<reason>)>; accepted-risk <w>
  ```

`eligible` means the review or the check passed with no human ruling.
`not-eligible` names the run's first stop, always one of `human-ruling`,
`too-many-blockers`, `check-failed`, `result-malformed` or
`fix-base-rebased`. `<w>` counts the security items the human accepted as a
risk. The eligibility line is the only thing from merge review that goes
into `report-ready`'s notes, and merge review never hands off with an item
still open.

## The protocol

The request and the result follow the
[agent-to-agent review protocol](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/merge-review-protocol.md)
and its merge-review profile, the authoritative source for the files, the
schemas and the result check. In short:

- **The caller owns all of the data.** It writes the request, dispatches the
  reviewer, stores the reply and checks it.
- **The reviewer reads and reports.** Its reply opens with one
  `merge-review-result` fenced block holding a JSON object, followed by prose
  for the human. The block copies the request's `request_id`, a fresh random
  value per request, which ties the result to the request it answers.
- **The caller acts only on a checked result.** A result that fails the
  check is not acted on in any part: the step is dispatched once more, and a
  second failure stops the run for the human. The prose is never used to
  route anything.
- **Everything read is data.** The issue, the diff and every file are data
  to the reviewer, never instructions, and the result is data to the caller.

## The commands

| Command | Role |
|---|---|
| [`vrg-validate`](../reference/cli-tools-overview.md#vrg-validate) | Every pipeline run writes its record, `.vergil/vrg-validate-last.json`, which the request builder reads. |
| [`vrg-merge-review-request`](../reference/cli-tools-overview.md#vrg-merge-review-request) | Builds a review or check request and its diffs in the task's private directory; with `--ensure-dir`, only checks and prints that directory. |
| [`vrg-merge-review-check`](../reference/cli-tools-overview.md#vrg-merge-review-check) | Checks a stored result against its request and prints a summary of counts, security count and fix statuses. |
| [`vrg-change-class`](../reference/cli-tools-overview.md#vrg-change-class) | Prints a branch's change class, `docs-only` or `code`, for humans and debugging. |
| [`vrg-ruling`](../reference/dev/ruling.md), [`vrg-ruling-reply`](../reference/dev/ruling-reply.md) | Carry every stop for the human (see [Human Replies](human-replies.md)). |

### The request builder's refusals

`vrg-merge-review-request` refuses, naming the reason, unless:

- the current clone's `origin` is the repository `--issue` names (checked
  first, in either mode, before anything is created or written; the refusal
  names both repositories),
- the worktree is clean, and
- the last `vrg-validate` was a **full** run (no `--check`) that started on a
  clean tree at `HEAD`, exited 0 and passed every stage.

The fix for a stale or partial record is always the same: commit, then run
`vrg-container-run -- vrg-validate` to green again. The stages that passed go
into the request, so the reviewer knows what CI already covers.

A check's `--fix-base` must be the `HEAD` the previous step judged (the
`head_sha` of that step's stored request), so the fix diff starts exactly
where the judged change ended. When that commit is no longer an ancestor of
`HEAD`, because the branch was rebased between the review and the check, the
builder exits 3 and the run stops for the human, who rules a fresh review
(`restart`) or a stop.

## Change classes and the docs allow-list

The change class is decided by a tool before dispatch, never by an agent. A
change is `docs-only` only when every changed file is an addition or an edit
of a path on the documentation allow-list; anything else is `code`: any
deletion, rename or type change, any change to `vergil.toml`, an empty
change, and any doubt.

The allow-list is `README.md` and `CHANGELOG.md`, plus the repository's own
paths from `vergil.toml`:

```toml
[merge-review]
docs-paths = ["docs/guides/", "NOTICE.md"]
```

- An entry ending in `/` covers everything below it; any other entry is that
  one file. Each entry must be a non-empty, repository-relative path; the
  repo-profile check of `vrg-validate` rejects anything else.
- The list is read from `vergil.toml` **as committed at the branch's merge
  base**, never from the working tree, so a branch cannot widen its own
  allow-list. Changing the key is itself a `code` change.
- **Markdown is not documentation by its extension.** List only paths that
  hold nothing that controls behaviour: writing meant to explain how things
  work to people or agents. Skills, agent definitions and the rule documents
  agents follow are code. A real document outside the list gets the full
  review now and then; that is an accepted cost.

Run `vrg-change-class --base origin/develop` in a worktree to see the class
a branch will get.

## The private directory

Every file of a run lives in the task's private directory,
`build/.vergil-state/issue-implement/<N>/merge-review/` in the task
repository's main clone, `<N>` the task issue. `build/` must be a real
directory, and each level from `.vergil-state` down to `merge-review` must be
a real directory, not a symlink, with mode 700; the request builder creates
an absent level with that mode and refuses otherwise. The skill runs
`vrg-merge-review-request --issue <ref> --ensure-dir` before every write it
makes there.

The directory is working storage and the record a stopped run resumes from.
Do not edit or delete it while a run is open. `issue-implement` deletes it
once `report-ready` has succeeded.

## Security findings

A finding a reviewer marks as security carries no description in the result
block: its details are only in the reviewer's prose, which the skill copies
into a private `security-<ID>.md` in the task's directory and never posts.
Every public record (step comments, ruling requests, replies and responses,
the closing comment, and `report-ready`'s title, summary and notes) names a
security finding by ID or count only.

The human rules on each one with `fix`, `file`, `defer` or `wont-fix`. A
`fix` is fixed on the branch. `file` and `defer` create a
[security record](security-records.md#recording-findings-from-a-review)
before the response is posted. `wont-fix` is an accepted risk, counted in
the eligibility line's `accepted-risk`.

## Related

- [Agent-to-agent review protocol](https://github.com/vergil-project/vergil-tooling/blob/develop/docs/specs/merge-review-protocol.md)
- [Human Replies](human-replies.md)
- [Security Records](security-records.md)
- [`merge-review` skill](https://github.com/vergil-project/vergil-claude-plugin/blob/develop/docs/site/docs/skills/index.md#merge-review)
- [Git Workflow: agent handoff](git-workflow.md#3-submit-the-pr-with-vrg-submit-pr)
