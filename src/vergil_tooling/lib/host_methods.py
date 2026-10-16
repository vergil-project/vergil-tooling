"""Install-method handlers: installed / latest / upgrade per host dependency.

Each :class:`~vergil_tooling.lib.host_deps.HostDependency` names a ``method``
(``brew-formula``, ``brew-cask``, ``uv-tool`` or ``custom``); this module maps
that method to the commands that read the installed version, discover the
latest version, and upgrade the tool (epic vergil-project/.github#167, spec
§7.3).

Every shell-out goes through the single :func:`_run` seam so tests stub one
point and never touch the live host.

No-silent-failure contract:

- Both version probes return a **normalized**, bare version
  (:func:`vergil_tooling.lib.host_versions.normalize`): ``"gh version 2.40.0"``
  and ``"v2.40.0"`` both come back as ``"2.40.0"``, so status compares like
  with like (spec §8).
- :func:`installed_version` returns ``None`` only when the dependency is
  missing (its probe failed). Output from a *successful* probe that cannot be
  parsed raises :class:`ProbeError` — a misread is never reported as missing
  or as current.
- :func:`latest_version` returns :data:`UNMANAGED` when the manifest declares
  the tool has no latest feed, and ``None`` when a probe that should work did
  not answer (the ``latest-unknown`` WARN). The two are never conflated. A
  latest probe that *did* answer but unreadably raises :class:`ProbeError`,
  exactly like the installed probe.
- :func:`upgrade` raises :class:`UpgradeUnsupportedError` (alias
  ``UpgradeUnsupported``) for a ``manual`` dependency and
  :class:`UpgradeFailedError` when the upgrade command fails.
- An unknown method or a ``custom`` entry missing a required probe raises
  :class:`~vergil_tooling.lib.host_deps.ManifestError`.

Privilege invariant (spec §10): ``vrg-host`` runs unprivileged. :func:`_run`
refuses privilege-escalation commands and gives every child a closed stdin, so
a command that unexpectedly wants a password fails instead of prompting.

The parsers are pure functions tested against output fixtures under
``tests/vergil_tooling/fixtures/host/`` (see the README there for each
fixture's provenance).
"""

from __future__ import annotations

import functools
import json
import subprocess
from typing import Final

from vergil_tooling.lib import host_versions
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


def _normalized(name: str, raw: str, what: str) -> str:
    """Normalize *raw* probe output to a bare version, or raise :class:`ProbeError`."""
    try:
        return host_versions.normalize(raw)
    except host_versions.UnrecognizedVersionError as exc:
        raise ProbeError(f"{name}: {what}: {exc}") from exc


def _parse_brew_list_versions(name: str, output: str) -> str:
    """Parse ``brew list --versions <name>`` (``"<name> <ver> [<ver>…]"``) → newest listed.

    Homebrew lists every installed keg (old kegs linger until ``brew cleanup``)
    in no guaranteed order, so the newest is chosen by the host comparator. A
    keg name that is not a version raises rather than guessing which is live
    (every version :func:`host_versions.normalize` returns is comparable).
    """
    parts = output.split()
    if len(parts) < 2 or parts[0] != name:
        raise ProbeError(f"{name}: unexpected 'brew list --versions' output {output!r}")
    versions = [_normalized(name, v, "brew list --versions") for v in parts[1:]]
    return max(versions, key=functools.cmp_to_key(host_versions.compare))


