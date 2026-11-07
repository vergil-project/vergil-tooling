"""The merge-review schemas' names, value sets, steps and finding ID rules.

Shared by the request builder and the result check, so the two cannot drift.
Every value here is fixed by ``docs/specs/merge-review-protocol.md``.
"""

from __future__ import annotations

import re

REQUEST_SCHEMA = "merge-review-request/1"
RESULT_SCHEMA = "merge-review-result/1"

#: The info string of the fenced block that opens a reviewer's reply.
RESULT_BLOCK = "merge-review-result"

QUESTIONS = ("production-safety", "merge-quality")
KINDS = ("defect", "design-question", "out-of-scope", "pre-existing")
FIX_STATUSES = ("fixed", "not-fixed")
CHANGE_CLASSES = ("docs-only", "code")

#: A step: the review, the first check, or a further check ``check-2`` to ``check-9``.
STEP_RE = re.compile(r"^(review|check|check-[2-9])$")

#: A request ID: 32 lowercase hex characters, from ``secrets.token_hex(16)``.
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: The longest finding ID epic 889's item ID rule allows.
FINDING_ID_MAX = 16

#: A finding's number ``<n>``: 1 to 999, with no leading zero.
_FINDING_NUMBER = r"[1-9][0-9]{0,2}"

#: A finding summary's longest length, so it fits a ``vrg-ruling`` item unchanged.
SUMMARY_MAX = 300

#: The result block's top-level fields. ``fix_status`` is a check's only.
RESULT_FIELDS = frozenset({"schema", "step", "request_id", "findings"})
CHECK_RESULT_FIELDS = RESULT_FIELDS | {"fix_status"}

#: A finding's fields. ``summary`` is required unless ``security`` is true, and
#: forbidden when it is.
FINDING_FIELDS = frozenset({"id", "question", "kind", "security", "location"})
FINDING_OPTIONAL_FIELDS = frozenset({"summary"})

#: A ``fix_status`` entry's fields.
FIX_STATUS_FIELDS = frozenset({"id", "status"})


def is_check(step: str) -> bool:
    """Whether ``step`` is a check (``check`` or ``check-<k>``), not the review."""
    return step != "review"


def step_id_prefix(step: str) -> str:
    """The finding ID prefix of ``step``: ``R``, ``C`` or ``C<k>.``.

    Raises :class:`ValueError` for anything that is not a step.
    """
    if STEP_RE.fullmatch(step) is None:
        msg = "not a merge-review step"
        raise ValueError(msg)
    if step == "review":
        return "R"
    if step == "check":
        return "C"
    return f"C{step.removeprefix('check-')}."


def finding_id_re(step: str) -> re.Pattern[str]:
    """The pattern a finding ID of ``step`` matches: its prefix and ``<n>``."""
    return re.compile(re.escape(step_id_prefix(step)) + _FINDING_NUMBER)


def earlier_steps(step: str) -> tuple[str, ...]:
    """Every step before ``step``, in order: none for the review."""
    order = ("review", "check", *(f"check-{k}" for k in range(2, 10)))
    step_id_prefix(step)  # refuses a non-step
    return order[: order.index(step)]
