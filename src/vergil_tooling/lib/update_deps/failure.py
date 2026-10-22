"""Failure output for dependency-update PRs, fix tasks and release comments.

Spec §5.2: carry the last ~200 lines of output plus the reproduce command, never
the full output. GitHub rejects bodies over 65,536 characters, and a rejected
body would leave no PR for the release gate to see.

For a validation failure the output is first narrowed to the failing
``vrg-validate`` stage(s) (#3283): the raw tail of a whole run is mostly passing
stages and container noise, and the cause can sit hundreds of lines earlier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

    from vergil_tooling.lib.update_deps.context import DepsFailure, UpdateDepsContext

RAW_TAIL_HEADING = (
    "Last lines of the raw output (the failing vrg-validate stage's output could not be isolated):"
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# vrg-validate's stage markers (lib/progress.py): the plain renderer opens a
# stage with "→ <name>  starting...", the GitHub Actions one with
# "::group::<name>"; both close it with "<symbol> <name>  <elapsed>[ — <error>]".
_STAGE_START_RE = re.compile(r"^(?:→ (?P<plain>\S+)  starting\.\.\.|::group::(?P<gha>\S+))$")
_SUMMARY_RULE = "─" * 45
_FAILED_SYMBOL = "✗"


@dataclass(frozen=True)
class FailureOutput:
    """Output to show for a failure, with the heading that says what it is."""

    heading: str
    text: str


def _stage_closed(line: str, name: str) -> str | None:
    """The status symbol if *line* is *name*'s closing status line, else None."""
    match = re.match(rf"^([✓⚠✗]) {re.escape(name)}(?:  | — |$)", line)
    return match.group(1) if match else None


def _failing_stages(stdout: str) -> tuple[list[str], list[str]]:
    """Names and output lines of the failed stages in vrg-validate's *stdout*.

    A stage still open when the output ends (vrg-validate died mid-stage) is
    the cause of the failure, so it counts as failed.
    """
    names: list[str] = []
    kept: list[str] = []
    current: str | None = None
    block: list[str] = []
    for line in _ANSI_RE.sub("", stdout).splitlines():
        if current is None:
            start = _STAGE_START_RE.match(line)
            if start:
                current = start.group("plain") or start.group("gha")
                block = [line]
            continue
        block.append(line)
        symbol = _stage_closed(line, current)
        if symbol is None:
            continue
        if symbol == _FAILED_SYMBOL:
            names.append(current)
            kept.extend(block)
        current = None
    if current is not None:
        names.append(current)
        kept.extend(block)
    return names, kept


def _summary_lines(stdout: str) -> list[str]:
    """vrg-validate's closing summary (failure list, full-log path), if present."""
    lines = _ANSI_RE.sub("", stdout).splitlines()
    if _SUMMARY_RULE not in lines:
        return []
    return lines[lines.index(_SUMMARY_RULE) :]


def validation_failure_output(exc: subprocess.CalledProcessError) -> FailureOutput:
    """The output of the failing vrg-validate stage(s), or the labelled raw output.

    vrg-validate writes every stage's output, tool stderr included, to its
    stdout; the process's stderr is container setup noise. The failing stages
    are isolated from stdout by their markers and followed by vrg-validate's
    summary. When no failing stage can be found, the whole combined output is
    returned under a heading that says it is the raw tail.
    """
    raw = exc.output
    stdout = (raw.decode(errors="replace") if isinstance(raw, bytes) else raw) or ""
    names, kept = _failing_stages(stdout)
    if not names:
        return FailureOutput(RAW_TAIL_HEADING, combined_output(exc))
    summary = _summary_lines(stdout)
    if summary:
        kept = [*kept, "", *summary]
    plural = "s" if len(names) > 1 else ""
    heading = f"Output of the failing vrg-validate stage{plural} ({', '.join(names)}):"
    return FailureOutput(heading, "\n".join(kept))


def combined_output(exc: subprocess.CalledProcessError) -> str:
    """A failed process's stdout then stderr, skipping empty streams.

    Test failures land on stdout while tool noise lands on stderr, so taking
    only one stream can drop exactly the lines that explain the failure.
    """
    texts = [
        s.decode(errors="replace") if isinstance(s, bytes) else s
        for s in (exc.output, exc.stderr)
        if s
    ]
    return "\n".join(t.rstrip("\n") for t in texts if t.strip())


def tail_output(text: str, *, lines: int = 200, max_chars: int = 20_000) -> str:
    """The last *lines* lines of *text*, at most *max_chars* characters.

    An omission marker line is prepended whenever anything was dropped.
    """
    all_lines = text.splitlines()
    kept = all_lines[-lines:]
    omitted = len(all_lines) - len(kept)
    body = "\n".join(kept)
    if len(body) > max_chars:
        body = body[-max_chars:]
        return f"[… output truncated to the last {max_chars} characters …]\n{body}"
    if omitted:
        return f"[… {omitted} earlier lines omitted …]\n{body}"
    return body


def failure_markdown(failure: DepsFailure) -> str:
    """A Markdown section describing *failure* for a PR body or issue."""
    output = failure.output_tail if failure.output_tail.strip() else "(no output was captured)"
    return "\n".join(
        [
            f"## Failure: {failure.kind}",
            "",
            failure.message,
            "",
            failure.output_heading,
            "",
            "```text",
            output,
            "```",
            "",
            f"Reproduce on the branch: `{failure.reproduce}`",
        ]
    )


def failure_summary(ctx: UpdateDepsContext) -> str:
    """One-paragraph summary for the status stage and the release tracking issue."""
    f = ctx.failure
    if f is None:  # callers check first; fail loud rather than print "None"
        msg = "failure_summary called without a recorded failure"
        raise ValueError(msg)
    head = f"Dependency update for {ctx.version} failed ({f.kind}): {f.message}"
    if f.kind == "update":
        return f"{head} No PR was opened."
    parts = [head, f"The PR stays open: {ctx.pr_url}."]
    if ctx.fix_issue_url:
        parts.append(f"Fix task: {ctx.fix_issue_url}.")
    parts.append("The next vrg-release is gated until it lands.")
    return " ".join(parts)
