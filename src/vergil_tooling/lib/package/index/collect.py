"""Collect and verify the packaged stable releases of each indexed product (spec §7.2).

- Only stable ``vX.Y.Z`` releases are considered; drafts, prereleases and
  ``develop-*`` tags never are.
- A release without ``packages-manifest.json`` is pre-packaging history: it is
  skipped with a printed note.
- A release whose manifest names a ``(fmt, arch, suite)`` with no attached file,
  or which attaches a package its manifest does not name, is a hard error.
- Every collected file must pass ``gh attestation verify`` pinned to the
  vergil-actions release workflow on ``main``; every ``.rpm`` must also carry a
  valid org signature. A failure is a hard error, never a skip.
- Every ``gh`` call retries a transient GitHub error with the shared bounded
  backoff of :mod:`vergil_tooling.lib.retry` (#3153); one that outlasts the
  budget is still a hard error.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import retry
from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package.repo_setup import Run, local_run

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.package.index.config import IndexConfig


MANIFEST = "packages-manifest.json"
SIGNER_WORKFLOW = "vergil-project/vergil-actions/.github/workflows/cd-release.yml"
SOURCE_REF = "refs/heads/main"
RELEASE_LIST_LIMIT = 1000

_STABLE_TAG = re.compile(r"v\d+\.\d+\.\d+")
_FMTS = ("deb", "rpm")
_MANIFEST_ARCHES = ("amd64", "arm64", "all")
_RPM_ARCH_TO_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "noarch": "all"}
_ARCH_TO_RPM_ARCH = {v: k for k, v in _RPM_ARCH_TO_ARCH.items()}
_RPM_QF_FIELDS = 4

Metadata = tuple[str, str, str, str, tuple[str, ...]]


@dataclass(frozen=True)
class Artifact:
    product: str
    tag: str
    path: Path
    fmt: str
    name: str
    version: str
    release: str
    arch: str
    depends: tuple[str, ...]
    sha256: str

    @property
    def full_version(self) -> str:
        return f"{self.version}-{self.release}"


@dataclass(frozen=True)
class _Entry:
    """One ``packages-manifest.json`` artifact: the shape ``matrix.manifest`` emits."""

    fmt: str
    arch: str
    suite: str | None

    def __str__(self) -> str:
        return f"{self.fmt}/{self.arch}" + (f"/{self.suite}" if self.suite else "")

    @property
    def revision(self) -> str:
        """Package revision: ``1`` shared; ``1~<suite>`` (deb) / ``1.<suite>`` (rpm) native."""
        if self.suite is None:
            return "1"
        return f"1~{self.suite}" if self.fmt == "deb" else f"1.{self.suite}"

    def pattern(self) -> re.Pattern[str]:
        rev = re.escape(self.revision)
        if self.fmt == "deb":
            return re.compile(rf".+_.+-{rev}_{self.arch}\.deb")
        return re.compile(rf".+-.+-{rev}\.{_ARCH_TO_RPM_ARCH[self.arch]}\.rpm")


def _detail(exc: subprocess.CalledProcessError) -> str:
    return str(exc.stderr or exc.stdout or f"exit {exc.returncode}").strip()


def _invoke(run: Run, *argv: str) -> subprocess.CompletedProcess[str]:
    """Run ``argv``; a ``gh`` call gets the shared transient-error retry (#3153).

    A ``gh`` failure that :func:`retry.is_retryable` classifies as transient (a
    5xx, GitHub's GraphQL "Something went wrong" blip, a transport drop) is
    retried with bounded backoff and, once the budget is spent, re-raised. Any
    other failure — a missing release, a failed attestation — is raised at once,
    never retried. Local tools (``dpkg-deb``, ``rpm``, ``rpmkeys``) are never
    retried.
    """
    if argv[0] == "gh":
        return retry.call_with_retry(lambda: run(*argv))
    return run(*argv)


def _call(run: Run, what: str, *argv: str) -> subprocess.CompletedProcess[str]:
    try:
        return _invoke(run, *argv)
    except subprocess.CalledProcessError as exc:
        msg = f"{what} failed: {_detail(exc)}"
        raise PackageError(msg) from exc


def _json(run: Run, what: str, *argv: str) -> Any:
    out = _call(run, what, *argv).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        msg = f"{what} returned invalid JSON: {exc}"
        raise PackageError(msg) from exc


def stable_tags(product: str, gh: Run = local_run) -> list[str]:
    """Published, non-prerelease ``vX.Y.Z`` release tags of ``product``."""
    what = f"listing releases of {product}"
    rows = _json(
        gh,
        what,
        "gh",
        "release",
        "list",
        "--repo",
        product,
        "--limit",
        str(RELEASE_LIST_LIMIT),
        "--json",
        "tagName,isDraft,isPrerelease",
    )
    if not isinstance(rows, list):
        msg = f"{what}: expected a JSON list"
        raise PackageError(msg)
    if len(rows) >= RELEASE_LIST_LIMIT:
        msg = f"{what}: {len(rows)} releases reached the listing limit; the list may be truncated"
        raise PackageError(msg)
    return [
        r["tagName"]
        for r in rows
        if not r["isDraft"] and not r["isPrerelease"] and _STABLE_TAG.fullmatch(r["tagName"])
    ]


def _asset_names(gh: Run, where: str, product: str, tag: str) -> list[str]:
    what = f"reading assets of {where}"
    data = _json(gh, what, "gh", "release", "view", tag, "--repo", product, "--json", "assets")
    return [a["name"] for a in data["assets"]]


def _download(gh: Run, where: str, product: str, tag: str, name: str, dest: Path) -> Path:
    _call(
        gh,
        f"downloading {name} from {where}",
        "gh",
        "release",
        "download",
        tag,
        "--repo",
        product,
        "--pattern",
        name,
        "--dir",
        str(dest),
        "--clobber",
    )
    path = dest / name
    if not path.is_file():
        msg = f"downloading {name} from {where} produced no file at {path}"
        raise PackageError(msg)
    return path


def _manifest_entries(path: Path, where: str) -> list[_Entry]:
    bad = f"{where}: {MANIFEST} is malformed"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{bad}: {exc}"
        raise PackageError(msg) from exc
    arts = data.get("artifacts") if isinstance(data, dict) else None
    if not isinstance(arts, list) or not arts:
        msg = f"{bad}: expected a non-empty 'artifacts' list"
        raise PackageError(msg)
    entries: list[_Entry] = []
    for raw in arts:
        if (
            not isinstance(raw, dict)
            or raw.get("fmt") not in _FMTS
            or raw.get("arch") not in _MANIFEST_ARCHES
            or not (raw.get("suite") is None or isinstance(raw.get("suite"), str))
        ):
            msg = f"{bad}: bad artifact entry {raw!r}"
            raise PackageError(msg)
        entries.append(_Entry(raw["fmt"], raw["arch"], raw.get("suite")))
    return entries


def _match(entries: list[_Entry], packages: list[str], where: str) -> list[tuple[_Entry, str]]:
    """Pair each manifest entry with exactly one attached package; nothing left over."""
    pairs: list[tuple[_Entry, str]] = []
    claimed: set[str] = set()
    for e in entries:
        hits = [p for p in packages if e.pattern().fullmatch(p)]
        if not hits:
            msg = f"{where} is missing {e}: no attached package matches its {MANIFEST} entry"
            raise PackageError(msg)
        if len(hits) > 1:
            msg = f"{where} has {len(hits)} packages for {e}: {', '.join(sorted(hits))}"
            raise PackageError(msg)
        if hits[0] in claimed:
            msg = f"{where}: {hits[0]} is named by more than one {MANIFEST} entry"
            raise PackageError(msg)
        claimed.add(hits[0])
        pairs.append((e, hits[0]))
    extra = sorted(set(packages) - claimed)
    if extra:
        msg = f"{where} attaches package(s) not in its {MANIFEST}: {', '.join(extra)}"
        raise PackageError(msg)
    return pairs


def _deb_depends(field: str) -> tuple[str, ...]:
    """Package names in a ``Depends`` field, alternatives included, constraints dropped."""
    names: list[str] = []
    for clause in field.split(","):
        for alt in clause.split("|"):
            token = alt.strip().split(" ", 1)[0].split("(", 1)[0].split(":", 1)[0]
            if token and token not in names:
                names.append(token)
    return tuple(names)


def deb_metadata(path: Path, gh: Run = local_run) -> Metadata:
    """``(name, version, release, arch, depends)`` of a ``.deb``."""
    out = _call(
        gh,
        f"reading {path.name}",
        "dpkg-deb",
        "-f",
        str(path),
        "Package",
        "Version",
        "Architecture",
        "Depends",
    ).stdout
    fields: dict[str, str] = {}
    key = ""
    for line in out.splitlines():
        if line[:1].isspace() and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            fields[key] = value.strip()
    missing = [k for k in ("Package", "Version", "Architecture") if not fields.get(k)]
    if missing:
        msg = f"{path.name}: control data lacks {', '.join(missing)}"
        raise PackageError(msg)
    version, _, release = fields["Version"].rpartition("-")
    if not version or not release:
        msg = f"{path.name}: Version {fields['Version']!r} has no revision"
        raise PackageError(msg)
    depends = _deb_depends(fields.get("Depends", ""))
    return fields["Package"], version, release, fields["Architecture"], depends


def rpm_metadata(path: Path, gh: Run = local_run) -> Metadata:
    """``(name, version, release, arch, depends)`` of an ``.rpm``; arch in deb terms."""
    what = f"reading {path.name}"
    qf = "%{NAME}\\n%{VERSION}\\n%{RELEASE}\\n%{ARCH}\\n"
    lines = _call(gh, what, "rpm", "-qp", "--qf", qf, str(path)).stdout.splitlines()
    if len(lines) != _RPM_QF_FIELDS or not all(lines):
        msg = f"{path.name}: unexpected rpm header query output {lines!r}"
        raise PackageError(msg)
    name, version, release, rpm_arch = lines
    arch = _RPM_ARCH_TO_ARCH.get(rpm_arch)
    if arch is None:
        msg = f"{path.name}: unsupported rpm arch {rpm_arch!r}"
        raise PackageError(msg)
    depends: list[str] = []
    for line in _call(gh, what, "rpm", "-qp", "--requires", str(path)).stdout.splitlines():
        token = line.strip().split(" ", 1)[0]
        if token and not token.startswith(("rpmlib(", "/")) and token not in depends:
            depends.append(token)
    return name, version, release, arch, tuple(depends)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _release_artifacts(product: str, tag: str, workdir: Path, gh: Run) -> list[Artifact] | None:
    """The artifacts of one release, or ``None`` when it has no manifest."""
    where = f"{product}@{tag}"
    names = _asset_names(gh, where, product, tag)
    if MANIFEST not in names:
        print(f"{where}: no {MANIFEST} (pre-packaging release) — skipped")
        return None
    dest = workdir / product / tag
    dest.mkdir(parents=True, exist_ok=True)
    entries = _manifest_entries(_download(gh, where, product, tag, MANIFEST, dest), where)
    packages = [n for n in names if n.endswith((".deb", ".rpm"))]
    arts: list[Artifact] = []
    for entry, file in _match(entries, packages, where):
        path = _download(gh, where, product, tag, file, dest)
        meta = deb_metadata if entry.fmt == "deb" else rpm_metadata
        name, version, release, arch, depends = meta(path, gh)
        if (arch, release) != (entry.arch, entry.revision):
            msg = (
                f"{where}: {file} declares arch {arch!r} release {release!r}, but its "
                f"{MANIFEST} entry {entry} expects arch {entry.arch!r} release "
                f"{entry.revision!r}"
            )
            raise PackageError(msg)
        sha = _sha256(path)
        arts.append(
            Artifact(product, tag, path, entry.fmt, name, version, release, arch, depends, sha)
        )
    return arts


def collect(cfg: IndexConfig, workdir: Path, gh: Run = local_run) -> list[Artifact]:
    """Download and describe every packaged stable release of every configured product."""
    out: list[Artifact] = []
    for product in cfg.products:
        for tag in stable_tags(product, gh):
            arts = _release_artifacts(product, tag, workdir, gh)
            if arts is not None:
                out.extend(arts)
    return out


def verify(
    artifacts: list[Artifact], keyfile: Path, gh: Run = local_run, *, rpmdb: Path | None = None
) -> None:
    """Verify every artifact's build provenance, and every ``.rpm``'s org signature.

    With ``rpmdb``, the org key is imported into, and signatures are checked
    against, a private rpm database in that directory rather than the system
    one (which needs root to write).
    """
    for a in artifacts:
        try:
            _invoke(
                gh,
                "gh",
                "attestation",
                "verify",
                str(a.path),
                "--repo",
                a.product,
                "--signer-workflow",
                SIGNER_WORKFLOW,
                "--source-ref",
                SOURCE_REF,
            )
        except subprocess.CalledProcessError as exc:
            msg = (
                f"attestation verification failed for {a.path.name} "
                f"({a.product}@{a.tag}): {_detail(exc)}"
            )
            raise PackageError(msg) from exc
    rpms = [a for a in artifacts if a.fmt == "rpm"]
    if not rpms:
        return
    db: tuple[str, ...] = ()
    if rpmdb is not None:
        rpmdb.mkdir(parents=True, exist_ok=True)
        db = ("--dbpath", str(rpmdb.resolve()))
    what = f"importing {keyfile} into the rpm keyring"
    _call(gh, what, "rpmkeys", *db, "--import", str(keyfile))
    for a in rpms:
        what = f"rpm signature check failed for {a.path.name} ({a.product}@{a.tag})"
        try:
            out = gh("rpmkeys", *db, "--checksig", str(a.path)).stdout
        except subprocess.CalledProcessError as exc:
            msg = f"{what}: {_detail(exc)}"
            raise PackageError(msg) from exc
        if "signatures OK" not in out:
            msg = f"{what}: {out.strip()}"
            raise PackageError(msg)
