"""The lock ref, ``refs/vergil/release-lock``, and the transport every lock ref shares.

Spec §5.2 and §5.10 of epic vergil-project/.github#1664. The lock is a
one-file commit, built as the relay records are
(:func:`github_transport.one_file_commit`), on the repo's origin:

- **Taking it** is a create-only push, ``--force-with-lease=<ref>:``.
- **Updating or deleting it** is a compare-and-swap push,
  ``--force-with-lease=<ref>:<expected>``.
- **Reading it** takes the lock as written, with no trusted-author check: a
  ref records no pusher, and a commit's author is whatever its writer set.
  A lock that is present but cannot be read or parsed is **held**, never
  absent.

Every operation runs in its own scratch bare repository whose ``origin`` is
the repo, so it needs no clone and never touches the caller's HEAD, index,
working tree or refs. The pushes go through ``lib.git``, which retries a
transient failure; a push whose first attempt landed but whose response was
lost then comes back as a lease rejection. So :func:`write_ref` reconciles
every failed push by re-reading the ref before it reports: the write's own
sha (or, for a delete, an absent ref) is a success, a value other than the
expected one is a mismatch, and a ref still at the expected value means the
push failed outright (:class:`RefWriteError`).
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.develop_clone import clone_url
from vergil_tooling.lib.pr_workflow.github_transport import one_file_commit
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock.model import (
    LockParseError,
    LockRecord,
    RefReadError,
    RefWriteError,
    ReleaseLockError,
    from_json,
    to_json,
)

if TYPE_CHECKING:
    from collections.abc import Generator

LOCK_REF = "refs/vergil/release-lock"
HISTORY_REF = "refs/vergil/release-lock-history"
ARMING_PREFIX = "refs/vergil/release-lock-arming/"
LOCK_FILE = "release-lock.json"
_LOCK_MESSAGE = "vergil: release lock"

#: Where a fetched ref lands in the scratch repository.
_FETCHED = "refs/vrg-fetched/ref"
#: The identity the scratch repository commits as; nothing reads it (§5.10).
_IDENTITY = (("user.name", "vergil release lock"), ("user.email", "release-lock@vergil.invalid"))
_REPO = re.compile(r"[\w.-]+/[\w.-]+")
#: A full SHA-1 or SHA-256 object name that is not all zeros.
_SHA = re.compile(r"(?!0+$)(?:[0-9a-f]{40}|[0-9a-f]{64})")

type _Failure = type[ReleaseLockError]


def origin_url(repo: str) -> str:
    """The URL the lock refs of *repo* (``<owner>/<repo>``) are read from and pushed to."""
    return clone_url(repo)


@contextlib.contextmanager
def scratch(repo: str, error: _Failure) -> Generator[Path]:
    """A throwaway bare repository whose ``origin`` is *repo*; *error* if it cannot be made."""
    if not _REPO.fullmatch(repo):
        msg = f"not an <owner>/<repo>: {repo!r}"
        raise ValueError(msg)
    with tempfile.TemporaryDirectory(prefix="vrg-release-lock-") as tmp:
        where = Path(tmp)
        try:
            git.read_output("init", "--quiet", "--bare", str(where))
            git.read_output("-C", tmp, "remote", "add", "origin", origin_url(repo))
            for key, value in (*_IDENTITY, ("commit.gpgSign", "false")):
                git.read_output("-C", tmp, "config", key, value)
        except (subprocess.CalledProcessError, OSError) as exc:
            msg = f"could not make a scratch repository for {repo}'s lock refs: {error_text(exc)}"
            raise error(msg) from exc
        yield where


def list_remote(where: Path, pattern: str) -> dict[str, str]:
    """Each ref on ``origin`` matching *pattern*, mapped to its sha.

    Raises :class:`RefReadError` when the listing fails.
    """
    try:
        listing = git.read_output("-C", str(where), "ls-remote", "origin", pattern)
    except subprocess.CalledProcessError as exc:
        msg = f"reading {pattern} from origin failed: {error_text(exc)}"
        raise RefReadError(msg) from exc
    found: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _tab, ref = line.partition("\t")
        found[ref] = sha
    return found


def remote_sha(where: Path, ref: str) -> str | None:
    """The sha *ref* has on ``origin``, ``None`` when absent.

    ``ls-remote`` matches a pattern by its tail, so only the line whose ref is
    exactly *ref* is read.
    """
    return list_remote(where, ref).get(ref)


def fetch(where: Path, ref: str) -> str:
    """Fetch *ref* (with its history) into the scratch repository and return the sha fetched."""
    try:
        git.read_output(
            "-C", str(where), "fetch", "--quiet", "--no-tags", "origin", f"+{ref}:{_FETCHED}"
        )
        return git.read_output("-C", str(where), "rev-parse", "--verify", _FETCHED)
    except subprocess.CalledProcessError as exc:
        msg = f"fetching {ref} from origin failed: {error_text(exc)}"
        raise RefReadError(msg) from exc


def read_file(where: Path, sha: str, filename: str) -> str:
    """The text of *filename* in commit *sha*; :class:`RefReadError` when it cannot be read."""
    try:
        return git.read_output("-C", str(where), "cat-file", "blob", f"{sha}:{filename}")
    except subprocess.CalledProcessError as exc:
        msg = f"{sha} has no readable {filename}: {error_text(exc)}"
        raise RefReadError(msg) from exc
    except UnicodeDecodeError:
        msg = f"{filename} in {sha} is not UTF-8 text"
        raise RefReadError(msg) from None


def build(where: Path, text: str, filename: str, message: str, *, parent: str | None = None) -> str:
    """Build the one-file commit a write pushes; :class:`RefWriteError` when it cannot."""
    try:
        return one_file_commit(text, filename, message, parent=parent, repo=where)
    except subprocess.CalledProcessError as exc:
        msg = f"building the {filename} commit failed: {error_text(exc)}"
        raise RefWriteError(msg) from exc


def write_ref(where: Path, ref: str, *, new: str | None, expected: str | None) -> bool:
    """Set *ref* on ``origin`` to *new* (``None`` deletes it) if it is at *expected*.

    *expected* ``None`` is a create-only write: the ref must be absent. True
    when the ref ends at *new*, False on a lease mismatch. A failed push is
    reconciled by re-reading the ref (see the module docstring); a push that
    failed with the ref unchanged, or a re-read that fails, raises
    :class:`RefWriteError`.
    """
    if expected is not None and not _SHA.fullmatch(expected):
        # git reads an all-zero lease as "must be absent", so it is never let through.
        msg = f"the expected value of {ref} is not a commit sha: {expected!r}"
        raise ValueError(msg)
    lease = f"--force-with-lease={ref}:{expected or ''}"
    refspec = f"{new}:{ref}" if new is not None else f":{ref}"
    try:
        git.read_output("-C", str(where), "push", "--no-verify", lease, "origin", refspec)
    except subprocess.CalledProcessError as exc:
        failure = error_text(exc)
    else:
        return True
    try:
        now = remote_sha(where, ref)
    except RefReadError as exc:
        msg = f"pushing {ref} failed ({failure}), and re-reading it to reconcile failed: {exc}"
        raise RefWriteError(msg) from exc
    if now == new:
        return True
    if now == expected:
        msg = f"pushing {ref} failed: {failure}"
        raise RefWriteError(msg)
    return False


@dataclass(frozen=True)
class LockState:
    """The lock as read: absent, held and readable, or held and unreadable."""

    sha: str | None  # None when the ref is absent
    record: LockRecord | None  # None when absent or unreadable
    error: str  # non-empty when present but unreadable

    @property
    def held(self) -> bool:
        """Whether the lock is held: present, readable or not."""
        return self.sha is not None


def read_lock(repo: str) -> LockState:
    """Read *repo*'s lock, with no trusted-author check.

    A lock that is present but cannot be fetched, read or parsed is held:
    ``sha`` set, ``record`` ``None`` and ``error`` naming why. When whether
    the lock is present cannot be told, :class:`RefReadError` is raised.
    """
    with scratch(repo, RefReadError) as where:
        sha = remote_sha(where, LOCK_REF)
        if sha is None:
            return LockState(sha=None, record=None, error="")
        try:
            sha = fetch(where, LOCK_REF)
            return LockState(sha=sha, record=from_json(read_file(where, sha, LOCK_FILE)), error="")
        except (RefReadError, LockParseError) as exc:
            return LockState(sha=sha, record=None, error=f"{LOCK_REF} is unreadable: {exc}")


def _write_lock(repo: str, rec: LockRecord, expected: str | None) -> str | None:
    with scratch(repo, RefWriteError) as where:
        sha = build(where, to_json(rec), LOCK_FILE, _LOCK_MESSAGE)
        return sha if write_ref(where, LOCK_REF, new=sha, expected=expected) else None


def take_lock(repo: str, rec: LockRecord) -> str | None:
    """Take *repo*'s lock with a create-only push: the new sha, or ``None`` if it is held."""
    return _write_lock(repo, rec, None)


def update_lock(repo: str, rec: LockRecord, *, expected: str) -> str | None:
    """Replace the lock at *expected* with *rec*: the new sha, or ``None`` on a mismatch."""
    return _write_lock(repo, rec, expected)


def delete_lock(repo: str, *, expected: str) -> bool:
    """Delete the lock if it is at *expected*: True once it is gone, False on a mismatch."""
    with scratch(repo, RefWriteError) as where:
        return write_ref(where, LOCK_REF, new=None, expected=expected)
