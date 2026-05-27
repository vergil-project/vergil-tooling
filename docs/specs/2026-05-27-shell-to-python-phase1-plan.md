# Shell-to-Python Migration: Phase 1 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shared output module, unify the language metadata registry with ecosystem data, clean up the primary-language enum, and deliver the `vrg-ecosystem-resolve` and `vrg-release-validate-inputs` CLIs.

**Architecture:** A new `lib/output.py` provides TTY-aware formatting for all utilities. The existing `lib/validate_commands.py` is refactored into `lib/languages.py`, which combines validation commands with ecosystem metadata (build/publish/credential) in a single `Language` dataclass. The `primary-language` config enum is tightened to five real languages; `shell`, `none`, and `claude-plugin` are removed. Two new CLI entry points expose the ecosystem data and release input validation.

**Tech Stack:** Python 3.12+, argparse, dataclasses, `sys.stdout.isatty()`, `tomllib`

**Design spec:** `docs/specs/2026-05-27-shell-to-python-migration-design.md`
**Umbrella issue:** #1192
**Phase 1 issues:** #1184, #1185

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `src/vergil_tooling/lib/output.py` | TTY-aware CI/interactive output formatting |
| Create | `tests/vergil_tooling/test_output.py` | Tests for output module |
| Create | `src/vergil_tooling/lib/languages.py` | Unified language metadata registry |
| Rename | `tests/vergil_tooling/test_validate_commands.py` → `test_languages.py` | Tests for language registry |
| Delete | `src/vergil_tooling/lib/validate_commands.py` | Replaced by languages.py |
| Create | `src/vergil_tooling/bin/vrg_ecosystem_resolve.py` | CLI: ecosystem metadata lookup |
| Create | `tests/vergil_tooling/test_vrg_ecosystem_resolve.py` | Tests for ecosystem resolve CLI |
| Create | `src/vergil_tooling/bin/vrg_release_validate_inputs.py` | CLI: release input validation |
| Create | `tests/vergil_tooling/test_vrg_release_validate_inputs.py` | Tests for release validate CLI |
| Modify | `src/vergil_tooling/lib/config.py` | Make primary-language optional, remove shell/none/claude-plugin |
| Modify | `src/vergil_tooling/lib/version.py` | Remove claude-plugin version handling |
| Modify | `src/vergil_tooling/lib/repo_init.py` | Update container suffix map |
| Modify | `src/vergil_tooling/lib/github_config.py` | Handle optional primary-language |
| Modify | `src/vergil_tooling/bin/vrg_validate.py` | Update imports |
| Modify | `src/vergil_tooling/lib/container_cache.py` | Update imports |
| Modify | `tests/vergil_tooling/test_vrg_validate.py` | Update imports |
| Modify | `tests/vergil_tooling/test_config.py` | Tests for optional primary-language |
| Modify | `tests/vergil_tooling/test_version.py` | Remove claude-plugin test cases |
| Modify | `pyproject.toml` | Add console_scripts entries |

---

### Task 1: Output Module — `lib/output.py`

**Files:**
- Create: `src/vergil_tooling/lib/output.py`
- Create: `tests/vergil_tooling/test_output.py`

- [ ] **Step 1: Write tests for the output module**

