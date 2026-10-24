"""Tests for vergil_tooling.bin.vrg_container_docs."""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin import vrg_container_docs
from vergil_tooling.bin.vrg_container_docs import main
from vergil_tooling.lib.github import GitHubAPIError

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_container_docs"
_real_stage_sources = vrg_container_docs._stage_sources


@pytest.fixture(autouse=True)
def stub_staging() -> Iterator[MagicMock]:
    """Stand in for host-side staging in the container-command tests.

    Staging itself is exercised against a real git repo in the
    ``_stage_sources`` tests at the end of this module.
    """

    def _staged(_root: Path, config: str) -> str:
        return str(Path(config).with_name("mkdocs.staged.yml"))

    with patch(f"{_MOD}._stage_sources", side_effect=_staged) as mock:
        yield mock


def test_banner_flushed_before_exec_when_stdout_is_not_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #3396: a pipe or file block-buffers stdout, and execvp replaces the
    # process without flushing it, so the banner must be flushed before exec.
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8"))
    written_at_exec: list[str] = []

    def _exec(_prog: str, _args: list[str]) -> None:
        written_at_exec.append(raw.getvalue().decode())

    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp", side_effect=_exec),
        patch.dict("os.environ", {}, clear=True),
    ):
        main(["build"])

    assert len(written_at_exec) == 1
    assert "Image:   " in written_at_exec[0]
    assert written_at_exec[0].rstrip().endswith("---")


def test_no_args() -> None:
    assert main([]) == 1


def test_help_prints_usage(capsys: pytest.CaptureFixture[str]) -> None:
    for flag in ("-h", "--help"):
        assert main([flag]) == 0
        assert "Usage: vrg-container-docs" in capsys.readouterr().out


def test_unknown_command() -> None:
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=Path("/repo")),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
    ):
        assert main(["unknown"]) == 1


def test_serve_execvp(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert args[0] == "docker"
    assert "-p" in args
    assert "8000:8000" in args
    assert "mkdocs serve" in args[-1]


def test_serve_execvp_nerdctl(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="nerdctl"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "nerdctl",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])
    mock_exec.assert_called_once()
    assert mock_exec.call_args[0][0] == "nerdctl"


def test_build_execvp(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
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
    mock_exec.assert_called_once()
    args = mock_exec.call_args[0][1]
    assert "-p" not in args
    assert "mkdocs build" in args[-1]


def test_serve_with_extra_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["serve", "--strict"])
    cmd_passed = mock_build.call_args[0][2]
    assert "--strict" in cmd_passed[2]


def test_custom_env_vars(tmp_path: Path, stub_staging: MagicMock) -> None:
    env = {
        "DOCKER_DOCS_IMAGE": "my-docs:1",
        "MKDOCS_CONFIG": "custom.yml",
        "DOCS_PORT": "9000",
    }
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", env, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            "my-docs:1",
            "bash",
            "-c",
            "placeholder",
        ]
        main(["serve"])
    assert mock_build.call_args[0][1] == "my-docs:1"
    cmd = mock_build.call_args[0][2]
    # The custom config is staged, and mkdocs runs on its staged sibling.
    assert stub_staging.call_args.args[1] == "custom.yml"
    assert "-f mkdocs.staged.yml" in cmd[2]
    args = mock_exec.call_args[0][1]
    assert "9000:8000" in args


def test_python_repo_uv_sync(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])
    cmd = mock_build.call_args[0][2]
    assert "uv sync --group docs && uv run" in cmd[2]


def test_non_python_repo_no_uv(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])
    cmd = mock_build.call_args[0][2]
    assert "uv" not in cmd[2]


def test_cli_prefix_used(tmp_path: Path) -> None:
    dev_image = "ghcr.io/vergil-project/dev-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", dev_image, "bash", "-c", "x"]
        main(["--prefix", "dev", "serve"])
    assert mock_build.call_args[0][1] == dev_image


def test_invalid_prefix() -> None:
    assert main(["--prefix", "staging", "serve"]) == 1


def test_prefix_missing_value() -> None:
    assert main(["--prefix"]) == 1


