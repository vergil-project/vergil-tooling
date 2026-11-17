"""Tests for vergil_tooling.lib.languages."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

import pytest

from vergil_tooling.lib import languages
from vergil_tooling.lib.languages import (
    _RUST_LICENSES_ALLOWLIST,
    _TYPESCRIPT_LICENSES_ALLOWLIST,
    CARGO_CHECK_REPORT,
    CARGO_DENY_REPORT,
    CLIPPY_REPORT,
    COVERAGE_REPORT,
    ESLINT_REPORT,
    JUNIT_REPORT,
    LICENSES_REPORT,
    MYPY_REPORT,
    NPM_AUDIT_REPORT,
    PIP_AUDIT_REPORT,
    PIP_AUDIT_REQUIREMENTS,
    PYTHON_REPORT_FILES,
    RUFF_REPORT,
    RUST_REPORT_FILES,
    TSC_REPORT,
    TYPESCRIPT_REPORT_FILES,
    Cardinality,
    CheckKind,
    EcosystemInfo,
    Language,
    build_python_test_argv,
    check_cardinality,
    ecosystem_metadata,
    language_commands,
    rust_coverage_export_cmd,
    supported_languages,
)


def _joined(cmds: list[list[str]]) -> list[str]:
    return [" ".join(c) for c in cmds]


# -- Python ------------------------------------------------------------------


def test_python_install_commands() -> None:
    cmds = language_commands("python", CheckKind.INSTALL)
    assert cmds == [["uv", "sync", "--frozen", "--group", "dev"]]


def test_python_lint_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.LINT))
    assert "ruff check src/ tests/" in joined
    assert "ruff format --check src/ tests/" in joined


def test_python_lint_emits_ruff_json_report() -> None:
    """A ``ruff check`` invocation writes the machine-readable findings file."""
    cmds = language_commands("python", CheckKind.LINT)
    report_cmd = [c for c in cmds if c[:2] == ["ruff", "check"] and "--output-format=json" in c]
    assert len(report_cmd) == 1
    assert "--output-format=json" in report_cmd[0]
    assert f"--output-file={RUFF_REPORT}" in report_cmd[0]
    # The developer-facing console invocations are preserved alongside it.
    assert ["ruff", "check", "src/", "tests/"] in cmds
    assert ["ruff", "format", "--check", "src/", "tests/"] in cmds


def test_python_typecheck_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.TYPECHECK))
    assert "mypy src/ --junit-xml quality-mypy.xml" in joined
    assert "ty check src tests" in joined


def test_python_typecheck_mypy_emits_junit_xml() -> None:
    """The mypy gate writes a JUnit-XML diagnostic report file."""
    cmds = language_commands("python", CheckKind.TYPECHECK)
    mypy_cmd = [c for c in cmds if c[0] == "mypy"]
    assert len(mypy_cmd) == 1
    assert "--junit-xml" in mypy_cmd[0]
    junit_idx = mypy_cmd[0].index("--junit-xml")
    assert mypy_cmd[0][junit_idx + 1] == MYPY_REPORT
    # ty stays stdout-only (unchanged).
    assert ["ty", "check", "src", "tests"] in cmds


def test_python_test_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.TEST))
    assert any("pytest" in c for c in joined)
    assert any("--cov=src" in c for c in joined)


def test_python_test_enforcement_flag_intact() -> None:
    """The coverage gate (``--cov-fail-under=100``) must survive report wiring."""
    cmds = language_commands("python", CheckKind.TEST)
    pytest_cmd = [c for c in cmds if c[0] == "pytest"]
    assert len(pytest_cmd) == 1
    assert "--cov-fail-under=100" in pytest_cmd[0]


def test_python_test_emits_coverage_xml() -> None:
    cmds = language_commands("python", CheckKind.TEST)
    pytest_cmd = [c for c in cmds if c[0] == "pytest"][0]
    assert f"--cov-report=xml:{COVERAGE_REPORT}" in pytest_cmd


def test_python_test_emits_junit_xml() -> None:
    cmds = language_commands("python", CheckKind.TEST)
    pytest_cmd = [c for c in cmds if c[0] == "pytest"][0]
    assert f"--junitxml={JUNIT_REPORT}" in pytest_cmd


# -- build_python_test_argv (epic vergil-project/.github#333, Task 6) ----------
#
# The computed Python TEST command. ``-n auto --dist worksteal`` (pytest-xdist
# work-stealing scheduler) is appended IFF xdist is available AND parallelism is
# not opted out via ``[test].parallel = false``; the coverage gate flags are
# present in every case. Task 4 (COVERAGE_CORE=sysmon) and Task 10
# (--import-mode=importlib) were both dropped, so neither may ever appear here.


@pytest.mark.parametrize(
    ("xdist_available", "parallel", "expect_xdist"),
    [
        (True, True, True),  # both true → work-stealing xdist
        (True, False, False),  # opt-out honored → serial
        (False, True, False),  # xdist missing → serial, no error
        (False, False, False),  # neither → serial
    ],
)
def test_build_python_test_argv_truth_table(
    *, xdist_available: bool, parallel: bool, expect_xdist: bool
) -> None:
    argv = build_python_test_argv(xdist_available=xdist_available, parallel=parallel)

    # A plain pytest argv (list[str]), never an (argv, env) tuple.
    assert isinstance(argv, list)
    assert all(isinstance(a, str) for a in argv)
    assert argv[0] == "pytest"

    has_xdist = "-n" in argv and "auto" in argv and "--dist" in argv and "worksteal" in argv
    assert has_xdist is expect_xdist

    # The coverage gate is present in every case.
    assert "--cov=src" in argv
    assert "--cov-branch" in argv
    assert "--cov-fail-under=100" in argv

    # Dropped levers must never appear: no import-mode (Task 10), no
    # sysmon/COVERAGE_CORE overlay (Task 4).
    assert "--import-mode=importlib" not in argv
    joined = " ".join(argv)
    assert "COVERAGE_CORE" not in joined
    assert "sysmon" not in joined
    assert "--import-mode" not in joined


def test_build_python_test_argv_xdist_flags_order() -> None:
    """When enabled, the four xdist tokens are appended as ``-n auto --dist worksteal``."""
    argv = build_python_test_argv(xdist_available=True, parallel=True)
    assert argv[-4:] == ["-n", "auto", "--dist", "worksteal"]


def test_build_python_test_argv_tuned_worker_count() -> None:
    """A resolved [test] worker count replaces ``auto`` (issue #3322)."""
    argv = build_python_test_argv(xdist_available=True, parallel=True, workers=12)
    assert argv[-4:] == ["-n", "12", "--dist", "worksteal"]


def test_build_python_test_argv_workers_ignored_when_serial() -> None:
    argv = build_python_test_argv(xdist_available=False, parallel=True, workers=12)
    assert "-n" not in argv


def test_language_commands_python_test_threads_workers() -> None:
    cmds = language_commands(
        "python", CheckKind.TEST, test_parallel=True, xdist_available=True, test_workers=8
    )
    assert cmds[0][-4:] == ["-n", "8", "--dist", "worksteal"]


def test_build_python_test_argv_serial_equals_base() -> None:
    """Serial output is exactly the base gate argv with nothing appended."""
    serial = build_python_test_argv(xdist_available=False, parallel=True)
    assert serial[-1] == f"--junitxml={JUNIT_REPORT}"  # last base flag, no xdist tail


def test_language_commands_python_test_is_computed_parallel() -> None:
    """(python, TEST) routes through build_python_test_argv; xdist+parallel → work-steal."""
    cmds = language_commands("python", CheckKind.TEST, test_parallel=True, xdist_available=True)
    assert cmds == [build_python_test_argv(xdist_available=True, parallel=True)]
    assert cmds[0][-4:] == ["-n", "auto", "--dist", "worksteal"]


def test_language_commands_python_test_serial_when_xdist_missing() -> None:
    cmds = language_commands("python", CheckKind.TEST, test_parallel=True, xdist_available=False)
    assert "-n" not in cmds[0]
    assert "--cov-fail-under=100" in cmds[0]


def test_language_commands_python_test_serial_when_opted_out() -> None:
    cmds = language_commands("python", CheckKind.TEST, test_parallel=False, xdist_available=True)
    assert "-n" not in cmds[0]
    assert "--cov-fail-under=100" in cmds[0]


def test_language_commands_python_test_defaults_serial() -> None:
    """A bare call (no probes) is serial and still carries the coverage gate."""
    cmds = language_commands("python", CheckKind.TEST)
    assert "-n" not in cmds[0]
    assert "--cov-fail-under=100" in cmds[0]
    assert "--import-mode=importlib" not in cmds[0]


def test_python_audit_commands() -> None:
    joined = _joined(language_commands("python", CheckKind.AUDIT))
    assert any("uv sync --check" in c for c in joined)
    assert any("uv lock --check" in c for c in joined)
    assert any(c.startswith("pip-audit") for c in joined)
    assert any("pip-licenses" in c for c in joined)


def test_python_audit_pip_audit_emits_json_report() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_audit_cmd = [c for c in cmds if c[0] == "pip-audit"]
    assert len(pip_audit_cmd) == 1
    assert "--format=json" in pip_audit_cmd[0]
    assert f"--output={PIP_AUDIT_REPORT}" in pip_audit_cmd[0]


def test_python_audit_exports_every_lock_group_and_extra() -> None:
    """The audit input is the whole uv.lock, not the synced dev env (#3498)."""
    cmds = language_commands("python", CheckKind.AUDIT)
    export_cmd = [c for c in cmds if c[:2] == ["uv", "export"]]
    assert len(export_cmd) == 1
    for flag in (
        "--frozen",
        "--all-groups",
        "--all-extras",
        "--no-emit-project",
        "--no-emit-local",
        "--format=requirements.txt",
        f"--output-file={PIP_AUDIT_REQUIREMENTS}",
    ):
        assert flag in export_cmd[0]
    # Hashes are kept: --require-hashes below depends on them.
    assert "--no-hashes" not in export_cmd[0]
    assert not any(arg.startswith("--group") or arg == "--only-dev" for arg in export_cmd[0])


def test_python_audit_pip_audit_reads_the_lock_export() -> None:
    """pip-audit audits the exported lock with pip disabled, hashes required."""
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_audit_cmd = next(c for c in cmds if c[0] == "pip-audit")
    assert f"--requirement={PIP_AUDIT_REQUIREMENTS}" in pip_audit_cmd
    assert "--disable-pip" in pip_audit_cmd
    assert "--require-hashes" in pip_audit_cmd
    assert "--no-deps" not in pip_audit_cmd


def test_python_audit_export_runs_before_pip_audit() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    export_idx = next(i for i, c in enumerate(cmds) if c[:2] == ["uv", "export"])
    audit_idx = next(i for i, c in enumerate(cmds) if c[0] == "pip-audit")
    assert export_idx < audit_idx


def test_python_audit_requirements_is_an_ignored_intermediate() -> None:
    """The export lives under the already-gitignored .vergil/, not as evidence."""
    assert PIP_AUDIT_REQUIREMENTS.startswith(".vergil/")
    assert PIP_AUDIT_REQUIREMENTS not in PYTHON_REPORT_FILES


def test_python_install_and_license_check_stay_on_dev_env() -> None:
    """Only the vulnerability audit widens; install and sync check stay on dev."""
    install = language_commands("python", CheckKind.INSTALL)
    assert install == [["uv", "sync", "--frozen", "--group", "dev"]]
    audit = language_commands("python", CheckKind.AUDIT)
    assert ["uv", "sync", "--check", "--frozen", "--group", "dev"] in audit


def test_python_audit_pip_licenses_allowlist_intact() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_licenses_cmd = [c for c in cmds if c[0] == "pip-licenses"]
    assert len(pip_licenses_cmd) == 1
    assert any(arg.startswith("--allow-only=") for arg in pip_licenses_cmd[0])


def test_python_audit_pip_licenses_allowlist_accepts_public_domain() -> None:
    """Public Domain is allowed fleet-wide (issue #3088; e.g. pyelftools)."""
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_licenses_cmd = [c for c in cmds if c[0] == "pip-licenses"][0]
    allow_only = next(a for a in pip_licenses_cmd if a.startswith("--allow-only="))
    allowed = allow_only.removeprefix("--allow-only=").split(";")
    assert "Public Domain" in allowed


def test_python_audit_pip_licenses_allowlist_accepts_mit_0() -> None:
    """MIT-0 is allowed fleet-wide (issue #4494; cffi 2.x, under cryptography)."""
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_licenses_cmd = [c for c in cmds if c[0] == "pip-licenses"][0]
    allow_only = next(a for a in pip_licenses_cmd if a.startswith("--allow-only="))
    allowed = allow_only.removeprefix("--allow-only=").split(";")
    assert "MIT-0" in allowed


def test_python_audit_pip_licenses_emits_json_report() -> None:
    cmds = language_commands("python", CheckKind.AUDIT)
    pip_licenses_cmd = [c for c in cmds if c[0] == "pip-licenses"][0]
    assert "--format=json" in pip_licenses_cmd
    assert f"--output-file={LICENSES_REPORT}" in pip_licenses_cmd


def test_python_report_files_contract() -> None:
    """The shared report-path constants match the T8 path contract."""
    assert PYTHON_REPORT_FILES == (
        "quality-ruff.json",
        "quality-mypy.xml",
        "coverage.xml",
        "junit.xml",
        "pip-audit.json",
        "licenses.json",
    )
    assert RUFF_REPORT == "quality-ruff.json"
    assert MYPY_REPORT == "quality-mypy.xml"
    assert COVERAGE_REPORT == "coverage.xml"
    assert JUNIT_REPORT == "junit.xml"
    assert PIP_AUDIT_REPORT == "pip-audit.json"
    assert LICENSES_REPORT == "licenses.json"


# -- Go ----------------------------------------------------------------------


def test_go_install_commands() -> None:
    cmds = language_commands("go", CheckKind.INSTALL)
    assert cmds == [["go", "mod", "download"]]


def test_go_lint_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.LINT))
    assert "golangci-lint run ./..." in joined
    assert any("gocyclo" in c for c in joined)


