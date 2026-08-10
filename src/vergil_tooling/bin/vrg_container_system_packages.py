"""Print a repo's declared [container].system-packages, for CI consumption.

Default: the package list, one per line. --install-script: the exact apt
install snippet (the single speller shared with the local cache build).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.config import container_system_packages
from vergil_tooling.lib.container import container_platform
from vergil_tooling.lib.container_cache import apt_install_command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print declared [container].system-packages.")
    parser.add_argument(
        "--install-script",
        action="store_true",
        help="print the apt install snippet instead of the package list",
    )
    parser.add_argument("--repo-root", default=".", help="repo root (default: CWD)")
    args = parser.parse_args(argv)

    root = Path(args.repo_root)
    packages = container_system_packages(root)
    if args.install_script:
        script = apt_install_command(packages, container_platform())
        if script:
            print(script)
    else:
        for pkg in packages:
            print(pkg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
