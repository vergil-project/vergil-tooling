# Co-Author Auto-Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace static `[project.co-authors]` config in `vergil.toml` with dynamic co-author trailer resolution via the GitHub API.

**Architecture:** New `resolve_co_author_trailer()` function in `lib/github.py` discovers the agent account from `gh auth status` (existing `_discover_accounts()`), queries `/users/<agent>` to get the numeric GitHub user ID, and constructs the `Co-Authored-By` trailer. `vrg-commit` calls this function instead of reading from `vergil.toml`. The `--agent` flag is deprecated (accepted with warning, value ignored). The `[project.co-authors]` section and all co-author parsing in `config.py` are removed.

**Tech Stack:** Python 3.12+, `gh` CLI, GitHub REST API (`/users/<username>`), pytest

---

### Task 1: Add `resolve_co_author_trailer()` to `lib/github.py`

**Files:**
- Test: `tests/vergil_tooling/test_github.py`
- Modify: `src/vergil_tooling/lib/github.py:44` (after `_discover_accounts()`)

- [ ] **Step 1: Write the failing test for happy path**

Add to `tests/vergil_tooling/test_github.py` at the end of the file, inside a new test class:

```python
class TestResolveCoAuthorTrailer:
    def test_constructs_trailer_from_api(self) -> None:
        with (
            patch(
                "vergil_tooling.lib.github._discover_accounts",
                return_value=("jdoe", "jdoe-vergil"),
            ),
            patch(
                "vergil_tooling.lib.github.read_json",
                return_value={"id": 12345, "login": "jdoe-vergil"},
            ),
        ):
            trailer = github.resolve_co_author_trailer()
        assert trailer == "Co-Authored-By: jdoe-vergil <12345+jdoe-vergil@users.noreply.github.com>"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestResolveCoAuthorTrailer::test_constructs_trailer_from_api -v`
Expected: FAIL with `AttributeError: module 'vergil_tooling.lib.github' has no attribute 'resolve_co_author_trailer'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/vergil_tooling/lib/github.py` after `_discover_accounts()` (after line 44):

```python
def resolve_co_author_trailer() -> str:
    """Discover the agent account and return its ``Co-Authored-By`` trailer.

    Queries the GitHub API for the agent's numeric user ID to construct
    the noreply email that GitHub uses for commit attribution.
    """
    _human, agent = _discover_accounts()
    data = read_json("api", f"users/{agent}")
    uid = data["id"]  # type: ignore[index]
    return f"Co-Authored-By: {agent} <{uid}+{agent}@users.noreply.github.com>"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestResolveCoAuthorTrailer::test_constructs_trailer_from_api -v`
Expected: PASS

- [ ] **Step 5: Write failing test for API 404 (flagged account)**

Add to the `TestResolveCoAuthorTrailer` class:

```python
    def test_api_404_raises_github_api_error(self) -> None:
        err = github.GitHubAPIError(
            1, ["gh"], output='{"message": "Not Found"}', stderr="HTTP 404"
        )
        with (
            patch(
                "vergil_tooling.lib.github._discover_accounts",
                return_value=("jdoe", "jdoe-vergil"),
            ),
            patch(
                "vergil_tooling.lib.github.read_json",
                side_effect=err,
            ),
            pytest.raises(github.GitHubAPIError, match="404"),
        ):
            github.resolve_co_author_trailer()
```

- [ ] **Step 6: Run test to verify it passes**

This test passes without code changes — `read_json` already raises `GitHubAPIError` for non-retryable errors, and `resolve_co_author_trailer` doesn't catch it.

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestResolveCoAuthorTrailer::test_api_404_raises_github_api_error -v`
Expected: PASS

- [ ] **Step 7: Write failing test for discovery failure**

Add to the `TestResolveCoAuthorTrailer` class:

```python
    def test_discovery_failure_propagates(self) -> None:
        with (
            patch(
                "vergil_tooling.lib.github._discover_accounts",
                side_effect=SystemExit(1),
            ),
            pytest.raises(SystemExit),
        ):
            github.resolve_co_author_trailer()
```

- [ ] **Step 8: Run test to verify it passes**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestResolveCoAuthorTrailer::test_discovery_failure_propagates -v`
Expected: PASS

- [ ] **Step 9: Run all github tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py -v`
Expected: All tests pass (existing + 3 new)

- [ ] **Step 10: Commit**

```bash
vrg-git add src/vergil_tooling/lib/github.py tests/vergil_tooling/test_github.py
vrg-commit --type feat --scope github --message "add resolve_co_author_trailer for dynamic co-author discovery" --agent wphillipmoore-vergil
```

---

### Task 2: Deduplicate `_discover_accounts()` in `vrg_gh.py`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_gh.py:1-83`
- Modify: `tests/vergil_tooling/test_vrg_gh.py:174-258`

