"""Tests for vergil_tooling.bin.vrg_validate."""

from __future__ import annotations

import json
import os
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, mock_open, patch

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Sequence

import pytest

from vergil_tooling.bin.vrg_validate import (
    _CONAN_AUDIT_TOKEN_ENV,
    ValidationError,
    _build_stages,
    _check_custom_validator,
    _command_stage,
    _default_test_version,
    _docs_stage,
    _find_custom_validator,
    _in_dev_container,
    _install_stage,
    _interpreter_version,
    _not_bootstrapped_stage,
    _probe_xdist,
    _python_test_stage,
    _python_version_test_stage,
    _repo_files,
    _resolve_test_workers,
    _run_audit_command,
    _run_common_checks,
    _run_docs_build,
    _run_semgrep,
    _run_trivy,
    _semgrep_rulesets,
    _semgrep_stage,
    _skip_conan_audit_scan,
    _skip_rust_doctests_without_lib,
    _sources_without_manifest_stage,
    _stage_trivy_target,
    _test_context,
    _trivy_stage,
    check_rust_lockfile,
    main,
)
from vergil_tooling.lib import languages
from vergil_tooling.lib.config import ConfigError, TestConfig
from vergil_tooling.lib.container_cache import unbootstrapped_manifests
from vergil_tooling.lib.languages import RUST_DOCTEST_CMD, CheckKind, language_commands
from vergil_tooling.lib.validate_record import RECORD_PATH, read_record

_MOD = "vergil_tooling.bin.vrg_validate"


@pytest.fixture(autouse=True)
def _container_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The test-tuning env must not leak in from the runner (GitHub Actions sets
    # GITHUB_ACTIONS=true), and no full run may probe the real test environment.
    for name in ("VRG_TEST_WORKERS", "VRG_TEST_CONTEXT", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    # A run records the commit and tree it judged; the tests' repos are not git.
    with (
        patch(_MOD + "._in_dev_container", return_value=True),
        patch(_MOD + "._probe_xdist", return_value=False),
        patch(_MOD + ".git.commit_sha", return_value="0" * 40),
        patch(_MOD + ".git.working_tree_status", return_value=""),
        # The tests' repo roots hold no manifests; the bootstrap skip has its
        # own tests, which restore the real check.
        patch(_MOD + ".unbootstrapped_manifests", return_value=[]),
        # No full run may scan for real (semgrep needs its registry); the
        # semgrep stage has its own tests, which call the real function.
        patch(_MOD + "._run_semgrep"),
        # Nor run Trivy for real (it needs its vulnerability database); the
        # trivy stage has its own tests.
        patch(_MOD + "._run_trivy"),
        # Nor build docs for real; the docs stage has its own tests.
        patch(_MOD + "._run_docs_build"),
    ):
        yield


def _write_config(tmp_path: Path, language: str = "") -> None:
    lang_line = f'primary-language = "{language}"\n' if language else ""
    (tmp_path / "vergil.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        f'{lang_line}\n[dependencies]\nvergil = "v2.0"\n'
        f'\n[ci]\nversions = ["3.14"]\n'
    )


# -- Container guard ----------------------------------------------------------


def test_rejects_host_execution() -> None:
    with patch(_MOD + "._in_dev_container", return_value=False):
        assert main([]) == 1


# -- Stage construction --------------------------------------------------------


def test_build_stages_full_run_order() -> None:
    with (
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        m_cmds.side_effect = lambda lang, kind, **_kw: [["echo", kind.value]]
        stages = _build_stages(None, "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == [
        "common",
        "install",
        "lint",
        "typecheck",
        "build",
        "test",
        "audit",
        "semgrep",
        "trivy",
        "docs",
    ]
    assert stages[1].mode == "fail_fast"
    assert all(s.mode == "fail_defer" for s in stages if s.name != "install")


def test_build_stages_full_run_includes_custom() -> None:
    with (
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
    ):
        m_cmds.side_effect = lambda lang, kind, **_kw: [["echo", kind.value]]
        stages = _build_stages(None, "python", Path("/tmp/r"))  # noqa: S108
    assert stages[-1].name == "custom"
    assert stages[-1].mode == "fail_defer"


def test_build_stages_check_common_only() -> None:
    stages = _build_stages("common", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["common"]


def test_build_stages_check_lint_runs_install_then_lint() -> None:
    with patch(_MOD + ".language_commands") as m_cmds:
        m_cmds.side_effect = lambda lang, kind, **_kw: [["echo", kind.value]]
        stages = _build_stages("lint", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["install", "lint"]
    assert stages[0].mode == "fail_fast"


def test_build_stages_check_lint_no_install_commands() -> None:
    def mock_lang_cmds(lang: str, kind: CheckKind, **_kwargs: object) -> list[list[str]]:
        if kind == CheckKind.INSTALL:
            return []
        if kind == CheckKind.LINT:
            return [["ruff", "check", "src/"]]
        return []

    with patch(_MOD + ".language_commands", side_effect=mock_lang_cmds):
        stages = _build_stages("lint", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["lint"]


def test_build_stages_no_language_skips_language_checks() -> None:
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages(None, None, Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["common", "semgrep", "trivy", "docs"]


def test_build_stages_no_commands_for_check() -> None:
    with patch(_MOD + ".language_commands", return_value=[]):
        stages = _build_stages("lint", "python", Path("/tmp/r"))  # noqa: S108
    assert stages == []


# -- _command_stage ------------------------------------------------------------


def test_command_stage_fail_defer_runs_all_commands() -> None:
    with patch(_MOD + ".progress.run", side_effect=[1, 0, 2]) as m_run:
        stage = _command_stage("test", [["cmd1"], ["cmd2"], ["cmd3"]], mode="fail_defer")
        with pytest.raises(ValidationError, match="2 of 3 test command"):
            stage.fn(None)
    assert m_run.call_count == 3


def test_command_stage_fail_fast_stops_on_first_failure() -> None:
    with patch(_MOD + ".progress.run", return_value=1) as m_run:
        stage = _command_stage("install", [["cmd1"], ["cmd2"]], mode="fail_fast")
        with pytest.raises(ValidationError, match="1 of 2 install command"):
            stage.fn(None)
    assert m_run.call_count == 1


def test_command_stage_success_no_raise() -> None:
    with patch(_MOD + ".progress.run", return_value=0):
        stage = _command_stage("lint", [["cmd1"]], mode="fail_defer")
        stage.fn(None)  # does not raise


def test_command_stage_uses_the_given_runner() -> None:
    runner = MagicMock(side_effect=[0, 3])
    with patch(_MOD + ".progress.run") as m_run:
        stage = _command_stage("audit", [["a"], ["b"]], mode="fail_defer", run_cmd=runner)
        with pytest.raises(ValidationError, match="1 of 2 audit command"):
            stage.fn(None)
    assert [c.args[0] for c in runner.call_args_list] == [["a"], ["b"]]
    m_run.assert_not_called()


# -- _run_audit_command (issue #4423) -------------------------------------------


def test_run_audit_command_retries_pip_audit() -> None:
    cmd = ["pip-audit", "--format=json"]
    with (
        patch(_MOD + ".audit_retry.run_pip_audit", return_value=0) as m_retry,
        patch(_MOD + ".progress.run") as m_run,
    ):
        assert _run_audit_command(cmd) == 0
    m_retry.assert_called_once_with(cmd)
    m_run.assert_not_called()


def test_run_audit_command_runs_other_commands_once() -> None:
    cmd = ["pip-licenses", "--format=json"]
    with (
        patch(_MOD + ".audit_retry.run_pip_audit") as m_retry,
        patch(_MOD + ".progress.run", return_value=1) as m_run,
    ):
        assert _run_audit_command(cmd) == 1
    m_run.assert_called_once_with(cmd, check=False)
    m_retry.assert_not_called()


def test_python_audit_stage_retries_a_transient_pip_audit_timeout(tmp_path: Path) -> None:
    """A single PyPI timeout no longer fails ``--check audit`` (issue #4423)."""
    _write_config(tmp_path, "python")
    pip_audit_runs: list[bool] = []

    def mock_run(cmd: list[str], *, check: bool = True) -> int:
        if cmd[0] == "pip-audit":
            pip_audit_runs.append(check)
            if len(pip_audit_runs) == 1:
                raise subprocess.CalledProcessError(
                    1, cmd, stderr="requests.exceptions.ReadTimeout: read timeout=15"
                )
        return 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run", side_effect=mock_run),
        patch("vergil_tooling.lib.audit_retry.time.sleep") as m_sleep,
    ):
        result = main(["--check", "audit"])
    assert result == 0
    assert pip_audit_runs == [True, True]
    m_sleep.assert_called_once()


# -- main() wiring -------------------------------------------------------------


def test_check_common_runs_common_checks(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=0) as mock,
    ):
        result = main(["--check", "common"])
    assert result == 0
    mock.assert_called_once()


def test_check_lint_runs_install_then_lint(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    calls: list[str] = []

    def mock_run(cmd: list[str], *, check: bool = True) -> int:
        calls.append(" ".join(cmd))
        return 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run", side_effect=mock_run),
    ):
        result = main(["--check", "lint"])
    assert result == 0
    assert "uv sync --frozen --group dev" in calls
    assert "ruff check src/ tests/" in calls


def test_check_lint_no_commands_without_language(tmp_path: Path) -> None:
    _write_config(tmp_path)
    with patch(_MOD + ".git.repo_root", return_value=tmp_path):
        result = main(["--check", "lint"])
    assert result == 0


def test_run_all_executes_stages_in_order(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    order: list[str] = []

    def mock_common(repo_root: Path) -> int:
        order.append("common")
        return 0

    def mock_run(cmd: list[str], *, check: bool = True) -> int:
        order.append(cmd[-1])
        return 0

    def mock_lang_cmds(lang: str, kind: CheckKind, **_kwargs: object) -> list[list[str]]:
        return [["echo", kind.value]]

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", side_effect=mock_common),
        patch(_MOD + ".language_commands", side_effect=mock_lang_cmds),
        patch(_MOD + ".progress.run", side_effect=mock_run),
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 0
    assert order == ["common", "install", "lint", "typecheck", "build", "test", "audit"]


def test_run_all_common_failure_reported(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")

    def mock_run(cmd: list[str], *, check: bool = True) -> int:
        return 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=1),
        patch(_MOD + ".progress.run", side_effect=mock_run),
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 1


def test_run_all_language_failure_does_not_stop_later_checks(tmp_path: Path) -> None:
    """Intentional behavior change: lint failure no longer prevents typecheck/test/audit."""
    _write_config(tmp_path, "python")
    ran: list[str] = []

    def mock_lang_cmds(lang: str, kind: CheckKind, **_kwargs: object) -> list[list[str]]:
        return [["echo", kind.value]]

    def mock_run(cmd: list[str], *, check: bool = True) -> int:
        ran.append(cmd[-1])
        return 1 if cmd[-1] == "lint" else 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=0),
        patch(_MOD + ".language_commands", side_effect=mock_lang_cmds),
        patch(_MOD + ".progress.run", side_effect=mock_run),
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 1
    assert "typecheck" in ran
    assert "test" in ran
    assert "audit" in ran


def test_run_all_install_failure_stops_later_checks(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    ran: list[str] = []

    def mock_lang_cmds(lang: str, kind: CheckKind, **_kwargs: object) -> list[list[str]]:
        return [["echo", kind.value]]

    def mock_run(cmd: list[str], *, check: bool = True) -> int:
        ran.append(cmd[-1])
        return 1 if cmd[-1] == "install" else 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=0),
        patch(_MOD + ".language_commands", side_effect=mock_lang_cmds),
        patch(_MOD + ".progress.run", side_effect=mock_run),
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 1
    assert "lint" not in ran


def test_run_all_includes_custom_validator(tmp_path: Path) -> None:
    _write_config(tmp_path)
    ran: list[str] = []

    def mock_run(cmd: tuple[str, ...], *, check: bool = True) -> int:
        ran.append(cmd[0])
        return 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=0),
        patch(_MOD + ".progress.run", side_effect=mock_run),
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
        patch(_MOD + "._check_custom_validator"),
    ):
        result = main([])
    assert result == 0
    assert ran == ["/path/to/custom"]


def test_run_all_custom_validator_failure(tmp_path: Path) -> None:
    _write_config(tmp_path)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=0),
        patch(_MOD + ".progress.run", return_value=1),
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
        patch(_MOD + "._check_custom_validator"),
    ):
        result = main([])
    assert result == 1


def test_run_all_language_none_skips_language_checks(tmp_path: Path) -> None:
    _write_config(tmp_path)

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=0),
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        result = main([])
    assert result == 0


