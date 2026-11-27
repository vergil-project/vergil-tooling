"""The release lock: its record, the lock ref, its history and the arming refs.

Epic vergil-project/.github#1664. All lock state lives in refs on the repo's
origin, written only by this package through ``lib.git`` (``vrg-git`` refuses
every push under ``refs/vergil/release-lock``):

- :mod:`.model`: the timing constants, the :class:`LockRecord` and its JSON.
- :mod:`.refs`: the lock ref, ``refs/vergil/release-lock``.
- :mod:`.history`: the lock history, ``refs/vergil/release-lock-history``.
- :mod:`.arming`: the arming refs, ``refs/vergil/release-lock-arming/<pr>``.
- :mod:`.stale`: whether a held lock is stale (spec §5.6).
- :mod:`.status`: the lock as ``vrg-release-lock status`` and
  ``vrg-repo-status`` show it, with the PRs it holds unarmed.
- :mod:`.sweeps`: the quiesce (the arming-ref wait and the disarm sweep,
  the only code that turns auto-merge off) and the re-arm sweep.
- :mod:`.reclaim`: reclaiming a stale lock, and ``vrg-release-lock break``.

A transport failure raises a :class:`ReleaseLockError` (:class:`RefWriteError`
or :class:`RefReadError`); a lease mismatch is a ``None`` or ``False`` return.
"""

from __future__ import annotations

from vergil_tooling.lib.release_lock.model import (
    LockParseError,
    RefReadError,
    RefWriteError,
    ReleaseLockError,
)

__all__ = ["LockParseError", "RefReadError", "RefWriteError", "ReleaseLockError"]
