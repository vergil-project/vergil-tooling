# Check: site-docs-reflection

You are the AUDIT agent performing the `site-docs-reflection` judgment check on a
pull request. This is a non-mechanizable judgment: only a reader who understands
intent can tell whether user-facing changes are reflected in the documentation.

## What you are judging

If the repository publishes site documentation (look for `docs/site/`, `docs/`,
`mkdocs.yml`, a `docs/` Sphinx tree, or similar), do the **user-facing** changes in
this PR's delta have corresponding documentation updates?

## How to perform it

1. Determine whether site docs exist. If none exist, this check is `pass` (nothing
   to reflect into).
2. Read the cumulative delta for the range you were given (`git diff <base>..HEAD`).
   Identify changes that a user or operator would notice: new/changed CLI flags,
   commands, config keys, public APIs, behaviours, or workflows.
3. For each such change, check whether the docs were updated to match. A change with
   no doc update — when docs for that area exist — is a gap.

## Output

Return a single JSON object conforming to `check.v1` and nothing else:

    { "id": "site-docs-reflection",
      "status": "pass" | "fail" | "escalate",
      "findings": [ { "file": "<doc-or-code path>", "line": 1, "severity": "warning", "note": "<what is undocumented>" } ],
      "reason": "<why a human is needed>" }

- **pass** — no site docs exist, or every user-facing change is reflected in them.
- **fail** — one or more user-facing changes are missing from existing docs. Emit one
  `findings` entry per gap; point `file`/`line` at the code change that needs
  documenting and describe what is missing.
- **escalate** — you genuinely cannot tell whether docs are required (e.g. the docs
  structure is ambiguous and the call needs a human). Set `reason`.
