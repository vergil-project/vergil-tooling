"""Print the GitHub identities whose issues and comments automation may trust.

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
"""

from __future__ import annotations

import argparse
import json
import sys

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    as_json_object,
    load_trusted_authors,
)


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        authors = load_trusted_authors()
    except (TrustedAuthorsNotConfiguredError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print the GitHub identities whose issues and comments automation may trust"
            " (the [trusted_authors] and [orgs.*] configuration). Exits 1 when none is"
            " configured."
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
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
