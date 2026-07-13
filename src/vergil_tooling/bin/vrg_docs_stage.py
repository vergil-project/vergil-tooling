"""Stage changelog and release notes into the docs build directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import github
from vergil_tooling.lib.ci_evidence import evidence_asset_name
from vergil_tooling.lib.docs import stage_docs
from vergil_tooling.lib.github import GitHubAPIError
from vergil_tooling.lib.output import emit_error, write_output

if TYPE_CHECKING:
    from collections.abc import Callable


def _evidence_asset_tags(repo: str) -> frozenset[str]:
    """Return the set of release tags that carry the CI-evidence bundle asset.

    A **single** paginated ``gh api`` call lists every release and its assets, so
    membership is resolved with one network round-trip regardless of release
    count — replacing the old one-``gh release view``-per-release lookup, whose
    N calls multiplied GitHub's flakiness into the docs build. A tag whose
    release does not exist simply never appears in the listing (reported as
    no-evidence), so no per-tag 404 handling is needed. Any genuine ``gh``
    failure propagates rather than being swallowed as "no evidence".
    """
    data = github.read_json("api", f"repos/{repo}/releases", "--paginate")
    releases = data if isinstance(data, list) else []
    tags: set[str] = set()
    for item in releases:
        if not isinstance(item, dict):
            continue
        release = cast("dict[str, Any]", item)
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        raw_assets = release.get("assets")
        assets = raw_assets if isinstance(raw_assets, list) else []
        wanted = evidence_asset_name(tag)
        if any(isinstance(asset, dict) and asset.get("name") == wanted for asset in assets):
            tags.add(tag)
    return frozenset(tags)


def _evidence_resolver(repo: str) -> Callable[[str], bool]:
    """Build a ``tag -> has-evidence-asset`` resolver backed by one API call.

    The batched releases listing is fetched lazily on first use and cached, so a
    docs build with no release pages makes zero calls and any build with one or
    more makes exactly one, no matter how many releases exist.
    """
    tags: frozenset[str] | None = None

    def resolve(tag: str) -> bool:
        nonlocal tags
        if tags is None:
            tags = _evidence_asset_tags(repo)
        return tag in tags

    return resolve


def _is_auth_error(exc: GitHubAPIError) -> bool:
    """Return whether *exc* looks like an unauthenticated ``gh`` invocation."""
    text = (exc.stderr or "").lower()
    return "gh_token" in text or "gh auth login" in text


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
    try:
        count = stage_docs(
            docs_dir=args.docs_dir,
            releases_dir=args.releases_dir,
            changelog=changelog,
            repo=repo,
            has_evidence_asset=_evidence_resolver(repo),
        )
    except GitHubAPIError as exc:
        if _is_auth_error(exc):
            emit_error("GH_TOKEN required for evidence linking; gh is not authenticated")
            return 1
        raise
    write_output("releases_staged", str(count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
