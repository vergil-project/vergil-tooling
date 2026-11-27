"""The arming refs, ``refs/vergil/release-lock-arming/<pr>``.

Spec §5.4 of epic vergil-project/.github#1664. The arming side of the
handshake between arming a PR and taking the lock: a caller about to arm a PR writes the PR's arming ref by a create-only push,
then reads the lock; the release writes the lock, then waits until every
arming ref is gone. Each ref is a one-file commit carrying a random nonce, so
a renewal always changes its value: the release measures an arming ref's
expiry from its own first sighting of a value, never from a time the ref
carries. Arming refs are read with no trusted-author check (§5.10).
"""

from __future__ import annotations

import json
import re

from vergil_tooling.lib.release_lock import refs
from vergil_tooling.lib.release_lock.model import RefReadError, RefWriteError, new_token

ARMING_FILE = "release-lock-arming.json"
_MESSAGE = "vergil: release-lock arming"
#: A PR number as an arming ref names it: decimal, no leading zero.
_PR = re.compile(r"[1-9][0-9]*")


def arming_ref(pr: int) -> str:
    """The arming ref of PR *pr*."""
    if pr < 1:
        msg = f"not a PR number: {pr}"
        raise ValueError(msg)
    return f"{refs.ARMING_PREFIX}{pr}"


def _write(repo: str, pr: int, expected: str | None) -> str | None:
    ref = arming_ref(pr)
    with refs.scratch(repo, RefWriteError) as where:
        text = json.dumps({"nonce": new_token(), "pr": pr}, sort_keys=True) + "\n"
        sha = refs.build(where, text, ARMING_FILE, _MESSAGE)
        return sha if refs.write_ref(where, ref, new=sha, expected=expected) else None


def create(repo: str, pr: int) -> str | None:
    """Write PR *pr*'s arming ref by a create-only push: its sha, or ``None`` if one exists."""
    return _write(repo, pr, None)


def renew(repo: str, pr: int, *, expected: str) -> str | None:
    """Renew PR *pr*'s arming ref at *expected* to a new value: its sha, or ``None`` on mismatch."""
    return _write(repo, pr, expected)


def delete(repo: str, pr: int, *, expected: str) -> bool:
    """Delete PR *pr*'s arming ref if it is at *expected*: True once gone, False on a mismatch."""
    ref = arming_ref(pr)
    with refs.scratch(repo, RefWriteError) as where:
        return refs.write_ref(where, ref, new=None, expected=expected)


def list_arming(repo: str) -> dict[int, str]:
    """Every arming ref on *repo*, PR number to sha, whoever wrote it.

    A ref under the arming namespace that does not name a PR raises
    :class:`RefReadError`, naming it: the release waits on every arming ref,
    so one it cannot account for is never skipped.
    """
    with refs.scratch(repo, RefReadError) as where:
        found = refs.list_remote(where, f"{refs.ARMING_PREFIX}*")
    armed: dict[int, str] = {}
    for ref, sha in found.items():
        if not ref.startswith(refs.ARMING_PREFIX):
            continue
        suffix = ref.removeprefix(refs.ARMING_PREFIX)
        if not _PR.fullmatch(suffix):
            msg = f"the arming ref {ref} does not name a PR"
            raise RefReadError(msg)
        armed[int(suffix)] = sha
    return armed
