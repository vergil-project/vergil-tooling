# Configurable Container Env-Var Passthrough Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace hardcoded env-var prefix passthrough in `container.py` with a configurable `[container].env-prefixes` list in `vergil.toml`.

**Architecture:** New optional `[container]` section in `vergil.toml` with an `env-prefixes` key (list of strings). `build_container_args()` gains an `env_prefixes` parameter; callers read config and pass it through. A convenience function `container_env_prefixes()` in `config.py` handles the common pattern of reading prefixes with a graceful fallback when `vergil.toml` is absent.

**Tech Stack:** Python 3.12+, pytest, tomllib (stdlib)

**Spec:** `docs/specs/2026-05-25-configurable-container-env-passthrough-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/vergil_tooling/lib/config.py` | Add `ContainerConfig`, parse `[container]`, add `container_env_prefixes()` |
| Modify | `src/vergil_tooling/lib/container.py` | Add `env_prefixes` parameter to `build_container_args()` and `build_docker_args()` |
| Modify | `src/vergil_tooling/bin/vrg_container_run.py` | Read config, pass `env_prefixes`, update help text |
| Modify | `src/vergil_tooling/bin/vrg_container_test.py` | Add `env_prefixes` to `build_test_container_args()`, read config in `main()` |
| Modify | `src/vergil_tooling/bin/vrg_container_docs.py` | Read config, pass `env_prefixes` |
| Modify | `tests/vergil_tooling/test_config.py` | Tests for `[container]` parsing and `container_env_prefixes()` |
| Modify | `tests/vergil_tooling/test_container.py` | Update passthrough tests, add no-prefix test |
| Modify | `tests/vergil_tooling/test_vrg_container_test.py` | Update MQ env test |
| Modify | `tests/vergil_tooling/test_vrg_container_run.py` | Update help text assertion |
| Modify | `CLAUDE.md` | Remove MQ_* passthrough documentation |

---

### Task 1: Add `[container]` config section to `config.py`

**Files:**
- Modify: `src/vergil_tooling/lib/config.py`
- Test: `tests/vergil_tooling/test_config.py`

- [ ] **Step 1: Write failing tests for `[container]` section parsing**

Add these tests to `tests/vergil_tooling/test_config.py`. Add `ContainerConfig` and `container_env_prefixes` to the import block at the top:

```python
from vergil_tooling.lib.config import (
    CiConfig,
    ConfigError,
    ContainerConfig,
    MarkdownlintConfig,
    _warn_unrecognized_keys,
    container_env_prefixes,
    read_config,
    vrg_install_tag,
)
```

Add this TOML fixture after the existing `_PUBLISH_RELEASE_ONLY_TOML`:

```python
_CONTAINER_TOML = (
    _VALID_TOML
    + """
[container]
env-prefixes = ["MQ_"]
"""
)
```

Add these tests after the unrecognized-key tests section:

```python
# -- [container] section ------------------------------------------------------


def test_read_config_container_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_CONTAINER_TOML)
    cfg = read_config(tmp_path)
    assert cfg.container == ContainerConfig(env_prefixes=["MQ_"])


def test_read_config_no_container_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.container == ContainerConfig(env_prefixes=[])


def test_read_config_container_empty_prefixes(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[container]\nenv-prefixes = []\n"
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.container.env_prefixes == []


def test_read_config_container_multiple_prefixes(tmp_path: Path) -> None:
    toml = _VALID_TOML + '[container]\nenv-prefixes = ["MQ_", "KAFKA_"]\n'
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.container.env_prefixes == ["MQ_", "KAFKA_"]


def test_read_config_container_missing_env_prefixes(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[container]\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[container\].*env-prefixes"):
        read_config(tmp_path)


def test_read_config_container_prefixes_not_list(tmp_path: Path) -> None:
    toml = _VALID_TOML + '[container]\nenv-prefixes = "MQ_"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[container\]\.env-prefixes must be a list"):
        read_config(tmp_path)


def test_read_config_container_prefixes_not_strings(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[container]\nenv-prefixes = [1, 2]\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[container\]\.env-prefixes must be a list of strings"):
        read_config(tmp_path)


def test_warns_unrecognized_container_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML + '[container]\nenv-prefixes = ["MQ_"]\nfoo = true\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'foo' in [container]" in err


# -- container_env_prefixes convenience function ------------------------------


def test_container_env_prefixes_with_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_CONTAINER_TOML)
    assert container_env_prefixes(tmp_path) == ["MQ_"]


def test_container_env_prefixes_no_file(tmp_path: Path) -> None:
    assert container_env_prefixes(tmp_path) == []


def test_container_env_prefixes_no_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    assert container_env_prefixes(tmp_path) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_config.py -v -k "container" 2>&1 | head -40`