def test_main_runs_pipeline_with_command_name(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        result = main(["--check", "common"])
    assert result == 0
    kwargs = m_pipeline.call_args.kwargs
    assert kwargs["command"] == "vrg-validate"
    assert kwargs["repo_root"] == tmp_path


# -- Version resolution config check (epic vergil-project/.github#407) ---------


def _write_pinned_config(tmp_path: Path, *, repo_type: str, versions: str) -> None:
    (tmp_path / "vergil.toml").write_text(
        f'[project]\nrepository-type = "{repo_type}"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        'primary-language = "python"\n[dependencies]\nvergil = "v2.1"\n'
        f"[ci]\nversions = {versions}\n"
    )


def test_main_reports_resolved_versions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.12", "3.14"]')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0),
    ):
        assert main(["--check", "common"]) == 0
    assert "CI versions: 3.14, 3.12 (primary 3.14)" in capsys.readouterr().out


def test_main_pin_outside_window_fails_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The same resolver CI runs: a pin the window no longer builds fails here.
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.11"]')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main([]) == 1
    m_pipeline.assert_not_called()
    err = capsys.readouterr().err
    assert "ERROR: [ci].versions pins 3.11" in err
    assert "remove it from [ci].versions" in err


def test_main_app_multi_pin_fails_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # An app builds exactly one version: the same fatal rule vrg-ci-resolve applies.
    _write_pinned_config(tmp_path, repo_type="tooling", versions='["3.13", "3.14"]')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main([]) == 1
    m_pipeline.assert_not_called()
    err = capsys.readouterr().err
    assert "ERROR: [ci].versions pins 2 versions (3.14, 3.13)" in err
    assert "pin a single version" in err
    assert 'set [project].repository-type = "library"' in err


def test_main_floor_warning_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.14"]')
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.11"\n')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0),
    ):
        assert main(["--check", "common"]) == 0
    assert "WARNING: requires-python floor 3.11 is below" in capsys.readouterr().err


def test_main_toolchain_selection_fails_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.14"]')
    (tmp_path / ".python-version").write_text("3.14\n")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main([]) == 1
    m_pipeline.assert_not_called()
    assert "ERROR: .python-version selects the Python interpreter" in capsys.readouterr().err


def test_main_python_preference_passes_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # python-preference picks the interpreter's source, not its version (#3410).
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.14"]')
    (tmp_path / "pyproject.toml").write_text('[tool.uv]\npython-preference = "only-managed"\n')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main([]) == 0
    m_pipeline.assert_called_once()
    assert "ERROR" not in capsys.readouterr().err


def test_main_floor_check_failure_fails_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.14"]')
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = "nonsense"\n')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0),
    ):
        assert main([]) == 1
    assert "is not a valid version specifier" in capsys.readouterr().err


# -- Unit tests for internal functions ----------------------------------------


def test_in_dev_container_dockerenv() -> None:
    with patch(_MOD + ".Path.exists", return_value=True):
        assert _in_dev_container() is True


_OVERLAY_MOUNTINFO = (
    "22 1 0:21 / /proc rw,nosuid - proc proc rw\n"
    "448 242 0:45 / / rw,relatime - overlay overlay rw,lowerdir=...\n"
)

_HOST_MOUNTINFO = (
    "22 1 0:21 / /proc rw,nosuid - proc proc rw\n"
    "1 0 259:2 / / rw,relatime - ext4 /dev/nvme0n1p2 rw\n"
)


def test_in_dev_container_overlay_mountinfo() -> None:
    with (
        patch(_MOD + ".Path.exists", return_value=False),
        patch.object(Path, "open", mock_open(read_data=_OVERLAY_MOUNTINFO)),
    ):
        assert _in_dev_container() is True


def test_in_dev_container_host_mountinfo() -> None:
    with (
        patch(_MOD + ".Path.exists", return_value=False),
        patch.object(Path, "open", mock_open(read_data=_HOST_MOUNTINFO)),
    ):
        assert _in_dev_container() is False


def test_in_dev_container_no_mountinfo() -> None:
    with (
        patch(_MOD + ".Path.exists", return_value=False),
        patch.object(Path, "open", side_effect=OSError),
    ):
        assert _in_dev_container() is False


def test_in_dev_container_env_var_no_longer_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ST_IN_DEV_CONTAINER", "1")
    with (
        patch(_MOD + ".Path.exists", return_value=False),
        patch.object(Path, "open", side_effect=OSError),
    ):
        assert _in_dev_container() is False


def test_find_custom_validator_entry_point() -> None:
    with patch(_MOD + ".shutil.which", return_value="/usr/bin/custom"):
        assert _find_custom_validator(Path("/fake")) == "/usr/bin/custom"


def test_find_custom_validator_local_script(tmp_path: Path) -> None:
    scripts_bin = tmp_path / "scripts" / "bin"
    scripts_bin.mkdir(parents=True)
    script = scripts_bin / "validate-custom"
    script.write_text("#!/bin/bash\n")
    script.chmod(0o755)
    with patch(_MOD + ".shutil.which", return_value=None):
        assert _find_custom_validator(tmp_path) == str(script)


def test_find_custom_validator_none(tmp_path: Path) -> None:
    with patch(_MOD + ".shutil.which", return_value=None):
        assert _find_custom_validator(tmp_path) is None


def _custom_script(repo: Path, mode: int) -> Path:
    scripts_bin = repo / "scripts" / "bin"
    scripts_bin.mkdir(parents=True)
    script = scripts_bin / "validate-custom"
    script.write_text("#!/bin/bash\n")
    script.chmod(mode)
    return script


def test_find_custom_validator_returns_non_executable_script(tmp_path: Path) -> None:
    # A present-but-unusable script must reach the custom stage to fail there,
    # not be skipped as "no custom validator" (#3349).
    script = _custom_script(tmp_path, 0o600)
    with patch(_MOD + ".shutil.which", return_value=None):
        assert _find_custom_validator(tmp_path) == str(script)


def test_find_custom_validator_returns_broken_symlink(tmp_path: Path) -> None:
    scripts_bin = tmp_path / "scripts" / "bin"
    scripts_bin.mkdir(parents=True)
    link = scripts_bin / "validate-custom"
    link.symlink_to(tmp_path / "missing")
    with patch(_MOD + ".shutil.which", return_value=None):
        assert _find_custom_validator(tmp_path) == str(link)


def test_check_custom_validator_executable_passes(tmp_path: Path) -> None:
    _check_custom_validator(_custom_script(tmp_path, 0o755))


def test_check_custom_validator_not_executable(tmp_path: Path) -> None:
    script = _custom_script(tmp_path, 0o600)
    with pytest.raises(ValidationError, match="is not executable") as exc:
        _check_custom_validator(script)
    assert f"chmod +x {script}" in str(exc.value)


def test_check_custom_validator_not_a_file(tmp_path: Path) -> None:
    (tmp_path / "validate-custom").mkdir()
    with pytest.raises(ValidationError, match="is not a regular file"):
        _check_custom_validator(tmp_path / "validate-custom")


def test_run_all_non_executable_custom_validator_fails(tmp_path: Path) -> None:
    _write_config(tmp_path)
    _custom_script(tmp_path, 0o600)
    ran: list[str] = []

    def mock_run(cmd: tuple[str, ...], *, check: bool = True) -> int:
        ran.append(cmd[0])
        return 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks", return_value=0),
        patch(_MOD + ".shutil.which", return_value=None),
        patch(_MOD + ".progress.run", side_effect=mock_run),
    ):
        result = main([])
    assert result == 1
    assert ran == []


# -- Config error handling ---------------------------------------------------


def test_missing_config_uses_empty_language(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
    ):
        result = main(["--check", "lint"])
    assert result == 0


def test_config_error_returns_1(tmp_path: Path) -> None:
    from vergil_tooling.lib.config import ConfigError

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(
            _MOD + ".config.read_config",
            side_effect=ConfigError("bad config"),
        ),
    ):
        result = main(["--check", "lint"])
    assert result == 1


# -- Install failure stops single check --------------------------------------


def test_check_lint_install_failure_stops(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    ran: list[str] = []

    def mock_run(cmd: list[str], *, check: bool = True) -> int:
        ran.append(" ".join(cmd))
        return 1 if "uv sync" in " ".join(cmd) else 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run", side_effect=mock_run),
    ):
        result = main(["--check", "lint"])
    assert result == 1
    assert all("ruff" not in cmd for cmd in ran)


# -- _run_common_checks body --------------------------------------------------


def test_run_common_checks_calls_common_main() -> None:
    with patch(
        "vergil_tooling.bin.validate_common.main",
        return_value=0,
    ) as mock:
        result = _run_common_checks(Path("/fake"))
    assert result == 0
    mock.assert_called_once()


# -- venv PATH handling --------------------------------------------------------


def test_venv_bin_prepended_to_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_config(tmp_path, "python")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin")

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run", return_value=0),
    ):
        result = main(["--check", "lint"])
    assert result == 0
    assert str(venv_bin) in os.environ["PATH"].split(os.pathsep)


def test_venv_bin_prepended_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under the container's anonymous `.venv` mask the dir is empty (does not
    # yet contain `bin`) at startup; PATH resolves at exec time, so the add is
    # unconditional and must not depend on the dir existing (#2486).
    _write_config(tmp_path, "python")
    venv_bin = tmp_path / ".venv" / "bin"
    assert not venv_bin.exists()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin")

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run", return_value=0),
    ):
        result = main(["--check", "lint"])
    assert result == 0
    assert str(venv_bin) in os.environ["PATH"].split(os.pathsep)


def test_venv_bin_not_prepended_when_already_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, "python")
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{venv_bin}:/usr/bin")

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run", return_value=0),
    ):
        main(["--check", "lint"])
    count = os.environ["PATH"].split(os.pathsep).count(str(venv_bin))
    assert count == 1


# -- _skip_conan_audit_scan (issue #2895) -------------------------------------

_AUDIT_CMDS = [
    ["conan", "audit", "scan", "."],
    ["conan", "graph", "info", ".", "--format=json"],
]


