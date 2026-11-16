"""Cargo's version-requirement matching.

:func:`matches` answers whether a version satisfies a Cargo dependency
requirement, by the rules Cargo documents
(https://doc.rust-lang.org/cargo/reference/specifying-dependencies.html) and
the ``semver`` crate implements: caret (the default, so a bare ``1.2`` means
``^1.2``), tilde, ``*`` wildcards, the comparison operators, comma-joined
comparators, ``=`` exact, and the pre-release rule. ``packaging`` is not used:
it implements Python's version rules, not Cargo's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NUM = r"0|[1-9][0-9]*"
_IDENT = r"[0-9A-Za-z-]+"
_VERSION_RE = re.compile(
    rf"^(?P<major>{_NUM})\.(?P<minor>{_NUM})\.(?P<patch>{_NUM})"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    rf"(?:\+{_IDENT}(?:\.{_IDENT})*)?$"
)
_PARTIAL_RE = re.compile(
    rf"^(?P<major>{_NUM})"
    rf"(?:\.(?P<minor>{_NUM}|[*xX])"
    rf"(?:\.(?P<patch>{_NUM}|[*xX])"
    rf"(?:-(?P<pre>{_IDENT}(?:\.{_IDENT})*))?"
    rf"(?:\+{_IDENT}(?:\.{_IDENT})*)?)?)?$"
)
# Longest first, so ">=" is never read as ">" followed by "=".
_OPS = (">=", "<=", ">", "<", "=", "^", "~")
_WILDCARDS = frozenset({"*", "x", "X"})


@dataclass(frozen=True)
class _Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...]


@dataclass(frozen=True)
class _Comparator:
    op: str  # one of ^ ~ = > >= < <= and "wildcard"
    major: int
    minor: int | None
    patch: int | None
    pre: tuple[str, ...]


def _pre_identifiers(pre: str | None, text: str) -> tuple[str, ...]:
    if pre is None:
        return ()
    idents = tuple(pre.split("."))
    for ident in idents:
        if ident.isdigit() and len(ident) > 1 and ident.startswith("0"):
            msg = f"invalid pre-release identifier {ident!r} in {text!r}: leading zero"
            raise ValueError(msg)
    return idents


def _parse_version(text: str) -> _Version:
    m = _VERSION_RE.match(text.strip())
    if m is None:
        msg = f"not a semver version: {text!r}"
        raise ValueError(msg)
    return _Version(
        int(m["major"]),
        int(m["minor"]),
        int(m["patch"]),
        _pre_identifiers(m["pre"], text),
    )


def _parse_comparator(text: str, req: str) -> _Comparator:
    stripped = text.strip()
    explicit_op = next((op for op in _OPS if stripped.startswith(op)), None)
    rest = stripped[len(explicit_op) :].strip() if explicit_op else stripped
    m = _PARTIAL_RE.match(rest)
    if m is None:
        msg = f"unparseable version requirement: {req!r}"
        raise ValueError(msg)
    minor_text, patch_text = m["minor"], m["patch"]
    minor_wild = minor_text in _WILDCARDS
    patch_wild = patch_text in _WILDCARDS
    if minor_wild and patch_text is not None and not patch_wild:
        msg = f"unparseable version requirement: {req!r}: a number follows a wildcard"
        raise ValueError(msg)
    if (minor_wild or patch_wild) and m["pre"] is not None:
        msg = f"unparseable version requirement: {req!r}: a pre-release follows a wildcard"
        raise ValueError(msg)
    minor = None if minor_text is None or minor_wild else int(minor_text)
    patch = None if patch_text is None or patch_wild else int(patch_text)
    if explicit_op is not None:
        op = explicit_op
    elif minor_wild or patch_wild:
        op = "wildcard"
    else:
        op = "^"
    return _Comparator(op, int(m["major"]), minor, patch, _pre_identifiers(m["pre"], req))


def _parse_req(req: str) -> tuple[_Comparator, ...] | None:
    """The requirement's comparators, or ``None`` for ``*`` (any version)."""
    stripped = req.strip()
    if stripped in _WILDCARDS:
        return None
    parts = [part.strip() for part in stripped.split(",")]
    if not stripped or any(not part for part in parts):
        msg = f"unparseable version requirement: {req!r}"
        raise ValueError(msg)
    if any(part in _WILDCARDS for part in parts):
        msg = f"unparseable version requirement: {req!r}: '*' must be the only comparator"
        raise ValueError(msg)
    return tuple(_parse_comparator(part, req) for part in parts)


