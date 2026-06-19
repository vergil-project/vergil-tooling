"""Parse identity VM configuration from ``identities.toml``."""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

_SIZE_PATTERN = re.compile(r"^\d+GiB$")

_DEFAULT_SESSION_STALE_DAYS = 7
_DEFAULT_SESSION_ARCHIVE_DAYS = 14


@dataclass
class Identity:
    vm_instance: str
    auth_type: str = "app"
    mode: str = ""  # "user" / "audit", derived from the stanza name at load time
    app_id: str = ""
    private_key_path: str = ""
    claude_token_path: str = ""
    projects_dir: str = ""
    vergil: str = ""
    vergil_vm: str = ""
    model: str = ""
    session_stale_days: int | None = None
    session_archive_days: int | None = None
    cpus: int | None = None
    memory: str | None = None
    disk: str | None = None
    overrides: dict[tuple[str, str], dict[str, object]] = dc_field(default_factory=dict)


@dataclass
class IdentityConfig:
    identities: dict[str, Identity]
    default_identity: str | None = None
    vergil: str = ""
    vergil_vm: str = ""
    model: str = ""
    session_stale_days: int = 7
    session_archive_days: int = 14


def _validate_session_thresholds(
    name: str, identity: Identity, cfg_stale: int, cfg_archive: int
) -> None:
    stale = identity.session_stale_days if identity.session_stale_days is not None else cfg_stale
    archive = (
        identity.session_archive_days if identity.session_archive_days is not None else cfg_archive
    )
    if archive != 0 and archive <= stale:
        print(
            f"ERROR: identity '{name}': session_archive_days ({archive}) must be 0 "
            f"or greater than session_stale_days ({stale})",
            file=sys.stderr,
        )
        raise SystemExit(1)


_VALID_AUTH_TYPES = ("app", "none")


