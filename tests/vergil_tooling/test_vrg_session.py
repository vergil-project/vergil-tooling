from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin.vrg_session import build_command, main

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
    """)
    )
    return tmp_path


def test_build_command_with_command() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
        command=["claude", "--model", "claude-opus-4-6"],
    )
    assert cmd == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
        "--",
        "claude",
        "--model",
        "claude-opus-4-6",
    ]


def test_build_command_no_command() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
    )
    assert cmd == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
    ]


def test_build_command_no_workspace() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace=None,
    )
    assert cmd == ["limactl", "shell", "--start", "vergil-agent"]


def test_build_command_command_no_workspace() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace=None,
        command=["ls", "-al"],
    )
    assert cmd == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--",
        "ls",
        "-al",
    ]


def test_identity_not_found(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    with pytest.raises(SystemExit):
        main(["--identity", "nonexistent"])


def test_main_no_args_defaults_to_shell(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )

    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    main([])
    assert len(execed) == 1
    assert execed[0][1] == ["limactl", "shell", "--start", "vergil-agent"]


def test_main_workspace_only_is_shell(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )

    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    main(["vergil-project/vergil-tooling"])
    assert len(execed) == 1
    assert execed[0][1] == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
    ]


def test_main_workspace_and_command(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )

    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    main(["vergil-project/vergil-tooling", "claude", "--model", "claude-opus-4-6"])
    assert len(execed) == 1
    assert execed[0][1] == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
        "--",
        "claude",
        "--model",
        "claude-opus-4-6",
    ]


def test_main_absolute_path(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )

    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    main(["/custom/path", "ls", "-al"])
    assert len(execed) == 1
    assert execed[0][1] == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/custom/path",
        "--",
        "ls",
        "-al",
    ]


def test_main_explicit_config(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    cfg = str(config_dir / ".config" / "vergil" / "identities.toml")
    main(["--config", cfg, "vergil-project/vergil-tooling", "claude"])
    assert len(execed) == 1
    assert execed[0][1] == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
        "--",
        "claude",
    ]


def test_main_explicit_identity(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )

    execed: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session.os.execvp",
        lambda prog, args: execed.append((prog, args)),
    )

    main(["--identity", "vergil", "vergil-project/vergil-tooling", "claude"])
    assert len(execed) == 1
    assert execed[0][1] == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
        "--",
        "claude",
    ]