```python
"""Tests for vergil_tooling.lib.output."""

from __future__ import annotations

import os
from io import StringIO
from unittest.mock import patch

from vergil_tooling.lib.output import (
    emit_error,
    emit_warning,
    is_ci,
    write_output,
    write_summary,
)


def test_is_ci_returns_true_when_not_a_tty() -> None:
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        assert is_ci() is True


def test_is_ci_returns_false_when_tty() -> None:
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True
        assert is_ci() is False


def test_emit_error_ci_mode(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_error("something broke")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.err == "::error ::something broke\n"


def test_emit_error_ci_mode_with_file_and_line(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_error("bad value", file="src/main.py", line=42)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.err == "::error file=src/main.py,line=42::bad value\n"


def test_emit_error_interactive_mode(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        emit_error("something broke")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "something broke" in captured.err
    assert "::" not in captured.err


def test_emit_warning_ci_mode(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_warning("heads up")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.err == "::warning ::heads up\n"


def test_emit_warning_ci_mode_with_file(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_warning("check this", file="action.yml")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert captured.err == "::warning file=action.yml::check this\n"


def test_write_output_ci_mode(tmp_path: object) -> None:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        write_output("version", "1.2.3")
    assert output_file.read_text() == "version=1.2.3\n"


def test_write_output_ci_mode_appends(tmp_path: object) -> None:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    output_file = tmp_path / "github_output"
    output_file.write_text("existing=value\n")
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        write_output("new_key", "new_value")
    assert output_file.read_text() == "existing=value\nnew_key=new_value\n"


def test_write_output_ci_mode_missing_env_var(capsys: object) -> None:
    env = os.environ.copy()
    env.pop("GITHUB_OUTPUT", None)
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, env, clear=True),
    ):
        write_output("version", "1.2.3")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "version: 1.2.3" in captured.out


def test_write_output_interactive_mode(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        write_output("version", "1.2.3")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "version: 1.2.3" in captured.out


def test_write_summary_ci_mode(tmp_path: object) -> None:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    summary_file = tmp_path / "step_summary"
    summary_file.write_text("")
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}),
    ):
        write_summary("## Results\n\nAll clean.")
    assert summary_file.read_text() == "## Results\n\nAll clean.\n"


def test_write_summary_ci_mode_missing_env_var(capsys: object) -> None:
    env = os.environ.copy()
    env.pop("GITHUB_STEP_SUMMARY", None)
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, env, clear=True),
    ):
        write_summary("## Results")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "## Results" in captured.out


def test_write_summary_interactive_mode(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        write_summary("## Results")
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "## Results" in captured.out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_output.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.output'`

- [ ] **Step 3: Implement the output module**

```python
"""TTY-aware output formatting for CI and interactive use.

Detection: ``sys.stdout.isatty()``. When stdout is a TTY, output is
formatted for human reading. When not (CI, piped), output uses
GitHub Actions workflow commands.
"""

from __future__ import annotations

import os
import sys


def is_ci() -> bool:
    return not sys.stdout.isatty()


def emit_error(msg: str, *, file: str | None = None, line: int | None = None) -> None:
    if is_ci():
        params = _annotation_params(file=file, line=line)
        print(f"::error {params}::{msg}", file=sys.stderr)
    else:
        print(f"ERROR: {msg}", file=sys.stderr)


def emit_warning(msg: str, *, file: str | None = None, line: int | None = None) -> None:
    if is_ci():
        params = _annotation_params(file=file, line=line)
        print(f"::warning {params}::{msg}", file=sys.stderr)
    else:
        print(f"WARNING: {msg}", file=sys.stderr)


def write_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT") if is_ci() else None
    if output_path:
        with open(output_path, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        print(f"{key}: {value}")


def write_summary(markdown: str) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY") if is_ci() else None
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(markdown if markdown.endswith("\n") else markdown + "\n")
    else:
        print(markdown)


def _annotation_params(*, file: str | None, line: int | None) -> str:
    parts: list[str] = []
    if file is not None:
        parts.append(f"file={file}")
    if line is not None:
        parts.append(f"line={line}")
    return ",".join(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_output.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope output --message "add TTY-aware CI/interactive output module" --body "Ref #1192"
```

---

### Task 2: Language Metadata Module — `lib/languages.py`

**Files:**
- Create: `src/vergil_tooling/lib/languages.py`
- Rename: `tests/vergil_tooling/test_validate_commands.py` → `tests/vergil_tooling/test_languages.py`

- [ ] **Step 1: Copy existing test file and update imports**

Copy `tests/vergil_tooling/test_validate_commands.py` to `tests/vergil_tooling/test_languages.py`. Replace the import block:

```python
"""Tests for vergil_tooling.lib.languages."""

from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.languages import (
    CheckKind,
    language_commands,
)
```

