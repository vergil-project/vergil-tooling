"""Stage changelog and release notes into the docs build directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.ci_evidence import evidence_asset_name
from vergil_tooling.lib.docs import stage_docs
from vergil_tooling.lib.github import GitHubAPIError
from vergil_tooling.lib.output import emit_error, write_output


def _has_evidence_asset(repo: str, tag: str) -> bool:
    """Return whether release *tag* carries the CI-evidence bundle asset.

    One cheap ``gh release view`` per release at docs-build time. A release that
    does not exist yet (404) legitimately has no asset, so that is reported as
    ``False``; any other ``gh`` failure propagates rather than being silently
    swallowed as "no evidence".
    """
    try:
        data = github.read_json("release", "view", tag, "--repo", repo, "--json", "assets")
    except GitHubAPIError as exc:
        stderr = (exc.stderr or "").lower()
        if "not found" in stderr or "404" in stderr:
            return False
        raise
    raw_assets = data.get("assets") if isinstance(data, dict) else None
    assets = cast("list[dict[str, Any]]", raw_assets) if isinstance(raw_assets, list) else []
    asset_name = evidence_asset_name(tag)
    return any(asset.get("name") == asset_name for asset in assets)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-docs-stage",
        description="Stage changelog and release notes into the docs build directory.",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        required=True,
        help="Target docs source directory",
    )
    parser.add_argument(
        "--releases-dir",
        type=Path,
        default=Path("releases"),
        help="Source releases directory",
    )
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="Source changelog file",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="owner/name of the repo (default: resolved from the git remote); "
        "used to link each release page to its CI-evidence bundle",
    )
    args = parser.parse_args(argv)

    if not args.docs_dir.is_dir():
        emit_error(f"docs directory not found: {args.docs_dir}")
        return 1

    changelog = args.changelog if args.changelog.is_file() else None
    repo = args.repo or github.current_repo()
    count = stage_docs(
        docs_dir=args.docs_dir,
        releases_dir=args.releases_dir,
        changelog=changelog,
        repo=repo,
        has_evidence_asset=lambda tag: _has_evidence_asset(repo, tag),
    )
    write_output("releases_staged", str(count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
