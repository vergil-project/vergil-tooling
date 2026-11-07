"""Authenticated human replies on task issues (epic vergil-project/.github#889).

The shared library behind ``vrg-ruling`` (the agent tool) and
``vrg-ruling-reply`` (the human tool), and behind ``vrg-scope-change`` (the
human tool that posts a scope change). One round on a task issue is a
**request** (posted by the agent), a **reply** (posted by the human, as the
human) and a **response** (posted by the agent). The modules here hold what
the tools share:

- :mod:`.identity` derives the human and agent logins from the trusted-authors
  configuration;
- :mod:`.comments` reads every comment on an issue, with its 64-bit ID;
- :mod:`.marker` renders and recognises the hidden end-of-body marker;
- :mod:`.plain_text` screens text down to printable ASCII plus newline;
- :mod:`.exchange` runs the turn checks over the trusted comment stream;
- :mod:`.items` loads the items files and renders the request, the drafted
  reply and the response;
- :mod:`.post` posts a comment exactly once and finds out whether a failed
  post landed;
- :mod:`.draft_ref` relays the reply (or scope change) the agent drafts to
  the human over a reserved git ref;
- :mod:`.human_post` holds what the human-only commands share: the terminal
  and login checks and the text shown before posting;
- :mod:`.scope` renders and recognises a scope change, which
  ``vrg-scope-change`` posts as the human.

Identity is never read from text: a comment's author is the only source of who
wrote it, and the marker only says what kind of comment it is.
"""

from __future__ import annotations


class RulingError(Exception):
    """A refusal: the tools stop with a non-zero exit and this message."""