Keep all existing test functions unchanged — they are the backward-compatibility suite.

- [ ] **Step 2: Add new API tests to `test_languages.py`**

Append to the file:

```python
from vergil_tooling.lib.languages import (
    EcosystemInfo,
    Language,
    ecosystem_metadata,
    supported_languages,
)


# -- New API ------------------------------------------------------------------


def test_supported_languages_returns_five() -> None:
    langs = supported_languages()
    assert langs == frozenset({"python", "go", "java", "ruby", "rust"})


def test_supported_languages_is_frozen() -> None:
    langs = supported_languages()
    assert isinstance(langs, frozenset)


def test_ecosystem_metadata_python() -> None:
    info = ecosystem_metadata("python")
    assert isinstance(info, EcosystemInfo)
    assert info.build_cmd is not None
    assert info.publish_cmd is not None
    assert info.credential_secret is not None


def test_ecosystem_metadata_go() -> None:
    info = ecosystem_metadata("go")
    assert isinstance(info, EcosystemInfo)
    assert info.credential_secret is None


def test_ecosystem_metadata_all_languages_have_entries() -> None:
    for lang in supported_languages():
        info = ecosystem_metadata(lang)
        assert isinstance(info, EcosystemInfo), f"Missing ecosystem for {lang}"


def test_ecosystem_metadata_unknown_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unsupported"):
        ecosystem_metadata("unknown")


def test_ecosystem_metadata_shell_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unsupported"):
        ecosystem_metadata("shell")


def test_language_commands_still_works_for_unknown() -> None:
    cmds = language_commands("unknown", CheckKind.LINT)
    assert cmds == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_languages.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.languages'`

- [ ] **Step 4: Implement `lib/languages.py`**

