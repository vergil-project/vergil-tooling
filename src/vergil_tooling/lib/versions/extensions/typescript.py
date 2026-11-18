"""The TypeScript resolver extension (epic vergil-project/.github#1281, spec §6.2).

Every TypeScript-specific version rule lives here, and nowhere in the shared code:

* **Toolchain selection.** The Node runtime belongs to Vergil, as Python's
  interpreter does (epic vergil-project/.github#407, spec §6.5): Vergil selects
  it through the image, and runs the npm bundled with each window Node major.
  So every setting that names a Node or package-manager version is an error
  (:func:`check_toolchain`): the ``.nvmrc`` and ``.node-version`` files, and,
  in the root ``package.json``, ``packageManager`` (whatever it names, npm
  included), ``volta``, an ``npm``, ``pnpm`` or ``yarn`` key under ``engines``,
  and a ``devEngines`` ``runtime`` or ``packageManager`` entry. npm checks
  ``devEngines`` before ``install``, ``ci`` and ``run``, and by default fails
  when the running Node or npm does not match. ``devEngines``' ``os``, ``cpu``
  and ``libc`` entries are allowed, and ``engines.node`` is a range, not a
  selection.
* **The floor.** ``engines.node`` is the repo's declared floor, checked on two
  sides as Python's ``requires-python`` is (epic vergil-project/.github#407,
  spec §6.4): :func:`check_floor` warns when the range admits a major below the
  window's oldest, or no release of its newest. A floor above the oldest is a
  deliberate restriction and is allowed.

The root ``package.json`` is read with :func:`~vergil_tooling.lib.container.read_package_json`,
the one parser of the file; a file it cannot read is a ``VersionsError``, so
``vrg-validate`` reports it as an ``ERROR:`` line, never a traceback.

``engines.node`` is read with npm's range grammar (node-semver's documented
range syntax, https://github.com/npm/node-semver#ranges: comparators,
``x``-ranges, ``~``, ``^``, hyphen ranges and ``||``) by :func:`_parse_range`,
a reader of its own: no maintained Python implementation of that grammar
supports Python 3.15 (epic vergil-project/.github#1281, spec §10). A range it
cannot read, and a range that admits no release, is a ``VersionsError``, never
"no floor". Prerelease versions play no part: the reader works on releases
only.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib.container import PackageJsonError, read_package_json
from vergil_tooling.lib.versions import VersionsError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["check_floor", "check_toolchain"]

_PACKAGE_JSON = "package.json"
_RUNTIME_OWNED = "the Node runtime, which belongs to Vergil (Vergil selects it through the image)"
_PM_OWNED = (
    "the package manager, which belongs to Vergil (Vergil runs the npm bundled with "
    "each window Node major)"
)
# Version files that select the Node runtime, in the order they are reported.
_VERSION_FILES = (".nvmrc", ".node-version")
# Package managers whose version an ``engines`` key would select.
_ENGINES_PMS = ("npm", "pnpm", "yarn")
# ``devEngines`` entries that select a version, with what each selects.
_DEV_ENGINES_OWNED = (("runtime", _RUNTIME_OWNED), ("packageManager", _PM_OWNED))


def _package_json(repo_root: Path) -> dict[str, object] | None:
    """The root ``package.json``, or ``None`` when there is none.

    A file that cannot be read, is not JSON or is not a JSON object is a
    :class:`VersionsError` with :class:`PackageJsonError`'s message.
    """
    path = repo_root / _PACKAGE_JSON
    if not path.exists():
        return None
    try:
        return read_package_json(path)
    except PackageJsonError as exc:
        raise VersionsError(str(exc)) from exc


def _json_type(value: object) -> str:
    """The JSON name of a parsed value's type (``null``, ``string``, ``array`` ...)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _object_field(path: Path, data: dict[str, object], field: str) -> dict[str, object]:
    """*data*'s *field* as an object (empty when absent); anything else, null included, is fatal."""
    if field not in data:
        return {}
    value = data[field]
    if not isinstance(value, dict):
        msg = (
            f"{path}: {field} is a JSON {_json_type(value)}, not an object. "
            f"Fix: make {field} an object"
        )
        raise VersionsError(msg)
    return value


