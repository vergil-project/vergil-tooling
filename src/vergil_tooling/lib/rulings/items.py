"""The items files behind a request, a drafted reply and a response, and their bodies.

An items file is a JSON array with one object per item. Every item has an
``id`` (dot-separated letters and digits, at most 16 characters) and a
``security`` flag (a boolean). A security item is marked by the flag, never by
the shape of its ID.

- **A request item**, when not flagged, has ``text`` (the question) and
  ``recommendation`` (the agent's), both non-empty strings. A flagged one has
  nothing else: the request carries only its ID and
  :data:`SECURITY_REQUEST_TEXT`.
- **A response item**, when not flagged, has ``text``: how the agent read the
  reply and what it did. A flagged one has only ``outcome``, one of
  :data:`RULING_OUTCOMES`, which is also its reading.
- **A reply item** (the reply the agent drafts for the human), when not
  flagged, has ``text``: the decision in prose. A flagged one has only
  ``decision``, one of :data:`RULING_DECISIONS`.

Every ``text`` and ``recommendation`` is plain text (printable ASCII, see
:mod:`.plain_text`) on a single line, so each item renders as exactly one line
of the body. Any other key, a flagged item carrying anything else, or any
failed check is refused with :class:`RulingError`, naming the item by its ID
(or, when the ID itself is bad, by its position). A refusal never echoes a
refused value, so a security detail placed by mistake in an items file never
reaches the terminal or a log.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.marker import Marker, render_marker
from vergil_tooling.lib.rulings.plain_text import check_plain_text

if TYPE_CHECKING:
    from pathlib import Path

SECURITY_REQUEST_TEXT = "security finding, details given privately"
RULING_OUTCOMES = ("fixed", "filed", "deferred", "wont-fix")
RULING_DECISIONS = ("fix", "file", "defer", "wont-fix")

_ID_RE = re.compile(r"[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*")
_ID_MAX = 16


@dataclass(frozen=True)
class Item:
    """One item of a request, a response or (Task 3) a reply.

    ``outcome`` holds a flagged response item's outcome and a flagged reply
    item's decision; it is ``None`` everywhere else.
    """

    id: str
    security: bool
    text: str | None
    recommendation: str | None
    outcome: str | None


def _load_array(path: Path) -> list[Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read the items file {path}: {exc}"
        raise RulingError(msg) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f"the items file {path} is not valid JSON: {exc.msg} at line {exc.lineno}"
        raise RulingError(msg) from exc
    if not isinstance(data, list):
        msg = f"the items file {path} must hold a JSON array of items"
        raise RulingError(msg)
    if not data:
        msg = f"the items file {path} holds no items"
        raise RulingError(msg)
    return data


def _item_id(raw: Any, position: int) -> str:
    """The item's ID, checked; a bad ID is named by the item's position only."""
    where = f"item {position} of the items file"
    if not isinstance(raw, dict):
        msg = f"{where} is not a JSON object"
        raise RulingError(msg)
    item_id = raw.get("id")
    if not isinstance(item_id, str) or len(item_id) > _ID_MAX or _ID_RE.fullmatch(item_id) is None:
        msg = (
            f"{where} has no valid id: an id is dot-separated letters and digits,"
            f" at most {_ID_MAX} characters"
        )
        raise RulingError(msg)
    if not isinstance(raw.get("security"), bool):
        msg = f"item {item_id} needs a 'security' flag, true or false"
        raise RulingError(msg)
    return item_id


def _check_keys(raw: dict[str, Any], item_id: str, allowed: set[str]) -> None:
    extra = set(raw) - allowed
    if extra:
        # The extra keys are counted, never named: a key could carry a detail.
        flagged = " a security item" if raw["security"] else ""
        msg = (
            f"item {item_id}:{flagged} may carry only {', '.join(sorted(allowed))};"
            f" it has {len(extra)} other key(s)"
        )
        raise RulingError(msg)
    missing = sorted(allowed - set(raw))
    if missing:
        msg = f"item {item_id} is missing {', '.join(missing)}"
        raise RulingError(msg)


def _prose(raw: dict[str, Any], item_id: str, key: str) -> str:
    """A ``text`` or ``recommendation`` field: a non-empty line of plain text."""
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        msg = f"item {item_id}: {key} must be a non-empty string"
        raise RulingError(msg)
    check_plain_text(value, what=f"item {item_id} {key}")
    if "\n" in value:
        msg = f"item {item_id}: {key} must be one line (each item renders as one line)"
        raise RulingError(msg)
    return value


def _check_unique(items: list[Item]) -> list[Item]:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            msg = f"item {item.id} appears more than once in the items file"
            raise RulingError(msg)
        seen.add(item.id)
    return items


def load_request_items(path: Path) -> list[Item]:
    """The items of a request, every check run; refuses with :class:`RulingError`."""
    items: list[Item] = []
    for position, raw in enumerate(_load_array(path), start=1):
        item_id = _item_id(raw, position)
        if raw["security"]:
            _check_keys(raw, item_id, {"id", "security"})
            items.append(Item(item_id, True, None, None, None))
            continue
        _check_keys(raw, item_id, {"id", "security", "text", "recommendation"})
        text = _prose(raw, item_id, "text")
        recommendation = _prose(raw, item_id, "recommendation")
        items.append(Item(item_id, False, text, recommendation, None))
    return _check_unique(items)


def load_response_items(path: Path) -> list[Item]:
    """The items of a response, every check run; refuses with :class:`RulingError`."""
    items: list[Item] = []
    for position, raw in enumerate(_load_array(path), start=1):
        item_id = _item_id(raw, position)
        if raw["security"]:
            _check_keys(raw, item_id, {"id", "security", "outcome"})
            outcome = raw["outcome"]
            if outcome not in RULING_OUTCOMES:
                msg = (
                    f"item {item_id}: a security item's outcome must be one of"
                    f" {', '.join(RULING_OUTCOMES)}"
                )
                raise RulingError(msg)
            items.append(Item(item_id, True, None, None, outcome))
            continue
        _check_keys(raw, item_id, {"id", "security", "text"})
        items.append(Item(item_id, False, _prose(raw, item_id, "text"), None, None))
    return _check_unique(items)


def load_reply_items(path: Path) -> list[Item]:
    """The items of a drafted reply, every check run; refuses with :class:`RulingError`.

    Shaped like a response's items, with ``decision`` in place of ``outcome``:
    a flagged item carries only its ID and a decision from
    :data:`RULING_DECISIONS`, held in :attr:`Item.outcome`.
    """
    items: list[Item] = []
    for position, raw in enumerate(_load_array(path), start=1):
        item_id = _item_id(raw, position)
        if raw["security"]:
            _check_keys(raw, item_id, {"id", "security", "decision"})
            decision = raw["decision"]
            if decision not in RULING_DECISIONS:
                msg = (
                    f"item {item_id}: a security item's decision must be one of"
                    f" {', '.join(RULING_DECISIONS)}"
                )
                raise RulingError(msg)
            items.append(Item(item_id, True, None, None, decision))
            continue
        _check_keys(raw, item_id, {"id", "security", "text"})
        items.append(Item(item_id, False, _prose(raw, item_id, "text"), None, None))
    return _check_unique(items)


def render_reply(items: list[Item]) -> str:
    """The drafted reply: one ``<id>: <text or decision>`` line per item, no marker.

    The reply is the human's comment, so it carries no marker; every line ends
    with a newline.
    """
    return "".join(f"{item.id}: {item.outcome if item.security else item.text}\n" for item in items)


def _body(lines: list[str], marker: Marker) -> str:
    """The item lines, a blank line, then the marker as the last line."""
    return "\n".join([*lines, "", render_marker(marker)])


def render_request(items: list[Item]) -> str:
    """The request's body: one line per item, ending with the request marker."""
    lines = [
        f"{item.id}: {SECURITY_REQUEST_TEXT}"
        if item.security
        else f"{item.id}: {item.text} Recommendation: {item.recommendation}"
        for item in items
    ]
    return _body(lines, Marker("request", None, None))


def render_response(items: list[Item], *, request_id: int, reply_id: int) -> str:
    """The response's body: one line per item, ending with the response marker."""
    lines = [f"{item.id}: {item.outcome if item.security else item.text}" for item in items]
    return _body(lines, Marker("response", request_id, reply_id))
