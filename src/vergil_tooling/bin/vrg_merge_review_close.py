"""Post merge review's closing comment, with its machine-readable record line.

``vrg-merge-review-close --issue <owner/name#N> --outcome <outcome>
--body-file <file>`` posts the closing comment of a merge-review run on the
task issue, as the agent, and prints the comment's ID. See
:mod:`vergil_tooling.lib.merge_review.record` (epic
vergil-project/.github#1384, spec §4.6).

``--outcome`` is the run's outcome, ``eligible`` or ``not-eligible
(<reason>)``; stopped, waiting, fatal and error are refused, since such a run
did not complete. ``--body-file`` holds the closing comment as the
merge-review skill writes it: the eligibility line for that outcome first,
then, on a fix to an open PR, the ``Branch as reviewed:`` line, then the
closing state of every item. Every line is kept, and the record line goes
right below the eligibility line (below the branch line, when there is one).

The head and the change fingerprint are computed here, never given: the
worktree's ``HEAD`` and its change since the merge base with
``origin/develop``, or, for an issue of a batch before handoff (it has a
batch marker), that issue's own change since the previous issue's commit.
The worktree must be clean, and the clone's ``origin`` the ``--issue``
repository.

The comment is posted exactly once. A re-run that finds the same comment
already posted by the agent prints its ID and posts nothing.

Runs only in the ``user`` identity mode, as the agent login
``<the human login>-vergil-user``.

Exit codes: ``0`` posted (or already posted), ``1`` a refusal or error, ``2``
a usage error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import git, github, identity_mode, trust
from vergil_tooling.lib.change_fingerprint import FingerprintError
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.merge_review import record
from vergil_tooling.lib.rulings import RulingError, comments
from vergil_tooling.lib.rulings.draft_ref import require_remote_is
from vergil_tooling.lib.rulings.identity import agent_login
from vergil_tooling.lib.rulings.post import post_comment_once
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthorsNotConfiguredError,
    load_trusted_authors,
)

_PROG = "vrg-merge-review-close"

EXIT_OK = 0
EXIT_ERROR = 1


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(_close(args.issue, args.outcome, Path(args.body_file)))
    except github.MissingGitHubTokenError:
        # A CalledProcessError too: cli_main reports it, not the git handler.
        raise
    except subprocess.CalledProcessError as exc:
        print(f"{_PROG}: git failed (exit {exc.returncode}): {' '.join(exc.cmd)}", file=sys.stderr)
        return EXIT_ERROR
    except (
        record.RecordError,
        FingerprintError,
        RulingError,
        TrustedAuthorsNotConfiguredError,
        trust.TrustReadError,
        github.GitHubAPIError,
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:
        # ValueError covers a body that is not UTF-8 and an invalid
        # trusted-authors configuration; RuntimeError covers GraphQL errors.
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Post merge review's closing comment on the task issue with its record line,"
            " computing the head and change fingerprint from the worktree."
        ),
    )
    parser.add_argument("--issue", required=True, help="the task issue, owner/name#N")
    parser.add_argument(
        "--outcome", required=True, help="the run's outcome: eligible or not-eligible (<reason>)"
    )
    parser.add_argument(
        "--body-file",
        required=True,
        help="the closing comment, its eligibility line first; the record line is added",
    )
    return parser


def _close(issue_ref: str, outcome: str, body_file: Path) -> int:
    """Post the closing comment and return its ID; refuse before posting on any check."""
    repo, number = comments.parse_issue_ref(issue_ref)
    record.check_outcome(outcome)
    body = body_file.read_text(encoding="utf-8")
    require_remote_is(repo, command=_PROG)
    authors = load_trusted_authors()
    agent = agent_login(authors, identity_mode.current_mode())
    if git.working_tree_status() != "":
        msg = "the worktree is not clean: commit or remove every change before closing"
        raise record.RecordError(msg)
    covered = record.covered_change(git.repo_root(), number, main_root=git.main_worktree_root())
    text = record.closing_body(
        body, outcome, record.render_record(covered.head, covered.fingerprint, outcome)
    )
    with github.target_org(repo.split("/", 1)[0]):
        stream = comments.read_stream(repo, number, authors)
        for comment in stream.comments:
            if comment.author == agent and comment.body == text:
                print(
                    f"{_PROG}: the closing comment is already posted on {repo}#{number};"
                    " nothing more was posted",
                    file=sys.stderr,
                )
                return comment.id
        return post_comment_once(repo, number, text, author=agent, authors=authors)


if __name__ == "__main__":
    sys.exit(main())
