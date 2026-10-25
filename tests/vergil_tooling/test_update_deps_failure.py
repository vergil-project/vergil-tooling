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
    assert "Reproduce on the branch: `vrg-container-run -- vrg-validate`" in text


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
    text = failure.failure_summary(ctx)
    assert "No PR was opened" in text
    assert "Fix task" not in text
    assert "gated" not in text


def test_failure_summary_update_kind_names_fix_task() -> None:
    # An update failure has no PR, so its fix task is the only record (#3550).
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/r"), version="2.1.243")
    ctx.failure = DepsFailure(kind="update", message="hook failed", output_tail="", reproduce="x")
    ctx.fix_issue_url = "https://github.com/o/r/issues/10"
    text = failure.failure_summary(ctx)
    assert text == (
        "Dependency update for 2.1.243 failed (update): hook failed No PR was opened. "
        "Fix task: https://github.com/o/r/issues/10."
    )


def test_failure_markdown_update_kind_reproduces_on_develop() -> None:
    # Nothing is pushed for an update failure, so there is no branch (#3550).
    f = DepsFailure(kind="update", message="hook: x", output_tail="t", reproduce="./hook.sh")
    text = failure.failure_markdown(f)
    assert "Reproduce on `develop`: `./hook.sh`" in text
    assert "on the branch" not in text


def test_failure_summary_requires_a_failure() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/r"), version="2.1.243")
    with pytest.raises(ValueError, match="without a recorded failure"):
        failure.failure_summary(ctx)


# --- isolating the failing vrg-validate stage (#3283) ---------------------------

_FIXTURES = Path(__file__).parent / "fixtures" / "update_deps"
_VALIDATE = ("vrg-container-run", "--", "vrg-validate")


def _validate_exc(stdout: str, stderr: str = "") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, _VALIDATE, output=stdout, stderr=stderr)


def _real_typecheck_failure() -> subprocess.CalledProcessError:
    """The first real update-deps validation failure (v2.1.243, PR #3281), trimmed.

    stdout is vrg-validate's plain-rendered stage output; stderr is the
    container's uv venv/install noise, which the old tail appended last.
    """
    return _validate_exc(
        (_FIXTURES / "vrg-validate-typecheck-failure.stdout.txt").read_text(encoding="utf-8"),
        (_FIXTURES / "vrg-validate-typecheck-failure.stderr.txt").read_text(encoding="utf-8"),
    )


def test_real_failure_raw_tail_misses_the_diagnostics() -> None:
    # Guards the fixture: the bug (#3283) is that the raw tail lost the cause.
    raw = failure.tail_output(failure.combined_output(_real_typecheck_failure()))
    assert "error[invalid-assignment]" not in raw


def test_validation_output_isolates_the_failing_typecheck_stage() -> None:
    out = failure.validation_failure_output(_real_typecheck_failure())
    text = out.text
    assert out.heading == "Output of the failing vrg-validate stage (typecheck):"
    assert text.startswith("→ typecheck  starting...")
    assert "error[invalid-assignment]" in text
    assert "tests/vergil_tooling/test_release_tracking.py:231:9" in text
    assert "error[invalid-argument-type]" in text
    assert "✗ typecheck  6.0s — ValidationError: 1 of 2 typecheck command(s) failed" in text
    # vrg-validate's own summary (failure list and full-log pointer) is kept.
    assert "PipelineError: 1 stage failed (typecheck) · exit 1" in text
    assert "full log → /workspace/.vergil/" in text
    # Passing stages and the container's stderr noise are not.
    for absent in ("→ test", "TOTAL", "→ audit", "→ lint", "Creating virtual environment"):
        assert absent not in text
    # The size bounds still apply and keep the diagnostics.
    assert "error[invalid-assignment]" in failure.tail_output(text)


def test_validation_output_keeps_every_failing_stage() -> None:
    stdout = "\n".join(
        [
            "→ lint  starting...",
            "E501 line too long",
            "✗ lint  0.4s — ValidationError: 1 of 1 lint command(s) failed",
            "",
            "→ typecheck  starting...",
            "Success: no issues found",
            "✓ typecheck  1.0s",
            "",
            "→ test  starting...",
            "FAILED tests/test_a.py::test_a",
            "✗ test  2.0s — ValidationError: 1 of 1 test command(s) failed",
            "",
        ]
    )
    out = failure.validation_failure_output(_validate_exc(stdout))
    assert out.heading == "Output of the failing vrg-validate stages (lint, test):"
    assert "E501 line too long" in out.text
    assert "FAILED tests/test_a.py::test_a" in out.text
    assert "Success: no issues found" not in out.text