def test_skip_conan_audit_scan_kept_when_token_present() -> None:
    with patch.dict(os.environ, {_CONAN_AUDIT_TOKEN_ENV: "tok"}):
        assert _skip_conan_audit_scan(_AUDIT_CMDS) == _AUDIT_CMDS


def test_skip_conan_audit_scan_dropped_when_token_unset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {k: v for k, v in os.environ.items() if k != _CONAN_AUDIT_TOKEN_ENV}
    with patch.dict(os.environ, env, clear=True):
        result = _skip_conan_audit_scan(_AUDIT_CMDS)
    assert result == [["conan", "graph", "info", ".", "--format=json"]]
    assert "skipping `conan audit scan`" in capsys.readouterr().err


def test_skip_conan_audit_scan_empty_token_treated_as_unset() -> None:
    with patch.dict(os.environ, {_CONAN_AUDIT_TOKEN_ENV: "   "}):
        result = _skip_conan_audit_scan(_AUDIT_CMDS)
    assert result == [["conan", "graph", "info", ".", "--format=json"]]


def test_skip_conan_audit_scan_noop_when_no_scan_command() -> None:
    cmds = [["conan", "graph", "info", ".", "--format=json"]]
    env = {k: v for k, v in os.environ.items() if k != _CONAN_AUDIT_TOKEN_ENV}
    with patch.dict(os.environ, env, clear=True):
        assert _skip_conan_audit_scan(cmds) == cmds


def test_skip_conan_audit_scan_can_empty_the_list() -> None:
    env = {k: v for k, v in os.environ.items() if k != _CONAN_AUDIT_TOKEN_ENV}
    with patch.dict(os.environ, env, clear=True):
        assert _skip_conan_audit_scan([["conan", "audit", "scan", "."]]) == []


# -- _skip_rust_doctests_without_lib (issue #4378) ------------------------------

# The registry's rust TEST commands: the coverage run, then the doctest run.
_RUST_TEST_CMDS = language_commands("rust", CheckKind.TEST)
_RUST_COVERAGE_CMD = _RUST_TEST_CMDS[0]


def _package(name: str, *kinds: list[str]) -> dict[str, object]:
    return {
        "id": f"{name}-id",
        "name": name,
        "targets": [{"kind": k, "name": name} for k in kinds],
    }


def _metadata(
    packages: list[dict[str, object]],
    *,
    members: list[str] | None = None,
    defaults: list[str] | None = None,
) -> str:
    """``cargo metadata --no-deps`` output; every package is a member by default."""
    member_ids = [p["id"] for p in packages] if members is None else [f"{n}-id" for n in members]
    default_ids = member_ids if defaults is None else [f"{n}-id" for n in defaults]
    return json.dumps(
        {
            "packages": packages,
            "workspace_members": member_ids,
            "workspace_default_members": default_ids,
        }
    )


@contextmanager
def _cargo_metadata(stdout: str, returncode: int = 0, stderr: str = "") -> Generator[MagicMock]:
    completed = subprocess.CompletedProcess(
        ["cargo", "metadata"], returncode, stdout=stdout, stderr=stderr
    )
    with patch(_MOD + ".subprocess.run", return_value=completed) as m_run:
        yield m_run


def test_rust_doctests_kept_for_a_lib_crate(tmp_path: Path) -> None:
    stdout = _metadata([_package("app", ["lib"], ["bin"])])
    with _cargo_metadata(stdout) as m_run:
        assert _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path) == _RUST_TEST_CMDS
    assert m_run.call_args.args[0] == [
        "cargo",
        "metadata",
        "--no-deps",
        "--format-version",
        "1",
        "--locked",
    ]
    assert m_run.call_args.kwargs["cwd"] == tmp_path


@pytest.mark.parametrize("kind", ["rlib", "dylib", "cdylib", "staticlib", "proc-macro"])
def test_rust_doctests_kept_for_every_library_kind(tmp_path: Path, kind: str) -> None:
    stdout = _metadata([_package("lib", [kind])])
    with _cargo_metadata(stdout):
        assert _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path) == _RUST_TEST_CMDS


def test_rust_doctests_dropped_for_a_bin_only_crate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stdout = _metadata([_package("tool", ["bin"], ["test"])])
    with _cargo_metadata(stdout):
        result = _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path)
    assert result == [_RUST_COVERAGE_CMD]
    err = capsys.readouterr().err
    assert "skipping `cargo test --doc`" in err
    assert "no library target in tool" in err


def test_doctest_skip_uses_real_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The commands come from the real registry, so the skip matches the
    # doctest command exactly as it is built, flags included (spec §6.4). A
    # workspace whose members are all binary-only has it dropped.
    cmds = language_commands("rust", CheckKind.TEST)
    assert RUST_DOCTEST_CMD in cmds
    assert "--workspace" in RUST_DOCTEST_CMD
    packages = [_package("cli", ["bin"]), _package("daemon", ["bin"], ["test"])]
    with _cargo_metadata(_metadata(packages)):
        result = _skip_rust_doctests_without_lib(cmds, tmp_path)
    assert result == [cmd for cmd in cmds if cmd != RUST_DOCTEST_CMD]
    assert len(result) == len(cmds) - 1
    assert "no library target in cli, daemon" in capsys.readouterr().err


def test_doctest_skip_keeps_lib_member(tmp_path: Path) -> None:
    # A binary-only root whose library member is not a default member: the
    # doctests run with --workspace, which selects every member, so the
    # library member keeps the doctest command.
    packages = [_package("cli", ["bin"]), _package("core", ["lib"])]
    with _cargo_metadata(_metadata(packages, defaults=["cli"])):
        assert _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path) == _RUST_TEST_CMDS


def test_rust_doctests_judge_only_the_workspace_members(tmp_path: Path) -> None:
    # A package outside workspace_members is not selected by --workspace, so
    # its library does not keep the doctest command.
    packages = [_package("cli", ["bin"]), _package("core", ["lib"])]
    with _cargo_metadata(_metadata(packages, members=["cli"])):
        result = _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path)
    assert result == [_RUST_COVERAGE_CMD]


def test_rust_doctests_kept_when_any_member_has_a_lib(tmp_path: Path) -> None:
    packages = [_package("cli", ["bin"]), _package("core", ["lib"])]
    with _cargo_metadata(_metadata(packages)):
        assert _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path) == _RUST_TEST_CMDS


def test_rust_doctests_notice_with_no_members(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _cargo_metadata(_metadata([])):
        result = _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path)
    assert result == [_RUST_COVERAGE_CMD]
    assert "the workspace members" in capsys.readouterr().err


def test_rust_doctests_no_probe_without_the_command(tmp_path: Path) -> None:
    cmds = [_RUST_COVERAGE_CMD]
    with patch(_MOD + ".subprocess.run") as m_run:
        assert _skip_rust_doctests_without_lib(cmds, tmp_path) == cmds
    m_run.assert_not_called()


def test_rust_doctests_probe_failure_is_loud(tmp_path: Path) -> None:
    with (
        _cargo_metadata("", returncode=101, stderr="could not find Cargo.toml"),
        pytest.raises(ValidationError, match=r"probe failed: .* exited 101: could not find"),
    ):
        _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path)


@pytest.mark.parametrize("exc", [OSError("no cargo"), subprocess.TimeoutExpired("cargo", 300)])
def test_rust_doctests_probe_that_cannot_run_is_loud(tmp_path: Path, exc: Exception) -> None:
    with (
        patch(_MOD + ".subprocess.run", side_effect=exc),
        pytest.raises(ValidationError, match="probe could not run"),
    ):
        _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path)


@pytest.mark.parametrize(
    "stdout",
    [
        "not json",
        json.dumps({"packages": []}),
        json.dumps({"packages": [{"id": "x"}], "workspace_members": ["x"]}),
        json.dumps({"packages": None, "workspace_members": []}),
    ],
)
def test_rust_doctests_unexpected_metadata_is_loud(tmp_path: Path, stdout: str) -> None:
    with (
        _cargo_metadata(stdout),
        pytest.raises(ValidationError, match="unexpected `cargo metadata` output"),
    ):
        _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path)


def test_rust_test_stage_probes_at_stage_time(tmp_path: Path) -> None:
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("test", "rust", tmp_path)
    assert [s.name for s in stages] == ["install", "test"]
    stdout = _metadata([_package("tool", ["bin"])])
    with (
        _cargo_metadata(stdout),
        patch(_MOD + ".progress.run", return_value=0) as m_progress,
    ):
        stages[1].fn(None)
    assert [c.args[0] for c in m_progress.call_args_list] == [_RUST_COVERAGE_CMD]
    assert _RUST_COVERAGE_CMD[:2] == ["sh", "-c"]
    assert "cargo llvm-cov nextest" in _RUST_COVERAGE_CMD[2]


# -- the uncovered-region listing (epic vergil-project/.github#1165, spec §6.2) --


def _coverage_export(root: Path, *, count: int = 0, notcovered: int = 1) -> dict[str, object]:
    """An llvm-cov export whose region at 12:21 ran *count* times; line 12 otherwise ran."""
    lib = str(root / "src" / "lib.rs")
    return {
        "data": [
            {
                "files": [{"filename": lib}],
                "functions": [
                    {
                        "name": "f",
                        "regions": [[12, 5, 12, 20, 4, 0, 0, 0], [12, 21, 12, 30, count, 0, 0, 0]],
                        "filenames": [lib],
                    }
                ],
                "totals": {"regions": {"count": 2, "covered": 1, "notcovered": notcovered}},
            }
        ]
    }


def _run_rust_test_stage(
    tmp_path: Path,
    *,
    coverage_rc: int = 1,
    export: object = None,
    export_rc: int = 0,
    doctest_rc: int = 0,
) -> tuple[list[list[str]], bool]:
    """Run the Rust TEST stage on a library crate; return the commands and whether it failed.

    The coverage command exits *coverage_rc*. The JSON export writes *export*
    (a string as it is, anything else as JSON) and exits *export_rc*.
    """
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("test", "rust", tmp_path)
    ran: list[list[str]] = []

    def fake_run(cmd: list[str], **_kw: object) -> int:
        ran.append(list(cmd))
        if cmd == _RUST_COVERAGE_CMD:
            return coverage_rc
        if cmd == RUST_DOCTEST_CMD:
            return doctest_rc
        assert cmd[:3] == ["cargo", "llvm-cov", "report"]
        if export is not None:
            text = export if isinstance(export, str) else json.dumps(export)
            Path(cmd[cmd.index("--output-path") + 1]).write_text(text, encoding="utf-8")
        return export_rc

    failed = False
    with (
        _cargo_metadata(_metadata([_package("lib", ["lib"])])),
        patch(_MOD + ".progress.run", side_effect=fake_run),
    ):
        try:
            stages[1].fn(None)
        except ValidationError:
            failed = True
    return ran, failed


def test_failed_coverage_prints_regions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ran, failed = _run_rust_test_stage(tmp_path, export=_coverage_export(tmp_path))
    assert failed
    out = capsys.readouterr().out
    assert "Uncovered regions (1):" in out
    assert "src/lib.rs:12:21: uncovered region" in out
    assert str(tmp_path) not in out.split("Uncovered regions")[1]
    assert "\nerror:" not in out
    export_cmd = ran[1]
    assert export_cmd[:-1] == [
        "cargo",
        "llvm-cov",
        "report",
        "--locked",
        "--workspace",
        "--json",
        "--output-path",
    ]
    assert ran == [_RUST_COVERAGE_CMD, export_cmd, RUST_DOCTEST_CMD]
    # The export is written to a temporary directory, removed afterwards.
    assert not Path(export_cmd[-1]).exists()


