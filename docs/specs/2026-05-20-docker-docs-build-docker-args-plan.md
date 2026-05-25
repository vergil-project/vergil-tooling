# vrg-container-docs build_docker_args Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the hardcoded `mq-rest-admin-common` mount in `vrg-container-docs` by delegating to the shared `build_docker_args()` function, bringing it in line with every other `vrg-docker-*` command.

**Architecture:** Replace the hand-built `docker run` argument list in `vrg_container_docs.py` with a call to `build_docker_args()` from `vergil_tooling.lib.docker`. Port mapping for `serve` is spliced into the returned list before the image entry, using the same `index(image)` pattern as `vrg_scorecard.py`. The hardcoded `mq-rest-admin-common` mount is deleted.

**Tech Stack:** Python 3.14, pytest, unittest.mock

**Spec:** `docs/specs/2026-05-20-docker-docs-build-docker-args-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `src/vergil_tooling/bin/vrg_container_docs.py` | Modify | Replace hand-built args with `build_docker_args()`, splice port mapping, remove hardcoded mount |
| `tests/vergil_tooling/test_vrg_container_docs.py` | Modify | Update tests for new arg structure, delete sibling mount test, add extra-volumes and worktree tests |

No new files. No changes to `docker.py` or other commands.

---

### Task 1: Update tests to mock `build_docker_args`

The existing tests call `main()` with a real `build_docker_args`-free code path. After the refactor, `main()` will call `build_docker_args()`. We need tests that mock it (like the scorecard tests do) and verify `vrg_container_docs`'s own logic: port splicing, `uv sync` wrapping, prefix handling, and the removal of the sibling mount.

**Files:**
- Modify: `tests/vergil_tooling/test_vrg_container_docs.py`

- [ ] **Step 1: Write the failing test for `build_docker_args` delegation**

Add a test that mocks `build_docker_args` and verifies `main(["build"])` passes the correct arguments to it. This test will fail because the current code doesn't call `build_docker_args`.

```python
def test_build_delegates_to_build_docker_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])

    call_args = mock_build.call_args
    assert call_args[0][0] == tmp_path
    assert call_args[0][1] == image
    assert call_args[0][2] == ["bash", "-c", "mkdocs build -f docs/site/mkdocs.yml"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_container_docs.py::test_build_delegates_to_build_docker_args -v`

Expected: FAIL — `build_docker_args` is not imported in `vrg_container_docs`, so the patch target doesn't exist.

- [ ] **Step 3: Write the failing test for serve port splicing**

```python
def test_serve_splices_port_before_image(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])

    args = mock_exec.call_args[0][1]
    image_idx = args.index(image)
    p_idx = args.index("-p")
    assert p_idx < image_idx
    assert args[p_idx + 1] == "8000:8000"
```

- [ ] **Step 4: Write the failing test for custom port splicing**

```python
def test_serve_custom_port(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {"DOCS_PORT": "9000"}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])

    args = mock_exec.call_args[0][1]
    assert "9000:8000" in args
```

- [ ] **Step 5: Write the failing test for build with no port**

```python
def test_build_no_port_splice(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])

    args = mock_exec.call_args[0][1]
    assert "-p" not in args
```

- [ ] **Step 6: Write the failing test for Python repo uv sync wrapping**

```python
def test_python_repo_uv_sync_in_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])

    cmd = mock_build.call_args[0][2]
    assert cmd[0] == "bash"
    assert cmd[1] == "-c"
    assert "uv sync --group docs && uv run" in cmd[2]
```

- [ ] **Step 7: Write the failing test for prefix passthrough**

```python
def test_prefix_passed_to_image(tmp_path: Path) -> None:
    dev_image = "ghcr.io/vergil-project/dev-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", dev_image, "bash", "-c", "x"]
        main(["--prefix", "dev", "serve"])

    assert mock_build.call_args[0][1] == dev_image
```

- [ ] **Step 8: Commit new tests (they will fail until Task 2)**

Commit message: `test(638): add tests for build_docker_args delegation in vrg-container-docs`

---

### Task 2: Refactor `vrg_container_docs.py` to use `build_docker_args`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_container_docs.py:14,93-113`

- [ ] **Step 1: Update the import**

Replace line 14:

```python
from vergil_tooling.lib.docker import docker_platform
```

with:

```python
from vergil_tooling.lib.docker import build_docker_args
```

The `docker_platform` import is no longer needed — `build_docker_args` calls it internally.

- [ ] **Step 2: Replace lines 93-113 with `build_docker_args` call and port splice**

Replace the entire block from `docker_args = [` through `docker_args.extend(["bash", "-c", container_cmd])` with:

```python
    docker_args = build_docker_args(
        repo_root, image, ["bash", "-c", container_cmd],
    )

    if command == "serve":
        idx = docker_args.index(image)
        docker_args[idx:idx] = ["-p", f"{port}:8000"]
```

This replaces 21 lines (the hand-built list, the serve port extend, the hardcoded `mq-rest-admin-common` mount, and the image/command append) with 6 lines.

The `index(image)` splice pattern is the same one `vrg_scorecard.py:78-79` uses.

