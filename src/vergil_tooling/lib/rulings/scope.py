"""A scope change on a task issue: the human's comment, and how to recognise it.

``vrg-scope-change`` posts a scope change on a task issue as the human: a
maintainer's addition to or narrowing of the task's scope. Its body is a fixed,
visible header line, a blank line and the text:

.. code-block:: text

    Scope change

    <the text, plain text, one or more lines>

The header is the scope comment's distinct marker. It is visible, not a hidden
``<!-- vrg-ruling`` marker: the human's comment is plain text
(:mod:`.plain_text`), so what the human confirms on screen is all that is
posted, and a hidden marker counts only on an agent comment anyway. Like every
marker, the header says what kind of comment it is, never who wrote it: a
comment is a scope change only when its author is the human login and its
first line is exactly :data:`SCOPE_HEADER` (:func:`is_scope_change`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.plain_text import check_plain_text

if TYPE_CHECKING:
    from vergil_tooling.lib.rulings.comments import IssueComment

SCOPE_HEADER = "Scope change"


def _first_line(text: str) -> str:
    lines = text.splitlines()
    return lines[0].rstrip() if lines else ""


def render_scope_change(text: str) -> str:
    """The scope comment's body: the header, a blank line, then ``text``.

    ``text`` must be plain text, not blank, and must not start with the header
    itself. Leading and trailing blank lines are dropped and the body ends with
    one newline.
    """
    check_plain_text(text, what="the scope change")
    stripped = text.strip("\n")
    if not stripped.strip():
        msg = "the scope change is blank"
        raise RulingError(msg)
    if _first_line(stripped) == SCOPE_HEADER:
        msg = f"give the scope change's text alone: the '{SCOPE_HEADER}' header is added for you"
        raise RulingError(msg)
    return f"{SCOPE_HEADER}\n\n{stripped}\n"


def is_scope_change(comment: IssueComment, human: str) -> bool:
    """Whether ``comment`` is a scope change: by ``human``, first line exactly the header.

    Whether the comment has been edited is left to the caller: it is on
    ``comment.edited``.
    """
    return bool(human) and comment.author == human and _first_line(comment.body) == SCOPE_HEADER
