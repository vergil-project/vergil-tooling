"""Tests for vergil_tooling.bin.vrg_validate."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import mock_open, patch

if TYPE_CHECKING:
    from collections.abc import Iterator

import pytest

from vergil_tooling.bin.vrg_validate import (
    ValidationError,
    _build_stages,
    _command_stage,
    _find_custom_validator,
    _in_dev_container,
    _run_common_checks,
    main,
)
from vergil_tooling.lib.languages import CheckKind

_MOD = "vergil_tooling.bin.vrg_validate"


@pytest.fixture(autouse=True)
def _container_env() -> Iterator[None]:
    with patch(_MOD + "._in_dev_container", return_value=True):
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
        m_cmds.side_effect = lambda lang, kind: [["echo", kind.value]]
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
        m_cmds.side_effect = lambda lang, kind: [["echo", kind.value]]
        stages = _build_stages(None, "python", Path("/tmp/r"))  # noqa: S108
    assert stages[-1].name == "custom"
    assert stages[-1].mode == "fail_defer"


def test_build_stages_check_common_only() -> None:
    stages = _build_stages("common", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["common"]


def test_build_stages_check_lint_runs_install_then_lint() -> None:
    with patch(_MOD + ".language_commands") as m_cmds:
        m_cmds.side_effect = lambda lang, kind: [["echo", kind.value]]
        stages = _build_stages("lint", "python", Path("/tmp/r"))  # noqa: S108
    assert [s.name for s in stages] == ["install", "lint"]
    assert stages[0].mode == "fail_fast"


def test_build_stages_check_lint_no_install_commands() -> None:
    def mock_lang_cmds(lang: str, kind: CheckKind) -> list[list[str]]:
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

    def mock_lang_cmds(lang: str, kind: CheckKind) -> list[list[str]]:
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

    def mock_lang_cmds(lang: str, kind: CheckKind) -> list[list[str]]:
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

    def mock_lang_cmds(lang: str, kind: CheckKind) -> list[list[str]]:
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
