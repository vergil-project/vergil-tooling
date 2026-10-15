"""Install-method handlers: installed / latest / upgrade per host dependency.

Each :class:`~vergil_tooling.lib.host_deps.HostDependency` names a ``method``
(``brew-formula``, ``brew-cask``, ``uv-tool`` or ``custom``); this module maps
that method to the commands that read the installed version, discover the
latest version, and upgrade the tool (epic vergil-project/.github#167, spec
§7.3).

Every shell-out goes through the single :func:`_run` seam so tests stub one
point and never touch the live host.

No-silent-failure contract:

- :func:`installed_version` returns ``None`` only when the dependency is
  missing (its probe failed). Output from a *successful* probe that cannot be
  parsed raises :class:`ProbeError` — a misread is never reported as missing
  or as current.
- :func:`latest_version` returns :data:`UNMANAGED` when the manifest declares
  the tool has no latest feed, and ``None`` when a probe that should work did
  not answer (the ``latest-unknown`` WARN). The two are never conflated.
- :func:`upgrade` raises :class:`UpgradeUnsupportedError` (alias
  ``UpgradeUnsupported``) for a ``manual`` dependency and
  :class:`UpgradeFailedError` when the upgrade command fails.
- An unknown method or a ``custom`` entry missing a required probe raises
  :class:`~vergil_tooling.lib.host_deps.ManifestError`.

Privilege invariant (spec §10): ``vrg-host`` runs unprivileged. :func:`_run`
refuses privilege-escalation commands and gives every child a closed stdin, so
a command that unexpectedly wants a password fails instead of prompting.

Parsing here covers the documented output shapes only; hardening against
recorded real-host fixtures is plan Task 7.
"""

from __future__ import annotations

import json
import subprocess
from typing import Final

from vergil_tooling.lib.host_deps import HostDependency, ManifestError

_BREW_METHODS: Final = frozenset({"brew-formula", "brew-cask"})
_ESCALATORS: Final = frozenset({"sudo", "doas", "su", "pkexec"})


class _Unmanaged:
    """Type of the :data:`UNMANAGED` sentinel."""

    def __repr__(self) -> str:
        return "UNMANAGED"


UNMANAGED: Final = _Unmanaged()
"""Returned by :func:`latest_version` for a dependency declared ``latest = "unmanaged"``."""


class UpgradeUnsupportedError(Exception):
    """``upgrade()`` was called on a ``manual`` (observe-only) dependency."""


UpgradeUnsupported = UpgradeUnsupportedError
"""Plan-interface name for :class:`UpgradeUnsupportedError` (same class)."""


class UpgradeFailedError(Exception):
    """An ``auto`` upgrade command did not succeed."""


class ProbeError(Exception):
    """A probe succeeded but its output could not be parsed."""


def _run(cmd: list[str]) -> str | None:
    """Run *cmd* unprivileged; return stripped stdout, or ``None`` on failure.

    Raises:
        ValueError: *cmd* is empty or would escalate privilege.
    """
    if not cmd:
        raise ValueError("refusing to run an empty command")
    if cmd[0] in _ESCALATORS:
        raise ValueError(f"refusing to escalate privilege: {cmd[0]!r} (vrg-host runs unprivileged)")
    try:
        out = subprocess.run(  # noqa: S603 - argv from the trusted, tooling-owned manifest
            cmd,
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, OSError):
        return None
    return out.stdout.strip()


# -- pure parsers ---------------------------------------------------------------


def _parse_brew_list_versions(name: str, output: str) -> str:
    """Parse ``brew list --versions <name>`` (``"<name> <ver> [<ver>…]"``) → newest listed."""
    parts = output.split()
    if len(parts) < 2 or parts[0] != name:
        raise ProbeError(f"{name}: unexpected 'brew list --versions' output {output!r}")
    return parts[-1]


def _parse_brew_info_latest(method: str, output: str) -> str | None:
    """Parse ``brew info --json=v2`` → the current stable (formula) or cask version."""
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    key = "casks" if method == "brew-cask" else "formulae"
    entries = data.get(key)
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        return None
    entry = entries[0]
    if method == "brew-cask":
        version = entry.get("version")
    else:
        versions = entry.get("versions")
        version = versions.get("stable") if isinstance(versions, dict) else None
    return version if isinstance(version, str) and version else None


def _uv_tool_line(name: str, output: str) -> list[str] | None:
    """Return the tokens of *name*'s header line in ``uv tool list`` output, if any."""
    for line in output.splitlines():
        parts = line.split()
        if parts and parts[0] == name:
            return parts
    return None


