"""The minimum gh version vergil-tooling needs, and the check that enforces it.

vrg-gh relies on gh features that an old install lacks. The newest of them
sets the minimum: ``run job-log`` (issue #4597) calls ``gh api
--allow-escape-sequences``, a flag gh added in v2.97.0, the release that
made ``gh api`` neutralize terminal escape sequences in non-JSON output
(https://github.com/cli/cli/releases/tag/v2.97.0, advisory
https://github.com/cli/cli/security/advisories/GHSA-3m3g-3wcr-px46). Every
other gh feature vrg-gh forwards to is older. A gh older than the minimum
would fail with gh's own unknown-flag error, so :func:`require_minimum`
refuses first, naming the version required (issue #4613). vrg-gh calls it
itself; every other direct gh call in vergil-tooling reaches it through
:func:`vergil_tooling.lib.github.require_gh` (issue #5046).

The check never skips. It reads gh's version with ``gh --version`` and
caches the answer per gh binary, so it adds no gh call to every checked
gh call: the cache entry is keyed on the resolved binary's path, size
and modification time, so an upgrade or reinstall of gh (a new binary, or
the same path rewritten) is re-probed. The cache holds the version, never a
verdict, so raising :data:`MIN_GH_VERSION` takes effect at once. An
unreadable or malformed cache file is a cache miss: the version is probed
again and the file rewritten, so the check itself still runs. A cache file
that cannot be written is reported on stderr, and the check still decides.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Final

from vergil_tooling.lib import host_versions

if TYPE_CHECKING:
    from collections.abc import Mapping

MIN_GH_VERSION: Final = "2.97.0"
"""The oldest gh vrg-gh supports (``gh api --allow-escape-sequences``, gh v2.97.0)."""

_PROBE_TIMEOUT_SECONDS: Final = 30


class GhVersionError(Exception):
    """gh is missing, its version cannot be read, or it is older than the minimum."""


def cache_path(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """The cache file: ``vergil/gh-version.json`` under the XDG cache home.

    The XDG cache home is ``$XDG_CACHE_HOME`` when it is an absolute path,
    else ``~/.cache``.
    """
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else (home or Path.home()) / ".cache"
    return base / "vergil" / "gh-version.json"


def _binary_key(gh: str) -> dict[str, object]:
    """What identifies the gh binary on disk: its resolved path, size and mtime."""
    real = Path(gh).resolve()
    try:
        st = real.stat()
    except OSError as exc:
        msg = f"cannot stat the gh binary {real}: {exc}"
        raise GhVersionError(msg) from exc
    return {"path": str(real), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _cached_version(path: Path, key: dict[str, object]) -> str | None:
    """The version cached for the binary *key* names, or ``None`` on a miss."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(data, dict) or data.get("binary") != key:
        return None
    version = data.get("version")
    return version if isinstance(version, str) and version else None


def _write_cache(path: Path, key: dict[str, object], version: str) -> None:
    """Record *version* for the binary *key* names; report a failure on stderr."""
    payload = json.dumps({"binary": key, "version": version}, sort_keys=True)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gh-version-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        print(
            f"warning: cannot cache the gh version in {path}: {exc}; "
            "gh will be probed again on the next call",
            file=sys.stderr,
        )


def _probe_version(gh: str) -> str:
    """Read the version ``gh --version`` reports, from its first line."""
    try:
        result = subprocess.run(  # noqa: S603 - gh resolved from PATH, fixed arguments
            [gh, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"cannot run '{gh} --version' to check its version: {exc}"
        raise GhVersionError(msg) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no output"
        msg = f"'{gh} --version' exited {result.returncode}: {detail}"
        raise GhVersionError(msg)
    first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    try:
        return host_versions.normalize(first)
    except host_versions.UnrecognizedVersionError as exc:
        msg = f"cannot read gh's version from '{gh} --version': {exc}"
        raise GhVersionError(msg) from exc


def installed_version(*, cache: Path | None = None) -> tuple[str, str]:
    """Return ``(gh_path, version)`` for the gh on PATH, from the cache when it is fresh.

    Raises:
        GhVersionError: gh is not on PATH, or its version cannot be read.
    """
    gh = shutil.which("gh")
    if gh is None:
        msg = f"gh is not on PATH; vergil-tooling needs gh {MIN_GH_VERSION} or newer"
        raise GhVersionError(msg)
    path = cache if cache is not None else cache_path()
    key = _binary_key(gh)
    version = _cached_version(path, key)
    if version is None:
        version = _probe_version(gh)
        _write_cache(path, key, version)
    return gh, version


def require_minimum(*, cache: Path | None = None) -> None:
    """Refuse unless the gh on PATH is at least :data:`MIN_GH_VERSION`.

    Raises:
        GhVersionError: gh is missing, its version cannot be read or ordered,
            or it is older than the minimum. The message names the version
            required and the one found.
    """
    gh, version = installed_version(cache=cache)
    try:
        too_old = host_versions.compare(version, MIN_GH_VERSION) < 0
    except host_versions.IncomparableVersionError as exc:
        msg = f"cannot tell whether gh {version} ({gh}) is at least {MIN_GH_VERSION}: {exc}"
        raise GhVersionError(msg) from exc
    if too_old:
        msg = (
            f"gh {version} ({gh}) is too old; vergil-tooling needs gh {MIN_GH_VERSION} or newer "
            "(for 'gh api --allow-escape-sequences'). Upgrade gh, for example with "
            "'vrg-host upgrade gh' on the macOS host."
        )
        raise GhVersionError(msg)
