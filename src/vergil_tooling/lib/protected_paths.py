"""The protected-path set of a repository, and the match of a change against it.

A **protected path** is a file whose change needs the human's approval before
it merges (epic vergil-project/.github#1384, spec §4.4). A repository's set
is the union of two sources:

- the fleet default, :data:`FLEET_DEFAULT`: ``.github/workflows/**`` and
  ``vergil.toml`` in every repository, and the registry file itself;
- the repository's own registry, :data:`REGISTRY_FILE` at the repository
  root, a TOML file holding exactly one key, ``paths = [<glob>, …]``.

The registry is read as committed at a ref the caller names: the freshly
fetched ``origin/develop`` at submit and in the watch, and the merge commit's
first parent in the audit. The pull request's own copy is never read, so a
branch cannot drop a path from the set its own change is judged by; and since
the registry is itself protected, the change that drops one needs an approval.

Every read fails closed: an unknown ref, a registry that is not a regular
file, is not UTF-8, is not valid TOML, or holds anything but a list of
well-formed globs raises :class:`ProtectedPathsError`, naming the file and the
ref. Only an absent registry is empty.

**The glob.** Paths are matched whole, case-sensitively, from the repository
root. ``*`` matches any run of characters but ``/``, ``?`` one character but
``/``, and ``[…]`` one character of a class (``[!…]`` negates it). ``**`` is a
whole path segment: ``a/**`` matches every path below ``a``, ``**/b`` matches
``b`` in any directory (the root included), and ``a/**/b`` matches ``a/b``
and ``a/x/…/b``.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: The registry file, at the repository root.
REGISTRY_FILE = "protected-paths.toml"

#: The paths protected in every repository, whatever its registry says.
FLEET_DEFAULT: tuple[str, ...] = (".github/workflows/**", "vergil.toml", REGISTRY_FILE)

#: The blob recorded for a protected path the change deletes.
DELETED = "deleted"

#: The ``git diff --name-status`` statuses the match understands. ``-M`` never
#: reports a copy, and a committed diff has no unmerged entry, so any other
#: status is a doubt and refuses.
_KNOWN_STATUSES = frozenset({"A", "M", "D", "T", "R"})

#: The modes of a regular file in a git tree.
_FILE_MODES = frozenset({"100644", "100755"})


class ProtectedPathsError(Exception):
    """The protected set or the change could not be read; nothing is protected-free."""


@dataclass(frozen=True)
class ProtectedChange:
    """One protected path a change touches, and its object at the head.

    *blob* is the object ID ``git ls-tree`` gives the path at the head, or
    :data:`DELETED` when the head has no file there.
    """

    path: str
    blob: str


# -- git ---------------------------------------------------------------------------


def _check_ref(ref: str) -> None:
    if not ref or ref.startswith("-"):
        msg = f"not a git revision: {ref!r}"
        raise ProtectedPathsError(msg)


def _git(repo: Path, *args: str) -> bytes:
    """Run ``git -C <repo> *args`` and return its stdout, unstripped.

    Pathspecs are literal (``GIT_LITERAL_PATHSPECS``), so a path is never read
    as a pattern. Any failure raises :class:`ProtectedPathsError` with git's
    own message.
    """
    cmd = ("git", "-C", str(repo), *args)
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            check=False,
            capture_output=True,
            env={**os.environ, "GIT_LITERAL_PATHSPECS": "1"},
        )
    except OSError as exc:
        msg = f"could not run {' '.join(cmd)}: {exc}"
        raise ProtectedPathsError(msg) from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        msg = f"{' '.join(cmd)} failed (exit {result.returncode}): {detail}"
        raise ProtectedPathsError(msg)
    return result.stdout


def _commit(repo: Path, ref: str) -> str:
    """Resolve *ref* to the commit it names, or raise naming the ref."""
    _check_ref(ref)
    try:
        return (
            _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
            .decode()
            .strip()
        )
    except ProtectedPathsError as exc:
        msg = f"cannot read ref {ref!r}: {exc}"
        raise ProtectedPathsError(msg) from exc


def _tree_entry(repo: Path, commit: str, path: str) -> tuple[str, str, str] | None:
    """Return ``(mode, type, object)`` of *path* in *commit*, or ``None`` when absent."""
    output = _git(repo, "ls-tree", "-z", "--full-tree", commit, "--", path)
    for record in output.split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        if name.decode("utf-8", "surrogateescape") != path:
            continue
        fields = meta.decode().split(" ")
        if len(fields) != 3:  # noqa: PLR2004 - mode, type, object
            msg = f"git ls-tree: unexpected entry {record!r}"
            raise ProtectedPathsError(msg)
        return fields[0], fields[1], fields[2]
    return None


# -- the glob ----------------------------------------------------------------------


def _translate(pattern: str) -> str:
    """Translate a well-formed glob into a regular expression (see the module docs)."""
    parts: list[str] = []
    segments = pattern.split("/")
    last = len(segments) - 1
    for number, segment in enumerate(segments):
        if segment == "**":
            parts.append(".*" if number == last else "(?:.*/)?")
            continue
        parts.append(_translate_segment(segment))
        if number != last:
            parts.append("/")
    return "".join(parts)


def _translate_segment(segment: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[":
            start = index + 1
            negate = segment[start : start + 1] in ("!", "^")
            if negate:
                start += 1
            end = segment.index("]", start + 1)
            body = segment[start:end]
            escaped = "".join("\\" + c if c in "\\^[]" else c for c in body)
            out.append(f"[^/{escaped}]" if negate else f"[{escaped}]")
            index = end
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _glob_problem(pattern: str) -> str | None:
    """Return why *pattern* is not a well-formed glob, or ``None`` when it is."""
    if not pattern:
        return "an empty path"
    if "\\" in pattern:
        return "a backslash, which globs do not use"
    if pattern.startswith("/"):
        return "a leading '/' (paths are relative to the repository root)"
    for segment in pattern.split("/"):
        if segment in ("", ".", ".."):
            return f"an empty, '.' or '..' segment ({segment!r})"
        if "**" in segment and segment != "**":
            return f"'**' inside a segment ({segment!r}); '**' must be a whole segment"
    index = 0
    while index < len(pattern):
        if pattern[index] == "[":
            start = index + 1
            if pattern[start : start + 1] in ("!", "^"):
                start += 1
            end = pattern.find("]", start + 1)
            if end == -1:
                return "an unclosed '['"
            if "/" in pattern[start:end]:
                return "a '/' inside '[…]'"
            index = end
        elif pattern[index] == "]":
            return "a ']' with no '['"
        index += 1
    return None


def matches(pattern: str, path: str) -> bool:
    """Whether *path* (from the repository root) matches the glob *pattern*.

    Raises :class:`ProtectedPathsError` when *pattern* is not well-formed.
    """
    problem = _glob_problem(pattern)
    if problem is not None:
        msg = f"protected path {pattern!r} is malformed: {problem}"
        raise ProtectedPathsError(msg)
    return re.fullmatch(_translate(pattern), path, flags=re.DOTALL) is not None


# -- the registry ------------------------------------------------------------------


def parse_registry(text: str, source: str) -> tuple[str, ...]:
    """Parse registry *text*; *source* names it (file and ref) in every error.

    The file holds exactly one key, ``paths``, a list of well-formed globs (an
    empty list is allowed). Anything else raises :class:`ProtectedPathsError`.
    """
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        msg = f"{source} is not valid TOML: {exc}"
        raise ProtectedPathsError(msg) from exc
    if set(raw) != {"paths"}:
        keys = ", ".join(sorted(raw)) or "none"
        msg = f"{source} must hold exactly one key, 'paths' (found: {keys})"
        raise ProtectedPathsError(msg)
    paths = raw["paths"]
    if not isinstance(paths, list):
        msg = f"{source}: 'paths' must be a list of globs, not {type(paths).__name__}"
        raise ProtectedPathsError(msg)
    globs: list[str] = []
    for entry in paths:
        if not isinstance(entry, str):
            msg = f"{source}: every entry of 'paths' must be a string, not {entry!r}"
            raise ProtectedPathsError(msg)
        problem = _glob_problem(entry)
        if problem is not None:
            msg = f"{source}: entry {entry!r} is malformed: {problem}"
            raise ProtectedPathsError(msg)
        globs.append(entry)
    return tuple(globs)


def _parse_entry(repo: Path, entry: tuple[str, str, str] | None, source: str) -> tuple[str, ...]:
    """Parse the registry tree *entry* (``None``: absent, so empty); *source* labels errors."""
    if entry is None:
        return ()
    mode, kind, obj = entry
    if kind != "blob" or mode not in _FILE_MODES:
        msg = f"{source} is not a regular file (mode {mode}, {kind})"
        raise ProtectedPathsError(msg)
    try:
        data = _git(repo, "cat-file", "blob", obj)
    except ProtectedPathsError as exc:
        msg = f"cannot read {source}: {exc}"
        raise ProtectedPathsError(msg) from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = f"{source} is not UTF-8: {exc}"
        raise ProtectedPathsError(msg) from exc
    return parse_registry(text, source)


def load_registry(repo: Path, ref: str) -> tuple[str, ...]:
    """Return the globs of the registry committed at *ref* in *repo*.

    An absent registry is ``()``. An unreadable ref, or a registry that is not
    a regular UTF-8 file of well-formed globs, raises
    :class:`ProtectedPathsError` naming the file and *ref*: it is never read
    as empty.
    """
    try:
        commit = _commit(repo, ref)
        entry = _tree_entry(repo, commit, REGISTRY_FILE)
    except ProtectedPathsError as exc:
        msg = f"cannot read {REGISTRY_FILE} at {ref!r}: {exc}"
        raise ProtectedPathsError(msg) from exc
    return _parse_entry(repo, entry, f"{REGISTRY_FILE} at {ref}")


def protected_set(repo: Path, ref: str) -> tuple[str, ...]:
    """The repository's protected set at *ref*: :data:`FLEET_DEFAULT`, then the registry's."""
    registry = load_registry(repo, ref)
    return tuple(dict.fromkeys((*FLEET_DEFAULT, *registry)))


# -- the match ---------------------------------------------------------------------


def _changed_paths(repo: Path, base: str, head: str) -> set[str]:
    """Every path ``<base>...<head>`` changes; a rename counts its old and new path."""
    output = _git(
        repo,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--name-status",
        "-M",
        "-z",
        "--end-of-options",
        f"{base}...{head}",
        "--",
    )
    fields = [field.decode("utf-8", "surrogateescape") for field in output.split(b"\0")]
    if fields and fields[-1] == "":
        fields.pop()
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index]
        letter = status[:1]
        if letter not in _KNOWN_STATUSES:
            msg = f"git diff --name-status {base}...{head}: unexpected status {status!r}"
            raise ProtectedPathsError(msg)
        count = 2 if letter == "R" else 1
        if index + count >= len(fields):
            msg = f"git diff --name-status {base}...{head}: truncated {status} entry"
            raise ProtectedPathsError(msg)
        paths.update(fields[index + 1 : index + 1 + count])
        index += 1 + count
    return paths


def _head_blob(repo: Path, head: str, path: str) -> str:
    entry = _tree_entry(repo, head, path)
    if entry is None or entry[1] == "tree":
        return DELETED
    return entry[2]


def changed_protected(repo: Path, base: str, head: str, registry_ref: str) -> list[ProtectedChange]:
    """The protected paths ``<base>...<head>`` changes, sorted by path, with their head blobs.

    The changed files are those ``git diff --name-status -M <base>...<head>``
    reports, from the merge base, so a commit added to *base* after the fork is
    not the branch's change; a rename counts both its old and its new path.
    They are matched against :func:`protected_set` at *registry_ref*, never at
    *head*. Each match carries the object at *head*, or :data:`DELETED` when
    the head has no file there. Raises :class:`ProtectedPathsError` on any
    unreadable ref, registry or diff.
    """
    globs = protected_set(repo, registry_ref)
    base_commit = _commit(repo, base)
    head_commit = _commit(repo, head)
    changed = _changed_paths(repo, base_commit, head_commit)
    return [
        ProtectedChange(path, _head_blob(repo, head_commit, path))
        for path in sorted(changed)
        if any(matches(glob, path) for glob in globs)
    ]
