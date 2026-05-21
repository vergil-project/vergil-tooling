# Commit Type Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten git-cliff commit parser regexes for precision, fix `doc`→`docs`, add `build`/`revert` types, and add `revert` to st-commit's `ALLOWED_TYPES`.

**Architecture:** Four files modified — two cliff TOML configs (identical parser changes), one Python source file (one-line tuple change), one Markdown doc (one-line type list update). No new files, no new dependencies.

**Tech Stack:** TOML (git-cliff config), Python, Markdown

**Spec:** `docs/specs/2026-05-08-commitlint-and-cliff-cleanup-design.md`

---

### Task 1: Update cliff.toml commit parsers

**Files:**
- Modify: `src/standard_tooling/configs/cliff.toml:30-41`

- [ ] **Step 1: Update the commit_parsers block**

Replace the `commit_parsers` array (lines 30–41) with precision regexes. Every type pattern gets `[(:!]` appended. `^doc` becomes `^docs[(:!]`. New entries for `build` and `revert` are added. The chore skip rules that use literal strings (e.g., `^chore: bump version to`) are already precise and stay unchanged.

```toml
commit_parsers = [
  { message = "^feat[(:!]", group = "Features" },
  { message = "^fix[(:!]", group = "Bug fixes" },
  { message = "^docs[(:!]", group = "Documentation" },
  { message = "^perf[(:!]", group = "Performance" },
  { message = "^refactor[(:!]", group = "Refactoring" },
  { message = "^style[(:!]", group = "Styling" },
  { message = "^test[(:!]", group = "Testing" },
  { message = "^build[(:!]", group = "Build" },
  { message = "^ci[(:!]", group = "CI" },
  { message = "^revert[(:!]", group = "Reverts" },
  { message = "^chore\\(release\\):", skip = true },
  { message = "^chore: bump version to", skip = true },
  { message = "^chore\\(version\\):", skip = true },
  { message = "^chore: prepare release", skip = true },
  { message = "^chore: merge .* into release/", skip = true },
  { message = "^chore[(:!]", group = "Chores" },
]
```

- [ ] **Step 2: Verify the file looks correct**

Run: `cat src/standard_tooling/configs/cliff.toml`

Expected: the `commit_parsers` block matches step 1 exactly, and the rest of the file is unchanged.

---

### Task 2: Update cliff-release-notes.toml commit parsers

**Files:**
- Modify: `src/standard_tooling/configs/cliff-release-notes.toml:24-35`

- [ ] **Step 1: Update the commit_parsers block**

Replace the `commit_parsers` array (lines 24–35) with the same precision regexes. This file has fewer chore skip rules — only `^chore(release):`.

```toml
commit_parsers = [
  { message = "^feat[(:!]", group = "Features" },
  { message = "^fix[(:!]", group = "Bug fixes" },
  { message = "^docs[(:!]", group = "Documentation" },
  { message = "^perf[(:!]", group = "Performance" },
  { message = "^refactor[(:!]", group = "Refactoring" },
  { message = "^style[(:!]", group = "Styling" },
  { message = "^test[(:!]", group = "Testing" },
  { message = "^build[(:!]", group = "Build" },
  { message = "^ci[(:!]", group = "CI" },
  { message = "^revert[(:!]", group = "Reverts" },
  { message = "^chore\\(release\\):", skip = true },
  { message = "^chore[(:!]", group = "Chores" },
]
```

- [ ] **Step 2: Verify the file looks correct**

Run: `cat src/standard_tooling/configs/cliff-release-notes.toml`

Expected: the `commit_parsers` block matches step 1 exactly, and the rest of the file is unchanged.

---

### Task 3: Add `revert` to st-commit ALLOWED_TYPES

**Files:**
- Modify: `src/standard_tooling/bin/st_commit.py:21`
- Test: `tests/standard_tooling/test_commit.py`

- [ ] **Step 1: Write a test that `revert` is accepted as a commit type**

Add a test to `tests/standard_tooling/test_commit.py` that parses args with `--type revert` and asserts success. Place it after the existing `test_parse_args_with_scope_and_body` test (after line 103).

```python
def test_parse_args_revert_type() -> None:
    args = parse_args(
        ["--type", "revert", "--scope", "auth", "--message", "undo token change", "--agent", "claude"]
    )
    assert args.commit_type == "revert"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_commit.py::test_parse_args_revert_type -v`

Expected: FAIL — `argparse` rejects `revert` because it's not in the `choices` tuple.

- [ ] **Step 3: Add `revert` to `ALLOWED_TYPES`**

In `src/standard_tooling/bin/st_commit.py` line 21, change:

```python
ALLOWED_TYPES = ("feat", "fix", "docs", "style", "refactor", "test", "chore", "ci", "build")
```

to:

```python
ALLOWED_TYPES = ("feat", "fix", "docs", "style", "refactor", "test", "chore", "ci", "build", "revert")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_commit.py::test_parse_args_revert_type -v`

Expected: PASS

- [ ] **Step 5: Run the full commit test suite**

Run: `st-docker-run -- uv run pytest tests/standard_tooling/test_commit.py -v`

Expected: all tests pass (the existing `test_parse_args_invalid_type` test uses `"invalid"` which is still rejected).

---

### Task 4: Update documentation

**Files:**
- Modify: `docs/repository-standards.md:45`

- [ ] **Step 1: Update the type list**

Change line 45 from:

```markdown
- `--type` (required): `feat|fix|docs|style|refactor|test|chore|ci|build`
```

to:

```markdown
- `--type` (required): `feat|fix|docs|style|refactor|test|chore|ci|build|revert`
```

---

### Task 5: Run full validation and commit

- [ ] **Step 1: Run st-validate**

Run: `st-docker-run -- uv run st-validate`

Expected: all checks pass.

- [ ] **Step 2: Commit all changes**

```bash
git add \
  src/standard_tooling/configs/cliff.toml \
  src/standard_tooling/configs/cliff-release-notes.toml \
  src/standard_tooling/bin/st_commit.py \
  tests/standard_tooling/test_commit.py \
  docs/repository-standards.md \
  docs/specs/2026-05-08-commitlint-and-cliff-cleanup-design.md \
  docs/plans/2026-05-08-commit-type-cleanup.md
st-commit \
  --type fix --scope commit \
  --message "tighten cliff regexes, fix doc to docs, add build/revert types" \
  --body "Ref #598" \
  --agent claude
```