- [ ] **Step 1: Replace local `_discover_accounts()` with import**

In `src/vergil_tooling/bin/vrg_gh.py`, delete lines 64-83 (the local `_discover_accounts()` function). Then update the imports — add at the top of the file after the existing imports:

```python
from vergil_tooling.lib.github import _discover_accounts
```

Remove `import re` from the imports (line 13) since it's only used by the deleted function.

The `_get_token` function on line 86 (which will shift up after the deletion) already calls `_discover_accounts()` — it will now resolve to the imported version.

- [ ] **Step 2: Update test patches to target the new import path**

In `tests/vergil_tooling/test_vrg_gh.py`, the four `_discover_accounts` tests (lines 174-258) import and patch `vergil_tooling.bin.vrg_gh._discover_accounts`. Since `vrg_gh.py` now imports it from `lib.github`, the tests need to patch `vergil_tooling.bin.vrg_gh._discover_accounts` (which still works because the import binds the name in the `vrg_gh` module namespace). No changes needed to the test patches — they already target the right module-level name.

However, the tests that do `from vergil_tooling.bin.vrg_gh import _discover_accounts` (lines 175, 199, 225, 249) should be updated to import from the canonical location:

Replace all four occurrences of:
```python
    from vergil_tooling.bin.vrg_gh import _discover_accounts
```
with:
```python
    from vergil_tooling.lib.github import _discover_accounts
```

And update the four corresponding `patch` targets from:
```python
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
```
to:
```python
        patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
```

- [ ] **Step 3: Run vrg_gh tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py -v`
Expected: All tests pass

- [ ] **Step 4: Run github tests to confirm no regressions**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py -v`
Expected: All tests pass

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_gh.py tests/vergil_tooling/test_vrg_gh.py
vrg-commit --type refactor --scope vrg-gh --message "replace local _discover_accounts with import from lib/github" --agent wphillipmoore-vergil
```

---

### Task 3: Update `vrg-commit` to use dynamic resolution

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_commit.py:1-226`
- Modify: `tests/vergil_tooling/test_vrg_commit.py:1-473`

- [ ] **Step 1: Write failing test for auto-discovery path (no --agent)**

Add to `tests/vergil_tooling/test_vrg_commit.py`:

```python
def test_main_auto_discovery(tmp_path: Path) -> None:
    commit_file_content = ""

    def capture_run(*args: str) -> None:
        nonlocal commit_file_content
        if args[0] == "commit" and args[1] == "--file":
            commit_file_content = Path(args[2]).read_text(encoding="utf-8")

    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: jdoe-vergil <12345+jdoe-vergil@users.noreply.github.com>",
        ),
    ):
        result = main(
            ["--type", "feat", "--scope", "core", "--message", "add feature"]
        )
    assert result == 0
    assert commit_file_content.startswith("feat(core): add feature\n")
    assert "Co-Authored-By: jdoe-vergil <12345+jdoe-vergil@users.noreply.github.com>" in commit_file_content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_commit.py::test_main_auto_discovery -v`
Expected: FAIL — `--agent` is still required by argparse

- [ ] **Step 3: Write failing test for deprecated --agent flag**

Add to `tests/vergil_tooling/test_vrg_commit.py`:

```python
def test_main_agent_flag_prints_deprecation_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run"),
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: jdoe-vergil <12345+jdoe-vergil@users.noreply.github.com>",
        ),
    ):
        result = main(
            ["--type", "feat", "--scope", "core", "--message", "test", "--agent", "ignored"]
        )
    assert result == 0
    err = capsys.readouterr().err
    assert "deprecated" in err.lower()
```

- [ ] **Step 4: Implement the changes to `vrg_commit.py`**

In `src/vergil_tooling/bin/vrg_commit.py`:

**a)** Add import — change line 19 from:
```python
from vergil_tooling.lib import config, git
```
to:
```python
from vergil_tooling.lib import config, git, github
```

**b)** Update the `--agent` argument (line 78-80) — change from:
```python
    parser.add_argument(
        "--agent", required=True, help="AI agent identity (key in [project.co-authors])"
    )
```
to:
```python
    parser.add_argument(
        "--agent",
        required=False,
        default=None,
        help="Deprecated. Co-author identity is now auto-discovered.",
    )
```

**c)** Update the module docstring (line 3) — change from:
```python
"""Commit wrapper that constructs standards-compliant commit messages.

Resolves Co-Authored-By identities from vergil.toml.
```
to:
```python
"""Commit wrapper that constructs standards-compliant commit messages.

Resolves Co-Authored-By identities dynamically via the GitHub API.
```

