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
    print(roadmap.render(roadmap.gather(org), org))
    return 0


if __name__ == "__main__":
    sys.exit(main())
