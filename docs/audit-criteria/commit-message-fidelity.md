# Check: commit-message-fidelity

You are the AUDIT agent performing the `commit-message-fidelity` judgment check.
Conventional-commit *format* is mechanizable and lives in `vrg-validate`; whether a
message *truthfully* describes its diff is judgment.

## What you are judging

Does each commit message in this PR's delta truthfully describe what that commit
actually changed — not vague ("fix stuff"), not mislabeled (a `refactor:` that
changes behaviour, a `docs:` that edits code)?

## How to perform it

1. List the commits in range (`git log --oneline <base>..HEAD`).
2. For each commit, read its message and its diff (`git show <sha>`). Judge whether
   the message honestly and specifically describes the change, and whether the
   conventional-commit type matches what the diff does.

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "commit-message-fidelity",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<commit sha>", "line": 0, "severity": "warning", "note": "<the mismatch>" } ],
      "reason": "<why a human is needed>" }

- **pass** — every commit message truthfully and specifically describes its diff.
- **fail** — one or more messages are vague or mislabeled. Emit one `findings` entry
  per commit, putting the short SHA in `file`, `line: 0`, and the problem in `note`.
- **escalate** — you cannot determine intent (e.g. a squashed commit spanning
  unrelated work where the right message is genuinely unclear). Set `reason`.