def test_go_typecheck_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.TYPECHECK))
    assert "go vet ./..." in joined


def test_go_test_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.TEST))
    assert any("go test" in c for c in joined)
    assert any("go-test-coverage" in c for c in joined)


def test_go_audit_commands() -> None:
    joined = _joined(language_commands("go", CheckKind.AUDIT))
    assert any("govulncheck" in c for c in joined)
    assert any("go-licenses" in c and "--allowed_licenses=" in c for c in joined)


def test_go_audit_go_licenses_allowlist_intact() -> None:
    cmds = language_commands("go", CheckKind.AUDIT)
    go_licenses_cmd = [c for c in cmds if c[0] == "go-licenses"]
    assert len(go_licenses_cmd) == 1
    flag = go_licenses_cmd[0][-1]
    assert flag.startswith("--allowed_licenses=")
    licenses = flag.split("=", 1)[1].split(",")
    assert "MIT" in licenses
    assert "Apache-2.0" in licenses
    assert len(licenses) == 7


# -- Rust ---------------------------------------------------------------------


def test_rust_install_commands() -> None:
    cmds = language_commands("rust", CheckKind.INSTALL)
    assert cmds == [["cargo", "fetch", "--locked"]]


# Each distinct rendered diagnostic once, in first-seen order (cargo's human
# output dedupes the copies ``--all-targets`` gives the lib and its tests).
_RUST_RENDER = (
    "jq -j -n "
    '\'[inputs | select(.reason == "compiler-message") | .message.rendered // empty] '
    "| reduce .[] as $r ([]; if any(.[]; . == $r) then . else . + [$r] end) "
    "| .[]' "
)


