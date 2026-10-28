"""Print the GitHub logins whose issues and comments automation may trust.

``vrg-trusted-authors`` is the one accessor for the trusted-authors
configuration (:mod:`vergil_tooling.lib.trusted_authors`): the VM's
``~/.config/vergil/trusted-authors.toml`` if it exists, else the
``[trusted_authors]`` stanza of ``~/.config/vergil/identities.toml``.

- ``vrg-trusted-authors`` prints ``human: <login>`` and ``agent: <login>``,
  one login per line.
- ``vrg-trusted-authors --json`` prints ``{"human": [...], "agents": [...]}``.

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
        print(json.dumps({"human": list(authors.human), "agents": list(authors.agents)}))
        return 0

    for login in authors.human:
        print(f"human: {login}")
    for login in authors.agents:
        print(f"agent: {login}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Print the GitHub logins whose issues and comments automation may trust"
            " (the [trusted_authors] configuration). Exits 1 when none is configured."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help='Print {"human": [...], "agents": [...]} instead of one login per line.',
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
