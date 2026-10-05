"""``packages.toml``: the products an org's package repository indexes (spec §7.1).

```toml
vendor = "vergil"
products = ["vergil-project/packages", "vergil-project/vergil-tooling"]

[retention]          # optional
keep = 3             # releases kept per major.minor line
lines = 2            # newest major.minor lines kept per product
```
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_KEEP = 3
DEFAULT_LINES = 2

_TOP_KEYS = frozenset({"vendor", "products", "retention"})
_RETENTION_KEYS = frozenset({"keep", "lines"})
_PRODUCT_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class IndexConfig:
    vendor: str
    products: list[str]
    keep: int
    lines: int


def _positive_int(table: dict[str, Any], key: str, default: int, name: str) -> int:
    value = table.get(key, default)
    # bool is an int subclass; ``keep = true`` is a mistake, not 1.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{name}: retention.{key} must be an integer >= 1, got {value!r}"
        raise PackageError(msg)
    return value


def load(path: Path) -> IndexConfig:
    """Parse and validate ``packages.toml`` at ``path``; raise ``PackageError`` on any problem."""
    name = path.name
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"{name} not found at {path}"
        raise PackageError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"{name} is not valid TOML: {exc}"
        raise PackageError(msg) from exc

    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        msg = f"{name}: unknown key(s): {', '.join(unknown)}"
        raise PackageError(msg)

    vendor = data.get("vendor")
    if not isinstance(vendor, str) or not vendor:
        msg = f"{name}: vendor is required (a non-empty string)"
        raise PackageError(msg)

    products = data.get("products")
    if not isinstance(products, list) or not products:
        msg = f"{name}: products must be a non-empty list of <owner>/<repo> strings"
        raise PackageError(msg)
    names: list[str] = []
    for p in products:
        if not isinstance(p, str) or not _PRODUCT_RE.fullmatch(p):
            msg = f"{name}: products entry {p!r} is not an <owner>/<repo> string"
            raise PackageError(msg)
        if p in names:
            msg = f"{name}: products lists {p!r} more than once"
            raise PackageError(msg)
        names.append(p)

    retention = data.get("retention", {})
    if not isinstance(retention, dict):
        msg = f"{name}: [retention] must be a table"
        raise PackageError(msg)
    unknown = sorted(set(retention) - _RETENTION_KEYS)
    if unknown:
        msg = f"{name}: unknown key(s) in [retention]: {', '.join(unknown)}"
        raise PackageError(msg)

    return IndexConfig(
        vendor=vendor,
        products=names,
        keep=_positive_int(retention, "keep", DEFAULT_KEEP, name),
        lines=_positive_int(retention, "lines", DEFAULT_LINES, name),
    )