```python
"""Unified per-language metadata registry.

Defines validation commands, build/publish commands, and credential
metadata for each supported language. This is the single source of
truth for all per-language data — update one place when adding a
language.

Replaces the former ``validate_commands`` module.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib.resources import files


class CheckKind(Enum):
    INSTALL = "install"
    LINT = "lint"
    TYPECHECK = "typecheck"
    TEST = "test"
    AUDIT = "audit"


@dataclass(frozen=True)
class EcosystemInfo:
    build_cmd: list[str] | None
    publish_cmd: list[str] | None
    credential_secret: str | None


@dataclass(frozen=True)
class Language:
    name: str
    checks: dict[CheckKind, list[list[str]]]
    ecosystem: EcosystemInfo


# -- License allowlists (unchanged from validate_commands) --------------------

_PIP_LICENSES_ALLOWLIST = ";".join(
    [
        "Apache-2.0",
        "Apache-2.0 AND CNRI-Python",
        "Apache-2.0 OR BSD-2-Clause",
        "Apache-2.0 OR BSD-3-Clause",
        "Apache Software License",
        "BSD License",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC License (ISCL)",
        "MIT",
        "MIT License",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "MPL-1.1 OR GPL-2.0-only OR LGPL-2.1-or-later",
        "PSF-2.0",
        "Python Software Foundation License",
    ]
)

_GO_LICENSES_ALLOWLIST = ",".join(
    [
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0",
        "ISC",
        "MIT",
        "MPL-2.0",
    ]
)

_MAVEN_LICENSES_ALLOWLIST = "|".join(
    [
        "Apache 2.0",
        "Apache-2.0",
        "The Apache License, Version 2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0-or-later",
        "ISC",
        "MIT License",
        "MPL-2.0",
    ]
)

# -- Registry -----------------------------------------------------------------
# Ecosystem build/publish commands use list[str] (subprocess argv).
# Credential secrets are the GitHub secret name the action reads.
# Values are derived from vergil-actions registry-publish action
# (vergil-project/vergil-actions#600).

_REGISTRY: dict[str, Language] = {
    "python": Language(
        name="python",
        checks={
            CheckKind.INSTALL: [["uv", "sync", "--frozen", "--group", "dev"]],
            CheckKind.LINT: [
                ["ruff", "check", "src/", "tests/"],
                ["ruff", "format", "--check", "src/", "tests/"],
            ],
            CheckKind.TYPECHECK: [["mypy", "src/"], ["ty", "check", "src", "tests"]],
            CheckKind.TEST: [
                ["pytest", "--cov=src", "--cov-branch", "--cov-fail-under=100"],
            ],
            CheckKind.AUDIT: [
                ["uv", "sync", "--check", "--frozen", "--group", "dev"],
                ["uv", "lock", "--check"],
                ["pip-audit"],
                ["pip-licenses", f"--allow-only={_PIP_LICENSES_ALLOWLIST}"],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["uv", "build"],
            publish_cmd=["uv", "publish"],
            credential_secret="PYPI_TOKEN",
        ),
    ),
    "go": Language(
        name="go",
        checks={
            CheckKind.INSTALL: [["go", "mod", "download"]],
            CheckKind.LINT: [
                ["golangci-lint", "run", "./..."],
                ["gocyclo", "-over", "15", "."],
            ],
            CheckKind.TYPECHECK: [["go", "vet", "./..."]],
            CheckKind.TEST: [
                ["go", "test", "-race", "-count=1", "-coverprofile=coverage.out", "./..."],
                ["go-test-coverage", "--config", ".testcoverage.yml"],
            ],
            CheckKind.AUDIT: [
                ["govulncheck", "./..."],
                [
                    "go-licenses",
                    "check",
                    "./...",
                    f"--allowed_licenses={_GO_LICENSES_ALLOWLIST}",
                ],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["go", "build", "./..."],
            publish_cmd=None,
            credential_secret=None,
        ),
    ),
    "java": Language(
        name="java",
        checks={
            CheckKind.INSTALL: [["./mvnw", "dependency:resolve", "-B"]],
            CheckKind.LINT: [["./mvnw", "spotless:check", "checkstyle:check", "-B"]],
            CheckKind.TYPECHECK: [["./mvnw", "compile", "-B"]],
            CheckKind.TEST: [["./mvnw", "verify", "-B"]],
            CheckKind.AUDIT: [
                ["./mvnw", "dependency:tree", "-B", "-q"],
                [
                    "./mvnw",
                    "org.codehaus.mojo:license-maven-plugin:add-third-party",
                    "-Dlicense.excludedScopes=test",
                    "-Dlicense.failIfWarning=true",
                    f"-Dlicense.includedLicenses={_MAVEN_LICENSES_ALLOWLIST}",
                    "-B",
                ],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["./mvnw", "package", "-B"],
            publish_cmd=["./mvnw", "deploy", "-B"],
            credential_secret="MAVEN_GPG_PASSPHRASE",
        ),
    ),
    "ruby": Language(
        name="ruby",
        checks={
            CheckKind.INSTALL: [["bundle", "install", "--jobs", "4"]],
            CheckKind.LINT: [["bundle", "exec", "rubocop"]],
            CheckKind.TYPECHECK: [["bundle", "exec", "steep", "check"]],
            CheckKind.TEST: [["bundle", "exec", "rake"]],
            CheckKind.AUDIT: [
                ["bundle", "exec", "bundle-audit", "check", "--update"],
                ["license_finder", "--decisions-file={configs}/ruby/license_finder.yml"],
            ],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["gem", "build"],
            publish_cmd=["gem", "push"],
            credential_secret="RUBYGEMS_API_KEY",
        ),
    ),
    "rust": Language(
        name="rust",
        checks={
            CheckKind.INSTALL: [["cargo", "fetch"]],
            CheckKind.LINT: [
                ["cargo", "fmt", "--all", "--", "--check"],
                ["cargo", "clippy", "--", "-D", "warnings"],
            ],
            CheckKind.TYPECHECK: [["cargo", "check"]],
            CheckKind.TEST: [["cargo", "llvm-cov", "--fail-under-lines", "100"]],
            CheckKind.AUDIT: [["cargo", "deny", "check"]],
        },
        ecosystem=EcosystemInfo(
            build_cmd=["cargo", "build", "--release"],
            publish_cmd=["cargo", "publish"],
            credential_secret="CRATES_IO_TOKEN",
        ),
    ),
}


def supported_languages() -> frozenset[str]:
    return frozenset(_REGISTRY)


def ecosystem_metadata(language: str) -> EcosystemInfo:
    entry = _REGISTRY.get(language)
    if entry is None:
        msg = f"unsupported language: {language}"
        raise ValueError(msg)
    return entry.ecosystem


def language_commands(language: str, kind: CheckKind) -> list[list[str]]:
    """Return the canonical commands for a language and check kind.

    Returns an empty list if the language is not in the registry or
    has no entry for the given check kind.

    Any argument containing ``{configs}`` is expanded to the resolved
    path of the ``vergil_tooling.configs`` package directory.
    """
    entry = _REGISTRY.get(language)
    if entry is None:
        return []
    configs_dir = str(files("vergil_tooling.configs"))
    return [
        [arg.replace("{configs}", configs_dir) for arg in cmd]
        for cmd in entry.checks.get(kind, [])
    ]
```

