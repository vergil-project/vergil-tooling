"""Integration tests: the check registry's report flags actually write files.

These exercise the real gate tools with the exact report-emitting flags the
registry produces, proving the machine-readable reports appear at the expected
paths after a run (not just that the flags are present). Command-shape
assertions live in ``test_languages.py``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib.languages import (
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
