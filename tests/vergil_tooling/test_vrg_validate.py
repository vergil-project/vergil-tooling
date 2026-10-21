"""Tests for vergil_tooling.bin.vrg_validate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import mock_open, patch

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

import pytest

from vergil_tooling.bin.vrg_validate import (
    _CONAN_AUDIT_TOKEN_ENV,
    ValidationError,
    _build_stages,
    _check_custom_validator,
    _command_stage,
    _find_custom_validator,
    _in_dev_container,
    _probe_xdist,
    _python_test_stage,
    _resolve_test_workers,
    _run_common_checks,
    _skip_conan_audit_scan,
    _test_context,
    main,
)
from vergil_tooling.lib.config import ConfigError, TestConfig
from vergil_tooling.lib.languages import CheckKind

_MOD = "vergil_tooling.bin.vrg_validate"


@pytest.fixture(autouse=True)
def _container_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # The test-tuning env must not leak in from the runner (GitHub Actions sets
    # GITHUB_ACTIONS=true), and no full run may probe the real test environment.
    for name in ("VRG_TEST_WORKERS", "VRG_TEST_CONTEXT", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)
    with (
        patch(_MOD + "._in_dev_container", return_value=True),
        patch(_MOD + "._probe_xdist", return_value=False),
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
        "test",
        "audit",
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
    assert [s.name for s in stages] == ["common"]


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
    assert order == ["common", "install", "lint", "typecheck", "test", "audit"]


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


def test_main_resolution_warning_does_not_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_pinned_config(tmp_path, repo_type="tooling", versions='["3.13", "3.14"]')
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".progress.run_pipeline", return_value=0),
    ):
        assert main(["--check", "common"]) == 0
    assert "WARNING: [ci].versions pins 2 versions" in capsys.readouterr().err


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
