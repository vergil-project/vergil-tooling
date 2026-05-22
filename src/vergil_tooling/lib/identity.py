"""Parse identity VM configuration from ``identities.toml``."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Identity:
    vm_instance: str
    auth_type: str = "app"
    app_id: str = ""
    private_key_path: str = ""
    projects_dir: str = ""


@dataclass
class IdentityConfig:
    identities: dict[str, Identity]
    default_identity: str | None = None


def load_config(path: Path) -> IdentityConfig:
    if not path.exists():
        print(f"ERROR: identity config not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    default_identity = raw.get("default_identity")

    identities: dict[str, Identity] = {}
    for name, data in raw.get("identities", {}).items():
        identities[name] = Identity(
            vm_instance=data["vm_instance"],
            auth_type=data.get("auth_type", "app"),
            app_id=str(data.get("app_id", "")),
            private_key_path=data.get("private_key_path", ""),
            projects_dir=data.get("projects_dir", ""),
        )
    return IdentityConfig(identities=identities, default_identity=default_identity)


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


_PROJECTS_PREFIX = "/projects"


def resolve_workspace(path: str) -> str:
    if path.startswith("/"):
        return path
    return f"{_PROJECTS_PREFIX}/{path}"
