"""Resolve a repo's CI versions from the published version window.

The single resolver's command-line face (epic vergil-project/.github#407, spec
§6.3). ``--format json`` (the default) prints one JSON object::

    {"versions": [...], "primary-version": "...", "primary-container-tag": "...",
     "container-tags": {...}, "container-suffixes": {...}}

``--format github-output`` prints ``versions``, ``primary-version`` and
``primary-container-tag`` as ``key=value`` lines for ``$GITHUB_OUTPUT``, with
the names and meanings the CI setup step's ``derive.sh`` emits. A repo with no
language resolves to ``latest``.

Any window or resolution failure exits 1 with the reason on stderr. Set
``VRG_VERSION_WINDOW=<path>`` to resolve against a local ``window.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError, read_config
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.resolve import ci_outputs, resolve_config

_GITHUB_OUTPUT_KEYS = ("versions", "primary-version", "primary-container-tag")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = args.repo_root or Path.cwd()
    try:
        cfg = read_config(repo_root)
        resolution = resolve_config(cfg, repo_root=repo_root)
    except (FileNotFoundError, ConfigError, VersionsError) as exc:
        print(f"vrg-ci-resolve: error: {exc}", file=sys.stderr)
        return 1
    print(_render(ci_outputs(resolution), args.format))
    return 0


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-ci-resolve",
        description="Resolve the CI versions of a repo from the published version window.",
    )
    parser.add_argument(
        "--format",
        choices=("json", "github-output"),
        default="json",
        help="Output format (default: json).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Directory holding vergil.toml (default: the current directory).",
    )
    return parser.parse_args(argv)


def _render(outputs: dict[str, object], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(outputs, indent=2)
    lines = []
    for key in _GITHUB_OUTPUT_KEYS:
        value = outputs[key]
        text = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        lines.append(f"{key}={text}")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