Expected: ImportError — `ContainerConfig` and `container_env_prefixes` don't exist yet.

- [ ] **Step 3: Implement `ContainerConfig` and `[container]` parsing**

In `src/vergil_tooling/lib/config.py`:

Add the `ContainerConfig` dataclass after `PublishConfig`:

```python
@dataclass
class ContainerConfig:
    env_prefixes: list[str]
```

Add `container: ContainerConfig` to `VergilConfig`:

```python
@dataclass
class VergilConfig:
    project: ProjectConfig
    dependencies: dict[str, str]
    markdownlint: MarkdownlintConfig
    ci: CiConfig
    publish: PublishConfig
    container: ContainerConfig
```

Update `_KNOWN_SECTIONS`:

```python
_KNOWN_SECTIONS = frozenset({"project", "dependencies", "markdownlint", "ci", "publish", "container"})
```

Update `_KNOWN_KEYS`:

```python
_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "project": frozenset(_PROJECT_FIELDS),
    "dependencies": frozenset({"vergil"}),
    "markdownlint": frozenset({"ignore"}),
    "ci": frozenset({"versions", "integration-tests"}),
    "publish": frozenset({"release", "docs", "consumer-refresh"}),
    "container": frozenset({"env-prefixes"}),
}
```

Add `[container]` parsing in `_parse_raw_config`, after the `publish` block and before the `project = ProjectConfig(...)` line:

```python
    container_raw = raw.get("container")
    if container_raw is not None:
        env_prefixes = container_raw.get("env-prefixes")
        if env_prefixes is None:
            msg = f"{CONFIG_FILE}: [container] missing required field 'env-prefixes'"
            raise ConfigError(msg)
        if not isinstance(env_prefixes, list) or not all(
            isinstance(p, str) for p in env_prefixes
        ):
            msg = f"{CONFIG_FILE}: [container].env-prefixes must be a list of strings"
            raise ConfigError(msg)
        container = ContainerConfig(env_prefixes=env_prefixes)
    else:
        container = ContainerConfig(env_prefixes=[])
```

Add `container=container` to the `VergilConfig(...)` return:

```python
    return VergilConfig(
        project=project,
        dependencies=dict(deps),
        markdownlint=markdownlint,
        ci=ci,
        publish=publish,
        container=container,
    )
```

Add the convenience function after `vrg_install_tag`:

```python
def container_env_prefixes(repo_root: Path) -> list[str]:
    """Return ``[container].env-prefixes`` from vergil.toml, or ``[]``."""
    try:
        cfg = read_config(repo_root)
    except FileNotFoundError:
        return []
    return cfg.container.env_prefixes
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_config.py -v 2>&1 | tail -30`

