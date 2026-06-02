"""Deprecated alias for vrg-finalize-pr.

This module exists for backward compatibility during the 2.0→2.1
transition. Use ``vrg-finalize-pr`` instead.
"""

from __future__ import annotations

import sys

from vergil_tooling.bin.vrg_finalize_pr import main as _main


def main(argv: list[str] | None = None) -> int:
    print(
        "WARNING: vrg-finalize-repo is deprecated. Use vrg-finalize-pr instead.",
        file=sys.stderr,
    )
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