def _parse_brew_info_latest(name: str, method: str, output: str) -> str:
    """Parse ``brew info --json=v2`` → the current stable (formula) or cask version.

    A formula's latest carries its Homebrew ``revision`` as ``_<n>`` (``3.12.7``
    with revision 1 is ``3.12.7_1``), matching how ``brew list --versions``
    names the installed keg, so a revision-only rebuild still reads as drift.

    Raises:
        ProbeError: the output is not the expected JSON shape, or its version
            is unreadable. ``brew info`` answered, so this is never "unknown".
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{name}: 'brew info --json=v2' output is not JSON: {exc}") from exc
    key = "casks" if method == "brew-cask" else "formulae"
    entries = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise ProbeError(f"{name}: 'brew info --json=v2' has no {key} entry")
    entry = entries[0]
    if method == "brew-cask":
        version = entry.get("version")
        revision = 0
    else:
        versions = entry.get("versions")
        version = versions.get("stable") if isinstance(versions, dict) else None
        revision = entry.get("revision", 0)
    if not isinstance(version, str) or not version:
        raise ProbeError(f"{name}: 'brew info --json=v2' {key} entry has no version")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ProbeError(f"{name}: 'brew info --json=v2' revision {revision!r} is not a count")
    bare = _normalized(name, version, "brew info --json=v2")
    return f"{bare}_{revision}" if revision else bare


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
    return _normalized(name, parts[1], "uv tool list")


def _parse_uv_tool_outdated(name: str, output: str) -> str | None:
    """Parse ``uv tool list --outdated`` (``"<name> v<ver> [latest: <ver>]"``).

    Returns the latest version when *name* is listed as outdated, and ``None``
    when it is not listed (it is current).

    Raises:
        ProbeError: *name* is listed but its ``[latest: …]`` marker is missing
            or unreadable — a listed tool is never silently read as current.
    """
    parts = _uv_tool_line(name, output)
    if parts is None:
        return None
    line = " ".join(parts)
    marker = "[latest: "
    start = line.find(marker)
    end = line.find("]", start)
    if start < 0 or end < 0:
        raise ProbeError(f"{name}: 'uv tool list --outdated' line has no latest: {line!r}")
    return _normalized(name, line[start + len(marker) : end], "uv tool list --outdated")


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


def _brew_kind_flag(dep: HostDependency) -> list[str]:
    """Disambiguate a name that is both a formula and a cask (e.g. ``docker``)."""
    return ["--cask"] if dep.method == "brew-cask" else ["--formula"]


def installed_version(dep: HostDependency) -> str | None:
    """Return *dep*'s normalized installed version, or ``None`` when not installed.

    Raises:
        ProbeError: the probe succeeded but its output is unparseable.
        ManifestError: unknown method, or a ``custom`` entry lacks the probe.
    """
    if dep.method in _BREW_METHODS:
        out = _run(["brew", "list", *_brew_kind_flag(dep), "--versions", dep.name])
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
        return _normalized(dep.name, out, "installed_version probe")
    raise _unknown_method(dep)


def latest_version(dep: HostDependency) -> str | _Unmanaged | None:
    """Return *dep*'s normalized latest available version.

    Returns :data:`UNMANAGED` when the manifest declares no latest feed, and
    ``None`` when a probe that should work could not answer.

    Raises:
        ProbeError: the probe answered but its output is unparseable.
        ManifestError: unknown method, or a ``custom`` entry lacks the probe.
    """
    if dep.latest == "unmanaged":
        return UNMANAGED
    if dep.method in _BREW_METHODS:
        out = _run(["brew", "info", "--json=v2", *_brew_kind_flag(dep), dep.name])
        return _parse_brew_info_latest(dep.name, dep.method, out) if out is not None else None
    if dep.method == "uv-tool":
        out = _run(["uv", "tool", "list", "--outdated"])
        if out is None:
            return None
        newer = _parse_uv_tool_outdated(dep.name, out)
        if newer is not None:
            return newer
        # Not listed as outdated: the installed version is the latest — but only
        # if it is installed at all; otherwise the latest is genuinely unknown.
        # (Only sound for a tool sourced from a package index; a git-sourced
        # tool has no index feed and must be declared `latest = "unmanaged"`.)
        return installed_version(dep)
    if dep.method == "custom":
        out = _run(_custom(dep, "latest_version"))
        if out is None:
            return None
        if not out:
            raise ProbeError(f"{dep.name}: latest_version probe printed nothing")
        return _normalized(dep.name, out, "latest_version probe")
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
