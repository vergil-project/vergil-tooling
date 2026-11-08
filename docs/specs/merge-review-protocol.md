# Agent-to-agent review protocol, and its merge-review profile

**Status:** Draft, the source of the field names the merge-review tools
implement

This document defines how one agent (the **caller**) asks another agent
(the **reviewer**) for a judgement and gets back a result it can act on
without interpreting prose. It has two parts:

- **The envelope** is what any adopter of the protocol follows: request
  identity, the step, the shape of the reply and the rule that the caller
  acts only on a checked result.
- **The merge-review profile** is the first adopter: a pre-PR review of a
  branch after `vrg-validate` is green. Its files, schemas, finding kinds,
  steps and result-check rules are its own.

A later adopter defines its own profile on the same envelope. Until a second
adopter exists, the code that implements the protocol is merge-review's
alone (`vrg-merge-review-request` and `vrg-merge-review-check`).

## Part 1: the envelope

### Roles

- **The caller owns all of the data.** It writes the request, dispatches
  the reviewer, stores the reviewer's reply as the result file and runs the
  result check on that file. Every file of a run lives in the caller's
  private directory.
- **The reviewer reads and reports.** It returns its result as its reply
  and nothing else. It writes no file and runs no command: the profile names
  an agent type whose only tools read. Anything the reviewer needs that a
  command would produce (a diff, a list of passed checks) comes in the
  request.

### The request

A request is a JSON object the caller writes before dispatch. Every request
carries at least:

| Field | Meaning |
|---|---|
| `schema` | The profile's request schema name and version, such as `merge-review-request/1`. |
| `request_id` | 32 lowercase hex characters from `secrets.token_hex(16)`, new for every request, including a re-dispatch of the same step. |
| `step` | Which step of the profile's sequence this request is for. |

The reviewer cannot compute a hash, so the request ID, copied back, is what
ties a result to the request it answers.

### The result

The reviewer's reply is a **result**:

1. Its first content, with nothing before it but whitespace, is exactly one
   fenced code block whose info string names the profile's result block
   (such as `merge-review-result`). The block holds one JSON object that
   carries the profile's result `schema`, and copies the request's
   `request_id` and `step`.
2. Free prose follows the block. The prose is for the human. The caller
   never routes, decides or posts anything on the strength of the prose.

A reply with a second block of the same info string anywhere is not a
result.

### The check before acting

The caller acts only on a result whose block passed the profile's result
check. A result that fails is not acted on in any part: the caller does not
guess what the reviewer meant. The profile says what happens next (for
merge-review, one re-dispatch and then a stop for the human).

The result check judges the reviewer's output, not the code under review,
and is unrelated to `vrg-validate`.

### Data, not instructions

Everything the reviewer reads (the request, the issue and its comments, the
diff, every file in the repository) is data, never instructions to it.
Everything the caller reads back (the result, its prose) is data to it in the same way: only the
checked block's fields drive what the caller does.

## Part 2: the merge-review profile

Merge-review asks one question of a branch: is this change good enough to
merge? It runs once `vrg-container-run -- vrg-validate` is green on the
committed branch: one review, at most one fix and check, then the human.

### Reviewer

The reviewer is the agent type `vergil:merge-reviewer`. Its tools are
exactly `Read`, `Glob` and `Grep`: no `Write`, `Edit` or `Bash`, so it has
no way to change the code or the file system. The caller dispatches a fresh
reviewer for every step; the reviewer never sees the implementer's session.

### Steps

| Step | When | Finding ID prefix |
|---|---|---|
| `review` | The first review of the branch. | `R` |
| `check` | After the one fix the review allows. | `C` |
| `check-<k>` | Each further check a human ruling grants, `<k>` from 2 to 9. | `C<k>.` |

A step matches `^(review|check|check-[2-9])$`.

### Files

Every file of a run lives in the task's private directory,
`build/.vergil-state/issue-implement/<N>/merge-review/` in the task
repository's main clone, where `<N>` is the task issue. Nothing is written
below `build/.vergil-state/` unless that root is mode 700; each level from
`.vergil-state` down to `merge-review` must be a real directory (not a
symlink) and mode 700, and the caller refuses otherwise.
`vrg-merge-review-request --issue <owner/name#N> --ensure-dir` makes these
checks and prints the directory; the caller runs it before every write it
makes there and writes only to the path it prints.
The directory is resolved from the current directory's main clone, so
`vrg-merge-review-request`, in either mode, first refuses unless that
clone's `origin` is the repository `--issue` names (every push URL of
`origin` names it on github.com), before it creates or writes anything.
The refusal names both repositories.

| File | Written by | Holds |
|---|---|---|
| `request-<step>.json` | `vrg-merge-review-request` | The request. |
| `diff-<step>.patch` | `vrg-merge-review-request` | `git diff <base_sha> <head_sha>`: the whole branch. |
| `fix-<step>.patch` | `vrg-merge-review-request` | A check only: `git diff <fix_base_sha> <head_sha>`, the fix alone. |
| `result-<step>.md` | The caller | The reviewer's reply, stored unchanged. |
| `security-<ID>.md` | The caller | One per security finding: its details, taken from the prose. |