- [ ] **Step 5: Run the NEW test file to verify all tests pass**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_languages.py -v`
Expected: All tests PASS (including all backward-compatibility tests)

- [ ] **Step 6: Commit**

```
vrg-commit --type feat --scope languages --message "add unified language metadata registry with ecosystem data" --body "Ref #1192, #1184"
```

---

### Task 3: Migrate Callers from `validate_commands` to `languages`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_validate.py` (line 18)
- Modify: `src/vergil_tooling/lib/repo_init.py` (grep for `validate_commands`)
- Modify: `src/vergil_tooling/lib/github_config.py` (grep for `validate_commands`)
- Modify: `src/vergil_tooling/lib/container_cache.py` (line 1-10 area)
- Modify: `tests/vergil_tooling/test_vrg_validate.py` (line 24)
- Delete: `src/vergil_tooling/lib/validate_commands.py`
- Delete: `tests/vergil_tooling/test_validate_commands.py`

- [ ] **Step 1: Update production code imports**

In each file, replace:
```python
from vergil_tooling.lib.validate_commands import CheckKind, language_commands
```
with:
```python
from vergil_tooling.lib.languages import CheckKind, language_commands
```

Files to update (check each one — some use `from vergil_tooling.lib.validate_commands import` inside a function body or `TYPE_CHECKING` block):
- `src/vergil_tooling/bin/vrg_validate.py` — top-level import
- `src/vergil_tooling/lib/repo_init.py` — inside a function (lazy import)
- `src/vergil_tooling/lib/github_config.py` — inside a function (lazy import)
- `src/vergil_tooling/lib/container_cache.py` — top-level import

- [ ] **Step 2: Update test imports**

In `tests/vergil_tooling/test_vrg_validate.py`, replace:
```python
from vergil_tooling.lib.validate_commands import CheckKind
```
with:
```python
from vergil_tooling.lib.languages import CheckKind
```

- [ ] **Step 3: Delete the old module and old test file**

Delete `src/vergil_tooling/lib/validate_commands.py` and `tests/vergil_tooling/test_validate_commands.py`.

- [ ] **Step 4: Run the full test suite to verify nothing broke**

Run: `cd <worktree> && vrg-container-run -- uv run vrg-validate`
Expected: All checks pass. No import errors.

- [ ] **Step 5: Commit**

```
vrg-commit --type refactor --scope languages --message "migrate all callers from validate_commands to languages" --body "Ref #1192"
```

---

### Task 4: Config Cleanup — Make `primary-language` Optional

**Files:**
- Modify: `src/vergil_tooling/lib/config.py`
- Modify: `tests/vergil_tooling/test_config.py`

- [ ] **Step 1: Write tests for optional primary-language**

Add to `tests/vergil_tooling/test_config.py`:

