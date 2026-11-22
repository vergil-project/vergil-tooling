"""Publish and check the GitHub Actions copies of the trusted-authors configuration.

A GitHub Actions job cannot read the human's host ``identities.toml``, so each
Vergil org carries two org-level Actions variables derived from it (epic
vergil-project/.github#1467, spec §5.1a):

- ``VRG_TRUSTED_AUTHORS``: the compact JSON of
  :func:`~vergil_tooling.lib.trusted_authors.render_actions_value`, the
  ``human`` and ``agents`` lists and **every** listed org's automation App;
- ``VRG_AUTOMATION_APP``: the slug of this org's own automation App.

:func:`publish` writes both from the configuration, with visibility ``all``.
It is a setup step the human runs when an org is set up or the file changes.
:func:`check` reads them back and reports each one that is missing, invalid
or not visible to every repo, and every repository-level variable of either
name, which would override the org copy in that repo's jobs. It is
automation: the daily config audit runs it as the org's automation App.
:func:`expected_values` is what the host file implies, with no GitHub call,
so the morning review can compare it with the audit's uploaded values.

Every value here is a login or an App slug, all public. Nothing here reads a
credential. A GitHub API error raises :class:`TrustPublishError` naming the
call; it never reads as "missing" or "clean".
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.automation_identity import ORG_VARIABLE as AUTOMATION_VARIABLE
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.trusted_authors import (
    ACTIONS_ENV,
    org_config,
    parse_actions_value,
    render_actions_value,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

TRUSTED_AUTHORS_VARIABLE = ACTIONS_ENV
ORG_VARIABLES = (TRUSTED_AUTHORS_VARIABLE, AUTOMATION_VARIABLE)
# Every repo of the org must see the org copies: a repo the variable is not
# visible to would get an empty value, and its jobs would stop.
VISIBILITY = "all"
_APP_PREFIX = "app/"
_BOT_SUFFIX = "[bot]"


class TrustPublishError(RuntimeError):
    """A GitHub call of :func:`check` or :func:`publish` failed, or answered in an unknown shape."""


class PublishRefusedError(RuntimeError):
    """:func:`publish` refused before writing: a repository-level variable shadows the org copy."""


@dataclass(frozen=True)
class CheckResult:
    """What :func:`check` found for one org.

    *findings* has one line per problem, metadata only. *shadows* is the
    subset that names a repository-level variable. *values* maps each org
    variable that exists to its current value.
    """

    findings: list[str] = field(default_factory=list)
    shadows: list[str] = field(default_factory=list)
    values: dict[str, str] = field(default_factory=dict)


def expected_values(org: str, authors: TrustedAuthors) -> dict[str, str]:
    """The org variables the configuration implies for *org*, with no GitHub call.

    Raises :class:`ValueError` when *org* has no ``[orgs.<org>]`` stanza.
    """
    config = org_config(authors, org)
    if config is None:
        msg = f"{org} is not a Vergil org: the configuration has no [orgs.{org}] stanza"
        raise ValueError(msg)
    return {
        TRUSTED_AUTHORS_VARIABLE: render_actions_value(authors),
        AUTOMATION_VARIABLE: config.automation_app,
    }


def _read_list(endpoint: str, key: str | None) -> list[dict[str, object]]:
    """Every item of the REST list *endpoint*, each a JSON object, or raise."""
    try:
        items = github.read_json_paginated(endpoint, key)
    except (subprocess.CalledProcessError, ValueError) as exc:
        msg = f"GET {endpoint} failed: {error_text(exc)}"
        raise TrustPublishError(msg) from exc
    objects: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            msg = f"GET {endpoint} returned an item that is not an object"
            raise TrustPublishError(msg)
        objects.append(cast("dict[str, object]", item))
    return objects


def _string_field(item: dict[str, object], name: str, endpoint: str) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        msg = f"GET {endpoint} returned an item with no string {name!r}"
        raise TrustPublishError(msg)
    return value


def _org_variables(org: str) -> dict[str, dict[str, object]]:
    """*org*'s Actions variables of :data:`ORG_VARIABLES`, by name."""
    endpoint = f"orgs/{org}/actions/variables"
    found: dict[str, dict[str, object]] = {}
    for item in _read_list(endpoint, "variables"):
        name = _string_field(item, "name", endpoint).upper()
        if name in ORG_VARIABLES:
            found[name] = item
    return found


def _slug_problem(slug: str) -> str | None:
    """Why *slug* is not a valid bare App slug, or ``None`` when it is."""
    if not slug.strip():
        return "it is empty"
    if slug != slug.strip():
        return "it has surrounding whitespace"
    if slug.startswith(_APP_PREFIX):
        return "it has the 'app/' prefix"
    if slug.endswith(_BOT_SUFFIX):
        return "it has the '[bot]' suffix"
    return None


