"""Install vergil-tooling on a VM from the org package repository (spec §9).

The tooling ref decides the install mode, never a failure:

- a **line** (``vX.Y``) or an **exact** release (``vX.Y.Z``) is a *packaged*
  install from the vergil apt repository, pinned to that line or version;
- anything else (``develop``, a feature branch) is an explicit *dev* install,
  ``uv tool install`` from git into ``~/.local/bin``, which shadows the packaged
  ``/usr/bin`` copy. A dev install records its ref so every ``vrg-vm`` command
  that touches the VM can say so (:data:`DEV_BANNER`), and the next packaged
  install removes it again.
"""

from __future__ import annotations

import re
import subprocess
import sys
from typing import TYPE_CHECKING, Literal, NoReturn

from vergil_tooling.lib.package import PackageError, orgs, repo_setup
from vergil_tooling.lib.package import targets as tg

if TYPE_CHECKING:
    from vergil_tooling.lib.vm_transport import Transport

_LINE = re.compile(r"v(\d+)\.(\d+)")
_EXACT = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
_PACKAGE = "vergil-tooling"
_VENDOR = "vergil"
_PIN_FILE = "/etc/apt/preferences.d/vergil-tooling"
_DEV_REF_FILE = "~/.config/vergil/tooling-dev-ref"
_DEV_INSTALL = "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@{ref}"
_UV_PATH = 'export PATH="$HOME/.local/bin:$PATH"'

DEV_BANNER = "DEV tooling (ref {ref}) — not the packaged install"


def classify_ref(ref: str) -> Literal["line", "exact", "dev"]:
    """Classify a tooling ref: a release line, an exact release, or a dev ref."""
    if _EXACT.fullmatch(ref):
        return "exact"
    if _LINE.fullmatch(ref):
        return "line"
    return "dev"


def _deb_version(ref: str) -> str:
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
    version = _deb_version(ref) if _require_release(ref) == "exact" else f"{ref[1:]}.*"
    return f"Package: {_PACKAGE}\nPin: version {version}\nPin-Priority: 1001\n"


def _candidate_matches(ref: str, candidate: str) -> bool:
    if classify_ref(ref) == "exact":
        return candidate == _deb_version(ref)
    return candidate.startswith(f"{ref[1:]}.")


def _die(msg: str) -> NoReturn:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _codename(transport: Transport) -> str:
    """The VM's Ubuntu codename, which must be a registered deb suite."""
    os_release = transport.run("cat", "/etc/os-release").stdout
    m = re.search(r"^VERSION_CODENAME=\"?([^\"\s]+)\"?", os_release, re.MULTILINE)
    suites = sorted({t.suite for t in tg.all_targets() if t.fmt == "deb"})
    codename = m[1] if m else "(unknown)"
    if codename not in suites:
        _die(
            f"VM OS codename {codename} is not a supported package target "
            f"(supported: {', '.join(suites)})"
        )
    return codename


def _remove_uv_copy(transport: Transport) -> None:
    """Remove a uv-installed vergil-tooling (legacy or dev) and the dev-ref marker.

    A uv copy in ``~/.local/bin`` would shadow the packaged ``/usr/bin`` one.
    """
    listed = transport.run("bash", "-c", f"{_UV_PATH}; uv tool list 2>/dev/null || true").stdout
    if re.search(rf"^{re.escape(_PACKAGE)} ", listed, re.MULTILINE):
        print("  Removing the uv-installed vergil-tooling (shadows the packaged install)...")
        transport.run("bash", "-c", f"{_UV_PATH}; uv tool uninstall {_PACKAGE}")
    transport.run("bash", "-c", f"rm -f {_DEV_REF_FILE}")


