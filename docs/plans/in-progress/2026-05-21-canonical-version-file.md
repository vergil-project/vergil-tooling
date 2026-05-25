# Canonical VERSION File — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `VERSION` file at the repo root the canonical
version source for all vergil-managed repositories, with sync
enforcement against language-specific version files.

**Architecture:** `vrg-version show` reads `VERSION` at the repo
root, then cross-checks the language-specific file (pyproject.toml,
Cargo.toml, etc.) and errors on mismatch. `vrg-version bump` writes
both files. The vergil.toml parser gains unrecognized-key warnings.
The init wizard creates `VERSION` during bootstrap.

**Tech Stack:** Python (vergil-tooling), pytest

**Spec:** `docs/specs/2026-05-21-canonical-version-file-design.md`
(#970)

**Repository:** vergil-tooling

**Worktree:** `.worktrees/issue-970-canonical-version/`
**Branch:** `feature/970-canonical-version`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/vergil_tooling/lib/config.py` | Modify | Add `_warn_unrecognized_keys`, known-key dictionaries |
| `src/vergil_tooling/lib/version.py` | Modify | Rewrite `show`/`bump` to use VERSION as canonical, add cross-check, add `VersionSyncError`, remove version-file override |
| `src/vergil_tooling/lib/repo_init.py` | Modify | Add `initial_version` field, version prompt in step 3, VERSION write in step 4 |
| `tests/vergil_tooling/test_config.py` | Modify | Add tests for unrecognized-key warnings |
| `tests/vergil_tooling/test_version.py` | Modify | Rewrite for VERSION-as-canonical: add VERSION files to all tests, add sync-error/warning tests, remove version-file override test |
| `tests/vergil_tooling/test_repo_init.py` | Modify | Add tests for version prompt and VERSION file creation |
| `tests/vergil_tooling/test_vrg_version.py` | Modify | Update ref test for new behavior |
| `VERSION` | Modify | Update from `2.0.4` to `2.0.28` |

---

### Task 1: vergil.toml unrecognized-key warnings

**Files:**
- Modify: `src/vergil_tooling/lib/config.py`
- Test: `tests/vergil_tooling/test_config.py`

- [ ] **Step 1: Write failing tests for unrecognized-key warnings**

Add to the end of `tests/vergil_tooling/test_config.py`:

```python
# -- unrecognized-key warnings ------------------------------------------------

_VALID_TOML_WITH_EXTRA_PROJECT_KEY = _VALID_TOML + ""

# Inline a copy that has an extra key in [project]
_EXTRA_PROJECT_KEY_TOML = (
    '[project]\n'
    'repository-type = "library"\n'
    'versioning-scheme = "semver"\n'
    'branching-model = "library-release"\n'
    'release-model = "tagged-release"\n'
    'primary-language = "python"\n'
    'version-file = "custom/VERSION"\n'
    '\n[dependencies]\nvergil = "v2.0"\n'
    '\n[ci]\nversions = ["3.14"]\n'
)


def test_warns_unrecognized_project_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text(_EXTRA_PROJECT_KEY_TOML)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'version-file' in [project]" in err


def test_warns_unrecognized_top_level_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML + '\n[custom]\nfoo = "bar"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized section [custom]" in err


def test_warns_unrecognized_dependency_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML.replace(
        'vergil = "v2.0"', 'vergil = "v2.0"\nother-tool = "v1.0"'
    )
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'other-tool' in [dependencies]" in err


def test_warns_unrecognized_ci_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML + ""  # _VALID_TOML already has [ci]
    # Replace the ci section to add an extra key
    toml = _BASE_TOML + '\n[ci]\nversions = ["3.14"]\nfoo = true\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'foo' in [ci]" in err


def test_warns_unrecognized_publish_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML + '\n[publish]\nrelease = true\nfoo = true\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'foo' in [publish]" in err


def test_no_warnings_for_valid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert err == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_config.py -v -k "warns_unrecognized or no_warnings_for_valid"`

Expected: All new tests FAIL (no warnings emitted yet).

- [ ] **Step 3: Implement unrecognized-key warnings in config.py**

Add these constants after the existing `_PROJECT_FIELDS` tuple in
`src/vergil_tooling/lib/config.py`:

```python
_KNOWN_SECTIONS = frozenset({"project", "dependencies", "markdownlint", "ci", "publish"})

_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "project": frozenset(_PROJECT_FIELDS),
    "dependencies": frozenset({"vergil"}),
    "markdownlint": frozenset({"ignore"}),
    "ci": frozenset({"versions", "integration-tests"}),
    "publish": frozenset({"release", "docs", "consumer-refresh"}),
}
```

Add `import sys` to the imports (alongside the existing `os` import).

Add this function before `_parse_raw_config`:

```python
def _warn_unrecognized_keys(raw: dict[str, Any]) -> None:
    for section in raw:
        if section not in _KNOWN_SECTIONS:
            print(f"vergil.toml: unrecognized section [{section}]", file=sys.stderr)
            continue
        if not isinstance(raw[section], dict):
            continue
        known = _KNOWN_KEYS.get(section, frozenset())
        for key in raw[section]:
            if key not in known:
                print(
                    f"vergil.toml: unrecognized key '{key}' in [{section}]",
                    file=sys.stderr,
                )
