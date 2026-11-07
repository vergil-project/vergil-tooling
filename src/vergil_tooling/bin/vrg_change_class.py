"""Print a branch's merge-review change class: ``docs-only`` or ``code``.

``vrg-change-class --base <ref> [--head <ref>]`` lists the files *head*
changed since its merge base with *base* (``git diff --name-status -M
<base>...<head>``) and classifies them against the documentation allow-list:
the built-in paths (``README.md``, ``CHANGELOG.md``) and the repository's own
``[merge-review]`` ``docs-paths`` from ``vergil.toml``. It prints
``docs-only`` or ``code`` on stdout and exits 0.

The change is ``docs-only`` only when every changed file is an addition or
an edit of an allow-listed path; any deletion, rename or type change, any
change to ``vergil.toml``, or an empty change is ``code``. See
``docs/specs/merge-review-protocol.md``.

It is for humans and debugging: the merge-review skill gets the class
through the request builder, which calls the same classifier. It reads the
repository and writes nothing.

Exit codes: ``0`` the class was printed, ``1`` an error (an unknown ref, no
merge base, an invalid ``vergil.toml``), ``2`` a usage error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from vergil_tooling.lib import git
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError, read_config
from vergil_tooling.lib.merge_review.change_class import allow_list, classify

EXIT_OK = 0
EXIT_ERROR = 1


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        repo_root = git.repo_root()
        docs_paths = allow_list(read_config(repo_root).merge_review.docs_paths)
        changes = git.diff_name_status(args.base, args.head)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: git failed (exit {exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        return EXIT_ERROR
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(classify(changes, docs_paths))
    return EXIT_OK


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-change-class",
        description="Print a branch's merge-review change class: docs-only or code.",
    )
    parser.add_argument("--base", required=True, help="the base ref, such as origin/develop")
    parser.add_argument("--head", default="HEAD", help="the branch ref (default: HEAD)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
