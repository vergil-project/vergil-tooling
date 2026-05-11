# Template Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align PR/issue templates, `st-submit-pr`, its docs, and the `pr-workflow` skill — then roll the corrected templates to all 22 consuming repos.

**Architecture:** Remove the Testing section from `st-submit-pr` and the PR body entirely; replace the PR template with a stub redirecting to the tool; redesign the issue template as a 3-field form; fix stale docs; update the `pr-workflow` skill; roll out fleet-wide via a scripted `gh` workflow.

**Tech Stack:** Python (st-submit-pr), YAML (issue template), Markdown (PR template, docs, skill), shell (fleet rollout script)

**Spec:** `docs/specs/2026-05-11-template-redesign-design.md`

**Worktree:** `/Users/pmoore/dev/github/standard-tooling/.worktrees/issue-650-template-redesign/`
**Branch:** `feature/650-template-redesign`

---

### Task 1: Update tests to expect new PR body format (no Testing section)

**Files:**
- Modify: `tests/standard_tooling/test_st_submit_pr.py`

The existing tests import and exercise `_extract_testing_section`. Those tests must be removed, and the dry-run tests must assert the new 3-section body format.

- [ ] **Step 1: Remove `_extract_testing_section` tests and update imports**

Remove the import of `_extract_testing_section` from line 11, and delete the three test functions that exercise it: `test_extract_testing_section_no_template`, `test_extract_testing_section_with_template`, and `test_extract_testing_section_testing_at_end`.

Updated import block:

```python
from standard_tooling.bin.st_submit_pr import (
    _resolve_issue_ref,
    main,
    parse_args,
)
```

Delete these three functions entirely (lines 73-94):
- `test_extract_testing_section_no_template`
- `test_extract_testing_section_with_template`
- `test_extract_testing_section_testing_at_end`

- [ ] **Step 2: Add a test that asserts the new PR body format**

Add a new test that uses `--dry-run` and captures stdout to verify the body has exactly three sections (Summary, Issue Linkage, Notes) and no Testing section:

```python
def test_dry_run_body_has_no_testing_section(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "pull_request_template.md").write_text(
        "> **Do not create PRs manually.**\n"
        "> Use `st-submit-pr`.\n"
    )
    with (
        patch("standard_tooling.bin.st_submit_pr.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0
    output = capsys.readouterr().out
    assert "## Summary" in output
    assert "## Issue Linkage" in output
    assert "## Notes" in output
    assert "## Testing" not in output
```

- [ ] **Step 3: Run tests to verify the new test fails (Testing section still present)**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-650-template-redesign && st-docker-run -- uv run pytest tests/standard_tooling/test_st_submit_pr.py -v`

Expected: `test_dry_run_body_has_no_testing_section` FAILS because the code still emits `## Testing`. The three deleted tests cause import errors (confirming they're gone). All other tests pass.

---

### Task 2: Update `st-submit-pr` to remove Testing section

**Files:**
- Modify: `src/standard_tooling/bin/st_submit_pr.py`

- [ ] **Step 1: Delete `_extract_testing_section()` and update the module docstring**

Remove the entire `_extract_testing_section` function (lines 47-62).

Update the module docstring (lines 1-3) from:

```python
"""PR submission wrapper that constructs standards-compliant PR bodies.

Populates .github/pull_request_template.md programmatically.
"""
```

to:

```python
"""PR submission wrapper that constructs standards-compliant PR bodies."""
```

- [ ] **Step 2: Remove the Testing section from the PR body in `main()`**

In the `main()` function, delete line 75 (`testing_section = _extract_testing_section(root)`).

Also remove the `re` import from line 9 (it was only used by `_extract_testing_section` and `_resolve_issue_ref` — check: `_resolve_issue_ref` uses compiled regexes `_ISSUE_PLAIN_RE` and `_ISSUE_CROSS_RE`, which still need `re`). Keep the `re` import.

Remove the `Path` import from line 12 — check: `Path` is still used on line 105 (`Path(tmp_path).unlink()`). Keep the `Path` import.

Replace the `pr_body` construction (lines 79-85) with:

```python
    pr_body = (
        f"# Pull Request\n\n"
        f"## Summary\n\n- {args.summary}\n\n"
        f"## Issue Linkage\n\n- {args.linkage} {issue_ref}\n\n"
        f"## Notes\n\n- {notes_section}"
    )
```

The only line removed from the body is `f"## Testing\n\n{testing_section}\n\n"`.