```

Add a call to `_warn_unrecognized_keys(raw)` as the first line of
`_parse_raw_config`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_config.py -v`

Expected: ALL tests pass (existing + new). Verify no regressions in
existing config tests.

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope config --message "add unrecognized-key warnings to vergil.toml parser"
```

---

### Task 2: version.show() reads from VERSION with cross-check

**Files:**
- Modify: `src/vergil_tooling/lib/version.py`
- Modify: `tests/vergil_tooling/test_version.py`

- [ ] **Step 1: Write failing tests for new show() behavior**

Add these imports and tests to `tests/vergil_tooling/test_version.py`.
Add `VersionSyncError` to the import from `vergil_tooling.lib.version`:

```python
from vergil_tooling.lib.version import VersionSyncError, bump, show, show_major_minor
```

Add these new tests after the existing `test_show_major_minor` test:

```python
# -- cross-check tests -------------------------------------------------------


def test_show_cross_checks_python(tmp_path: Path) -> None:
    """show() reads VERSION and cross-checks pyproject.toml."""
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.2.3"\n')
    assert show(tmp_path) == "1.2.3"


def test_show_mismatch_raises(tmp_path: Path) -> None:
    """show() raises VersionSyncError when VERSION and language file disagree."""
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "9.9.9"\n')
    with pytest.raises(VersionSyncError, match="VERSION contains 1.2.3 but pyproject.toml contains 9.9.9"):
        show(tmp_path)


def test_show_missing_language_file_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """show() warns when language file missing (bootstrap scenario)."""
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    # No pyproject.toml — bootstrap scenario
    result = show(tmp_path)
    assert result == "0.1.0"
    err = capsys.readouterr().err
    assert "pyproject.toml not found" in err


def test_show_shell_skips_cross_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """show() skips cross-check for shell language."""
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("2.0.1\n")
    assert show(tmp_path) == "2.0.1"
    err = capsys.readouterr().err
    assert err == ""


def test_show_none_skips_cross_check(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """show() skips cross-check for none language."""
    _write_toml(tmp_path, "none")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    assert show(tmp_path) == "1.0.0"
    err = capsys.readouterr().err
    assert err == ""


def test_show_missing_version_file_raises(tmp_path: Path) -> None:
    """show() raises FileNotFoundError when VERSION is missing."""
    _write_toml(tmp_path, "python")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "1.0.0"\n')
    with pytest.raises(FileNotFoundError, match="VERSION"):
        show(tmp_path)


def test_show_ref_reads_version_file_no_cross_check(tmp_path: Path) -> None:
    """show(ref=...) reads VERSION from ref, no cross-check."""
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.0.0\n")
    with patch("vergil_tooling.lib.version.subprocess.run") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="1.9.0\n"
        )
        result = show(tmp_path, ref="origin/main")
    assert result == "1.9.0"
    mock_run.assert_called_once_with(
        ["git", "show", "origin/main:VERSION"],
        capture_output=True, text=True, check=True,
    )
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v -k "cross_check or mismatch or missing_language or shell_skips or none_skips or missing_version_file_raises or ref_reads_version"`

Expected: All FAIL — `VersionSyncError` doesn't exist yet, `show()`
reads from language file not VERSION.

- [ ] **Step 3: Implement new show() logic in version.py**

In `src/vergil_tooling/lib/version.py`, make these changes:

Add constant and exception after the existing `_DEFAULT_VERSION_FILES` dict:

```python
VERSION_FILE = "VERSION"