def test_passing_coverage_runs_no_export(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran, failed = _run_rust_test_stage(tmp_path, coverage_rc=0)
    assert not failed
    assert ran == [_RUST_COVERAGE_CMD, RUST_DOCTEST_CMD]
    assert "Uncovered regions" not in capsys.readouterr().out


def test_failed_doctest_runs_no_export(tmp_path: Path) -> None:
    ran, failed = _run_rust_test_stage(tmp_path, coverage_rc=0, doctest_rc=101)
    assert failed
    assert ran == [_RUST_COVERAGE_CMD, RUST_DOCTEST_CMD]


def test_failed_coverage_with_no_uncovered_region(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A failed test run with every region covered: the listing says so.
    export = _coverage_export(tmp_path, count=2, notcovered=0)
    _, failed = _run_rust_test_stage(tmp_path, export=export)
    assert failed
    out = capsys.readouterr().out
    assert "Uncovered regions: none" in out
    assert "\nerror:" not in out


def test_failed_coverage_export_failure_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran, failed = _run_rust_test_stage(tmp_path, export_rc=2)
    assert failed
    assert len(ran) == 3
    out = capsys.readouterr().out
    assert "error: the coverage export exited 2; the uncovered regions are not listed" in out


@pytest.mark.parametrize(
    ("export", "reason"),
    [
        (None, "No such file"),
        ("{not json", "Expecting property name"),
        ("[1]", "coverage export: not a JSON object"),
        ({"data": "x"}, "coverage export: no 'data' list"),
    ],
)
def test_failed_coverage_unreadable_export_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], export: object, reason: str
) -> None:
    _, failed = _run_rust_test_stage(tmp_path, export=export)
    assert failed
    out = capsys.readouterr().out
    assert "error: could not read the coverage export: " in out
    assert reason in out


def test_failed_coverage_listing_disagreeing_with_totals_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, failed = _run_rust_test_stage(tmp_path, export=_coverage_export(tmp_path, notcovered=3))
    assert failed
    out = capsys.readouterr().out
    assert "src/lib.rs:12:21: uncovered region" in out
    assert (
        "error: 1 uncovered region(s) listed, but the coverage totals count 3 missed; "
        "the listing is incomplete"
    ) in out


def test_build_stages_drops_audit_when_only_scan_and_no_token() -> None:
    # cpp audit degrades to just the scan in this mock; with no token the scan
    # is dropped, the audit command list empties, and the stage is omitted.
    env = {k: v for k, v in os.environ.items() if k != _CONAN_AUDIT_TOKEN_ENV}
    with (
        patch.dict(os.environ, env, clear=True),
        patch(_MOD + "._find_custom_validator", return_value=None),
        patch(_MOD + ".language_commands") as m_cmds,
    ):
        m_cmds.side_effect = lambda _lang, kind, **_kw: (
            [["conan", "audit", "scan", "."]] if kind is CheckKind.AUDIT else [["echo", kind.value]]
        )
        stages = _build_stages(None, "cpp", Path("/tmp/r"))  # noqa: S108
    assert "audit" not in [s.name for s in stages]


def test_build_stages_keeps_audit_when_token_present() -> None:
    with (
        patch.dict(os.environ, {_CONAN_AUDIT_TOKEN_ENV: "tok"}),
        patch(_MOD + "._find_custom_validator", return_value=None),
        patch(_MOD + ".language_commands") as m_cmds,
    ):
        m_cmds.side_effect = lambda _lang, kind, **_kw: (
            [["conan", "audit", "scan", "."]] if kind is CheckKind.AUDIT else [["echo", kind.value]]
        )
        stages = _build_stages(None, "cpp", Path("/tmp/r"))  # noqa: S108
    assert "audit" in [s.name for s in stages]


# -- xdist probe: the project's test environment (issue #3322) ---------------


def _fake_env(tmp_path: Path, *, interpreter: str | None, body: str) -> Path:
    """Make a bin dir holding a ``pytest`` and (optionally) a fake interpreter."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "pytest").write_text("#!/bin/sh\n")
    (bin_dir / "pytest").chmod(0o755)
    if interpreter is not None:
        (bin_dir / interpreter).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / interpreter).chmod(0o755)
    return bin_dir


def _only_on_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin")


def test_probe_asks_test_env_not_own_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: xdist is importable here, but the test env says no → False.

    This suite's own interpreter has pytest-xdist installed (it is in the dev
    group), exactly like vrg-validate's tool venv in the issue. The probe must
    still report the environment the resolved ``pytest`` belongs to.
    """
    import importlib.util

    assert importlib.util.find_spec("xdist") is not None
    bin_dir = _fake_env(tmp_path, interpreter="python", body="exit 3")
    _only_on_path(monkeypatch, bin_dir)
    assert _probe_xdist() is False


def test_probe_reports_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = _fake_env(tmp_path, interpreter="python", body="exit 0")
    _only_on_path(monkeypatch, bin_dir)
    assert _probe_xdist() is True


def test_probe_falls_back_to_python3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = _fake_env(tmp_path, interpreter="python3", body="exit 0")
    _only_on_path(monkeypatch, bin_dir)
    assert _probe_xdist() is True


def test_probe_error_exit_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = _fake_env(tmp_path, interpreter="python", body="echo boom >&2; exit 1")
    _only_on_path(monkeypatch, bin_dir)
    with pytest.raises(ValidationError, match=r"probe failed: .* exited 1: boom"):
        _probe_xdist()


def test_probe_without_pytest_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _only_on_path(monkeypatch, tmp_path)
    with (
        patch(_MOD + ".shutil.which", return_value=None),
        pytest.raises(ValidationError, match="pytest is not on PATH"),
    ):
        _probe_xdist()


def test_probe_without_interpreter_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = _fake_env(tmp_path, interpreter=None, body="")
    _only_on_path(monkeypatch, bin_dir)
    with pytest.raises(ValidationError, match="no python interpreter next to"):
        _probe_xdist()


@pytest.mark.parametrize(
    "exc", [OSError("exec format error"), subprocess.TimeoutExpired("python", 60)]
)
def test_probe_that_cannot_run_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    bin_dir = _fake_env(tmp_path, interpreter="python", body="exit 0")
    _only_on_path(monkeypatch, bin_dir)
    with (
        patch(_MOD + ".subprocess.run", side_effect=exc),
        pytest.raises(ValidationError, match="probe could not run"),
    ):
        _probe_xdist()


# -- worker count resolution (issue #3322) ----------------------------------


def test_test_context_unknown_without_signals() -> None:
    assert _test_context() is None


def test_test_context_github_actions_is_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert _test_context() == "ci"


def test_test_context_forwarded_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("VRG_TEST_CONTEXT", "local-vm")
    assert _test_context() == "local-vm"


def test_test_context_unknown_value_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_TEST_CONTEXT", "my-laptop")
    with pytest.raises(ConfigError, match="'my-laptop' is not a known context"):
        _test_context()


_TUNED = TestConfig(workers=4, context_workers={"local-vm": 12})


def test_resolve_workers_uses_context_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_TEST_CONTEXT", "local-vm")
    assert _resolve_test_workers(_TUNED) == (12, "[test.local-vm].workers, context local-vm")


def test_resolve_workers_default_for_unknown_context() -> None:
    assert _resolve_test_workers(_TUNED) == (4, "[test].workers, context <unknown>")


def test_resolve_workers_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_TEST_CONTEXT", "local-vm")
    monkeypatch.setenv("VRG_TEST_WORKERS", "6")
    assert _resolve_test_workers(_TUNED) == (6, "VRG_TEST_WORKERS")


def test_resolve_workers_invalid_env_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_TEST_WORKERS", "lots")
    with pytest.raises(ConfigError, match="VRG_TEST_WORKERS must be"):
        _resolve_test_workers(_TUNED)


def test_main_invalid_workers_env_returns_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, "python")
    monkeypatch.setenv("VRG_TEST_WORKERS", "0")
    with patch(_MOD + ".git.repo_root", return_value=tmp_path):
        assert main([]) == 1
    assert "VRG_TEST_WORKERS must be" in capsys.readouterr().err


# -- deferred Python test stage (issue #3322) ---------------------------------


def _run_test_stage(
    *, parallel: bool, workers: int | str, workers_source: str, xdist_probe: Callable[[], bool]
) -> list[list[str]]:
    ran: list[list[str]] = []

    def record(cmd: list[str], *, check: bool = True) -> int:  # noqa: ARG001
        ran.append(cmd)
        return 0

    stage = _python_test_stage(
        parallel=parallel, workers=workers, workers_source=workers_source, xdist_probe=xdist_probe
    )
    with patch(_MOD + ".progress.run", side_effect=record):
        stage.fn(None)
    return ran


def test_python_test_stage_parallel_with_tuned_workers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran = _run_test_stage(
        parallel=True,
        workers=12,
        workers_source="[test.local-vm].workers",
        xdist_probe=lambda: True,
    )
    assert ran[0][-4:] == ["-n", "12", "--dist", "worksteal"]
    assert "Test parallelism: -n 12 (from [test.local-vm].workers)" in capsys.readouterr().out


def test_python_test_stage_serial_when_xdist_absent(capsys: pytest.CaptureFixture[str]) -> None:
    ran = _run_test_stage(parallel=True, workers=12, workers_source="x", xdist_probe=lambda: False)
    assert "-n" not in ran[0]
    assert "pytest-xdist is not installed" in capsys.readouterr().out


def test_python_test_stage_opt_out_skips_probe(capsys: pytest.CaptureFixture[str]) -> None:
    def probe() -> bool:
        raise AssertionError("probe must not run when parallel = false")

    ran = _run_test_stage(parallel=False, workers="auto", workers_source="x", xdist_probe=probe)
    assert "-n" not in ran[0]
    assert "[test].parallel = false" in capsys.readouterr().out


def test_build_stages_defers_python_probe_to_stage_time() -> None:
    """The probe must not run at build time: install has not populated .venv yet."""
    calls: list[str] = []

    def probe() -> bool:
        calls.append("probe")
        return True

    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("test", "python", Path("/r"), test_workers=12, xdist_probe=probe)
    assert [s.name for s in stages] == ["install", "test"]
    assert calls == []
    with patch(_MOD + ".progress.run", return_value=0) as m_run:
        stages[1].fn(None)
    assert calls == ["probe"]
    assert m_run.call_args.args[0][-4:] == ["-n", "12", "--dist", "worksteal"]


# -- --all-versions coverage matrix (issue #3547) -----------------------------


def test_all_versions_rejects_non_python(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, "go")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._versions_resolve", return_value=("1.24",)),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--all-versions"]) == 1
    m_pipeline.assert_not_called()
    err = capsys.readouterr().err
    assert "--all-versions supports Python only (primary-language is go)" in err


def test_all_versions_rejects_missing_language(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--all-versions"]) == 1
    m_pipeline.assert_not_called()
    assert "primary-language is <not set>" in capsys.readouterr().err


@pytest.mark.parametrize("check", ["common", "lint", "typecheck", "audit"])
def test_all_versions_rejects_non_test_check(check: str) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--all-versions", "--check", check])
    assert exc.value.code == 2


def test_all_versions_adds_a_stage_per_resolved_version(tmp_path: Path) -> None:
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.12", "3.14"]')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._find_custom_validator", return_value=None),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--all-versions", "--check", "test"]) == 0
    stages = m_pipeline.call_args.args[1]
    assert [s.name for s in stages] == [
        "install",
        "test",
        "test (python 3.14)",
        "test (python 3.12)",
    ]