def _validate_auth_type(name: str, identity: Identity) -> None:
    if identity.auth_type not in _VALID_AUTH_TYPES:
        print(
            f"ERROR: identity '{name}': auth_type must be one of "
            f"{', '.join(_VALID_AUTH_TYPES)}, got {identity.auth_type!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _validate_identity_resources(name: str, identity: Identity) -> None:
    if identity.cpus is not None and (not isinstance(identity.cpus, int) or identity.cpus < 1):
        print(
            f"ERROR: identity '{name}': cpus must be a positive integer, got {identity.cpus!r}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    for field in ("memory", "disk"):
        value = getattr(identity, field)
        if value is not None and not _SIZE_PATTERN.fullmatch(value):
            print(
                f"ERROR: identity '{name}': {field} must be '<number>GiB'"
                f' (e.g., "32GiB"), got {value!r}',
                file=sys.stderr,
            )
            raise SystemExit(1)


def derive_identity_mode(name: str) -> str:
    """Derive the identity mode (``"user"`` or ``"audit"``) from an identity name.

    The dual-identity convention names stanzas ``vergil-user`` and
    ``vergil-audit``; the role token in the name is the mode. Returns
    ``""`` when the name contains neither token or both — callers that
    require a mode (VM provisioning) must fail loudly on ``""`` rather
    than guess.
    """
    has_user = "user" in name
    has_audit = "audit" in name
    if has_user == has_audit:
        return ""
    return "user" if has_user else "audit"


def load_config(path: Path) -> IdentityConfig:
    if not path.exists():
        print(f"ERROR: identity config not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    default_identity = raw.get("default_identity")
    vergil = raw.get("vergil", "")
    vergil_vm = raw.get("vergil-vm", "")
    model = raw.get("model", "")
    session_stale_days = raw.get("session_stale_days", _DEFAULT_SESSION_STALE_DAYS)
    session_archive_days = raw.get("session_archive_days", _DEFAULT_SESSION_ARCHIVE_DAYS)

    identities: dict[str, Identity] = {}
    for name, data in raw.get("identities", {}).items():
        # Nested [identities.<id>.<org>.<repo>] tables appear as dict-of-dict values in
        # `data`; scalar identity fields (cpus, etc.) are non-dict and are skipped.
        overrides: dict[tuple[str, str], dict[str, object]] = {}
        for org_key, org_val in data.items():
            if not isinstance(org_val, dict):
                continue
            for repo_key, repo_val in org_val.items():
                if isinstance(repo_val, dict):
                    overrides[(org_key, repo_key)] = repo_val

        identities[name] = Identity(
            vm_instance=data["vm_instance"],
            auth_type=data.get("auth_type", "app"),
            mode=derive_identity_mode(name),
            app_id=str(data.get("app_id", "")),
            private_key_path=data.get("private_key_path", ""),
            claude_token_path=data.get("claude_token_path", ""),
            projects_dir=data.get("projects_dir", ""),
            vergil=data.get("vergil", ""),
            vergil_vm=data.get("vergil-vm", ""),
            model=data.get("model", ""),
            session_stale_days=data.get("session_stale_days"),
            session_archive_days=data.get("session_archive_days"),
            cpus=data.get("cpus"),
            memory=data.get("memory"),
            disk=data.get("disk"),
            overrides=overrides,
        )
        _validate_auth_type(name, identities[name])
        _validate_identity_resources(name, identities[name])
        _validate_session_thresholds(
            name, identities[name], session_stale_days, session_archive_days
        )
    return IdentityConfig(
        identities=identities,
        default_identity=default_identity,
        vergil=vergil,
        vergil_vm=vergil_vm,
        model=model,
        session_stale_days=session_stale_days,
        session_archive_days=session_archive_days,
    )


def default_config_path() -> Path:
    return Path.home() / ".config" / "vergil" / "identities.toml"


def resolve_identity(config: IdentityConfig, name: str | None = None) -> Identity:
    if name is not None:
        if name not in config.identities:
            print(f"ERROR: identity '{name}' not found", file=sys.stderr)
            raise SystemExit(1)
        return config.identities[name]

    if config.default_identity is not None:
        if config.default_identity not in config.identities:
            print(
                f"ERROR: default_identity '{config.default_identity}' not found",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return config.identities[config.default_identity]

    if len(config.identities) == 1:
        return next(iter(config.identities.values()))

    print(
        "ERROR: multiple identities configured — use --identity to select one",
        file=sys.stderr,
    )
    raise SystemExit(1)


def resolve_identity_by_name(
    config: IdentityConfig, name: str | None = None
) -> tuple[str, Identity]:
    if name is not None:
        if name not in config.identities:
            print(f"ERROR: identity '{name}' not found", file=sys.stderr)
            raise SystemExit(1)
        return name, config.identities[name]

    if config.default_identity is not None:
        if config.default_identity not in config.identities:
            print(
                f"ERROR: default_identity '{config.default_identity}' not found",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return config.default_identity, config.identities[config.default_identity]

    if len(config.identities) == 1:
        key = next(iter(config.identities))
        return key, config.identities[key]

    print(
        "ERROR: multiple identities configured — use --identity to select one",
        file=sys.stderr,
    )
    raise SystemExit(1)


def resolve_vergil_version(config: IdentityConfig, identity: Identity) -> str:
    """Return the vergil ecosystem version: identity-level, then config-level."""
    if identity.vergil:
        return identity.vergil
    if config.vergil:
        return config.vergil
    print("ERROR: no 'vergil' version configured in identities.toml", file=sys.stderr)
    raise SystemExit(1)


def resolve_vm_tag(config: IdentityConfig, identity: Identity) -> str:
    """Return the VM template tag: identity vergil-vm, config vergil-vm, then vergil."""
    if identity.vergil_vm:
        return identity.vergil_vm
    if config.vergil_vm:
        return config.vergil_vm
    return resolve_vergil_version(config, identity)


def resolve_model(config: IdentityConfig, identity: Identity, cli_model: str = "") -> str:
    """Return the Claude model: CLI override, then identity, then config-level.

    Returns ``""`` when none is configured, meaning no ``--model`` flag is passed.
    """
    if cli_model:
        return cli_model
    if identity.model:
        return identity.model
    return config.model


def resolve_session_stale_days(config: IdentityConfig, identity: Identity) -> int:
    """Days idle before a session warns: identity, then config-level."""
    if identity.session_stale_days is not None:
        return identity.session_stale_days
    return config.session_stale_days


def resolve_session_archive_days(config: IdentityConfig, identity: Identity) -> int:
    """Days idle before a session auto-archives (0 disables): identity, then config."""
    if identity.session_archive_days is not None:
        return identity.session_archive_days
    return config.session_archive_days


def resolve_workspace(path: str, projects_dir: str) -> str:
    if path.startswith("/"):
        return path
    return f"{projects_dir}/{path}"