def _value_findings(org: str, values: dict[str, str]) -> list[str]:
    """The findings on the values of the org variables that exist."""
    findings: list[str] = []
    authors = None
    if TRUSTED_AUTHORS_VARIABLE in values:
        where = f"{org}: org variable {TRUSTED_AUTHORS_VARIABLE} is invalid"
        try:
            authors = parse_actions_value(values[TRUSTED_AUTHORS_VARIABLE], where=where)
        except ValueError as exc:
            findings.append(str(exc))
    slug = values.get(AUTOMATION_VARIABLE)
    if slug is not None:
        problem = _slug_problem(slug)
        if problem is not None:
            findings.append(f"{org}: org variable {AUTOMATION_VARIABLE} is invalid: {problem}")
            slug = None
    if authors is not None and slug is not None:
        config = org_config(authors, org)
        if config is None:
            findings.append(
                f"{org}: org variable {TRUSTED_AUTHORS_VARIABLE} has no stanza for {org}"
            )
        elif config.automation_app != slug:
            findings.append(
                f"{org}: org variable {AUTOMATION_VARIABLE} is {slug!r}, but"
                f" {TRUSTED_AUTHORS_VARIABLE} names {config.automation_app!r} for {org}"
            )
    return findings


def _repo_shadows(org: str) -> list[str]:
    """One finding per repository-level variable of either name in any repo of *org*."""
    repos_endpoint = f"orgs/{org}/repos?type=all"
    shadows: list[str] = []
    for repo in _read_list(repos_endpoint, None):
        full_name = _string_field(repo, "full_name", repos_endpoint)
        endpoint = f"repos/{full_name}/actions/variables"
        for item in _read_list(endpoint, "variables"):
            name = _string_field(item, "name", endpoint).upper()
            if name in ORG_VARIABLES:
                shadows.append(
                    f"{full_name}: repository variable {name} shadows the org variable"
                    " in this repo's jobs"
                )
    return shadows


def check(org: str) -> CheckResult:
    """Check *org*'s Actions copies of the configuration, reading GitHub only.

    A finding for each of :data:`ORG_VARIABLES` that is missing, invalid or
    not visible to every repo, for a ``VRG_AUTOMATION_APP`` that differs from
    the org's ``automation_app`` in ``VRG_TRUSTED_AUTHORS``, and for every
    repository-level variable of either name. Raises
    :class:`TrustPublishError` when a read fails.
    """
    findings: list[str] = []
    values: dict[str, str] = {}
    variables = _org_variables(org)
    endpoint = f"orgs/{org}/actions/variables"
    for name in ORG_VARIABLES:
        item = variables.get(name)
        if item is None:
            findings.append(f"{org}: org variable {name} is missing")
            continue
        values[name] = _string_field(item, "value", endpoint)
        visibility = item.get("visibility")
        if visibility != VISIBILITY:
            findings.append(
                f"{org}: org variable {name} has visibility {visibility!r}, not {VISIBILITY!r}"
            )
    findings += _value_findings(org, values)
    shadows = _repo_shadows(org)
    return CheckResult(findings=findings + shadows, shadows=shadows, values=values)


def publish(org: str, authors: TrustedAuthors) -> CheckResult:
    """Set *org*'s org variables from *authors*, visibility ``all``; return the check before it.

    Runs :func:`check` first and raises :class:`PublishRefusedError`, writing
    nothing, when a repository-level variable shadows either org variable:
    the published copy would not reach that repo's jobs. Raises
    :class:`ValueError` when *org* is not listed, and
    :class:`TrustPublishError` when a read or a write fails.
    """
    expected = expected_values(org, authors)
    before = check(org)
    if before.shadows:
        listed = "; ".join(before.shadows)
        msg = f"refusing to publish to {org}: remove the repository-level variables first: {listed}"
        raise PublishRefusedError(msg)
    for name, value in expected.items():
        body: dict[str, object] = {"name": name, "value": value, "visibility": VISIBILITY}
        if name in before.values:
            method, endpoint = "PATCH", f"orgs/{org}/actions/variables/{name}"
        else:
            method, endpoint = "POST", f"orgs/{org}/actions/variables"
        try:
            github.write_json(method, endpoint, body)
        except subprocess.CalledProcessError as exc:
            msg = f"{method} {endpoint} failed: {error_text(exc)}"
            raise TrustPublishError(msg) from exc
    return before
