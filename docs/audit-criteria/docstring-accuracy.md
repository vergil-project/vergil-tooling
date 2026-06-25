# Check: docstring-accuracy

You are the AUDIT agent performing the `docstring-accuracy` judgment check. A linter
can check that a docstring *exists*; only judgment can tell whether it *truthfully*
describes the code it documents.

## What you are judging

For every docstring added or changed in this PR's delta, does it accurately describe
what the current code actually does — parameters, return values, behaviour, and side
effects?

## How to perform it

1. Read the cumulative delta (`git diff <base>..HEAD`). Find added/modified
   docstrings (module, class, function, method).
2. For each, read the code it documents and compare. Look for: stale parameter or
   return descriptions, described behaviour the code no longer has, omitted side
   effects, or copy-paste from another symbol.

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "docstring-accuracy",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<path>", "line": 1, "severity": "warning", "note": "<the mismatch>" } ],
      "reason": "<why a human is needed>" }

- **pass** — every changed docstring matches its code (or no docstrings changed).
- **fail** — one or more docstrings misdescribe their code. Emit one `findings` entry
  per mismatch at the docstring's location, naming the discrepancy.
- **escalate** — the intended behaviour is ambiguous enough that you cannot tell
  whether the docstring or the code is wrong. Set `reason`.
