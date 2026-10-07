"""Write a copy of a SARIF file without its accepted-suppressed results.

GitHub code scanning raises an alert for every SARIF result, even one carrying
an accepted in-source suppression (``# nosemgrep: <rule-id>``). The SARIF gate
(``vrg-sarif-evaluate``) honors those suppressions, so the CI security
composites upload this filtered copy to code scanning while the unfiltered
original stays in the CI-evidence bundle.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib.output import emit_error
from vergil_tooling.lib.sarif import filter_suppressed, parse_sarif


def _write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a sibling temp file and an atomic replace."""
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-sarif-filter",
        description=(
            "Write a copy of a SARIF file with every accepted-suppressed result "
            "removed (suppression kind inSource/external, status absent or "
            "accepted). All other content is preserved. INPUT and OUTPUT may be "
            "the same path."
        ),
    )
    parser.add_argument("input", type=Path, metavar="INPUT", help="SARIF file to read")
    parser.add_argument("output", type=Path, metavar="OUTPUT", help="SARIF file to write")
    args = parser.parse_args(argv)

    src: Path = args.input
    dst: Path = args.output

    try:
        sarif = parse_sarif(src)
        filtered, removed = filter_suppressed(sarif)
    except (OSError, ValueError) as exc:
        emit_error(f"cannot read SARIF from {src}: {exc}")
        return 1

    try:
        _write_atomic(dst, json.dumps(filtered, indent=2, ensure_ascii=False) + "\n")
    except OSError as exc:
        emit_error(f"cannot write SARIF to {dst}: {exc}")
        return 1

    print(f"vrg-sarif-filter: removed {removed} suppressed result(s) from {src} -> {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