_LANGUAGES_WITH_SEPARATE_VERSION = frozenset({
    "python", "rust", "java", "ruby", "go", "claude-plugin",
})


class VersionSyncError(Exception):
    """Raised when VERSION and language-specific file disagree."""
```

Add a new cross-check helper after `_discover_version_file`:

```python
def _cross_check_language_file(
    repo_root: Path, language: str, canonical_version: str
) -> None:
    if language not in _LANGUAGES_WITH_SEPARATE_VERSION:
        return
    try:
        lang_file = _discover_version_file(repo_root, language)
    except (FileNotFoundError, ValueError):
        print(
            f"warning: {language} version file not found; sync check skipped",
            file=sys.stderr,
        )
        return
    if not lang_file.is_file():
        rel = lang_file.relative_to(repo_root)
        print(
            f"warning: {rel} not found; sync check skipped",
            file=sys.stderr,
        )
        return
    lang_version = _read_version(lang_file.read_text(), language)
    if lang_version != canonical_version:
        rel = lang_file.relative_to(repo_root)
        msg = f"VERSION contains {canonical_version} but {rel} contains {lang_version}"
        raise VersionSyncError(msg)
```

Replace the existing `show` function:

```python
def show(repo_root: Path, *, ref: str | None = None) -> str:
    if ref is not None:
        return _read_version_from_ref(ref, VERSION_FILE, "shell")

    version_file = repo_root / VERSION_FILE
    if not version_file.is_file():
        msg = f"VERSION file not found at {repo_root}"
        raise FileNotFoundError(msg)
    version = version_file.read_text().strip()

    cfg = read_config(repo_root)
    _cross_check_language_file(repo_root, cfg.project.primary_language, version)

    return version
```

Remove these functions (no longer needed):

- `_get_version_file`
- `_version_file_relative`

- [ ] **Step 4: Run new tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v -k "cross_check or mismatch or missing_language or shell_skips or none_skips or missing_version_file_raises or ref_reads_version"`

Expected: All PASS.

- [ ] **Step 5: Update existing show() tests for new behavior**

The existing `test_show_*` tests for non-shell languages read from
the language file only. They now need a VERSION file too. Update each
one to add a VERSION file with the matching version:

`test_show_python`: Add `(tmp_path / "VERSION").write_text("1.2.3\n")`

`test_show_rust`: Add `(tmp_path / "VERSION").write_text("0.3.7\n")`

`test_show_ruby`: Add `(tmp_path / "VERSION").write_text("4.1.0\n")`

`test_show_go`: Add `(tmp_path / "VERSION").write_text("1.0.5\n")`

`test_show_java`: Add `(tmp_path / "VERSION").write_text("3.2.1\n")`

`test_show_claude_plugin`: Add `(tmp_path / "VERSION").write_text("1.4.19\n")`

`test_show_generic_version_file`: Already creates VERSION for shell
language. No change needed.

`test_show_major_minor`: Already creates VERSION for shell language.
No change needed.

Remove `test_show_with_version_file_override` entirely (feature removed).

Replace `test_show_missing_version_file` with
`test_show_missing_version_file_raises` (already written in Step 1 —
remove the old one to avoid duplication).

Replace `test_show_ref_reads_via_git` with
`test_show_ref_reads_version_file_no_cross_check` (already written
in Step 1 — remove the old one).

- [ ] **Step 6: Run all show tests to verify everything passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v -k "show"`

Expected: All PASS.

- [ ] **Step 7: Commit**

```
vrg-commit --type feat --scope version --message "show reads from canonical VERSION file with language cross-check"
```

---

### Task 3: version.bump() dual-write

**Files:**
- Modify: `src/vergil_tooling/lib/version.py`
- Modify: `tests/vergil_tooling/test_version.py`

- [ ] **Step 1: Write failing test for dual-write bump**

Add to `tests/vergil_tooling/test_version.py`:

```python
# -- dual-write bump tests ---------------------------------------------------


