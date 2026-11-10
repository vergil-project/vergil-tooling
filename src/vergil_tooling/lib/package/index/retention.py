"""Choose what the index publishes: per-line retention and the retire filter (spec §7.2).

Retention runs on release tags, before anything is downloaded
(:func:`select_tags`): for each product, the newest ``lines`` major.minor
lines are kept, and within each line the newest ``keep`` releases. Only those
releases are downloaded, and every artifact of one is kept.

Then every artifact whose package name is in ``retire`` is dropped, for every
format and arch (:func:`drop_retired`), before what remains is verified and
indexed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from vergil_tooling.lib.package.index.collect import Artifact

_STABLE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def _tag_key(tag: str) -> tuple[int, int, int]:
    m = _STABLE_TAG.fullmatch(tag)
    if m is None:
        msg = f"{tag} is not a stable vX.Y.Z tag"
        raise PackageError(msg)
    return int(m[1]), int(m[2]), int(m[3])


def version_key(full_version: str) -> tuple[int, ...]:
    """Order package versions by their numeric components only.

    Mixed int/str tuples are not orderable in Python 3, and only the numeric
    parts carry meaning for the versions this repository produces.
    """
    return tuple(int(x) for x in re.split(r"[.+~:-]", full_version) if x.isdigit())


def select_tags(tags: list[str], keep: int, lines: int) -> list[str]:
    """The newest ``keep`` tags in each of the newest ``lines`` major.minor lines, newest first."""
    line_tags: dict[tuple[int, int], list[str]] = {}
    for tag in tags:
        major, minor, _ = _tag_key(tag)
        line_tags.setdefault((major, minor), []).append(tag)
    out: list[str] = []
    for line in sorted(line_tags, reverse=True)[:lines]:
        out.extend(sorted(line_tags[line], key=_tag_key, reverse=True)[:keep])
    return out


def drop_retired(artifacts: list[Artifact], retire: frozenset[str]) -> list[Artifact]:
    """``artifacts`` without any whose package name is in ``retire``, in input order."""
    return [a for a in artifacts if a.name not in retire]