def test_rust_lint_commands() -> None:
    # clippy lints every target (tests, examples and benches included), not
    # only the lib and bins it checks with no target flag (#4379). Its JSON
    # messages are the lint report, and their rendered text goes to the log
    # (spec §6.5).
    assert language_commands("rust", CheckKind.LINT) == [
        ["cargo", "fmt", "--all", "--", "--check"],
        [
            "sh",
            "-c",
            "cargo clippy --all-targets --locked --workspace --message-format=json "
            "-- -D warnings >quality-clippy.json; "
            "rc=$?; "
            f"{_RUST_RENDER}quality-clippy.json >&2 || exit 1; "
            'exit "$rc"',
        ],
    ]


def test_rust_typecheck_commands() -> None:
    assert language_commands("rust", CheckKind.TYPECHECK) == [
        [
            "sh",
            "-c",
            "cargo check --locked --workspace --message-format=json >quality-cargo-check.json; "
            "rc=$?; "
            f"{_RUST_RENDER}quality-cargo-check.json >&2 || exit 1; "
            'exit "$rc"',
        ]
    ]


def test_rust_quality_report_names() -> None:
    """The names are the contract with vergil-actions' reports.sh (spec §6.5)."""
    assert CLIPPY_REPORT == "quality-clippy.json"
    assert CARGO_CHECK_REPORT == "quality-cargo-check.json"


def test_rust_report_files_include_quality() -> None:
    assert CLIPPY_REPORT in RUST_REPORT_FILES
    assert CARGO_CHECK_REPORT in RUST_REPORT_FILES


def test_rust_lint_writes_clippy_report() -> None:
    """clippy's JSON goes to the report; the gate flags are unchanged."""
    (_, (_, _, script)) = language_commands("rust", CheckKind.LINT)
    clippy = _cargo_args(script, "cargo clippy")
    assert clippy[-1] == f">{CLIPPY_REPORT}"
    # cargo's own option, so before the ``--`` that starts clippy's.
    assert "--message-format=json" in clippy[: clippy.index("--")]
    assert " ".join(clippy) == (
        "cargo clippy --all-targets --locked --workspace --message-format=json "
        f"-- -D warnings >{CLIPPY_REPORT}"
    )


def test_rust_typecheck_writes_report() -> None:
    """cargo check's JSON goes to the typecheck report."""
    ((_, _, script),) = language_commands("rust", CheckKind.TYPECHECK)
    check = _cargo_args(script, "cargo check")
    assert check == [
        "cargo",
        "check",
        "--locked",
        "--workspace",
        "--message-format=json",
        f">{CARGO_CHECK_REPORT}",
    ]


def _rust_nextest_toml() -> Path:
    return Path(str(files("vergil_tooling.configs"))) / "rust" / "nextest.toml"


def test_rust_test_commands() -> None:
    # One nextest run under cargo-llvm-cov, then one report from its profiles:
    # the 100% line gate, Cobertura ``coverage.xml`` (issue #4394) and the
    # nextest JUnit report copied to ``junit.xml`` (issue #4395). Nextest runs
    # no doctests, so a separate ``cargo test --doc`` runs them (issue #4378).
    assert language_commands("rust", CheckKind.TEST) == [
        [
            "sh",
            "-c",
            "rm -f coverage.xml junit.xml target/nextest/vergil/junit.xml; "
            "cargo llvm-cov clean --workspace || exit 1; "
            "NEXTEST_PROFILE=vergil cargo llvm-cov nextest --locked --workspace --no-report "
            f'--tool-config-file "vergil:{_rust_nextest_toml()}"; '
            "test_rc=$?; "
            "cargo llvm-cov report --locked --workspace --fail-under-lines 100 "
            "--fail-under-regions 100 --show-missing-lines "
            "--cobertura --output-path coverage.xml; "
            "cov_rc=$?; "
            "if [ -f target/nextest/vergil/junit.xml ]; then "
            "cp target/nextest/vergil/junit.xml junit.xml || exit 1; "
            'elif [ "$test_rc" -eq 0 ]; then '
            'echo "error: cargo-nextest wrote no JUnit report at '
            'target/nextest/vergil/junit.xml" >&2; exit 1; '
            "fi; "
            '[ "$test_rc" -ne 0 ] && exit "$test_rc"; '
            'exit "$cov_rc"',
        ],
        ["cargo", "test", "--doc", "--locked", "--workspace"],
    ]


def test_rust_gate_requires_regions() -> None:
    """The coverage report gates on 100% lines and 100% regions (spec §6.2)."""
    script = language_commands("rust", CheckKind.TEST)[0][2]
    report = _cargo_args(script, "cargo llvm-cov report")
    assert "--fail-under-lines" in report
    assert report[report.index("--fail-under-lines") + 1] == "100"
    assert "--fail-under-regions" in report
    assert report[report.index("--fail-under-regions") + 1] == "100"


def test_rust_coverage_export_cmd() -> None:
    """The JSON export reads the gate's report with its --locked --workspace."""
    assert rust_coverage_export_cmd("out/coverage.json") == [
        "cargo",
        "llvm-cov",
        "report",
        "--locked",
        "--workspace",
        "--json",
        "--output-path",
        "out/coverage.json",
    ]


def test_rust_nextest_toml_turns_on_junit_for_the_vergil_profile() -> None:
    """The packaged tool config defines the profile the TEST command selects."""
    config = tomllib.loads(_rust_nextest_toml().read_text())
    assert config == {"profile": {"vergil": {"fail-fast": False, "junit": {"path": JUNIT_REPORT}}}}
    script = language_commands("rust", CheckKind.TEST)[0][2]
    assert "NEXTEST_PROFILE=vergil " in script
    assert f"target/nextest/vergil/{JUNIT_REPORT}" in script


def _rust_deny_toml() -> Path:
    return Path(str(files("vergil_tooling.configs"))) / "rust" / "deny.toml"


def test_rust_audit_commands() -> None:
    # cargo-deny 0.20 takes ``--config`` (and ``--format``) on the root
    # command, before ``check`` (EmbarkStudios/cargo-deny#881); after ``check``
    # it is a usage error. Its stderr is the JSON report (issue #4394).
    # ``--locked`` and ``--workspace`` are root options too (spec §6.4).
    assert language_commands("rust", CheckKind.AUDIT) == [
        [
            "sh",
            "-c",
            f'cargo deny --format json --config "{_rust_deny_toml()}" --locked --workspace '
            "check --show-stats "
            f"2>{CARGO_DENY_REPORT}; rc=$?; cat {CARGO_DENY_REPORT} >&2 || exit 1; "
            'exit "$rc"',
        ]
    ]


def test_rust_lockfile_commands_are_locked() -> None:
    """Every Rust command that reads Cargo.lock uses it as-is (spec §6.1, #1165)."""
    cmds = {
        kind: _joined(language_commands("rust", kind))
        for kind in (
            CheckKind.INSTALL,
            CheckKind.LINT,
            CheckKind.TYPECHECK,
            CheckKind.TEST,
            CheckKind.AUDIT,
        )
    }
    assert cmds[CheckKind.INSTALL] == ["cargo fetch --locked"]
    fmt, clippy = cmds[CheckKind.LINT]
    assert fmt.startswith("cargo fmt ")
    assert "--locked" not in fmt
    assert "cargo clippy --all-targets --locked " in clippy
    (check,) = cmds[CheckKind.TYPECHECK]
    assert "cargo check --locked --workspace " in check
    coverage, doctest = cmds[CheckKind.TEST]
    assert "cargo llvm-cov nextest --locked " in coverage
    assert "cargo llvm-cov report --locked " in coverage
    assert doctest == "cargo test --doc --locked --workspace"
    assert " --locked " in cmds[CheckKind.AUDIT][0]


