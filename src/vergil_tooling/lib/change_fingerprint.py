"""The change fingerprint: a rebase-stable hash of a branch's own change.

A merge-review record, and a merge-review exemption, name the change they
cover by its fingerprint, not only by the head commit, so a clean rebase
keeps them while any change to the branch's own lines voids them
(epic vergil-project/.github#1384, spec §4.6).

The fingerprint is the SHA-256 of a normalised zero-context diff
(``git diff -U0 -M``) from the merge base to the head:

* the ``diff --git``, ``---`` and ``+++`` file headers, the mode, rename and
  copy lines, ``\\ No newline at end of file``, and every ``+`` and ``-``
  line are kept byte for byte, whitespace included;
* the ``@@`` hunk headers (line numbers) and the ``index``, ``similarity
  index`` and ``dissimilarity index`` lines are dropped, since their values
  change whenever ``develop`` edits the file elsewhere;
* each binary file's ``Binary files … differ`` line becomes
  ``binary <path> <head blob>`` (``deleted`` for a deletion), so a binary
  counts by its content.

``git patch-id`` is not used: it ignores whitespace and sees a binary only as
"differ". Every diff option the output depends on is pinned on the command
line, and attributes are read from the head commit, so the same change has
the same fingerprint in any clone, under any git configuration. Every
failure raises :class:`FingerprintError`; nothing yields a fingerprint by
default.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class FingerprintError(Exception):
    """A fingerprint could not be computed: a git failure or an empty change."""


#: Every diff option the normalised output depends on, pinned so that no
#: configuration (``diff.*``, ``color.*``, ``core.quotePath``, ``core.
#: bigFileThreshold``, …) can change it between clones.
_CONFIG: tuple[str, ...] = (
    "-c",
    "core.quotePath=true",
    "-c",
    "core.bigFileThreshold=512m",
    "-c",
    "core.attributesFile=",
    "-c",
    "diff.renameLimit=1000",
)
_DIFF_OPTIONS: tuple[str, ...] = (
    "-M",
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--inter-hunk-context=0",
    "--ignore-submodules=none",
    "--submodule=short",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "-O/dev/null",
)

#: Environment variables that change ``git diff`` output outside its options.
_SCRUBBED_ENV: tuple[str, ...] = (
    "GIT_DIFF_OPTS",
    "GIT_EXTERNAL_DIFF",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)

_DROPPED_PREFIXES: tuple[bytes, ...] = (
    b"@@",
    b"index ",
    b"similarity index ",
    b"dissimilarity index ",
)
_SECTION = b"diff --git "
_BINARY = b"Binary files "
_NULL_BLOB = "0" * 40


def _git(repo: Path, *args: str, attr_source: str | None = None) -> bytes:
    """Run ``git *args`` in *repo* and return its stdout; raise on any failure."""
    env = {key: value for key, value in os.environ.items() if key not in _SCRUBBED_ENV}
    if attr_source is not None:
        env["GIT_ATTR_SOURCE"] = attr_source
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), *_CONFIG, *args],  # noqa: S607
            capture_output=True,
            check=False,
            env=env,
        )
    except OSError as exc:
        msg = f"git {args[0]} could not run in {repo}: {exc}"
        raise FingerprintError(msg) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        msg = f"git {' '.join(args)} failed in {repo} (exit {result.returncode}): {stderr}"
        raise FingerprintError(msg)
    return result.stdout


def _commit(repo: Path, ref: str) -> str:
    """The full commit SHA *ref* names."""
    return (
        _git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        .decode()
        .strip()
    )


def _raw_entries(repo: Path, left: str, right: str) -> list[tuple[bytes, str]]:
    """``(path, head blob)`` for each changed file, in the patch's file order.

    The path is the destination path (the source path of a deletion) and the
    blob is ``deleted`` for a deletion. A type change (status ``T``: a file
    becoming a symlink or a gitlink, or back) is one raw record but two patch
    sections, a deletion then a creation, so it gives two entries: the path
    with ``deleted``, then the path with its new blob.
    """
    out = _git(
        repo, "diff", "--raw", "-z", "--no-abbrev", *_DIFF_OPTIONS, left, right, attr_source=right
    )
    fields = out.removesuffix(b"\0").split(b"\0") if out else []
    entries: list[tuple[bytes, str]] = []
    index = 0
    while index < len(fields):
        meta = fields[index].decode()
        parts = meta.split()
        if not meta.startswith(":") or len(parts) != 5:  # noqa: PLR2004
            msg = f"unexpected git diff --raw record {meta!r} for {left}..{right}"
            raise FingerprintError(msg)
        new_blob, status = parts[3], parts[4]
        count = 2 if status[:1] in {"R", "C"} else 1
        paths = fields[index + 1 : index + 1 + count]
        if len(paths) != count:
            msg = f"truncated git diff --raw record {meta!r} for {left}..{right}"
            raise FingerprintError(msg)
        if status == "T":
            entries.append((paths[-1], "deleted"))
        entries.append((paths[-1], "deleted" if new_blob == _NULL_BLOB else new_blob))
        index += 1 + count
    return entries


def _sections(patch: bytes) -> list[list[bytes]]:
    """Split a patch into one list of lines per file, each starting at ``diff --git``."""
    lines = patch.removesuffix(b"\n").split(b"\n") if patch else []
    sections: list[list[bytes]] = []
    for line in lines:
        if line.startswith(_SECTION):
            sections.append([line])
        elif sections:
            sections[-1].append(line)
        else:
            msg = f"unexpected line before the first file header: {line[:80]!r}"
            raise FingerprintError(msg)
    return sections


def _fingerprint(repo: Path, left: str, right: str) -> str:
    """The fingerprint of the diff from commit *left* to commit *right*."""
    patch = _git(repo, "diff", "-U0", *_DIFF_OPTIONS, left, right, attr_source=right)
    sections = _sections(patch)
    if not sections:
        msg = f"the change from {left} to {right} is empty: there is nothing to fingerprint"
        raise FingerprintError(msg)
    entries = _raw_entries(repo, left, right)
    if len(entries) != len(sections):
        msg = (
            f"git diff reported {len(sections)} file(s) in its patch but {len(entries)} "
            f"in its raw listing for {left}..{right}"
        )
        raise FingerprintError(msg)
    digest = hashlib.sha256()
    for section, (path, blob) in zip(sections, entries, strict=True):
        for line in section:
            if line.startswith(_DROPPED_PREFIXES):
                continue
            if line.startswith(_BINARY):
                line = b"binary " + path + b" " + blob.encode()  # noqa: PLW2901
            digest.update(line + b"\n")
    return digest.hexdigest()


def change_fingerprint(repo: Path, base: str, head: str) -> str:
    """The fingerprint of *head*'s change since its merge base with *base*.

    The same change as ``git diff -U0 -M <base>...<head>``. Raises
    :class:`FingerprintError` when either ref does not name a commit, the
    refs have no merge base, a git command fails, or the change is empty.
    """
    head_sha = _commit(repo, head)
    merge_base = _git(repo, "merge-base", _commit(repo, base), head_sha).decode().strip()
    return _fingerprint(repo, merge_base, head_sha)


def commit_fingerprint(repo: Path, commit: str) -> str:
    """The fingerprint of *commit*'s own change: its diff against its first parent.

    For one commit of a batch PR. Raises :class:`FingerprintError` as
    :func:`change_fingerprint` does, and for a root commit, which has no
    parent.
    """
    commit_sha = _commit(repo, commit)
    return _fingerprint(repo, _commit(repo, f"{commit_sha}^"), commit_sha)