```python
def test_config_without_primary_language(tmp_path: Path) -> None:
    """Repos with no toolchain can omit primary-language."""
    toml = tmp_path / "vergil.toml"
    toml.write_text(
        '[project]\n'
        'repository-type = "infrastructure"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        '\n'
        '[dependencies]\n'
        'vergil = "v2.0.60"\n'
        '\n'
        '[ci]\n'
        'versions = ["3.12"]\n'
    )
    cfg = read_config(tmp_path)
    assert cfg.project.primary_language is None


def test_config_rejects_shell_language(tmp_path: Path) -> None:
    toml = tmp_path / "vergil.toml"
    toml.write_text(
        '[project]\n'
        'repository-type = "tooling"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        'primary-language = "shell"\n'
        '\n'
        '[dependencies]\n'
        'vergil = "v2.0.60"\n'
        '\n'
        '[ci]\n'
        'versions = ["3.12"]\n'
    )
    import pytest
    with pytest.raises(ConfigError):
        read_config(tmp_path)


def test_config_rejects_none_language(tmp_path: Path) -> None:
    toml = tmp_path / "vergil.toml"
    toml.write_text(
        '[project]\n'
        'repository-type = "tooling"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        'primary-language = "none"\n'
        '\n'
        '[dependencies]\n'
        'vergil = "v2.0.60"\n'
        '\n'
        '[ci]\n'
        'versions = ["3.12"]\n'
    )
    import pytest
    with pytest.raises(ConfigError):
        read_config(tmp_path)


def test_config_rejects_claude_plugin_language(tmp_path: Path) -> None:
    toml = tmp_path / "vergil.toml"
    toml.write_text(
        '[project]\n'
        'repository-type = "tooling"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        'primary-language = "claude-plugin"\n'
        '\n'
        '[dependencies]\n'
        'vergil = "v2.0.60"\n'
        '\n'
        '[ci]\n'
        'versions = ["3.12"]\n'
    )
    import pytest
    with pytest.raises(ConfigError):
        read_config(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_config.py -v -k "primary_language or shell_language or none_language or claude_plugin"`
Expected: FAIL — the new tests fail (optional language not yet supported, rejected values still accepted)

- [ ] **Step 3: Update `config.py`**

In `config.py`, make these changes:

1. Update the enum to remove `shell`, `none`, `claude-plugin`:
```python
"primary-language": {"python", "go", "java", "ruby", "rust"},
```

2. Change `primary-language` from required to optional in `_PROJECT_FIELDS` handling. Remove it from the mandatory check loop and handle it separately:

In `_parse_raw_config`, replace the existing `primary-language` validation with logic that:
- Allows the field to be absent or empty string
- If present and non-empty, validates against the 5-language enum
- Sets `primary_language` to `None` when absent or empty

3. Change `ProjectConfig.primary_language` type from `str` to `str | None`.

4. Update existing tests that construct configs with `shell`, `none`, or `claude-plugin` to use one of the five valid languages or omit the field.

- [ ] **Step 4: Run config tests**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_config.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope config --message "make primary-language optional, restrict to five real languages" --body "Remove shell, none, and claude-plugin from the primary-language enum.
Repos without a toolchain omit the field (primary_language becomes None).