def test_bump_writes_both_version_and_pyproject(tmp_path: Path) -> None:
    """bump() updates both VERSION and pyproject.toml."""
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.0.0\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "example"\nversion = "2.0.0"\n')
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "2.0.1"
    assert (tmp_path / "VERSION").read_text().strip() == "2.0.1"
    assert 'version = "2.0.1"' in (tmp_path / "pyproject.toml").read_text()


def test_bump_shell_writes_version_only(tmp_path: Path) -> None:
    """bump() for shell language only writes VERSION."""
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    result = bump(tmp_path)
    assert result == "1.0.1"
    assert (tmp_path / "VERSION").read_text().strip() == "1.0.1"


def test_bump_missing_language_file_still_bumps_version(tmp_path: Path) -> None:
    """bump() updates VERSION even if language file doesn't exist (bootstrap)."""
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("0.1.0\n")
    # No pyproject.toml
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path)
    assert result == "0.1.1"
    assert (tmp_path / "VERSION").read_text().strip() == "0.1.1"
```

- [ ] **Step 2: Run new tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v -k "bump_writes_both or bump_shell_writes or bump_missing_language"`

Expected: FAIL — `bump()` still reads from language file, not VERSION.

- [ ] **Step 3: Implement new bump() logic**

Replace the existing `bump` function in
`src/vergil_tooling/lib/version.py`:

```python
def bump(repo_root: Path) -> str:
    version_file = repo_root / VERSION_FILE
    if not version_file.is_file():
        msg = f"VERSION file not found at {repo_root}"
        raise FileNotFoundError(msg)
    old_version = version_file.read_text().strip()
    new_version = _increment_patch(old_version)

    version_file.write_text(new_version + "\n")

    cfg = read_config(repo_root)
    language = cfg.project.primary_language
    if language in _LANGUAGES_WITH_SEPARATE_VERSION:
        try:
            lang_file = _discover_version_file(repo_root, language)
            if lang_file.is_file():
                _write_version(lang_file, language, old_version, new_version)
        except (FileNotFoundError, ValueError):
            pass

    _run_lockfile_maintenance(repo_root, language)
    return new_version
```

- [ ] **Step 4: Run new bump tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v -k "bump_writes_both or bump_shell_writes or bump_missing_language"`

Expected: All PASS.

- [ ] **Step 5: Update existing bump tests for new behavior**

Each existing `test_bump_*` test for non-shell languages needs a
VERSION file. Add a VERSION file line to each, matching the initial
version. Also add an assertion that VERSION was updated:

`test_bump_python`: Add `(tmp_path / "VERSION").write_text("2.0.0\n")`
and `assert (tmp_path / "VERSION").read_text().strip() == "2.0.1"`.

`test_bump_rust`: Add `(tmp_path / "VERSION").write_text("0.3.7\n")`
and `assert (tmp_path / "VERSION").read_text().strip() == "0.3.8"`.

`test_bump_ruby`: Add `(tmp_path / "VERSION").write_text("1.0.0\n")`
and `assert (tmp_path / "VERSION").read_text().strip() == "1.0.1"`.

`test_bump_go`: Add `(tmp_path / "VERSION").write_text("1.0.5\n")`
and `assert (tmp_path / "VERSION").read_text().strip() == "1.0.6"`.

`test_bump_java`: Add `(tmp_path / "VERSION").write_text("3.2.1\n")`
and `assert (tmp_path / "VERSION").read_text().strip() == "3.2.2"`.

`test_bump_claude_plugin`: Add `(tmp_path / "VERSION").write_text("1.4.19\n")`
and `assert (tmp_path / "VERSION").read_text().strip() == "1.4.20"`.

`test_bump_generic`: Already creates VERSION for shell language. No
change needed.

For **lockfile maintenance tests**, each one needs a VERSION file too:

`test_bump_python_runs_uv_lock`: Add `(tmp_path / "VERSION").write_text("1.0.0\n")`.

`test_bump_rust_runs_cargo_update`: Add `(tmp_path / "VERSION").write_text("0.1.0\n")`.

`test_bump_ruby_runs_bundle_install`: Add `(tmp_path / "VERSION").write_text("1.0.0\n")`.

`test_bump_generic_skips_lockfile`: Already uses shell with VERSION.
No change needed.

`test_bump_claude_plugin_skips_lockfile`: Add `(tmp_path / "VERSION").write_text("1.0.0\n")`.

- [ ] **Step 6: Run all bump tests to verify everything passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v -k "bump"`

