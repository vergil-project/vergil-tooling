from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from vergil_tooling.lib.identity import (
    Identity,
    IdentityConfig,
    default_config_path,
    load_config,
    resolve_identity,
    resolve_workspace,
)


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        auth_type = "app"
        app_id = 12345
        private_key_path = "~/.config/vergil/keys/vergil-agent.pem"
    """)
    )
    return p


def test_load_config(config_file: Path) -> None:
    cfg = load_config(config_file)
    assert isinstance(cfg, IdentityConfig)
    assert "vergil" in cfg.identities


def test_identity_fields(config_file: Path) -> None:
    cfg = load_config(config_file)
    ident = cfg.identities["vergil"]
    assert isinstance(ident, Identity)
    assert ident.vm_instance == "vergil-agent"
    assert ident.auth_type == "app"
    assert ident.app_id == "12345"


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_config(tmp_path / "nonexistent.toml")


def test_default_config_path() -> None:
    result = default_config_path()
    assert result.name == "identities.toml"
    assert result.parent.name == "vergil"


def test_resolve_identity_sole(config_file: Path) -> None:
    cfg = load_config(config_file)
    ident = resolve_identity(cfg)
    assert ident.vm_instance == "vergil-agent"


def test_resolve_identity_by_name(config_file: Path) -> None:
    cfg = load_config(config_file)
    ident = resolve_identity(cfg, "vergil")
    assert ident.vm_instance == "vergil-agent"


def test_resolve_identity_not_found(config_file: Path) -> None:
    cfg = load_config(config_file)
    with pytest.raises(SystemExit):
        resolve_identity(cfg, "nonexistent")


def test_resolve_identity_ambiguous(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"

        [identities.mimir]
        vm_instance = "mimir-agent"
    """)
    )
    cfg = load_config(p)
    with pytest.raises(SystemExit):
        resolve_identity(cfg)


def test_resolve_workspace_relative() -> None:
    assert resolve_workspace("vergil-project/vergil-tooling") == (
        "/projects/vergil-project/vergil-tooling"
    )


def test_resolve_workspace_absolute() -> None:
    assert resolve_workspace("/custom/path") == "/custom/path"
