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
    resolve_model,
    resolve_session_archive_days,
    resolve_session_stale_days,
    resolve_vergil_version,
    resolve_vm_tag,
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
    assert ident.model == ""  # defaults to empty when unset


def test_identity_model_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        model = "opus"
    """)
    )
    ident = load_config(p).identities["vergil"]
    assert ident.model == "opus"


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
    assert resolve_workspace("vergil-project/vergil-tooling", "/home/user/dev") == (
        "/home/user/dev/vergil-project/vergil-tooling"
    )


def test_resolve_workspace_absolute() -> None:
    assert resolve_workspace("/custom/path", "/home/user/dev") == "/custom/path"


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


def test_vergil_version_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
    """)
    )
    cfg = load_config(p)
    assert cfg.vergil == "v2.0"


def test_vergil_version_per_identity(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
        vergil = "v2.2"
    """)
    )
    cfg = load_config(p)
    assert cfg.identities["vergil"].vergil == "v2.2"


def test_resolve_vergil_version_from_config() -> None:
    identity = Identity(vm_instance="test")
    config = IdentityConfig(identities={"test": identity}, vergil="v2.0")
    assert resolve_vergil_version(config, identity) == "v2.0"


def test_resolve_vergil_version_identity_overrides() -> None:
    identity = Identity(vm_instance="test", vergil="v2.2")
    config = IdentityConfig(identities={"test": identity}, vergil="v2.0")
    assert resolve_vergil_version(config, identity) == "v2.2"


def test_resolve_vergil_version_missing() -> None:
    identity = Identity(vm_instance="test")
    config = IdentityConfig(identities={"test": identity})
    with pytest.raises(SystemExit):
        resolve_vergil_version(config, identity)


def test_vergil_vm_parsed_config_level(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        vergil = "v2.0"
        vergil-vm = "v2.1"

        [identities.vergil]
        vm_instance = "vergil-agent"
    """)
    )
    cfg = load_config(p)
    assert cfg.vergil_vm == "v2.1"


def test_vergil_vm_parsed_identity_level(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
        vergil-vm = "v2.1"
    """)
    )
    cfg = load_config(p)
    assert cfg.identities["vergil"].vergil_vm == "v2.1"


def test_resolve_vm_tag_from_identity() -> None:
    identity = Identity(vm_instance="test", vergil_vm="v2.1")
    config = IdentityConfig(identities={"test": identity}, vergil="v2.0", vergil_vm="v1.9")
    assert resolve_vm_tag(config, identity) == "v2.1"


def test_resolve_vm_tag_from_config() -> None:
    identity = Identity(vm_instance="test")
    config = IdentityConfig(identities={"test": identity}, vergil="v2.0", vergil_vm="v2.1")
    assert resolve_vm_tag(config, identity) == "v2.1"


def test_resolve_vm_tag_falls_back_to_vergil() -> None:
    identity = Identity(vm_instance="test")
    config = IdentityConfig(identities={"test": identity}, vergil="v2.0")
    assert resolve_vm_tag(config, identity) == "v2.0"


def test_claude_token_path_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        claude_token_path = "~/.config/vergil/keys/claude-oauth-token"
    """)
    )
    cfg = load_config(p)
    expected = "~/.config/vergil/keys/claude-oauth-token"
    assert cfg.identities["vergil"].claude_token_path == expected


def test_claude_token_path_defaults_empty(config_file: Path) -> None:
    cfg = load_config(config_file)
    assert cfg.identities["vergil"].claude_token_path == ""


def test_resource_fields_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        cpus = 12
        memory = "32GiB"
        disk = "100GiB"
    """)
    )
    cfg = load_config(p)
    ident = cfg.identities["vergil"]
    assert ident.cpus == 12
    assert ident.memory == "32GiB"
    assert ident.disk == "100GiB"


def test_resource_fields_default_none(config_file: Path) -> None:
    cfg = load_config(config_file)
    ident = cfg.identities["vergil"]
    assert ident.cpus is None
    assert ident.memory is None
    assert ident.disk is None


def test_resource_validation_rejects_negative_cpus(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        cpus = -1
    """)
    )
    with pytest.raises(SystemExit):
        load_config(p)


