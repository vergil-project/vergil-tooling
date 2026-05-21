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
    resolve_project,
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
        installation_id = 67890
        private_key_path = "~/.config/vergil/keys/vergil-agent.pem"

        [identities.vergil.workspaces]
        vergil-tooling = "/projects/vergil-project/vergil-tooling"
        diogenes-core = "/projects/diogenes-project/diogenes-core"
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
    assert ident.installation_id == "67890"
    assert ident.workspaces["vergil-tooling"] == "/projects/vergil-project/vergil-tooling"


def test_resolve_project_found(config_file: Path) -> None:
    cfg = load_config(config_file)
    ident, workspace = resolve_project(cfg, "vergil-tooling")
    assert ident.vm_instance == "vergil-agent"
    assert workspace == "/projects/vergil-project/vergil-tooling"


def test_resolve_project_not_found(config_file: Path) -> None:
    cfg = load_config(config_file)
    with pytest.raises(SystemExit):
        resolve_project(cfg, "nonexistent-project")


def test_resolve_project_ambiguous(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        auth_type = "app"
        app_id = 11111
        installation_id = 22222
        private_key_path = "~/.config/vergil/keys/vergil.pem"
        [identities.vergil.workspaces]
        shared-repo = "/projects/shared-repo"

        [identities.mimir]
        vm_instance = "mimir-agent"
        auth_type = "app"
        app_id = 33333
        installation_id = 44444
        private_key_path = "~/.config/vergil/keys/mimir.pem"
        [identities.mimir.workspaces]
        shared-repo = "/projects/shared-repo"
    """)
    )
    cfg = load_config(p)
    with pytest.raises(SystemExit):
        resolve_project(cfg, "shared-repo")


def test_missing_config_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        load_config(tmp_path / "nonexistent.toml")


def test_default_config_path() -> None:
    result = default_config_path()
    assert result.name == "identities.toml"
    assert result.parent.name == "vergil"