def test_default_run_has_no_version_stages(tmp_path: Path) -> None:
    _write_pinned_config(tmp_path, repo_type="library", versions='["3.12", "3.14"]')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._find_custom_validator", return_value=None),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--check", "test"]) == 0
    assert [s.name for s in m_pipeline.call_args.args[1]] == ["install", "test"]


def test_build_stages_version_stages_follow_test_before_audit() -> None:
    with (
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        m_cmds.side_effect = lambda lang, kind, **_kw: [["echo", kind.value]]
        stages = _build_stages(None, "python", Path("/r"), all_versions=("3.14", "3.13"))
    assert [s.name for s in stages] == [
        "common",
        "install",
        "lint",
        "typecheck",
        "build",
        "test",
        "test (python 3.14)",
        "test (python 3.13)",
        "audit",
        "semgrep",
        "trivy",
        "docs",
    ]


def test_build_stages_lint_only_ignores_all_versions() -> None:
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("lint", "python", Path("/r"), all_versions=("3.14",))
    assert [s.name for s in stages] == ["install", "lint"]


_Ran = list[tuple[list[str], "dict[str, str] | None"]]


def _version_stage(
    tmp_path: Path, version: str, *, default_version: str = "3.15", parallel: bool = True
) -> Callable[[object], None]:
    return _python_version_test_stage(
        version,
        parallel=parallel,
        workers=8,
        workers_source="[test].workers",
        default_version=lambda: default_version,
        env_root=tmp_path,
    ).fn


def _run_version_stage(
    tmp_path: Path,
    *,
    version: str = "3.12",
    default_version: str = "3.15",
    parallel: bool = True,
    rcs: tuple[int, ...] = (0, 0, 0),
    xdist: bool = True,
) -> _Ran:
    ran: _Ran = []
    codes = iter(rcs)

    def record(
        cmd: list[str],
        *,
        env: dict[str, str] | None = None,
        check: bool = True,  # noqa: ARG001
    ) -> int:
        ran.append((list(cmd), env))
        return next(codes)

    fn = _version_stage(tmp_path, version, default_version=default_version, parallel=parallel)
    with (
        patch(_MOD + ".progress.run", side_effect=record),
        patch(_MOD + "._probe_xdist", return_value=xdist) as m_probe,
    ):
        fn(None)
    if parallel and ran:
        m_probe.assert_called_once_with(tmp_path / f"python-{version}" / "bin")
    return ran


def test_version_stage_installs_syncs_and_runs_gate(tmp_path: Path) -> None:
    ran = _run_version_stage(tmp_path)
    env_dir = tmp_path / "python-3.12"
    cmds = [cmd for cmd, _env in ran]
    assert cmds[0] == ["uv", "python", "install", "3.12"]
    assert cmds[1] == [
        "uv",
        "sync",
        "--frozen",
        "--group",
        "dev",
        "--python",
        "3.12",
        "--python-preference",
        "only-managed",
    ]
    pytest_cmd = cmds[2]
    assert pytest_cmd[0] == str(env_dir / "bin" / "pytest")
    # The inviolate coverage gate is unchanged.
    assert "--cov-branch" in pytest_cmd
    assert "--cov-fail-under=100" in pytest_cmd
    # Reports land beside the version environment, never over the workspace's.
    assert f"--cov-report=xml:{env_dir / 'coverage.xml'}" in pytest_cmd
    assert f"--junitxml={env_dir / 'junit.xml'}" in pytest_cmd
    assert "--cov-report=xml:coverage.xml" not in pytest_cmd
    assert pytest_cmd[-4:] == ["-n", "8", "--dist", "worksteal"]
    for _cmd, env in ran:
        assert env is not None
        assert env["UV_PROJECT_ENVIRONMENT"] == str(env_dir)
        assert env["VIRTUAL_ENV"] == str(env_dir)
        assert env["PATH"].split(os.pathsep)[0] == str(env_dir / "bin")


def test_version_stage_serial_when_xdist_absent(tmp_path: Path) -> None:
    ran = _run_version_stage(tmp_path, xdist=False)
    assert "-n" not in ran[2][0]


def test_version_stage_opt_out_skips_probe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = _run_version_stage(tmp_path, parallel=False)
    assert "-n" not in ran[2][0]
    assert "[test].parallel = false" in capsys.readouterr().out


def test_version_stage_skips_the_container_interpreter(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ran = _run_version_stage(tmp_path, version="3.15", default_version="3.15")
    assert ran == []
    assert "Python 3.15: covered by the test stage" in capsys.readouterr().out


@pytest.mark.parametrize("rcs", [(1,), (0, 1), (0, 0, 1)])
def test_version_stage_failure_raises_and_stops(tmp_path: Path, rcs: tuple[int, ...]) -> None:
    with pytest.raises(ValidationError, match=r"python 3\.12 test command failed"):
        _run_version_stage(tmp_path, rcs=rcs, xdist=False)


def test_version_stage_failure_stops_later_commands(tmp_path: Path) -> None:
    with patch(_MOD + ".progress.run", return_value=1) as m_run, pytest.raises(ValidationError):
        _version_stage(tmp_path, "3.12")(None)
    assert m_run.call_count == 1
    assert m_run.call_args.args[0] == ["uv", "python", "install", "3.12"]


def test_probe_xdist_in_given_bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The bin dir is given, so PATH is not consulted at all.
    bin_dir = _fake_env(tmp_path, interpreter="python", body="exit 0")
    monkeypatch.setenv("PATH", "/nonexistent")
    assert _probe_xdist(bin_dir) is True


def test_interpreter_version_reads_major_minor(tmp_path: Path) -> None:
    bin_dir = _fake_env(tmp_path, interpreter="python", body="echo 3.13")
    assert _interpreter_version(bin_dir / "python") == "3.13"


def test_interpreter_version_error_exit_is_loud(tmp_path: Path) -> None:
    bin_dir = _fake_env(tmp_path, interpreter="python", body="echo bad >&2; exit 2")
    with pytest.raises(ValidationError, match=r"exited 2: bad"):
        _interpreter_version(bin_dir / "python")


def test_interpreter_version_that_cannot_run_is_loud(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="could not run"):
        _interpreter_version(tmp_path / "missing-python")


def test_default_test_version_asks_the_test_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = _fake_env(tmp_path, interpreter="python", body="echo 3.15")
    _only_on_path(monkeypatch, bin_dir)
    assert _default_test_version() == "3.15"


def test_default_test_version_without_pytest_is_loud() -> None:
    with (
        patch(_MOD + ".shutil.which", return_value=None),
        pytest.raises(ValidationError, match="cannot read the test interpreter version"),
    ):
        _default_test_version()


# -- BUILD stage (epic vergil-project/.github#1174) ----------------------------


def _python_with_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn BUILD on for Python, which leaves it off, with ``uv build``."""
    entry = languages._REGISTRY["python"]
    ecosystem = replace(entry.ecosystem, build_cmd=["uv", "build"], validate_build=True)
    monkeypatch.setitem(languages._REGISTRY, "python", replace(entry, ecosystem=ecosystem))


def test_check_build_nothing_to_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline") as m_pipeline,
    ):
        assert main(["--check", "build"]) == 0
    m_pipeline.assert_not_called()
    assert "build: no build check for python — nothing to run" in capsys.readouterr().out


def test_full_validate_runs_build_after_typecheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _python_with_build(monkeypatch)
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages(None, "python", tmp_path)
    names = [s.name for s in stages]
    assert names[names.index("typecheck") + 1] == "build"
    assert names[names.index("build") + 1] == "test"
    assert next(s for s in stages if s.name == "build").mode == "fail_defer"


def test_check_build_runs_install_then_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _python_with_build(monkeypatch)
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0) as m_pipeline,
    ):
        assert main(["--check", "build"]) == 0
    assert [s.name for s in m_pipeline.call_args.args[1]] == ["install", "build"]


def test_build_stage_runs_the_build_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _python_with_build(monkeypatch)
    ran: list[tuple[str, ...]] = []

    def mock_run(cmd: Sequence[str], *, check: bool = True) -> int:
        ran.append(tuple(cmd))
        return 0

    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("build", "python", tmp_path)
    build = next(s for s in stages if s.name == "build")
    with patch(_MOD + ".progress.run", side_effect=mock_run):
        build.fn(None)
    assert ran == [("uv", "build")]


# -- --check custom (issue #3650) ----------------------------------------------

_NO_CUSTOM_LINE = (
    "custom: no custom validator (scripts/bin/validate-custom or "
    "vrg-validate-custom) — nothing to run"
)


def test_build_stages_check_custom_runs_install_then_custom() -> None:
    with (
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
    ):
        m_cmds.side_effect = lambda lang, kind, **_kw: [["echo", kind.value]]
        stages = _build_stages("custom", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["install", "custom"]
    assert stages[0].mode == "fail_fast"
    assert stages[1].mode == "fail_defer"


def test_build_stages_check_custom_no_install_commands() -> None:
    with (
        patch(_MOD + ".language_commands", return_value=[]),
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
    ):
        stages = _build_stages("custom", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["custom"]


def test_build_stages_check_custom_no_language() -> None:
    with (
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
    ):
        stages = _build_stages("custom", None, Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["custom"]
    m_cmds.assert_not_called()


def test_build_stages_check_custom_no_validator() -> None:
    with (
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        m_cmds.side_effect = lambda lang, kind, **_kw: [["echo", kind.value]]
        stages = _build_stages("custom", "python", Path("/tmp/r"))  # noqa: S108
    assert stages == []


def test_build_stages_check_lint_excludes_custom() -> None:
    with (
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
    ):
        m_cmds.side_effect = lambda lang, kind, **_kw: [["echo", kind.value]]
        stages = _build_stages("lint", "python", Path("/tmp/r"))  # noqa: S108
    assert "custom" not in [s.name for s in stages]


def test_main_check_custom_no_validator(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_config(tmp_path)
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._find_custom_validator", return_value=None),
        patch(_MOD + ".progress.run_pipeline") as m_pipeline,
    ):
        assert main(["--check", "custom"]) == 0
    m_pipeline.assert_not_called()
    assert _NO_CUSTOM_LINE in capsys.readouterr().out


def test_main_check_custom_runs_validator(tmp_path: Path) -> None:
    _write_config(tmp_path)
    ran: list[str] = []

    def mock_run(cmd: tuple[str, ...], *, check: bool = True) -> int:
        ran.append(cmd[0])
        return 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks") as m_common,
        patch(_MOD + ".progress.run", side_effect=mock_run),
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
        patch(_MOD + "._check_custom_validator"),
    ):
        assert main(["--check", "custom"]) == 0
    m_common.assert_not_called()
    assert ran == ["/path/to/custom"]


def test_main_check_custom_failure(tmp_path: Path) -> None:
    _write_config(tmp_path)
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run", return_value=1),
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
        patch(_MOD + "._check_custom_validator"),
    ):
        assert main(["--check", "custom"]) == 1


def test_main_check_custom_non_executable_fails(tmp_path: Path) -> None:
    _write_config(tmp_path)
    _custom_script(tmp_path, 0o644)
    ran: list[str] = []

    def mock_run(cmd: tuple[str, ...], *, check: bool = True) -> int:
        ran.append(cmd[0])
        return 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".shutil.which", return_value=None),
        patch(_MOD + ".progress.run", side_effect=mock_run),
    ):
        assert main(["--check", "custom"]) == 1
    assert ran == []


def test_check_custom_rejects_all_versions() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--check", "custom", "--all-versions"])
    assert exc.value.code == 2


# -- A repo not bootstrapped yet (issue #4384) ---------------------------------

_NOT_BOOTSTRAPPED = "skipped: no Cargo.toml, Cargo.lock (rust repo not bootstrapped yet)"


@pytest.fixture
def real_bootstrap_check(tmp_path: Path) -> Iterator[None]:
    """Restore the real bootstrapped-ness test the autouse fixture stubs out.

    The repo root is made a git repo, since the test lists its files with git.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S607
    with patch(_MOD + ".unbootstrapped_manifests", side_effect=unbootstrapped_manifests):
        yield


def _run_validate(
    tmp_path: Path, argv: list[str], *, custom: str | None = None
) -> tuple[int, list[str]]:
    """Run main() for real on a rust repo at *tmp_path*; return rc and the commands run."""
    ran: list[str] = []

    def mock_run(cmd: tuple[str, ...] | list[str], *, check: bool = True) -> int:
        ran.append(" ".join(cmd))
        return 1 if cmd[0] == "cargo" else 0

    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._versions_resolve", return_value=("1.90",)),
        patch(_MOD + "._run_common_checks", return_value=0) as m_common,
        patch(_MOD + "._find_custom_validator", return_value=custom),
        patch(_MOD + "._check_custom_validator"),
        patch(_MOD + ".progress.run", side_effect=mock_run),
    ):
        rc = main(argv)
    if "--check" not in argv:
        m_common.assert_called_once()
    return rc, ran


@pytest.mark.usefixtures("real_bootstrap_check")
def test_unbootstrapped_rust_skips_language_stages_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, "rust")
    rc, ran = _run_validate(tmp_path, [], custom="/path/to/custom")
    assert rc == 0
    # No cargo command ran; the common checks and the custom validator did.
    assert ran == ["/path/to/custom"]
    record = read_record(tmp_path)
    assert record.full
    assert record.not_passed == ()
    assert record.passed == (
        "common",
        "install",
        "lint",
        "typecheck",
        "test",
        "audit",
        "semgrep",
        "trivy",
        "docs",
        "custom",
    )
    out = capsys.readouterr().out
    for stage in ("install", "lint", "typecheck", "test", "audit"):
        assert f"NOTICE: {stage}: {_NOT_BOOTSTRAPPED}; add them to run the {stage} stage" in out
    # Each skip is a warning row and a summary line, never a silent ✓.
    assert "warnings (non-fatal):" in out
    assert f"install — {_NOT_BOOTSTRAPPED}" in out


