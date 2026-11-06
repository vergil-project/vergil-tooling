"""The human and agent logins, derived only from the trusted-authors configuration.

vergil is built for a single human, so the ``human`` entry must hold exactly one
login. The agent tool runs only in the ``user`` identity mode, and the login it
expects to post as is ``<the human login>-vergil-user``, which must be on the
``agents`` list. Nothing asks GitHub who the caller is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.rulings import RulingError

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors


def human_login(authors: TrustedAuthors) -> str:
    """The one human login; refuse when the configuration lists none or several."""
    if len(authors.human) != 1:
        msg = f"trusted authors must list exactly one human login; found {len(authors.human)}"
        raise RulingError(msg)
    return authors.human[0]


def agent_login(authors: TrustedAuthors, mode: IdentityMode) -> str:
    """The login the agent tool posts as, ``<human>-vergil-user``.

    Refuses in any identity mode but ``user`` (the ``audit`` App can only read
    issues, and a human shell is not an agent), and when the derived login is
    not on the ``agents`` list.
    """
    if mode is not IdentityMode.USER:
        msg = (
            "vrg-ruling runs only in the 'user' identity mode;"
            f" this session is in '{mode.value}' mode"
        )
        raise RulingError(msg)
    login = f"{human_login(authors)}-vergil-user"
    if login not in authors.agents:
        msg = f"the agent login {login} is not on the trusted authors' agents list"
        raise RulingError(msg)
    return login