def _cargo_args(script: str, subcommand: str) -> list[str]:
    """Return the words of the one ``<subcommand>`` call in a shell script.

    A leading ``NAME=value`` environment assignment is dropped first.
    """
    calls = []
    for part in script.split(";"):
        words = part.split()
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
        if " ".join(words).startswith(f"{subcommand} "):
            calls.append(words)
    assert len(calls) == 1, (subcommand, calls)
    return calls[0]


def test_rust_gate_commands_cover_workspace() -> None:
    """Every Rust gate command that selects packages validates every member (spec §6.4)."""
    (_, (_, _, lint)) = language_commands("rust", CheckKind.LINT)
    clippy = _cargo_args(lint, "cargo clippy")
    assert "--workspace" in clippy[: clippy.index("--")]
    ((_, _, typecheck),) = language_commands("rust", CheckKind.TYPECHECK)
    assert "--workspace" in _cargo_args(typecheck, "cargo check")
    (coverage, doctest) = language_commands("rust", CheckKind.TEST)
    script = coverage[2]
    assert "--workspace" in _cargo_args(script, "cargo llvm-cov nextest")
    assert "--workspace" in _cargo_args(script, "cargo llvm-cov report")
    assert "--workspace" in doctest
    ((_, _, audit),) = language_commands("rust", CheckKind.AUDIT)
    assert "--workspace" in _cargo_args(audit, "cargo deny")


def test_cargo_deny_root_flags_before_check() -> None:
    # cargo-deny's ``--format``, ``--config``, ``--locked`` and ``--workspace``
    # are root options: after ``check`` they are a usage error.
    ((_, _, audit),) = language_commands("rust", CheckKind.AUDIT)
    args = _cargo_args(audit, "cargo deny")
    check = args.index("check")
    for flag in ("--format", "--config", "--locked", "--workspace"):
        assert args.index(flag) < check, flag


def test_rust_doctest_cmd_is_the_test_list_entry() -> None:
    # vrg-validate's doctest skip matches this exact list, so the TEST list
    # must carry it unchanged.
    assert languages.RUST_DOCTEST_CMD == ["cargo", "test", "--doc", "--locked", "--workspace"]
    assert languages.RUST_DOCTEST_CMD in language_commands("rust", CheckKind.TEST)


def test_rust_build_cmd_is_locked_release_workspace() -> None:
    # The release job's build and vrg-validate's BUILD stage run this one
    # command, so validation builds the profile the release ships (spec §6.9).
    info = ecosystem_metadata("rust")
    assert info.build_cmd == ["cargo", "build", "--release", "--locked", "--workspace"]
    assert info.publish_cmd == ["cargo", "publish", "--locked"]


def test_rust_build_check_enabled() -> None:
    assert ecosystem_metadata("rust").validate_build is True
    assert language_commands("rust", CheckKind.BUILD) == [
        ["cargo", "build", "--release", "--locked", "--workspace"]
    ]


def test_rust_report_files_contract() -> None:
    """The rust report names match vergil-actions' staging (issues #4394, #4395, #4473)."""
    assert RUST_REPORT_FILES == (
        "quality-clippy.json",
        "quality-cargo-check.json",
        "coverage.xml",
        "junit.xml",
        "cargo-deny.json",
    )
    assert CARGO_DENY_REPORT == "cargo-deny.json"
    assert f">{CLIPPY_REPORT};" in language_commands("rust", CheckKind.LINT)[1][2]
    assert f">{CARGO_CHECK_REPORT};" in language_commands("rust", CheckKind.TYPECHECK)[0][2]
    test_script = language_commands("rust", CheckKind.TEST)[0][2]
    assert f"--output-path {COVERAGE_REPORT};" in test_script
    assert f" {JUNIT_REPORT} || exit 1;" in test_script
    assert f"2>{CARGO_DENY_REPORT};" in language_commands("rust", CheckKind.AUDIT)[0][2]


def test_rust_deny_toml_allowlist_matches_constant() -> None:
    """The packaged cargo-deny policy carries exactly the reviewed allowlist."""
    policy = tomllib.loads(_rust_deny_toml().read_text())
    assert policy["licenses"]["allow"] == _RUST_LICENSES_ALLOWLIST


# -- C++ (epic vergil-project/.github#207, T5) --------------------------------


def test_cpp_is_supported() -> None:
    assert "cpp" in supported_languages()


def test_cpp_install_commands() -> None:
    joined = _joined(language_commands("cpp", CheckKind.INSTALL))
    # conan.lock is a COMMITTED input consumed via --lockfile, not regenerated
    # on every run — validation must never regenerate the lock (it dirtied the
    # tree and defeated reproducibility). (#3021)
    assert not any("conan lock create" in c for c in joined)
    # Conan resolves deps in the same build_type (Debug) as the CMake
    # coverage/sanitizer builds — a Release/Debug mismatch broke the cold
    # rebuild in T11 (#2558): fmt/format.h not found. (#2572)
    # Conan writes its generator output under the already-ignored ``build/``
    # (``--output-folder=build``), not the source root — retiring the gitignore
    # whack-a-mole for Conan's root-dropped files (#2878/#2908). (#2912)
    assert (
        "conan install . -s build_type=Debug --build=missing "
        "--lockfile=conan.lock --output-folder=build" in joined
    )
    # The conan step pins build_type=Debug to stay consistent with the build.
    for cmd in language_commands("cpp", CheckKind.INSTALL):
        if cmd and cmd[0] == "conan":
            assert "build_type=Debug" in cmd
    # cmake configure exports the compile DB that feeds clang-tidy and threads
    # the [cpp] std/stdlib in as cache vars.
    cmake_cmd = [c for c in language_commands("cpp", CheckKind.INSTALL) if c[0] == "cmake"]
    assert len(cmake_cmd) == 1
    assert "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON" in cmake_cmd[0]
    assert "-DVERGIL_CPP_STD=c++20" in cmake_cmd[0]
    assert "-DVERGIL_CPP_STDLIB=libstdc++" in cmake_cmd[0]


def test_cpp_lint_commands() -> None:
    cmds = language_commands("cpp", CheckKind.LINT)
    joined = _joined(cmds)
    assert any("clang-format" in c and "--dry-run --Werror" in c for c in joined)
    assert any("run-clang-tidy" in c for c in joined)
    # clang-format's find driver prunes build/build-sanitize *and* .worktrees as
    # directories at any depth — a root-level validation must never lint a
    # sibling worktree's tree or a nested CMake output dir (#2906). The old
    # top-level-only ``-path ./build`` prune is gone.
    clang_format_cmd = next(c for c in joined if "clang-format" in c)
    assert "-name build -o -name build-sanitize -o -name .worktrees" in clang_format_cmd
    assert "-path ./build" not in clang_format_cmd
    # cppcheck runs with --library=googletest so it parses GoogleTest's TEST()
    # macro instead of throwing a syntaxError on it — GoogleTest is the
    # documented default framework and the images ship googletest.cfg. (#2579)
    #
    # The enable set is *curated*, not ``--enable=all`` (#2585): it drops the
    # unreliable ``unusedFunction`` check (a systematic false positive on
    # GoogleTest's static-registered ``TEST()`` functions — cppcheck cannot see
    # cross-TU usage) plus the noisy ``information``/``missingInclude``. The
    # build tree is excluded with ``-i build -i build-sanitize`` so cppcheck
    # never walks CMake's compiler-probe file and trips ``toomanyconfigs``;
    # ``.worktrees`` is excluded too so it never reads a sibling worktree (#2906).
    assert any(
        "cppcheck" in c
        and "--enable=warning,style,performance,portability" in c
        and "--enable=all" not in c
        and "--error-exitcode=1" in c
        and "--library=googletest" in c
        and "-i build " in c
        and "-i build-sanitize " in c
        and "-i .worktrees " in c
        for c in joined
    )


