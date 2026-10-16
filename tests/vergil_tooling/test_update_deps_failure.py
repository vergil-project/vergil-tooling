from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps import failure
from vergil_tooling.lib.update_deps.context import DepsFailure, UpdateDepsContext


def test_tail_output_keeps_last_lines() -> None:
    text = "\n".join(f"line {i}" for i in range(1000))
    tail = failure.tail_output(text, lines=200)
    assert tail.splitlines()[0].startswith("[… 800 earlier lines omitted …]")
    assert tail.splitlines()[-1] == "line 999"
    assert "line 799" not in tail
    assert "line 800" in tail


def test_tail_output_short_text_unchanged() -> None:
    assert failure.tail_output("a\nb") == "a\nb"


def test_tail_output_empty_text() -> None:
    assert failure.tail_output("") == ""


def test_tail_output_caps_characters() -> None:
    text = "\n".join("x" * 1000 for _ in range(150))  # 150 lines, ~150k chars
    tail = failure.tail_output(text)
    assert len(tail) <= 20_000 + 100  # cap + the omission marker line
    assert tail.startswith("[… output truncated to the last 20000 characters …]\n")
    assert tail.endswith("x" * 1000)


def test_combined_output_keeps_stdout_and_stderr() -> None:
    exc = subprocess.CalledProcessError(1, ["x"], output="FAILED test_a\n", stderr="warn\n")
    assert failure.combined_output(exc) == "FAILED test_a\nwarn"


def test_combined_output_decodes_bytes_and_skips_blank() -> None:
    exc = subprocess.CalledProcessError(1, ["x"], output=b"out", stderr="  \n")
    assert failure.combined_output(exc) == "out"


def test_combined_output_empty() -> None:
    assert failure.combined_output(subprocess.CalledProcessError(1, ["x"])) == ""


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [("uv lock", "uv lock"), (["uv", "lock"], "uv lock"), (("a", 1), "a 1"), (7, "7")],
)
def test_command_text(cmd: object, expected: str) -> None:
    assert failure.command_text(cmd) == expected


def test_failure_markdown_has_tail_and_reproduce() -> None:
    f = DepsFailure(
        kind="validation",
        message="Validation failed.",
        output_tail="E   assert 1 == 2",
        reproduce="vrg-container-run -- vrg-validate",
    )
    text = failure.failure_markdown(f)
    assert "## Failure: validation" in text
    assert "```text\nE   assert 1 == 2\n```" in text
    assert "`vrg-container-run -- vrg-validate`" in text


def test_failure_summary_names_pr_and_fix_task() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/r"), version="2.1.243")
    ctx.failure = DepsFailure(kind="merge", message="checks failed", output_tail="", reproduce="x")
    ctx.pr_url = "https://github.com/o/r/pull/9"
    ctx.fix_issue_url = "https://github.com/o/r/issues/10"
    text = failure.failure_summary(ctx)
    assert "2.1.243" in text
    assert "merge" in text
    assert "pull/9" in text
    assert "issues/10" in text
    assert "gated" in text


def test_failure_summary_without_fix_task() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/r"), version="2.1.243")
    ctx.failure = DepsFailure(kind="validation", message="red", output_tail="", reproduce="x")
    ctx.pr_url = "https://github.com/o/r/pull/9"
    text = failure.failure_summary(ctx)
    assert "pull/9" in text
    assert "Fix task" not in text


def test_failure_summary_update_kind_has_no_pr() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/r"), version="2.1.243")
    ctx.failure = DepsFailure(kind="update", message="hook failed", output_tail="", reproduce="x")
    assert "No PR was opened" in failure.failure_summary(ctx)


def test_failure_summary_requires_a_failure() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/r"), version="2.1.243")
    with pytest.raises(ValueError, match="without a recorded failure"):
        failure.failure_summary(ctx)
