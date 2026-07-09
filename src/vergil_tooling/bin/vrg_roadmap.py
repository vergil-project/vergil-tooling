"""Print the generated project roadmap (open epics in a repo's resolved epic home).

A pure read-and-render command — see :mod:`vergil_tooling.lib.roadmap`. The
nightly job that writes/publishes the rendered markdown is separate (T8c).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github, roadmap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the generated project roadmap.")
    parser.add_argument(
        "--org",
        help="GitHub org whose .github epics to read (default: current repo's owner).",
    )
    parser.add_argument(
        "--repo",
        help=(
            "Target repo 'owner/repo' whose roadmap to show (its resolved epic "
            "home; a private repo self-homes its epics). Mutually exclusive with --org."
        ),
    )
    args = parser.parse_args(argv)
    if args.org and args.repo:
        print("vrg-roadmap: --org and --repo are mutually exclusive", file=sys.stderr)
        return 1
    if args.repo:
        if "/" not in args.repo:
            print(
                f"vrg-roadmap: --repo must be 'owner/repo' (got {args.repo!r})",
                file=sys.stderr,
            )
            return 1
        owner, bare = args.repo.split("/", 1)
        org, home = owner, epics.resolve_epic_home(owner, bare)
    else:
        org, home = args.org or github.current_org(), None
    # Scope the App token to the org being read so a cross-org --org/--repo selects
    # that org's installation, not the cwd repo's (#2070).
    try:
        with github.target_org(org):
            print(roadmap.render(roadmap.gather(org, home=home), org, home=home))
    except github.NoInstallationError as exc:
        print(f"vrg-roadmap: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
