"""Off-platform (cloud) VM backend: tofu two-state lifecycle + IAP transport."""

from __future__ import annotations

import hashlib
import re

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
