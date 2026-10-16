"""Host pins: the host-local overlay of epic #155 pins onto the host manifest.

``vrg-host pin`` / ``vrg-host unpin`` (epic vergil-project/.github#167, spec §5,
§9) record which manifest dependencies a given human is **holding back**. Pins
are user configuration, not package data, so they live in a host-local file —
``~/.config/vergil/host-pins.toml``, beside ``identities.toml`` — and never in
the packaged ``host_dependencies.toml`` (writing there would mutate the installed
package and be lost on every reinstall). :func:`load_effective_manifest`
overlays the pins onto the packaged manifest so ``status`` (and ``upgrade``) see
the merged view.

File format — one table per pinned dependency, carrying #155's pin schema plus
the host-only ``pinned_date``::

    [pin.gh]
    constraint = "2.40.x"
    inducing_release = "2.41.0"
    deterministic = true
    reason = "2.41.0 breaks vrg-gh auth"
    state = "active"
    tracking_issue = "https://github.com/org/repo/issues/1"
    pinned_date = "2026-10-15"

Invariants:

- **Manifest trust (spec §10).** The pins file can only hold back dependencies
  the packaged manifest already declares. It cannot add dependencies or
  override methods, probes or checks — the only key it carries is ``pin``.
- **No silent failure.** A missing file means "no pins". A malformed file, a pin
  naming an undeclared dependency, a pin with no ``reason``, or a dependency
  pinned both in the manifest and in the file is a loud :class:`PinsError`.
- **Freed pins are kept as history.** ``unpin`` sets ``state = "freed"`` (spec
  §6: "free a pin (state → freed)", #155's lifecycle) rather than deleting the
  record, so the file remains the audit trail of what was held back and why. A
  freed pin holds nothing back; a later ``pin`` of the same dependency replaces
  it.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import re
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import host_deps, host_versions
from vergil_tooling.lib.host_deps import HostPin, ManifestError

if TYPE_CHECKING:
    from vergil_tooling.lib.host_deps import HostDependency

_PIN_COMPONENT = re.compile(r"[0-9A-Za-z]+")

# Field order for the written file (stable, human-readable diffs).
_FIELD_ORDER = (
    "constraint",
    "inducing_release",
    "deterministic",
    "reason",
    "state",
    "tracking_issue",
    "pinned_date",
)


class PinsError(ManifestError):
    """The host pins file is malformed or inconsistent with the manifest."""


def default_pins_path() -> Path:
    """Return the host-local pins file (mirrors ``identity.default_config_path``)."""
    return Path.home() / ".config" / "vergil" / "host-pins.toml"


# -- constraint grammar --------------------------------------------------------------


def parse_constraint(constraint: str) -> list[str]:
    """Return the concrete leading components of a pin *constraint*.

    A constraint is a dotted version prefix whose trailing components may be
    the wildcard ``x`` — ``1.x``, ``2.40.x`` or an exact ``2.40.0`` (spec §5).
    Trailing ``x`` components are dropped.

    Raises:
        ValueError: *constraint* uses any other syntax (operators such as
            ``<=``/``>=``/``~=``, empty components, a non-trailing ``x``, or
            no concrete component at all). It is refused, never guessed at.
    """
    parts = constraint.split(".")
    while parts and parts[-1] == "x":
        parts.pop()
    if not parts or not all(_PIN_COMPONENT.fullmatch(p) and p != "x" for p in parts):
        raise ValueError(
            f"unsupported pin constraint {constraint!r}: expected a dotted version "
            "prefix with optional trailing 'x' wildcards (e.g. '1.x', '2.40.x', '2.40.0')"
        )
    return parts


# -- #155 re-evaluation trigger -------------------------------------------------------


def is_reevaluation_due(pin: HostPin, current_leading: str) -> bool:
    """Return whether the leading edge has moved past the pin's inducing release.

    This is the #155 trigger: a pin is valid only while its ``inducing_release``
    is the leading edge. Versions are ordered by the single host comparator,
    :func:`vergil_tooling.lib.host_versions.compare`.

    Raises:
        host_versions.IncomparableVersionError: either version is outside the
            comparable grammar; the trigger is never guessed at.
    """
    return host_versions.compare(current_leading, pin.inducing_release) > 0


# -- pin creation ---------------------------------------------------------------------


def make_pin(
    *,
    constraint: str,
    inducing_release: str,
    deterministic: bool,
    reason: str,
    tracking_issue: str | None,
    today: datetime.date | None = None,
) -> HostPin:
    """Return a new ``active`` pin stamped with today's ``pinned_date``.

    Raises:
        ValueError: *reason* is blank (the #155 justification gate), the
            *constraint* is not supported syntax, or *inducing_release* or
            *tracking_issue* is blank.
    """
    if not reason.strip():
        raise ValueError("a pin requires a non-empty reason (#155 justification gate)")
    parse_constraint(constraint)
    if not inducing_release.strip():
        raise ValueError("a pin requires a non-empty inducing release")
    if tracking_issue is not None and not tracking_issue.strip():
        raise ValueError("tracking issue, when given, must be non-empty")
    return HostPin(
        constraint=constraint,
        inducing_release=inducing_release,
        deterministic=deterministic,
        reason=reason,
        state="active",
        tracking_issue=tracking_issue,
        pinned_date=(today or datetime.date.today()).isoformat(),
    )


# -- the pins file --------------------------------------------------------------------


def load_pins(path: Path | None = None) -> dict[str, HostPin]:
    """Read and validate the pins file; a missing file means no pins.

    Raises:
        PinsError: the file is not valid TOML or any pin is malformed.
    """
    path = path or default_pins_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PinsError(f"{path}: not valid TOML: {exc}") from exc
    unknown = sorted(set(data) - {"pin"})
    if unknown:
        raise PinsError(f"{path}: unknown key(s) {', '.join(unknown)} (only [pin.<dep>] tables)")
    raw = data.get("pin", {})
    if not isinstance(raw, dict):
        raise PinsError(f"{path}: 'pin' must be a table of [pin.<dep>] tables")
    pins: dict[str, HostPin] = {}
    for name, pin_raw in raw.items():
        try:
            pins[name] = host_deps._parse_pin(pin_raw, f"{path}: {name!r}")  # noqa: SLF001 - shared pin schema
        except ManifestError as exc:
            raise PinsError(str(exc)) from exc
    return pins


def _toml_str(value: str) -> str:
    out = ['"']
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _dump(pins: dict[str, HostPin]) -> str:
    lines = [
        "# Host-local vrg-host pins (epic #155 doctrine). Managed by",
        "# `vrg-host pin` / `vrg-host unpin`; freed pins are kept as history.",
    ]
    for name in sorted(pins):
        fields: dict[str, Any] = dataclasses.asdict(pins[name])
        lines.append("")
        lines.append(f"[pin.{_toml_str(name)}]")
        for key in _FIELD_ORDER:
            value = fields[key]
            if value is None:
                continue
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            else:
                lines.append(f"{key} = {_toml_str(value)}")
    return "\n".join(lines) + "\n"


def _save(pins: dict[str, HostPin], path: Path) -> None:
    """Write *pins* atomically (temp file + rename in the same directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".host-pins.", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_dump(pins))
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _declared(name: str) -> None:
    names = {dep.name for dep in host_deps.load_manifest()}
    if name not in names:
        raise PinsError(
            f"{name!r} is not a dependency declared in the host manifest "
            f"(declared: {', '.join(sorted(names)) or 'none'})"
        )


def write_pin(name: str, pin: HostPin, path: Path | None = None) -> None:
    """Record *pin* for dependency *name*, replacing any earlier pin for it.

    Raises:
        PinsError: *name* is not declared in the manifest, or the existing pins
            file is malformed (it is never silently overwritten).
    """
    _declared(name)
    path = path or default_pins_path()
    pins = load_pins(path)
    pins[name] = pin
    _save(pins, path)


def clear_pin(name: str, path: Path | None = None) -> HostPin:
    """Free the pin on *name* (state → ``freed``, kept as history); return it.

    Raises:
        PinsError: *name* has no pin in the file, or its pin is already freed,
            or the pins file is malformed.
    """
    path = path or default_pins_path()
    pins = load_pins(path)
    pin = pins.get(name)
    if pin is None:
        raise PinsError(f"{name!r} has no pin in {path}")
    if pin.state == "freed":
        raise PinsError(f"{name!r} pin is already freed")
    freed = dataclasses.replace(pin, state="freed")
    pins[name] = freed
    _save(pins, path)
    return freed


def load_effective_manifest(path: Path | None = None) -> tuple[HostDependency, ...]:
    """Return the packaged manifest with the host-local pins overlaid.

    Raises:
        ManifestError: the packaged manifest is malformed.
        PinsError: the pins file is malformed, pins an undeclared dependency,
            or pins a dependency the manifest itself already pins.
    """
    deps = host_deps.load_manifest()
    pins = load_pins(path)
    by_name = {dep.name: dep for dep in deps}
    undeclared = sorted(set(pins) - set(by_name))
    if undeclared:
        raise PinsError(
            f"pins file names undeclared dependenc{'y' if len(undeclared) == 1 else 'ies'} "
            f"{', '.join(undeclared)}; a pin can only hold back a manifest dependency"
        )
    merged: list[HostDependency] = []
    for dep in deps:
        pin = pins.get(dep.name)
        if pin is None:
            merged.append(dep)
            continue
        if dep.pin is not None:
            raise PinsError(
                f"{dep.name!r} is pinned both in the packaged manifest and in the pins file"
            )
        merged.append(dataclasses.replace(dep, pin=pin))
    return tuple(merged)