def test_build_delegates_to_build_container_args(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
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

    call_args = mock_build.call_args
    assert call_args[0][0] == tmp_path
    assert call_args[0][1] == image
    assert call_args[0][2] == ["bash", "-c", "mkdocs build -f docs/site/mkdocs.staged.yml"]


def test_serve_splices_port_before_image(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])

    args = mock_exec.call_args[0][1]
    image_idx = args.index(image)
    p_idx = args.index("-p")
    assert p_idx < image_idx
    assert args[p_idx + 1] == "8000:8000"


def test_serve_custom_port(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {"DOCS_PORT": "9000"}, clear=True),
    ):
        mock_build.return_value = [
            "docker",
            "run",
            "--rm",
            image,
            "bash",
            "-c",
            "mkdocs serve -f docs/site/mkdocs.yml -a 0.0.0.0:8000",
        ]
        main(["serve"])

    args = mock_exec.call_args[0][1]
    assert "9000:8000" in args


def test_build_no_port_splice(tmp_path: Path) -> None:
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
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

    args = mock_exec.call_args[0][1]
    assert "-p" not in args


def test_python_repo_uv_sync_in_command(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    image = "ghcr.io/vergil-project/prod-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", image, "bash", "-c", "placeholder"]
        main(["build"])

    cmd = mock_build.call_args[0][2]
    assert cmd[0] == "bash"
    assert cmd[1] == "-c"
    assert "uv sync --group docs && uv run" in cmd[2]


def test_prefix_passed_to_image(tmp_path: Path) -> None:
    dev_image = "ghcr.io/vergil-project/dev-base:latest"
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.build_container_args") as mock_build,
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp"),
        patch.dict("os.environ", {}, clear=True),
    ):
        mock_build.return_value = ["docker", "run", "--rm", dev_image, "bash", "-c", "x"]
        main(["--prefix", "dev", "serve"])

    assert mock_build.call_args[0][1] == dev_image


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


def _docker_args_for(tmp_path: Path, command: str) -> list[str]:
    """Run main() with the real build_container_args; return the execvp args."""
    with (
        patch("vergil_tooling.bin.vrg_container_docs.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_container_docs.detect_runtime", return_value="docker"),
        patch("vergil_tooling.bin.vrg_container_docs.os.execvp") as mock_exec,
        patch.dict("os.environ", {}, clear=True),
    ):
        main([command])
    mock_exec.assert_called_once()
    return list(mock_exec.call_args[0][1])


def test_build_sets_no_mkdocs_2_warning(tmp_path: Path) -> None:
    args = _docker_args_for(tmp_path, "build")
    idx = args.index("NO_MKDOCS_2_WARNING=1")
    assert args[idx - 1] == "-e"
    # A `docker run` option, so it must precede the image.
    assert idx < args.index("ghcr.io/vergil-project/prod-base:latest")


def test_serve_sets_no_mkdocs_2_warning(tmp_path: Path) -> None:
    args = _docker_args_for(tmp_path, "serve")
    idx = args.index("NO_MKDOCS_2_WARNING=1")
    assert args[idx - 1] == "-e"
    assert idx < args.index("ghcr.io/vergil-project/prod-base:latest")


# -- the interpreter source (vergil-tooling#3414) -------------------------------


def test_python_repo_only_managed_pins_the_image_python(tmp_path: Path) -> None:
    # Regression for #3414: a repo with python-preference = "only-managed" must
    # not make uv in the docs image fetch (or fail to find) a managed
    # interpreter. UV_PYTHON_PREFERENCE=only-system pins the image's Python,
    # matching the CI docs jobs (vergil-actions#1002); the env var beats the
    # repo's [tool.uv] setting. No fallback: if the image's Python misses
    # requires-python, uv fails loudly.
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n\n[tool.uv]\npython-preference = "only-managed"\n'
        'python-downloads = "manual"\n'
    )
    for command in ("build", "serve"):
        args = _docker_args_for(tmp_path, command)
        idx = args.index("UV_PYTHON_PREFERENCE=only-system")
        assert args[idx - 1] == "-e"
        assert idx < args.index("ghcr.io/vergil-project/prod-base:latest")
        # A docs build resolves no version, so uv is not handed one.
        assert not any(a.startswith("UV_PYTHON=") for a in args)
        assert "NO_MKDOCS_2_WARNING=1" in args


