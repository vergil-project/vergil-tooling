"""Load and validate the tooling-owned macOS host-dependency manifest.

The manifest (``vergil_tooling/data/host_dependencies.toml``) is the single
source of truth for which host tools Vergil depends on, how each is installed
and upgraded, how its latest version is discovered, which auxiliary checks it
carries, and whether it is pinned (epic #155 pin doctrine).

Security invariant (epic vergil-project/.github#167, spec §10): the manifest
is tooling-owned and **never** repo- or CWD-overridable. ``custom`` probe
commands are trusted, tooling-authored content, so the manifest is only ever
read from the installed ``vergil_tooling.data`` package.

Every malformed entry raises :class:`ManifestError` — unknown enum values,
missing or mistyped fields, unknown keys, a pin without a ``reason`` — so a
typo can never silently fall back to a default.
"""

from __future__ import annotations

import datetime
import tomllib
from dataclasses import dataclass
from importlib import resources
from typing import Any

_MANIFEST_FILE = "host_dependencies.toml"

_METHODS = frozenset({"brew-formula", "brew-cask", "uv-tool", "custom"})
_UPGRADE = frozenset({"auto", "manual"})
_LATEST = frozenset({"probe", "unmanaged"})
_PIN_STATES = frozenset({"active", "under-evaluation", "freed"})

_DEP_KEYS = frozenset({"name", "why", "method", "upgrade", "latest", "probes", "check", "pin"})
_CHECK_KEYS = frozenset({"name", "probe", "remediation"})
_PIN_KEYS = frozenset(
    {
        "constraint",
        "inducing_release",
        "deterministic",
        "reason",
        "state",
        "tracking_issue",
        "pinned_date",
    }
)


class ManifestError(Exception):
    """The host-dependency manifest is malformed."""


@dataclass(frozen=True)
class AuxCheck:
    """An auxiliary health requirement on a dependency (spec §7.4)."""

    name: str
    probe: list[str]
    remediation: list[str] | None


@dataclass(frozen=True)
class HostPin:
    """A pin per epic #155's schema, plus the host-only ``pinned_date``."""

    constraint: str
    inducing_release: str
    deterministic: bool
    reason: str
    state: str
    tracking_issue: str | None
    pinned_date: str  # ISO YYYY-MM-DD; host extension to #155 for age rendering


@dataclass(frozen=True)
class HostDependency:
    """One Vergil-critical host dependency declared in the manifest."""

    name: str
    why: str
    method: str
    upgrade: str
    latest: str
    probes: dict[str, list[str]]
    checks: tuple[AuxCheck, ...]
    pin: HostPin | None


def _read_manifest_text() -> str:
    """Return the packaged manifest text — never a repo or CWD file."""
    ref = resources.files("vergil_tooling.data").joinpath(_MANIFEST_FILE)
    return ref.read_text(encoding="utf-8")


