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
    resolve_identity_by_name,
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


def test_projects_dir_field(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        projects_dir = "/home/user/projects"
    """)
    )
    cfg = load_config(p)
    assert cfg.identities["vergil"].projects_dir == "/home/user/projects"


def test_projects_dir_defaults_empty(config_file: Path) -> None:
    cfg = load_config(config_file)
    assert cfg.identities["vergil"].projects_dir == ""


def test_default_identity_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "vergil"

        [identities.vergil]
        vm_instance = "vergil-agent"

        [identities.mimir]
        vm_instance = "mimir-agent"
    """)
    )
    cfg = load_config(p)
    assert cfg.default_identity == "vergil"


def test_default_identity_resolves(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "mimir"

        [identities.vergil]
        vm_instance = "vergil-agent"

        [identities.mimir]
        vm_instance = "mimir-agent"
    """)
    )
    cfg = load_config(p)
    ident = resolve_identity(cfg)
    assert ident.vm_instance == "mimir-agent"


def test_default_identity_overridden_by_explicit(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "mimir"

        [identities.vergil]
        vm_instance = "vergil-agent"

        [identities.mimir]
        vm_instance = "mimir-agent"
    """)
    )
    cfg = load_config(p)
    ident = resolve_identity(cfg, "vergil")
    assert ident.vm_instance == "vergil-agent"


def test_default_identity_not_found(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "nonexistent"

        [identities.vergil]
        vm_instance = "vergil-agent"
    """)
    )
    cfg = load_config(p)
    with pytest.raises(SystemExit):
        resolve_identity(cfg)


def test_resolve_identity_by_name_sole(config_file: Path) -> None:
    cfg = load_config(config_file)
    name, ident = resolve_identity_by_name(cfg)
    assert name == "vergil"
    assert ident.vm_instance == "vergil-agent"


def test_resolve_identity_by_name_explicit(config_file: Path) -> None:
    cfg = load_config(config_file)
    name, ident = resolve_identity_by_name(cfg, "vergil")
    assert name == "vergil"
    assert ident.vm_instance == "vergil-agent"


def test_resolve_identity_by_name_default(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "mimir"

        [identities.vergil]
        vm_instance = "vergil-agent"

        [identities.mimir]
        vm_instance = "mimir-agent"
    """)
    )
    cfg = load_config(p)
    name, ident = resolve_identity_by_name(cfg)
    assert name == "mimir"
    assert ident.vm_instance == "mimir-agent"


def test_resolve_identity_by_name_not_found(config_file: Path) -> None:
    cfg = load_config(config_file)
    with pytest.raises(SystemExit):
        resolve_identity_by_name(cfg, "nonexistent")


def test_resolve_identity_by_name_bad_default(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "nonexistent"

        [identities.vergil]
        vm_instance = "vergil-agent"
    """)
    )
    cfg = load_config(p)
    with pytest.raises(SystemExit):
        resolve_identity_by_name(cfg)


def test_resolve_identity_by_name_ambiguous(tmp_path: Path) -> None:
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
        resolve_identity_by_name(cfg)