def _check_dev_engines(path: Path, dev_engines: dict[str, object]) -> None:
    """Each ``devEngines`` entry must be an object or an array of objects (npm's forms)."""
    for key, value in dev_engines.items():
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            if not isinstance(entry, dict):
                msg = (
                    f"{path}: devEngines.{key} holds a JSON {_json_type(entry)}; npm "
                    f"accepts an object or an array of objects. Fix: correct devEngines.{key}"
                )
                raise VersionsError(msg)


def check_toolchain(repo_root: Path) -> list[str]:
    """One error for each setting that names a Node or package-manager version.

    In this order: ``.nvmrc``, ``.node-version``, ``packageManager``,
    ``volta``, ``engines.npm``, ``engines.pnpm``, ``engines.yarn``,
    ``devEngines.runtime``, ``devEngines.packageManager``. A malformed
    ``package.json``, ``engines`` or ``devEngines`` is a :class:`VersionsError`
    naming the field.
    """
    errors = [
        f"{name} selects {_RUNTIME_OWNED}. Fix: delete {name}"
        for name in _VERSION_FILES
        if (repo_root / name).exists()
    ]
    data = _package_json(repo_root)
    if data is None:
        return errors
    path = repo_root / _PACKAGE_JSON
    engines = _object_field(path, data, "engines")
    dev_engines = _object_field(path, data, "devEngines")
    _check_dev_engines(path, dev_engines)
    if "packageManager" in data:
        errors.append(
            f'{_PACKAGE_JSON} "packageManager" selects {_PM_OWNED}. '
            f'Fix: remove the "packageManager" field'
        )
    if "volta" in data:
        errors.append(
            f'{_PACKAGE_JSON} "volta" pins the Node runtime and package managers, which '
            f"belong to Vergil (Vergil selects Node through the image, and runs the npm "
            f'bundled with each window Node major). Fix: remove the "volta" field'
        )
    errors.extend(
        f"{_PACKAGE_JSON} engines.{pm} selects {_PM_OWNED}. Fix: remove engines.{pm}"
        for pm in _ENGINES_PMS
        if pm in engines
    )
    errors.extend(
        f"{_PACKAGE_JSON} devEngines.{entry} selects {owned}; by default npm fails "
        f"install, ci and run when it does not match. Fix: remove devEngines.{entry}"
        for entry, owned in _DEV_ENGINES_OWNED
        if entry in dev_engines
    )
    return errors


# --- engines.node: npm's range grammar, read over releases ------------------
#
# The regexes follow node-semver's own (internal/re.js, non-loose mode): a
# version part is ``0`` or a number without a leading zero, or ``x``, ``X`` or
# ``*``; a prerelease and a build may follow only a third part; a version may be
# preceded by any run of ``v``, ``=`` and spaces.

# A release version, as (major, minor, patch).
type _Release = tuple[int, int, int]

_NR = r"0|[1-9]\d*"
_XR = rf"[xX*]|{_NR}"
_PRE_ID = r"0|[1-9]\d*|\d*[a-zA-Z-][a-zA-Z0-9-]*"


def _partial(p: str = "") -> str:
    """A version with optional ``x`` parts, its groups named with the prefix *p*."""
    return (
        rf"[v=\s]*(?P<{p}M>{_XR})(?:\.(?P<{p}m>{_XR})(?:\.(?P<{p}p>{_XR})"
        rf"(?:-(?P<{p}pre>(?:{_PRE_ID})(?:\.(?:{_PRE_ID}))*))?"
        r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)?)?"
    )


_HYPHEN = re.compile(rf"^{_partial('a')}\s+-\s+{_partial('b')}$")
_COMPARATOR = re.compile(rf"^(?P<op>~>?|\^|[<>]?=?){_partial()}$")
# node-semver's comparator, tilde and caret trims: an operator binds to the
# version after it across spaces, before the alternative is split on spaces.
_OPERATOR_TRIM = re.compile(r"(~>?|\^|[<>]=?|=)\s+")
_OR = re.compile(r"\s*\|\|\s*")
# node-semver rejects a version part above JavaScript's Number.MAX_SAFE_INTEGER.
_MAX_PART = 2**53 - 1


