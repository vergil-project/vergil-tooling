"""Keep a workflow's stage checklist in a tracking issue's body.

A workflow that spans sessions (``epic-create`` first) records how far it has
got as one delimited task list per workflow in its tracking issue's body (epic
vergil-project/.github#1546). A fresh session runs ``status`` to find the
next stage; each stage ends with a ``tick``. See
:mod:`vergil_tooling.lib.checkpoint` for the block's format and its strict
parse.

- ``init <ref> --workflow <name> [--tick <stage>]…`` writes the block for the
  workflow's current stage list, all unticked or with the named leading
  stages ticked. It refuses a body that already holds a block.
- ``status <ref> --workflow <name> [--json]`` prints the stages, their ticks
  and the next stage (the first unticked one).
- ``tick <ref> --workflow <name> <stage>`` ticks one stage. It refuses a stage
  not in the block, or one with an earlier stage unticked. A stage already
  ticked is a success that writes nothing, so a re-entry can repeat it.
- ``reset <ref> --workflow <name>`` removes every closed block of the
  workflow, malformed or duplicate ones included, and the blank line before
  each. With no block it succeeds and writes nothing.
- ``reconcile <ref> --workflow <name> [--tick <stage>]…`` rewrites a skewed
  block to the workflow's current stage list: a matching stage keeps its
  tick, a removed stage is dropped, and a new stage is unticked unless
  ``--tick`` names it (the human says it is done). It refuses a block with no
  skew, and a ``--tick`` of a stage that is not new. It prints one line per
  change, then posts them in a comment on the issue whose first line is
  ``vrg-checkpoint: reconciled <workflow>``. A comment that fails after the
  write exits 1 with the comment's text, to post by hand.

Every operation reads the body through the trust layer
(:func:`vergil_tooling.lib.issue_body.read_body`): a body whose author or an
editor holds no role is never parsed or written. Every read and write runs
under the issue owner's App token (``github.target_org``). A write rewrites
only the block, and is skipped when the body would not change.

Exit codes: 0 success; 1 failure or refusal; 2 bad arguments; 3 untrusted
body; 4 block missing; 5 skew (the block's stages are not the workflow's
current list); 6 malformed block. Nothing is written on any exit but 0, save
a ``reconcile`` whose comment failed after its write.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import checkpoint, epics, github, issue_body, trust
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Sequence

_PROG = "vrg-checkpoint"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNTRUSTED = 3
EXIT_MISSING = 4
EXIT_SKEW = 5
EXIT_MALFORMED = 6


class _StopError(Exception):
    """A refusal or a state that ends the run with *code*, before any write."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    ref: epics.IssueRef = args.ref
    try:
        with github.target_org(ref.owner):
            return _COMMANDS[args.command](ref, args)
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return EXIT_FAILED
    except github.MissingGitHubTokenError:
        raise
    except trust.UntrustedContentError as exc:
        # The message says the run stopped before any write, metadata only.
        print(f"{_PROG}: {ref.slug}: {exc}", file=sys.stderr)
        return EXIT_UNTRUSTED
    except _WriteFailedError:
        return EXIT_FAILED
    except _StopError as exc:
        print(f"{_PROG}: {ref.slug}: {exc}; nothing was written", file=sys.stderr)
        return exc.code
    except (
        github.GitHubAPIError,
        trust.TrustReadError,
        TrustedAuthorsNotConfiguredError,
        ValueError,
    ) as exc:
        # Every failure that reaches here comes before the write (_write
        # reports a failed write itself): an unreadable API answer, a missing
        # or invalid trusted-author configuration, or a failed read.
        print(f"{_PROG}: {ref.slug}: {exc}; nothing was written", file=sys.stderr)
        return EXIT_FAILED


class _WriteFailedError(Exception):
    """A write failed after it was sent; already reported."""


def _read(ref: epics.IssueRef) -> str:
    return issue_body.read_body(ref.owner, ref.repo, ref.number)


def _write(ref: epics.IssueRef, old: str, new: str) -> bool:
    """Write *new* unless it equals *old*; True when a write was made.

    Raises :class:`_WriteFailedError` (exit 1) for a failed write, reported here: the
    client retries, so the edit may have landed.
    """
    if new == old:
        return False
    try:
        issue_body.write_body(ref.owner, ref.repo, ref.number, new)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        print(
            f"{_PROG}: {ref.slug}: the edit may not have been applied ({exc}); "
            "re-run status to confirm",
            file=sys.stderr,
        )
        raise _WriteFailedError from exc
    return True