def _check_keys(raw: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ManifestError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _req_str(raw: dict[str, Any], key: str, where: str) -> str:
    if key not in raw:
        raise ManifestError(f"{where}: missing required field {key!r}")
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: field {key!r} must be a non-empty string")
    return value


def _opt_str(raw: dict[str, Any], key: str, default: str | None, where: str) -> str | None:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: field {key!r} must be a non-empty string")
    return value


def _enum(raw: dict[str, Any], key: str, default: str, allowed: frozenset[str], where: str) -> str:
    value = raw.get(key, default)
    if value not in allowed:
        raise ManifestError(
            f"{where}: unknown {key} {value!r} (expected one of {', '.join(sorted(allowed))})"
        )
    return str(value)


def _argv(value: Any, what: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise ManifestError(f"{what} must be a non-empty list of non-empty strings")
    return [str(part) for part in value]


def _parse_pin(raw: Any, where: str) -> HostPin:
    where = f"{where} pin"
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: must be a table")
    _check_keys(raw, _PIN_KEYS, where)
    if not raw.get("reason"):
        raise ManifestError(f"{where}: pin without a reason is refused")
    reason = _req_str(raw, "reason", where)
    deterministic = raw.get("deterministic", False)
    if not isinstance(deterministic, bool):
        raise ManifestError(f"{where}: field 'deterministic' must be a boolean")
    pinned_date = raw.get("pinned_date")
    if isinstance(pinned_date, datetime.date) and not isinstance(pinned_date, datetime.datetime):
        # A bare TOML date literal (pinned_date = 2026-10-15) parses to a date.
        pinned_date = pinned_date.isoformat()
    if not isinstance(pinned_date, str):
        raise ManifestError(f"{where}: missing or non-string field 'pinned_date' (YYYY-MM-DD)")
    try:
        datetime.date.fromisoformat(pinned_date)
    except ValueError as exc:
        raise ManifestError(f"{where}: pinned_date {pinned_date!r} is not YYYY-MM-DD") from exc
    return HostPin(
        constraint=_req_str(raw, "constraint", where),
        inducing_release=_req_str(raw, "inducing_release", where),
        deterministic=deterministic,
        reason=reason,
        state=_enum(raw, "state", "active", _PIN_STATES, where),
        tracking_issue=_opt_str(raw, "tracking_issue", None, where),
        pinned_date=pinned_date,
    )


def _parse_check(raw: Any, where: str) -> AuxCheck:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: each check must be a table")
    _check_keys(raw, _CHECK_KEYS, f"{where} check")
    name = _req_str(raw, "name", f"{where} check")
    cwhere = f"{where} check {name!r}"
    if "probe" not in raw:
        raise ManifestError(f"{cwhere}: missing required field 'probe'")
    probe = _argv(raw["probe"], f"{cwhere}: probe")
    remediation = (
        _argv(raw["remediation"], f"{cwhere}: remediation") if "remediation" in raw else None
    )
    return AuxCheck(name=name, probe=probe, remediation=remediation)


def _parse_dep(raw: Any, index: int) -> HostDependency:
    if not isinstance(raw, dict):
        raise ManifestError(f"dependency #{index}: must be a table")
    name = _req_str(raw, "name", f"dependency #{index}")
    where = f"dependency {name!r}"
    _check_keys(raw, _DEP_KEYS, where)
    method = raw.get("method")
    if method not in _METHODS:
        raise ManifestError(f"{where}: unknown method {method!r}")
    probes_raw = raw.get("probes", {})
    if not isinstance(probes_raw, dict):
        raise ManifestError(f"{where}: probes must be a table")
    probes = {key: _argv(cmd, f"{where}: probe {key!r}") for key, cmd in probes_raw.items()}
    checks_raw = raw.get("check", [])
    if not isinstance(checks_raw, list):
        raise ManifestError(f"{where}: check must be an array of tables")
    checks = tuple(_parse_check(c, where) for c in checks_raw)
    names = [c.name for c in checks]
    if len(names) != len(set(names)):
        raise ManifestError(f"{where}: duplicate check names")
    return HostDependency(
        name=name,
        why=_opt_str(raw, "why", "", where) or "",
        method=str(method),
        upgrade=_enum(raw, "upgrade", "auto", _UPGRADE, where),
        latest=_enum(raw, "latest", "probe", _LATEST, where),
        probes=probes,
        checks=checks,
        pin=_parse_pin(raw["pin"], where) if "pin" in raw else None,
    )


def load_manifest() -> tuple[HostDependency, ...]:
    """Load and validate the packaged host-dependency manifest.

    Raises:
        ManifestError: the manifest is not valid TOML or any entry is malformed.
    """
    try:
        data = tomllib.loads(_read_manifest_text())
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"host-dependency manifest is not valid TOML: {exc}") from exc
    _check_keys(data, frozenset({"dependency"}), "manifest")
    raw_deps = data.get("dependency", [])
    if not isinstance(raw_deps, list):
        raise ManifestError("manifest: 'dependency' must be an array of tables")
    deps = tuple(_parse_dep(raw, i) for i, raw in enumerate(raw_deps))
    seen: set[str] = set()
    for dep in deps:
        if dep.name in seen:
            raise ManifestError(f"duplicate dependency name {dep.name!r}")
        seen.add(dep.name)
    return deps