@dataclass(frozen=True)
class _Interval:
    """The releases ``low <= v < high``; ``high`` ``None`` is unbounded."""

    low: _Release = (0, 0, 0)
    high: _Release | None = None

    def meet(self, other: _Interval) -> _Interval:
        highs = [h for h in (self.high, other.high) if h is not None]
        return _Interval(max(self.low, other.low), min(highs) if highs else None)

    @property
    def empty(self) -> bool:
        return self.high is not None and self.low >= self.high


_ANY = _Interval()
_NOTHING = _Interval((0, 0, 0), (0, 0, 0))


@dataclass(frozen=True)
class _NodeRange:
    """An npm range, as the releases its ``||`` alternatives admit (none empty)."""

    intervals: tuple[_Interval, ...]

    def lowest(self) -> _Release | None:
        """The lowest release the range admits; ``None`` when it admits none."""
        return min((i.low for i in self.intervals), default=None)

    def admits_major(self, major: int) -> bool:
        """Whether the range admits some ``major.x.y`` release."""
        return any(
            i.low < (major + 1, 0, 0) and (i.high is None or i.high > (major, 0, 0))
            for i in self.intervals
        )


class _RangeSyntaxError(Exception):
    """A part of ``engines.node`` that is not npm range syntax."""


def _part(text: str | None) -> int | None:
    """A version part as a number; ``None`` for a missing or ``x`` part."""
    if text is None or text in {"x", "X", "*"}:
        return None
    value = int(text)
    if value > _MAX_PART:
        msg = f"version part {text} is above {_MAX_PART}"
        raise _RangeSyntaxError(msg)
    return value


def _parts(match: re.Match[str], p: str = "") -> tuple[int | None, int | None, int | None, bool]:
    """(major, minor, patch, has-prerelease); a part after an ``x`` part is ``x`` too."""
    major = _part(match[f"{p}M"])
    minor = None if major is None else _part(match[f"{p}m"])
    patch = None if minor is None else _part(match[f"{p}p"])
    return major, minor, patch, match[f"{p}pre"] is not None


def _tilde(major: int | None, minor: int | None, patch: int | None) -> _Interval:
    """``~``: patch changes if a minor is given, minor changes if not."""
    if major is None:
        return _ANY
    if minor is None:
        return _Interval((major, 0, 0), (major + 1, 0, 0))
    return _Interval((major, minor, patch or 0), (major, minor + 1, 0))


def _caret(major: int | None, minor: int | None, patch: int | None) -> _Interval:
    """``^``: changes that keep the left-most non-zero part."""
    if major is None:
        return _ANY
    if minor is None:
        return _Interval((major, 0, 0), (major + 1, 0, 0))
    low = (major, minor, patch or 0)
    if major > 0:
        return _Interval(low, (major + 1, 0, 0))
    if minor > 0 or patch is None:
        return _Interval(low, (0, minor + 1, 0))
    return _Interval(low, (0, 0, patch + 1))


def _partial_bound(op: str, major: int, minor: int | None) -> _Interval:
    """A ``<``, ``<=``, ``>`` or ``>=`` comparator on a version with an ``x`` part."""
    start = (major, minor or 0, 0)
    after = (major + 1, 0, 0) if minor is None else (major, minor + 1, 0)
    return {
        ">": _Interval(after),
        ">=": _Interval(start),
        "<": _Interval(high=start),
        "<=": _Interval(high=after),
    }[op]


def _full_bound(op: str, release: _Release, prerelease: bool) -> _Interval:
    """A comparator on a full version, over releases.

    A prerelease sits just below its release, so among releases ``>v-pre`` and
    ``>=v-pre`` are ``>=v``, ``<v-pre`` and ``<=v-pre`` are ``<v``, and
    ``=v-pre`` admits none.
    """
    major, minor, patch = release
    after = (major, minor, patch + 1)
    if op in {"", "="}:
        return _NOTHING if prerelease else _Interval(release, after)
    if op == ">":
        return _Interval(release if prerelease else after)
    if op == ">=":
        return _Interval(release)
    if op == "<":
        return _Interval(high=release)
    return _Interval(high=release if prerelease else after)  # "<="


