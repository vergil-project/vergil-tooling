"""Failure output for dependency-update PRs, fix tasks and release comments.

Spec §5.2: carry the last ~200 lines of output plus the reproduce command, never
the full output. GitHub rejects bodies over 65,536 characters, and a rejected
body would leave no PR for the release gate to see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import subprocess

    from vergil_tooling.lib.update_deps.context import DepsFailure, UpdateDepsContext


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


def command_text(cmd: object) -> str:
    """A CalledProcessError's ``cmd`` (a string or an argv sequence) as one string."""
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


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
    return "\n".join(
        [
            f"## Failure: {failure.kind}",
            "",
            failure.message,
            "",
            "Last lines of output:",
            "",
            "```text",
            failure.output_tail,
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