def test_cpp_lint_uses_packaged_configs() -> None:
    """Every LINT tool points at a packaged {configs}/cpp/* file that exists."""
    cmds = language_commands("cpp", CheckKind.LINT)
    flat = [arg for cmd in cmds for arg in cmd]
    # No unresolved placeholder survives expansion.
    assert all("{configs}" not in arg for arg in flat)
    # clang-format, clang-tidy and cppcheck each reference a real packaged file.
    referenced = [arg for arg in flat if "/cpp/" in arg]
    for arg in referenced:
        # Extract the path token (may be embedded in a --flag=path or sh script).
        for token in arg.split():
            if "/cpp/" in token:
                path = token.split("=", 1)[-1].split(":")[-1]
                assert Path(path).exists(), f"packaged config missing: {path}"


def test_cpp_typecheck_is_the_warnings_build() -> None:
    cmds = language_commands("cpp", CheckKind.TYPECHECK)
    joined = _joined(cmds)
    # A configure carrying the warning flags, then a build.
    assert any(c.startswith("cmake -S . -B build") for c in joined)
    assert any("cmake --build build" in c for c in joined)


def test_cpp_typecheck_carries_curated_warning_set() -> None:
    """The floor + curated extras all land on one CMAKE_CXX_FLAGS cache value."""
    cmds = language_commands("cpp", CheckKind.TYPECHECK)
    cxx_flags = [arg for cmd in cmds for arg in cmd if arg.startswith("-DCMAKE_CXX_FLAGS=")]
    assert len(cxx_flags) == 1
    flags = cxx_flags[0].split("=", 1)[1].split()
    # Floor (spec §3.2).
    for floor in ("-Wall", "-Wextra", "-Werror", "-Wpedantic"):
        assert floor in flags, f"missing floor flag {floor}"
    # Curated extras owned + tested by this task (T5); T9 only documents them.
    for extra in (
        "-Wshadow",
        "-Wconversion",
        "-Wsign-conversion",
        "-Wcast-qual",
        "-Wold-style-cast",
        "-Wnon-virtual-dtor",
        "-Woverloaded-virtual",
        "-Wdouble-promotion",
        "-Wformat=2",
        "-Wimplicit-fallthrough",
        "-Wnull-dereference",
    ):
        assert extra in flags, f"missing curated extra {extra}"


def test_cpp_test_commands_coverage_ctest_and_sanitizers() -> None:
    cmds = language_commands("cpp", CheckKind.TEST)
    joined = _joined(cmds)
    # CTest as the framework-agnostic runner.
    assert any(c.startswith("ctest") and "--output-on-failure" in c for c in joined)
    # Coverage held to 100% line via gcovr. The root/filter that anchor the
    # search live on the command line so they resolve against the repo root
    # (cwd), not the packaged config's own dir — a config-relative root/filter
    # filtered all coverage out in T11 (#2558). (#2572)
    assert any(
        "gcovr" in c and "--fail-under-line 100" in c and "--root ." in c and "--filter src/" in c
        for c in joined
    )
    # A separate ASan+UBSan build+run in its own build dir.
    assert any("-DVERGIL_CPP_SANITIZE=address,undefined" in c for c in joined)
    assert any("cmake --build build-sanitize" in c for c in joined)
    assert any("ctest --test-dir build-sanitize" in c for c in joined)


def _cpp_config_path(name: str) -> Path:
    """Resolve a packaged ``configs/cpp/<name>`` path.

    Derives the packaged ``cpp/`` config directory from the gcovr ``--config``
    argument in the TEST commands, then joins *name* — so it resolves the
    packaged config file's content without hard-coding the package layout.
    """
    for cmd in language_commands("cpp", CheckKind.TEST):
        for arg in cmd:
            if arg.endswith("/cpp/gcovr.cfg"):
                return Path(arg).parent / name
    msg = "no TEST arg references the cpp configs dir"
    raise AssertionError(msg)