def packaged_install(transport: Transport, ref: str) -> None:
    """Install (or upgrade within the pin) vergil-tooling from the org apt repository.

    Exits with an actionable error, installing nothing, when the repository has
    no package matching ``ref``. Any uv-installed copy is removed only after the
    apt install succeeds, so a failed install never leaves the VM without tooling.
    """
    kind = _require_release(ref)
    codename = _codename(transport)
    org = orgs.for_vendor(_VENDOR)
    try:
        repo_setup.bootstrap(transport.run, org, "deb", codename, sudo=True)
    except PackageError as exc:
        _die(f"could not set up the {_VENDOR} package repository ({org.base_url}): {exc}")
    transport.pipe(f"sudo tee {_PIN_FILE} >/dev/null", apt_pin(ref))
    transport.run("sudo", *repo_setup.apt_get("update"))
    policy = transport.run("apt-cache", "policy", _PACKAGE).stdout
    m = re.search(r"Candidate:\s*(\S+)", policy)
    if m is None or not _candidate_matches(ref, m[1]):
        _die(
            f"no packaged vergil-tooling matching {ref} in {org.base_url} "
            f"(apt candidate: {m[1] if m else '(none)'}). Release {ref} first, or "
            f"install a git ref explicitly with 'vrg-vm update --tag <git-ref>' (a dev install)."
        )
    spec = f"{_PACKAGE}={_deb_version(ref)}" if kind == "exact" else _PACKAGE
    # An explicit pin may move the VM to an older release (identity pinned back).
    transport.run("sudo", *repo_setup.apt_get("install", "-y", "--allow-downgrades", spec))
    _remove_uv_copy(transport)


def _uv_tool_install(transport: Transport, install_spec: str, *, reinstall: bool) -> None:
    """Run ``uv tool install`` inside the VM, self-healing a poisoned uv cache.

    A corrupt cache entry — e.g. a zero-byte wheel ``METADATA`` left behind by
    an unclean VM stop — makes ``uv tool install`` fail with "wheel is invalid".
    On the reinstall path uv removes the existing tool *before* it fails, so a
    poisoned cache leaves the VM with no tooling at all and bricks every future
    ``vrg-vm session`` until the cache is cleared by hand. So on failure, clear
    the VM's uv cache and retry the install once.

    The retry escalates to ``--force``. The same unclean stop also corrupts the
    tool *receipt*: uv removes the entry but, unable to read the receipt, cannot
    enumerate the tool's entry points, so the ``vrg-*`` executables orphan in
    ``~/.local/bin``. Clearing the cache fixes the wheel, but the retry would
    then die with "Executable already exists" — only ``--force`` replaces
    existing entry points (``--reinstall`` alone does not), so the retry must
    force to fully recover.
    """
    flag = "--reinstall " if reinstall else ""
    install_cmd = f'{_UV_PATH} && uv tool install {flag}"{install_spec}"'
    retry_cmd = f'{_UV_PATH} && uv tool install --force --reinstall "{install_spec}"'
    try:
        transport.run("bash", "-c", install_cmd)
        return
    except subprocess.CalledProcessError:
        print(
            "  uv tool install failed — clearing the VM uv cache and retrying once...",
            file=sys.stderr,
        )
        transport.run("bash", "-c", f"{_UV_PATH} && uv cache clean")
        transport.run("bash", "-c", retry_cmd)


def dev_install(transport: Transport, ref: str) -> None:
    """Explicitly install vergil-tooling from git ``ref`` with uv, and record the ref."""
    if classify_ref(ref) != "dev":
        msg = f"{ref!r} is a release version; it installs from the package repository"
        raise ValueError(msg)
    _uv_tool_install(transport, _DEV_INSTALL.format(ref=ref), reinstall=True)
    transport.run("bash", "-c", f"mkdir -p $(dirname {_DEV_REF_FILE})")
    transport.pipe(f"cat > {_DEV_REF_FILE}", f"{ref}\n")


def dev_ref(transport: Transport) -> str | None:
    """The ref of the VM's dev install, or ``None`` when it runs the packaged install."""
    out = transport.run("bash", "-c", f"cat {_DEV_REF_FILE} 2>/dev/null || true").stdout.strip()
    return out or None
