"""What the human-only commands share: the terminal, the login and the shown text.

``vrg-ruling-reply`` and ``vrg-scope-change`` each post a comment **as the
human**, from the human's own shell. Before posting, each:

- needs an interactive terminal (:func:`is_interactive`), since the text is
  shown and confirmed at a prompt;
- refuses unless its credentials post as the one human login
  (:func:`require_login`);
- prints the text exactly as it will be posted, with its byte length and
  SHA-256 (:func:`show`).
"""

from __future__ import annotations

import hashlib
import sys

from vergil_tooling.lib import github
from vergil_tooling.lib.rulings import RulingError


def is_interactive() -> bool:
    """Whether stdin and stdout are both terminals."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def require_login(human: str, prog: str) -> None:
    """Refuse unless the credentials in this shell post as ``human``.

    ``prog`` names the command in the refusal. The login found is never echoed.
    """
    try:
        login = github.read_output("api", "user", "--jq", ".login")
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        msg = (
            "cannot read the login these credentials post as (gh api user failed;"
            f" run {prog} in the human's own shell, logged in to gh): {exc}"
        )
        raise RulingError(msg) from exc
    if login != human:
        msg = (
            f"these credentials post as a login other than the human login {human};"
            f" run {prog} in the human's own shell, logged in to gh as {human}"
        )
        raise RulingError(msg)


def show(text: str, noun: str) -> None:
    """Print ``text`` exactly as it will be posted, with its length and hash.

    ``noun`` names it in the delimiter lines (``the reply``, ``the scope
    change``). ``text`` is plain text, so it is ASCII.
    """
    data = text.encode("ascii")
    print(f"----- {noun}, exactly as it will be posted -----")
    sys.stdout.write(text)
    if not text.endswith("\n"):
        print()
        print(f"----- end of {noun} (it has no final newline) -----")
    else:
        print(f"----- end of {noun} -----")
    print(f"bytes: {len(data)}")
    print(f"sha256: {hashlib.sha256(data).hexdigest()}")
    sys.stdout.flush()