Ref #1192"
```

---

### Task 5: Version Module Cleanup

**Files:**
- Modify: `src/vergil_tooling/lib/version.py`
- Modify: `tests/vergil_tooling/test_version.py`

- [ ] **Step 1: Update `version.py`**

1. Remove `"shell": "VERSION"` and `"none": "VERSION"` from `_DEFAULT_VERSION_FILES` (these languages no longer exist).
2. Remove `"claude-plugin": ".claude-plugin/plugin.json"` from `_DEFAULT_VERSION_FILES`.
3. Remove `"claude-plugin"` from `_LANGUAGES_WITH_SEPARATE_VERSION`.
4. Remove the `claude-plugin` branch from `_read_version()` (the `if language == "claude-plugin":` block).
5. Remove the `claude-plugin` branch from `_write_version()` (the `elif language == "claude-plugin":` line).
6. Update `_cross_check_language_file` and `bump` to handle `primary_language` being `None` — when it's `None`, skip the cross-check and separate version file write (same as the current behavior for unknown languages, which returns early).

- [ ] **Step 2: Update test file**

In `tests/vergil_tooling/test_version.py`, remove or update any test cases that use `shell`, `none`, or `claude-plugin` as a language value. Update fixtures that construct `vergil.toml` with these values.

- [ ] **Step 3: Run version tests**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```
vrg-commit --type refactor --scope version --message "remove shell, none, and claude-plugin from version handling" --body "Ref #1192"
```

---

### Task 6: repo_init and github_config Cleanup

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `src/vergil_tooling/lib/github_config.py`

- [ ] **Step 1: Update `repo_init.py`**

1. In `_container_suffix()`, remove the `"shell"`, `"none"`, and `"claude-plugin"` entries. The function already defaults to `"base"` via `.get(language, "base")`. Add handling for `None` — return `"base"` when `primary_language` is `None`.

2. In `_container_tag()`, handle `language is None` — return `"latest"`.

3. Anywhere `ctx.primary_language` is passed to `language_commands()`, handle the `None` case (pass empty string or guard with an `if` — `language_commands("", ...)` returns `[]` which is correct).

4. In the `_CODEQL_LANGUAGES` check, handle `None` (skip CodeQL when no language).

- [ ] **Step 2: Update `github_config.py`**

In `desired_actions_permissions()`, handle `primary_language` being `None` — use only `_BASE_ACTION_PATTERNS` when no language is set.

- [ ] **Step 3: Run full validation**

Run: `cd <worktree> && vrg-container-run -- uv run vrg-validate`
Expected: All checks pass

- [ ] **Step 4: Commit**

```
vrg-commit --type refactor --scope config --message "handle optional primary-language in repo_init and github_config" --body "Ref #1192"
```

---

### Task 7: CLI — `vrg-ecosystem-resolve` (#1184)

**Files:**
- Create: `src/vergil_tooling/bin/vrg_ecosystem_resolve.py`
- Create: `tests/vergil_tooling/test_vrg_ecosystem_resolve.py`
- Modify: `pyproject.toml` (add entry point)

- [ ] **Step 1: Write tests**

```python
"""Tests for vrg-ecosystem-resolve CLI."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from vergil_tooling.bin.vrg_ecosystem_resolve import main


def test_python_ecosystem_interactive(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["python"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert rc == 0
    assert "build_cmd:" in captured.out
    assert "publish_cmd:" in captured.out
    assert "credential_secret:" in captured.out


def test_python_ecosystem_ci_mode(capsys: object, tmp_path: object) -> None:
    import pathlib

    assert isinstance(tmp_path, pathlib.Path)
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        rc = main(["python"])
    assert rc == 0
    content = output_file.read_text()
    assert "build_cmd=" in content
    assert "publish_cmd=" in content
    assert "credential_secret=" in content


def test_go_ecosystem_no_publish(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["go"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert rc == 0
    assert "publish_cmd:" in captured.out


def test_unknown_language_fails() -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["unknown"])
    assert rc == 1


def test_no_args_fails() -> None:
    import pytest

    with pytest.raises(SystemExit):
        main([])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_ecosystem_resolve.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the CLI**

```python
"""Resolve ecosystem metadata for a language.