def _pairs(body: str, workflow: str) -> list[tuple[str, bool]]:
    """The block's pairs, or a :class:`_StopError` for a missing, malformed or skewed block."""
    try:
        pairs = checkpoint.parse(body, workflow)
    except checkpoint.Missing as exc:
        raise _StopError(EXIT_MISSING, str(exc)) from exc
    except checkpoint.Malformed as exc:
        raise _StopError(EXIT_MALFORMED, f"malformed block: {exc}") from exc
    found = checkpoint.skew(pairs, workflow)
    if found is not None:
        msg = (
            f"the {workflow} block does not match its current stage list "
            f"(run status to see the difference)"
        )
        raise _StopError(EXIT_SKEW, msg)
    return pairs


def _next_line(pairs: Sequence[tuple[str, bool]]) -> str:
    stage = checkpoint.next_stage(pairs)
    return f"next: {stage}" if stage is not None else "next: none (every stage is ticked)"


# --- init ---------------------------------------------------------------------


def _init(ref: epics.IssueRef, args: argparse.Namespace) -> int:
    workflow: str = args.workflow
    stages = checkpoint.WORKFLOWS[workflow]
    ticks: list[str] = list(dict.fromkeys(args.tick or []))
    if set(ticks) != set(stages[: len(ticks)]):
        msg = (
            f"--tick must name the first stages of {workflow} "
            f"({', '.join(stages)}), got {', '.join(ticks)}"
        )
        raise _StopError(EXIT_FAILED, msg)
    body = _read(ref)
    try:
        checkpoint.parse(body, workflow)
    except checkpoint.Missing:
        pass
    except checkpoint.Malformed as exc:
        raise _StopError(EXIT_MALFORMED, f"malformed block: {exc}") from exc
    else:
        msg = f"the body already holds a {workflow} block (run status, or reset to start over)"
        raise _StopError(EXIT_FAILED, msg)
    new = checkpoint.upsert(body, checkpoint.render(workflow, ticks), workflow)
    _write(ref, body, new)
    pairs = checkpoint.parse(new, workflow)
    print(f"{ref.slug}: wrote the {workflow} block; {_next_line(pairs)}")
    return EXIT_OK


# --- status -------------------------------------------------------------------


def _status(ref: epics.IssueRef, args: argparse.Namespace) -> int:
    workflow: str = args.workflow
    body = _read(ref)
    pairs: list[tuple[str, bool]] = []
    found: checkpoint.Skew | None = None
    detail = ""
    try:
        pairs = checkpoint.parse(body, workflow)
    except checkpoint.Missing as exc:
        state, code, detail = "missing", EXIT_MISSING, str(exc)
    except checkpoint.Malformed as exc:
        state, code, detail = "malformed", EXIT_MALFORMED, str(exc)
    else:
        found = checkpoint.skew(pairs, workflow)
        state, code = ("skew", EXIT_SKEW) if found is not None else ("ok", EXIT_OK)
    stage = checkpoint.next_stage(pairs) if state == "ok" else None
    if state == "malformed":
        print(f"{_PROG}: {ref.slug}: malformed block: {detail}", file=sys.stderr)
    if args.json:
        print(json.dumps(_status_json(ref, workflow, state, pairs, stage, found), indent=2))
        return code
    print(f"{ref.slug} {workflow}: {state}")
    if state == "missing":
        print(detail)
    for name, done in pairs:
        print(f"- [{'x' if done else ' '}] {name}")
    if found is not None:
        print(_skew_text(found))
    if state == "ok":
        print(_next_line(pairs))
    return code


def _status_json(  # noqa: PLR0913 - one argument per field of the JSON shape
    ref: epics.IssueRef,
    workflow: str,
    state: str,
    pairs: Sequence[tuple[str, bool]],
    stage: str | None,
    found: checkpoint.Skew | None,
) -> dict[str, object]:
    return {
        "issue": ref.slug,
        "workflow": workflow,
        "state": state,
        "stages": [{"name": name, "done": done} for name, done in pairs],
        "next": stage,
        "skew": None
        if found is None
        else {
            "recorded": list(found.recorded),
            "current": list(found.current),
            "added": list(found.added),
            "removed": list(found.removed),
            "reordered": found.reordered,
        },
    }


def _skew_text(found: checkpoint.Skew) -> str:
    def names(stages: Sequence[str]) -> str:
        return ", ".join(stages) if stages else "(none)"

    return "\n".join(
        [
            "the block does not match the current stage list:",
            f"  current: {names(found.current)}",
            f"  added: {names(found.added)}",
            f"  removed: {names(found.removed)}",
            f"  reordered: {'yes' if found.reordered else 'no'}",
        ]
    )


# --- tick ---------------------------------------------------------------------


