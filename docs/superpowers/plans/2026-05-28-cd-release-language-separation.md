# CD Release Language Separation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate the `language` and `container-suffix` concerns in `cd-release.yml`, making `language` optional and eliminating the `language: base` lie from non-language projects.

**Architecture:** The `vrg-release-validate-inputs` CLI changes from a required positional `language` argument to an optional `--language` flag. When no language is provided, all ecosystem validation is skipped. The `cd-release.yml` workflow gains a `container-suffix` input (matching the CI workflow pattern) and derives the container image from `container-suffix || language || 'base'`. Consuming repos stop passing `language: base` and instead pass `container-suffix: base` or rely on defaults.

**Tech Stack:** Python 3.12+, pytest, GitHub Actions YAML

**Spec:** `docs/specs/2026-05-28-cd-release-language-separation-design.md`
**Issue:** #1272

---

## File Map

| File | Repo | Status | Responsibility |
|------|------|--------|----------------|
| `src/vergil_tooling/bin/vrg_release_validate_inputs.py` | vergil-tooling | Modify | Remove `_NON_LANGUAGE_TYPES`, change `language` to optional `--language` flag |
| `tests/vergil_tooling/test_vrg_release_validate_inputs.py` | vergil-tooling | Modify | Replace base tests with no-language tests, update all tests to `--language` flag syntax |
| `.github/workflows/cd-release.yml` | vergil-actions | Modify | Add `container-suffix` input, make `language` default empty, fix container image line |
| `actions/cd/release/validate-inputs/action.yml` | vergil-actions | Modify | Make `language` optional, conditionally pass `--language` flag |
| `.github/workflows/cd.yml` | vergil-docker | Modify | Replace `language: base` with `container-suffix: base` |
| `.github/workflows/cd.yml` | vergil-vm | Modify | Replace `language: base` with `container-suffix: base` |

vergil-tooling and vergil-claude-plugin `cd.yml` files need no changes.

---

## Task 1: Update `vrg-release-validate-inputs` CLI and tests (vergil-tooling)

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_release_validate_inputs.py`
- Modify: `tests/vergil_tooling/test_vrg_release_validate_inputs.py`

### Step 1.1: Write failing tests for no-language case

- [ ] Replace the test file content. Remove the three `test_base_*` tests and `test_no_args_fails`. Add no-language tests. Update all existing tests to use `--language` flag syntax.

Write this complete test file to `tests/vergil_tooling/test_vrg_release_validate_inputs.py`:

```python
"""Tests for vrg-release-validate-inputs CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import pytest

from vergil_tooling.bin.vrg_release_validate_inputs import main


def test_valid_python_release(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "python"])
    assert rc == 0


def test_valid_python_with_registry_publish(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "python", "--registry-publish"])
    assert rc == 0


def test_unsupported_language_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "unknown"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "unsupported" in captured.err.lower() or "unsupported" in captured.out.lower()


def test_go_with_registry_publish_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "go", "--registry-publish"])
    assert rc == 1


def test_container_tag_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "go", "--container-tag", "v1.0.0"])
    assert rc == 0


def test_reports_all_failures(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--language", "unknown", "--registry-publish"])
    assert rc == 1


def test_container_tag_unsupported_language_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=False),
        patch(
            "vergil_tooling.bin.vrg_release_validate_inputs._CONTAINER_LANGUAGES",
            frozenset(),
        ),
    ):
        rc = main(["--language", "python", "--container-tag", "v1.0.0"])
    assert rc == 1


def test_no_language_passes(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main([])
    assert rc == 0


def test_no_language_ignores_registry_publish(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--registry-publish"])
    assert rc == 0


def test_no_language_ignores_container_tag(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["--container-tag", "latest"])
    assert rc == 0
```

### Step 1.2: Run tests to verify they fail

- [ ] Run from the worktree:

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1272-language-separation && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_release_validate_inputs.py -v
```

Expected: failures on the new no-language tests (because `language` is still a required positional arg) and on updated tests (because they pass `--language` as a flag but the parser expects a positional).

### Step 1.3: Implement the CLI changes

- [ ] Write this complete file to `src/vergil_tooling/bin/vrg_release_validate_inputs.py`:

```python
"""Validate release workflow inputs.

Checks that the language is supported and that flag combinations
(container-tag, registry-publish) are compatible with the language.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.languages import ecosystem_metadata, supported_languages
from vergil_tooling.lib.output import emit_error

