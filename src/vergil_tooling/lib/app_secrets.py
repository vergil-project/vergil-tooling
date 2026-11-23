"""Check that the automation App's secrets reach each repo that needs them.

Every unattended job of epic vergil-project/.github#1494 mints its token
from the org's automation App, through the Actions secrets
:data:`APP_SECRETS` (``APP_CLIENT_ID`` and ``APP_PRIVATE_KEY``). Since epic
vergil-project/.github#1384 those secrets live only in each repo's
``automation`` deployment environment
(:data:`~vergil_tooling.lib.github_config.AUTOMATION_ENVIRONMENT_NAME`),
never as repository or organization secrets. :func:`check_repo` reports, for
one repo, whether each secret is in that environment, and every repository
or organization secret of the same name that the repo can see, which the
layout forbids.

The repos to check are those that run an in-scope job: a repo that is not
archived and has a workflow file named in :data:`IN_SCOPE_WORKFLOWS`
(:func:`in_scope_repos`), the callers of spec §4.1 of that epic.

Only secret **names** are read. The GitHub secrets list APIs return no value,
and each read here projects every item to its ``name`` at the source
(``--jq``), so no other field of a secret is ever held. A GitHub API error, or
a response in an unknown shape, raises :class:`AppSecretsError` naming the
call; it never reads as "missing" or "present". The one answer read as
absence is an HTTP 404 on the environment's secrets list, read only after
the repo's own secrets list has been read: the repo is then known to be
visible and readable, so the 404 is the environment's.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from typing import cast

from vergil_tooling.lib import github
from vergil_tooling.lib.github_config import AUTOMATION_ENVIRONMENT_NAME
from vergil_tooling.lib.process_errors import error_text

APP_SECRETS = ("APP_CLIENT_ID", "APP_PRIVATE_KEY")
# The workflow files that call an in-scope unattended job (spec §4.1 of
# vergil-project/.github#1494): OPS, ROLLUP, SWEEP and CD.
IN_SCOPE_WORKFLOWS = (
    ".github/workflows/cd.yml",
    ".github/workflows/epic-rollup.yml",
    ".github/workflows/epic-sweep.yml",
    ".github/workflows/ops.yml",
)
# Where an App secret is found: in the automation environment, or nowhere.
ENVIRONMENT = "environment"
MISSING = "MISSING"
# The levels at which a copy of an App secret is forbidden.
REPO = "repo"
ORG = "org"
_NAMES_ONLY = "{secrets: [.secrets[] | {name}]}"
# gh ends its error line with the status, ``gh: Not Found (HTTP 404)``; a
# ``404`` anywhere else in the output does not count.
_NOT_FOUND_RE = re.compile(r"\(HTTP 404\)$", re.MULTILINE)


class AppSecretsError(RuntimeError):
    """A GitHub read failed, or answered in an unknown shape."""


@dataclass(frozen=True)
class RepoSecrets:
    """Where each of :data:`APP_SECRETS` stands in one repo.

    *sources* maps each secret name to :data:`ENVIRONMENT` (a secret of the
    ``automation`` environment) or :data:`MISSING`. *has_environment* is
    False when the repo has no ``automation`` environment, so both are
    missing. *forbidden* lists each ``(name, level)`` of an App secret the
    repo can also see as a repository (:data:`REPO`) or organization
    (:data:`ORG`) secret, which the layout forbids.
    """

    repo: str
    sources: dict[str, str]
    has_environment: bool = True
    forbidden: list[tuple[str, str]] = field(default_factory=lambda: [])

    @property
    def missing(self) -> list[str]:
        return [name for name, source in self.sources.items() if source == MISSING]


def _objects(endpoint: str, items: list[object]) -> list[dict[str, object]]:
    """*items* of *endpoint*, each a JSON object, or raise."""
    objects: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            msg = f"GET {endpoint} returned an item that is not an object"
            raise AppSecretsError(msg)
        objects.append(cast("dict[str, object]", item))
    return objects


def _read_error(endpoint: str, exc: subprocess.CalledProcessError | ValueError) -> AppSecretsError:
    return AppSecretsError(f"GET {endpoint} failed: {error_text(exc)}")


def _read_list(endpoint: str, key: str | None) -> list[dict[str, object]]:
    """Every item of the REST list *endpoint*, each a JSON object, or raise."""
    try:
        items = github.read_json_paginated(endpoint, key)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise _read_error(endpoint, exc) from exc
    return _objects(endpoint, items)


def _string_field(item: dict[str, object], name: str, endpoint: str) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        msg = f"GET {endpoint} returned an item with no string {name!r}"
        raise AppSecretsError(msg)
    return value


def _names(endpoint: str, items: list[object]) -> set[str]:
    """The upper-cased ``name`` of each secret in *items*, read from *endpoint*."""
    return {_string_field(item, "name", endpoint).upper() for item in _objects(endpoint, items)}


def _secret_names(repo: str, listing: str) -> set[str]:
    """The upper-cased names of *repo*'s secrets in the *listing* API, names only."""
    endpoint = f"repos/{repo}/actions/{listing}"
    try:
        # The endpoint is spelled out at the call so the trust guard test can
        # resolve it.
        items = github.read_json_paginated(
            f"repos/{repo}/actions/{listing}", "secrets", jq=_NAMES_ONLY
        )
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise _read_error(endpoint, exc) from exc
    return _names(endpoint, items)


