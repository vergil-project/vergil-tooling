"""The org automation App: its name, its numeric ID, and token verification.

The ``automation`` identity mode (:mod:`vergil_tooling.lib.identity_mode`)
belongs to a process whose GitHub credential is an installation token of the
org's automation App. It is used only for mechanized work, never for anything
an AI agent does (epic vergil-project/.github#1064, spec §3 and §6.1).

**Which App is the automation App** is configuration, not code. The human's
trusted-authors configuration names each Vergil org's App slug once, as the
``automation_app`` of its ``[orgs.<org>]`` stanza
(:mod:`vergil_tooling.lib.trusted_authors`, epic vergil-project/.github#1467,
spec §5.1a). The org-level Actions variable ``VRG_AUTOMATION_APP`` is a copy
derived from it; a workflow job that runs as the automation identity copies
that into the ``VRG_AUTOMATION_APP`` environment variable.
:func:`automation_app_slug` reads the environment variable only for the job's
own org, and the configuration otherwise; it never reads the API.

**The environment variable only decides whether verification runs.** With it
set, :func:`~vergil_tooling.lib.identity_mode.resolve` calls
:func:`verify_token_is_app`, which asks GitHub who the credential is (GraphQL
``viewer { login }``) and requires ``<slug>[bot]``. The variable alone grants
nothing: without that App's token, verification fails and so does the run.

**What is trusted, and why.** The slug in ``VRG_AUTOMATION_APP`` is trusted
because ops copies it from the org-level Actions variable, which only an org
admin can set. Verification proves the token belongs to the App the slug
names; it does not prove that App is the org's automation App. Outside ops, a
process that sets the environment variable by hand still holds only its own
token, so GitHub bounds what it can do: naming its own App passes
verification but gains no right the token did not already have. On an agent
VM, the agent identity signals (``VRG_IDENTITY_MODE``, the mode file, the
provisioned App credential) make resolution refuse, and ``update`` refuses the
agent modes. The epic's plan review accepted this trust assumption in place of
a stronger check on the App's owner.
"""

from __future__ import annotations

import os
from typing import cast

from vergil_tooling.lib import trusted_authors
from vergil_tooling.lib.process_errors import error_text

# ``lib.github`` is imported inside the functions that call GitHub, never at
# module level: ``identity_mode`` imports this module, and the many commands
# that resolve their mode without talking to GitHub must not pay its import
# cost (the same rule ``lib.cli`` follows).

AUTOMATION_ENV = "VRG_AUTOMATION_APP"
ORG_VARIABLE = "VRG_AUTOMATION_APP"
# The org of the job's own repo, set by GitHub Actions in every job.
_REPOSITORY_OWNER_ENV = "GITHUB_REPOSITORY_OWNER"

_VIEWER_LOGIN_QUERY = "query { viewer { login } }"


class AutomationIdentityError(RuntimeError):
    """The automation App's name, ID or credential could not be established."""


def _field(payload: object, name: str) -> object:
    """*payload*'s *name* field when it is a JSON object, else ``None``."""
    if not isinstance(payload, dict):
        return None
    return cast("dict[str, object]", payload).get(name)


def env_slug() -> str | None:
    """The slug from ``$VRG_AUTOMATION_APP``, or ``None`` when it is not set.

    A variable that is set but empty or blank raises: a job that sets it means
    to run as the automation identity, and an unset org variable reaches the
    job as an empty string. That is missing configuration, never "not set".
    """
    raw = os.environ.get(AUTOMATION_ENV)
    if raw is None:
        return None
    slug = raw.strip()
    if not slug:
        msg = (
            f"${AUTOMATION_ENV} is set but empty: the org-level Actions variable "
            f"{ORG_VARIABLE} is missing or empty"
        )
        raise AutomationIdentityError(msg)
    return slug


def _is_own_org(org: str) -> bool:
    """Whether *org* is the org of the job's own repo (``$GITHUB_REPOSITORY_OWNER``)."""
    owner = os.environ.get(_REPOSITORY_OWNER_ENV, "").strip()
    return bool(owner) and owner.casefold() == org.casefold()


def automation_app_slug(org: str) -> str:
    """The automation App's slug for *org*; never read from the API.

    ``$VRG_AUTOMATION_APP`` only when *org* is the job's own org
    (``$GITHUB_REPOSITORY_OWNER``): that variable is the job's own org's
    value. Otherwise the ``automation_app`` of *org*'s ``[orgs.<org>]``
    stanza in the trusted-authors configuration
    (:func:`~vergil_tooling.lib.trusted_authors.load_trusted_authors`).
    Raises :class:`AutomationIdentityError` when the configuration cannot be
    loaded or has no stanza for *org*.
    """
    if _is_own_org(org):
        slug = env_slug()
        if slug is not None:
            return slug
    try:
        authors = trusted_authors.load_trusted_authors()
    except (trusted_authors.TrustedAuthorsNotConfiguredError, ValueError) as exc:
        msg = f"could not read the automation App of {org} from the configuration: {exc}"
        raise AutomationIdentityError(msg) from exc
    config = trusted_authors.org_config(authors, org)
    if config is None:
        msg = (
            f"{org} is not a Vergil org: the trusted-authors configuration has no"
            f" [{trusted_authors.ORGS_STANZA}.{org}] stanza naming its automation App"
        )
        raise AutomationIdentityError(msg)
    return config.automation_app


def automation_app_id(slug: str) -> int:
    """The numeric ID of the App named *slug*, from ``GET apps/<slug>``.

    The org automation Apps are private, and the unauthenticated public lookup
    answers 404 for a private App, so this goes through the authenticated
    :func:`github.read_json` (the run's own token), never an anonymous request.
    The endpoint is documented at
    https://docs.github.com/en/rest/apps/apps#get-an-app and is listed among
    those available to installation access tokens at
    https://docs.github.com/en/rest/authentication/endpoints-available-for-github-app-installation-access-tokens#apps
    """
    from vergil_tooling.lib import github

    try:
        payload = github.read_json("api", f"apps/{slug}")
    except github.GitHubAPIError as exc:
        msg = f"could not look up the GitHub App {slug!r}: {error_text(exc)}"
        raise AutomationIdentityError(msg) from exc
    app_id = _field(payload, "id")
    if not isinstance(app_id, int) or isinstance(app_id, bool):
        msg = f"the GitHub App lookup for {slug!r} returned no numeric id: {payload!r}"
        raise AutomationIdentityError(msg)
    return app_id


def verify_token_is_app(slug: str) -> None:
    """Require the run's GitHub credential to be an installation token of *slug*.

    GraphQL ``viewer { login }`` must equal ``<slug>[bot]``. Transient errors
    are retried by :func:`github.graphql` (through ``_run_with_retry``); any
    failure left after that, or a different login, raises
    :class:`AutomationIdentityError` naming the expected and actual login.
    """
    from vergil_tooling.lib import github

    expected = f"{slug}[bot]"
    try:
        data = github.graphql(_VIEWER_LOGIN_QUERY)
    except (github.GitHubAPIError, RuntimeError) as exc:
        msg = (
            f"${AUTOMATION_ENV} names the automation App {slug!r}, but the token's "
            f"identity could not be read (expected login {expected!r}): {error_text(exc)}"
        )
        raise AutomationIdentityError(msg) from exc
    actual = _field(data.get("viewer"), "login")
    if actual != expected:
        msg = (
            f"${AUTOMATION_ENV} names the automation App {slug!r}, but the token "
            f"belongs to {actual!r}, not {expected!r}"
        )
        raise AutomationIdentityError(msg)
