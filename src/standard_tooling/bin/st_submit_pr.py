"""PR submission wrapper that constructs standards-compliant PR bodies.

Populates .github/pull_request_template.md programmatically.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

from standard_tooling.lib import git, github

ALLOWED_LINKAGES = ("Ref",)
_ISSUE_PLAIN_RE = re.compile(r"^[1-9]\d*$")
_ISSUE_CROSS_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9]\d*$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Create a standards-compliant pull request.")
    parser.add_argument(
        "--issue", required=True, help="Issue reference: number or owner/repo#number"
    )
    parser.add_argument("--summary", required=True, help="One-line PR summary")
    parser.add_argument(
        "--linkage", default="Ref", choices=ALLOWED_LINKAGES, help="Issue linkage keyword"
    )
    parser.add_argument("--notes", default="", help="Additional notes")
    parser.add_argument("--title", required=True, help="PR title")
    parser.add_argument("--dry-run", action="store_true", help="Print without executing")
    return parser.parse_args(argv)


def _resolve_issue_ref(issue: str) -> str:
    """Validate and normalize the issue reference."""
    if _ISSUE_PLAIN_RE.match(issue):
        return f"#{issue}"
    if _ISSUE_CROSS_RE.match(issue):
        return issue
    msg = f"--issue must be a number (42) or cross-repo ref (owner/repo#42), got '{issue}'."
    raise SystemExit(msg)


def _extract_testing_section(root: Path) -> str:
    """Extract the testing section from the PR template."""
    template = root / ".github" / "pull_request_template.md"
    if not template.is_file():
        return ""
    lines: list[str] = []
    in_testing = False
    for line in template.read_text(encoding="utf-8").splitlines():
        if re.match(r"^##\s+Testing", line):
            in_testing = True
            continue
        if in_testing and re.match(r"^##\s+", line):
            break
        if in_testing:
            lines.append(line)
    return "\n".join(lines).strip()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    issue_ref = _resolve_issue_ref(args.issue)
    root = git.repo_root()
    branch = git.current_branch()

    target_branch = "main" if branch.startswith("release/") else "develop"

    title = args.title

    testing_section = _extract_testing_section(root)

    notes_section = args.notes or "-"

    pr_body = (
        f"# Pull Request\n\n"
        f"## Summary\n\n- {args.summary}\n\n"
        f"## Issue Linkage\n\n- {args.linkage} {issue_ref}\n\n"
        f"## Testing\n\n{testing_section}\n\n"
        f"## Notes\n\n- {notes_section}"
    )

    if args.dry_run:
        print(f"=== PR Title ===\n{title}\n")
        print(f"=== Target Branch ===\n{target_branch}\n")
        print(f"=== PR Body ===\n{pr_body}")
        return 0

    print(f"Pushing branch '{branch}' to origin...")
    git.run("push", "-u", "origin", branch)

    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(pr_body)
        tmp_path = f.name

    try:
        print("Creating PR...")
        pr_url = github.create_pr(base=target_branch, title=title, body_file=tmp_path)
        print(f"PR created: {pr_url}")
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    print(f"Done. PR URL: {pr_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
