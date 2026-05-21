from __future__ import annotations

import json
import subprocess
import textwrap
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin.vrg_session import build_command, check_vm_running, main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / ".config" / "vergil"
    cfg.mkdir(parents=True)
    (cfg / "identities.toml").write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        auth_type = "app"
        app_id = 12345
        installation_id = 67890
        private_key_path = "~/.config/vergil/keys/vergil-agent.pem"

        [identities.vergil.workspaces]
        vergil-tooling = "/projects/vergil-project/vergil-tooling"
    """)
    )
    return tmp_path


def test_build_command_claude_session() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
        api_key="sk-test-key",
        shell_only=False,
    )
    assert cmd[0] == "limactl"
    assert cmd[1] == "shell"
    assert cmd[2] == "vergil-agent"
    joined = " ".join(cmd)
    assert "ANTHROPIC_API_KEY=sk-test-key" in joined
    assert "claude" in joined


def test_build_command_shell_only() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
        api_key="sk-test-key",
        shell_only=True,
    )
    joined = " ".join(cmd)
    assert "claude" not in joined
    assert "cd" in joined


def test_build_command_no_workspace() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace=None,
        api_key="sk-test-key",
        shell_only=True,
    )
    joined = " ".join(cmd)
    assert "cd" not in joined


def test_missing_api_key(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    with pytest.raises(SystemExit):
        main(["vergil-tooling"])


def test_no_project_or_identity(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    with pytest.raises(SystemExit):
        main([])


def test_identity_not_found(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    with pytest.raises(SystemExit):
        main(["--shell", "--identity", "nonexistent"])


def test_check_vm_running_true(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        *args: object,
        **kwargs: object,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps([{"name": "vergil-agent", "status": "Running"}]),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert check_vm_running("vergil-agent") is True


def test_check_vm_running_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        *args: object,
        **kwargs: object,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps([{"name": "vergil-agent", "status": "Stopped"}]),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert check_vm_running("vergil-agent") is False


def test_check_vm_running_command_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        *args: object,
        **kwargs: object,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert check_vm_running("vergil-agent") is False


def test_check_vm_running_not_in_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        *args: object,
        **kwargs: object,  # noqa: ARG001
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps([{"name": "other-vm", "status": "Running"}]),
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert check_vm_running("vergil-agent") is False


def test_main_identity_only(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    monkeypatch.setattr("vergil_tooling.bin.vrg_session.check_vm_running", lambda _: True)

    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    main(["--shell", "--identity", "vergil"])
    assert len(execed) == 1
    assert execed[0][0] == "limactl"


def test_main_launches_session(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    monkeypatch.setattr("vergil_tooling.bin.vrg_session.check_vm_running", lambda _: True)

    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    main(["vergil-tooling"])
    assert len(execed) == 1
    assert "claude" in " ".join(execed[0][1])


def test_main_starts_vm_if_not_running(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    monkeypatch.setattr("vergil_tooling.bin.vrg_session.check_vm_running", lambda _: False)

    started: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> None:
        started.append(cmd)

    monkeypatch.setattr("vergil_tooling.bin.vrg_session.subprocess.run", fake_run)
    monkeypatch.setattr("vergil_tooling.bin.vrg_session.os.execvp", lambda *_: None)

    main(["vergil-tooling"])
    assert len(started) == 1
    assert started[0] == ["limactl", "start", "vergil-agent"]
