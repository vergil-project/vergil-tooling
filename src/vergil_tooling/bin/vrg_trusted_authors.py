"""Print, publish and check the GitHub identities whose content automation may trust.

``vrg-trusted-authors`` is the one accessor for the trusted-authors
configuration (:mod:`vergil_tooling.lib.trusted_authors`): the VM's
``~/.config/vergil/trusted-authors.toml`` if it exists, else the
``[trusted_authors]`` and ``[orgs.*]`` stanzas of
``~/.config/vergil/identities.toml``, else, in a GitHub Actions job only,
``$VRG_TRUSTED_AUTHORS``.

- ``vrg-trusted-authors`` prints ``human: <login>`` and ``agent: <login>``,
  one login per line, then ``org: <org> automation: <slug>`` for each Vergil
  org.
- ``vrg-trusted-authors --json`` prints
  ``{"human": [...], "agents": [...], "orgs": {"<org>": {"automation_app": "<slug>"}}}``.

When nothing is configured, or the configuration is invalid, it prints the
error on stderr and exits 1. It never prints a default.

Three subcommands handle the GitHub Actions copies
(:mod:`vergil_tooling.lib.trust_publish`, spec §5.1a), each for ``--org`` or,
without it, every listed org:

- ``publish`` writes the org variables ``VRG_TRUSTED_AUTHORS`` and
  ``VRG_AUTOMATION_APP`` from the configuration. It runs only as the human,
  and refuses an org where a repository-level variable shadows either one.
- ``check`` reads them back and prints one finding per problem. It runs as the
  human or the org's automation App, never an agent identity. Exit 0 clean,
  1 with findings, 2 when it could not run. ``--values-out PATH`` writes the
  current values as ``{"<org>": {"<name>": "<value>"}}``.
- ``render`` prints the values the configuration implies, in the same shape,
  with no GitHub call.

``app-secrets`` (:mod:`vergil_tooling.lib.app_secrets`) checks that the
automation App's Actions secrets ``APP_CLIENT_ID`` and ``APP_PRIVATE_KEY``
reach each repo that runs an in-scope unattended job, in ``--org``, in every
listed org, or in each ``--repo``. The secrets belong only in each repo's
``automation`` environment: it prints each as ``environment`` or
``MISSING``, and reports as a finding each one missing and each repository
or organization secret of the same name the repo can see. It reads secret
names only, never a value. Like ``check``, it runs as the human or the
org's automation App, never an agent identity. Exit 0 when every repo
passes, 1 on any finding, 2 when it could not run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vergil_tooling.lib import app_secrets, gh_version, github, identity_mode, trust_publish
from vergil_tooling.lib.automation_identity import AutomationIdentityError
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.github_config import AUTOMATION_ENVIRONMENT_NAME
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
    as_json_object,
    load_trusted_authors,
    vergil_orgs,
)

_AGENT_MODES = frozenset({identity_mode.IdentityMode.USER, identity_mode.IdentityMode.AUDIT})
_CHECK_COULD_NOT_RUN = 2


class _RefusedError(Exception):
    """The command refuses to run in this identity mode, or for this org."""


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "check":
        return _check(args)
    if args.command == "app-secrets":
        return _app_secrets(args)
    try:
        authors = load_trusted_authors()
    except (TrustedAuthorsNotConfiguredError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.command == "publish":
        return _publish(args, authors)
    if args.command == "render":
        return _render(args, authors)

    if args.json:
        print(json.dumps(as_json_object(authors)))
        return 0

    for login in authors.human:
        print(f"human: {login}")
    for login in authors.agents:
        print(f"agent: {login}")
    for org, config in authors.orgs.items():
        print(f"org: {org} automation: {config.automation_app}")
    return 0


def _orgs(args: argparse.Namespace, authors: TrustedAuthors) -> tuple[str, ...]:
    return (args.org,) if args.org else vergil_orgs(authors)


def _mode() -> identity_mode.IdentityMode:
    try:
        return identity_mode.current_mode()
    except AutomationIdentityError as exc:
        msg = f"the identity could not be resolved: {exc}"
        raise _RefusedError(msg) from exc


def _render(args: argparse.Namespace, authors: TrustedAuthors) -> int:
    try:
        rendered = {
            org: trust_publish.expected_values(org, authors) for org in _orgs(args, authors)
        }
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(rendered, ensure_ascii=False, indent=2))
    return 0


def _publish(args: argparse.Namespace, authors: TrustedAuthors) -> int:
    try:
        mode = _mode()
        if mode is not identity_mode.IdentityMode.HUMAN:
            msg = (
                f"publish runs only as the human, and the identity mode is {mode.value!r}:"
                " publishing the org variables is a setup step the human runs"
            )
            raise _RefusedError(msg)
        for org in _orgs(args, authors):
            with github.target_org(org):
                trust_publish.publish(org, authors)
            names = " and ".join(trust_publish.ORG_VARIABLES)
            print(f"{org}: set the org variables {names} (visibility {trust_publish.VISIBILITY})")
    except (
        _RefusedError,
        ValueError,
        trust_publish.PublishRefusedError,
        trust_publish.TrustPublishError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _refuse_agent_mode(command: str, permission: str) -> None:
    mode = _mode()
    if mode in _AGENT_MODES:
        msg = (
            f"{command} runs as the org automation App or the human, and the identity mode is"
            f" {mode.value!r}, an agent mode, which holds no {permission} permission"
        )
        raise _RefusedError(msg)


def _check(args: argparse.Namespace) -> int:
    try:
        _refuse_agent_mode("check", "Variables")
        orgs = (args.org,) if args.org else vergil_orgs(load_trusted_authors())
        findings: list[str] = []
        values: dict[str, dict[str, str]] = {}
        for org in orgs:
            with github.target_org(org):
                result = trust_publish.check(org)
            findings += result.findings
            values[org] = result.values
    except (
        _RefusedError,
        TrustedAuthorsNotConfiguredError,
        ValueError,
        trust_publish.TrustPublishError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return _CHECK_COULD_NOT_RUN
    if args.values_out:
        Path(args.values_out).write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n")
    for finding in findings:
        print(f"FINDING: {finding}")
    if findings:
        return 1
    print(f"trusted-authors variables are clean in {', '.join(orgs)}")
    return 0


def _app_secrets(args: argparse.Namespace) -> int:
    try:
        _refuse_agent_mode("app-secrets", "Secrets")
        results: list[app_secrets.RepoSecrets] = []
        if args.repo:
            for repo in args.repo:
                with github.target_org(repo.split("/", 1)[0]):
                    results.append(app_secrets.check_repo(repo))
        else:
            orgs = (args.org,) if args.org else vergil_orgs(load_trusted_authors())
            for org in orgs:
                with github.target_org(org):
                    repos = app_secrets.in_scope_repos(org)
                    if not repos:
                        print(f"{org}: no repo runs an in-scope job")
                    results += [app_secrets.check_repo(repo) for repo in repos]
    except (
        _RefusedError,
        TrustedAuthorsNotConfiguredError,
        ValueError,
        app_secrets.AppSecretsError,
        gh_version.GhVersionError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return _CHECK_COULD_NOT_RUN
    # A GitHub login that failed read no secret, so it is "could not run" (2),
    # never "a secret is missing" (1), the code cli_main would give it. A
    # missing token reaches here as an AppSecretsError: it is a gh failure.
    except github.NoInstallationError as exc:
        print(f"ERROR: {github.no_installation_message(exc)}", file=sys.stderr)
        return _CHECK_COULD_NOT_RUN
    except github.AppAuthError as exc:
        print(f"ERROR: {github.app_auth_message(exc)}", file=sys.stderr)
        return _CHECK_COULD_NOT_RUN
    env = AUTOMATION_ENVIRONMENT_NAME
    findings: list[str] = []
    for result in results:
        sources = " ".join(f"{name}={source}" for name, source in result.sources.items())
        absent = "" if result.has_environment else f" (no {env} environment)"
        print(f"{result.repo}: {sources}{absent}")
        findings += [
            f"{result.repo}: the {env} environment secret {name} is missing"
            for name in result.missing
        ]
        findings += [
            f"{result.repo}: {name} is also {_LEVEL_TEXT[level]} secret, which only"
            f" the {env} environment may hold"
            for name, level in result.forbidden
        ]
    for finding in findings:
        print(f"FINDING: {finding}")
    if findings:
        return 1
    print(f"the App secrets reach every repo checked ({len(results)}), from the {env} environment")
    return 0


_LEVEL_TEXT = {
    app_secrets.REPO: "a repository",
    app_secrets.ORG: "an organization",
}


def _repo_arg(value: str) -> str:
    owner, sep, name = value.partition("/")
    if not sep or not owner or not name or "/" in name:
        msg = f"expected OWNER/NAME, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print the GitHub identities whose issues and comments automation may trust"
            " (the [trusted_authors] and [orgs.*] configuration), or publish, check or"
            " render their GitHub Actions copies. Exits 1 when none is configured."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            'Print {"human": [...], "agents": [...], "orgs": {"<org>": {"automation_app":'
            ' "<slug>"}}} instead of one line per login.'
        ),
    )
    commands = parser.add_subparsers(dest="command")
    org_help = "One Vergil org (default: every org the configuration lists)."
    publish = commands.add_parser(
        "publish",
        help="Write the org variables VRG_TRUSTED_AUTHORS and VRG_AUTOMATION_APP (human only).",
    )
    publish.add_argument("--org", help=org_help)
    check = commands.add_parser(
        "check",
        help=(
            "Check the org variables and flag repository-level shadows"
            " (exit 0 clean, 1 findings, 2 could not run)."
        ),
    )
    check.add_argument("--org", help=org_help)
    check.add_argument(
        "--values-out",
        metavar="PATH",
        help='Write the current values as {"<org>": {"<name>": "<value>"}} to PATH.',
    )
    secrets = commands.add_parser(
        "app-secrets",
        help=(
            "Check that APP_CLIENT_ID and APP_PRIVATE_KEY reach each repo that runs an"
            " in-scope job from its automation environment alone, names only (exit 0 all"
            " pass, 1 any finding, 2 could not run)."
        ),
    )
    scope = secrets.add_mutually_exclusive_group()
    scope.add_argument(
        "--org",
        help=(
            "One Vergil org: every repo of it that runs an in-scope job"
            " (default: every org the configuration lists)."
        ),
    )
    scope.add_argument(
        "--repo",
        action="append",
        type=_repo_arg,
        metavar="OWNER/NAME",
        help="Check this repo, whatever its workflows (repeatable).",
    )
    render = commands.add_parser(
        "render",
        help="Print the org variables the configuration implies, with no GitHub call.",
    )
    render.add_argument("--org", help=org_help)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
