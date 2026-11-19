"""Install vergil-tooling on a VM with uv, and remove the vergil packages.

Every VM install is ``uv tool install`` of one commit, rendered by
:mod:`vergil_tooling.lib.tooling_install` (epic vergil-project/.github#1350,
spec §4.5):

- a **line** (``vX.Y``) or an **exact** release (``vX.Y.Z``) is resolved on the
  host, once, to its tag and commit SHA, and installs that commit constrained
  by its own ``uv.lock``. It clears the dev-ref marker;
- anything else (``develop``, a feature branch) is an explicit **dev** install
  of that git ref, unconstrained. It records its ref so every ``vrg-vm``
  command that touches the VM can say so (:data:`DEV_BANNER`).

Both land in uv's default ``~/.local/bin`` and are checked there
(:func:`tooling_install.check_lines`). Once the check passes, the caller runs
:func:`cleanup_packages`, which removes what the old packaged install left: the
vergil-tooling, ``vergil-python*`` and keyring packages, the vergil apt source,
key and pin.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from typing import TYPE_CHECKING, Literal, NoReturn

from vergil_tooling.lib import tooling_install
from vergil_tooling.lib.atomic_write import atomic_write_cmd
from vergil_tooling.lib.package import orgs, repo_setup
from vergil_tooling.lib.tooling_install import classify_ref as classify_ref

if TYPE_CHECKING:
    from vergil_tooling.lib.vm_transport import Transport

_PACKAGE = "vergil-tooling"
_RUNTIME_PREFIX = "vergil-python"
_ORG = orgs.for_vendor("vergil")
_PIN_FILE = "/etc/apt/preferences.d/vergil-tooling"
# What the old packaged install left besides the packages: the apt source the
# keyring package wrote, the key that source trusts, and the version pin.
_APT_FILES = (
    repo_setup.deb_source_path(_ORG),
    repo_setup.installed_key_path(_ORG, "deb"),
    _PIN_FILE,
)
_DEV_REF_FILE = "~/.config/vergil/tooling-dev-ref"
_UV_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
_BIN_DIR = "$HOME/.local/bin"
_LOG_PREFIX = "vergil-tooling: "

DEV_BANNER = "DEV tooling (ref {ref}) — dev ref, not a release"


# deb_version and apt_pin serve only the container cache build's packaged
# branch, which vergil-project/vergil-tooling#4740 removes.


def deb_version(ref: str) -> str:
    """The deb version an exact ``vX.Y.Z`` ref names (shared builds are revision 1)."""
    return f"{ref[1:]}-1"


def _require_release(ref: str) -> Literal["line", "exact"]:
    kind = classify_ref(ref)
    if kind == "dev":
        msg = f"{ref!r} is not a release version (vX.Y or vX.Y.Z)"
        raise ValueError(msg)
    return kind


def apt_pin(ref: str) -> str:
    """The ``/etc/apt/preferences.d`` text pinning vergil-tooling to ``ref``."""
    version = deb_version(ref) if _require_release(ref) == "exact" else f"{ref[1:]}.*"
    return f"Package: {_PACKAGE}\nPin: version {version}\nPin-Priority: 1001\n"


def _die(msg: str) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _resolve(ref: str) -> tooling_install.ResolvedRef:
    """Resolve *ref* on the host, once; a failure exits naming the URL and the ref."""
    try:
        return tooling_install.resolve(ref, ls_remote=tooling_install.git_ls_remote)
    except tooling_install.ToolingInstallError as exc:
        _die(str(exc))


def _script(lines: list[str]) -> str:
    """*lines* as one ``bash -c`` script, with uv's ``~/.local/bin`` first on ``PATH``."""
    return "\n".join([_UV_PATH, *lines])


def _print_log(stderr: str) -> None:
    """Print the install's own ``vergil-tooling:`` log lines (tag and SHA)."""
    for line in stderr.splitlines():
        if line.startswith(_LOG_PREFIX):
            print(f"  {line}")


def _uv_tool_install(transport: Transport, resolved: tooling_install.ResolvedRef) -> None:
    """Run *resolved*'s install lines inside the VM, self-healing a poisoned uv cache.

    A corrupt cache entry — e.g. a zero-byte wheel ``METADATA`` left behind by
    an unclean VM stop — makes ``uv tool install`` fail with "wheel is invalid".
    On the reinstall path uv removes the existing tool *before* it fails, so a
    poisoned cache leaves the VM with no tooling at all and bricks every future
    ``vrg-vm session`` until the cache is cleared by hand. So on failure, clear
    the VM's uv cache and retry the install once.

    The install lines always pass ``--force``: the same unclean stop also
    corrupts the tool *receipt*, so uv cannot enumerate the tool's entry points
    and the ``vrg-*`` executables orphan in ``~/.local/bin``. Only ``--force``
    replaces existing entry points, so the retry recovers fully.
    """
    script = _script(
        tooling_install.install_lines(resolved, python_dir_env=None, tool_dir=None, bin_dir=None)
    )
    try:
        result = transport.run("bash", "-c", script)
    except subprocess.CalledProcessError:
        print(
            "  uv tool install failed — clearing the VM uv cache and retrying once...",
            file=sys.stderr,
        )
        transport.run("bash", "-c", f"{_UV_PATH} && uv cache clean")
        result = transport.run("bash", "-c", script)
    _print_log(result.stderr or "")


