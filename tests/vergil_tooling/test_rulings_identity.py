"""Tests for ``vergil_tooling.lib.rulings.identity``."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.identity import agent_login, human_login
from vergil_tooling.lib.trusted_authors import TrustedAuthors


def test_human_login_requires_exactly_one() -> None:
    assert human_login(TrustedAuthors(human=("h",), agents=())) == "h"
    with pytest.raises(RulingError, match="exactly one human login; found 0"):
        human_login(TrustedAuthors(human=(), agents=()))
    with pytest.raises(RulingError, match="exactly one human login; found 2"):
        human_login(TrustedAuthors(human=("a", "b"), agents=()))


def test_agent_login_user_mode_only() -> None:
    a = TrustedAuthors(human=("h",), agents=("h-vergil-user",))
    assert agent_login(a, IdentityMode.USER) == "h-vergil-user"
    with pytest.raises(RulingError, match="audit"):
        agent_login(a, IdentityMode.AUDIT)
    with pytest.raises(RulingError, match="human"):
        agent_login(a, IdentityMode.HUMAN)


def test_agent_login_must_be_listed() -> None:
    with pytest.raises(RulingError, match="h-vergil-user"):
        agent_login(TrustedAuthors(human=("h",), agents=()), IdentityMode.USER)


def test_agent_login_ignores_other_agents() -> None:
    a = TrustedAuthors(human=("h",), agents=("h-vergil-audit",))
    with pytest.raises(RulingError, match="h-vergil-user is not on"):
        agent_login(a, IdentityMode.USER)


def test_agent_login_refuses_when_human_is_ambiguous() -> None:
    a = TrustedAuthors(human=("a", "b"), agents=("a-vergil-user",))
    with pytest.raises(RulingError, match="exactly one"):
        agent_login(a, IdentityMode.USER)
