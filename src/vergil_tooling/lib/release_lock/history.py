"""The lock history, ``refs/vergil/release-lock-history`` (spec §5.2, epic vergil-project/.github#1664).

One commit per break, reclaim, reclaim re-arm, supersede takeover or retake,
each a one-file commit whose parent is the entry before it. An entry is
appended by compare-and-swap against the tip it was built on, and rebuilt on
the new tip when another append won; like the lock, the history is read with
no trusted-author check.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from vergil_tooling.lib import git
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_lock import refs
from vergil_tooling.lib.release_lock.model import RefReadError, RefWriteError

if TYPE_CHECKING:
    from pathlib import Path

HISTORY_FILE = "release-lock-history.json"
#: How many times an append is rebuilt on a tip another append moved.
_APPEND_ATTEMPTS = 5

Action = Literal["break", "reclaim", "reclaim-rearm", "takeover", "retake"]
_ACTIONS: tuple[str, ...] = get_args(Action)
_TEXT_FIELDS = ("action", "old", "actor", "at", "reason")


@dataclass(frozen=True)
class HistoryEntry:
    """One override of the lock: what was done to which lock, by whom, when and why.

    *old* is the old lock's file text as read, even when it did not parse. A
    ``reclaim-rearm`` entry's *rearmed* lists the PRs its sweep armed, and its
    *reason* names a failed sweep's error (empty when the sweep succeeded).
    """

    action: Action
    old: str
    actor: str
    at: str
    reason: str
    rearmed: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            msg = f"action is {self.action!r}, not one of {', '.join(_ACTIONS)}"
            raise ValueError(msg)


def entry_json(entry: HistoryEntry) -> str:
    """The history file's text for *entry*."""
    data = {name: getattr(entry, name) for name in _TEXT_FIELDS}
    return json.dumps({**data, "rearmed": list(entry.rearmed)}, indent=2, sort_keys=True) + "\n"


def _parse(text: str, sha: str) -> HistoryEntry:
    what = f"{refs.HISTORY_REF} entry {sha}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{what} is not valid JSON: {exc}"
        raise RefReadError(msg) from None
    if not isinstance(data, dict) or set(data) != {*_TEXT_FIELDS, "rearmed"}:
        msg = f"{what} is not a history entry"
        raise RefReadError(msg)
    rearmed = data["rearmed"]
    texts = [data[name] for name in _TEXT_FIELDS]
    if not isinstance(rearmed, list) or not all(isinstance(v, str) for v in [*texts, *rearmed]):
        msg = f"{what} has a field of the wrong type"
        raise RefReadError(msg)
    try:
        return HistoryEntry(*texts, rearmed=tuple(rearmed))
    except ValueError as exc:
        msg = f"{what} is not valid: {exc}"
        raise RefReadError(msg) from None


def read_history(repo: str) -> list[HistoryEntry]:
    """Every entry in *repo*'s lock history, oldest first; :class:`RefReadError` on any failure."""
    with refs.scratch(repo, RefReadError) as where:
        if refs.remote_sha(where, refs.HISTORY_REF) is None:
            return []
        tip = refs.fetch(where, refs.HISTORY_REF)
        try:
            shas = git.read_output("-C", str(where), "rev-list", "--reverse", tip).split()
        except subprocess.CalledProcessError as exc:
            msg = f"listing {refs.HISTORY_REF} failed: {error_text(exc)}"
            raise RefReadError(msg) from exc
        return [_parse(refs.read_file(where, sha, HISTORY_FILE), sha) for sha in shas]


def _tip(where: Path) -> str | None:
    """The history's tip on origin, fetched with its history; ``None`` when it has none."""
    try:
        if refs.remote_sha(where, refs.HISTORY_REF) is None:
            return None
        return refs.fetch(where, refs.HISTORY_REF)
    except RefReadError as exc:
        msg = f"appending to {refs.HISTORY_REF}: {exc}"
        raise RefWriteError(msg) from exc


def _buried(where: Path, built: str) -> bool:
    """Whether *built* landed, under entries appended since, in the history now on origin."""
    tip = _tip(where)
    if tip is None:
        return False
    try:
        return git.is_ancestor(built, tip, repo=where)
    except subprocess.CalledProcessError as exc:
        msg = f"appending to {refs.HISTORY_REF}: checking {built} against the tip failed: {error_text(exc)}"
        raise RefWriteError(msg) from exc


def append(repo: str, entry: HistoryEntry) -> None:
    """Append *entry* to *repo*'s lock history by compare-and-swap.

    Rebuilt on the new tip when another append won, up to a bound past which
    it raises :class:`RefWriteError`. A failed push whose entry landed (alone
    or under entries appended since) is a success, never a second copy.
    """
    with refs.scratch(repo, RefWriteError) as where:
        message = f"vergil: release-lock {entry.action}"
        for _attempt in range(_APPEND_ATTEMPTS):
            tip = _tip(where)
            built = refs.build(where, entry_json(entry), HISTORY_FILE, message, parent=tip)
            if refs.write_ref(where, refs.HISTORY_REF, new=built, expected=tip):
                return
            if _buried(where, built):
                return
        msg = f"appending to {refs.HISTORY_REF} lost to a concurrent append {_APPEND_ATTEMPTS} times"
        raise RefWriteError(msg)