_CONTAINER_LANGUAGES = frozenset({"python", "java", "ruby", "rust", "go"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-release-validate-inputs",
        description="Validate release workflow inputs.",
    )
    parser.add_argument(
        "--language",
        default="",
        help="Programming language (go, java, python, ruby, rust). "
        "Omit for non-language projects.",
    )
    parser.add_argument(
        "--container-tag",
        default="",
        help="Container image tag (empty = no container publish)",
    )
    parser.add_argument(
        "--registry-publish",
        action="store_true",
        help="Whether to publish to a package registry",
    )
    args = parser.parse_args(argv)

    if not args.language:
        return 0

    errors: list[str] = []
    langs = supported_languages()

    if args.language not in langs:
        errors.append(
            f"unsupported language: {args.language} (supported: {', '.join(sorted(langs))})"
        )
    else:
        info = ecosystem_metadata(args.language)
        if args.registry_publish and info.publish_cmd is None:
            errors.append(
                f"--registry-publish is not supported for {args.language} "
                f"(no publish command defined)"
            )
        if args.container_tag and args.language not in _CONTAINER_LANGUAGES:
            errors.append(f"--container-tag is not supported for {args.language}")

    if errors:
        for msg in errors:
            emit_error(msg)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

### Step 1.4: Run tests to verify they pass

- [ ] Run from the worktree:

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1272-language-separation && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_release_validate_inputs.py -v
```

Expected: all 10 tests pass.

### Step 1.5: Run full validation

- [ ] Run from the worktree:

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1272-language-separation && vrg-container-run -- uv run vrg-validate
```

Expected: all checks pass (lint, typecheck, test, audit, common).

### Step 1.6: Commit

- [ ] Commit the vergil-tooling changes:

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1272-language-separation && vrg-git add src/vergil_tooling/bin/vrg_release_validate_inputs.py tests/vergil_tooling/test_vrg_release_validate_inputs.py && vrg-git commit -m "fix(release): make language optional in vrg-release-validate-inputs

Remove _NON_LANGUAGE_TYPES hack and change language from a required
positional argument to an optional --language flag. When no language
is provided, all ecosystem validation is skipped and the CLI returns
success. This supports non-language projects that have no programming
language ecosystem.

Ref #1272"
```

---

## Task 2: Add design spec to the commit

**Files:**
- Existing: `docs/specs/2026-05-28-cd-release-language-separation-design.md`

### Step 2.1: Stage and commit the spec

- [ ] The spec was written during brainstorming. Include it in the branch:

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-1272-language-separation && vrg-git add docs/specs/2026-05-28-cd-release-language-separation-design.md && vrg-git commit -m "docs: add design spec for cd-release language separation

Ref #1272"
```

---

## Task 3: Update `cd-release.yml` and validate-inputs action (vergil-actions)

> **Note:** This task modifies files in `/Users/pmoore/dev/projects/vergil-project/vergil-actions/`. It requires its own branch and PR in that repository.

**Files:**
- Modify: `/Users/pmoore/dev/projects/vergil-project/vergil-actions/.github/workflows/cd-release.yml`
- Modify: `/Users/pmoore/dev/projects/vergil-project/vergil-actions/actions/cd/release/validate-inputs/action.yml`

### Step 3.1: Update `cd-release.yml` inputs and container line

- [ ] In `.github/workflows/cd-release.yml`, make these changes:

**Replace the `language` input (lines 6-9):**

```yaml
# Before:
      language:
        description: Primary language (matches container image suffix).
        type: string
        default: "base"

# After:
      language:
        description: >-
          Primary programming language (one of: go, java, python, ruby,
          rust). Leave empty for non-language projects.
        type: string
        default: ""
```

**Add `container-suffix` input after `language` (insert after the `language` block, before `container-tag`):**

```yaml
      container-suffix:
        description: >-
          Container image name suffix (e.g. python, go, base). Defaults
          to the language input when set, "base" otherwise.
        type: string
        default: ""
```

**Replace the container image line (line 64):**

```yaml
# Before:
    container: ghcr.io/vergil-project/${{ inputs.container-prefix || 'prod' }}-${{ inputs.language }}:${{ inputs.container-tag }}

# After:
    container: ghcr.io/vergil-project/${{ inputs.container-prefix || 'prod' }}-${{ inputs.container-suffix || inputs.language || 'base' }}:${{ inputs.container-tag }}
```

No other changes to `cd-release.yml`. The "Build and publish" step (line 119) already gates on the five real languages.

### Step 3.2: Update the validate-inputs action

- [ ] Replace the full content of `actions/cd/release/validate-inputs/action.yml`:

```yaml
name: Validate publish inputs
description: >-
  Pre-flight validation for cd-release inputs. Fails early on invalid
  input combinations.

inputs:
  language:
    description: Primary language string.
    required: false
    default: ""
  container-tag:
    description: Dev container image tag.
    required: true
  registry-publish:
    description: Whether publishing is enabled.
    required: true

runs:
  using: composite
  steps:
    - name: Validate inputs
      shell: bash
      env:
        INPUT_LANGUAGE: ${{ inputs.language }}
        INPUT_CONTAINER_TAG: ${{ inputs.container-tag }}
        INPUT_REGISTRY_PUBLISH: ${{ inputs.registry-publish }}
      run: |
        args=()

        if [ -n "$INPUT_LANGUAGE" ]; then
          args+=(--language "$INPUT_LANGUAGE")
        fi

        if [ -n "$INPUT_CONTAINER_TAG" ]; then
          args+=(--container-tag "$INPUT_CONTAINER_TAG")
        fi

        if [ "$INPUT_REGISTRY_PUBLISH" = "true" ]; then
          args+=(--registry-publish)
        fi

        vrg-release-validate-inputs "${args[@]}"
```

### Step 3.3: Commit vergil-actions changes

- [ ] Create a branch and commit in vergil-actions:

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-actions && vrg-git checkout -b feature/1272-language-separation && vrg-git add .github/workflows/cd-release.yml actions/cd/release/validate-inputs/action.yml && vrg-git commit -m "fix(release): separate language from container-suffix in cd-release

Add container-suffix input to cd-release.yml, matching the pattern
already used by CI workflows. Make language optional with empty default.
The container image is now derived from container-suffix || language ||
'base'. Update validate-inputs action to pass --language as a flag
only when non-empty.

Ref vergil-project/vergil-tooling#1272"
```

---

## Task 4: Update consuming repos (vergil-docker, vergil-vm)

> **Note:** These are one-line changes in each repo. They should land after vergil-actions tags a release that includes Task 3.

**Files:**
- Modify: `/Users/pmoore/dev/projects/vergil-project/vergil-docker/.github/workflows/cd.yml`
- Modify: `/Users/pmoore/dev/projects/vergil-project/vergil-vm/.github/workflows/cd.yml`

### Step 4.1: Update vergil-docker `cd.yml`

- [ ] In `/Users/pmoore/dev/projects/vergil-project/vergil-docker/.github/workflows/cd.yml`, replace the release job's `with` block (lines 26-28):

```yaml
# Before:
    with:
      language: base
      container-tag: latest

# After:
    with:
      container-suffix: base
```

The `container-tag` defaults to `"latest"` in `cd-release.yml`, so it can be omitted.

### Step 4.2: Update vergil-vm `cd.yml`

- [ ] In `/Users/pmoore/dev/projects/vergil-project/vergil-vm/.github/workflows/cd.yml`, replace the release job's `with` block (lines 23-25):

```yaml
# Before:
    with:
      language: base
      container-tag: latest

# After:
    with:
      container-suffix: base
```

### Step 4.3: Commit consuming repo changes

- [ ] Each repo needs its own branch and commit. These land after vergil-actions tags a new release.

vergil-docker:
```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-docker && vrg-git checkout -b feature/1272-language-separation && vrg-git add .github/workflows/cd.yml && vrg-git commit -m "fix(cd): use container-suffix instead of language for base image

Stop passing language: base to cd-release.yml. Non-language projects
use container-suffix to select the container image without lying about
having a programming language.

Ref vergil-project/vergil-tooling#1272"
```

vergil-vm:
```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-vm && vrg-git checkout -b feature/1272-language-separation && vrg-git add .github/workflows/cd.yml && vrg-git commit -m "fix(cd): use container-suffix instead of language for base image

Stop passing language: base to cd-release.yml. Non-language projects
use container-suffix to select the container image without lying about
having a programming language.

Ref vergil-project/vergil-tooling#1272"
```

---

## Rollout Order

1. **vergil-tooling** (Tasks 1-2): Merge PR, release a new version.
2. **vergil-actions** (Task 3): Merge PR, tag new minor version. Update `vergil.toml` dependency to the new vergil-tooling version.
3. **vergil-docker, vergil-vm** (Task 4): Merge PRs. Update `vergil.toml` dependency and vergil-actions tag reference.
4. **vergil-claude-plugin**: No code changes needed — its `cd.yml` already omits `language`, and the new defaults resolve correctly to `prod-base:latest`.