- [ ] **Step 3: Run tests to verify all pass**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-650-template-redesign && st-docker-run -- uv run pytest tests/standard_tooling/test_st_submit_pr.py -v`

Expected: All tests PASS, including the new `test_dry_run_body_has_no_testing_section`.

- [ ] **Step 4: Commit**

```
refactor(st-submit-pr): remove Testing section from PR body

The Testing section was extracted from the PR template and pasted
verbatim into every PR — a static linter list that added no value.
CI results are visible on the PR itself. The PR template is now a
stub redirecting to st-submit-pr, so there is no Testing section
to extract.

Ref #650
```

---

### Task 3: Replace PR template with stub

**Files:**
- Modify: `.github/pull_request_template.md`

- [ ] **Step 1: Replace the PR template**

Replace the entire contents of `.github/pull_request_template.md` with:

```markdown
> **Do not create PRs manually.**
> Use [`st-submit-pr`](https://wphillipmoore.github.io/standard-tooling/reference/dev/submit-pr/).
```

- [ ] **Step 2: Commit**

```
refactor(templates): replace PR template with redirect stub

st-submit-pr is the required path for creating PRs. The template
now exists only to redirect anyone who opens the GitHub UI.

Ref #650
```

---

### Task 4: Replace issue template with 3-field form

**Files:**
- Modify: `.github/ISSUE_TEMPLATE/issue.yml`

- [ ] **Step 1: Replace the issue template**

Replace the entire contents of `.github/ISSUE_TEMPLATE/issue.yml` with:

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

- [ ] **Step 2: Commit**

```
refactor(templates): redesign issue template as 3-field form

Trimmed from 7 fields to 3 (issue type, problem/goal, acceptance
criteria). Standardized the type dropdown to 6 canonical types:
feature, bug, documentation, refactor, chore, research. Removed
stale fields that duplicated each other or belonged in the PR.

Ref #650
```

---

### Task 5: Fix `st-submit-pr` documentation

**Files:**
- Modify: `docs/site/docs/reference/dev/submit-pr.md`

- [ ] **Step 1: Rewrite the documentation**

Replace the entire contents of `docs/site/docs/reference/dev/submit-pr.md` with:

```markdown
# st-submit-pr

**Installed as:** `st-submit-pr` (Python console script)

**Source:** `src/standard_tooling/bin/st_submit_pr.py`

Wrapper that creates standards-compliant pull requests with
proper issue linkage.

!!! warning "Required for AI agents"
    AI agents **must** use this tool instead of raw
    `gh pr create`. The tool constructs the PR body from
    CLI arguments.

## Prerequisites

When running inside a dev container, `GH_TOKEN` must be set so `gh` can
authenticate. The
[Getting Started prerequisites](../../getting-started.md#prerequisites)
cover `gh auth login` and how `GH_TOKEN` flows through to the
container.

## Usage

```bash
st-submit-pr \
  --issue NUMBER --summary TEXT --title TEXT [options]
```

## Arguments

| Argument | Required | Description |
| -------- | -------- | ----------- |
| `--issue` | Yes | Issue number or cross-repo ref |
| `--summary` | Yes | One-line PR summary |
| `--title` | Yes | PR title |
| `--linkage` | No | Linkage keyword (default: `Ref`) |
| `--notes` | No | Additional notes for the PR |
| `--dry-run` | No | Print PR body without executing |

### Linkage Keywords

`Ref`

## Examples

```bash
# Standard PR
st-submit-pr \
  --issue 42 \
  --summary "Add new lint check for X" \
  --title "feat(lint): add new check for X"

# Dry run to preview
st-submit-pr \
  --issue 42 \
  --summary "Fix regex bug" \
  --title "fix(regex): handle edge case" \
  --dry-run
```

## Behavior

1. Validates arguments and issue reference format.
2. Detects target branch from the current branch:
    - `release/*` branches target `main`
    - All other branches target `develop`
3. Pushes the branch to origin.
4. Creates the PR via `gh pr create`.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| 0 | PR created |
| 1 | Validation failure |
```

- [ ] **Step 2: Commit**

```
docs(st-submit-pr): fix stale documentation

Corrected source path, removed nonexistent auto-merge references,
fixed default linkage to Ref (only allowed value), marked --title
as required, removed Testing section references, and updated
examples to include --title.

Ref #650
```

---

### Task 6: Update `pr-workflow` skill

**Files:**
- Modify: `/Users/pmoore/dev/github/standard-tooling-plugin/skills/pr-workflow/SKILL.md`

This file lives in the `standard-tooling-plugin` repo, not the main worktree. The changes target two specific sections.

- [ ] **Step 1: Remove the template population preflight bullet**

Replace lines 87-88:

```markdown
- Locate the pull-request template at
  `.github/pull_request_template.md` if present; use its fields.
```

with nothing (delete these two lines entirely).

- [ ] **Step 2: Replace the "Populate the PR template fields" pre-submission step**

Replace lines 104-115 (the block starting with "4. Populate the PR template fields. Required:"):

```markdown
4. Populate the PR template fields. Required:
   - Issue linkage using `Ref #N`. **Do not use `Fixes`, `Closes`,
     or `Resolves`** — those keywords auto-close the issue on
     merge, bypassing finalization. Using `Ref` instead defers the
     *timing* of closure, not the *responsibility*: if this PR
     resolves the issue, the agent must close it explicitly after
     finalization (see [Close the issue](#close-the-issue)).
     Enforcement is mechanical: `st-commit` rejects auto-close
     keywords in commit bodies, and the `st-pr-issue-linkage` CI
     check rejects them in PR bodies. The plugin's
     `block-autoclose-linkage` PreToolUse hook adds a further
     guard at the agent tool-call layer.
```

with:

```markdown
4. Prepare `st-submit-pr` arguments. The tool constructs the
   entire PR body — there is no template to populate. Required:
   - `--issue <N>` — the issue this PR addresses.
   - `--summary "<one-line>"` — what this PR does.
   - `--title "<conventional-commit-style title>"` — the PR title.
   - `--linkage Ref` — **always `Ref`**. Do not use `Fixes`,
     `Closes`, or `Resolves` — those keywords auto-close the
     issue on merge, bypassing finalization. Using `Ref` instead
     defers the *timing* of closure, not the *responsibility*:
     if this PR resolves the issue, the agent must close it
     explicitly after finalization (see
     [Close the issue](#close-the-issue)). Enforcement is
     mechanical: `st-commit` rejects auto-close keywords in
     commit bodies, and the `st-pr-issue-linkage` CI check
     rejects them in PR bodies. The plugin's
     `block-autoclose-linkage` PreToolUse hook adds a further
     guard at the agent tool-call layer.
   - `--notes "<text>"` (optional) — additional context.
```

- [ ] **Step 3: Commit (in the standard-tooling-plugin repo)**

This commit happens in the `standard-tooling-plugin` repo, not the standard-tooling worktree. Use `st-commit` from that repo's checkout.

```
refactor(pr-workflow): align skill with template redesign

The PR template is now a stub — st-submit-pr constructs the entire
body from CLI arguments. Removed guidance telling agents to locate
and populate the template. Replaced with explicit st-submit-pr
argument preparation.

Ref wphillipmoore/standard-tooling#650
```

---

### Task 7: Run full validation

- [ ] **Step 1: Run st-validate in the worktree**

Run: `cd /Users/pmoore/dev/github/standard-tooling/.worktrees/issue-650-template-redesign && st-docker-run -- uv run st-validate`

Expected: All checks pass.

- [ ] **Step 2: Fix any failures and re-run until clean**

If any check fails, fix the issue and re-run. Commit each fix separately.

---

### Task 8: File `st-create-issue` design issue

**Files:** None (GitHub issue only)

- [ ] **Step 1: Create the issue via `gh`**

```bash
gh issue create \
  --repo wphillipmoore/standard-tooling \
  --title "feat: add st-create-issue CLI tool" \
  --body-file <tmpfile>
```

Issue body content:

```markdown
## Problem / Goal

Issues are created ad-hoc by AI agents (via `gh issue create`) and
by humans (via the GitHub UI form). The UI form enforces structure
via `issue.yml`, but the CLI path has no guardrails — agents can
create issues with arbitrary body formats, missing fields, or
inconsistent type labels.

`st-create-issue` would be the issue-creation counterpart to
`st-submit-pr`: a CLI tool that enforces the same structure as
the issue template, ensuring consistency regardless of creation
path.

## Design Sketch

- Accept `--type`, `--title`, `--problem`, `--acceptance` arguments.
- `--type` enforces the canonical 6 issue types: `feature`, `bug`,
  `documentation`, `refactor`, `chore`, `research`.
- Construct a structured issue body matching the `issue.yml` form
  layout.
- Create the issue via `gh issue create`.
- Optionally apply a GitHub label matching the issue type.

## Acceptance Criteria

- Design is brainstormed and approved via the brainstorming skill.
- Implementation plan exists.
- Tool is implemented, tested, and documented.
- `pr-workflow` skill and agent workflows updated to use the tool.
```

- [ ] **Step 2: Note the issue number for the commit message**

---

### Task 9: Archive `mq-rest-admin-template`

- [ ] **Step 1: Archive the repo**

```bash
gh repo archive wphillipmoore/mq-rest-admin-template --yes
```

Expected: Repository archived successfully.

---

### Task 10: Fleet rollout — push templates to all consuming repos

**Files:** Creates or replaces in each of 22 repos:
- `.github/pull_request_template.md`
- `.github/ISSUE_TEMPLATE/issue.yml`
- `.github/ISSUE_TEMPLATE/config.yml` (only if missing)

The rollout is mechanical — the same two files go to every repo. Use the GitHub API to commit directly to each repo's default branch.

- [ ] **Step 1: Create the canonical template files locally**

Create two local files to use as the source content:

`/tmp/fleet-pr-template.md`:
```markdown
> **Do not create PRs manually.**
> Use [`st-submit-pr`](https://wphillipmoore.github.io/standard-tooling/reference/dev/submit-pr/).
```

`/tmp/fleet-issue-template.yml`:
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

`/tmp/fleet-config.yml`:
```yaml
blank_issues_enabled: false
```

- [ ] **Step 2: Roll out to each repo**

For each of the 21 non-`standard-tooling` repos (standard-tooling is updated via its own worktree), use `gh api` to commit the files. The repos are:

1. `standard-tooling-docker`
2. `standard-actions`
3. `the-infrastructure-mindset`
4. `mq-rest-admin-common`
5. `ai-research-methodology`
6. `mq-rest-admin-dev-environment`
7. `mq-rest-admin-rust`
8. `standard-tooling-plugin`
9. `mq-rest-admin-java`
10. `mq-rest-admin-ruby`
11. `mq-rest-admin-go`
12. `mq-rest-admin-python`
13. `career-strategy`
14. `home-equity-project`
15. `mnemosys-operations`
16. `mnemosys-core`
17. `renegade-dotfiles`
18. `paad`
19. `mempalace`
20. `lunatick-racing`
21. `cognition`

For each repo:

a. Check if archived: `gh api repos/wphillipmoore/<repo> --jq '.archived'` — skip if `true`.

b. Get the default branch SHA: `gh api repos/wphillipmoore/<repo>/git/refs/heads/<default-branch> --jq '.object.sha'`

c. Get the current tree SHA: `gh api repos/wphillipmoore/<repo>/git/commits/<commit-sha> --jq '.tree.sha'`

d. Create a new tree with the three files using the GitHub Trees API (`POST repos/{owner}/{repo}/git/trees`), basing on the current tree.

e. Create a commit with message:
```
refactor(templates): align PR and issue templates with standard-tooling

Replace PR template with redirect stub (st-submit-pr is the
required path). Redesign issue template as 3-field form with
standardized type dropdown.

Ref wphillipmoore/standard-tooling#650
```

f. Update the ref to point to the new commit.

g. Verify the commit landed: `gh api repos/wphillipmoore/<repo>/commits/<default-branch> --jq '.sha'`

- [ ] **Step 3: Verify no repo uses auto-close keywords**

After rollout, spot-check a few repos:

```bash
for repo in standard-actions mq-rest-admin-java mq-rest-admin-go mnemosys-core; do
  content=$(gh api "repos/wphillipmoore/$repo/contents/.github/pull_request_template.md" --jq '.content' | base64 -d)
  echo "=== $repo ==="
  echo "$content"
  echo "$content" | grep -i 'Fixes\|Closes\|Resolves' && echo "  FAIL" || echo "  OK"
done
```

Expected: All repos show the stub template. No auto-close keywords.

- [ ] **Step 4: Verify issue templates**

```bash
for repo in standard-actions mq-rest-admin-java mq-rest-admin-go mnemosys-core; do
  content=$(gh api "repos/wphillipmoore/$repo/contents/.github/ISSUE_TEMPLATE/issue.yml" --jq '.content' | base64 -d)
  echo "=== $repo ==="
  echo "$content" | head -5
  echo "$content" | grep -c 'issue_type\|problem\|acceptance'
done
```

Expected: Each repo shows the new 3-field form with 3 matching field IDs.