def test_resource_validation_rejects_zero_cpus(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        cpus = 0
    """)
    )
    with pytest.raises(SystemExit):
        load_config(p)


def test_resource_validation_rejects_string_cpus(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        cpus = "four"
    """)
    )
    with pytest.raises(SystemExit):
        load_config(p)


def test_resource_validation_rejects_bad_memory(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        memory = "32GB"
    """)
    )
    with pytest.raises(SystemExit):
        load_config(p)


def test_resource_validation_rejects_bad_disk(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        disk = "lots"
    """)
    )
    with pytest.raises(SystemExit):
        load_config(p)


def test_resource_validation_accepts_valid_values(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        cpus = 12
        memory = "32GiB"
        disk = "100GiB"
    """)
    )
    cfg = load_config(p)
    assert cfg.identities["vergil"].cpus == 12


def test_resource_validation_error_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        memory = "32GB"
    """)
    )
    with pytest.raises(SystemExit):
        load_config(p)
    captured = capsys.readouterr()
    assert "vergil" in captured.err
    assert "memory" in captured.err
    assert "<number>GiB" in captured.err


def test_top_level_model_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        model = "opus"

        [identities.vergil]
        vm_instance = "vergil-agent"
    """)
    )
    cfg = load_config(p)
    assert cfg.model == "opus"
    assert cfg.identities["vergil"].model == ""  # identity-level unset


def test_resolve_model_precedence() -> None:
    cfg = IdentityConfig(identities={}, model="config-model")
    with_model = Identity(vm_instance="x", model="ident-model")
    without_model = Identity(vm_instance="x")
    assert resolve_model(cfg, with_model, "cli-model") == "cli-model"  # CLI wins
    assert resolve_model(cfg, with_model) == "ident-model"  # then identity
    assert resolve_model(cfg, without_model) == "config-model"  # then config


def test_resolve_model_none_configured() -> None:
    cfg = IdentityConfig(identities={})
    assert resolve_model(cfg, Identity(vm_instance="x")) == ""


def test_session_thresholds_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        session_stale_days = 5
        session_archive_days = 30
        [identities.vergil]
        vm_instance = "vergil-agent"
        session_stale_days = 2
    """)
    )
    cfg = load_config(p)
    assert cfg.session_stale_days == 5
    assert cfg.session_archive_days == 30
    assert cfg.identities["vergil"].session_stale_days == 2
    assert cfg.identities["vergil"].session_archive_days is None


def test_resolve_session_thresholds_cascade() -> None:
    cfg = IdentityConfig(identities={}, session_stale_days=5, session_archive_days=30)
    ident = Identity(vm_instance="x", session_stale_days=2)
    assert resolve_session_stale_days(cfg, ident) == 2  # identity override
    assert resolve_session_archive_days(cfg, ident) == 30  # falls back to config
    # identity-level archive override
    ident2 = Identity(vm_instance="x", session_archive_days=20)
    assert resolve_session_archive_days(cfg, ident2) == 20


def test_resolve_session_thresholds_builtin_defaults() -> None:
    cfg = IdentityConfig(identities={})
    ident = Identity(vm_instance="x")
    assert resolve_session_stale_days(cfg, ident) == 7
    assert resolve_session_archive_days(cfg, ident) == 14


def test_session_archive_days_zero_disables(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        session_archive_days = 0
        [identities.vergil]
        vm_instance = "vergil-agent"
    """)
    )
    assert load_config(p).session_archive_days == 0


def test_session_archive_days_must_exceed_stale(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        session_stale_days = 10
        session_archive_days = 5
    """)
    )
    with pytest.raises(SystemExit):
        load_config(p)
