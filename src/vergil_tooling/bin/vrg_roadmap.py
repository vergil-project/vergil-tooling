"""Print the generated project roadmap (open epics in the org ``.github`` repo).

A pure read-and-render command — see :mod:`vergil_tooling.lib.roadmap`. The
nightly job that writes/publishes the rendered markdown is separate (T8c).
"""

from __future__ import annotations

import sys

from vergil_tooling.lib import roadmap


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    print(roadmap.render(roadmap.gather()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