Expected: All PASS.

- [ ] **Step 7: Commit**

```
vrg-commit --type feat --scope version --message "bump writes both VERSION and language-specific file"
```

---

### Task 4: Update CLI tests and clean up dead code

**Files:**
- Modify: `tests/vergil_tooling/test_vrg_version.py`
- Modify: `tests/vergil_tooling/test_version.py`
- Modify: `src/vergil_tooling/lib/version.py`

- [ ] **Step 1: Update CLI ref test**

In `tests/vergil_tooling/test_vrg_version.py`, replace
`test_show_ref`:

```python
def test_show_ref(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("vergil_tooling.bin.vrg_version.Path.cwd", return_value=tmp_path),
        patch(
            "vergil_tooling.lib.version.subprocess.run",
            return_value=__import__("subprocess").CompletedProcess(
                args=[], returncode=0, stdout="1.1.0\n",
            ),
        ),
        patch("sys.argv", ["vrg-version", "show", "--ref", "origin/main"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.1.0"
```

- [ ] **Step 2: Remove _read_version_from_ref body test**

In `tests/vergil_tooling/test_version.py`, the test
`test_read_version_from_ref_body` tests the old behavior (passing
arbitrary path and language). Update it to test the VERSION path:

```python
def test_read_version_from_ref_body(tmp_path: Path) -> None:
    from vergil_tooling.lib.version import _read_version_from_ref

    with patch(
        "vergil_tooling.lib.version.subprocess.run",
        return_value=__import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="1.2.3\n"
        ),
    ):
        result = _read_version_from_ref("origin/main", "VERSION", "shell")
    assert result == "1.2.3"
```

- [ ] **Step 3: Run all tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py tests/vergil_tooling/test_vrg_version.py -v`

Expected: All PASS.

- [ ] **Step 4: Remove dead code from version.py**

Verify these functions are no longer called anywhere:

```
grep -rn "_get_version_file\|_version_file_relative" --include="*.py" src/ tests/
```

If only the definitions appear (no callers), delete both functions
from `src/vergil_tooling/lib/version.py`.

Also remove this block from `show()` / `_get_version_file` (already
removed if you followed Task 2 Step 3, but verify):
- The `version-file` override logic that reads
  `raw.get("project", {}).get("version-file")`

- [ ] **Step 5: Run full test suite**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py tests/vergil_tooling/test_vrg_version.py tests/vergil_tooling/test_config.py -v`

Expected: All PASS.

- [ ] **Step 6: Commit**

```
vrg-commit --type refactor --scope version --message "remove dead code: _get_version_file, _version_file_relative, version-file override"
```

---

