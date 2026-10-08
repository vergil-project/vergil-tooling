"""Bootstrap trust in an org package repository (spec §7.4, §9).

Reused by VM provisioning, install tests and builders. Every command goes
through a :data:`Run` callable, so the same sequence runs on this host
(:func:`local_run`) or inside a guest (``Transport.run``).

The sequence is: install any missing prerequisites, download
``keys/<vendor>.asc``, check that it holds exactly one primary key whose
fingerprint equals the one pinned in :mod:`~vergil_tooling.lib.package.orgs` —
**before** anything is written to ``sources.list.d`` / ``yum.repos.d`` — then
write a temporary ``-bootstrap`` source and key, install the org's keyring
package (which owns the permanent key and source from then on), and remove the
bootstrap files. The bootstrap files are removed even when the keyring install
fails; the failure still propagates.

Re-running is safe (#3138): stale bootstrap files are removed before any apt or
dnf call, and a target whose keyring package is already installed, with its
source and key in place, skips the bootstrap after checking the installed key
against the pinned fingerprint. A keyring installed without its source or key is
reinstalled through the bootstrap path.

apt never downloads the full Ubuntu archive indexes unless a prerequisite is
actually missing: the bootstrap source is refreshed on its own
(:func:`apt_scoped_update`), and every apt call carries :data:`APT_FAIL_FAST`.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from vergil_tooling.lib.package.orgs import OrgRepo

Run = Callable[..., subprocess.CompletedProcess[str]]
"""Called as ``run(*argv)``; raises ``CalledProcessError`` on a nonzero exit."""


APT_FAIL_FAST: tuple[str, ...] = (
    "-o",
    "Acquire::Retries=3",
    "-o",
    "Acquire::http::Timeout=20",
    "-o",
    "Acquire::https::Timeout=20",
)
"""Options on every apt invocation here: a dead mirror connection retries in seconds."""

DEB_PREREQ_PROBE = (
    "dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null"
    " | grep -qx 'install ok installed' || echo ca-certificates; "
    "command -v curl >/dev/null 2>&1 || echo curl; "
    "command -v gpg >/dev/null 2>&1 || echo gnupg"
)
"""Prints the apt package of each missing bootstrap prerequisite, one per line."""

RPM_PREREQ_PROBE = (
    "rpm -q ca-certificates >/dev/null 2>&1 || echo ca-certificates; "
    "command -v curl >/dev/null 2>&1 || echo /usr/bin/curl; "
    "command -v gpg >/dev/null 2>&1 || echo /usr/bin/gpg"
)
"""Prints the dnf spec of each missing bootstrap prerequisite, one per line.

File provides, not ``curl``: RHEL ships curl-minimal, which conflicts with the
full ``curl`` package but provides ``/usr/bin/curl``.
"""

APT_INDEXES_PROBE = "compgen -G '/var/lib/apt/lists/*_Packages*' >/dev/null && echo present || true"
"""Prints ``present`` when apt holds any package index (``apt-get update`` has run)."""


def local_run(*argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` on this host, capturing text output; raise on a nonzero exit."""
    return subprocess.run(list(argv), check=True, capture_output=True, text=True)  # noqa: S603


def apt_get(*args: str) -> tuple[str, ...]:
    """The ``apt-get`` argv for ``args``, with the :data:`APT_FAIL_FAST` options."""
    return ("apt-get", *APT_FAIL_FAST, *args)


def _apt_scope(source: str) -> tuple[str, ...]:
    """apt options that make ``source`` the only source apt reads."""
    return ("-o", f"Dir::Etc::sourcelist={source}", "-o", "Dir::Etc::sourceparts=-")


def apt_scoped_update(source: str) -> tuple[str, ...]:
    """The ``apt-get update`` argv that refreshes **only** the indexes of ``source``.

    The other sources are not consulted, and their already-downloaded indexes are
    kept (``List-Cleanup=0``), so no full Ubuntu archive download happens.
    """
    return apt_get(*_apt_scope(source), "-o", "APT::Get::List-Cleanup=0", "update")


def deb_source_path(org: OrgRepo) -> str:
    """The permanent apt source the org's keyring package writes (its postinst)."""
    return f"/etc/apt/sources.list.d/{org.vendor}.sources"


def rpm_source_path(org: OrgRepo) -> str:
    """The permanent dnf repository file the org's keyring package ships."""
    return f"/etc/yum.repos.d/{org.vendor}.repo"