def test_validation_output_reads_github_actions_groups() -> None:
    stdout = "\n".join(
        [
            "::group::lint",
            "ok",
            "::endgroup::",
            "✓ lint  0.1s",
            "",
            "::group::test",
            "FAILED tests/test_a.py::test_a",
            "::endgroup::",
            "✗ test  2.0s — ValidationError: 1 of 1 test command(s) failed",
            "::error title=test failed::ValidationError: 1 of 1 test command(s) failed",
        ]
    )
    out = failure.validation_failure_output(_validate_exc(stdout))
    assert out.heading == "Output of the failing vrg-validate stage (test):"
    assert out.text.startswith("::group::test")
    assert "FAILED tests/test_a.py::test_a" in out.text
    assert "\nok\n" not in out.text


def test_validation_output_strips_ansi_escapes() -> None:
    stdout = "\x1b[1m→ test  starting...\x1b[0m\n\x1b[31mFAILED\x1b[0m\n✗ test  1.0s — boom"
    out = failure.validation_failure_output(_validate_exc(stdout))
    assert out.text == "→ test  starting...\nFAILED\n✗ test  1.0s — boom"


def test_validation_output_keeps_a_stage_cut_off_mid_run() -> None:
    # vrg-validate died inside a stage (no status line): that stage is the cause.
    stdout = "→ lint  starting...\n✓ lint  0.1s\n\n→ test  starting...\nSegmentation fault"
    out = failure.validation_failure_output(_validate_exc(stdout, "Killed"))
    assert out.heading == "Output of the failing vrg-validate stage (test):"
    assert out.text == "→ test  starting...\nSegmentation fault"


def test_validation_output_decodes_bytes() -> None:
    stdout = "→ t  starting...\nE\n✗ t  1s"
    exc = subprocess.CalledProcessError(1, _VALIDATE, output=stdout.encode())
    assert failure.validation_failure_output(exc).text == stdout


def test_validation_output_does_not_close_a_stage_on_another_stages_status() -> None:
    stdout = "→ test  starting...\n✓ test-helper ok\n✗ test  1.0s — boom"
    out = failure.validation_failure_output(_validate_exc(stdout))
    assert out.text == stdout


def test_validation_output_falls_back_to_the_labelled_raw_tail() -> None:
    # No stage markers at all (e.g. the container never started).
    out = failure.validation_failure_output(_validate_exc("", "docker: daemon not running"))
    assert out.heading == failure.RAW_TAIL_HEADING
    assert "could not be isolated" in out.heading
    assert out.text == "docker: daemon not running"


def test_validation_output_falls_back_when_no_stage_failed() -> None:
    stdout = "→ lint  starting...\n✓ lint  0.1s\n"
    out = failure.validation_failure_output(_validate_exc(stdout, "post-run error"))
    assert out.heading == failure.RAW_TAIL_HEADING
    assert out.text == "→ lint  starting...\n✓ lint  0.1s\npost-run error"


def test_failure_markdown_uses_the_recorded_heading() -> None:
    f = DepsFailure(
        kind="validation",
        message="Validation failed.",
        output_tail="E",
        reproduce="r",
        output_heading="Output of the failing vrg-validate stage (test):",
    )
    text = failure.failure_markdown(f)
    assert "Output of the failing vrg-validate stage (test):\n\n```text\nE\n```" in text
    assert "Last lines of output" not in text


def test_failure_markdown_default_heading_is_the_tail() -> None:
    f = DepsFailure(kind="merge", message="m", output_tail="x", reproduce="r")
    assert "Last lines of output:\n\n```text\nx\n```" in failure.failure_markdown(f)


def test_failure_markdown_never_renders_an_empty_block() -> None:
    f = DepsFailure(kind="update", message="m", output_tail="  \n", reproduce="r")
    text = failure.failure_markdown(f)
    assert "```text\n(no output was captured)\n```" in text
