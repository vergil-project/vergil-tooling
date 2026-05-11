# Redesign PR and Issue Templates

**Issue:** #650
**Date:** 2026-05-11
**Status:** Design approved

## Problem

The PR template, issue template, `st-submit-pr`, and `pr-workflow`
skill accumulated independently and no longer align:

1. `st-submit-pr` constructs the PR body programmatically but reads a
   static Testing section from the template — a list of linter names
   pasted verbatim into every PR regardless of what was validated.
2. The `pr-workflow` skill tells agents to "populate the PR template
   fields" then immediately calls `st-submit-pr`, which ignores most
   of those fields.
3. The Testing section provides no useful information — CI results are
   visible on the PR itself.
4. Most consuming repos still use `Fixes` / `Closes` / `Resolves` in
   their PR templates, despite CI rejecting auto-close keywords.
5. The issue template has stale fields and an ad-hoc type list that
   has never been rationalized.
6. The `st-submit-pr` reference documentation contains several stale
   claims (wrong default linkage, nonexistent auto-merge behavior,
   wrong source path).

## Design Decisions

### PR template becomes a redirect stub

`st-submit-pr` is the required path for creating PRs. The GitHub UI
is not a supported interface. The template's only purpose is to tell
someone who opens the UI that they're in the wrong place.

New `.github/pull_request_template.md`:

```markdown
> **Do not create PRs manually.**
> Use [`st-submit-pr`](https://wphillipmoore.github.io/standard-tooling/reference/dev/submit-pr/).
```

### `st-submit-pr` becomes self-contained

Changes to `src/standard_tooling/bin/st_submit_pr.py`:

- **Delete `_extract_testing_section()`** — the template is a stub
  and no longer contains a Testing section to extract.
- **Remove the Testing section from the PR body.** The body becomes
  three sections:

  ```
  # Pull Request

  ## Summary

  - {summary}

  ## Issue Linkage

  - Ref {issue_ref}

  ## Notes

  - {notes}
  ```

- No changes to the CLI interface. `--issue`, `--summary`, `--title`,
  `--linkage`, `--notes`, and `--dry-run` remain as-is.

### `st-submit-pr` documentation fix

`docs/site/docs/reference/dev/submit-pr.md` has stale claims that
must be corrected:

| What | Current (wrong) | Corrected |
|------|-----------------|-----------|
| Source path | `src/standard_tooling/submit_pr.py` | `src/standard_tooling/bin/st_submit_pr.py` |
| Description | mentions "auto-merge configuration" | remove auto-merge references |
| Warning box | "configures auto-merge automatically" | remove |
| Default linkage | `Fixes` | `Ref` |
| `--title` | marked optional | marked required |
| Allowed linkages | `Fixes, Closes, Resolves, Ref` | `Ref` |
| Behavior step 2 | mentions merge/squash strategy | remove strategy references |
| Behavior step 3 | "Reads testing section from template" | remove |
| Behavior step 6 | "Enables auto-merge" | remove |
| Exit code 0 | "and auto-merge enabled" | remove |
| Examples | imply other linkage options exist | update to show `--title` (required); remove example that highlights `--linkage Ref` as a special case |

### Issue template redesigned as a 3-field form

New `.github/ISSUE_TEMPLATE/issue.yml`:

```yaml
name: Issue
description: Track work with explicit acceptance criteria.
body:
  - type: dropdown
    id: issue_type
    attributes:
      label: Issue type
      options:
        - feature
        - bug
        - documentation
        - refactor
        - chore
        - research
    validations:
      required: true
  - type: textarea
    id: problem
    attributes:
      label: Problem / Goal
      description: What is broken, missing, or needed? Include context and motivation.
      placeholder: Describe the problem or desired outcome.
    validations:
      required: true
  - type: textarea
    id: acceptance
    attributes:
      label: Acceptance criteria
      description: What does "done" look like? One sentence is fine for trivial changes.
      placeholder: List the conditions that must be true when this is complete.
    validations:
      required: true
```

