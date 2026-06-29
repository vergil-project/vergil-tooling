"""Shared fixtures for the vergil_tooling tests.

**Hermetic identity.** The identity-mode resolver (``lib/identity_mode``)
consults, in order: the ``VRG_IDENTITY_MODE`` env var, the
``~/.config/vergil/identity-mode`` file, and ``~/.config/vergil/app.pem``. On an
*agent-provisioned* box those files exist and resolve to ``USER``, so a test
that does not explicitly choose a mode inherits the host's agent identity and
trips the human-maintainer guards in vrg-submit-pr / vrg-gh / release / audit —
even though it is green on CI and human dev boxes, where no such files exist.
Tests that *do* choose a mode neutralize only the env var (``delenv``), not the
file fallbacks, so they too break on agent boxes.

This autouse fixture points the two file signals at nonexistent paths and clears
the identity env vars, so resolution depends solely on what a test sets via
``VRG_IDENTITY_MODE`` (default: ``HUMAN``). It runs *before* any per-class
identity fixture, so a test that sets an agent/audit mode still wins. It does
**not** touch ``HOME``, so tests that assert on home-relative paths
(``test_vrg_vm``) are unaffected. The modules that exercise the resolver itself
(``test_identity_mode``, ``test_vrg_whoami``) manage their own signals, so they
opt out (see ``_RESOLVER_TEST_MODULES``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import identity_mode

if TYPE_CHECKING:
    from pathlib import Path

# Modules that test identity resolution directly and set the signals themselves.
_RESOLVER_TEST_MODULES = frozenset({"test_identity_mode", "test_vrg_whoami"})


@pytest.fixture(autouse=True)
def _hermetic_identity(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if request.module.__name__.rsplit(".", 1)[-1] in _RESOLVER_TEST_MODULES:
        return
    absent = tmp_path / "absent"
    monkeypatch.setattr(identity_mode, "_MODE_FILE", str(absent / "identity-mode"))
    monkeypatch.setattr(identity_mode, "_APP_KEY_FILE", str(absent / "app.pem"))
    monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
    monkeypatch.delenv("VRG_APP_ID", raising=False)
