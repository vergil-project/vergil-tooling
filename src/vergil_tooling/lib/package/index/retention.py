"""Choose what the index keeps: per-line retention plus dependency closure (spec §7.2).

For each product, the newest ``lines`` major.minor lines are kept, and within
each line the newest ``keep`` releases. Every artifact of a retained release is
kept. Then, for every dependency name of a retained artifact that some
collected artifact provides (same format, same arch or ``all``), the newest
provider is also kept, transitively — so anything indexed is installable.

Every artifact whose package name is in ``retire`` is dropped, for every format
and arch, before the closure runs, so a retired package pulls in none of its
own dependencies. A kept artifact that still depends on a retired name is a
hard error: the index would publish something that cannot be installed.

Duplicates share ``(fmt, name, full_version, arch)``. Different bytes are fatal
(across *all* collected artifacts, not only retained ones); identical bytes
collapse to one.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from vergil_tooling.lib.package.index.collect import Artifact, Identity

_STABLE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def _tag_key(a: Artifact) -> tuple[int, int, int]:
    m = _STABLE_TAG.fullmatch(a.tag)
    if m is None:
        msg = f"{a.product}@{a.tag} is not a stable vX.Y.Z tag"
        raise PackageError(msg)
    return int(m[1]), int(m[2]), int(m[3])


def version_key(full_version: str) -> tuple[int, ...]:
    """Order package versions by their numeric components only.

    Mixed int/str tuples are not orderable in Python 3, and only the numeric
    parts carry meaning for the versions this repository produces.
    """
    return tuple(int(x) for x in re.split(r"[.+~:-]", full_version) if x.isdigit())


def _check_conflicts(artifacts: list[Artifact]) -> None:
    first: dict[Identity, Artifact] = {}
    for a in artifacts:
        prev = first.setdefault(a.identity, a)
        if prev.sha256 != a.sha256:
            msg = (
                f"{a.name} {a.full_version} {a.arch} ({a.fmt}) differs between "
                f"{prev.product}@{prev.tag} and {a.product}@{a.tag}; a re-release must "
                "bump the package version"
            )
            raise PackageError(msg)


def _by_lines(artifacts: list[Artifact], keep: int, lines: int) -> set[int]:
    """Indices (into ``artifacts``) of the artifacts in retained releases."""
    tags: dict[str, set[tuple[int, int, int]]] = {}
    for a in artifacts:
        tags.setdefault(a.product, set()).add(_tag_key(a))
    retained: set[tuple[str, tuple[int, int, int]]] = set()
    for product, versions in tags.items():
        line_tags: dict[tuple[int, int], list[tuple[int, int, int]]] = {}
        for v in versions:
            line_tags.setdefault((v[0], v[1]), []).append(v)
        for line in sorted(line_tags, reverse=True)[:lines]:
            retained.update((product, v) for v in sorted(line_tags[line], reverse=True)[:keep])
    return {i for i, a in enumerate(artifacts) if (a.product, _tag_key(a)) in retained}


def _close(artifacts: list[Artifact], kept: set[int]) -> set[int]:
    """Add the newest provider of every dependency of a kept artifact, transitively."""
    providers: dict[tuple[str, str], list[int]] = {}
    for i, a in enumerate(artifacts):
        providers.setdefault((a.fmt, a.name), []).append(i)
    pending = sorted(kept)
    while pending:
        a = artifacts[pending.pop()]
        for dep in a.depends:
            candidates = [
                j for j in providers.get((a.fmt, dep), []) if artifacts[j].arch in (a.arch, "all")
            ]
            if not candidates:
                continue  # an OS package, or another org's: not ours to keep
            newest = max(candidates, key=lambda j: version_key(artifacts[j].full_version))
            if newest not in kept:
                kept.add(newest)
                pending.append(newest)
    return kept


def _check_retired(artifacts: list[Artifact], kept: set[int], retire: frozenset[str]) -> None:
    """Refuse a kept artifact that depends on a retired package name."""
    for i in sorted(kept):
        a = artifacts[i]
        hit = sorted(retire.intersection(a.depends))
        if hit:
            msg = (
                f"{a.identity} from {a.product}@{a.tag} depends on retired package(s) "
                f"{', '.join(hit)}; un-retire them or stop the index keeping this release"
            )
            raise PackageError(msg)


def select(
    artifacts: list[Artifact], keep: int, lines: int, *, retire: frozenset[str] = frozenset()
) -> list[Artifact]:
    """Return the artifacts the index keeps, in input order."""
    _check_conflicts(artifacts)
    by_lines = {i for i in _by_lines(artifacts, keep, lines) if artifacts[i].name not in retire}
    kept = _close(artifacts, by_lines)
    # A retired artifact enters the closure only as a dependency of a kept one,
    # so this check also guarantees none is kept.
    _check_retired(artifacts, kept, retire)
    out: list[Artifact] = []
    seen: set[Identity] = set()
    for i, a in enumerate(artifacts):
        if i in kept and a.identity not in seen:
            seen.add(a.identity)
            out.append(a)
    return out
