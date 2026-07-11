"""Ensure a repo's ad-hoc epic exists (create-if-missing), idempotently.

The ``Epic (ad hoc): <repo>`` umbrella (labelled ``epic`` + ``ad-hoc``) lives in
the repo's resolved epic home — ``<org>/.github`` for a public repo, the repo
itself when private — one per repo. This provisions it before linking
pre-existing issues — e.g. ``migrate-repo`` step 1,
which must ensure the epic exists before it can link ad-hoc tasks to it. Routing
work to ``adhoc`` (``vrg-issue-create``/``vrg-epic-move --epic adhoc``) also
ensures it via the same path.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github


def cmd_ensure(args: argparse.Namespace) -> int:
    repo = args.repo or github.current_repo()
    if "/" not in repo:
        print(
            f"vrg-adhoc-epic: --repo must be 'owner/repo' (got {repo!r})",
            file=sys.stderr,
        )
        return 1
    owner, bare = repo.split("/", 1)
    home = epics.resolve_epic_home(owner, bare)
    print(f"-> epic home: {home} [{github.repo_visibility(home)}]")
    epic = epics.ensure_adhoc_epic(repo)
    print(f"Ad-hoc epic: {epic.slug}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-adhoc-epic",
        description=(
            "Manage a repo's ad-hoc epic (Epic (ad hoc): <repo>, labelled "
            "epic + ad-hoc, homed by visibility: <org>/.github for a public "
            "repo, the repo itself when private)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_ensure = sub.add_parser(
        "ensure",
        help="Ensure the repo's ad-hoc epic exists (create-if-missing, idempotent).",
    )
    p_ensure.add_argument("--repo", help="Target repo owner/name (defaults to the current repo)")
    p_ensure.set_defaults(func=cmd_ensure)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