def test_cpp_gcovr_config_ignores_source_not_found() -> None:
    """The packaged gcovr config ignores gcov ``source_not_found`` so third-party
    (Conan) dependency headers (e.g. gtest under ``/opt/conan2``) that gcov
    cannot resolve to a source file do not abort the coverage gate. Only
    ``source_not_found`` is ignored — real gcov errors still fail the gate — and
    the config still carries no ``root``/``filter`` settings (those stay on the
    command line so they resolve against the repo root, #2572). (#3026)
    """
    text = _cpp_config_path("gcovr.cfg").read_text()
    assert "gcov-ignore-errors = source_not_found" in text
    # The path-anchoring ``root``/``filter`` stay on the command line, never as
    # config settings here — assert on actual setting keys, not comment prose.
    setting_keys = {
        line.split("=", 1)[0].strip()
        for line in text.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert "root" not in setting_keys
    assert "filter" not in setting_keys


def test_cpp_cppcheck_suppressions_file_has_no_comments() -> None:
    """The packaged cppcheck suppressions list holds only suppression IDs.

    cppcheck 2.13.0 (Ubuntu 24.04) rejects a comment line in a
    ``--suppressions-list`` file with ``Failed to add suppression. No id.``, so
    the file carries no comment or blank lines; the rationale lives in
    ``cppcheck-suppressions.md`` beside it. Every suppression is kept. (#4138)
    """
    lines = _cpp_config_path("cppcheck-suppressions.txt").read_text().splitlines()
    for line in lines:
        assert line.strip(), "blank line in cppcheck-suppressions.txt"
        assert not line.lstrip().startswith("#"), f"comment line: {line!r}"
        assert line == line.strip() and " " not in line, f"not a bare ID: {line!r}"
    assert lines == [
        "missingIncludeSystem",
        "unmatchedSuppression",
        "checkersReport",
        "missingInclude",
    ]


def test_cpp_audit_commands() -> None:
    joined = _joined(language_commands("cpp", CheckKind.AUDIT))
    # conan audit is the CVE scan — it reads ConanCenter advisories using the
    # CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER env token. OSV-Scanner was reverted
    # because OSV.dev carries no ConanCenter data, so it produced a false
    # all-clear (T11 #2558; decision reversal #209). (#2579)
    # The scan resolves the graph in the lock's own configuration (Debug, as
    # ``conan lock create`` and INSTALL use) against the COMMITTED conan.lock,
    # so a Debug-conditional locked package is scanned and a missing lock is an
    # error, not a silent fresh re-resolution. (#3553)
    assert "conan audit scan . -s build_type=Debug --lockfile=conan.lock" in joined
    # OSV-Scanner is retired here.
    assert not any("osv-scanner" in c for c in joined)
    # Best-effort license-metadata surfacing (hardened gating deferred, §9 #7).
    # The graph is resolved against the COMMITTED conan.lock via --lockfile so
    # the audit reflects the pinned graph, not a fresh re-resolution. (#3021)
    assert "conan graph info . -s build_type=Debug --format=json --lockfile=conan.lock" in joined


def test_cpp_audit_resolves_in_the_lock_build_type() -> None:
    """Every AUDIT conan step resolves in the same build_type as the lock."""
    lock = languages.language_lock_command("cpp")
    assert lock is not None
    lock_build_type = lock[lock.index("-s") + 1]
    conan_cmds = [c for c in language_commands("cpp", CheckKind.AUDIT) if c[0] == "conan"]
    assert len(conan_cmds) == 2
    for cmd in conan_cmds:
        # Without -s Conan resolves the default profile (Release) and silently
        # skips Debug-conditional locked packages; without an explicit
        # --lockfile a missing conan.lock falls back to a fresh resolution.
        assert cmd[cmd.index("-s") + 1] == lock_build_type, cmd
        assert "--lockfile=conan.lock" in cmd, cmd


def test_cpp_ecosystem_metadata() -> None:
    info = ecosystem_metadata("cpp")
    assert info.build_cmd == ["cmake", "--build", "build"]
    # C++ has no v1 publish target (non-goal §8).
    assert info.publish_cmd is None
    assert info.publish_env_var is None


def test_cpp_cardinality_lint_and_audit_run_once() -> None:
    assert check_cardinality("cpp", CheckKind.LINT) is Cardinality.ONCE
    assert check_cardinality("cpp", CheckKind.AUDIT) is Cardinality.ONCE


def test_cpp_cardinality_typecheck_and_test_are_per_version() -> None:
    assert check_cardinality("cpp", CheckKind.TYPECHECK) is Cardinality.PER_VERSION
    assert check_cardinality("cpp", CheckKind.TEST) is Cardinality.PER_VERSION


def test_cpp_std_stdlib_default_to_v1_pins() -> None:
    """The two-argument call resolves {std}/{stdlib} to the v1 pins."""
    for kind in (CheckKind.INSTALL, CheckKind.TYPECHECK, CheckKind.TEST, CheckKind.LINT):
        for cmd in language_commands("cpp", kind):
            for arg in cmd:
                assert "{std}" not in arg and "{stdlib}" not in arg


def test_cpp_std_stdlib_are_threaded_from_config() -> None:
    """An explicit [cpp] std/stdlib overrides the pins in the cache vars."""
    cmds = language_commands("cpp", CheckKind.INSTALL, cpp_std="c++17", cpp_stdlib="libstdc++")
    cmake_cmd = [c for c in cmds if c[0] == "cmake"][0]
    assert "-DVERGIL_CPP_STD=c++17" in cmake_cmd
    # cppcheck --std is threaded too.
    lint = language_commands("cpp", CheckKind.LINT, cpp_std="c++17")
    cppcheck_cmd = [c for c in lint if c[0] == "cppcheck"][0]
    assert "--std=c++17" in cppcheck_cmd


def test_cpp_install_writes_conan_output_to_build() -> None:
    cmds = language_commands("cpp", CheckKind.INSTALL, cpp_std="c++20", cpp_stdlib="libstdc++")
    conan_install = next(c for c in cmds if c[:2] == ["conan", "install"])
    assert "--output-folder=build" in conan_install


def test_cpp_cmake_configures_use_conan_toolchain() -> None:
    for kind in (CheckKind.INSTALL, CheckKind.TYPECHECK, CheckKind.TEST):
        cmds = language_commands("cpp", kind, cpp_std="c++20", cpp_stdlib="libstdc++")
        for c in cmds:
            if c[:1] == ["cmake"] and "-S" in c:  # a configure, not a --build
                assert "-DCMAKE_TOOLCHAIN_FILE=build/conan_toolchain.cmake" in c, (kind, c)


# -- TypeScript (epic vergil-project/.github#284, T4) -------------------------


def _ts_config_path(name: str) -> Path:
    """Resolve a packaged ``configs/typescript/<name>`` path.

    No command references the packaged configs any more: LINT reads the
    managed copies in the repo (epic vergil-project/.github#1174, spec §6.4).
    """
    return Path(str(files("vergil_tooling.configs"))) / "typescript" / name


def test_typescript_is_supported() -> None:
    assert "typescript" in supported_languages()


def test_typescript_install_ignores_scripts() -> None:
    """INSTALL never runs install scripts (epic vergil-project/.github#1174, §6.3)."""
    assert language_commands("typescript", CheckKind.INSTALL) == [["npm", "ci", "--ignore-scripts"]]


def test_typescript_gates_run_repo_binaries_by_path() -> None:
    """TYPECHECK, TEST and LINT's ESLint step run the repo's tools by path.

    No bare name a global binary could answer, and no ``npx``, which falls
    back to the global ``bin`` even with ``--no-install`` (spec §6.3).
    """
    for kind, tool in (
        (CheckKind.LINT, "eslint"),
        (CheckKind.TYPECHECK, "tsc"),
        (CheckKind.TEST, "vitest"),
    ):
        script = _ts_script(kind)
        assert f"node_modules/.bin/{tool} " in script, kind
        # The tool appears only by path, never as a bare command.
        assert f"; {tool} " not in script, kind
        assert not script.startswith(f"{tool} "), kind
        assert "npx" not in script, kind
    # Prettier stays an image tool.
    assert language_commands("typescript", CheckKind.LINT)[0][0] == "prettier"


def _ts_script(kind: CheckKind) -> str:
    """The one ``sh -c`` script of a TypeScript gate (its report-writing run)."""
    scripts = [cmd[2] for cmd in language_commands("typescript", kind) if cmd[:2] == ["sh", "-c"]]
    assert len(scripts) == 1, scripts
    return scripts[0]


def test_typescript_lint_commands() -> None:
    """Prettier reads the managed ``.prettierrc.json``; ESLint finds the repo's own config."""
    lint = language_commands("typescript", CheckKind.LINT)
    assert lint[0] == [
        "prettier",
        "--check",
        ".",
        "!CLAUDE.md",
        "!.claude/settings.json",
        "!.claude/hooks/guard.sh",
        "!.gitignore",
        "!.github/dependabot.yml",
        "!.github/workflows/ops.yml",
        "!tsconfig.vergil.json",
        "!eslint.vergil.config.mjs",
        "!.prettierrc.json",
        "--config",
        ".prettierrc.json",
    ]
    assert lint[1][:2] == ["sh", "-c"]
    assert len(lint) == 2


def test_lint_has_no_staging() -> None:
    """LINT reads the repo's configs in place: no staged copy, no packaged path.

    The managed ``eslint.vergil.config.mjs`` sits in the repo root, so its bare
    imports resolve from the repo's ``node_modules`` and the copy-in,
    delete-after staging of #2771 is gone (spec §6.4).
    """
    flat = [arg for cmd in language_commands("typescript", CheckKind.LINT) for arg in cmd]
    assert not any(".vergil-eslint.config.mjs" in arg for arg in flat)
    assert not any("/typescript/" in arg for arg in flat)
    # ESLint takes no --config: it reads the repo's eslint.config.mjs itself.
    assert "--config" not in _ts_script(CheckKind.LINT)


def test_typescript_lint_writes_eslint_report() -> None:
    """The gating ESLint run writes its JSON report and keeps ESLint's status (spec §6.6)."""
    assert ESLINT_REPORT == "quality-eslint.json"
    script = _ts_script(CheckKind.LINT)
    assert script.startswith(f"rm -f {ESLINT_REPORT}; ")
    assert (
        f"node_modules/.bin/eslint . --format json --output-file {ESLINT_REPORT}; rc=$?; " in script
    )
    # The findings are printed from the report, and the gate exits with ESLint's status.
    assert f"' {ESLINT_REPORT} || exit 1; " in script
    assert script.endswith('exit "$rc"')
    # A passing run that left no report fails the gate.
    assert f'echo "error: ESLint wrote no report at {ESLINT_REPORT}" >&2; exit 1; ' in script


def test_typescript_lint_ban_ts_comment_rule_present() -> None:
    """The no-standing-suppression rule is wired via the packaged ESLint config."""
    text = _ts_config_path("eslint.config.mjs").read_text()
    assert "@typescript-eslint/ban-ts-comment" in text
    # Bare @ts-ignore / @ts-nocheck are banned; @ts-expect-error needs a reason.
    assert '"ts-ignore": true' in text
    assert '"ts-nocheck": true' in text
    assert '"ts-expect-error": "allow-with-description"' in text
    # Type-aware linting is enabled.
    assert "recommendedTypeChecked" in text
    assert "projectService" in text


def test_typescript_typecheck_commands() -> None:
    cmds = language_commands("typescript", CheckKind.TYPECHECK)
    # One canonical type engine; strict set lives in the base tsconfig consumers
    # extend, so the command is a --noEmit typecheck with no strictness flags.
    assert cmds == [["sh", "-c", languages._TYPESCRIPT_TSC_SCRIPT]]
    assert "node_modules/.bin/tsc --noEmit --pretty false --diagnostics >" in cmds[0][2]


def test_typescript_typecheck_writes_tsc_report() -> None:
    """The gating ``tsc`` run writes its diagnostics, and its status is kept (spec §6.6)."""
    assert TSC_REPORT == "quality-tsc.txt"
    assert _ts_script(CheckKind.TYPECHECK) == (
        f"node_modules/.bin/tsc --noEmit --pretty false --diagnostics >{TSC_REPORT}; "
        "rc=$?; "
        f"cat {TSC_REPORT} || exit 1; "
        'exit "$rc"'
    )


def test_typescript_base_tsconfig_curated_extras() -> None:
    """The curated 'warnings to 11' set is authored in the packaged base tsconfig.

    This task (T4) owns and tests the concrete strictness set (spec §3.2); T8
    only documents it. Every option must be present and enabled, and each is a
    real ``tsc`` 6.x compiler option.
    """
    base = _ts_config_path("tsconfig.base.json")
    # tsconfig.base.json is authored as pure JSON (no comments) so it is both
    # tsc-loadable and directly parseable here — the file *is* the source of
    # truth for the strict set.
    opts = json.loads(base.read_text())["compilerOptions"]
    curated = (
        "strict",
        "noUncheckedIndexedAccess",
        "exactOptionalPropertyTypes",
        "noImplicitOverride",
        "noImplicitReturns",
        "noFallthroughCasesInSwitch",
        "noPropertyAccessFromIndexSignature",
        "noUnusedLocals",
        "noUnusedParameters",
    )
    for opt in curated:
        assert opts.get(opt) is True, f"base tsconfig missing/disabled: {opt}"


def test_typescript_test_commands() -> None:
    cmds = language_commands("typescript", CheckKind.TEST)
    assert len(cmds) == 1
    script = _ts_script(CheckKind.TEST)
    # Vitest with the V8 coverage provider.
    assert "node_modules/.bin/vitest run --coverage --coverage.provider=v8 " in script


def _vitest_args() -> list[str]:
    """The arguments of the TEST gate's one Vitest run."""
    script = _ts_script(CheckKind.TEST)
    runs = [part for part in script.split("; ") if part.startswith("node_modules/.bin/vitest ")]
    assert len(runs) == 1, runs
    return runs[0].split()


def test_typescript_test_enforces_100_line_coverage() -> None:
    """The gate must fail under 100% line coverage (spec §5)."""
    assert "--coverage.thresholds.lines=100" in _vitest_args()


def test_typescript_test_writes_junit_and_cobertura() -> None:
    """One Vitest run writes ``junit.xml`` and Cobertura ``coverage.xml`` (spec §6.6)."""
    args = _vitest_args()
    # The console reporter stays, beside JUnit to the root report.
    assert "--reporter=default" in args
    assert "--reporter=junit" in args
    assert f"--outputFile.junit={JUNIT_REPORT}" in args
    # The coverage table stays on the console, beside Cobertura.
    assert "--coverage.reporter=text" in args
    assert "--coverage.reporter=cobertura" in args
    assert "--coverage.reportsDirectory=coverage" in args
    script = _ts_script(CheckKind.TEST)
    # Stale reports are removed first; Cobertura is copied to the root report.
    assert script.startswith(
        f"rm -f {COVERAGE_REPORT} {JUNIT_REPORT} coverage/cobertura-coverage.xml; "
    )
    assert f"cp coverage/cobertura-coverage.xml {COVERAGE_REPORT} || exit 1; " in script
    # A passing run that left either report missing fails the gate.
    assert "Vitest wrote no Cobertura report" in script
    assert f"Vitest wrote no JUnit report at {JUNIT_REPORT}" in script
    assert script.endswith('exit "$rc"')


def test_typescript_test_reports_coverage_on_failure() -> None:
    """Vitest writes no coverage when a test fails unless it is told to (spec §6.6)."""
    assert "--coverage.reportOnFailure" in _vitest_args()


def test_typescript_audit_writes_json_and_gates() -> None:
    """The gating ``npm audit`` run writes ``npm-audit.json`` at the unchanged threshold."""
    assert NPM_AUDIT_REPORT == "npm-audit.json"
    assert _ts_script(CheckKind.AUDIT) == (
        f"npm audit --audit-level=high --json >{NPM_AUDIT_REPORT}; "
        "rc=$?; "
        f"cat {NPM_AUDIT_REPORT} || exit 1; "
        'exit "$rc"'
    )


def test_typescript_report_files() -> None:
    """The TypeScript report names match vergil-actions' staging (spec §6.6)."""
    assert TYPESCRIPT_REPORT_FILES == (
        "coverage.xml",
        "junit.xml",
        "npm-audit.json",
        "quality-eslint.json",
        "quality-tsc.txt",
    )
    every_script = " ".join(
        _ts_script(kind)
        for kind in (CheckKind.LINT, CheckKind.TYPECHECK, CheckKind.TEST, CheckKind.AUDIT)
    )
    for report in TYPESCRIPT_REPORT_FILES:
        assert report in every_script, report


def test_typescript_audit_commands() -> None:
    cmds = language_commands("typescript", CheckKind.AUDIT)
    joined = _joined(cmds)
    # npm audit is the CVE scan, over the whole locked dependency set with an
    # explicit severity threshold.
    assert _ts_script(CheckKind.AUDIT).startswith("npm audit --audit-level=high ")
    # OSV-Scanner is the documented contingency, not the v1 default.
    assert not any("osv-scanner" in c for c in joined)
    # Best-effort license-metadata surfacing (hardened gating deferred, §9 #7).
    assert any("license-checker" in c for c in joined)


def test_typescript_audit_covers_dev_dependencies() -> None:
    """The CVE scan audits devDependencies too, not just production (#3552).

    A vulnerable devDependency (docs tooling, test runners) must fail the
    gate, so ``npm audit`` must not narrow its scope to production.
    """
    npm_audit = _ts_script(CheckKind.AUDIT).split(" >")[0].split()
    assert npm_audit[:2] == ["npm", "audit"]
    assert not any(arg.startswith(("--omit", "--only")) for arg in npm_audit)
    assert "--production" not in npm_audit


def test_typescript_audit_license_allowlist_reviewed_set() -> None:
    """The TS license allowlist constant carries the reviewed permissive set."""
    licenses = _TYPESCRIPT_LICENSES_ALLOWLIST.split(";")
    assert "MIT" in licenses
    assert "Apache-2.0" in licenses
    assert "ISC" in licenses
    # Semicolon-separated (the form license-checker --onlyAllow consumes).
    assert ";" in _TYPESCRIPT_LICENSES_ALLOWLIST


def test_typescript_build_cmd_installs_first_without_scripts() -> None:
    # The release job's Build step runs on a fresh checkout with no
    # node_modules, so the build installs from the lockfile first, with no
    # install script run (epic vergil-project/.github#1174, spec §6.5).
    info = ecosystem_metadata("typescript")
    assert info.build_cmd == ["sh", "-c", "npm ci --ignore-scripts && npm run build"]


def test_publish_cmd_stays_none() -> None:
    # No npm publishing (spec §6.5): a TypeScript release publishes nothing.
    info = ecosystem_metadata("typescript")
    assert info.publish_cmd is None
    assert info.publish_env_var is None


def test_build_on_for_typescript() -> None:
    assert ecosystem_metadata("typescript").validate_build is True
    assert language_commands("typescript", CheckKind.BUILD) == [
        ["sh", "-c", "npm ci --ignore-scripts && npm run build"]
    ]
    for lang in ("python", "go", "cpp"):
        assert language_commands(lang, CheckKind.BUILD) == []


def test_typescript_cardinality_typecheck_lint_audit_run_once() -> None:
    assert check_cardinality("typescript", CheckKind.TYPECHECK) is Cardinality.ONCE
    assert check_cardinality("typescript", CheckKind.LINT) is Cardinality.ONCE
    assert check_cardinality("typescript", CheckKind.AUDIT) is Cardinality.ONCE


def test_typescript_cardinality_test_is_per_version() -> None:
    # Only TEST fans out per Node version (§3.6).
    assert check_cardinality("typescript", CheckKind.TEST) is Cardinality.PER_VERSION


def test_typescript_no_unresolved_placeholders() -> None:
    for kind in CheckKind:
        for cmd in language_commands("typescript", kind):
            for arg in cmd:
                assert "{configs}" not in arg, f"Unresolved placeholder in: {arg}"


# -- Edge cases ---------------------------------------------------------------


def test_unknown_language_returns_empty() -> None:
    cmds = language_commands("unknown", CheckKind.LINT)
    assert cmds == []


def test_none_language_returns_empty() -> None:
    cmds = language_commands(None, CheckKind.LINT)
    assert cmds == []


def test_none_language_install_returns_empty() -> None:
    cmds = language_commands(None, CheckKind.INSTALL)
    assert cmds == []


def test_configs_placeholder_is_resolved() -> None:
    """No command of any language may carry an unresolved {configs} placeholder."""
    for lang in supported_languages():
        for kind in CheckKind:
            for cmd in language_commands(lang, kind):
                for arg in cmd:
                    assert "{configs}" not in arg, f"Unresolved placeholder in: {arg}"


# -- New API ------------------------------------------------------------------


def test_supported_languages_includes_cpp() -> None:
    langs = supported_languages()
    assert langs == frozenset({"python", "go", "rust", "cpp", "typescript"})


def test_supported_languages_is_frozen() -> None:
    langs = supported_languages()
    assert isinstance(langs, frozenset)


def test_ecosystem_metadata_python() -> None:
    info = ecosystem_metadata("python")
    assert isinstance(info, EcosystemInfo)
    assert info.build_cmd is not None
    assert info.publish_cmd is not None
    # PyPI trusted publishing (OIDC): no token env var is named (#3394).
    assert info.publish_env_var is None


def test_ecosystem_metadata_rust_names_cargo_registry_token() -> None:
    # cargo publish reads CARGO_REGISTRY_TOKEN, not CRATES_IO_TOKEN (#3394).
    info = ecosystem_metadata("rust")
    assert info.publish_cmd == ["cargo", "publish", "--locked"]
    assert info.publish_env_var == "CARGO_REGISTRY_TOKEN"


def test_ecosystem_metadata_go() -> None:
    info = ecosystem_metadata("go")
    assert isinstance(info, EcosystemInfo)
    assert info.publish_env_var is None


def test_ecosystem_metadata_all_languages_have_entries() -> None:
    for lang in supported_languages():
        info = ecosystem_metadata(lang)
        assert isinstance(info, EcosystemInfo), f"Missing ecosystem for {lang}"


def test_ecosystem_metadata_unknown_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="unsupported"):
        ecosystem_metadata("unknown")


