"""Install a vergil-tooling release on this host, locked to its ``uv.lock``.

``vrg-tooling-install vX.Y.Z`` fetches the release tag over git, exports that
commit's ``uv.lock`` as constraints, and runs ``uv tool install --force`` of the
commit under them on uv's managed Python 3.15, with no uv config file and no
``UV_PYTHON*`` variable able to change the interpreter
(:func:`vergil_tooling.lib.tooling_install.install_release`).
``vrg-tooling-install vX.Y`` first resolves the line through its rolling tag
``vX.Y`` to the one ``vX.Y.Z`` release on that tag's commit, then installs and
reports that exact release.

vergil-tooling's own release runs it on the Mac, through ``vergil.toml``'s
``[publish].consumer-refresh``, with the just-released version, and any other
host runs it to move to a new release. It is the installed vergil-tooling
installing the new one: if the installed copy is broken, install by hand
(docs/site/docs/getting-started.md) while the bug is fixed.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.tooling_install import ToolingInstallError, install_release


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        installed = install_release(args.tag)
    except ToolingInstallError as exc:
        print(f"vrg-tooling-install: {exc}", file=sys.stderr)
        return 1
    print(f"vrg-tooling-install: installed {installed.tag} (commit {installed.sha})")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-tooling-install",
        description=(
            "Install a vergil-tooling release on this host with uv, locked to "
            "that release's uv.lock, on uv's managed Python 3.15. A line vX.Y "
            "installs the release its rolling tag vX.Y names."
        ),
    )
    parser.add_argument(
        "tag",
        metavar="vX.Y or vX.Y.Z",
        help="a release line, e.g. v2.1, or an exact release tag, e.g. v2.1.330",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
