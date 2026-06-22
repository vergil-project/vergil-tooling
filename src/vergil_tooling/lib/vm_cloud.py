"""Off-platform (cloud) VM backend: tofu two-state lifecycle + IAP transport."""

from __future__ import annotations

import hashlib
import re
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

_MAX_NAME = 59  # GCP instance name <=63; the module appends "-ssh" to the firewall name.
_HASH_LEN = 6


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or "x"


def cloud_resource_name(identity: str, org: str, repo: str) -> str:
    """A deterministic RFC1035 name (<=59 chars) for the GCP instance/disk/firewall."""
    base = "-".join(_slug(p) for p in (identity, org, repo))
    if not base[:1].isalpha():
        base = f"v-{base}"
    if len(base) <= _MAX_NAME:
        return base
    digest = hashlib.sha256(f"{identity}/{org}/{repo}".encode()).hexdigest()[:_HASH_LEN]
    keep = _MAX_NAME - _HASH_LEN - 1
    return f"{base[:keep].rstrip('-')}-{digest}"


def cloud_labels(identity: str, org: str, repo: str) -> dict[str, str]:
    """Structured labels for label-based recovery (independent of the mangled name)."""
    return {
        "vergil-identity": _slug(identity),
        "vergil-org": _slug(org),
        "vergil-repo": _slug(repo),
    }


_MODULES_URL = (
    "https://github.com/vergil-project/vergil-vm/releases/download/"
    "{tag}/opentofu-modules-{version}.tar.gz"
)
_TAG_RE = re.compile(r"^v\d+\.\d+(\.\d+)?$")


def fetch_modules(tag: str) -> Path:
    """Download the vergil-vm OpenTofu module tarball at *tag* and return its modules root.

    The release asset keeps ``v<version>`` in the download path segment but the
    filename drops the leading ``v`` (``opentofu-modules-<version>.tar.gz``). The
    tarball roots at ``opentofu/``, so the extracted modules live under
    ``opentofu/modules``.
    """
    if not _TAG_RE.fullmatch(tag):
        print(f"ERROR: invalid module tag '{tag}' (expected vN.N or vN.N.N)", file=sys.stderr)
        raise SystemExit(1)
    url = _MODULES_URL.format(tag=tag, version=tag.lstrip("v"))
    tmp = Path(tempfile.mkdtemp(prefix="vergil-modules-"))
    archive = tmp / "modules.tar.gz"
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310
            archive.write_bytes(resp.read())
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")  # noqa: S202
    except (urllib.error.URLError, tarfile.TarError, OSError) as exc:
        print(f"ERROR: failed to fetch modules from {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    modules = tmp / "opentofu" / "modules"
    if not modules.is_dir():
        print(f"ERROR: module archive missing opentofu/modules ({url})", file=sys.stderr)
        raise SystemExit(1)
    return modules
