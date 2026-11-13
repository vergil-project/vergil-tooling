"""The Rust resolver extension (epic vergil-project/.github#407, spec §6.4-§6.5).

Rust has two rules, both specified in epic vergil-project/.github#1095, spec §6.4:

* **Floor.** The root ``Cargo.toml``'s ``rust-version`` below the window's
  oldest version is a warning. It is read from ``[package].rust-version`` when
  that is a string, and from ``[workspace.package].rust-version`` when
  ``[package].rust-version`` is ``{ workspace = true }`` or there is no
  ``[package]`` (a virtual workspace). Workspace members are not checked. A
  floor above the window's oldest version is a deliberate restriction and is
  allowed.
* **Toolchain selection.** ``rust-toolchain.toml``, or the legacy
  ``rust-toolchain``, chooses the toolchain behind Vergil's back, so either is
  an error. The Vergil Rust image pins its own installed release through
  ``RUSTUP_TOOLCHAIN``, so Rust has no container-environment hook.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.config import _version_sort_key
from vergil_tooling.lib.versions import VersionsError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["check_floor", "check_toolchain"]

_CARGO_TOML = "Cargo.toml"
_TOOLCHAIN_FILES = ("rust-toolchain.toml", "rust-toolchain")
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?")


def _read_cargo_toml(path: Path) -> dict[str, Any] | None:
    """*path* parsed, or ``None`` when it does not exist. Invalid TOML is fatal."""
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise VersionsError(msg) from exc


def _table(data: dict[str, Any], key: str, where: str, path: Path) -> dict[str, Any] | None:
    value = data.get(key)
    if value is None or isinstance(value, dict):
        return value
    msg = f"{path}: {where} is not a table"
    raise VersionsError(msg)


def _workspace_floor(data: dict[str, Any], path: Path, *, required: bool) -> object:
    """``[workspace.package].rust-version``, or ``None`` when absent and not *required*."""
    workspace = _table(data, "workspace", "[workspace]", path) or {}
    package = _table(workspace, "package", "[workspace.package]", path) or {}
    raw = package.get("rust-version")
    if raw is None and required:
        msg = (
            f"{path}: [package].rust-version inherits from the workspace, but "
            f"[workspace.package].rust-version is not set"
        )
        raise VersionsError(msg)
    return raw


def _declared_floor(path: Path) -> str | None:
    """The root ``Cargo.toml``'s ``rust-version`` as ``x.y``, or ``None`` when undeclared."""
    data = _read_cargo_toml(path)
    if data is None:
        return None
    package = _table(data, "package", "[package]", path)
    where = "[package].rust-version"
    if package is None:
        where = "[workspace.package].rust-version"
        raw = _workspace_floor(data, path, required=False)
    else:
        raw = package.get("rust-version")
        if isinstance(raw, dict):
            if raw != {"workspace": True}:
                msg = f"{path}: {where} {raw!r} is neither a version nor {{ workspace = true }}"
                raise VersionsError(msg)
            where = "[workspace.package].rust-version"
            raw = _workspace_floor(data, path, required=True)
    if raw is None:
        return None
    version = _VERSION_RE.fullmatch(raw) if isinstance(raw, str) else None
    if version is None:
        msg = f"{path}: {where} {raw!r} is not an x.y or x.y.z Rust version"
        raise VersionsError(msg)
    return f"{version.group(1)}.{version.group(2)}"


def check_floor(repo_root: Path, oldest: str, _newest: str) -> list[str]:
    """Warn when the root ``Cargo.toml``'s ``rust-version`` is below the window's *oldest*."""
    floor = _declared_floor(repo_root / _CARGO_TOML)
    if floor is None:
        return []
    if _version_sort_key(floor) < _version_sort_key(oldest):
        return [
            f"Cargo.toml rust-version floor {floor} is below the oldest supported version "
            f"{oldest}. Fix: raise it to {oldest}"
        ]
    return []


def check_toolchain(repo_root: Path) -> list[str]:
    """One error for each toolchain file at *repo_root* that selects the Rust toolchain."""
    return [
        f"{name} selects the Rust toolchain, which belongs to Vergil (the Vergil Rust "
        f"image pins it). Fix: delete {name}"
        for name in _TOOLCHAIN_FILES
        if (repo_root / name).exists()
    ]