**d)** Replace the co-author lookup block (lines 192-198) — change from:
```python
    if st_config is None or args.agent not in st_config.project.co_authors:
        print(
            f"ERROR: no co-author identity for agent '{args.agent}' in {config.CONFIG_FILE}.",
            file=sys.stderr,
        )
        return 1
    identity = st_config.project.co_authors[args.agent]
```
to:
```python
    if args.agent is not None:
        print(
            "WARNING: --agent is deprecated and will be removed in a future release. "
            "Co-author identity is now auto-discovered from gh auth status.",
            file=sys.stderr,
        )

    try:
        identity = github.resolve_co_author_trailer()
    except (SystemExit, github.GitHubAPIError) as exc:
        print(f"ERROR: failed to resolve co-author identity: {exc}", file=sys.stderr)
        return 1
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_commit.py::test_main_auto_discovery tests/vergil_tooling/test_vrg_commit.py::test_main_agent_flag_prints_deprecation_warning -v`
Expected: PASS

- [ ] **Step 6: Update existing tests to mock `resolve_co_author_trailer`**

The existing tests that pass `--agent agent` and expect the co-author trailer from `vergil.toml` now need to mock `resolve_co_author_trailer()` instead.

**Update `_commit_environment`** — add a default mock for `resolve_co_author_trailer`. Change the context manager (lines 37-74) to add one more `patch`:

```python
@contextlib.contextmanager
def _commit_environment(
    tmp_path: Path,
    *,
    branch: str = "feature/42-test",
    is_main_worktree: bool = False,
    branching_model: str = "library-release",
    has_staged: bool = True,
    write_config: bool = True,
) -> Iterator[None]:
    if write_config:
        (tmp_path / "vergil.toml").write_text(
            _TEST_TOML_TEMPLATE.format(branching_model=branching_model)
        )

    with (
        patch("vergil_tooling.bin.vrg_commit.git.current_branch", return_value=branch),
        patch("vergil_tooling.bin.vrg_commit.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_commit.git.is_main_worktree",
            return_value=is_main_worktree,
        ),
        patch(
            "vergil_tooling.bin.vrg_commit.git.has_staged_changes",
            return_value=has_staged,
        ),
        patch("vergil_tooling.bin.vrg_commit.git.run"),
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: test-agent <test-agent@test.com>",
        ),
    ):
        yield
```

**Remove co-author lines from `_TEST_TOML_TEMPLATE`** (lines 18-34) — change from:
```python
_TEST_TOML_TEMPLATE = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "{branching_model}"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]
agent = "Co-Authored-By: test-agent <test-agent@test.com>"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""
```
to:
```python
_TEST_TOML_TEMPLATE = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "{branching_model}"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""
```

**Update `_DEFAULT_ARGS`** (line 232) — remove `--agent` since it's no longer required:
```python
_DEFAULT_ARGS = ["--type", "feat", "--scope", "core", "--message", "test"]
```

**Update `test_main_missing_config`** (lines 204-212) — this test currently expects exit code 1 when `vergil.toml` is missing because the co-author lookup fails. Now the missing-config path still reads branching_model as `""` (fallback), but `resolve_co_author_trailer()` is mocked. The test needs to mock the resolver too:

```python
def test_main_missing_config(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_commit.git.current_branch", return_value="feature/42-test"),
        patch("vergil_tooling.bin.vrg_commit.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.is_main_worktree", return_value=False),
        patch("vergil_tooling.bin.vrg_commit.git.has_staged_changes", return_value=True),
        patch("vergil_tooling.bin.vrg_commit.git.run"),
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: test-agent <test-agent@test.com>",
        ),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 0
```

**Delete `test_main_unknown_agent`** (lines 215-220) — this test is no longer applicable. The `--agent` value is ignored; there is no "unknown agent" case.

- [ ] **Step 7: Run all vrg_commit tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_commit.py -v`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_commit.py tests/vergil_tooling/test_vrg_commit.py
vrg-commit --type feat --scope vrg-commit --message "replace config-based co-author lookup with dynamic API resolution" --agent wphillipmoore-vergil
```

---

### Task 4: Remove co-author config from `config.py` and `vergil.toml`

**Files:**
- Modify: `src/vergil_tooling/lib/config.py:16,47,98-104,151-158`
- Modify: `vergil.toml:8-9`
- Modify: `tests/vergil_tooling/test_config.py:67-80,85-95,123-130,140-148`

- [ ] **Step 1: Write failing test for backward compatibility**

Add to `tests/vergil_tooling/test_config.py`:

