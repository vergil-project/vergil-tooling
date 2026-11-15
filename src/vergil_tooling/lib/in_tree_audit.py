"""Audit vergil-tooling's managed files with the code in its own source tree.

vergil-tooling holds the master copies of the managed-file templates, and a
change to a master copy updates vergil-tooling's own managed files in the same
PR. Its ``develop`` is therefore ahead of the installed release after any
template change, and auditing those files with the installed release reports
drift that is not there. When ``vrg-release`` releases vergil-tooling, the
managed-file half of its audit runs here instead: with the templates and the
checks of the source tree being released, the ones that will ship (issue
#4414). The GitHub-side half, and every other repo's audit, keep using the
installed release.

The work is split across two processes:

- :func:`run`, called by the installed ``vrg-github-repo-config``, starts a
  Python process whose ``vergil_tooling`` package is the source tree under
  ``<root>/src`` (``PYTHONPATH``; ``-P`` keeps the working directory off
  ``sys.path``), running this module on *root*;
- :func:`main`, in that process, checks the package really was loaded from
  ``<root>/src`` (never silently from the installed release), runs
  :func:`~vergil_tooling.lib.repo_config.audit_local_config` on *root* and
  prints the local report.

The exit codes between the two are a fixed protocol, since the two sides are
different vergil-tooling versions: :data:`COMPLIANT`, :data:`NON_COMPLIANT`,
and anything else (a refused load, a traceback) means the audit could not
complete.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

#: The audited files match the source tree's templates.
COMPLIANT = 0

#: The audited files drift from the source tree's templates. Distinct from 1,
#: the exit code of an uncaught exception, so a crash never reads as drift.
NON_COMPLIANT = 10

#: The module the source-tree process runs.
MODULE = "vergil_tooling.lib.in_tree_audit"


def run(root: Path) -> int:
    """Audit *root*'s managed files with the source tree under ``<root>/src``.

    The child's report goes straight to this process's stdout and stderr.
    Returns its exit code: :data:`COMPLIANT`, :data:`NON_COMPLIANT`, or any
    other code when the audit could not complete.
    """
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    sys.stdout.flush()
    sys.stderr.flush()
    result = subprocess.run(  # noqa: S603
        (sys.executable, "-P", "-m", MODULE, str(root)),
        cwd=root,
        env=env,
        check=False,
    )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    """Audit the managed files of the root named in *argv* and print the report."""
    import vergil_tooling
    from vergil_tooling.bin.vrg_github_repo_config import print_local_diff
    from vergil_tooling.lib.output import emit_error
    from vergil_tooling.lib.repo_config import audit_local_config

    parser = argparse.ArgumentParser(prog=MODULE, description=main.__doc__)
    parser.add_argument("root", type=Path, help="the vergil-tooling checkout to audit")
    root: Path = parser.parse_args(argv).root.absolute()

    package = Path(vergil_tooling.__file__).absolute()
    source = root / "src"
    if not package.is_relative_to(source):
        emit_error(
            f"the in-tree audit must run vergil_tooling from {source}, "
            f"but loaded it from {package.parent}"
        )
        return 2

    diff = audit_local_config(root)
    print_local_diff(diff)
    return COMPLIANT if diff.is_compliant() else NON_COMPLIANT


if __name__ == "__main__":
    sys.exit(main())
