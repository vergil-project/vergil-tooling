"""The hold rule: which failed stages hold a later vrg-release stage back.

No stage is optional, and a deferred publish holds back only what needs it
(spec §4.4 of epic vergil-project/.github#1440):

- ``promote``, ``cd-publish`` and ``finalize`` need only the stages through
  ``confirm-develop``. ``cd-publish`` delivers the CD publish jobs the
  confirm stages record, so it waits for ``confirm-develop``: run past a
  failed one, it would find nothing recorded, be recorded done, and never
  deliver a job a resumed ``confirm-develop`` records;
- ``install`` needs those and ``promote``;
- ``release-records``, ``unlock``, ``rearm`` and ``close-tracking`` need every
  stage before them, so any failure leaves the release lock held until
  ``vrg-release --resume`` finishes the release (spec §5.3 step 2 of epic
  vergil-project/.github#1664);
- ``cd-publish``, ``package-index`` and ``update-deps`` hold back none of
  ``promote``, ``finalize`` and ``install``, and nothing holds
  ``package-index`` or ``update-deps`` back.

A held stage does not run and is not recorded done, so ``vrg-release
--resume`` runs it once the failed stage has succeeded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# The stages through confirm-develop, in pipeline order: the tag, the GitHub
# Release, the back-merge and develop's CD. The orchestrator's tests pin it to
# the head of the stage list.
THROUGH_CONFIRM_DEVELOP = (
    "audit",
    "preflight",
    "prepare",
    "merge-release",
    "confirm-main",
    "back-merge-bump",
    "teardown-worktree",
    "confirm-develop",
)
_NEEDS: dict[str, frozenset[str]] = {
    "promote": frozenset(THROUGH_CONFIRM_DEVELOP),
    "cd-publish": frozenset(THROUGH_CONFIRM_DEVELOP),
    "finalize": frozenset(THROUGH_CONFIRM_DEVELOP),
    "install": frozenset({*THROUGH_CONFIRM_DEVELOP, "promote"}),
}
# The stages that need every stage before them.
_NEEDS_ALL = frozenset({"release-records", "unlock", "rearm", "close-tracking"})


def holds(stage: str, failed: Sequence[str]) -> list[str]:
    """The stages in *failed* that hold *stage* back, in *failed*'s order.

    Empty when *stage* may run: every stage the rule does not name, and any
    stage none of whose needed stages failed.
    """
    if stage in _NEEDS_ALL:
        return list(failed)
    needs = _NEEDS.get(stage, frozenset())
    return [name for name in failed if name in needs]