`.github/ISSUE_TEMPLATE/config.yml` remains: `blank_issues_enabled: false`.

#### Standardized issue types

The dropdown is trimmed from 12 ad-hoc types to 6 canonical types.
This list represents kinds of work a human would ask for, not
commit-level implementation details.

| Type | Purpose |
|------|---------|
| feature | New capability |
| bug | Something is broken |
| documentation | Docs need creating or updating |
| refactor | Restructure without behavior change |
| chore | Maintenance (deps, config, cleanup) |
| research | Investigation before implementation can start |

Dropped types (`style`, `test`, `ci`, `build`, `rtfm`,
`observability`) can still appear as conventional commit prefixes —
they are commit-level distinctions, not issue-level categories.

#### Fields removed and why

| Field | Reason |
|-------|--------|
| Title prefix `[Issue]: ` | Noise in every issue title |
| Intro markdown block | No value — the form is self-explanatory |
| Summary (separate from Problem) | Redundant — merged into Problem / Goal |
| Acceptance criteria clarity checkbox | Cannot conditionally hide the textarea; contradicts the required textarea that says "write Obvious if obvious" |
| Validation / Evidence | Premature at issue creation; belongs in the PR |

### `pr-workflow` skill update

The `pr-workflow` skill in `standard-tooling-plugin` must be updated
to remove contradictory guidance:

- Remove instructions telling agents to "populate the PR template
  fields" — the template is a stub.
- Clarify that `st-submit-pr` is the sole path — agents prepare CLI
  arguments, not template content.
- Remove any reference to a Testing section in the PR body.

This change is part of the implementation scope (not a separate
issue).

### `st-create-issue` design draft

A new issue will be filed against this repo with a design sketch for
an `st-create-issue` CLI tool. The tool would:

- Accept `--type`, `--title`, `--problem`, `--acceptance` arguments.
- Enforce the same 6 issue types as the template dropdown.
- Create the issue via `gh issue create` with a structured body.
- Optionally apply a label matching the issue type.

This is a **design-only deliverable** — no implementation in this
change. The new issue is the starting point for a future session.

## Fleet Rollout

### Scope

22 non-archived repos with `standard-tooling.toml` receive updated
templates:

| Repo | PR template | Issue template |
|------|-------------|----------------|
| `standard-tooling` | replace | replace |
| `standard-tooling-docker` | replace | replace |
| `standard-actions` | replace | replace |
| `the-infrastructure-mindset` | replace | replace |
| `mq-rest-admin-common` | replace | replace |
| `ai-research-methodology` | replace | replace |
| `mq-rest-admin-dev-environment` | replace | replace |
| `mq-rest-admin-rust` | replace | replace |
| `standard-tooling-plugin` | replace | replace |
| `mq-rest-admin-java` | replace | replace |
| `mq-rest-admin-ruby` | replace | replace |
| `mq-rest-admin-go` | replace | replace |
| `mq-rest-admin-python` | replace | replace |
| `career-strategy` | replace | replace |
| `home-equity-project` | replace | replace |
| `mnemosys-operations` | replace | replace |
| `mnemosys-core` | replace | replace |
| `renegade-dotfiles` | replace | replace |
| `paad` | replace | replace |
| `mempalace` | replace | replace |
| `lunatick-racing` | replace | replace |
| `cognition` | replace | replace |

### Additional actions

- **Archive `mq-rest-admin-template`** — excluded from rollout.
- Rollout must skip any repo that becomes archived before
  implementation begins.

### Per-repo changes

Each repo receives:

1. `.github/pull_request_template.md` — replaced with the stub.
2. `.github/ISSUE_TEMPLATE/issue.yml` — replaced with the 3-field
   form.
3. `.github/ISSUE_TEMPLATE/config.yml` — kept as-is
   (`blank_issues_enabled: false`); created if missing.

## Out of Scope

- Implementation of `st-create-issue` (design draft only).
- Changes to `st-commit` or the pre-commit hook.
- Adding new CI checks or validation rules.
