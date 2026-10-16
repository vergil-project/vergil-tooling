"""Normalize and compare host-dependency versions (the single comparator).

``vrg-host`` (epic vergil-project/.github#167, spec §8) classifies a dependency
as ``behind`` only when its installed version is *older* than the latest — not
merely different. That needs two things, both here:

- :func:`normalize` reduces a probe's raw output to a bare version string:
  ``"gh version 2.40.0 (2023-12-07)"`` and ``"v2.40.0"`` both become
  ``"2.40.0"``, so cosmetic differences never read as drift.
- :func:`compare` orders two bare versions. It is the **one** comparator used
  for the ``behind``/``current`` decision and for the #155 pin re-evaluation
  trigger (:func:`vergil_tooling.lib.host_pins.is_reevaluation_due`).

Both are deliberately narrow and refuse rather than guess (the tooling-wide
no-silent-failure rule): a comparable version is dotted numeric with an
optional leading ``v`` and an optional Homebrew revision suffix ``_<n>``
(``3.12.7_1`` is a rebuild of ``3.12.7`` and sorts just after it). Missing
trailing components are ``0`` (``2.0`` == ``2.0.0``) and components compare as
integers (``2024.10.04`` == ``2024.10.4``). Pre-release tags (``2.0-beta``)
and anything else raise. :func:`normalize` reads a Homebrew cask's
``<version>,<build>`` (``4.34.2,167172``) as its leading version ``4.34.2``,
the same way on the installed and the latest side; :func:`compare` itself
refuses the raw comma form.
"""

from __future__ import annotations

import re
from typing import Final

_BARE: Final = re.compile(r"v?([0-9]+(?:\.[0-9]+)*)(?:_([0-9]+))?")
# A version embedded in prose: at least two dotted components, not glued to a
# word, path, URL or other dotted/hyphenated token (so a URL's tag or a
# hyphenated date never counts as a second, competing version).
_EMBEDDED: Final = re.compile(r"(?<![\w./-])v?([0-9]+(?:\.[0-9]+)+(?:_[0-9]+)?)(?![\w./-])")


class UnrecognizedVersionError(ValueError):
    """Probe output does not contain exactly one recognizable version."""


class IncomparableVersionError(ValueError):
    """Two versions cannot be ordered by the narrow comparator."""


def normalize(raw: str) -> str:
    """Return the bare version in *raw* probe output (no leading ``v``).

    *raw* is either a bare version (``"2.40.0"``, ``"v2.40.0"``, ``"15"``) or
    prose containing exactly one distinct dotted version (``"gh version 2.40.0
    (2023-12-07)"``, ``"Docker version 27.3.1, build ce12230"``).

    Raises:
        UnrecognizedVersionError: *raw* holds no version, or several distinct
            ones (which one is meant would be a guess).
    """
    text = raw.strip()
    bare = _BARE.fullmatch(text)
    if bare is not None:
        return text.removeprefix("v")
    found = list(dict.fromkeys(_EMBEDDED.findall(text)))
    if len(found) != 1:
        what = "no version" if not found else f"several versions ({', '.join(found)})"
        raise UnrecognizedVersionError(f"cannot read a version from {raw!r}: found {what}")
    return str(found[0])


def upstream(version: str) -> str:
    """Return *version* without a Homebrew revision suffix (``3.12.7_1`` → ``3.12.7``)."""
    match = _BARE.fullmatch(version.strip())
    if match is None or match.group(2) is None:
        return version
    return version.strip().rsplit("_", 1)[0]


def _key(version: str) -> tuple[tuple[int, ...], int]:
    match = _BARE.fullmatch(version.strip())
    if match is None:
        raise IncomparableVersionError(
            f"cannot compare version {version!r}: only dotted numeric versions "
            "(e.g. '2.41.0', optional Homebrew revision '_1') are comparable"
        )
    revision = int(match.group(2)) if match.group(2) is not None else 0
    return tuple(int(part) for part in match.group(1).split(".")), revision


def compare(left: str, right: str) -> int:
    """Return -1/0/1 as *left* is older than/equal to/newer than *right*.

    Raises:
        IncomparableVersionError: either version is outside the comparable
            grammar described in the module docstring.
    """
    (a, a_rev), (b, b_rev) = _key(left), _key(right)
    width = max(len(a), len(b))
    a += (0,) * (width - len(a))
    b += (0,) * (width - len(b))
    ka, kb = (a, a_rev), (b, b_rev)
    return (ka > kb) - (ka < kb)