def test_language_commands_still_works_for_unknown() -> None:
    cmds = language_commands("unknown", CheckKind.LINT)
    assert cmds == []


# -- Check cardinality --------------------------------------------------------


def test_single_image_languages_default_to_per_version_cardinality() -> None:
    """Every single-image language defaults to per-version for every check kind.

    This backward-compatibility guarantee is what keeps their generated CI
    gates byte-identical after the cardinality concept was introduced. C++ and
    TypeScript are the matrix languages that legitimately declare ONCE kinds, so
    they are excluded here and covered by their own cardinality tests. BUILD,
    the newer kind, runs once for every language (its own test below).
    """
    for lang in supported_languages() - {"cpp", "typescript"}:
        for kind in CheckKind:
            if kind is CheckKind.BUILD:
                continue
            assert check_cardinality(lang, kind) is Cardinality.PER_VERSION


def test_check_cardinality_unknown_language_defaults_per_version() -> None:
    assert check_cardinality("unknown", CheckKind.LINT) is Cardinality.PER_VERSION


def test_check_cardinality_none_language_defaults_per_version() -> None:
    assert check_cardinality(None, CheckKind.LINT) is Cardinality.PER_VERSION


# -- BUILD check kind (epic vergil-project/.github#1174) ----------------------


def test_build_kind_value() -> None:
    assert CheckKind.BUILD.value == "build"