def _comparator(token: str) -> _Interval:
    """One space-separated comparator, as node-semver desugars it."""
    match = _COMPARATOR.match(token)
    if match is None:
        msg = f"{token!r} is not a comparator"
        raise _RangeSyntaxError(msg)
    op = match["op"]
    major, minor, patch, prerelease = _parts(match)
    if op.startswith("~"):
        return _tilde(major, minor, patch)
    if op == "^":
        return _caret(major, minor, patch)
    op = "" if op == "=" else op
    if major is None:
        return _NOTHING if op in {"<", ">"} else _ANY
    if minor is None or patch is None:
        if not op:
            return _tilde(major, minor, patch)
        return _partial_bound(op, major, minor)
    return _full_bound(op, (major, minor, patch), prerelease)


def _hyphen(match: re.Match[str]) -> _Interval:
    """``A - B``: ``>=A <=B``, a partial ``A`` filled with zeros, a partial ``B`` widened."""
    low_major, low_minor, low_patch, _ = _parts(match, "a")
    high_major, high_minor, high_patch, high_pre = _parts(match, "b")
    low = _ANY if low_major is None else _Interval((low_major, low_minor or 0, low_patch or 0))
    if high_major is None:
        high = _ANY
    elif high_minor is None or high_patch is None:
        high = _partial_bound("<=", high_major, high_minor)
    else:
        high = _full_bound("<=", (high_major, high_minor, high_patch), high_pre)
    return low.meet(high)


def _alternative(text: str) -> _Interval:
    """One ``||`` alternative: a hyphen range, or comparators that all must hold."""
    hyphen = _HYPHEN.match(text)
    if hyphen is not None:
        return _hyphen(hyphen)
    interval = _ANY
    for token in _OPERATOR_TRIM.sub(r"\1", text).split():
        interval = interval.meet(_comparator(token))
    return interval


def _parse_range(text: str) -> _NodeRange:
    """*text* read as an npm range; anything that is not one is a :class:`VersionsError`.

    An alternative that admits no release is dropped; a range that keeps none
    is returned with no intervals, for :func:`check_floor` to report.
    """
    try:
        intervals = [_alternative(alt) for alt in _OR.split(text.strip())]
    except _RangeSyntaxError as exc:
        msg = f"engines.node {json.dumps(text)} is not an npm semver range: {exc}"
        raise VersionsError(msg) from exc
    return _NodeRange(tuple(i for i in intervals if not i.empty))


def _major(which: str, version: str) -> int:
    """A window version as a Node major; the window lists bare majors (``"22"``)."""
    if re.fullmatch(_NR, version) is None:
        msg = f"the typescript window's {which} version {version!r} is not a Node major"
        raise VersionsError(msg)
    return int(version)


def check_floor(repo_root: Path, oldest: str, newest: str) -> list[str]:
    """Warn when ``engines.node`` admits a major below *oldest*, or excludes *newest*.

    No ``package.json``, ``engines`` or ``engines.node`` gives nothing. An
    ``engines.node`` that is not a string, is not an npm range or admits no
    release is a :class:`VersionsError` naming the value.
    """
    oldest_major, newest_major = _major("oldest", oldest), _major("newest", newest)
    data = _package_json(repo_root)
    if data is None:
        return []
    path = repo_root / _PACKAGE_JSON
    engines = _object_field(path, data, "engines")
    if "node" not in engines:
        return []
    text = engines["node"]
    if not isinstance(text, str):
        msg = (
            f"{path}: engines.node is a JSON {_json_type(text)} ({json.dumps(text)}), not a "
            f'string. Fix: make engines.node an npm range string, such as ">={oldest}"'
        )
        raise VersionsError(msg)
    try:
        node_range = _parse_range(text)
    except VersionsError as exc:
        msg = f"{path}: {exc}. Fix: correct engines.node, for example to >={oldest}"
        raise VersionsError(msg) from exc
    lowest = node_range.lowest()
    if lowest is None:
        msg = (
            f"{path}: engines.node {text} admits no Node release. "
            f"Fix: correct engines.node, for example to >={oldest}"
        )
        raise VersionsError(msg)
    warnings: list[str] = []
    if lowest[0] < oldest_major:
        warnings.append(
            f"engines.node {text} admits Node {lowest[0]}, below the oldest supported "
            f"major {oldest}. Fix: raise it to >={oldest}"
        )
    if not node_range.admits_major(newest_major):
        warnings.append(
            f"engines.node {text} excludes Node {newest}. Fix: drop the upper bound, "
            f"or widen it to admit {newest}"
        )
    return warnings
