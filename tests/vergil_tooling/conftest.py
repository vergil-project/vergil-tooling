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

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vergil_tooling.lib import conan_token, github, host_pins, identity_mode
from vergil_tooling.lib.versions import window
from vergil_tooling.lib.versions.resolve import Resolution

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


@pytest.fixture(autouse=True)
def _hermetic_host_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the host-local vrg-host pins file at a per-test path.

    ``host_pins.default_pins_path()`` resolves under the real ``~/.config``;
    no test may read or write a developer's actual pins. Tests that exercise
    the real resolver import it before this fixture patches the module.
    """
    monkeypatch.setattr(
        host_pins, "default_pins_path", lambda: tmp_path / "vergil-config" / "host-pins.toml"
    )


@pytest.fixture(autouse=True)
def _hermetic_conan_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the Conan audit token file sources at absent per-test paths.

    ``vrg-container-run`` resolves the token from ``~/.config/vergil/conan.env``
    and ``identities.toml`` (#2902); no test may read a developer's or agent
    VM's real token. ``test_conan_token`` captures the real locators at import,
    before this fixture patches the module.
    """
    absent = tmp_path / "absent-conan"
    monkeypatch.setattr(conan_token, "conan_env_path", lambda: absent / "conan.env")
    monkeypatch.setattr(conan_token, "identities_path", lambda: absent / "identities.toml")
    monkeypatch.delenv(conan_token.CONAN_AUDIT_TOKEN_ENV, raising=False)


FIXTURE_WINDOW = Path(__file__).parent / "fixtures" / "versions" / "window.json"


@pytest.fixture(autouse=True)
def _hermetic_version_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve CI versions against a fixture window, never the network.

    The resolver reads the published window over HTTP unless
    ``VRG_VERSION_WINDOW`` names a file (epic vergil-project/.github#407). The
    fixture is a copy of the schema-1 window vergil-containers publishes at
    v2.1. Tests of the fetch path pass their own ``env`` and ``fetch``.
    """
    monkeypatch.setenv(window.WINDOW_ENV, str(FIXTURE_WINDOW))


def window_default_resolution(_repo_root: Path, lang: str) -> Resolution | None:
    """A stand-in for ``container.repo_resolution`` that needs no ``vergil.toml``.

    It resolves *lang* as an unpinned app would, to the newest version of the
    language's first fixture-window family (``python`` -> ``3.14``, ``cpp`` ->
    ``clang-20``), so container tests that only drop a language marker still get
    a real image. Tests of resolution itself use the real function.
    """
    if not lang:
        return None
    family = window.load_window("v2.1", env={window.WINDOW_ENV: str(FIXTURE_WINDOW)}).languages[
        lang
    ][0]
    newest = family.versions[0]
    token = family.token(newest)
    return Resolution(
        language=lang,
        kind="app",
        versions=(token,),
        primary=token,
        container_tags={token: newest.version},
        container_suffixes={token: family.key},
    )


@dataclass
class FakeApp:
    """A configured GitHub App with fake installations, for token-scoping tests.

    ``installations`` maps owner -> installation ID; ``cwd_org`` is what the
    cwd git remote resolves to (``None`` = outside any GitHub checkout). Every
    minted token is ``ghs_fake_<owner>``, so :meth:`token_owner` reports which
    installation the current context's ``gh`` calls would run under.
    """

    installations: dict[str, str] = field(
        default_factory=lambda: {"target-org": "101", "cwd-org": "202"}
    )
    cwd_org: str | None = None

    @staticmethod
    def token_owner() -> str:
        """Owner whose installation minted the token ``_gh_env`` hands to ``gh`` now."""
        env = github._gh_env()
        assert env is not None
        return env["GH_TOKEN"].removeprefix("ghs_fake_")


@pytest.fixture
def fake_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeApp:
    """Put ``lib.github`` in App mode against :class:`FakeApp` installations.

    Only the network edges are faked (JWT signing, the installations listing,
    the token exchange, and cwd org detection); the real token selection in
    ``get_installation_token`` / ``_gh_env`` / ``target_org`` runs unchanged.
    """
    app = FakeApp()

    def _jwt_api_request(endpoint: str, _jwt: str, *, method: str = "GET") -> dict[str, str]:
        assert method == "POST"
        installation_id = endpoint.split("/")[3]  # /app/installations/<id>/access_tokens
        owner = {iid: org for org, iid in app.installations.items()}[installation_id]
        return {"token": f"ghs_fake_{owner}"}

    monkeypatch.setattr(github, "_load_app_config", lambda: ("12345", tmp_path / "app.pem"))
    monkeypatch.setattr(github, "_generate_jwt", lambda _app_id, _key: "fake-jwt")
    monkeypatch.setattr(github, "_resolve_installations", lambda _jwt: dict(app.installations))
    monkeypatch.setattr(github, "_jwt_api_request", _jwt_api_request)
    monkeypatch.setattr(github, "detect_org", lambda: app.cwd_org)
    monkeypatch.setattr(github, "_token_cache", {})
    monkeypatch.setattr(github, "_warned_no_installation", set())
    return app