def _is_not_found(exc: subprocess.CalledProcessError) -> bool:
    """True when *exc* is gh reporting an HTTP 404 status."""
    stderr = exc.stderr if isinstance(exc.stderr, str) else ""
    return _NOT_FOUND_RE.search(stderr.replace("\r", "")) is not None


def _environment_secret_names(repo: str) -> set[str] | None:
    """The upper-cased names of *repo*'s ``automation`` environment secrets.

    ``None`` when GitHub answers HTTP 404: the environment does not exist.
    Call it only once a read of *repo*'s own secrets has succeeded, so the
    404 cannot be the repo's. Any other failure raises.
    """
    endpoint = f"repos/{repo}/environments/{AUTOMATION_ENVIRONMENT_NAME}/secrets"
    try:
        # Spelled out at the call, as in _secret_names, for the trust guard.
        items = github.read_json_paginated(
            f"repos/{repo}/environments/{AUTOMATION_ENVIRONMENT_NAME}/secrets",
            "secrets",
            jq=_NAMES_ONLY,
        )
    except subprocess.CalledProcessError as exc:
        if _is_not_found(exc):
            return None
        raise _read_error(endpoint, exc) from exc
    except ValueError as exc:
        raise _read_error(endpoint, exc) from exc
    return _names(endpoint, items)


def check_repo(repo: str) -> RepoSecrets:
    """Where each of :data:`APP_SECRETS` stands in *repo* (``owner/name``).

    Reads the repository secrets, the organization secrets visible to the
    repo and the ``automation`` environment's secrets, names only, in that
    order. Raises :class:`AppSecretsError` when a read fails.
    """
    repo_names = _secret_names(repo, "secrets")
    org_names = _secret_names(repo, "organization-secrets")
    env_names = _environment_secret_names(repo)
    sources = {
        name: ENVIRONMENT if env_names is not None and name in env_names else MISSING
        for name in APP_SECRETS
    }
    forbidden = [
        (name, level)
        for name in APP_SECRETS
        for level, names in ((REPO, repo_names), (ORG, org_names))
        if name in names
    ]
    return RepoSecrets(
        repo=repo,
        sources=sources,
        has_environment=env_names is not None,
        forbidden=forbidden,
    )


def _runs_in_scope_job(repo: str) -> bool:
    endpoint = f"repos/{repo}/actions/workflows"
    paths = {_string_field(item, "path", endpoint) for item in _read_list(endpoint, "workflows")}
    return any(path in paths for path in IN_SCOPE_WORKFLOWS)


def in_scope_repos(org: str) -> list[str]:
    """The ``owner/name`` of each repo of *org* that runs an in-scope job, sorted.

    A repo runs one when it is not archived and has a workflow file named in
    :data:`IN_SCOPE_WORKFLOWS`. Raises :class:`AppSecretsError` when a read
    fails.
    """
    endpoint = f"orgs/{org}/repos?type=all"
    repos: list[str] = []
    for item in _read_list(endpoint, None):
        full_name = _string_field(item, "full_name", endpoint)
        archived = item.get("archived")
        if not isinstance(archived, bool):
            msg = f"GET {endpoint} returned {full_name} with no boolean 'archived'"
            raise AppSecretsError(msg)
        if not archived and _runs_in_scope_job(full_name):
            repos.append(full_name)
    return sorted(repos)