@pytest.mark.usefixtures("real_bootstrap_check")
def test_unbootstrapped_rust_stages_are_warnings(tmp_path: Path) -> None:
    _write_config(tmp_path, "rust")
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages(None, "rust", tmp_path)
    language_stages = [s for s in stages if s.name not in ("common", "semgrep", "trivy", "docs")]
    assert [s.name for s in language_stages] == ["install", "lint", "typecheck", "test", "audit"]
    assert all(s.mode == "warn" for s in language_stages)
    _run_validate(tmp_path, [])
    data = json.loads((tmp_path / RECORD_PATH).read_text())
    status = {stage["name"]: stage["status"] for stage in data["stages"]}
    assert status == {
        "common": "ok",
        "install": "warn",
        "lint": "warn",
        "typecheck": "warn",
        "test": "warn",
        "audit": "warn",
        "semgrep": "ok",
        "trivy": "ok",
        "docs": "ok",
    }


@pytest.mark.usefixtures("real_bootstrap_check")
def test_bootstrapped_rust_with_failing_fetch_still_fails(tmp_path: Path) -> None:
    _write_config(tmp_path, "rust")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    (tmp_path / "Cargo.lock").write_text("version = 4\n")
    rc, ran = _run_validate(tmp_path, [])
    assert rc == 1
    # The install stage is fail_fast: its first cargo command failed and stopped it.
    assert ran[0] == "cargo fetch --locked"
    assert read_record(tmp_path).not_passed[0] == "install"


# -- Cargo.lock is enforced (epic vergil-project/.github#1165, spec §6.1) -------

_LOCKFILE_MISSING = (
    "Cargo.lock is missing: run 'cargo generate-lockfile' in the container and commit it"
)


def test_missing_cargo_lock_fails_with_guidance(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    with pytest.raises(ValidationError) as exc:
        check_rust_lockfile(tmp_path)
    assert str(exc.value) == _LOCKFILE_MISSING


def test_present_cargo_lock_passes_the_check(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    (tmp_path / "Cargo.lock").write_text("version = 4\n")
    check_rust_lockfile(tmp_path)


def test_lockfile_check_needs_a_cargo_toml(tmp_path: Path) -> None:
    check_rust_lockfile(tmp_path)


@pytest.mark.usefixtures("real_bootstrap_check")
def test_missing_cargo_lock_fails_install_before_cargo_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, "rust")
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
    rc, ran = _run_validate(tmp_path, [])
    assert rc == 1
    assert not any(cmd.startswith("cargo ") for cmd in ran)
    record = read_record(tmp_path)
    assert record.not_passed[0] == "install"
    assert "lint" not in record.passed
    assert _LOCKFILE_MISSING in capsys.readouterr().out


def test_cargo_metadata_locked(tmp_path: Path) -> None:
    with _cargo_metadata(_metadata([_package("core", ["lib"])])) as m_run:
        _skip_rust_doctests_without_lib(_RUST_TEST_CMDS, tmp_path)
    assert "--locked" in m_run.call_args.args[0]


@pytest.mark.usefixtures("real_bootstrap_check")
def test_unbootstrapped_rust_single_check_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, "rust")
    rc, ran = _run_validate(tmp_path, ["--check", "lint"])
    assert rc == 0
    assert ran == []
    assert read_record(tmp_path).passed == ("install", "lint")
    assert f"NOTICE: lint: {_NOT_BOOTSTRAPPED}" in capsys.readouterr().out


@pytest.mark.usefixtures("real_bootstrap_check")
def test_unbootstrapped_rust_check_custom_skips_install_runs_validator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path, "rust")
    rc, ran = _run_validate(tmp_path, ["--check", "custom"], custom="/path/to/custom")
    assert rc == 0
    assert ran == ["/path/to/custom"]
    assert read_record(tmp_path).passed == ("install", "custom")
    assert f"NOTICE: install: {_NOT_BOOTSTRAPPED}" in capsys.readouterr().out


@pytest.mark.usefixtures("real_bootstrap_check")
def test_unbootstrapped_python_all_versions_skips_every_test_stage(tmp_path: Path) -> None:
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("test", "python", tmp_path, all_versions=("3.12", "3.14"))
    assert [s.name for s in stages] == ["install", "test"]
    assert all(s.mode == "warn" for s in stages)


@pytest.mark.usefixtures("real_bootstrap_check")
def test_partially_bootstrapped_python_runs_its_stages(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("lint", "python", tmp_path)
    assert [s.name for s in stages] == ["install", "lint"]
    assert stages[0].mode == "fail_fast"


def test_not_bootstrapped_stage_names_every_missing_manifest(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stage = _not_bootstrapped_stage("lint", "python", ["pyproject.toml", "uv.lock"])
    with patch(_MOD + ".progress.mark_warn") as m_warn:
        stage.fn(None)
    reason = (
        "skipped: no pyproject.toml, uv.lock (python repo not bootstrapped yet); "
        "add them to run the lint stage"
    )
    m_warn.assert_called_once_with(reason)
    assert f"NOTICE: lint: {reason}" in capsys.readouterr().out


# -- Sources with no manifest are not unbootstrapped (issue #4384, R1) ---------

_SOURCES_NO_MANIFEST = (
    "no Cargo.toml, Cargo.lock, but the repo has rust sources (src/lib.rs); only a repo "
    "with no sources is skipped as not bootstrapped yet, so add the manifests or remove "
    "the sources"
)


@pytest.mark.usefixtures("real_bootstrap_check")
def test_rust_sources_without_cargo_toml_fail_install(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A branch that deletes Cargo.toml, or adds sources without one, must not
    # pass every language gate as a warning.
    _write_config(tmp_path, "rust")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("")
    rc, ran = _run_validate(tmp_path, [])
    assert rc == 1
    assert ran == []
    record = read_record(tmp_path)
    assert record.not_passed[0] == "install"
    assert "lint" not in record.passed
    assert _SOURCES_NO_MANIFEST in capsys.readouterr().out


@pytest.mark.usefixtures("real_bootstrap_check")
def test_rust_sources_without_cargo_toml_stage_shape(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "lib.rs").write_text("")
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages(None, "rust", tmp_path)
    language_stages = [s for s in stages if s.name not in ("common", "semgrep", "trivy", "docs")]
    assert [s.name for s in language_stages] == ["install", "lint", "typecheck", "test", "audit"]
    assert language_stages[0].mode == "fail_fast"
    with pytest.raises(ValidationError, match="add the manifests or remove the sources"):
        language_stages[0].fn(None)


@pytest.mark.usefixtures("real_bootstrap_check")
def test_rust_sources_without_cargo_toml_check_custom_fails_install(tmp_path: Path) -> None:
    _write_config(tmp_path, "rust")
    (tmp_path / "main.rs").write_text("")
    rc, ran = _run_validate(tmp_path, ["--check", "custom"], custom="/path/to/custom")
    assert rc == 1
    assert ran == []
    assert read_record(tmp_path).not_passed[0] == "install"


# -- The doctest probe (issue #4378) on a repo with no Cargo.toml (issue #4384) --


@pytest.mark.usefixtures("real_bootstrap_check")
@pytest.mark.parametrize("sources", [False, True], ids=["unbootstrapped", "sources-no-manifest"])
def test_rust_without_cargo_toml_never_probes_for_doctests(tmp_path: Path, sources: bool) -> None:
    # The library-target probe runs `cargo metadata`, which fails with no
    # Cargo.toml; an unbootstrapped repo's test stage is a skip and a repo
    # with sources but no manifest stops at its install, so neither probes.
    _write_config(tmp_path, "rust")
    if sources:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "lib.rs").write_text("")
    with patch(_MOD + "._rust_lib_packages") as m_probe:
        rc, _ran = _run_validate(tmp_path, ["--check", "test"])
    m_probe.assert_not_called()
    assert rc == (1 if sources else 0)
    record = read_record(tmp_path)
    if sources:
        assert record.not_passed[0] == "install"
        assert "test" not in record.passed
    else:
        assert record.passed == ("install", "test")


@pytest.mark.usefixtures("real_bootstrap_check")
def test_other_language_sources_do_not_block_the_skip(tmp_path: Path) -> None:
    # A Rust repo's helper scripts are not Rust sources.
    _write_config(tmp_path, "rust")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "helper.py").write_text("")
    rc, ran = _run_validate(tmp_path, [])
    assert rc == 0
    assert ran == []


@pytest.mark.usefixtures("real_bootstrap_check")
def test_ignored_and_deleted_sources_do_not_block_the_skip(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("target/\n")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "build.rs").write_text("")
    gone = tmp_path / "gone.rs"
    gone.write_text("")
    subprocess.run(["git", "-C", str(tmp_path), "add", "gone.rs"], check=True)  # noqa: S607
    gone.unlink()
    assert _repo_files(tmp_path) == [".gitignore"]
    with patch(_MOD + "._find_custom_validator", return_value=None):
        stages = _build_stages("lint", "rust", tmp_path)
    assert [s.mode for s in stages] == ["warn", "warn"]


def test_install_stage_choices(tmp_path: Path) -> None:
    cmds = [["cargo", "fetch", "--locked"]]
    assert _install_stage("rust", [], [], [], repo_root=tmp_path) is None
    assert _install_stage("rust", [], ["Cargo.toml"], [], repo_root=tmp_path) is None
    for missing, mode in (([], "fail_fast"), (["Cargo.toml"], "warn")):
        stage = _install_stage("rust", cmds, missing, [], repo_root=tmp_path)
        assert stage is not None
        assert stage.mode == mode
    failed = _install_stage("rust", [], ["Cargo.toml"], ["src/lib.rs"], repo_root=tmp_path)
    assert failed is not None
    assert failed.mode == "fail_fast"


def test_sources_without_manifest_stage_names_at_most_three_sources() -> None:
    sources = ["a.py", "b.py", "c.py", "d.py", "e.py"]
    stage = _sources_without_manifest_stage("python", ["pyproject.toml", "uv.lock"], sources)
    assert stage.name == "install"
    msg = (
        "no pyproject.toml, uv.lock, but the repo has python sources (a.py, b.py, c.py "
        "and 2 more); only a repo with no sources is skipped as not bootstrapped yet, "
        "so add the manifests or remove the sources"
    )
    with pytest.raises(ValidationError) as exc:
        stage.fn(None)
    assert str(exc.value) == msg


# -- semgrep stage (issue #4003) -------------------------------------------------

_CLEAN_SARIF: dict[str, object] = {
    "version": "2.1.0",
    "runs": [{"tool": {"driver": {"name": "semgrep"}}}],
}


def _sarif_result(rule: str, *, suppressed: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "ruleId": rule,
        "level": "error",
        "message": {"text": f"{rule} matched"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": "src/app.py"},
                    "region": {"startLine": 7},
                }
            }
        ],
    }
    if suppressed:
        result["suppressions"] = [{"kind": "inSource"}]
    return result