def installed_key_path(org: OrgRepo, fmt: str) -> str:
    """The key file the keyring package's permanent source trusts, per format."""
    if fmt == "deb":
        return f"/usr/share/keyrings/{org.keyring_package}.asc"
    return f"/etc/pki/rpm-gpg/RPM-GPG-KEY-{org.vendor}"


def keyring_probe(fmt: str, package: str, source: str, key: str) -> str:
    """A script printing what of the keyring package's permanent setup is in place.

    It prints ``installed`` when ``package`` is installed, ``source`` when its
    permanent source file exists and ``key`` when the key that source trusts
    exists, one per line.
    """
    pkg = shlex.quote(package)
    if fmt == "deb":
        installed = (
            f"dpkg-query -W -f='${{Status}}' {pkg} 2>/dev/null | grep -qx 'install ok installed'"
        )
    else:
        installed = f"rpm -q {pkg} >/dev/null 2>&1"
    return (
        f"{installed} && echo installed; "
        f"test -e {shlex.quote(source)} && echo source; "
        f"test -e {shlex.quote(key)} && echo key; true"
    )


def apt_indexes_present(run: Run) -> bool:
    """Whether the target already holds apt package indexes (a full update has run)."""
    return run("bash", "-c", APT_INDEXES_PROBE).stdout.strip() == "present"


def _missing_prerequisites(run: Run, fmt: str) -> list[str]:
    probe = DEB_PREREQ_PROBE if fmt == "deb" else RPM_PREREQ_PROBE
    return run("bash", "-c", probe).stdout.split()


def parse_primary_fingerprint(colons: str) -> str:
    """Return the primary-key fingerprint from ``gpg --show-keys --with-colons`` output.

    The key file must hold exactly one primary key: any extra key in it would be
    trusted alongside the pinned one once the file is used as ``Signed-By``.
    """
    primaries: list[str] = []
    expecting = False
    for line in colons.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            primaries.append("")
            expecting = True
        elif fields[0] == "fpr" and expecting:
            primaries[-1] = fields[9] if len(fields) > 9 else ""
            expecting = False
    if len(primaries) > 1:
        msg = f"the published key file must hold exactly one primary key, found {len(primaries)}"
        raise PackageError(msg)
    if not primaries or not primaries[0]:
        msg = "no primary key fingerprint in the published key file"
        raise PackageError(msg)
    return primaries[0]


def _deb_files(org: OrgRepo, suite: str) -> tuple[str, str, str]:
    key = f"/etc/apt/keyrings/{org.vendor}-bootstrap.asc"
    src = f"/etc/apt/sources.list.d/{org.vendor}-bootstrap.sources"
    content = (
        "Types: deb\n"
        f"URIs: {org.base_url}/deb\n"
        f"Suites: {suite}\n"
        "Components: main\n"
        f"Signed-By: {key}\n"
    )
    return key, src, content


def _rpm_files(org: OrgRepo) -> tuple[str, str, str]:
    key = f"/etc/pki/rpm-gpg/RPM-GPG-KEY-{org.vendor}-bootstrap"
    src = f"/etc/yum.repos.d/{org.vendor}-bootstrap.repo"
    content = (
        f"[{org.vendor}-bootstrap]\n"
        f"name={org.vendor} packages (bootstrap)\n"
        f"baseurl={org.base_url}/rpm/el$releasever/$basearch\n"
        "enabled=1\n"
        "gpgcheck=1\n"
        "repo_gpgcheck=1\n"
        f"gpgkey=file://{key}\n"
    )
    return key, src, content


_KEYRING_COMPLETE = frozenset({"installed", "source", "key"})
"""What :func:`keyring_probe` prints when the keyring's permanent setup is whole."""


def _keyring_state(run: Run, probe: str) -> set[str]:
    return set(run("bash", "-c", probe).stdout.split())


def _verify_installed_key(run: Run, org: OrgRepo, key: str) -> None:
    """Fail unless the installed key file's primary fingerprint is the pinned one."""
    got = parse_primary_fingerprint(run("gpg", "--show-keys", "--with-colons", key).stdout)
    if got != org.fingerprint:
        msg = (
            f"installed {org.vendor} key {key} has primary fingerprint {got}, "
            f"pinned {org.fingerprint}: refusing to trust it "
            f"(remove {org.keyring_package} and re-run to re-bootstrap from the published key)"
        )
        raise PackageError(msg)