Prints build command, publish command, and credential secret name
for the given language identifier.
"""

from __future__ import annotations

import argparse
import json
import sys

from vergil_tooling.lib.languages import ecosystem_metadata, supported_languages
from vergil_tooling.lib.output import emit_error, is_ci, write_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-ecosystem-resolve",
        description="Resolve ecosystem metadata for a language.",
    )
    parser.add_argument("language", help="Language identifier")
    args = parser.parse_args(argv)

    try:
        info = ecosystem_metadata(args.language)
    except ValueError:
        emit_error(
            f"unsupported language: {args.language} "
            f"(supported: {', '.join(sorted(supported_languages()))})"
        )
        return 1

    build_str = json.dumps(info.build_cmd) if info.build_cmd else ""
    publish_str = json.dumps(info.publish_cmd) if info.publish_cmd else ""
    credential_str = info.credential_secret or ""

    if is_ci():
        write_output("build_cmd", build_str)
        write_output("publish_cmd", publish_str)
        write_output("credential_secret", credential_str)
    else:
        print(f"build_cmd: {build_str}")
        print(f"publish_cmd: {publish_str}")
        print(f"credential_secret: {credential_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add entry point to `pyproject.toml`**

In the `[project.scripts]` section, add:
```toml
vrg-ecosystem-resolve = "vergil_tooling.bin.vrg_ecosystem_resolve:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_ecosystem_resolve.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```
vrg-commit --type feat --scope ecosystem --message "add vrg-ecosystem-resolve CLI for language metadata lookup" --body "Ref #1192, #1184"
```

---

### Task 8: CLI — `vrg-release-validate-inputs` (#1185)

**Files:**
- Create: `src/vergil_tooling/bin/vrg_release_validate_inputs.py`
- Create: `tests/vergil_tooling/test_vrg_release_validate_inputs.py`
- Modify: `pyproject.toml` (add entry point)

- [ ] **Step 1: Write tests**

```python
"""Tests for vrg-release-validate-inputs CLI."""

from __future__ import annotations

from unittest.mock import patch

from vergil_tooling.bin.vrg_release_validate_inputs import main


def test_valid_python_release(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["python"])
    assert rc == 0


def test_valid_python_with_registry_publish(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["python", "--registry-publish"])
    assert rc == 0


def test_unsupported_language_fails(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["unknown"])
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert rc == 1
    assert "unsupported" in captured.err.lower() or "unsupported" in captured.out.lower()


def test_go_with_registry_publish_fails(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["go", "--registry-publish"])
    assert rc == 1


def test_reports_all_failures(capsys: object) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        rc = main(["unknown", "--registry-publish"])
    assert rc == 1


def test_no_args_fails() -> None:
    import pytest

    with pytest.raises(SystemExit):
        main([])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_release_validate_inputs.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement the CLI**

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-release-validate-inputs",
        description="Validate release workflow inputs.",
    )
    parser.add_argument("language", help="Language identifier")
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

    errors: list[str] = []
    langs = supported_languages()

    if args.language not in langs:
        errors.append(
            f"unsupported language: {args.language} "
            f"(supported: {', '.join(sorted(langs))})"
        )
    else:
        info = ecosystem_metadata(args.language)
        if args.registry_publish and info.publish_cmd is None:
            errors.append(
                f"--registry-publish is not supported for {args.language} "
                f"(no publish command defined)"
            )

    if errors:
        for msg in errors:
            emit_error(msg)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Add entry point to `pyproject.toml`**

In the `[project.scripts]` section, add:
```toml
vrg-release-validate-inputs = "vergil_tooling.bin.vrg_release_validate_inputs:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_release_validate_inputs.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```
vrg-commit --type feat --scope release --message "add vrg-release-validate-inputs CLI" --body "Ref #1192, #1185"
```

---

### Task 9: Final Validation

- [ ] **Step 1: Run full validation pipeline**

Run: `cd <worktree> && vrg-container-run -- uv run vrg-validate`
Expected: All checks pass (lint, typecheck, test, audit, common checks)

- [ ] **Step 2: Verify no references to validate_commands remain**

Run: `cd <worktree> && grep -r "validate_commands" src/ tests/ --include="*.py" | grep -v __pycache__`
Expected: No output (no remaining references)

- [ ] **Step 3: Verify new entry points are registered**

Run: `cd <worktree> && grep "vrg-ecosystem-resolve\|vrg-release-validate-inputs" pyproject.toml`
Expected: Both entry points listed

- [ ] **Step 4: Commit any final fixes if needed, then push**

```
vrg-git push -u origin feature/1192-shell-to-python
```
