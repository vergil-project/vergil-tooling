"""The epic-size scale, the sizing comment and an item's size record.

A planning or deferred item carries an **estimated epic size**, recorded twice:
an ``epic-size:<S|M|L|XL>`` label, which issue lists and filters read, and a
dated **sizing comment** with the task count, a confidence and a one-line
reason. **The task count is the estimate; the size is derived from it**
(:func:`size_for_tasks`): S 1–9, M 10–19, L 20–39, XL 40+. No caller states
both.

The sizing comment, as :func:`render_sizing_comment` writes it::

    Sized 2026-11-16: epic-size M, about 14 tasks.
    Epic-size: M
    Estimated-tasks: 14
    Confidence: med
    Reason: <one sentence>

Only comments by **trusted authors** (:func:`trusted_logins`, compared after
stripping a leading ``app/``) count. The latest trusted sizing comment after
the latest trusted **cleared marker** (``Size cleared YYYY-MM-DD: …``, posted
when an item leaves as a one-off) is the current estimate. :func:`size_record`
checks it against the item's labels and reports any disagreement or malformed
comment as an error, never resolving it to a size.

This module owns every constant the size commands and views share: the band
edges, the size order (:data:`SIZES`), the label prefix and the comment and
marker formats. It reads nothing from GitHub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, get_args

from vergil_tooling.lib.trusted_authors import bare_login, load_trusted_authors

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

Size = Literal["S", "M", "L", "XL"]
Confidence = Literal["high", "med", "low"]

# The size order, smallest first; the order the queue view sorts by.
SIZES: tuple[Size, ...] = get_args(Size)
CONFIDENCES: tuple[Confidence, ...] = get_args(Confidence)

LABEL_PREFIX = "epic-size:"

# The smallest task count of each size above S, largest first; S starts at 1.
_BAND_FLOORS: tuple[tuple[int, Size], ...] = ((40, "XL"), (20, "L"), (10, "M"))

SIZING_PREFIX = "Sized"
CLEARED_PREFIX = "Size cleared"
CLEARED_TEXT = "moved out of planning as a one-off."

EPIC_SIZE_KEY = "Epic-size:"
ESTIMATED_TASKS_KEY = "Estimated-tasks:"
CONFIDENCE_KEY = "Confidence:"
REASON_KEY = "Reason:"
_KEYS = (EPIC_SIZE_KEY, ESTIMATED_TASKS_KEY, CONFIDENCE_KEY, REASON_KEY)

_DAY = r"\d{4}-\d{2}-\d{2}"
_SIZING_RE = re.compile(rf"^{re.escape(SIZING_PREFIX)} {_DAY}:")
_CLEARED_RE = re.compile(rf"^{re.escape(CLEARED_PREFIX)} {_DAY}:")
_TASKS_RE = re.compile(r"^[1-9][0-9]*$")


def label_for(size: Size) -> str:
    """The label that carries *size*: ``epic-size:<size>``."""
    return f"{LABEL_PREFIX}{size}"


def size_for_tasks(tasks: int) -> Size:
    """The size an estimate of *tasks* tasks derives; raises ValueError below 1."""
    if tasks < 1:
        msg = f"an estimated task count must be a positive integer, got {tasks}"
        raise ValueError(msg)
    for floor, size in _BAND_FLOORS:
        if tasks >= floor:
            return size
    return "S"


@dataclass(frozen=True)
class Sizing:
    """One sizing: the estimated task count, a confidence and a one-line reason."""

    tasks: int
    confidence: Confidence
    reason: str

    def __post_init__(self) -> None:
        if isinstance(self.tasks, bool) or not isinstance(self.tasks, int) or self.tasks < 1:
            msg = f"an estimated task count must be a positive integer, got {self.tasks!r}"
            raise ValueError(msg)
        if self.confidence not in CONFIDENCES:
            msg = f"confidence must be one of {', '.join(CONFIDENCES)}, got {self.confidence!r}"
            raise ValueError(msg)
        if not self.reason.strip():
            msg = "a sizing reason must not be empty"
            raise ValueError(msg)
        if "\n" in self.reason or "\r" in self.reason:
            msg = "a sizing reason must be one line"
            raise ValueError(msg)

    @property
    def size(self) -> Size:
        return size_for_tasks(self.tasks)


def render_sizing_comment(sizing: Sizing, day: date) -> str:
    """The sizing comment for *sizing*, dated *day*."""
    size = sizing.size
    return "\n".join(
        [
            f"{SIZING_PREFIX} {day.isoformat()}: epic-size {size}, about {sizing.tasks} tasks.",
            f"{EPIC_SIZE_KEY} {size}",
            f"{ESTIMATED_TASKS_KEY} {sizing.tasks}",
            f"{CONFIDENCE_KEY} {sizing.confidence}",
            f"{REASON_KEY} {sizing.reason.strip()}",
        ]
    )


def render_cleared_marker(day: date) -> str:
    """The cleared marker a one-off posts, dated *day*."""
    return f"{CLEARED_PREFIX} {day.isoformat()}: {CLEARED_TEXT}"


@dataclass(frozen=True)
class SizeRecord:
    """An item's size as its labels and trusted sizing comments give it.

    Unsized: every field ``None``. Sized: ``size``, ``tasks`` and
    ``confidence`` set, ``error`` ``None``. In error: only ``error`` set.
    """

    size: Size | None
    tasks: int | None
    confidence: Confidence | None
    error: str | None

    @property
    def display(self) -> str:
        """``M/14`` when sized, ``?`` when unsized, ``!`` when in error."""
        if self.error is not None:
            return "!"
        if self.size is None:
            return "?"
        return f"{self.size}/{self.tasks}"


_UNSIZED = SizeRecord(size=None, tasks=None, confidence=None, error=None)


def _error(message: str) -> SizeRecord:
    return SizeRecord(size=None, tasks=None, confidence=None, error=message)


def trusted_logins() -> frozenset[str]:
    """Every configured trusted login, human and agents; the loader's errors propagate."""
    authors = load_trusted_authors()
    return frozenset(authors.human + authors.agents)


