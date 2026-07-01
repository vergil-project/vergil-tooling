"""Print the generated project roadmap (open epics in the org ``.github`` repo).

A pure read-and-render command — see :mod:`vergil_tooling.lib.roadmap`. The
nightly job that writes/publishes the rendered markdown is separate (T8c).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import github, roadmap


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the generated project roadmap.")
    parser.add_argument(
        "--org",
        help="GitHub org whose .github epics to read (default: current repo's owner).",
    )
    args = parser.parse_args(argv)
    org = args.org or github.current_org()
    # Scope the App token to the org being read so a cross-org --org selects
    # that org's installation, not the cwd repo's (#2070).
    try:
        with github.target_org(org):
            print(roadmap.render(roadmap.gather(org), org))
    except github.NoInstallationError as exc:
        print(f"vrg-roadmap: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
