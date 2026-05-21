"""Parse identity VM configuration from ``identities.toml``."""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Identity:
    vm_instance: str
    auth_type: str = "app"
    app_id: str = ""
    installation_id: str = ""
    private_key_path: str = ""
    workspaces: dict[str, str] = field(default_factory=dict)


@dataclass
class IdentityConfig:
    identities: dict[str, Identity] = field(default_factory=dict)


def load_config(path: Path) -> IdentityConfig:
    if not path.exists():
        print(f"ERROR: identity config not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    with path.open("rb") as f:
        raw = tomllib.load(f)

    identities: dict[str, Identity] = {}
    for name, data in raw.get("identities", {}).items():
        identities[name] = Identity(
            vm_instance=data["vm_instance"],
            auth_type=data.get("auth_type", "app"),
            app_id=str(data.get("app_id", "")),
            installation_id=str(data.get("installation_id", "")),
            private_key_path=data.get("private_key_path", ""),
            workspaces=data.get("workspaces", {}),
        )
    return IdentityConfig(identities=identities)


def default_config_path() -> Path:
    return Path.home() / ".config" / "vergil" / "identities.toml"


def resolve_project(config: IdentityConfig, project: str) -> tuple[Identity, str]:
    matches: list[tuple[Identity, str]] = []
    for ident in config.identities.values():
        if project in ident.workspaces:
            matches.append((ident, ident.workspaces[project]))

    if not matches:
        print(
            f"ERROR: project '{project}' not found in any identity",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if len(matches) > 1:
        print(
            f"ERROR: project '{project}' found in multiple identities — ambiguous",
            file=sys.stderr,
        )
        raise SystemExit(1)

    return matches[0]