def _sarif(*results: dict[str, object]) -> dict[str, object]:
    return {
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "semgrep"}}, "results": list(results)}],
    }


def _git_repo(root: Path, *files: str) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S607
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    return root


def _fake_semgrep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sarif: dict[str, object] | None,
    rc: int = 0,
    stderr: str = "",
) -> Path:
    """Put a ``semgrep`` first on PATH that logs its argv and writes *sarif*.

    The SARIF goes to the path after ``--output``; ``None`` writes nothing.
    Returns the argv log.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "semgrep-argv.json"
    payload = tmp_path / "fake-sarif.json"
    if sarif is not None:
        payload.write_text(json.dumps(sarif))
    script = fake_bin / "semgrep"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, shutil, sys\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"payload = pathlib.Path({str(payload)!r})\n"
        "out = sys.argv[sys.argv.index('--output') + 1]\n"
        "if payload.exists():\n"
        "    shutil.copy(payload, out)\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({rc})\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    return log


def test_build_stages_check_semgrep_runs_only_semgrep() -> None:
    with patch(_MOD + ".language_commands") as m_cmds:
        stages = _build_stages("semgrep", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["semgrep"]
    assert stages[0].mode == "fail_defer"
    m_cmds.assert_not_called()


def test_build_stages_semgrep_comes_before_custom() -> None:
    with (
        patch(_MOD + ".language_commands", return_value=[["echo"]]),
        patch(_MOD + "._find_custom_validator", return_value="/path/to/custom"),
    ):
        stages = _build_stages(None, "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages][-5:] == ["audit", "semgrep", "trivy", "docs", "custom"]


def test_semgrep_stage_runs_the_scan_for_its_language() -> None:
    stage = _semgrep_stage("go", Path("/r"))
    with patch(_MOD + "._run_semgrep") as m_run:
        stage.fn(None)
    m_run.assert_called_once_with("go", Path("/r"))


def test_check_semgrep_runs_only_the_semgrep_stage(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks") as m_common,
        patch(_MOD + ".progress.run") as m_run,
        patch(_MOD + "._run_semgrep") as m_semgrep,
    ):
        assert main(["--check", "semgrep"]) == 0
    m_semgrep.assert_called_once_with("python", tmp_path)
    m_common.assert_not_called()
    m_run.assert_not_called()


def test_check_semgrep_failure_fails_the_run(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_semgrep", side_effect=ValidationError("1 semgrep finding(s)")),
    ):
        assert main(["--check", "semgrep"]) == 1


def test_semgrep_rulesets_language_only(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "src/app.py")
    assert _semgrep_rulesets("python", repo) == ["p/python"]


def test_semgrep_rulesets_add_dockerfile_rules_for_a_dockerfile_anywhere(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "docker/base/Dockerfile.template")
    assert _semgrep_rulesets("python", repo) == ["p/python", "p/dockerfile"]


@pytest.mark.parametrize("name", ["ci.yml", "ci.yaml", "nested/ci.yml"])
def test_semgrep_rulesets_add_workflow_rules(tmp_path: Path, name: str) -> None:
    repo = _git_repo(tmp_path, f".github/workflows/{name}")
    assert _semgrep_rulesets("rust", repo) == ["p/rust", "p/github-actions"]


def test_semgrep_rulesets_ignore_non_yaml_in_workflows_and_yaml_elsewhere(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, ".github/workflows/README.md", ".github/dependabot.yml")
    assert _semgrep_rulesets("go", repo) == ["p/golang"]


def test_semgrep_rulesets_skip_gitignored_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "build/Dockerfile")
    (repo / ".gitignore").write_text("build/\n")
    assert _semgrep_rulesets("python", repo) == ["p/python"]


@pytest.mark.parametrize(
    ("language", "expected"),
    [("cpp", ["p/c"]), ("typescript", ["p/typescript"])],
)
def test_semgrep_rulesets_resolve_the_language_ruleset(
    tmp_path: Path, language: str, expected: list[str]
) -> None:
    repo = _git_repo(tmp_path, "README.md")
    assert _semgrep_rulesets(language, repo) == expected


def test_semgrep_rulesets_none_for_a_language_less_repo(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, "README.md")
    assert _semgrep_rulesets(None, repo) == []


def test_run_semgrep_clean_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path / "repo", "src/app.py", ".github/workflows/ci.yml")
    log = _fake_semgrep(tmp_path, monkeypatch, sarif=_CLEAN_SARIF)

    _run_semgrep("python", repo)

    argv = json.loads(log.read_text())
    assert argv[:2] == ["scan", "--sarif"]
    assert argv[argv.index("--metrics") + 1] == "off"
    configs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--config"]
    assert configs == ["p/python", "p/github-actions"]
    assert "--exclude-rule" in argv
    assert argv[-1] == str(repo)
    # The SARIF is written outside the working tree.
    output = Path(argv[argv.index("--output") + 1])
    assert not output.is_relative_to(repo)
    assert not output.exists()
    out = capsys.readouterr().out
    assert "Running (semgrep): semgrep scan --config p/python --config p/github-actions" in out
    assert "semgrep: no findings (0 suppressed in source)" in out


def test_run_semgrep_finding_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path / "repo", "src/app.py")
    _fake_semgrep(tmp_path, monkeypatch, sarif=_sarif(_sarif_result("py.rule")), rc=1)

    with pytest.raises(ValidationError, match=r"^1 semgrep finding\(s\)$"):
        _run_semgrep("python", repo)

    captured = capsys.readouterr()
    assert "ERROR: src/app.py:7: [py.rule] py.rule matched" in captured.err
    assert "**1 finding(s)**" in captured.out


def test_run_semgrep_suppressed_finding_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path / "repo", "src/app.py")
    sarif = _sarif(_sarif_result("py.rule", suppressed=True))
    _fake_semgrep(tmp_path, monkeypatch, sarif=sarif, rc=0)

    _run_semgrep("python", repo)

    out = capsys.readouterr().out
    assert "**1 suppressed finding(s)**" in out
    assert "semgrep: no findings (1 suppressed in source)" in out


def test_run_semgrep_scan_error_fails_with_its_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path / "repo", "src/app.py")
    _fake_semgrep(tmp_path, monkeypatch, sarif=None, rc=2, stderr="cannot reach semgrep.dev\n")

    with pytest.raises(
        ValidationError,
        match=r"^semgrep scan failed with exit code 2: cannot reach semgrep\.dev$",
    ):
        _run_semgrep("python", repo)

    assert "--- semgrep stderr ---" in capsys.readouterr().err


def test_run_semgrep_without_sarif_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path / "repo", "src/app.py")
    _fake_semgrep(tmp_path, monkeypatch, sarif=None, rc=0)

    with pytest.raises(ValidationError, match=r"^semgrep exited 0 but wrote no SARIF output$"):
        _run_semgrep("python", repo)


def test_run_semgrep_without_rulesets_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path, "README.md")
    with (
        patch(_MOD + ".run_scan") as m_scan,
        patch(_MOD + ".progress.mark_warn") as m_warn,
    ):
        _run_semgrep(None, repo)
    m_scan.assert_not_called()
    reason = "no semgrep rulesets for language <not set>, and no Dockerfile or workflow to scan"
    m_warn.assert_called_once_with(reason)
    assert f"NOTICE: semgrep: {reason}" in capsys.readouterr().out


# -- trivy stage (issue #4445) ---------------------------------------------------


def _fake_trivy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fs_rc: int = 0,
    sarif_rc: int = 0,
    report: bool = True,
) -> Path:
    """Put a ``trivy`` first on PATH that plays scan.sh's three commands.

    It logs each argv and working directory, and the files the scan target
    holds. ``trivy fs`` exits *fs_rc*; the SARIF ``convert`` writes its
    ``--output`` when *report* and exits *sarif_rc*. Returns the log.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    log = tmp_path / "trivy-calls.jsonl"
    script = fake_bin / "trivy"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "argv = sys.argv[1:]\n"
        "files = sorted("
        "str(p.relative_to('.')) for p in pathlib.Path('.').rglob('*') if p.is_file())\n"
        f"with open({str(log)!r}, 'a') as fh:\n"
        "    fh.write(json.dumps({'argv': argv, 'cwd': os.getcwd(), 'files': files}) + '\\n')\n"
        "if argv[0] == 'fs':\n"
        f"    sys.exit({fs_rc})\n"
        "if '--format' in argv and argv[argv.index('--format') + 1] == 'sarif':\n"
        f"    if {report!r}:\n"
        "        pathlib.Path(argv[argv.index('--output') + 1]).write_text('{}')\n"
        f"    sys.exit({sarif_rc})\n"
        "print('TABLE OF FINDINGS')\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("VRG_TRIVY_CACHE_DIR", str(tmp_path / "trivy-cache"))
    return log


