"""Deferred ``package-index`` stage: wait for the release in the package index (spec §8.3).

A repo with a ``[package]`` block publishes OS packages; the org's
``publish-index`` workflow then adds them to the apt/dnf index asynchronously.
This stage polls the published index until the released version of every
``[[package]]`` entry is visible, so the consumer refresh that follows never
races it. A miss (timeout, or any error while polling) is recorded as a
deferred publish failure — the release itself stands, and ``consumer_refresh``
holds while any publish is deferred.

While it waits, the stage streams the ``Publish package index`` run in
``<org>/packages`` that covers this release, and prints a heartbeat on every
poll while no run is being streamed (#3528). Run-watching is progress output
only: the index poll is the sole pass/fail check.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET  # noqa: N817
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import config, github, retry
from vergil_tooling.lib.package import matrix, naming, orgs
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import watch_workflow

if TYPE_CHECKING:
    from vergil_tooling.lib.config import PackageConfig
    from vergil_tooling.lib.package.targets import Target
    from vergil_tooling.lib.release.context import ReleaseContext

Fetch = Callable[[str], bytes]

PHASE = "package-index"

# The org packages repo's index workflow, dispatched by cd-release's release job.
_INDEX_WORKFLOW = "publish-index.yml"
_RUN_LIST_LIMIT = "20"


def _fetch(url: str) -> bytes:
    """GET ``url``; a 404 (index or file not published yet) is ``b""``.

    Any other error propagates.
    """
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https base URL
            data: bytes = resp.read()
            return data
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return b""
        raise


def _deb_url(base_url: str, suite: str, arch: str) -> str:
    return f"{base_url}/deb/dists/{suite}/main/binary-{arch}/Packages"


def _rpm_dir(base_url: str, el: str, rpm_arch: str) -> str:
    return f"{base_url}/rpm/{el}/{rpm_arch}"


def _stanzas(text: str) -> list[dict[str, str]]:
    """The top-level ``Key: value`` fields of each stanza of a deb ``Packages`` index."""
    stanzas: list[dict[str, str]] = []
    for stanza in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in stanza.splitlines():
            if line[:1].isspace() or ":" not in line:
                continue
            key, _, value = line.partition(":")
            fields[key] = value.strip()
        stanzas.append(fields)
    return stanzas


def deb_has(
    base_url: str, suite: str, arch: str, name: str, full_version: str, fetch: Fetch
) -> bool:
    """Whether the suite's ``binary-<arch>/Packages`` lists ``name`` at ``full_version``."""
    text = fetch(_deb_url(base_url, suite, arch)).decode("utf-8")
    return any(
        fields.get("Package") == name and fields.get("Version") == full_version
        for fields in _stanzas(text)
    )


def deb_package_versions(base_url: str, suite: str, arch: str, fetch: Fetch) -> dict[str, set[str]]:
    """Each package in the suite's ``binary-<arch>/Packages`` index, with its upstream versions.

    The Debian revision (``-1``, ``-1~noble``) is dropped, so a version reads as
    the package's upstream version. A stanza without both fields is skipped.
    """
    text = fetch(_deb_url(base_url, suite, arch)).decode("utf-8")
    versions: dict[str, set[str]] = {}
    for s in _stanzas(text):
        if "Package" in s and "Version" in s:
            versions.setdefault(s["Package"], set()).add(s["Version"].rsplit("-", 1)[0])
    return versions


def _local(tag: str) -> str:
    """An element's tag without its ``{namespace}`` prefix."""
    return tag.rsplit("}", 1)[-1]


def _rpm_primary(base_url: str, el: str, rpm_arch: str, fetch: Fetch) -> ET.Element | None:
    """The parsed primary metadata of ``<el>/<rpm_arch>``; ``None`` if not published.

    Reads ``repodata/repomd.xml`` to locate the primary metadata. A ``repomd.xml``
    without a primary location raises ``ReleaseError``.
    """
    repo = _rpm_dir(base_url, el, rpm_arch)
    repomd = fetch(f"{repo}/repodata/repomd.xml")
    if not repomd:
        return None
    # The index is the org's own signed Pages site, fetched over HTTPS.
    root = ET.fromstring(repomd)  # noqa: S314
    href = None
    for data in root:
        if _local(data.tag) == "data" and data.get("type") == "primary":
            location = next((c for c in data if _local(c.tag) == "location"), None)
            href = location.get("href") if location is not None else None
            if href:
                break
    if not href:
        raise ReleaseError(
            phase=PHASE,
            command="parse repomd.xml",
            message=f"{repo}/repodata/repomd.xml has no primary metadata location",
        )
    primary = fetch(f"{repo}/{href}")
    if not primary:
        return None
    return ET.fromstring(gzip.decompress(primary))  # noqa: S314