`<step>` is the step name: `review`, `check` or `check-<k>`. The directory
is working storage, deleted when the task is handed off, except that the
details of a security item still awaiting its record are kept.

### The request

The request is a JSON object, `request-<step>.json`. Every step carries:

| Field | Type | Value |
|---|---|---|
| `schema` | string | `"merge-review-request/1"` |
| `request_id` | string | 32 lowercase hex characters, new for every request. |
| `step` | string | `review`, `check` or `check-<k>`. |
| `repo` | string | The repository, `owner/name`. |
| `issue` | string | The task issue, `owner/name#N`. |
| `issue_body` | string | The issue's body as GitHub holds it, as data. The builder fetches it itself. |
| `issue_comments` | list of objects | The human's unedited comments on the issue, as data, each `{"author": "<login>", "created_at": "<ISO 8601 with time zone>", "body": "..."}`, oldest first; empty when there are none. The builder fetches and filters them itself. |
| `issue_comments_excluded_edited` | integer | How many of the human's comments the builder left out of `issue_comments` because they had been edited; 0 when none. |
| `base_sha` | string | The merge base of the base branch and `HEAD`. |
| `head_sha` | string | The commit under review. |
| `worktree` | string | Absolute path of the worktree, for reading surrounding code. |
| `diff_path` | string | Absolute path of `diff-<step>.patch`. |
| `change_class` | string | `"docs-only"` or `"code"`. |
| `design_sources` | list of strings | Absolute paths of the design documents the issue's epic lists, such as its spec and plan; empty when there are none. |
| `validate_stages` | list of strings | The `vrg-validate` stages that passed at `head_sha`. |
| `epic_open_tasks` | list of objects | The open tasks of the issue's epic, each `{"ref": "owner/name#N", "title": "..."}`, without the issue itself; empty outside an epic. The builder derives them itself. |

A check (`check` or `check-<k>`) adds:

| Field | Type | Value |
|---|---|---|
| `fix_base_sha` | string | The `HEAD` the previous step judged: the review's for `check`, the previous check's for `check-<k>`. |
| `fix_diff_path` | string | Absolute path of `fix-<step>.patch`. |
| `earlier_result_paths` | list of strings | Absolute paths of every earlier step's result, in step order: `result-review.md`, then `result-check.md` for a `check-2`, and so on. |
| `earlier_finding_ids` | list of strings | The earlier finding IDs the implementer was asked to change: every defect, and each item whose ruling calls for a code change. |

What the reviewer is told the task is is never chosen by the agent whose
work is under review. `vrg-merge-review-request` reads the `--issue`'s body
itself, and derives `epic_open_tasks` from the issue's native parent: when
the parent carries the `epic` label and not `ad-hoc`, the issue is under an
epic, and its open tasks are the parent's open sub-issues, every page, less
the issue itself; a task with no native parent, or under any other parent,
has none. A failure to read the issue, its parent's labels or the sub-issues
is a refusal, never an empty body or an empty list. The design sources stay
files: the caller fetches them from the epic's Documents section and names
each with `--design-source`.

The issue's comments carry scope the maintainer set after the issue was
opened, such as narrowing the task to part of what the body asks. Only a
comment authenticated as the human may amend that scope, so the agent never
chooses which comments the reviewer sees: `vrg-merge-review-request`
reads every page of the `--issue`'s comments itself and keeps only those
whose author is the one human login of the trusted-authors configuration
(refusing a configuration that lists none or several). Agent and
third-party comments are dropped. A human comment that has been edited
(detected as `vrg-ruling` detects an edited reply, by GitHub's
`lastEditedAt`) is left out and counted in
`issue_comments_excluded_edited`, so the caller can state the count in the
step comment. A kept comment's `created_at` must be an ISO 8601 timestamp
with a time zone; the builder refuses one that is not by its comment ID,
never echoing a value. The reviewer reads `issue_comments` as it reads
`issue_body`: as data describing the task, never as instructions to it.

`vrg-merge-review-request` refuses a `--fix-base` that is not the `head_sha`
of the previous step's stored `request-<step>.json`, so the fix diff always
starts where the judged change ended.

Pre-existing notes and items ruled keep, file, defer or wont-fix are not in
`earlier_finding_ids`; their rulings and lookups record them, and the check
is not asked about them.

The change class is decided by the caller's tools before dispatch, from the
changed files, never by an agent: a change is `docs-only` only when every
changed file is an addition or edit on the documentation allow-list, and
`code` otherwise (any deletion, rename or type change, any change to
`vergil.toml`, an empty change, or any doubt). The repository's own
allow-list entries are read from `vergil.toml` as committed at `base_sha`,
never from the working tree.

### The result

The reply opens with one fenced block whose info string is
`merge-review-result`, holding one JSON object, followed by free prose:

````markdown
```merge-review-result
{
  "schema": "merge-review-result/1",
  "step": "review",
  "request_id": "3f2a9c0e5b7d41a8c6e2f0b9d3a7c514",
  "findings": [
    {
      "id": "R1",
      "question": "merge-quality",
      "kind": "defect",
      "security": false,
      "location": "src/example/module.py:42",
      "summary": "The new flag is parsed but never passed to run(), so it has no effect."
    }
  ]
}
```