def _check(transport: Transport) -> None:
    """Check ``vrg-whoami`` resolves to ``~/.local/bin`` and runs; raise if not.

    A ``/usr/bin/vrg-whoami`` that shadows the uv install fails it.
    """
    transport.run("bash", "-c", _script(tooling_install.check_lines(_BIN_DIR)))


def released_install(transport: Transport, ref: str) -> None:
    """Install the release ``ref`` names with uv, check it, and clear the dev-ref marker.

    A line resolves to its newest ``vX.Y.Z``; the install is that one commit,
    constrained by its ``uv.lock``. The marker is cleared only once the check
    passes, so a failed install still reports the dev install it left.
    """
    _require_release(ref)
    _uv_tool_install(transport, _resolve(ref))
    _check(transport)
    transport.run("bash", "-c", f"rm -f {_DEV_REF_FILE}")


def dev_install(transport: Transport, ref: str) -> None:
    """Explicitly install vergil-tooling from git ``ref`` with uv, check it, record the ref."""
    if classify_ref(ref) != "dev":
        msg = f"{ref!r} is a release version; it installs as a release"
        raise ValueError(msg)
    _uv_tool_install(transport, _resolve(ref))
    _check(transport)
    transport.run("bash", "-c", f"mkdir -p $(dirname {_DEV_REF_FILE})")
    transport.pipe(atomic_write_cmd(_DEV_REF_FILE, 0o644), f"{ref}\n")


def dev_ref(transport: Transport) -> str | None:
    """The ref of the VM's dev install, or ``None`` when it runs a release."""
    out = transport.run("bash", "-c", f"cat {_DEV_REF_FILE} 2>/dev/null || true").stdout.strip()
    return out or None


def _is_vergil_package(name: str) -> bool:
    return name in (_PACKAGE, _ORG.keyring_package) or name.startswith(_RUNTIME_PREFIX)


def _vergil_packages(transport: Transport) -> list[str]:
    """The vergil packages dpkg still knows, installed or with config files left.

    A package counts unless dpkg records it as not installed (status ``?n``).
    """
    listing = transport.run("dpkg-query", "-W", "-f=${db:Status-Abbrev} ${Package}\n").stdout
    names: set[str] = set()
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) != 2:  # noqa: PLR2004
            continue
        status, name = fields
        if _is_vergil_package(name) and status[1:2] != "n":
            names.add(name)
    return sorted(names)


def _vergil_apt_files(transport: Transport) -> list[str]:
    """Which of the vergil apt source, key and pin files exist."""
    probe = "; ".join(f"test -e {shlex.quote(f)} && echo {shlex.quote(f)}" for f in _APT_FILES)
    out = transport.run("bash", "-c", f"{probe}; true").stdout
    return [f for f in out.splitlines() if f in _APT_FILES]


def cleanup_packages(transport: Transport) -> list[str]:
    """Remove the vergil packages and apt files the packaged install left; name them.

    It purges ``vergil-tooling``, every ``vergil-python*`` and the keyring
    package, naming each (never a blanket ``apt-get autoremove``), then removes
    the vergil apt source, key and pin files, and confirms none is left. Run it
    only once the uv install has passed its check. When it removed a package,
    it checks the uv install again, so an install that still leaned on a
    removed package fails here, loudly. A VM with none of them (created after
    the move, or cleaned already) changes nothing.
    """
    removed: list[str] = []
    packages = _vergil_packages(transport)
    if packages:
        print(f"  Removing the vergil packages: {' '.join(packages)}")
        try:
            transport.run("sudo", *repo_setup.apt_get("purge", "-y", *packages))
        except subprocess.CalledProcessError as exc:
            _die(
                f"could not remove the vergil packages {' '.join(packages)} (exit {exc.returncode})"
            )
        removed += packages
    files = _vergil_apt_files(transport)
    if files:
        print(f"  Removing the vergil apt files: {' '.join(files)}")
        transport.run("sudo", "rm", "-f", "--", *files)
        removed += files
    left = _vergil_packages(transport) + _vergil_apt_files(transport)
    if left:
        _die(f"the vergil packages and apt files are not all removed; left: {' '.join(left)}")
    if packages:
        _check(transport)
    return removed
