"""``vrg-agent-audit`` — the daily audit of agent merges (epic vergil-project/.github#1384).

``vrg-agent-audit --repo <owner/name>`` audits every merge into ``develop``
and ``main`` of the repository since its markers, and moves each marker to
where its audit reached. See :mod:`vergil_tooling.lib.agent_audit` for the
checks. It runs in a full-history clone of that repository (its ``origin``
must name it), fetches what it reads, and changes nothing in the clone's
branches or worktree.

``vrg-agent-audit --repo <owner/name> --init`` creates each missing marker at
its branch's current tip and audits nothing. It is used only by repo init and
the one-time go-live loop; a scheduled run never creates a marker.

The human and agent logins come from the trusted-authors configuration
(:func:`~vergil_tooling.lib.trusted_authors.load_trusted_authors`; in
Actions, the delivered ``VRG_TRUSTED_AUTHORS`` copy). The audit refuses when
it is missing, or names no human or more than one.

It is a security-signal job: it files no issue and posts nothing. Each
finding is one line on stdout.

Exit codes: ``0`` clean, ``1`` any finding or read failure, ``2`` a usage
error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import agent_audit, git, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.draft_ref import require_remote_is
from vergil_tooling.lib.rulings.identity import human_login
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

_PROG = "vrg-agent-audit"

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo: str = args.repo
    # Mint the App token for the audited repo's owner, not the cwd's org. A
    # GitHub auth failure exits 1 through cli_main, as any failure does here.
    try:
        with github.target_org(repo.split("/", 1)[0]):
            if args.init:
                return _init(repo)
            return _audit(repo)
    except agent_audit.AuditError as exc:
        _fail(str(exc))
    return EXIT_FINDINGS


def _fail(message: str) -> None:
    print(f"{_PROG}: {message}", file=sys.stderr)


def _init(repo: str) -> int:
    for marker, sha, created in agent_audit.init_markers(repo):
        state = "created at" if created else "already set at"
        print(f"{repo}: {marker} {state} {sha}")
    return EXIT_CLEAN


def _audit(repo: str) -> int:
    try:
        authors = load_trusted_authors()
        human_login(authors)
    except (TrustedAuthorsNotConfiguredError, RulingError) as exc:
        _fail(f"the trusted-authors configuration cannot be used: {exc}")
        return EXIT_FINDINGS
    try:
        require_remote_is(repo, command=_PROG)
    except RulingError as exc:
        _fail(str(exc))
        return EXIT_FINDINGS
    try:
        shallow = git.read_output("rev-parse", "--is-shallow-repository")
    except subprocess.CalledProcessError as exc:
        _fail(f"cannot read the clone: git exited {exc.returncode}")
        return EXIT_FINDINGS
    if shallow != "false":
        _fail("the clone is shallow; the audit needs its full history (fetch-depth: 0)")
        return EXIT_FINDINGS
    results = agent_audit.audit(Path.cwd(), repo, authors)
    findings = [finding for result in results for finding in result.findings]
    for finding in findings:
        print(finding.render())
    for result in results:
        reached = result.audited_to[:12] if result.audited_to else "unchanged"
        print(f"{repo} {result.branch}: {len(result.findings)} finding(s); marker {reached}")
    return EXIT_FINDINGS if findings else EXIT_CLEAN


def _repo_arg(value: str) -> str:
    owner, sep, name = value.partition("/")
    if not sep or not owner or not name or "/" in name:
        msg = f"expected OWNER/REPO, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Audit every merge into develop and main since the audit's markers:"
            " the merge and auto-merge actors of every PR, and, on an"
            " agent-involved PR merged into develop, its merge-review coverage."
            " Run it in a full-history"
            " clone of the repository. It files no issue and posts nothing."
        ),
        epilog="Exit codes: 0 clean, 1 any finding or read failure, 2 usage error.",
    )
    parser.add_argument(
        "--repo", required=True, type=_repo_arg, help="the repository to audit, OWNER/REPO"
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help=(
            "create each missing marker at its branch's current tip and audit nothing"
            " (repo init and the one-time go-live only)"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