def _first_line(body: str) -> str:
    lines = body.strip().splitlines()
    return lines[0].strip() if lines else ""


def _is_sizing(body: str) -> bool:
    return bool(_SIZING_RE.match(_first_line(body)))


def _is_cleared(body: str) -> bool:
    return bool(_CLEARED_RE.match(_first_line(body)))


def _parse_sizing(body: str) -> Sizing | str:
    """The sizing a sizing comment records, or what is malformed in it."""
    values: dict[str, str] = {}
    for raw in body.strip().splitlines()[1:]:
        line = raw.rstrip()
        for key in _KEYS:
            if line.startswith(key):
                if key in values:
                    return f"{key} appears more than once"
                values[key] = line.removeprefix(key).strip()
    missing = [key for key in _KEYS if key not in values]
    if missing:
        return f"missing {', '.join(missing)}"
    stated = values[EPIC_SIZE_KEY]
    if stated not in SIZES:
        return f"{EPIC_SIZE_KEY} {stated!r} is not one of {', '.join(SIZES)}"
    tasks_text = values[ESTIMATED_TASKS_KEY]
    if not _TASKS_RE.match(tasks_text):
        return f"{ESTIMATED_TASKS_KEY} {tasks_text!r} is not a positive integer"
    tasks = int(tasks_text)
    confidence = values[CONFIDENCE_KEY]
    if confidence not in CONFIDENCES:
        return f"{CONFIDENCE_KEY} {confidence!r} is not one of {', '.join(CONFIDENCES)}"
    if not values[REASON_KEY]:
        return f"{REASON_KEY} is empty"
    derived = size_for_tasks(tasks)
    if stated != derived:
        return (
            f"{EPIC_SIZE_KEY} {stated} does not match"
            f" {ESTIMATED_TASKS_KEY} {tasks} (size {derived})"
        )
    return Sizing(tasks=tasks, confidence=confidence, reason=values[REASON_KEY])


def size_record(
    labels: Iterable[str],
    comments: Sequence[tuple[str, str]],
    trusted: frozenset[str],
) -> SizeRecord:
    """The size record of an item with *labels* and *comments* (``(author, body)``, oldest first).

    Only comments whose author (a leading ``app/`` stripped) is in *trusted*
    count, and only the sizing comments after the latest trusted cleared
    marker. The latest of those is checked against the ``epic-size:*`` labels:
    several labels, a malformed comment, a comment without a label, a label
    without a comment or a disagreement is an error, never a size.
    """
    size_labels = sorted(label for label in labels if label.startswith(LABEL_PREFIX))

    latest: str | None = None
    for author, body in comments:
        if bare_login(author) not in trusted:
            continue
        if _is_cleared(body):
            latest = None
        elif _is_sizing(body):
            latest = body

    if len(size_labels) > 1:
        return _error(f"several size labels: {', '.join(size_labels)}")
    label = size_labels[0] if size_labels else None
    if label is not None and label.removeprefix(LABEL_PREFIX) not in SIZES:
        return _error(f"unknown size label {label}")

    if latest is None:
        if label is None:
            return _UNSIZED
        return _error(f"label {label} without a sizing comment")

    parsed = _parse_sizing(latest)
    if isinstance(parsed, str):
        return _error(f"malformed sizing comment: {parsed}")
    if label is None:
        return _error(f"sizing comment (epic-size {parsed.size}) without label")
    if label != label_for(parsed.size):
        return _error(f"disagreement: label {label}, sizing comment {EPIC_SIZE_KEY} {parsed.size}")
    return SizeRecord(
        size=parsed.size, tasks=parsed.tasks, confidence=parsed.confidence, error=None
    )


def has_size(labels: Iterable[str], comment_bodies: Sequence[str]) -> bool:
    """Whether an item carries any size: an ``epic-size:*`` label or a ``Sized …`` comment.

    Any author's comment counts: the one use is deciding whether a one-off
    posts a cleared marker, and an extra marker is harmless.
    """
    if any(label.startswith(LABEL_PREFIX) for label in labels):
        return True
    return any(_is_sizing(body) for body in comment_bodies)
