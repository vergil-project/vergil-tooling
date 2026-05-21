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


def test_build_command_claude_session() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
        shell_only=False,
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
    ]


def test_build_command_shell_only() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
        shell_only=True,
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
        shell_only=True,
    )
    assert cmd == ["limactl", "shell", "--start", "vergil-agent"]


def test_build_command_with_claude_args() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
        shell_only=False,
        claude_args=["--model", "claude-opus-4-6"],
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


def test_build_command_claude_args_ignored_in_shell_mode() -> None:
    cmd = build_command(
        vm_instance="vergil-agent",
        workspace="/projects/vergil-project/vergil-tooling",
        shell_only=True,
        claude_args=["--model", "claude-opus-4-6"],
    )
    assert cmd == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
    ]


def test_no_workspace_or_identity(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    with pytest.raises(SystemExit):
        main([])


def test_identity_not_found(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "vergil_tooling.bin.vrg_session._default_config_path",
        lambda: config_dir / ".config" / "vergil" / "identities.toml",
    )
    with pytest.raises(SystemExit):
        main(["--shell", "--identity", "nonexistent"])


def test_main_shell_with_identity(
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

    main(["--shell", "--identity", "vergil"])
    assert len(execed) == 1
    assert execed[0][1] == ["limactl", "shell", "--start", "vergil-agent"]


def test_main_relative_path(
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
        "--",
        "claude",
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

    main(["/custom/absolute/path"])
    assert len(execed) == 1
    assert execed[0][1] == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/custom/absolute/path",
        "--",
        "claude",
    ]


def test_main_passes_claude_args(
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

    main(["vergil-project/vergil-tooling", "--", "--model", "claude-opus-4-6"])
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
    main(["--config", cfg, "vergil-project/vergil-tooling"])
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


def test_main_shell_default_identity(
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

    main(["--shell"])
    assert len(execed) == 1
    assert execed[0][1] == ["limactl", "shell", "--start", "vergil-agent"]


def test_main_shell_with_workspace(
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

    main(["--shell", "vergil-project/vergil-tooling"])
    assert len(execed) == 1
    assert execed[0][1] == [
        "limactl",
        "shell",
        "--start",
        "vergil-agent",
        "--workdir",
        "/projects/vergil-project/vergil-tooling",
    ]
