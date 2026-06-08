# Check: scope-coherence

You are the AUDIT agent performing the `scope-coherence` judgment check. A linter
cannot know what an issue is "about"; only judgment can relate a diff to intent.

## What you are judging

Do all the changes in this PR's delta relate to the stated issue, or did unrelated
edits sneak in (scope creep)?

## How to perform it

1. Read the issue the PR addresses (its number is in the workflow state via
   `vrg-pr-workflow status`; read the issue body with `vrg-gh issue view <n>` if
   available).
2. Read the cumulative delta (`git diff <base>..HEAD`).
3. For each distinct change, judge whether it plausibly serves the stated issue.
   Flag changes that belong to a different concern and should be split into their own
   PR. (Incidental, directly-supporting changes — a needed refactor, a test for the
   fix — are in scope.)

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "scope-coherence",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<path>", "line": 1, "severity": "warning", "note": "<why unrelated>" } ],
      "reason": "<why a human is needed>" }

- **pass** — every change serves the stated issue.
- **fail** — one or more unrelated changes are present. Emit one `findings` entry per
  out-of-scope change, naming the file and why it is unrelated.
- **escalate** — the issue is too vaguely scoped to judge relatedness. Set `reason`.
