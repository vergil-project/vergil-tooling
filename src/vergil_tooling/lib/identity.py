"""Parse identity VM configuration from ``identities.toml``."""

from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

_SIZE_PATTERN = re.compile(r"^\d+GiB$")


@dataclass
class Identity:
    vm_instance: str
    auth_type: str = "app"
    app_id: str = ""
    private_key_path: str = ""
    claude_token_path: str = ""
    projects_dir: str = ""
    vergil: str = ""
    vergil_vm: str = ""
    model: str = ""
    cpus: int | None = None
    memory: str | None = None
    disk: str | None = None


@dataclass
class IdentityConfig:
    identities: dict[str, Identity]
    default_identity: str | None = None
    vergil: str = ""
    vergil_vm: str = ""


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


def load_config(path: Path) -> IdentityConfig:
    if not path.exists():
        print(f"ERROR: identity config not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    default_identity = raw.get("default_identity")
    vergil = raw.get("vergil", "")
    vergil_vm = raw.get("vergil-vm", "")

    identities: dict[str, Identity] = {}
    for name, data in raw.get("identities", {}).items():
        identities[name] = Identity(
            vm_instance=data["vm_instance"],
            auth_type=data.get("auth_type", "app"),
            app_id=str(data.get("app_id", "")),
            private_key_path=data.get("private_key_path", ""),
            claude_token_path=data.get("claude_token_path", ""),
            projects_dir=data.get("projects_dir", ""),
            vergil=data.get("vergil", ""),
            vergil_vm=data.get("vergil-vm", ""),
            model=data.get("model", ""),
            cpus=data.get("cpus"),
            memory=data.get("memory"),
            disk=data.get("disk"),
        )
        _validate_identity_resources(name, identities[name])
    return IdentityConfig(
        identities=identities,
        default_identity=default_identity,
        vergil=vergil,
        vergil_vm=vergil_vm,
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


def resolve_workspace(path: str, projects_dir: str) -> str:
    if path.startswith("/"):
        return path
    return f"{projects_dir}/{path}"
