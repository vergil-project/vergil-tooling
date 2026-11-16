"""Integration tests: the check registry's report flags actually write files.

These exercise the real gate tools with the exact report-emitting flags the
registry produces, proving the machine-readable reports appear at the expected
paths after a run (not just that the flags are present). Command-shape
assertions live in ``test_languages.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.languages import (
    CARGO_CHECK_REPORT,
    CARGO_DENY_REPORT,
    CLIPPY_REPORT,
    COVERAGE_REPORT,
    JUNIT_REPORT,
    LICENSES_REPORT,
    MYPY_REPORT,
    RUFF_REPORT,
    CheckKind,
    language_commands,
)

if TYPE_CHECKING:
    from pathlib import Path


def _registry_cmd(kind: CheckKind, tool: str) -> list[str]:
    cmds = language_commands("python", kind)
    matching = [c for c in cmds if c[0] == tool]
    assert len(matching) == 1, f"expected exactly one {tool} command, got {matching}"
    return matching[0]


def test_test_command_report_flags_write_files(tmp_path: Path) -> None:
    """pytest, run with the registry's report flags, writes coverage.xml + junit.xml."""
    (tmp_path / "m.py").write_text("def f() -> int:\n    return 1\n")
    (tmp_path / "test_m.py").write_text(
        "from m import f\n\n\ndef test_f() -> None:\n    assert f() == 1\n"
    )

    pytest_cmd = _registry_cmd(CheckKind.TEST, "pytest")
    report_flags = [
        arg
        for arg in pytest_cmd
        if arg.startswith("--cov-report=xml:") or arg.startswith("--junitxml=")
    ]
    # Both report families must be wired, else this fixture proves nothing.
    assert len(report_flags) == 2

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "test_m.py",
            f"--rootdir={tmp_path}",
            f"--confcutdir={tmp_path}",
            "-p",
            "no:cacheprovider",
            "--cov=m",
            *report_flags,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / COVERAGE_REPORT).is_file()
    assert (tmp_path / JUNIT_REPORT).is_file()