def _ident_key(ident: str) -> tuple[int, int, str]:
    # Numeric identifiers sort numerically and before alphanumeric ones.
    if ident.isdigit():
        return (0, int(ident), "")
    return (1, 0, ident)


def _pre_cmp(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Compare pre-releases; no pre-release sorts after every pre-release."""
    if a == b:
        return 0
    if not a:
        return 1
    if not b:
        return -1
    ka = [_ident_key(i) for i in a]
    kb = [_ident_key(i) for i in b]
    return -1 if ka < kb else 1


def _matches_exact(c: _Comparator, v: _Version) -> bool:
    if v.major != c.major:
        return False
    if c.minor is not None and v.minor != c.minor:
        return False
    if c.patch is not None and v.patch != c.patch:
        return False
    return v.pre == c.pre


def _matches_greater(c: _Comparator, v: _Version) -> bool:
    if v.major != c.major:
        return v.major > c.major
    if c.minor is None:
        return False
    if v.minor != c.minor:
        return v.minor > c.minor
    if c.patch is None:
        return False
    if v.patch != c.patch:
        return v.patch > c.patch
    return _pre_cmp(v.pre, c.pre) > 0


def _matches_less(c: _Comparator, v: _Version) -> bool:
    if v.major != c.major:
        return v.major < c.major
    if c.minor is None:
        return False
    if v.minor != c.minor:
        return v.minor < c.minor
    if c.patch is None:
        return False
    if v.patch != c.patch:
        return v.patch < c.patch
    return _pre_cmp(v.pre, c.pre) < 0


def _matches_tilde(c: _Comparator, v: _Version) -> bool:
    if v.major != c.major:
        return False
    if c.minor is not None and v.minor != c.minor:
        return False
    if c.patch is not None and v.patch != c.patch:
        return v.patch > c.patch
    return _pre_cmp(v.pre, c.pre) >= 0


def _matches_caret(c: _Comparator, v: _Version) -> bool:
    if v.major != c.major:
        return False
    if c.minor is None:
        return True
    if c.patch is None:
        return v.minor >= c.minor if c.major > 0 else v.minor == c.minor
    if c.major > 0:
        if v.minor != c.minor:
            return v.minor > c.minor
        if v.patch != c.patch:
            return v.patch > c.patch
    elif c.minor > 0:
        if v.minor != c.minor:
            return False
        if v.patch != c.patch:
            return v.patch > c.patch
    elif v.minor != c.minor or v.patch != c.patch:
        return False
    return _pre_cmp(v.pre, c.pre) >= 0


def _matches_one(c: _Comparator, v: _Version) -> bool:
    if c.op in ("=", "wildcard"):
        return _matches_exact(c, v)
    if c.op == ">":
        return _matches_greater(c, v)
    if c.op == ">=":
        return _matches_exact(c, v) or _matches_greater(c, v)
    if c.op == "<":
        return _matches_less(c, v)
    if c.op == "<=":
        return _matches_exact(c, v) or _matches_less(c, v)
    if c.op == "~":
        return _matches_tilde(c, v)
    return _matches_caret(c, v)


def matches(req: str, version: str) -> bool:
    """Whether *version* satisfies the Cargo requirement *req*.

    A pre-release version matches only when some comparator names the same
    ``major.minor.patch`` with a pre-release of its own, as Cargo requires.
    An unparseable requirement or version raises :class:`ValueError`.
    """
    comparators = _parse_req(req)
    v = _parse_version(version)
    if comparators is None:
        return not v.pre
    if not all(_matches_one(c, v) for c in comparators):
        return False
    if not v.pre:
        return True
    return any(
        c.major == v.major and c.minor == v.minor and c.patch == v.patch and c.pre
        for c in comparators
    )