def _tick(ref: epics.IssueRef, args: argparse.Namespace) -> int:
    workflow: str = args.workflow
    stage: str = args.stage
    body = _read(ref)
    _pairs(body, workflow)
    try:
        new = checkpoint.tick(body, workflow, stage)
    except ValueError as exc:
        raise _StopError(EXIT_FAILED, str(exc)) from exc
    wrote = _write(ref, body, new)
    pairs = checkpoint.parse(new, workflow)
    what = "ticked" if wrote else "already ticked"
    print(f"{ref.slug}: {what} {stage}; {_next_line(pairs)}")
    return EXIT_OK


# --- reset --------------------------------------------------------------------


def _reset(ref: epics.IssueRef, args: argparse.Namespace) -> int:
    workflow: str = args.workflow
    body = _read(ref)
    try:
        new = checkpoint.remove(body, workflow)
    except checkpoint.Malformed as exc:
        raise _StopError(EXIT_MALFORMED, f"malformed block: {exc}") from exc
    wrote = _write(ref, body, new)
    if wrote:
        print(f"{ref.slug}: removed the {workflow} block")
    else:
        print(f"{ref.slug}: no {workflow} block; nothing to remove")
    return EXIT_OK


# --- reconcile ----------------------------------------------------------------


def _change_lines(changes: Sequence[str]) -> str:
    """The change lines, one Markdown list item each, for stdout and the comment alike."""
    return "\n".join(f"- {change}" for change in changes)


def _reconcile_comment(workflow: str, changes: Sequence[str]) -> str:
    return f"{_PROG}: reconciled {workflow}\n\n{_change_lines(changes)}\n"


def _reconcile(ref: epics.IssueRef, args: argparse.Namespace) -> int:
    workflow: str = args.workflow
    body = _read(ref)
    try:
        new, changes = checkpoint.reconcile(body, workflow, args.tick or [])
    except checkpoint.Missing as exc:
        raise _StopError(EXIT_MISSING, str(exc)) from exc
    except checkpoint.Malformed as exc:
        raise _StopError(EXIT_MALFORMED, f"malformed block: {exc}") from exc
    except ValueError as exc:
        raise _StopError(EXIT_FAILED, str(exc)) from exc
    comment = _reconcile_comment(workflow, changes)
    try:
        _write(ref, body, new)
    except _WriteFailedError:
        print(
            f"{_PROG}: {ref.slug}: if status shows the block reconciled, "
            f"post this comment by hand:\n{comment}",
            end="",
            file=sys.stderr,
        )
        raise
    pairs = checkpoint.parse(new, workflow)
    print(f"{ref.slug}: reconciled the {workflow} block:\n{_change_lines(changes)}")
    print(_next_line(pairs))
    try:
        issue_body.post_comment(ref.owner, ref.repo, ref.number, comment)
    except github.GitHubAPIError as exc:
        # A missing token included: the block is already written, so the
        # comment text must reach the human whatever stopped the post.
        print(
            f"{_PROG}: {ref.slug}: the block was written, but the comment recording "
            f"it may not have been posted ({exc}); check the issue's comments and, "
            f"if it is missing, post this comment by hand:\n{comment}",
            end="",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return EXIT_OK


_COMMANDS = {
    "init": _init,
    "status": _status,
    "tick": _tick,
    "reset": _reset,
    "reconcile": _reconcile,
}


# --- arguments ----------------------------------------------------------------


def _issue_ref(value: str) -> epics.IssueRef:
    try:
        return epics.parse_issue_ref(value, default_repo="")
    except ValueError as exc:
        msg = f"{exc}; give a fully qualified owner/repo#N"
        raise argparse.ArgumentTypeError(msg) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Keep a workflow's stage checklist in a tracking issue's body.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text, description=help_text)
        p.add_argument("ref", type=_issue_ref, help="the tracking issue: owner/repo#N")
        p.add_argument(
            "--workflow",
            required=True,
            choices=sorted(checkpoint.WORKFLOWS),
            help="the workflow whose block to use",
        )
        return p

    init = command("init", "write the workflow's block, all stages unticked")
    init.add_argument(
        "--tick",
        action="append",
        metavar="STAGE",
        help="tick this stage in the new block (repeatable; the first stages, in order)",
    )
    status = command("status", "print the stages, their ticks and the next stage")
    status.add_argument("--json", action="store_true", help="print JSON")
    tick = command("tick", "tick one stage")
    tick.add_argument("stage", help="the stage to tick")
    command("reset", "remove the workflow's block")
    reconcile = command(
        "reconcile", "rewrite a skewed block to the current stage list, keeping matching ticks"
    )
    reconcile.add_argument(
        "--tick",
        action="append",
        metavar="STAGE",
        help="tick this new stage: the human says it is done (repeatable; new stages only)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