def test_build_kind_off_for_every_other_language() -> None:
    # On for Rust and TypeScript only (their own tests above): each other
    # language turns it on in its own task, with a fixture run.
    for lang in ("python", "go", "cpp"):
        assert ecosystem_metadata(lang).validate_build is False
        assert language_commands(lang, CheckKind.BUILD) == []
    assert language_commands(None, CheckKind.BUILD) == []
    assert language_commands("unknown", CheckKind.BUILD) == []


def _with_ecosystem(
    monkeypatch: pytest.MonkeyPatch, lang: str, *, build_cmd: list[str] | None
) -> None:
    entry = languages._REGISTRY[lang]
    ecosystem = replace(entry.ecosystem, build_cmd=build_cmd, validate_build=True)
    monkeypatch.setitem(languages._REGISTRY, lang, replace(entry, ecosystem=ecosystem))


def test_build_commands_come_from_build_cmd(monkeypatch: pytest.MonkeyPatch) -> None:
    # The BUILD command is the release job's build_cmd: one source for both.
    _with_ecosystem(monkeypatch, "go", build_cmd=["go", "build", "./..."])
    assert language_commands("go", CheckKind.BUILD) == [["go", "build", "./..."]]


def test_build_commands_are_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_ecosystem(monkeypatch, "go", build_cmd=["go", "build", "./..."])
    language_commands("go", CheckKind.BUILD)[0].append("-x")
    assert ecosystem_metadata("go").build_cmd == ["go", "build", "./..."]


def test_build_on_with_no_build_cmd_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_ecosystem(monkeypatch, "typescript", build_cmd=None)
    assert language_commands("typescript", CheckKind.BUILD) == []


def test_build_cardinality_is_once() -> None:
    for lang in sorted(supported_languages()):
        assert check_cardinality(lang, CheckKind.BUILD) is Cardinality.ONCE


def test_language_cardinality_defaults_to_empty_mapping() -> None:
    """A Language that declares no cardinality carries an empty mapping."""
    lang = Language(
        name="x",
        checks={},
        ecosystem=EcosystemInfo(build_cmd=None, publish_cmd=None, publish_env_var=None),
    )
    assert lang.cardinality == {}


def test_language_may_declare_once_cardinality() -> None:
    """A language can declare a kind as run-once (e.g. a matrix language)."""
    lang = Language(
        name="x",
        checks={},
        ecosystem=EcosystemInfo(build_cmd=None, publish_cmd=None, publish_env_var=None),
        cardinality={CheckKind.LINT: Cardinality.ONCE},
    )
    assert lang.cardinality[CheckKind.LINT] is Cardinality.ONCE


def test_cardinality_enum_values() -> None:
    assert Cardinality.PER_VERSION.value == "per-version"
    assert Cardinality.ONCE.value == "once"


# -- Per-language lock command (epic vergil-project/.github#342, T2) -----------


def test_cpp_lock_command() -> None:
    assert languages.language_lock_command("cpp") == [
        "conan",
        "lock",
        "create",
        ".",
        "-s",
        "build_type=Debug",
    ]


def test_lockless_language_has_no_lock_command() -> None:
    assert languages.language_lock_command("go") is None


def test_unknown_language_has_no_lock_command() -> None:
    assert languages.language_lock_command("cobol") is None


def test_none_language_has_no_lock_command() -> None:
    assert languages.language_lock_command(None) is None