def rpm_package_versions(
    base_url: str, el: str, rpm_arch: str, fetch: Fetch
) -> dict[str, set[str]]:
    """Each package in ``<el>/<rpm_arch>``'s primary metadata, with its versions.

    A version is the ``ver`` attribute (no release). Empty if unpublished; a
    package entry without a name or a version is skipped.
    """
    meta = _rpm_primary(base_url, el, rpm_arch, fetch)
    if meta is None:
        return {}
    versions: dict[str, set[str]] = {}
    for pkg in meta:
        fields = {_local(c.tag): c for c in pkg}
        name_el = fields.get("name")
        ver_el = fields.get("version")
        ver = ver_el.get("ver") if ver_el is not None else None
        if name_el is not None and name_el.text and ver:
            versions.setdefault(name_el.text, set()).add(ver)
    return versions


def rpm_has(
    base_url: str,
    el: str,
    rpm_arch: str,
    name: str,
    version: str,
    release: str,
    fetch: Fetch,
) -> bool:
    """Whether ``<el>/<rpm_arch>``'s primary metadata lists ``name`` at ``version-release``."""
    meta = _rpm_primary(base_url, el, rpm_arch, fetch)
    if meta is None:
        return False
    for pkg in meta:
        fields = {_local(c.tag): c for c in pkg}
        name_el = fields.get("name")
        ver_el = fields.get("version")
        if (
            name_el is not None
            and ver_el is not None
            and name_el.text == name
            and ver_el.get("ver") == version
            and ver_el.get("rel") == release
        ):
            return True
    return False


def _pick_target(pkg: PackageConfig) -> Target:
    """The first selected deb target, else the first rpm target."""
    selected = matrix.select_targets(pkg)
    debs = [t for t in selected if t.fmt == "deb"]
    return debs[0] if debs else selected[0]


def _revision(pkg: PackageConfig, target: Target) -> str:
    """Package revision: ``1`` for shared builds, suite-suffixed for ``native`` ones."""
    if not any(fnmatchcase(target.key, p) for p in pkg.native):
        return "1"
    return f"1~{target.suite}" if target.fmt == "deb" else f"1.{target.suite}"


@dataclass(frozen=True)
class _Awaited:
    """One package the stage waits for: its identity, and the index file that lists it."""

    label: str
    where: str
    visible: Callable[[], bool]

    @classmethod
    def of(cls, ctx: ReleaseContext, pkg: PackageConfig, base: str, fetch: Fetch) -> _Awaited:
        name = naming.package_name(ctx.repo_root, pkg)
        version = naming.package_version(pkg, ctx.version)
        target = _pick_target(pkg)
        release = _revision(pkg, target)
        full = f"{version}-{release}"
        label = f"{name} {full}"
        # A noarch package (deb ``all`` / rpm ``noarch``) is listed in every
        # per-arch index, so the concrete target arch directory is always polled.
        if target.fmt == "deb":
            return cls(
                label,
                _deb_url(base, target.suite, target.arch),
                lambda: deb_has(base, target.suite, target.arch, name, full, fetch),
            )
        return cls(
            label,
            f"{_rpm_dir(base, target.suite, target.rpm_arch)}/repodata/repomd.xml",
            lambda: rpm_has(base, target.suite, target.rpm_arch, name, version, release, fetch),
        )