def bootstrap(run: Run, org: OrgRepo, fmt: str, suite: str, *, sudo: bool) -> None:
    """Make ``org``'s repository trusted on the target and install its keyring package.

    ``fmt`` is ``deb`` (apt; ``suite`` is the Ubuntu codename) or ``rpm`` (dnf;
    the EL release comes from ``$releasever``). With ``sudo``, privileged
    commands are prefixed with ``sudo``; the key download and check never are.

    Idempotent: bootstrap files left by an interrupted earlier run are removed
    before any apt or dnf call (next to the keyring's own source they make apt
    reject the source list: conflicting ``Signed-By``). When the keyring package
    is installed with its source and key in place, the bootstrap is skipped: the
    installed key's primary fingerprint is checked against the pinned one and
    the keyring's own source is refreshed. A keyring package installed without
    its source or key is reinstalled through the bootstrap path, which restores
    them.
    """
    if fmt not in ("deb", "rpm"):
        msg = f"unknown package format {fmt!r} (expected deb or rpm)"
        raise PackageError(msg)
    s = ("sudo",) if sudo else ()
    if fmt == "deb":
        key, src, content = _deb_files(org, suite)
        permanent = deb_source_path(org)
    else:
        key, src, content = _rpm_files(org)
        permanent = rpm_source_path(org)
    # A stale bootstrap source from an interrupted run, next to the keyring's own
    # source, makes apt reject the source list: remove it before any apt/dnf call.
    run(*s, "rm", "-f", src, key)

    # Prerequisites are usually present (VMs, set-up CI containers): install only
    # what is missing, and only then pay for a full index refresh.
    missing = _missing_prerequisites(run, fmt)
    if missing:
        if fmt == "deb":
            run(*s, *apt_get("update"))
            run(*s, *apt_get("install", "-y", *missing))
        else:
            run(*s, "dnf", "install", "-y", *missing)

    installed_key = installed_key_path(org, fmt)
    probe = keyring_probe(fmt, org.keyring_package, permanent, installed_key)
    state = _keyring_state(run, probe)
    if state == _KEYRING_COMPLETE:
        _verify_installed_key(run, org, installed_key)
        if fmt == "deb":
            run(*s, *apt_scoped_update(permanent))
        else:
            run(*s, "dnf", "makecache", "--refresh", "--repo", org.vendor)
        return
    # Installed but its source or key is gone: only a reinstall re-runs what wrote them.
    reinstall = "installed" in state

    tmp = run("mktemp", "-d").stdout.strip()
    if not tmp.startswith("/"):
        msg = f"mktemp -d returned {tmp!r} instead of an absolute path"
        raise PackageError(msg)
    try:
        key_tmp = f"{tmp}/{org.vendor}.asc"
        run("curl", "-fsSL", f"{org.base_url}/keys/{org.vendor}.asc", "-o", key_tmp)
        shown = run("gpg", "--show-keys", "--with-colons", key_tmp)
        got = parse_primary_fingerprint(shown.stdout)
        if got != org.fingerprint:
            msg = (
                f"{org.vendor} package key fingerprint mismatch: "
                f"published {got}, pinned {org.fingerprint} ({org.base_url}/keys/{org.vendor}.asc)"
            )
            raise PackageError(msg)

        # Only now, with the key verified, does anything touch the package sources.
        script = (
            f"umask 022 && install -D -m 0644 {shlex.quote(key_tmp)} {shlex.quote(key)}"
            f" && printf %s {shlex.quote(content)} > {shlex.quote(src)}"
        )
        try:
            run(*s, "bash", "-c", script)
            if fmt == "deb":
                # Refresh and install from the bootstrap source alone: the keyring
                # package has no dependencies outside the base system, so no other
                # index is needed, and a stray permanent source cannot conflict.
                run(*s, *apt_scoped_update(src))
                again = ("--reinstall",) if reinstall else ()
                run(*s, *apt_get(*_apt_scope(src), "install", "-y", *again, org.keyring_package))
            else:
                verb = "reinstall" if reinstall else "install"
                repo = f"{org.vendor}-bootstrap"
                run(*s, "dnf", verb, "-y", "--repo", repo, org.keyring_package)
        finally:
            run(*s, "rm", "-f", src, key)
        missing_after = sorted(_KEYRING_COMPLETE - _keyring_state(run, probe))
        if missing_after:
            msg = (
                f"installing {org.keyring_package} did not leave its setup in place "
                f"(missing: {', '.join(missing_after)}; "
                f"source {permanent}, key {installed_key})"
            )
            raise PackageError(msg)
    finally:
        run("rm", "-rf", tmp)
