"""Print the generated project roadmap (the epics in an org's ``.github``).

A pure read-and-render command — see :mod:`vergil_tooling.lib.roadmap`. The
nightly job that writes/publishes the rendered markdown is separate (T8c).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epic_docs, github, roadmap, trust
from vergil_tooling.lib.cli import cli_main


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the generated project roadmap.")
    parser.add_argument(
        "--org",
        help="GitHub org whose .github epics to read (default: current repo's owner).",
    )
    args = parser.parse_args(argv)
    # Resolve the org and render inside one try: every step here shells out to
    # gh, so a missing token or missing installation must surface as a clean
    # message, not a traceback. Scope the App token to the org being read so a
    # cross-org --org selects that org's installation, not the cwd repo's
    # (#2070).
    try:
        org = args.org or github.current_org()
        with github.target_org(org):
            print(roadmap.render(roadmap.gather(org), roadmap.gather_completed(org), org))
    except github.MissingGitHubTokenError:
        print(github.missing_token_message("vrg-roadmap"), file=sys.stderr)
        return 1
    except github.NoInstallationError as exc:
        print(f"vrg-roadmap: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    except (trust.TrustReadError, epic_docs.EpicDocsFormatError) as exc:
        # A Completed epic that could not be read, or whose Documents section
        # is malformed, fails the page rather than vanishing from it.
        print(f"vrg-roadmap: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
