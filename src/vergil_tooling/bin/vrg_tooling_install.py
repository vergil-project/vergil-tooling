"""Install a vergil-tooling ref on this host, locked to its commit's ``uv.lock``.

``vrg-tooling-install vX.Y.Z`` fetches the release tag over git, exports that
commit's ``uv.lock`` as constraints, and runs ``uv tool install --force`` of the
commit under them on uv's managed Python 3.15, with no uv config file and no
``UV_PYTHON*`` variable able to change the interpreter
(:func:`vergil_tooling.lib.tooling_install.install_ref`).
``vrg-tooling-install vX.Y`` first resolves the line through its rolling tag
``vX.Y`` to the one ``vX.Y.Z`` release on that tag's commit, then installs and
reports that exact release.

Any other git ref of vergil-tooling (vergil-tooling#4841) installs the same
way: a branch such as ``develop``, another tag, or a full commit SHA. It is
resolved against the fixed vergil-tooling remote to one commit, which is
installed under that commit's own ``uv.lock`` and reported with the ref. The
argument is a ref name only, never a URL or another repository, and anything
starting with ``-`` is refused. It is a convenience for a hand install: every
workflow and script keeps passing a release, so the next normal install
replaces it.

vergil-tooling's own release runs it on the Mac, through ``vergil.toml``'s
``[publish].install``, with the just-released version, and any other
host runs it to move to a new release. It is the installed vergil-tooling
installing the new one: if the installed copy is broken, install by hand
(docs/site/docs/getting-started.md) while the bug is fixed.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.tooling_install import ToolingInstallError, install_ref


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        installed = install_ref(args.ref)
    except ToolingInstallError as exc:
        print(f"vrg-tooling-install: {exc}", file=sys.stderr)
        return 1
    print(f"vrg-tooling-install: installed {installed.ref} (commit {installed.sha})")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-tooling-install",
        description=(
            "Install a vergil-tooling ref on this host with uv, locked to the "
            "uv.lock of the commit it names, on uv's managed Python 3.15. A "
            "line vX.Y installs the release its rolling tag vX.Y names; any "
            "other ref (a branch, a tag or a full commit SHA) installs the "
            "commit it names on the vergil-tooling remote."
        ),
    )
    parser.add_argument(
        "ref",
        metavar="ref",
        help=(
            "a release line, e.g. v2.1, an exact release tag, e.g. v2.1.330, "
            "or any other branch, tag or full commit SHA of vergil-tooling, "
            "e.g. develop (a ref name only, never a URL)"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