def test_non_python_repo_sets_no_interpreter_source(tmp_path: Path) -> None:
    # Without a pyproject.toml the docs build never runs uv.
    args = _docker_args_for(tmp_path, "build")
    assert not any(a.startswith("UV_PYTHON") for a in args)


# -- staging the docs sources as CI does (vergil-tooling#3439) -----------------

_MKDOCS_YML = """\
site_name: Demo
docs_dir: docs
nav:
  - Home: index.md
  - Releases:
      - Changelog: changelog.md
      - Release Notes:
          - releases/index.md
"""

_STAGED_IGNORES = "docs/site/docs/changelog.md\ndocs/site/docs/releases/\nmkdocs.staged.yml\n"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout


def _docs_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    gitignore: str = _STAGED_IGNORES,
    changelog: bool = True,
) -> Path:
    """A committed repo laid out like a fleet repo, with the cwd inside it."""
    repo = tmp_path / "repo"
    (repo / "docs" / "site" / "docs").mkdir(parents=True)
    (repo / "docs" / "site" / "mkdocs.yml").write_text(_MKDOCS_YML)
    (repo / "docs" / "site" / "docs" / "index.md").write_text("# Demo\n")
    (repo / "releases").mkdir()
    (repo / "releases" / "v1.0.0.md").write_text("# Release 1.0.0 (2026-01-01)\n")
    (repo / "releases" / "v1.1.0.md").write_text("# Release 1.1.0 (2026-02-01)\n")
    if changelog:
        (repo / "CHANGELOG.md").write_text("# Changelog\n")
    if gitignore:
        (repo / ".gitignore").write_text(gitignore)
    _git(repo, "init", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    monkeypatch.chdir(repo)
    return repo


@pytest.fixture
def no_evidence() -> Iterator[MagicMock]:
    """Stub vrg-docs-stage's GitHub lookups: repo o/r, no release evidence."""
    with (
        patch("vergil_tooling.bin.vrg_docs_stage.github.current_repo", return_value="o/r"),
        patch("vergil_tooling.bin.vrg_docs_stage.github.read_json", return_value=[]) as mock,
    ):
        yield mock


def test_stage_sources_stages_like_ci_without_dirtying_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_evidence: MagicMock
) -> None:
    repo = _docs_repo(tmp_path, monkeypatch)
    staged = _real_stage_sources(repo, "docs/site/mkdocs.yml")

    assert staged == "docs/site/mkdocs.staged.yml"
    docs = repo / "docs" / "site" / "docs"
    assert (docs / "changelog.md").read_text() == "# Changelog\n"
    assert (docs / "releases" / "v1.0.0.md").is_file()
    assert (docs / "releases" / "v1.1.0.md").is_file()
    assert "1.1.0" in (docs / "releases" / "index.md").read_text()
    # The release versions are patched into the staged copy's nav ...
    staged_text = (repo / staged).read_text()
    assert "- 1.1.0: releases/v1.1.0.md" in staged_text
    assert "- 1.0.0: releases/v1.0.0.md" in staged_text
    # ... never into the tracked config, and nothing staged shows as a change.
    assert (repo / "docs" / "site" / "mkdocs.yml").read_text() == _MKDOCS_YML
    assert _git(repo, "status", "--porcelain") == ""
    no_evidence.assert_called_once()


def test_stage_sources_refuses_unignored_paths_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _docs_repo(tmp_path, monkeypatch, gitignore="", changelog=False)
    with patch(f"{_MOD}.vrg_docs_stage.main") as mock_stage:
        assert _real_stage_sources(repo, "docs/site/mkdocs.yml") is None

    mock_stage.assert_not_called()
    err = capsys.readouterr().err
    assert "docs/site/mkdocs.staged.yml" in err
    assert "docs/site/docs/releases/index.md" in err
    assert "docs/site/docs/releases/v1.0.0.md" in err
    # No CHANGELOG.md, so no staged changelog is written or checked.
    assert "changelog.md" not in err
    assert "vrg-git rm --cached" in err
    assert "vrg-gitignore-sync" in err
    assert not (repo / "docs" / "site" / "mkdocs.staged.yml").exists()
    assert _git(repo, "status", "--porcelain") == ""