Expected: All tests pass, including the new `container` tests.

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/lib/config.py tests/vergil_tooling/test_config.py
vrg-commit --type feat --scope config --message "add [container].env-prefixes section to vergil.toml (#777)" --body "Ref #777"
```

---

### Task 2: Make env passthrough configurable in `container.py`

**Files:**
- Modify: `src/vergil_tooling/lib/container.py`
- Test: `tests/vergil_tooling/test_container.py`

- [ ] **Step 1: Write failing tests for `env_prefixes` parameter**

In `tests/vergil_tooling/test_container.py`, replace `test_build_docker_args_env_passthrough` (lines 224-230) with:

```python
def test_build_docker_args_env_passthrough(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "GH_TOKEN": "tok", "OTHER": "x"}
    with patch.dict("os.environ", env, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"], env_prefixes=("MQ_",))
    assert "MQ_HOST" in args
    assert "GH_TOKEN" not in args
    assert "OTHER" not in args
```

Add a new test after it:

```python
def test_build_docker_args_no_prefixes_no_passthrough(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "GH_TOKEN": "tok", "GITHUB_SHA": "abc"}
    with patch.dict("os.environ", env, clear=True):
        args = build_docker_args(tmp_path, "img:1", ["cmd"])
    assert "MQ_HOST" not in args
    assert "GH_TOKEN" not in args
    assert "GITHUB_SHA" not in args
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_container.py::test_build_docker_args_env_passthrough tests/vergil_tooling/test_container.py::test_build_docker_args_no_prefixes_no_passthrough -v 2>&1 | tail -20`

Expected: `test_build_docker_args_env_passthrough` fails (unexpected keyword argument `env_prefixes`). `test_build_docker_args_no_prefixes_no_passthrough` fails (MQ_HOST and GH_TOKEN still appear because of hardcoded prefixes).

- [ ] **Step 3: Implement `env_prefixes` parameter**

In `src/vergil_tooling/lib/container.py`:

Add import at the top, after the existing imports:

```python
from collections.abc import Sequence
```

Update `build_container_args` signature (lines 121-128) to add `env_prefixes`:

```python
def build_container_args(
    repo_root: Path,
    image: str,
    command: list[str],
    *,
    runtime: str = "docker",
    pull_policy: str = "always",
    env_prefixes: Sequence[str] = (),
) -> list[str]:
```

Replace the hardcoded passthrough block (lines 162-164):

```python
    for name in os.environ:
        if name.startswith(("MQ_", "GH_", "GITHUB_")):
            container_args.extend(["-e", name])
```

With:

```python
    if env_prefixes:
        prefixes = tuple(env_prefixes)
        for name in os.environ:
            if name.startswith(prefixes):
                container_args.extend(["-e", name])
```

Update `build_docker_args` (lines 183-193) to forward `env_prefixes`:

```python
def build_docker_args(
    repo_root: Path,
    image: str,
    command: list[str],
    *,
    pull_policy: str = "always",
    env_prefixes: Sequence[str] = (),
) -> list[str]:
    """Build the container ``run`` argument list (legacy alias)."""
    return build_container_args(
        repo_root,
        image,
        command,
        runtime="docker",
        pull_policy=pull_policy,
        env_prefixes=env_prefixes,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_container.py -v 2>&1 | tail -30`

Expected: All tests pass.

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/lib/container.py tests/vergil_tooling/test_container.py
vrg-commit --type feat --scope container --message "make env-var passthrough configurable via env_prefixes parameter (#777)" --body "Ref #777"
```

---

### Task 3: Wire `vrg_container_run.py` to config

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_container_run.py`
- Test: `tests/vergil_tooling/test_vrg_container_run.py`

- [ ] **Step 1: Write failing test for updated help text**

In `tests/vergil_tooling/test_vrg_container_run.py`, update `test_help_flag` (lines 19-23):

```python
def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: vrg-container-run" in out
    assert "GH_TOKEN" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vrg_container_run.py::test_help_flag -v 2>&1 | tail -10`

Expected: FAIL — help text still contains `GH_TOKEN`.

- [ ] **Step 3: Implement config reading and update help text**

In `src/vergil_tooling/bin/vrg_container_run.py`:

Add import after the existing imports (line 14):

```python
from vergil_tooling.lib.config import container_env_prefixes
```

Update the `_USAGE` string — remove the `GH_TOKEN` line (line 39). The environment variables block becomes:

```python
environment variables:
  DOCKER_DEV_IMAGE        override the auto-detected container image
  DOCKER_NETWORK          join a Docker network (e.g. for integration tests)
  VRG_DOCKER_INSTALL_TAG   override the vergil-tooling version tag from vergil.toml
```

Update the `build_container_args` call (lines 111-112) to pass `env_prefixes`:

```python
    env_prefixes = container_env_prefixes(repo_root)
    pull_policy = "never" if image_source == "cached" else "always"
    container_args = build_container_args(
        repo_root,
        image,
        command,
        runtime=runtime,
        pull_policy=pull_policy,
        env_prefixes=env_prefixes,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vrg_container_run.py -v 2>&1 | tail -30`

Expected: All tests pass. Existing tests that don't set up `vergil.toml` get empty prefixes via `container_env_prefixes()` catching `FileNotFoundError` — no behavior change for them.

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/bin/vrg_container_run.py tests/vergil_tooling/test_vrg_container_run.py
vrg-commit --type feat --scope container --message "wire vrg-container-run to [container].env-prefixes config (#777)" --body "Ref #777"
```

---

### Task 4: Wire `vrg_container_test.py` to config

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_container_test.py`
- Test: `tests/vergil_tooling/test_vrg_container_test.py`

- [ ] **Step 1: Write failing test for env_prefixes passthrough**

In `tests/vergil_tooling/test_vrg_container_test.py`, update `test_build_container_args_mq_env` (lines 96-102):

```python
def test_build_container_args_mq_env(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "MQ_PORT": "1414"}
    with patch.dict("os.environ", env, clear=True):
        args = build_test_container_args(
            tmp_path, "python", runtime="docker", env_prefixes=("MQ_",)
        )
    assert "-e" in args
    assert "MQ_HOST" in args
    assert "MQ_PORT" in args
```

Add a new test after it:

```python
def test_build_container_args_no_prefixes_no_mq_env(tmp_path: Path) -> None:
    env = {"MQ_HOST": "localhost", "MQ_PORT": "1414"}
    with patch.dict("os.environ", env, clear=True):
        args = build_test_container_args(tmp_path, "python", runtime="docker")
    assert "MQ_HOST" not in args
    assert "MQ_PORT" not in args
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_vrg_container_test.py::test_build_container_args_mq_env tests/vergil_tooling/test_vrg_container_test.py::test_build_container_args_no_prefixes_no_mq_env -v 2>&1 | tail -15`

Expected: `test_build_container_args_mq_env` fails (unexpected keyword `env_prefixes`). `test_build_container_args_no_prefixes_no_mq_env` fails (MQ vars still appear).

- [ ] **Step 3: Implement env_prefixes in build_test_container_args and main()**

In `src/vergil_tooling/bin/vrg_container_test.py`:

Add imports. After the existing imports (around line 9):

```python
from collections.abc import Sequence
```

After the `from vergil_tooling.lib import git` import:

```python
from vergil_tooling.lib.config import container_env_prefixes
```

Update `build_test_container_args` signature (line 30) to accept `env_prefixes`:

```python
def build_test_container_args(
    repo_root: Path,
    lang: str,
    *,
    runtime: str = "docker",
    env_prefixes: Sequence[str] = (),
) -> list[str]:
```

Update the `build_container_args` call at the end of the function (line 51) to forward `env_prefixes`:

```python
    return build_container_args(
        repo_root, image, ["bash", "-c", test_cmd], runtime=runtime, env_prefixes=env_prefixes
    )
```

Update `main()` (around line 89) to read config and pass prefixes:

```python
    env_prefixes = container_env_prefixes(repo_root)
    container_args = build_test_container_args(
        repo_root, lang, runtime=runtime, env_prefixes=env_prefixes
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vrg_container_test.py -v 2>&1 | tail -30`

Expected: All tests pass.

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/bin/vrg_container_test.py tests/vergil_tooling/test_vrg_container_test.py
vrg-commit --type feat --scope container --message "wire vrg-container-test to [container].env-prefixes config (#777)" --body "Ref #777"
```

---

### Task 5: Wire `vrg_container_docs.py` to config

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_container_docs.py`
- Test: `tests/vergil_tooling/test_vrg_container_docs.py`

- [ ] **Step 1: Write failing test for env_prefixes being passed**

In `tests/vergil_tooling/test_vrg_container_docs.py`, add this test after `test_build_no_port_splice`:

```python
def test_env_prefixes_passed_to_build_container_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch(
            "vergil_tooling.bin.vrg_container_docs.container_env_prefixes",
            return_value=["MQ_"],
        ),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])
    assert mock_build.call_args[1]["env_prefixes"] == ["MQ_"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_vrg_container_docs.py::test_env_prefixes_passed_to_build_container_args -v 2>&1 | tail -10`

Expected: FAIL — `container_env_prefixes` is not imported in `vrg_container_docs.py` yet.

- [ ] **Step 3: Implement config reading**

In `src/vergil_tooling/bin/vrg_container_docs.py`:

Add import after the existing imports (around line 9):

```python
from vergil_tooling.lib.config import container_env_prefixes
```

Update the `build_container_args` call in `main()` (lines 94-99) to pass `env_prefixes`:

```python
    env_prefixes = container_env_prefixes(repo_root)
    container_args = build_container_args(
        repo_root,
        image,
        ["bash", "-c", container_cmd],
        runtime=runtime,
        env_prefixes=env_prefixes,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_vrg_container_docs.py -v 2>&1 | tail -30`

Expected: All tests pass.

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/bin/vrg_container_docs.py tests/vergil_tooling/test_vrg_container_docs.py
vrg-commit --type feat --scope container --message "wire vrg-container-docs to [container].env-prefixes config (#777)" --body "Ref #777"
```

---

### Task 6: Update documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Remove MQ_* passthrough documentation from CLAUDE.md**

In `CLAUDE.md`, in the "Docker Dev Images" section under "Environment overrides", remove the line:

```
- `MQ_*` env vars are automatically passed through to the container
```

That bullet list becomes:

```markdown
- `DOCKER_DEV_IMAGE` — override the container image
- `DOCKER_TEST_CMD` — override the test command
- `DOCKER_NETWORK` — join a Docker network (e.g., for integration tests)
```

- [ ] **Step 2: Run full validation**

Run: `vrg-container-run -- uv run vrg-validate`

Expected: All checks pass.

- [ ] **Step 3: Commit**

```
vrg-git add CLAUDE.md
vrg-commit --type docs --scope container --message "remove hardcoded MQ_* passthrough documentation (#777)" --body "Ref #777"
```