def _parse_uv_tool_installed(name: str, output: str) -> str | None:
    """Parse ``uv tool list`` (``"<name> v<ver>"`` header lines) → version, or None if absent."""
    parts = _uv_tool_line(name, output)
    if parts is None:
        return None
    if len(parts) < 2 or not parts[1].startswith("v") or len(parts[1]) < 2:
        raise ProbeError(f"{name}: unexpected 'uv tool list' line {' '.join(parts)!r}")
    return parts[1][1:]


def _parse_uv_tool_outdated(name: str, output: str) -> str | None:
    """Parse ``uv tool list --outdated`` (``"<name> v<ver> [latest: <ver>]"``).

    Returns the latest version when *name* is listed as outdated, else ``None``.
    """
    parts = _uv_tool_line(name, output)
    if parts is None:
        return None
    line = " ".join(parts)
    marker = "[latest: "
    start = line.find(marker)
    end = line.find("]", start)
    if start < 0 or end < 0:
        return None
    version = line[start + len(marker) : end].strip().removeprefix("v")
    return version or None


# -- dispatch -------------------------------------------------------------------


def _unknown_method(dep: HostDependency) -> ManifestError:
    return ManifestError(f"dependency {dep.name!r}: unknown method {dep.method!r}")


def _custom(dep: HostDependency, key: str) -> list[str]:
    """Return the ``custom`` probe *key* for *dep*, failing loudly when it is absent."""
    cmd = dep.probes.get(key)
    if not cmd:
        raise ManifestError(f"dependency {dep.name!r}: custom method requires probe {key!r}")
    return cmd


def _brew_cask_flag(dep: HostDependency) -> list[str]:
    return ["--cask"] if dep.method == "brew-cask" else []


def installed_version(dep: HostDependency) -> str | None:
    """Return *dep*'s installed version, or ``None`` when it is not installed.

    Raises:
        ProbeError: the probe succeeded but its output is unparseable.
        ManifestError: unknown method, or a ``custom`` entry lacks the probe.
    """
    if dep.method in _BREW_METHODS:
        out = _run(["brew", "list", *_brew_cask_flag(dep), "--versions", dep.name])
        return _parse_brew_list_versions(dep.name, out) if out else None
    if dep.method == "uv-tool":
        out = _run(["uv", "tool", "list"])
        return _parse_uv_tool_installed(dep.name, out) if out is not None else None
    if dep.method == "custom":
        out = _run(_custom(dep, "installed_version"))
        if out is None:
            return None
        if not out:
            raise ProbeError(f"{dep.name}: installed_version probe printed nothing")
        return out
    raise _unknown_method(dep)


def latest_version(dep: HostDependency) -> str | _Unmanaged | None:
    """Return *dep*'s latest available version.

    Returns :data:`UNMANAGED` when the manifest declares no latest feed, and
    ``None`` when a probe that should work could not answer.

    Raises:
        ManifestError: unknown method, or a ``custom`` entry lacks the probe.
    """
    if dep.latest == "unmanaged":
        return UNMANAGED
    if dep.method in _BREW_METHODS:
        out = _run(["brew", "info", "--json=v2", *_brew_cask_flag(dep), dep.name])
        return _parse_brew_info_latest(dep.method, out) if out else None
    if dep.method == "uv-tool":
        out = _run(["uv", "tool", "list", "--outdated"])
        if out is None:
            return None
        newer = _parse_uv_tool_outdated(dep.name, out)
        if newer is not None:
            return newer
        # Not listed as outdated: the installed version is the latest — but only
        # if it is installed at all; otherwise the latest is genuinely unknown.
        return installed_version(dep)
    if dep.method == "custom":
        return _run(_custom(dep, "latest_version")) or None
    raise _unknown_method(dep)


def upgrade(dep: HostDependency) -> None:
    """Upgrade *dep* via its method handler.

    Raises:
        UpgradeUnsupportedError: *dep* is ``upgrade = "manual"``.
        UpgradeFailedError: the upgrade command did not succeed.
        ManifestError: unknown method, or a ``custom`` entry lacks the probe.
    """
    if dep.upgrade == "manual":
        raise UpgradeUnsupportedError(dep.name)
    if dep.method in _BREW_METHODS:
        cmd = ["brew", "upgrade", *_brew_cask_flag(dep), dep.name]
    elif dep.method == "uv-tool":
        cmd = ["uv", "tool", "upgrade", dep.name]
    elif dep.method == "custom":
        cmd = _custom(dep, "upgrade")
    else:
        raise _unknown_method(dep)
    if _run(cmd) is None:
        raise UpgradeFailedError(f"{dep.name}: upgrade command failed: {' '.join(cmd)}")