def _trivy_calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_build_stages_check_trivy_runs_only_trivy() -> None:
    with patch(_MOD + ".language_commands") as m_cmds:
        stages = _build_stages("trivy", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["trivy"]
    assert stages[0].mode == "fail_defer"
    m_cmds.assert_not_called()


def test_trivy_stage_runs_the_scan() -> None:
    stage = _trivy_stage(Path("/r"))
    with patch(_MOD + "._run_trivy") as m_run:
        stage.fn(None)
    m_run.assert_called_once_with(Path("/r"))


def test_check_trivy_runs_only_the_trivy_stage(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".language_commands") as m_cmds,
        patch(_MOD + "._run_trivy") as m_trivy,
    ):
        assert main(["--check", "trivy"]) == 0
    m_trivy.assert_called_once_with(tmp_path)
    m_cmds.assert_not_called()


def test_check_trivy_failure_fails_the_run(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_trivy", side_effect=ValidationError("Trivy found vulnerabilities")),
    ):
        assert main(["--check", "trivy"]) == 1


def test_run_trivy_clean_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path / "repo", "uv.lock", ".trivyignore", "build/out.txt")
    (repo / ".gitignore").write_text("build/\n")
    log = _fake_trivy(tmp_path, monkeypatch)

    _run_trivy(repo)

    calls = _trivy_calls(log)
    assert [call["argv"][:2] for call in calls] == [
        ["fs", "--scanners"],
        ["convert", "--severity"],
        ["convert", "--severity"],
    ]
    fs_argv = calls[0]["argv"]
    assert fs_argv[-1] == "."
    cache = (tmp_path / "trivy-cache").resolve()
    assert fs_argv[fs_argv.index("--cache-dir") + 1] == str(cache)
    # The scan runs from a copy of the checkout, outside the repo: the
    # ignored build/ is not in it, and the .trivyignore is.
    cwd = Path(str(calls[0]["cwd"]))
    assert not cwd.is_relative_to(repo)
    assert calls[0]["files"] == [".gitignore", ".trivyignore", "uv.lock"]
    assert {call["cwd"] for call in calls} == {str(cwd)}
    # The reports went to the temporary directory, which is gone.
    assert not cwd.exists()
    assert sorted(p.name for p in repo.iterdir()) == [
        ".git",
        ".gitignore",
        ".trivyignore",
        "build",
        "uv.lock",
    ]
    assert (cache / "vrg-validate.lock").is_file()
    out = capsys.readouterr().out
    assert f"trivy: vulnerability database cache {cache}" in out
    assert "TABLE OF FINDINGS" in out
    assert "trivy: no CRITICAL,HIGH vulnerabilities" in out


def test_run_trivy_findings_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path / "repo", "requirements.txt")
    log = _fake_trivy(tmp_path, monkeypatch, sarif_rc=1)

    with pytest.raises(
        ValidationError,
        match=r"^Trivy found CRITICAL,HIGH vulnerabilities \(exit 1; see the table above\)$",
    ):
        _run_trivy(repo)
    # Findings are never retried.
    assert len(_trivy_calls(log)) == 3


def test_run_trivy_failed_database_fetch_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(tmp_path / "repo", "uv.lock")
    log = _fake_trivy(tmp_path, monkeypatch, fs_rc=1, report=False)
    sleeps: list[float] = []

    with pytest.raises(
        ValidationError, match=r"^Trivy scan errored \(no report produced\) after 3 attempts"
    ):
        _run_trivy(repo, sleep=sleeps.append)
    assert [call["argv"][0] for call in _trivy_calls(log)] == ["fs", "fs", "fs"]
    assert sleeps == [15, 30]
    assert "attempt 3/3" in capsys.readouterr().out


def test_run_trivy_refuses_a_cache_in_the_working_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "repo", "uv.lock")
    log = _fake_trivy(tmp_path, monkeypatch)
    monkeypatch.setenv("VRG_TRIVY_CACHE_DIR", str(repo / ".trivy-cache"))

    with pytest.raises(ValidationError, match=r"is inside the working tree"):
        _run_trivy(repo)
    assert not log.exists()
    assert not (repo / ".trivy-cache").exists()


def test_run_trivy_unusable_cache_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path / "repo", "uv.lock")
    _fake_trivy(tmp_path, monkeypatch)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("VRG_TRIVY_CACHE_DIR", str(blocker / "cache"))

    with pytest.raises(
        ValidationError, match=r"^trivy: cannot use the vulnerability database cache "
    ):
        _run_trivy(repo)


def test_run_trivy_without_trivy_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _git_repo(tmp_path / "repo", "uv.lock")
    monkeypatch.setenv("VRG_TRIVY_CACHE_DIR", str(tmp_path / "trivy-cache"))
    with (
        patch(_MOD + ".progress.run", side_effect=FileNotFoundError("trivy")),
        pytest.raises(ValidationError, match=r"^trivy not found: trivy$"),
    ):
        _run_trivy(repo)


def test_stage_trivy_target_copies_the_checkout_files(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "repo", "a/b.txt", "node_modules/x/package.json")
    (repo / ".gitignore").write_text("node_modules/\n")
    target = tmp_path / "target"
    target.mkdir()
    _stage_trivy_target(repo, target)
    assert sorted(str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()) == [
        ".gitignore",
        "a/b.txt",
    ]


# -- docs stage (issue #4446) ----------------------------------------------------

_DOCS_GITIGNORE = "docs/site/docs/changelog.md\ndocs/site/docs/releases/\nmkdocs.staged.yml\n"
_DOCS_MKDOCS_YML = (
    "site_name: Test\n"
    "docs_dir: docs\n"
    "nav:\n"
    "  - Home: index.md\n"
    "  - Release Notes:\n"
    "      - releases/index.md\n"
)


def _docs_repo(root: Path, *, gitignore: str = _DOCS_GITIGNORE) -> Path:
    """A git repo with a docs site, a changelog and one release note."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)  # noqa: S607
    (root / ".gitignore").write_text(gitignore)
    docs = root / "docs" / "site" / "docs"
    docs.mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n")
    (root / "docs" / "site" / "mkdocs.yml").write_text(_DOCS_MKDOCS_YML)
    (root / "CHANGELOG.md").write_text("# Changelog\n")
    (root / "releases").mkdir()
    (root / "releases" / "v1.2.3.md").write_text("# Release 1.2.3 (2026-01-02)\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S607
    return root


def _fake_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, rc: int = 0) -> Path:
    """Put a *name* first on PATH that logs its argv and banner env, then exits *rc*.

    It also copies the config after ``-f`` beside the log, so a test can read
    what the build was given. Returns the log.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    log = tmp_path / f"{name}-argv.json"
    script = fake_bin / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, shutil, sys\n"
        "argv = sys.argv[1:]\n"
        "config = argv[argv.index('-f') + 1]\n"
        f"shutil.copy(config, {str(tmp_path / 'built-config.yml')!r})\n"
        f"pathlib.Path({str(log)!r}).write_text(json.dumps("
        "{'argv': argv, 'banner': os.environ.get('NO_MKDOCS_2_WARNING')}))\n"
        f"sys.exit({rc})\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    return log


def test_build_stages_check_docs_runs_only_docs() -> None:
    with patch(_MOD + ".language_commands") as m_cmds:
        stages = _build_stages("docs", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["docs"]
    assert stages[0].mode == "fail_defer"
    m_cmds.assert_not_called()


def test_docs_stage_runs_the_build_for_its_language() -> None:
    stage = _docs_stage("go", Path("/r"))
    with patch(_MOD + "._run_docs_build") as m_run:
        stage.fn(None)
    m_run.assert_called_once_with("go", Path("/r"))


def test_check_docs_runs_only_the_docs_stage(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_common_checks") as m_common,
        patch(_MOD + ".progress.run") as m_run,
        patch(_MOD + "._run_docs_build") as m_docs,
    ):
        assert main(["--check", "docs"]) == 0
    m_docs.assert_called_once_with("python", tmp_path)
    m_common.assert_not_called()
    m_run.assert_not_called()


def test_check_docs_failure_fails_the_run(tmp_path: Path) -> None:
    _write_config(tmp_path, "python")
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + "._run_docs_build", side_effect=ValidationError("docs build failed")),
    ):
        assert main(["--check", "docs"]) == 1


def test_run_docs_build_without_a_config_passes_with_a_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch(_MOD + ".progress.run") as m_run:
        _run_docs_build("python", tmp_path)
    m_run.assert_not_called()
    assert "NOTICE: docs: no docs/site/mkdocs.yml; no docs build to run, as in CI" in (
        capsys.readouterr().out
    )


def test_run_docs_build_stages_and_builds_strictly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _docs_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    log = _fake_tool(tmp_path, monkeypatch, "mkdocs")

    _run_docs_build("go", repo)

    data = json.loads(log.read_text())
    argv = data["argv"]
    staged = repo / "docs" / "site" / "mkdocs.staged.yml"
    assert argv[:5] == ["build", "--strict", "-f", str(staged), "-d"]
    # The site is built outside the working tree.
    assert not Path(argv[5]).is_relative_to(repo)
    assert data["banner"] == "1"
    # The build gets the nav-patched copy; the tracked config is untouched.
    assert "      - 1.2.3: releases/v1.2.3.md" in (tmp_path / "built-config.yml").read_text()
    assert (repo / "docs" / "site" / "mkdocs.yml").read_text() == _DOCS_MKDOCS_YML
    docs = repo / "docs" / "site" / "docs"
    assert (docs / "changelog.md").read_text() == "# Changelog\n"
    assert (docs / "releases" / "v1.2.3.md").is_file()
    assert (docs / "releases" / "index.md").is_file()
    # Every staged path is gitignored: the tree is as clean as before.
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status.splitlines() == [
        "A  .gitignore",
        "A  CHANGELOG.md",
        "A  docs/site/docs/index.md",
        "A  docs/site/mkdocs.yml",
        "A  releases/v1.2.3.md",
    ]
    assert f"Running (docs): mkdocs build --strict -f {staged}" in capsys.readouterr().out


def test_run_docs_build_uses_uv_run_for_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _docs_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    log = _fake_tool(tmp_path, monkeypatch, "uv")

    _run_docs_build("python", repo)

    argv = json.loads(log.read_text())["argv"]
    assert argv[:8] == [
        "run",
        "--frozen",
        "--project",
        str(repo),
        "mkdocs",
        "build",
        "--strict",
        "-f",
    ]


def test_run_docs_build_failure_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _docs_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    _fake_tool(tmp_path, monkeypatch, "mkdocs", rc=1)

    with pytest.raises(
        ValidationError, match=r"^strict mkdocs build of docs/site/mkdocs\.yml exited 1$"
    ):
        _run_docs_build("rust", repo)


def test_run_docs_build_without_mkdocs_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _docs_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    with (
        patch(_MOD + ".progress.run", side_effect=FileNotFoundError("No such file: 'mkdocs'")),
        pytest.raises(ValidationError, match=r"^mkdocs not found: No such file: 'mkdocs'$"),
    ):
        _run_docs_build("rust", repo)


def test_run_docs_build_refuses_unignored_staging_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _docs_repo(tmp_path / "repo", gitignore="mkdocs.staged.yml\n")
    monkeypatch.chdir(repo)
    with (
        patch(_MOD + ".progress.run") as m_run,
        pytest.raises(ValidationError, match="docs staging would write paths that are not"),
    ):
        _run_docs_build("rust", repo)
    m_run.assert_not_called()
    # Nothing was staged.
    assert not (repo / "docs" / "site" / "docs" / "changelog.md").exists()
    assert not (repo / "docs" / "site" / "mkdocs.staged.yml").exists()