def test_lint_ruff_writes_json_report(tmp_path: Path) -> None:
    """ruff check, run with the registry's report flags, writes quality-ruff.json."""
    if shutil.which("ruff") is None:
        import pytest

        pytest.skip("ruff not installed in this environment")

    (tmp_path / "clean.py").write_text("x: int = 1\n")

    cmds = language_commands("python", CheckKind.LINT)
    report_cmd = [c for c in cmds if c[:2] == ["ruff", "check"] and "--output-format=json" in c]
    assert len(report_cmd) == 1, f"expected one report ruff command, got {report_cmd}"
    # Point the report flags at the fixture file instead of src/ tests/.
    output_flags = [
        arg
        for arg in report_cmd[0]
        if arg.startswith("--output-format=") or arg.startswith("--output-file=")
    ]
    assert len(output_flags) == 2

    result = subprocess.run(
        ["ruff", "check", *output_flags, "clean.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / RUFF_REPORT).is_file()


def test_typecheck_mypy_writes_junit_report(tmp_path: Path) -> None:
    """mypy, run with the registry's report flag, writes quality-mypy.xml."""
    if shutil.which("mypy") is None:
        import pytest

        pytest.skip("mypy not installed in this environment")

    (tmp_path / "clean.py").write_text("def f() -> int:\n    return 1\n")

    mypy_cmd = _registry_cmd(CheckKind.TYPECHECK, "mypy")
    junit_idx = mypy_cmd.index("--junit-xml")
    report_flag = mypy_cmd[junit_idx : junit_idx + 2]
    assert report_flag == ["--junit-xml", MYPY_REPORT]

    result = subprocess.run(
        ["mypy", "clean.py", *report_flag],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / MYPY_REPORT).is_file()


def test_audit_pip_licenses_writes_report(tmp_path: Path) -> None:
    """pip-licenses, run with the registry's output flags, writes licenses.json."""
    if shutil.which("pip-licenses") is None:
        import pytest

        pytest.skip("pip-licenses not installed in this environment")

    pip_licenses_cmd = _registry_cmd(CheckKind.AUDIT, "pip-licenses")
    output_flags = [
        arg
        for arg in pip_licenses_cmd
        if arg.startswith("--format=") or arg.startswith("--output-file=")
    ]
    # Both the format and the output-file flags must be wired.
    assert len(output_flags) == 2

    result = subprocess.run(
        ["pip-licenses", *output_flags],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / LICENSES_REPORT).is_file()


# -- Rust AUDIT: cargo-deny's stderr is the report (issue #4394) ---------------
#
# cargo-deny is not in this image, so a stand-in ``cargo`` on PATH plays it:
# JSON Lines on stderr and a chosen exit status. The real tool's output is
# proven in the prod-rust image (the issue's evidence comment).

_FAKE_CARGO = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CARGO_ARGS"
echo '{"type":"diagnostic","fields":{"severity":"error"}}' >&2
echo '{"type":"summary","fields":{}}' >&2
exit "$FAKE_CARGO_RC"
"""


def _run_rust_audit(tmp_path: Path, rc: int) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text(_FAKE_CARGO)
    cargo.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (cmd,) = language_commands("rust", CheckKind.AUDIT)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CARGO_ARGS": str(tmp_path / "args.txt"),
        "FAKE_CARGO_RC": str(rc),
    }
    return subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, check=False)


@pytest.mark.parametrize("rc", [0, 1, 2])
def test_rust_audit_writes_report_and_keeps_cargo_deny_status(tmp_path: Path, rc: int) -> None:
    """The AUDIT script writes cargo-deny's stderr to the report and exits with its status."""
    result = _run_rust_audit(tmp_path, rc)

    assert result.returncode == rc, result.stdout + result.stderr
    report = (tmp_path / "work" / CARGO_DENY_REPORT).read_text()
    records = [json.loads(line) for line in report.splitlines()]
    assert [r["type"] for r in records] == ["diagnostic", "summary"]
    # The report is echoed to stderr, so the gate's log still shows it.
    assert result.stderr == report
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args[:4] == ["deny", "--format", "json", "--config"]
    assert args[5:7] == ["--locked", "--workspace"]
    assert args[-2:] == ["check", "--show-stats"]


def test_rust_audit_fails_when_report_cannot_be_written(tmp_path: Path) -> None:
    """A report path that cannot be opened fails the gate rather than passing silently."""
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / CARGO_DENY_REPORT).mkdir()

    result = _run_rust_audit(tmp_path, 0)

    assert result.returncode != 0
    assert not (tmp_path / "args.txt").exists()  # cargo-deny never ran


# -- Rust LINT and TYPECHECK: cargo's JSON messages are the report (#4473) -----
#
# A stand-in ``cargo`` plays clippy and ``cargo check`` with
# ``--message-format=json``: JSON messages on stdout, a status line on stderr,
# and a chosen exit status. The real jq renders the log. The real tools' output
# is proven by the live check against vergil-fixture-rust-lib.

_RENDERED = "warning: unused variable: `x`\n --> src/lib.rs:2:9\n"
_RENDERED_2 = "warning: missing documentation for a function\n --> src/lib.rs:1:1\n"

# The lib and the lib's tests (``--all-targets``) each report the same
# diagnostic; the log prints it once, as cargo's human output does.
_CARGO_JSON_MESSAGES = [
    {"reason": "compiler-artifact", "target": {"name": "dep"}},
    {"reason": "compiler-message", "message": {"rendered": _RENDERED, "level": "warning"}},
    {"reason": "compiler-message", "message": {"rendered": _RENDERED_2, "level": "warning"}},
    {"reason": "compiler-message", "message": {"rendered": None, "level": "note"}},
    {"reason": "compiler-message", "message": {"rendered": _RENDERED, "level": "warning"}},
    {"reason": "build-finished", "success": False},
]

_FAKE_CARGO_JSON = f"""#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_CARGO_ARGS"
cat <<'JSON'
{chr(10).join(json.dumps(m) for m in _CARGO_JSON_MESSAGES)}
JSON
echo "    Finished dev profile" >&2
exit "$FAKE_CARGO_RC"
"""

_RUST_QUALITY_GATES = [
    pytest.param(CheckKind.LINT, CLIPPY_REPORT, "clippy", id="clippy"),
    pytest.param(CheckKind.TYPECHECK, CARGO_CHECK_REPORT, "check", id="check"),
]


def _rust_quality_cmd(kind: CheckKind) -> list[str]:
    (cmd,) = [c for c in language_commands("rust", kind) if c[:2] == ["sh", "-c"]]
    return cmd


def _run_rust_quality(
    tmp_path: Path, kind: CheckKind, rc: int, *, fake_jq: str | None = None
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cargo = bin_dir / "cargo"
    cargo.write_text(_FAKE_CARGO_JSON)
    cargo.chmod(0o755)
    if fake_jq is not None:
        jq = bin_dir / "jq"
        jq.write_text(fake_jq)
        jq.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CARGO_ARGS": str(tmp_path / "args.txt"),
        "FAKE_CARGO_RC": str(rc),
    }
    return subprocess.run(
        _rust_quality_cmd(kind), cwd=work, env=env, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize("rc", [0, 101])
@pytest.mark.parametrize(("kind", "report", "subcommand"), _RUST_QUALITY_GATES)
def test_failing_lint_still_writes_report(
    tmp_path: Path, kind: CheckKind, report: str, subcommand: str, rc: int
) -> None:
    """The report is cargo's JSON, written on success and failure alike, and the
    command exits with cargo's status."""
    result = _run_rust_quality(tmp_path, kind, rc)

    assert result.returncode == rc, result.stdout + result.stderr
    lines = (tmp_path / "work" / report).read_text().splitlines()
    assert [json.loads(line) for line in lines] == _CARGO_JSON_MESSAGES
    # The log shows the rendered diagnostic and cargo's own status line.
    assert result.stderr == "    Finished dev profile\n" + _RENDERED + _RENDERED_2
    assert result.stdout == ""
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args[0] == subcommand
    assert "--message-format=json" in args


@pytest.mark.parametrize(("kind", "report", "subcommand"), _RUST_QUALITY_GATES)
def test_rust_quality_replaces_stale_report(
    tmp_path: Path, kind: CheckKind, report: str, subcommand: str
) -> None:
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / report).write_text('{"reason": "stale"}\n')

    result = _run_rust_quality(tmp_path, kind, 0)

    assert result.returncode == 0, result.stderr
    assert "stale" not in (tmp_path / "work" / report).read_text()


@pytest.mark.parametrize(("kind", "report", "subcommand"), _RUST_QUALITY_GATES)
def test_rust_quality_fails_when_report_cannot_be_written(
    tmp_path: Path, kind: CheckKind, report: str, subcommand: str
) -> None:
    """A report path that cannot be opened fails the gate rather than passing silently."""
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / report).mkdir()

    result = _run_rust_quality(tmp_path, kind, 0)

    assert result.returncode != 0
    assert not (tmp_path / "args.txt").exists()  # cargo never ran


@pytest.mark.parametrize(("kind", "report", "subcommand"), _RUST_QUALITY_GATES)
def test_rust_quality_fails_when_rendering_fails(
    tmp_path: Path, kind: CheckKind, report: str, subcommand: str
) -> None:
    """A passing cargo whose diagnostics cannot be rendered fails the gate."""
    result = _run_rust_quality(tmp_path, kind, 0, fake_jq="#!/bin/sh\nexit 2\n")

    assert result.returncode == 1
    assert (tmp_path / "work" / report).read_text()