### Task 5: Init wizard — version prompt and VERSION file

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py`
- Modify: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write failing test for version prompt in step 3**

Add to `tests/vergil_tooling/test_repo_init.py` inside
`TestStepGenerateConfig`:

```python
    def test_prompts_for_initial_version(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path

        inputs = iter(
            [
                "5",  # repository-type: tooling
                "8",  # primary-language: shell
                "3",  # branching-model: library-release
                "4",  # versioning-scheme: semver
                "4",  # release-model: tagged-release
                "latest",  # ci versions
                "n",  # integration tests
                "y",  # publish releases
                "y",  # publish docs
                "",  # vergil version (default v2.0)
                "1",  # license: GPL-3.0
                "1.0.0",  # initial version
            ]
        )

        with (
            patch("builtins.input", side_effect=lambda _="": next(inputs)),
            patch("vergil_tooling.lib.repo_init.git.run"),
        ):
            step_generate_config(ctx)

        assert ctx.initial_version == "1.0.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepGenerateConfig::test_prompts_for_initial_version -v`

Expected: FAIL — `StopIteration` because `step_generate_config`
doesn't consume the 12th input yet, or `initial_version` attribute
doesn't exist.

- [ ] **Step 3: Add initial_version to RepoInitContext and prompt**

In `src/vergil_tooling/lib/repo_init.py`:

Add field to `RepoInitContext` dataclass (after `license_type`):

```python
    initial_version: str = "0.1.0"
```

Add the version prompt at the end of `step_generate_config`, after
the `license_type` prompt but before writing vergil.toml:

```python
    ctx.initial_version = prompt_free_text(
        "Initial version",
        default="0.1.0",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepGenerateConfig::test_prompts_for_initial_version -v`

Expected: PASS.

- [ ] **Step 5: Write failing test for VERSION file in step 4**

Add to `tests/vergil_tooling/test_repo_init.py` inside
`TestStepScaffoldConfigFiles`:

```python
    def test_creates_version_file(self, tmp_path: Path) -> None:
        ctx = RepoInitContext(org="vergil-project", name="vergil-vm")
        ctx.work_dir = tmp_path
        ctx.description = "Test repo"
        ctx.license_type = "MIT"
        ctx.publish_docs = True
        ctx.initial_version = "1.0.0"

        with patch("vergil_tooling.lib.repo_init.git.run"):
            step_scaffold_config_files(ctx)

        version_file = tmp_path / "VERSION"
        assert version_file.exists()
        assert version_file.read_text() == "1.0.0\n"
```

- [ ] **Step 6: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepScaffoldConfigFiles::test_creates_version_file -v`

Expected: FAIL — VERSION file not created yet.

- [ ] **Step 7: Add VERSION creation to step_scaffold_config_files**

In `src/vergil_tooling/lib/repo_init.py`, inside
`step_scaffold_config_files`, add this block before the
`git.run("add", "-A")` line:

```python
    # VERSION
    (wd / "VERSION").write_text(ctx.initial_version + "\n")
```

- [ ] **Step 8: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_repo_init.py::TestStepScaffoldConfigFiles::test_creates_version_file -v`

Expected: PASS.

- [ ] **Step 9: Fix existing step 3 tests that now need the extra input**

The existing `test_prompts_and_writes_vergil_toml` and
`test_prefills_from_existing_toml_in_adopt_mode` tests provide a
fixed number of inputs. They now need one more input for the version
prompt.

`test_prompts_and_writes_vergil_toml`: Add `"",` (accept default
`0.1.0`) to the end of the inputs iterator (after the license
choice `"1"`).

`test_prefills_from_existing_toml_in_adopt_mode`: Change
`iter([""] * 11)` to `iter([""] * 12)`.

- [ ] **Step 10: Run all repo_init tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_repo_init.py -v`

Expected: All PASS.

- [ ] **Step 11: Commit**

```
vrg-commit --type feat --scope repo-init --message "add initial version prompt and VERSION file to bootstrap wizard"
```

---

### Task 6: Fix stale VERSION file and validate

**Files:**
- Modify: `VERSION`

- [ ] **Step 1: Update VERSION to match pyproject.toml**

The current `VERSION` contains `2.0.4`. The current
`pyproject.toml` has `version = "2.0.28"`. Update `VERSION` to
`2.0.28`:

Write `2.0.28\n` to `VERSION`.

- [ ] **Step 2: Run full validation**

Run: `vrg-container-run -- uv run vrg-validate`

Expected: All checks pass — lint, typecheck, tests, audit.

- [ ] **Step 3: Commit**

```
vrg-commit --type fix --scope version --message "sync stale VERSION file with pyproject.toml (2.0.4 -> 2.0.28)"
```

---

## Execution Notes

**Test runner:** All tests run inside the dev container via
`vrg-container-run -- uv run pytest <path> -v`. Final validation
before each commit: `vrg-container-run -- uv run vrg-validate`.

**Commit tool:** Use `vrg-commit` (not `git commit`) for all
commits. It enforces conventional commit format and the pre-commit
gate.

**Working directory:** All file paths are relative to the worktree
at `.worktrees/issue-970-canonical-version/`. Use absolute paths
for Read/Edit/Write tools.

**Task ordering:** Tasks 1-6 must be done in order. Task 1 (config
warnings) is independent but should go first so the warning
infrastructure is in place when the version-file override is removed.
Tasks 2-4 form the core version.py change. Task 5 is the init
wizard. Task 6 is the rollout fix.