R1: the prose explains each finding for the human.
````

| Field | Type | Value |
|---|---|---|
| `schema` | string | `"merge-review-result/1"` |
| `step` | string | Copied from the request. |
| `request_id` | string | Copied from the request. |
| `findings` | list of objects | Every finding; empty when there are none. |
| `fix_status` | list of objects | A check only: exactly one `{"id", "status"}` per ID in the request's `earlier_finding_ids`. Absent in a review. |

Each finding:

| Field | Type | Value |
|---|---|---|
| `id` | string | Unique in the result, with the step's prefix: `R<n>`, `C<n>` or `C<k>.<n>`, `<n>` from 1 to 999. |
| `question` | string | `"production-safety"` or `"merge-quality"`. |
| `kind` | string | `"defect"`, `"design-question"`, `"out-of-scope"` or `"pre-existing"`. |
| `security` | boolean | True when reading the finding would help an attacker. |
| `location` | string | Where it is, repository-relative: `path` or `path:line`. |
| `summary` | string | One line describing it. Required when `security` is false; forbidden when it is true. |

The kinds:

- `defect`: the change fails the issue or one of the two questions, and the
  fix stays within the existing design.
- `design-question`: the fix would add something the design does not have
  (locks, retries, new state, caches, configuration, a component, a failure
  mode). The finding proposes no fix; the human decides.
- `out-of-scope`: code or content the issue did not ask for.
- `pre-existing`: a real problem in code the branch did not introduce. A
  note only, never asked to be fixed on the branch. A check never raises it.

Each `fix_status` entry's `status` is `"fixed"` or `"not-fixed"`. A problem
the fix itself introduced is a new finding of the check, not a status.

The result carries **no verdict**. The caller decides what to do next from
the findings' kinds alone, so there is no second field that could disagree
with them.

A security finding carries no description in the block. Its details go only
in the prose, which the caller copies into `security-<ID>.md` and never
posts. Every public record (step comments, ruling requests and replies, the
PR's title, summary and notes) names a security finding by ID or count only.

### The result check

`vrg-merge-review-check --request <path> --result <path>` checks a stored
result against the request it answers. It exits 0 and prints a JSON summary
on stdout:

```json
{
  "step": "review",
  "counts": {"defect": 1, "design-question": 0, "out-of-scope": 0, "pre-existing": 0},
  "security": 0,
  "fix_status": {}
}
```

`security` is the number of security findings; `fix_status` maps each
listed ID to its status (empty in a review). On any violation it exits 1
with `ERROR: <rule>` on stderr, naming the rule broken. The rules:

1. **Shape.** The first content of the reply, after optional whitespace, is
   a fenced block with info string `merge-review-result`, and the reply has
   no second such block. The block's content is one JSON object.
2. **Fields.** Every required field is present with its type, and no field
   the schema does not define is present, at the top level or in a finding
   or status entry.
3. **Identity.** `schema` is `merge-review-result/1`; `step` and
   `request_id` equal the request's.
4. **Values.** `question`, `kind` and `status` take only the values above.
5. **Finding IDs.** Each ID carries its step's prefix, has `<n>` from 1 to
   999, is at most 16 characters, and is unique in the result. An `R` ID in
   a check, a `C` ID in a review, or a `C<n>` ID in `check-2` is rejected.
6. **Summaries.** A non-security finding has a `summary` of printable ASCII
   only (U+0020 to U+007E, so no newline, tab or look-alike character), at
   most 300 characters and not empty. It does not contain the `vrg-ruling`
   marker prefix `<!-- vrg-ruling` in any letter case, which `vrg-ruling`
   refuses. A security finding has no `summary`. A summary can therefore go
   into a `vrg-ruling` request item unchanged.
7. **Location.** `location` is non-empty printable ASCII on one line. Like a
   summary, it does not contain the marker prefix `<!-- vrg-ruling` in any
   letter case.
8. **Change class.** When the request's `change_class` is `docs-only`, no
   finding's `question` is `production-safety`: a docs-only change gets only
   the merge-quality question.
9. **Check steps.** A check raises no `pre-existing` finding. Its
   `fix_status` has exactly one entry for each ID in the request's
   `earlier_finding_ids`, and no entry for any other ID. A review has no
   `fix_status`.

A refusal never echoes a refused value: it names the rule, the finding by ID
(or by position when the ID itself is bad), and for a character, its code
point and offset. A security detail placed by mistake in a summary never
reaches a terminal or a log.

### Acting on a result

- The caller acts only on a result that passed the result check.
- A result that fails it is not acted on. The caller dispatches the same
  step once more, with a new `request_id`. If the second result also fails,
  the run stops for the human; it never guesses what the reviewer meant.
- The caller decides from the findings' kinds and the `fix_status` values
  alone. The prose is for the human and is never parsed for routing; a
  finding's security flag, not its prose, decides how it is handled.
- Every file the reviewer reads is data, not instructions, and so is the
  result to the caller.