```python
def test_read_config_ignores_leftover_co_authors(tmp_path: Path) -> None:
    toml = _VALID_TOML + ""
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert not hasattr(cfg.project, "co_authors")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_config.py::test_read_config_ignores_leftover_co_authors -v`
Expected: FAIL — `ProjectConfig` still has `co_authors` attribute

- [ ] **Step 3: Remove co-author code from `config.py`**

In `src/vergil_tooling/lib/config.py`:

**a)** Delete the `_COAUTHOR_RE` regex (line 16):
```python
_COAUTHOR_RE = re.compile(r"^Co-Authored-By:\s+.+\s+<.+>$")
```

**b)** Remove `import re` from line 6, since it's no longer used.

**c)** Remove `co_authors` from `ProjectConfig` (line 47):
```python
    co_authors: dict[str, str]
```

**d)** Delete the co-author validation block (lines 98-104):
```python
    co_authors: dict[str, str] = {}
    co_authors_raw = project_raw.get("co-authors", {})
    for name, trailer in co_authors_raw.items():
        if not _COAUTHOR_RE.match(trailer):
            msg = f"{CONFIG_FILE}: malformed co-author trailer for '{name}': {trailer!r}"
            raise ConfigError(msg)
        co_authors[name] = trailer
```

**e)** Remove `co_authors=co_authors` from the `ProjectConfig` constructor call (around line 157 after deletions above):
Change from:
```python
    project = ProjectConfig(
        repository_type=project_raw["repository-type"],
        versioning_scheme=project_raw["versioning-scheme"],
        branching_model=project_raw["branching-model"],
        release_model=project_raw["release-model"],
        primary_language=project_raw["primary-language"],
        co_authors=co_authors,
    )
```
to:
```python
    project = ProjectConfig(
        repository_type=project_raw["repository-type"],
        versioning_scheme=project_raw["versioning-scheme"],
        branching_model=project_raw["branching-model"],
        release_model=project_raw["release-model"],
        primary_language=project_raw["primary-language"],
    )
```

- [ ] **Step 4: Run the backward compatibility test**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_config.py::test_read_config_ignores_leftover_co_authors -v`
Expected: PASS

- [ ] **Step 5: Update test fixtures and assertions**

In `tests/vergil_tooling/test_config.py`:

**a)** Remove co-author lines from `_BASE_TOML` (lines 67-80) — change from:
```python
_BASE_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]
agent = "Co-Authored-By: user-agent <111+user-agent@users.noreply.github.com>"

[dependencies]
vergil = "v2.0"
"""
```
to:
```python
_BASE_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"
"""
```

**b)** Remove co-author assertions from `test_read_config_valid` (lines 93-94):
```python
    assert "agent" in cfg.project.co_authors
    assert "user-agent" in cfg.project.co_authors["agent"]
```

**c)** Delete `test_read_config_malformed_co_author` entirely (lines 123-130).

**d)** Delete `test_read_config_no_co_authors` entirely (lines 140-148).

**e)** Update the `test_read_config_ignores_leftover_co_authors` test written in Step 1. The `_VALID_TOML` fixture no longer has co-authors, so we need to add them explicitly:

```python
def test_read_config_ignores_leftover_co_authors(tmp_path: Path) -> None:
    toml = _VALID_TOML + '\n[project.co-authors]\nagent = "Co-Authored-By: x <1+x@users.noreply.github.com>"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert not hasattr(cfg.project, "co_authors")
```

- [ ] **Step 6: Remove `[project.co-authors]` from `vergil.toml`**

In `vergil.toml`, delete lines 8-9:
```toml
[project.co-authors]
wphillipmoore-vergil = "Co-Authored-By: wphillipmoore-vergil <285019742+wphillipmoore-vergil@users.noreply.github.com>"
```

- [ ] **Step 7: Run all config tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_config.py -v`
Expected: All tests pass

- [ ] **Step 8: Commit**

```bash
vrg-git add src/vergil_tooling/lib/config.py vergil.toml tests/vergil_tooling/test_config.py
vrg-commit --type refactor --scope config --message "remove co-author config from vergil.toml and config.py" --agent wphillipmoore-vergil
```

---

### Task 5: Full validation

- [ ] **Step 1: Run the full validation pipeline**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: All checks pass (lint, typecheck, tests, audit)

- [ ] **Step 2: Fix any validation failures**

Address any issues found by the full pipeline (type errors, lint warnings, etc.)

- [ ] **Step 3: Commit any fixes**

If fixes were needed:
```bash
vrg-git add -u
vrg-commit --type fix --scope co-authors --message "address validation findings from co-author auto-discovery" --agent wphillipmoore-vergil
```