def test_stage_sources_refuses_a_tracked_staged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A repo that commits a staged copy (e.g. releases/index.md) is refused
    # even once the managed patterns are in its .gitignore: staging would
    # rewrite a tracked file.
    repo = _docs_repo(tmp_path, monkeypatch, gitignore="")
    tracked = repo / "docs" / "site" / "docs" / "releases" / "index.md"
    tracked.parent.mkdir()
    tracked.write_text("placeholder\n")
    (repo / ".gitignore").write_text(_STAGED_IGNORES)
    _git(repo, "add", "-f", ".gitignore", str(tracked))
    _git(repo, "commit", "-m", "track a staged copy")

    assert _real_stage_sources(repo, "docs/site/mkdocs.yml") is None
    err = capsys.readouterr().err
    assert "docs/site/docs/releases/index.md" in err
    assert "docs/site/mkdocs.staged.yml" not in err
    assert tracked.read_text() == "placeholder\n"


def test_stage_sources_missing_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _real_stage_sources(tmp_path, "docs/site/mkdocs.yml") is None
    assert "mkdocs config not found: docs/site/mkdocs.yml" in capsys.readouterr().err


def test_stage_sources_fails_loudly_without_gh_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Release pages need gh for their CI-evidence links; missing auth fails the
    # command rather than skipping staging.
    repo = _docs_repo(tmp_path, monkeypatch)
    err = GitHubAPIError(
        1, "gh api", stderr="gh: To use GitHub CLI, populate the GH_TOKEN environment variable"
    )
    with (
        patch("vergil_tooling.bin.vrg_docs_stage.github.current_repo", return_value="o/r"),
        patch("vergil_tooling.bin.vrg_docs_stage.github.read_json", side_effect=err),
    ):
        assert _real_stage_sources(repo, "docs/site/mkdocs.yml") is None
    assert "GH_TOKEN" in capsys.readouterr().err
    assert not (repo / "docs" / "site" / "mkdocs.staged.yml").exists()


def test_stage_sources_nav_patch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_evidence: MagicMock
) -> None:
    repo = _docs_repo(tmp_path, monkeypatch)
    with patch(f"{_MOD}.vrg_docs_patch_nav.main", return_value=1):
        assert _real_stage_sources(repo, "docs/site/mkdocs.yml") is None


def test_main_stages_then_patches_nav_then_runs_mkdocs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub_staging: MagicMock,
    no_evidence: MagicMock,
) -> None:
    repo = _docs_repo(tmp_path, monkeypatch)
    stub_staging.side_effect = _real_stage_sources
    calls: list[str] = []
    real_stage = vrg_container_docs.vrg_docs_stage.main
    real_nav = vrg_container_docs.vrg_docs_patch_nav.main

    def _stage(argv: list[str]) -> int:
        calls.append("stage")
        return real_stage(argv)

    def _nav(argv: list[str]) -> int:
        calls.append("patch-nav")
        return real_nav(argv)

    def _exec(_prog: str, args: list[str]) -> None:
        calls.append(args[-1])

    for command in ("build", "serve"):
        calls.clear()
        with (
            patch(f"{_MOD}.git.repo_root", return_value=repo),
            patch(f"{_MOD}.detect_runtime", return_value="docker"),
            patch(f"{_MOD}.vrg_docs_stage.main", side_effect=_stage),
            patch(f"{_MOD}.vrg_docs_patch_nav.main", side_effect=_nav),
            patch(f"{_MOD}.os.execvp", side_effect=_exec),
            # Keep PATH: staging runs git for real.
            patch.dict("os.environ", {"PATH": os.environ["PATH"]}, clear=True),
        ):
            main([command])
        assert calls[:2] == ["stage", "patch-nav"]
        assert calls[2].startswith(f"mkdocs {command} -f docs/site/mkdocs.staged.yml")
    assert _git(repo, "status", "--porcelain") == ""


def test_main_staging_failure_skips_the_container(tmp_path: Path, stub_staging: MagicMock) -> None:
    stub_staging.side_effect = None
    stub_staging.return_value = None
    with (
        patch(f"{_MOD}.git.repo_root", return_value=tmp_path),
        patch(f"{_MOD}.os.execvp") as mock_exec,
    ):
        assert main(["build"]) == 1
    mock_exec.assert_not_called()
