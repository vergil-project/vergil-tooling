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
from typing import Any, Final

from vergil_tooling.lib import host_versions
from vergil_tooling.lib.host_deps import HostDependency, ManifestError

_BREW_METHODS: Final = frozenset({"brew-formula", "brew-cask"})
_BUILTIN_METHODS: Final = _BREW_METHODS | {"uv-tool"}
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

    Only stdout is returned. stderr is captured and dropped, so a notice a
    tool prints there (gcloud's SDK-version notice, for one) never reaches a
    parser.

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


def _brew_info_entry(name: str, method: str, output: str) -> dict[str, Any]:
    """Return the first formula (or cask) entry of ``brew info --json=v2`` output.

    Raises:
        ProbeError: the output is not JSON or has no such entry.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{name}: 'brew info --json=v2' output is not JSON: {exc}") from exc
    key = "casks" if method == "brew-cask" else "formulae"
    entries = data.get(key) if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries or not isinstance(entries[0], dict):
        raise ProbeError(f"{name}: 'brew info --json=v2' has no {key} entry")
    return entries[0]


def _parse_brew_info_latest(name: str, method: str, output: str) -> str:
    """Parse ``brew info --json=v2`` → the current stable (formula) or cask version.

    A formula's latest carries its Homebrew ``revision`` as ``_<n>`` (``3.14.8``
    with revision 2 is ``3.14.8_2``), matching how ``brew list --versions``
    names the installed keg, so a revision-only rebuild still reads as drift.
    A cask's ``<version>,<build>`` (``4.94.0,241994``) reads as its leading
    version (see :func:`host_versions.normalize`).

    Raises:
        ProbeError: the output is not the expected JSON shape, or its version
            is unreadable. ``brew info`` answered, so this is never "unknown".
    """
    entry = _brew_info_entry(name, method, output)
    key = "casks" if method == "brew-cask" else "formulae"
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


def _parse_brew_cask_auto_updates(name: str, output: str) -> bool:
    """Parse ``brew info --json=v2 --cask`` → whether the cask updates itself.

    Homebrew prints ``"auto_updates": true`` for a self-updating app and
    ``null`` (or ``false``) otherwise.

    Raises:
        ProbeError: the output is not the expected JSON shape, or
            ``auto_updates`` is neither a boolean nor null.
    """
    value = _brew_info_entry(name, "brew-cask", output).get("auto_updates")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise ProbeError(f"{name}: 'brew info --json=v2' auto_updates {value!r} is not a boolean")
    return value


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

    A non-``custom`` entry that declares an ``installed_version`` probe reads
    its installed version from that probe instead of the package manager's
    record — for a self-updating app (e.g. Docker Desktop) whose Homebrew
    record goes stale. Its latest and upgrade still come from the method.
    """
    overridden = dep.method in _BUILTIN_METHODS and "installed_version" in dep.probes
    if dep.method in _BREW_METHODS and not overridden:
        out = _run(["brew", "list", *_brew_kind_flag(dep), "--versions", dep.name])
        return _parse_brew_list_versions(dep.name, out) if out else None
    if dep.method == "uv-tool" and not overridden:
        out = _run(["uv", "tool", "list"])
        return _parse_uv_tool_installed(dep.name, out) if out is not None else None
    if dep.method == "custom" or overridden:
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


def _refuse_self_updating_cask(dep: HostDependency) -> None:
    """Refuse to ``brew upgrade`` a cask that Homebrew marks ``auto_updates``.

    Such an app updates itself; ``brew upgrade --cask`` silently skips it
    (unless ``--greedy``), and forcing it through brew fights the app's own
    updater. Either outcome would be a silent non-upgrade, so it fails loudly.

    Raises:
        UpgradeFailedError: the cask is self-updating, or its metadata could
            not be read to tell.
    """
    out = _run(["brew", "info", "--json=v2", "--cask", dep.name])
    if out is None:
        raise UpgradeFailedError(f"{dep.name}: cannot read cask metadata (brew info failed)")
    try:
        self_updating = _parse_brew_cask_auto_updates(dep.name, out)
    except ProbeError as exc:
        raise UpgradeFailedError(str(exc)) from exc
    if self_updating:
        raise UpgradeFailedError(
            f"{dep.name}: the cask is auto_updates (the app updates itself), so vrg-host "
            "never upgrades it through brew; use the app's own updater "
            '(the manifest entry should be upgrade = "manual")'
        )


def upgrade(dep: HostDependency) -> None:
    """Upgrade *dep* via its method handler.

    Raises:
        UpgradeUnsupportedError: *dep* is ``upgrade = "manual"``.
        UpgradeFailedError: the upgrade command did not succeed, or *dep* is
            a self-updating (``auto_updates``) cask that brew must not upgrade.
        ManifestError: unknown method, or a ``custom`` entry lacks the probe.
    """
    if dep.upgrade == "manual":
        raise UpgradeUnsupportedError(dep.name)
    if dep.method == "brew-cask":
        _refuse_self_updating_cask(dep)
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