- [ ] **Step 3: Run new tests from Task 1 to verify they pass**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_container_docs.py::test_build_delegates_to_build_docker_args tests/vergil_tooling/test_vrg_container_docs.py::test_serve_splices_port_before_image tests/vergil_tooling/test_vrg_container_docs.py::test_build_no_port_splice tests/vergil_tooling/test_vrg_container_docs.py::test_python_repo_uv_sync_in_command tests/vergil_tooling/test_vrg_container_docs.py::test_prefix_passed_to_image tests/vergil_tooling/test_vrg_container_docs.py::test_serve_custom_port -v`

Expected: all PASS.

- [ ] **Step 4: Commit implementation**

Commit message: `feat(638): replace hand-built docker args with build_docker_args in vrg-container-docs`

---

### Task 3: Update and clean up existing tests

The original tests mock at a lower level (`os.execvp` and `os.environ`) and assert on the raw arg list. Some will break because the arg structure changed (e.g., `--pull=always` is now present, `docker_platform` is no longer imported directly). Update them to either mock `build_docker_args` or adjust assertions.

**Files:**
- Modify: `tests/vergil_tooling/test_vrg_container_docs.py`

- [ ] **Step 1: Delete `test_common_sibling_mount`**

Remove the entire test (lines 119-132). The behavior it tests no longer exists.

- [ ] **Step 2: Update `test_serve_execvp`**

The test patches `os.execvp` and checks raw args. After the refactor, `build_docker_args` is called internally. The simplest fix: mock `build_docker_args` to return a controlled list, then verify the port splice and final `execvp` call. Replace:

```python
def test_serve_execvp(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["serve"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert any(a.startswith("--platform=linux/") for a in args)
    assert "-p" in args
    assert "8000:8000" in args
    assert "mkdocs serve" in args[-1]
```

with:

```python
def test_serve_execvp(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert args[0] == "docker"
    assert "-p" in args
    assert "8000:8000" in args
    assert "mkdocs serve" in args[-1]
```

- [ ] **Step 3: Update `test_build_execvp`**

Replace:

```python
def test_build_execvp(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert "-p" not in args
    assert "mkdocs build" in args[-1]
```

with:

```python
def test_build_execvp(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", image, "bash", "-c",
            "mkdocs build -f docs/site/mkdocs.yml",
        ]
        main(["build"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert "-p" not in args
    assert "mkdocs build" in args[-1]
```

- [ ] **Step 4: Update `test_serve_with_extra_args`**

Replace:

```python
def test_serve_with_extra_args(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["serve", "--strict"])
    container_cmd = mock_exec.call_args[0][1][-1]
    assert "--strict" in container_cmd
```

with:

```python
def test_serve_with_extra_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["serve", "--strict"])
    cmd_passed = mock_build.call_args[0][2]
    assert "--strict" in cmd_passed[2]
```

- [ ] **Step 5: Update `test_custom_env_vars`**

Replace:

```python
def test_custom_env_vars(tmp_path: Path) -> None:
    env = {
        "DOCKER_DOCS_IMAGE": "my-docs:1",
        "MKDOCS_CONFIG": "custom.yml",
        "DOCS_PORT": "9000",
    }
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        main(["serve"])
    args = mock_exec.call_args[0][1]
    assert "my-docs:1" in args
    assert "9000:8000" in args
    assert "custom.yml" in args[-1]
```

with:

```python
def test_custom_env_vars(tmp_path: Path) -> None:
    env = {
        "DOCKER_DOCS_IMAGE": "my-docs:1",
        "MKDOCS_CONFIG": "custom.yml",
        "DOCS_PORT": "9000",
    }
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_build.return_value = [
            "docker", "run", "--rm", "my-docs:1", "bash", "-c", "placeholder",
        ]
        main(["serve"])
    assert mock_build.call_args[0][1] == "my-docs:1"
    cmd = mock_build.call_args[0][2]
    assert "custom.yml" in cmd[2]
    args = mock_exec.call_args[0][1]
    assert "9000:8000" in args
```

- [ ] **Step 6: Update `test_python_repo_uv_sync`**

Replace:

```python
def test_python_repo_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])
    container_cmd = mock_exec.call_args[0][1][-1]
    assert "uv sync --group docs && uv run" in container_cmd
```

with:

```python
def test_python_repo_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])
    cmd = mock_build.call_args[0][2]
    assert "uv sync --group docs && uv run" in cmd[2]
```

- [ ] **Step 7: Update `test_non_python_repo_no_uv`**

Replace:

```python
def test_non_python_repo_no_uv(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])
    container_cmd = mock_exec.call_args[0][1][-1]
    assert "uv" not in container_cmd
```

with:

```python
def test_non_python_repo_no_uv(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])
    cmd = mock_build.call_args[0][2]
    assert "uv" not in cmd[2]
```

- [ ] **Step 8: Update `test_cli_prefix_used`**

Replace:

```python
def test_cli_prefix_used(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["--prefix", "dev", "serve"])
    args = mock_exec.call_args[0][1]
    assert "ghcr.io/vergil-project/dev-base:latest" in args
```

with:

```python
def test_cli_prefix_used(tmp_path: Path) -> None:
    dev_image = "ghcr.io/vergil-project/dev-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.build_docker_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", dev_image, "bash", "-c", "x"]
        main(["--prefix", "dev", "serve"])
    assert mock_build.call_args[0][1] == dev_image
```

- [ ] **Step 9: Run the full test suite**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_container_docs.py -v`

Expected: all tests PASS, no test references `mq-rest-admin-common`.

- [ ] **Step 10: Commit test updates**

Commit message: `test(638): update existing tests to mock build_docker_args, remove sibling mount test`

---

### Task 4: Run full validation

**Files:** None (validation only)

- [ ] **Step 1: Run vrg-validate**

Run: `cd <worktree> && vrg-container-run -- uv run vrg-validate`

Expected: all checks pass (lint, typecheck, tests, audit, common checks).

- [ ] **Step 2: Fix any issues found**

If validation fails, fix the issue and re-run. Common things to watch for:
- Ruff may flag the removed `docker_platform` import if you forgot to remove it
- Mypy may flag type mismatches if `build_docker_args` return type doesn't match usage
- Unused import of `docker_platform` if not fully cleaned up

- [ ] **Step 3: Commit any fixes**

Commit message: `fix(638): address validation findings`