def _timestamp(value: str) -> datetime:
    """A GitHub API timestamp (``2026-10-24T09:33:22Z``) as an aware datetime."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _dispatch_time(ctx: ReleaseContext) -> datetime | None:
    """A lower bound on when this release's CD run dispatched ``package-released``.

    cd-release dispatches at the end of its release job, so the job's start
    time precedes the dispatch. ``None`` when no CD run is recorded (a resumed
    release) or the run has no started release job.
    """
    run_id = ctx.cd_run_id
    if not run_id:
        return None
    out = retry.retry_known_resource(
        lambda: github.read_output("run", "view", "--repo", ctx.repo, run_id, "--json", "jobs"),
        resource=f"CD run {run_id}",
    )
    jobs: list[dict[str, Any]] = json.loads(out).get("jobs", []) if out.strip() else []
    for job in jobs:
        # The release job is identified by its leaf segment, as in confirm.
        if job.get("name", "").rsplit(" / ", 1)[-1] == "release" and job.get("startedAt"):
            return _timestamp(job["startedAt"])
    return None


def _following(ctx: ReleaseContext, packages_repo: str) -> datetime | None:
    """The dispatch time to follow index runs from, or ``None`` to poll only.

    Run-following is progress output, so a ``gh`` failure here is reported and
    the stage falls back to polling rather than failing.
    """
    try:
        since = _dispatch_time(ctx)
    except subprocess.CalledProcessError as exc:
        print(f"  Could not read the CD run's dispatch time ({exc}); polling the index only.")
        return None
    if since is None:
        print("  No CD release job recorded; polling the index only.")
    else:
        print(f"  Following {packages_repo} publish-index runs created since {since.isoformat()}.")
    return since


def _next_index_run(packages_repo: str, since: datetime, seen: set[str]) -> dict[str, Any] | None:
    """The earliest index run created since ``since`` that is not cancelled or followed.

    A ``repository_dispatch`` run carries no release SHA, so it is matched by
    time. Every run rebuilds the whole index, so any run after the dispatch
    covers this release, including one that supersedes a cancelled run.
    """
    out = github.read_output(
        "run",
        "list",
        "--repo",
        packages_repo,
        "--workflow",
        _INDEX_WORKFLOW,
        "--limit",
        _RUN_LIST_LIMIT,
        "--json",
        "databaseId,createdAt,conclusion,url",
    )
    runs: list[dict[str, Any]] = json.loads(out) if out.strip() else []
    candidates = [
        run
        for run in runs
        if str(run["databaseId"]) not in seen
        and run.get("conclusion") != "cancelled"
        and _timestamp(run["createdAt"]) >= since
    ]
    candidates.sort(key=lambda run: _timestamp(run["createdAt"]))
    return candidates[0] if candidates else None


def _follow_index_run(packages_repo: str, run: dict[str, Any]) -> None:
    """Stream ``run`` until it completes and say how it ended (progress output only)."""
    run_id = str(run["databaseId"])
    resource = f"publish-index run {run_id}"
    print(f"  Publish-index run: {run['url']}")
    retry.retry_known_resource(
        lambda: watch_workflow(packages_repo, run_id, check_status=False), resource=resource
    )
    conclusion = retry.retry_known_resource(
        lambda: github.read_output(
            "run",
            "view",
            "--repo",
            packages_repo,
            run_id,
            "--json",
            "conclusion",
            "--jq",
            ".conclusion",
        ),
        resource=resource,
    ).strip()
    if conclusion == "cancelled":
        print("  Publish-index run was cancelled (superseded); following the next one.")
    else:
        print(f"  Publish-index run finished ({conclusion}); checking the index.")


def _wait(
    ctx: ReleaseContext,
    *,
    fetch: Fetch,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    timeout: int,
    interval: int,
) -> None:
    cfg = config.read_config(ctx.repo_root)
    vendor = config.single_vendor(cfg)
    if vendor is None:
        print("No [package] section — nothing to wait for in a package index.")
        return
    org = orgs.for_vendor(vendor)
    waits = [_Awaited.of(ctx, pkg, org.base_url, fetch) for pkg in cfg.packages]
    for w in waits:
        print(f"Waiting for {w.label} in {w.where} (timeout {timeout}s)...")
    start = now()
    deadline = start + timeout
    packages_repo = f"{org.github_org}/packages"
    since = _following(ctx, packages_repo)
    seen: set[str] = set()
    # A package once seen stays seen: only the ones still missing are polled.
    missing = waits
    while missing := [w for w in missing if not w.visible()]:
        current = now()
        if current >= deadline:
            raise ReleaseError(
                phase=PHASE,
                command="poll package index",
                message="; ".join(f"{w.label} not visible in {w.where}" for w in missing)
                + f" after {timeout}s",
                detail=(
                    f"re-run the publish-index workflow in {org.github_org}/packages; "
                    "the release itself stands"
                ),
            )
        if since is not None:
            try:
                run = _next_index_run(packages_repo, since, seen)
                if run is not None:
                    seen.add(str(run["databaseId"]))
                    _follow_index_run(packages_repo, run)
                    continue
            except subprocess.CalledProcessError as exc:
                since = None
                print(f"  Could not follow the publish-index run ({exc}); polling the index only.")
        print(
            f"  {', '.join(w.label for w in missing)} not in the index yet "
            f"({int(current - start)}s elapsed); polling again in {interval}s"
        )
        sleep(interval)
    ctx.package_index_urls = tuple(w.where for w in waits)
    for w in waits:
        print(f"{w.label} is visible in {w.where}.")


def wait_for_index(
    ctx: ReleaseContext,
    *,
    fetch: Fetch = _fetch,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    timeout: int = 1200,
    interval: int = 30,
) -> None:
    """Poll the org package index until every package of this release is visible.

    Each ``[[package]]`` entry is polled at its own full version
    (``naming.package_version``) in its first deb target's index, else its
    first rpm target's. The stage succeeds only once all of them are listed,
    and records each one's index file in ``ctx.package_index_urls``.

    No-op without ``[package]``. Any failure (a timeout, or an error while
    polling) records ``package-index`` in ``ctx.deferred_publish_failures``
    before propagating, so the consumer refresh holds.
    """
    try:
        _wait(ctx, fetch=fetch, sleep=sleep, now=now, timeout=timeout, interval=interval)
    except Exception:
        if PHASE not in ctx.deferred_publish_failures:
            ctx.deferred_publish_failures.append(PHASE)
        raise
