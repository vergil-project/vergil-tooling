"""Unified validation command.

Reads primary_language from vergil.toml, then either runs a
specific check type (--check) or all checks in sequence:
  common -> install -> lint -> typecheck -> test -> audit -> semgrep -> trivy
  -> docs -> custom

The single checks are common, lint, typecheck, test, audit, semgrep, trivy,
docs and custom. The semgrep stage runs the scan PR CI's ``security / semgrep``
gate runs, with the same rulesets and fleet exclusions, so a finding fails
here before the PR is opened (issue #4003). The trivy stage runs the
vulnerability scan of PR CI's ``security / trivy`` gate (issue #4445). The
docs stage runs the strict
mkdocs build of PR CI's ``docs / docs`` gate when the repo has
``docs/site/mkdocs.yml`` (issue #4446).
``--check custom`` runs only the repo's custom validator
(``scripts/bin/validate-custom`` or ``vrg-validate-custom``), after the
install stage when the language has one so the validator sees the dev
dependencies. With no custom validator it reports that and exits 0; a
validator that is present but unusable still fails (issue #3650).

Checks run as progress-framework stages: install is fail_fast,
everything else is fail_defer so all failures are reported in one run.

A repo with none of its language's manifests (a Rust repo with no
``Cargo.toml`` yet) is not bootstrapped: the image warmup skips it, and so
do the install and language stages here, each recorded as a warning that
names the missing manifests (issue #4384). See
:func:`~vergil_tooling.lib.container_cache.unbootstrapped_manifests`. A repo
with none of its manifests but with sources of its language (a branch that
deletes ``Cargo.toml``, say) is not unstarted: its install stage fails.

``--all-versions`` (Python only) adds one test stage per resolved CI version
after the test stage, so the coverage gate runs on every interpreter PR-CI
tests, not only the dev container's (issue #3547).

Every pipeline run, pass or fail, interrupted or not, writes
``.vergil/vrg-validate-last.json``: the commit checked out at the start,
the exit code, whether the run was full (no ``--check``), whether the tree
was clean at the start, and each stage's status. merge-review's request
builder reads it (:mod:`vergil_tooling.lib.validate_record`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import audit_retry, config, git, progress, trivy_scan
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.container_cache import language_sources, unbootstrapped_manifests
from vergil_tooling.lib.docs import (
    patch_nav,
    stage_docs,
    staging_outputs,
    unignored_staging_error,
)
from vergil_tooling.lib.languages import (
    COVERAGE_REPORT,
    JUNIT_REPORT,
    RUST_DOCTEST_CMD,
    CheckKind,
    build_python_test_argv,
    language_commands,
)
from vergil_tooling.lib.progress import Stage, StageMode
from vergil_tooling.lib.sarif import evaluate_findings, format_summary, parse_sarif
from vergil_tooling.lib.semgrep import resolve_rulesets, run_scan, scan_failure_message
from vergil_tooling.lib.validate_record import write_record
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.floors import check_floors, check_toolchain_selection
from vergil_tooling.lib.versions.resolve import resolve_language
from vergil_tooling.lib.versions.window import load_window

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_CHECK_KINDS = {
    "common": None,
    "lint": CheckKind.LINT,
    "typecheck": CheckKind.TYPECHECK,
    "test": CheckKind.TEST,
    "audit": CheckKind.AUDIT,
    "semgrep": None,
    "trivy": None,
    "docs": None,
    "custom": None,
}

_NO_CUSTOM_VALIDATOR = (
    "custom: no custom validator (scripts/bin/validate-custom or "
    "vrg-validate-custom) — nothing to run"
)

_LANGUAGE_CHECK_ORDER = [
    CheckKind.LINT,
    CheckKind.TYPECHECK,
    CheckKind.TEST,
    CheckKind.AUDIT,
]


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-validate",
        description="Run validation checks from the command registry.",
    )
    parser.add_argument(
        "--check",
        choices=list(_CHECK_KINDS.keys()),
        default=None,
        help="Run only this check type. Omit to run all.",
    )
    parser.add_argument(
        "--all-versions",
        action="store_true",
        help=(
            "Also run the test and coverage gate on every resolved CI version, "
            "each on a uv-managed interpreter (Python only)."
        ),
    )
    progress.add_progress_args(parser, ())
    args = parser.parse_args(argv)
    if args.all_versions and args.check not in (None, "test"):
        parser.error("--all-versions runs the test stage; use it alone or with --check test")

    if not _in_dev_container():
        print(
            "ERROR: vrg-validate must run inside a dev container.\n"
            "       Run: vrg-container-run -- vrg-validate",
            file=sys.stderr,
        )
        return 1

    # Prepend `.venv/bin` unconditionally. Under the container's anonymous
    # `.venv` mask the dir is empty at startup and populated by the install
    # stage; PATH resolves at exec time, so prepending a not-yet-existent dir is
    # harmless and required for later bare `ruff`/`mypy`/`pytest` to resolve
    # (#2486).
    venv_bin = Path.cwd() / ".venv" / "bin"
    if str(venv_bin) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"

    repo_root = git.repo_root()

    cpp_std: str | None = None
    cpp_stdlib: str | None = None
    test_config = config.TestConfig()
    vergil_config: config.VergilConfig | None = None
    try:
        vergil_config = config.read_config(repo_root)
        language = vergil_config.project.primary_language
        cpp_std = vergil_config.cpp.std
        cpp_stdlib = vergil_config.cpp.stdlib
        test_config = vergil_config.test
    except FileNotFoundError:
        language = None
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.all_versions and language != "python":
        print(
            "ERROR: --all-versions supports Python only "
            f"(primary-language is {language or '<not set>'})",
            file=sys.stderr,
        )
        return 1

    ci_versions: tuple[str, ...] = ()
    if vergil_config is not None:
        resolved = _versions_resolve(vergil_config, repo_root)
        if resolved is None:
            return 1
        ci_versions = resolved

    try:
        test_workers, test_workers_source = _resolve_test_workers(test_config)
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    stages = _build_stages(
        args.check,
        language,
        repo_root,
        cpp_std=cpp_std,
        cpp_stdlib=cpp_stdlib,
        test_parallel=test_config.parallel,
        test_workers=test_workers,
        test_workers_source=test_workers_source,
        all_versions=ci_versions if args.all_versions else (),
    )
    if not stages:
        if args.check == "custom":
            print(_NO_CUSTOM_VALIDATOR)
            return 0
        print(f"No {args.check} commands for language '{language or '<not set>'}'")
        return 0

    # The record of this run (for merge-review) describes the commit and tree
    # the stages judged, so both are taken before the first stage runs.
    head = git.commit_sha("HEAD")
    clean_at_start = git.working_tree_status() == ""
    results: list[progress.StageResult] = []
    exit_code = 1
    try:
        exit_code = progress.run_pipeline(
            None,
            stages,
            command="vrg-validate",
            label="vrg-validate",
            args=args,
            repo_root=repo_root,
            results_sink=results,
        )
    except KeyboardInterrupt:
        exit_code = 130
        raise
    finally:
        write_record(
            repo_root,
            head=head,
            exit_code=exit_code,
            full=args.check is None,
            clean_at_start=clean_at_start,
            results=results,
        )
    return exit_code


def _versions_resolve(
    vergil_config: config.VergilConfig, repo_root: Path
) -> tuple[str, ...] | None:
    """The config check: the repo's versions must resolve against the window.

    Runs the single resolver CI uses (epic vergil-project/.github#407), so a pin
    outside the published window, or a window that cannot be read, fails
    validation here instead of first failing in CI. A setting that selects the
    toolchain behind Vergil's back fails too (spec §6.5). Floor warnings (spec
    §6.4) are printed and do not fail.

    Returns the resolved CI versions (empty for a repo with no language), or
    ``None`` when the check fails.
    """
    language = vergil_config.project.primary_language
    if not language:
        return ()
    try:
        window = load_window(vergil_config.dependencies["vergil"])
        resolution = resolve_language(vergil_config, window, language, repo_root=repo_root)
        warnings = check_floors(repo_root, resolution, window)
        errors = check_toolchain_selection(repo_root, language)
    except VersionsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return None
    print(f"CI versions: {', '.join(resolution.versions)} (primary {resolution.primary})")
    for warning in warnings:
        print(f"WARNING: {warning}", file=sys.stderr)
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return None if errors else resolution.versions


def _test_context() -> str | None:
    """Return the validation context the worker count is tuned for, if known.

    vrg-container-run resolves the context outside the container (where the
    platform is observable) and forwards it as ``VRG_TEST_CONTEXT``. A run that
    did not come through vrg-container-run still recognizes a GitHub Actions
    runner as ``ci``; anything else is unknown and gets the ``[test]`` default.
    An unrecognized forwarded value is an error, never silently ignored.
    """
    forwarded = os.environ.get(config.TEST_CONTEXT_ENV, "").strip()
    if forwarded:
        if forwarded not in config.TEST_CONTEXTS:
            expected = ", ".join(config.TEST_CONTEXTS)
            msg = f"{config.TEST_CONTEXT_ENV}={forwarded!r} is not a known context ({expected})"
            raise config.ConfigError(msg)
        return forwarded
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return "ci"
    return None


def _resolve_test_workers(test_config: config.TestConfig) -> tuple[int | str, str]:
    """Resolve the xdist worker count and a label naming where it came from.

    Precedence, highest first: ``VRG_TEST_WORKERS`` (a one-machine override),
    ``[test.<context>].workers``, ``[test].workers``, ``"auto"`` (issue #3322).
    """
    override = os.environ.get(config.TEST_WORKERS_ENV, "").strip()
    if override:
        workers = config.parse_test_workers(override, config.TEST_WORKERS_ENV)
        return workers, config.TEST_WORKERS_ENV
    context = _test_context()
    workers, source = test_config.resolve_workers(context)
    return workers, f"{source}, context {context or '<unknown>'}"


# Exit 0 when pytest-xdist is importable, _XDIST_ABSENT_RC when it is not; any
# other exit is a broken probe and is reported as such, never read as "absent".
_XDIST_ABSENT_RC = 3
_XDIST_PROBE = (
    "import importlib.util, sys; "
    f"sys.exit(0 if importlib.util.find_spec('xdist') else {_XDIST_ABSENT_RC})"
)


def _test_interpreter(bin_dir: Path | None, purpose: str) -> Path:
    """Return the interpreter of the environment the tests run in.

    With no *bin_dir*, that is the environment the ``pytest`` on ``PATH``
    belongs to — the same ``pytest`` the TEST command resolves, with
    ``.venv/bin`` prepended. *purpose* prefixes the error when it cannot be
    found.
    """
    if bin_dir is None:
        pytest_path = shutil.which("pytest")
        if pytest_path is None:
            msg = f"{purpose}: pytest is not on PATH"
            raise ValidationError(msg)
        bin_dir = Path(pytest_path).parent
    interpreter = next(
        (bin_dir / name for name in ("python", "python3") if (bin_dir / name).exists()), None
    )
    if interpreter is None:
        msg = f"{purpose}: no python interpreter next to {bin_dir / 'pytest'}"
        raise ValidationError(msg)
    return interpreter


def _probe_xdist(bin_dir: Path | None = None) -> bool:
    """Return whether pytest-xdist is importable where the tests actually run.

    The probe must not look at this process's own interpreter: ``vrg-validate``
    is usually a uv tool with its own venv, while the tests run under the
    project's ``.venv`` (issue #3322). So it asks the interpreter of the test
    environment (see :func:`_test_interpreter`), or of the environment in
    *bin_dir* for an ``--all-versions`` stage. It runs at test-stage time,
    after the install stage has populated ``.venv``. A probe that cannot run,
    or errors, raises :class:`ValidationError` instead of degrading to serial.
    """
    interpreter = _test_interpreter(bin_dir, "cannot probe pytest-xdist")
    try:
        result = subprocess.run(  # noqa: S603
            [str(interpreter), "-c", _XDIST_PROBE],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"pytest-xdist probe could not run {interpreter}: {exc}"
        raise ValidationError(msg) from exc
    if result.returncode not in (0, _XDIST_ABSENT_RC):
        msg = (
            f"pytest-xdist probe failed: {interpreter} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
        raise ValidationError(msg)
    available = result.returncode == 0
    print(f"pytest-xdist probe: {interpreter} -> {'installed' if available else 'not installed'}")
    return available


_VERSION_PROBE = "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"


def _interpreter_version(interpreter: Path) -> str:
    """Return *interpreter*'s ``major.minor`` version; failure raises loudly."""
    try:
        result = subprocess.run(  # noqa: S603
            [str(interpreter), "-c", _VERSION_PROBE],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"version probe could not run {interpreter}: {exc}"
        raise ValidationError(msg) from exc
    if result.returncode != 0:
        msg = (
            f"version probe failed: {interpreter} exited {result.returncode}: "
            f"{result.stderr.strip()}"
        )
        raise ValidationError(msg)
    return result.stdout.strip()


def _default_test_version() -> str:
    """Return the Python version the plain test stage runs on (the container's)."""
    return _interpreter_version(_test_interpreter(None, "cannot read the test interpreter version"))


def _in_dev_container() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        with Path("/proc/1/mountinfo").open() as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 5 and parts[4] == "/" and "overlay" in parts:
                    return True
    except OSError:
        pass
    return False


def _run_common_checks(repo_root: Path) -> int:  # noqa: ARG001
    from vergil_tooling.bin.validate_common import main as common_main

    return common_main()


def _find_custom_validator(repo_root: Path) -> str | None:
    scripts_bin = repo_root / "scripts" / "bin"
    entry_point = shutil.which("vrg-validate-custom")
    if entry_point is not None:
        return entry_point
    local = scripts_bin / "validate-custom"
    # Anything present is returned, usable or not: a non-executable script must
    # fail the custom stage loudly (_check_custom_validator), never be skipped as
    # if the repo had no custom validator (#3349).
    if local.exists() or local.is_symlink():
        return str(local)
    return None


class ValidationError(Exception):
    """One or more validation commands failed."""


def _check_custom_validator(path: Path) -> None:
    if not path.is_file():
        msg = f"{path} exists but is not a regular file"
        raise ValidationError(msg)
    if not os.access(path, os.X_OK):
        msg = (
            f"{path} exists but is not executable; run `chmod +x {path}` and commit the mode change"
        )
        raise ValidationError(msg)


# The name of an environment variable, not a secret value. (S105 false positive.)
_CONAN_AUDIT_TOKEN_ENV = "CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER"  # noqa: S105


def _skip_conan_audit_scan(cmds: list[list[str]]) -> list[list[str]]:
    """Drop ``conan audit scan`` when no ConanCenter provider token is set.

    ``conan audit scan`` authenticates against ConanCenter's advisory API and
    hard-fails (``Missing authentication token``) without a provider token — an
    empty token does not degrade it. ``ci-audit.yml`` passes the token from a
    repo/org secret and documents that *vrg-validate* skips this gate with a
    notice when the secret is unset, so a cpp repo without the token is not
    wedged; this is where that promised skip actually lives. The rest of the
    audit stage (e.g. ``conan graph info``) still runs, and a real token
    restores the scan. Detection is by command shape, so only the cpp conan
    audit is affected.
    """
    if os.environ.get(_CONAN_AUDIT_TOKEN_ENV, "").strip():
        return cmds
    kept: list[list[str]] = []
    for cmd in cmds:
        if cmd[:3] == ["conan", "audit", "scan"]:
            print(
                f"NOTICE: skipping `conan audit scan` — {_CONAN_AUDIT_TOKEN_ENV} "
                "is unset, so there is no credential for ConanCenter's advisory "
                "API. Set the secret to enable CVE scanning.",
                file=sys.stderr,
            )
            continue
        kept.append(cmd)
    return kept


# Cargo's library target kinds: any of them makes ``cargo test --doc`` select a
# library target instead of refusing with "no library targets found".
_RUST_LIB_KINDS = frozenset({"lib", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"})


def _rust_lib_packages(repo_root: Path) -> tuple[list[str], list[str]]:
    """Return the workspace-member package names with and without a library target.

    ``cargo test --doc --workspace`` runs on every workspace member and
    refuses with "no library targets found" when none of them has a library
    target, so this reads exactly those packages, ``workspace_members`` (not
    ``workspace_default_members``), from ``cargo metadata --no-deps``
    (epic vergil-project/.github#1165, spec §6.4). A probe that cannot run,
    exits non-zero or prints unexpected output raises
    :class:`ValidationError` (issue #4378).
    """
    cmd = ["cargo", "metadata", "--no-deps", "--format-version", "1", "--locked"]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f"library-target probe could not run `{' '.join(cmd)}`: {exc}"
        raise ValidationError(msg) from exc
    if result.returncode != 0:
        msg = (
            f"library-target probe failed: `{' '.join(cmd)}` exited "
            f"{result.returncode}: {result.stderr.strip()}"
        )
        raise ValidationError(msg)
    try:
        metadata = json.loads(result.stdout)
        members = set(metadata["workspace_members"])
        with_lib: list[str] = []
        without_lib: list[str] = []
        for package in metadata["packages"]:
            if package["id"] not in members:
                continue
            kinds = {kind for target in package["targets"] for kind in target["kind"]}
            (with_lib if kinds & _RUST_LIB_KINDS else without_lib).append(package["name"])
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        msg = f"library-target probe: unexpected `cargo metadata` output: {exc!r}"
        raise ValidationError(msg) from exc
    return with_lib, without_lib


def _skip_rust_doctests_without_lib(cmds: list[list[str]], repo_root: Path) -> list[list[str]]:
    """Drop ``cargo test --doc`` when no workspace member has a library target.

    Rustdoc examples run only for a library target, and on a crate with none
    (a plain ``cargo new`` binary) ``cargo test --doc`` exits non-zero with
    "no library targets found", which would fail the TEST stage of a repo
    with no doctest to run. The probe runs at stage time, after the install
    stage has fetched the dependencies, and only when the command is present.
    """
    if RUST_DOCTEST_CMD not in cmds:
        return cmds
    with_lib, without_lib = _rust_lib_packages(repo_root)
    if with_lib:
        return cmds
    print(
        "NOTICE: skipping `cargo test --doc` — no library target in "
        f"{', '.join(without_lib) or 'the workspace members'}; "
        "rustdoc examples run only for a library target.",
        file=sys.stderr,
    )
    return [cmd for cmd in cmds if cmd != RUST_DOCTEST_CMD]


_RUST_LOCKFILE_MISSING = (
    "Cargo.lock is missing: run 'cargo generate-lockfile' in the container and commit it"
)


def check_rust_lockfile(repo_root: Path) -> None:
    """Fail a Rust repo that has ``Cargo.toml`` but no committed ``Cargo.lock``.

    Every Rust gate command runs with ``--locked``, which on a missing lockfile
    gives cargo's generic "needs to be updated" error. This check runs before
    the install stage instead, naming the file and how to create it. Libraries
    commit the lockfile too (epic vergil-project/.github#1165, spec §6.1).
    """
    if (repo_root / "Cargo.toml").is_file() and not (repo_root / "Cargo.lock").is_file():
        raise ValidationError(_RUST_LOCKFILE_MISSING)


def _command_stage(
    label: str,
    cmds: list[list[str]] | Callable[[], list[list[str]]],
    *,
    mode: StageMode,
    run_cmd: Callable[[list[str]], int] | None = None,
) -> Stage:
    """Run *cmds* as one stage; a callable is resolved when the stage runs.

    Each command runs through *run_cmd*, which returns its exit code; the
    default is a plain :func:`progress.run`.
    """

    def fn(_ctx: object) -> None:
        resolved = cmds if isinstance(cmds, list) else cmds()
        failed = 0
        for cmd in resolved:
            print(f"Running ({label}): {' '.join(cmd)}")
            rc = run_cmd(cmd) if run_cmd is not None else progress.run(cmd, check=False)
            if rc != 0:
                failed += 1
                if mode == "fail_fast":
                    break
        if failed:
            msg = f"{failed} of {len(resolved)} {label} command(s) failed"
            raise ValidationError(msg)

    return Stage(label, fn, mode=mode)


def _run_audit_command(cmd: list[str]) -> int:
    """Run one AUDIT command, giving ``pip-audit`` its network retry (#4423).

    ``pip-audit`` alone talks to a remote feed per package, so only it gets
    :func:`~vergil_tooling.lib.audit_retry.run_pip_audit`'s bounded retry on
    a network or timeout error; every other command runs once.
    """
    if cmd and cmd[0] == "pip-audit":
        return audit_retry.run_pip_audit(cmd)
    return progress.run(cmd, check=False)


def _not_bootstrapped_stage(name: str, language: str, missing: list[str]) -> Stage:
    """A language stage skipped, loudly, because the repo has no manifest yet.

    The test of bootstrapped-ness is the image warmup's
    (:func:`~vergil_tooling.lib.container_cache.unbootstrapped_manifests`), so
    a freshly initialised repo the image build skips warming is not then
    failed by validation's install stage (issue #4384). The stage is recorded
    ``warn`` (⚠, with the reason on its row and in the summary), never ``ok``,
    so the skip cannot pass unnoticed; ``warn`` still counts as passed, so the
    run can reach merge review. Adding the manifest runs the stage for real.
    """
    reason = (
        f"skipped: no {', '.join(missing)} ({language} repo not bootstrapped yet); "
        f"add {'it' if len(missing) == 1 else 'them'} to run the {name} stage"
    )

    def fn(_ctx: object) -> None:
        print(f"NOTICE: {name}: {reason}")
        progress.mark_warn(reason)

    return Stage(name, fn, mode="warn")


# How many source paths the sources-without-manifest failure names.
_SOURCES_SHOWN = 3


def _sources_without_manifest_stage(language: str, missing: list[str], sources: list[str]) -> Stage:
    """The install stage, failed: the repo has sources but none of its manifests.

    Such a repo has lost its manifest (a branch that deletes ``Cargo.toml``)
    or never got one (sources added without it), so it is not the unstarted
    repo :func:`_not_bootstrapped_stage` skips (issue #4384). The stage is
    ``fail_fast``, like the install it replaces, so no language stage runs on
    the broken repo.
    """
    shown = ", ".join(sources[:_SOURCES_SHOWN])
    if len(sources) > _SOURCES_SHOWN:
        shown += f" and {len(sources) - _SOURCES_SHOWN} more"
    msg = (
        f"no {', '.join(missing)}, but the repo has {language} sources ({shown}); "
        f"only a repo with no sources is skipped as not bootstrapped yet, so add the "
        f"{'manifest' if len(missing) == 1 else 'manifests'} or remove the sources"
    )

    def fn(_ctx: object) -> None:
        raise ValidationError(msg)

    return Stage("install", fn, mode="fail_fast")


def _repo_files(repo_root: Path) -> list[str]:
    """The repo's tracked and untracked, not ignored, files that exist on disk."""
    out = git.read_output(
        "-C", str(repo_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    return [path for path in out.split("\0") if path and (repo_root / path).is_file()]


def _bootstrap_state(language: str, repo_root: Path) -> tuple[list[str], list[str]]:
    """Return *language*'s missing manifests and its sources in the repo.

    Both are empty for a bootstrapped repo. The manifests are non-empty only
    when every one is missing
    (:func:`~vergil_tooling.lib.container_cache.unbootstrapped_manifests`);
    the repo's files are listed only then.
    """
    missing = unbootstrapped_manifests(language, repo_root)
    if not missing:
        return [], []
    return missing, language_sources(language, _repo_files(repo_root))


def _install_stage(
    language: str,
    install_cmds: list[list[str]],
    missing: list[str],
    sources: list[str],
    *,
    repo_root: Path,
) -> Stage | None:
    """The install stage for *language*, or ``None`` when it has no install.

    With every manifest missing it is skipped, loudly, when the repo has no
    sources, and failed when it has some. A Rust install first runs
    :func:`check_rust_lockfile`, so a missing ``Cargo.lock`` fails it.
    """
    if missing and sources:
        return _sources_without_manifest_stage(language, missing, sources)
    if not install_cmds:
        return None
    if missing:
        return _not_bootstrapped_stage("install", language, missing)
    if language == "rust":

        def rust_install_cmds() -> list[list[str]]:
            check_rust_lockfile(repo_root)
            return install_cmds

        return _command_stage("install", rust_install_cmds, mode="fail_fast")
    return _command_stage("install", install_cmds, mode="fail_fast")


def _python_test_stage(
    *,
    parallel: bool,
    workers: int | str,
    workers_source: str,
    xdist_probe: Callable[[], bool],
) -> Stage:
    """Build the Python TEST stage, deciding parallelism when it runs.

    The xdist probe is deferred to stage time because the project ``.venv`` it
    inspects is only populated by the earlier install stage. The decision is
    printed so the run log shows whether, and with how many workers, the suite
    ran in parallel.
    """

    def cmds() -> list[list[str]]:
        xdist_available = False
        if not parallel:
            print("Test parallelism: serial ([test].parallel = false)")
        elif xdist_probe():
            xdist_available = True
            print(f"Test parallelism: -n {workers} (from {workers_source})")
        else:
            print(
                "Test parallelism: serial (pytest-xdist is not installed in the test environment)"
            )
        return language_commands(
            "python",
            CheckKind.TEST,
            test_parallel=parallel,
            xdist_available=xdist_available,
            test_workers=workers,
        )

    return _command_stage("test", cmds, mode="fail_defer")


def _python_version_test_stage(
    version: str,
    *,
    parallel: bool,
    workers: int | str,
    workers_source: str,
    default_version: Callable[[], str],
    env_root: Path,
) -> Stage:
    """Build an ``--all-versions`` stage: the Python TEST gate on *version*.

    The dev container ships one interpreter, so the stage has uv install
    *version* as a managed interpreter and sync the dev group into its own
    environment under *env_root* — outside the workspace, so the project
    ``.venv`` is untouched. The gate argv is the plain test stage's, with the
    coverage and JUnit reports redirected beside that environment so they do
    not overwrite the workspace's. The container interpreter's own version is
    already covered by the plain test stage and is skipped. Commands run in
    order and the first failure fails the stage (issue #3547).
    """
    label = f"python {version} test"

    def fn(_ctx: object) -> None:
        if version == default_version():
            print(f"Python {version}: covered by the test stage (the dev container's interpreter)")
            return
        env_dir = env_root / f"python-{version}"
        bin_dir = env_dir / "bin"
        env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(env_dir),
            "VIRTUAL_ENV": str(env_dir),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        }
        setup = [
            ["uv", "python", "install", version],
            [
                "uv",
                "sync",
                "--frozen",
                "--group",
                "dev",
                "--python",
                version,
                "--python-preference",
                "only-managed",
            ],
        ]
        for cmd in setup:
            print(f"Running ({label}): {' '.join(cmd)}")
            if progress.run(cmd, env=env, check=False) != 0:
                msg = f"{label} command failed: {' '.join(cmd)}"
                raise ValidationError(msg)

        xdist_available = False
        if not parallel:
            print("Test parallelism: serial ([test].parallel = false)")
        elif _probe_xdist(bin_dir):
            xdist_available = True
            print(f"Test parallelism: -n {workers} (from {workers_source})")
        else:
            print(
                "Test parallelism: serial (pytest-xdist is not installed in the test environment)"
            )
        reports = {
            f"--cov-report=xml:{COVERAGE_REPORT}": f"--cov-report=xml:{env_dir / COVERAGE_REPORT}",
            f"--junitxml={JUNIT_REPORT}": f"--junitxml={env_dir / JUNIT_REPORT}",
        }
        argv = build_python_test_argv(
            xdist_available=xdist_available, parallel=parallel, workers=workers
        )
        argv = [str(bin_dir / "pytest"), *(reports.get(arg, arg) for arg in argv[1:])]
        print(f"Running ({label}): {' '.join(argv)}")
        if progress.run(argv, env=env, check=False) != 0:
            msg = f"{label} command failed: pytest (coverage gate on Python {version})"
            raise ValidationError(msg)

    return Stage(f"test (python {version})", fn, mode="fail_defer")


_DOCKERFILE_PREFIX = "Dockerfile"
_WORKFLOWS_DIR = ".github/workflows/"


def _semgrep_rulesets(language: str | None, repo_root: Path) -> list[str]:
    """The rulesets PR CI's semgrep job resolves for this repo.

    The job's action adds the Dockerfile rules when the checkout holds a
    ``Dockerfile*`` anywhere, and the GitHub Actions rules when
    ``.github/workflows/`` holds a ``*.yml`` or ``*.yaml``. Here the repo's
    tracked and untracked, not ignored, files stand in for the checkout.
    """
    files = _repo_files(repo_root)
    has_dockerfiles = any(Path(path).name.startswith(_DOCKERFILE_PREFIX) for path in files)
    has_workflows = any(
        path.startswith(_WORKFLOWS_DIR) and Path(path).suffix in (".yml", ".yaml") for path in files
    )
    return resolve_rulesets(
        language or "",
        has_dockerfiles=has_dockerfiles,
        has_workflows=has_workflows,
    )


def _run_semgrep(language: str | None, repo_root: Path) -> None:
    """Run the semgrep scan of PR CI's ``security / semgrep`` gate (issue #4003).

    The rulesets, the fleet rule exclusions and the gate's verdict
    (:func:`~vergil_tooling.lib.sarif.evaluate_findings`, where an
    in-source ``nosemgrep`` suppression does not count) are the gate's, so a
    finding fails here before the PR is opened. Semgrep fetches the rulesets
    from its registry, so the stage needs network access, as the audit stage
    does. Its usage metrics are turned off: a local run sends nothing beyond
    the ruleset download. The SARIF goes to a temporary directory, never the
    working tree. A scan that errors, or writes no SARIF, fails the stage.
    """
    rulesets = _semgrep_rulesets(language, repo_root)
    if not rulesets:
        reason = (
            f"no semgrep rulesets for language {language or '<not set>'}, "
            "and no Dockerfile or workflow to scan"
        )
        print(f"NOTICE: semgrep: {reason}")
        progress.mark_warn(reason)
        return
    print(f"Running (semgrep): semgrep scan --config {' --config '.join(rulesets)}")
    with tempfile.TemporaryDirectory(prefix="vrg-validate-semgrep-") as tmp:
        output = Path(tmp) / "semgrep-results.sarif"
        scan = run_scan(rulesets, repo_root, output, metrics="off")
        if scan.returncode > 1:
            raise ValidationError(scan_failure_message(scan))
        if not scan.sarif_produced:
            msg = f"semgrep exited {scan.returncode} but wrote no SARIF output"
            raise ValidationError(msg)
        result = evaluate_findings([parse_sarif(output)])
    if result.findings or result.suppressed:
        print(format_summary(result))
    if not result.passed:
        for finding in result.findings:
            print(
                f"ERROR: {finding.file}:{finding.line}: [{finding.rule_id}] {finding.message}",
                file=sys.stderr,
            )
        msg = f"{len(result.findings)} semgrep finding(s)"
        raise ValidationError(msg)
    print(f"semgrep: no findings ({len(result.suppressed)} suppressed in source)")


def _semgrep_stage(language: str | None, repo_root: Path) -> Stage:
    def fn(_ctx: object) -> None:
        _run_semgrep(language, repo_root)

    return Stage("semgrep", fn, mode="fail_defer")


def _stage_trivy_target(repo_root: Path, target: Path) -> None:
    """Copy the files a CI checkout would hold into *target*.

    The gate scans a fresh checkout, so the scan here sees the repo's
    tracked and untracked, not ignored, files and nothing else: no
    ``.venv``, ``node_modules``, ``target`` or ``build`` directory, which CI
    never has, can add a finding CI would not report.
    """
    for rel in _repo_files(repo_root):
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo_root / rel, dest)


def _trivy_command(cmd: Sequence[str], cwd: Path) -> int:
    print(f"Running (trivy): {' '.join(cmd)}")
    try:
        return progress.run(cmd, check=False, cwd=cwd)
    except FileNotFoundError as exc:
        msg = f"trivy not found: {exc}"
        raise ValidationError(msg) from exc


def _run_trivy(repo_root: Path, sleep: Callable[[float], None] = time.sleep) -> None:
    """Run the vulnerability scan of PR CI's ``security / trivy`` gate (issue #4445).

    The scan is vergil-actions' ``scan.sh`` (see
    :mod:`~vergil_tooling.lib.trivy_scan`): ``trivy fs --scanners vuln
    --severity CRITICAL,HIGH`` with the repo's ``.trivyignore``, run on a
    copy of the files a checkout holds, so a finding fails here before the
    PR is opened. The vulnerability database is cached between runs outside
    the working tree, and the reports go to a temporary directory. A scan
    that writes no report (a failed database download) is retried as in CI,
    then fails the stage as a scan error, never as a pass.
    """
    try:
        cache = trivy_scan.cache_dir(repo_root, os.environ)
    except trivy_scan.TrivyError as exc:
        raise ValidationError(str(exc)) from exc
    print(f"trivy: vulnerability database cache {cache}")
    with tempfile.TemporaryDirectory(prefix="vrg-validate-trivy-") as tmp:
        target = Path(tmp) / "checkout"
        workdir = Path(tmp) / "reports"
        target.mkdir()
        workdir.mkdir()
        _stage_trivy_target(repo_root, target)
        try:
            with trivy_scan.cache_lock(cache, print):
                outcome = trivy_scan.run_scan(
                    target, workdir, cache, run=_trivy_command, log=print, sleep=sleep
                )
        except OSError as exc:
            msg = f"trivy: cannot use the vulnerability database cache {cache}: {exc}"
            raise ValidationError(msg) from exc
    if outcome.result == "findings":
        msg = (
            f"Trivy found {trivy_scan.SEVERITY} vulnerabilities "
            f"(exit {outcome.returncode}; see the table above)"
        )
        raise ValidationError(msg)
    if outcome.result == "error":
        msg = (
            f"Trivy scan errored (no report produced) after {trivy_scan.SCAN_ATTEMPTS} "
            "attempts. This is a scan failure, not a vulnerability finding (a failed "
            "vulnerability database download is the usual cause); see the Trivy errors above"
        )
        raise ValidationError(msg)
    print(f"trivy: no {trivy_scan.SEVERITY} vulnerabilities")


def _trivy_stage(repo_root: Path) -> Stage:
    def fn(_ctx: object) -> None:
        _run_trivy(repo_root)

    return Stage("trivy", fn, mode="fail_defer")


# The mkdocs config CI's ``docs / docs`` job builds (vergil-actions
# .github/workflows/ci-docs.yml, its ``mkdocs-config`` input's default; no fleet
# caller overrides it), and the nav-patched sibling copy that staging writes,
# as vrg-container-docs does. Both are gitignored by the managed base
# gitignore, with the other staging outputs.
_MKDOCS_CONFIG = "docs/site/mkdocs.yml"
_STAGED_MKDOCS_CONFIG = "mkdocs.staged.yml"

# mkdocs-material 9.7+ prints a MkDocs 2.0 banner on every build unless this
# is set; CI's docs job sets it too.
_DOCS_ENV = {"NO_MKDOCS_2_WARNING": "1"}


def _stage_docs_sources(repo_root: Path, config: Path) -> Path:
    """Stage the docs sources as CI's docs job does; return the staged config.

    CI's ``actions/shared/docs/stage`` copies the changelog and release notes
    into ``<config dir>/docs`` and patches the release versions into the nav.
    Here the nav patch goes to a sibling copy of the config, never the
    tracked one, and staging is offline: CI also appends a CI-evidence
    download line to each release page, an external link that a strict build
    does not check, so it is left out rather than asking GitHub for it. Every
    path staging writes must be gitignored, so the working tree stays clean.
    """
    staged = config.with_name(_STAGED_MKDOCS_CONFIG)
    docs_dir = config.parent / "docs"
    releases_dir = repo_root / "releases"
    changelog = repo_root / "CHANGELOG.md"
    outputs = staging_outputs(
        staged_config=staged, docs_dir=docs_dir, releases_dir=releases_dir, changelog=changelog
    )
    error = unignored_staging_error(repo_root, outputs)
    if error is not None:
        raise ValidationError(error)
    stage_docs(docs_dir=docs_dir, releases_dir=releases_dir, changelog=changelog)
    shutil.copyfile(config, staged)
    patch_nav(staged, docs_dir / "releases")
    return staged


def _run_docs_build(language: str | None, repo_root: Path) -> None:
    """Run the strict mkdocs build of PR CI's ``docs / docs`` gate (issue #4446).

    A repo with no ``docs/site/mkdocs.yml`` has no docs build, and CI's job
    passes as a no-op, so the stage passes with a notice. Otherwise the
    sources are staged as CI stages them and ``mkdocs build --strict`` runs,
    through ``uv run`` for a Python repo as in CI, so a broken internal link
    or nav entry fails here before the PR is opened. ``uv run`` takes
    ``--frozen`` so it never rewrites ``uv.lock`` in the working tree. A
    Python repo with no mkdocs in its environment, like any other repo, uses
    the dev image's mkdocs-material. The site goes to a temporary directory,
    never the working tree.
    """
    config = repo_root / _MKDOCS_CONFIG
    if not config.is_file():
        print(f"NOTICE: docs: no {_MKDOCS_CONFIG}; no docs build to run, as in CI")
        return
    staged = _stage_docs_sources(repo_root, config)
    if language == "python":
        mkdocs = ["uv", "run", "--frozen", "--project", str(repo_root), "mkdocs"]
    else:
        mkdocs = ["mkdocs"]
    with tempfile.TemporaryDirectory(prefix="vrg-validate-docs-") as tmp:
        cmd = [*mkdocs, "build", "--strict", "-f", str(staged), "-d", tmp]
        print(f"Running (docs): {' '.join(cmd)}")
        try:
            rc = progress.run(cmd, check=False, env={**os.environ, **_DOCS_ENV})
        except FileNotFoundError as exc:
            msg = f"{mkdocs[0]} not found: {exc}"
            raise ValidationError(msg) from exc
    if rc != 0:
        msg = f"strict mkdocs build of {_MKDOCS_CONFIG} exited {rc}"
        raise ValidationError(msg)


def _docs_stage(language: str | None, repo_root: Path) -> Stage:
    def fn(_ctx: object) -> None:
        _run_docs_build(language, repo_root)

    return Stage("docs", fn, mode="fail_defer")


def _build_stages(
    check: str | None,
    language: str | None,
    repo_root: Path,
    *,
    cpp_std: str | None = None,
    cpp_stdlib: str | None = None,
    test_parallel: bool = True,
    test_workers: int | str = config.DEFAULT_TEST_WORKERS,
    test_workers_source: str = "[test].workers",
    xdist_probe: Callable[[], bool] | None = None,
    all_versions: tuple[str, ...] = (),
    default_version: Callable[[], str] | None = None,
) -> list[Stage]:
    stages: list[Stage] = []

    if check in (None, "common"):

        def common_fn(_ctx: object) -> None:
            rc = _run_common_checks(repo_root)
            if rc != 0:
                msg = f"common checks exited {rc}"
                raise ValidationError(msg)

        stages.append(Stage("common", common_fn, mode="fail_defer"))

    if language is not None and check not in ("common", "semgrep", "trivy", "docs"):
        kinds = [kind for kind in _LANGUAGE_CHECK_ORDER if check is None or check == kind.value]
        kind_cmds = [
            (
                kind,
                language_commands(
                    language,
                    kind,
                    cpp_std=cpp_std,
                    cpp_stdlib=cpp_stdlib,
                    test_parallel=test_parallel,
                    test_workers=test_workers,
                ),
            )
            for kind in kinds
        ]
        kind_cmds = [
            (kind, _skip_conan_audit_scan(cmds) if kind is CheckKind.AUDIT else cmds)
            for kind, cmds in kind_cmds
        ]
        kind_cmds = [(kind, cmds) for kind, cmds in kind_cmds if cmds]
        missing, sources = _bootstrap_state(language, repo_root)
        if kind_cmds and missing and not sources:
            install_cmds = language_commands(
                language, CheckKind.INSTALL, cpp_std=cpp_std, cpp_stdlib=cpp_stdlib
            )
            names = (["install"] if install_cmds else []) + [kind.value for kind, _ in kind_cmds]
            stages.extend(_not_bootstrapped_stage(name, language, missing) for name in names)
        elif kind_cmds:
            install_cmds = language_commands(
                language, CheckKind.INSTALL, cpp_std=cpp_std, cpp_stdlib=cpp_stdlib
            )
            install = _install_stage(language, install_cmds, missing, sources, repo_root=repo_root)
            if install is not None:
                stages.append(install)
            for kind, cmds in kind_cmds:
                if language == "rust" and kind is CheckKind.TEST:
                    stages.append(
                        _command_stage(
                            kind.value,
                            partial(_skip_rust_doctests_without_lib, cmds, repo_root),
                            mode="fail_defer",
                        )
                    )
                    continue
                if kind is CheckKind.AUDIT:
                    stages.append(
                        _command_stage(
                            kind.value, cmds, mode="fail_defer", run_cmd=_run_audit_command
                        )
                    )
                    continue
                if language != "python" or kind is not CheckKind.TEST:
                    stages.append(_command_stage(kind.value, cmds, mode="fail_defer"))
                    continue
                stages.append(
                    _python_test_stage(
                        parallel=test_parallel,
                        workers=test_workers,
                        workers_source=test_workers_source,
                        xdist_probe=xdist_probe or _probe_xdist,
                    )
                )
                env_root = Path(tempfile.gettempdir()) / "vrg-validate"
                stages.extend(
                    _python_version_test_stage(
                        version,
                        parallel=test_parallel,
                        workers=test_workers,
                        workers_source=test_workers_source,
                        default_version=default_version or _default_test_version,
                        env_root=env_root,
                    )
                    for version in all_versions
                )

    if check in (None, "semgrep"):
        stages.append(_semgrep_stage(language, repo_root))

    if check in (None, "trivy"):
        stages.append(_trivy_stage(repo_root))

    if check in (None, "docs"):
        stages.append(_docs_stage(language, repo_root))

    if check in (None, "custom"):
        custom = _find_custom_validator(repo_root)
        if custom is not None:
            if check == "custom" and language is not None:
                # Run alone, the validator still needs the dev dependencies the
                # full run's install stage would have provided.
                install_cmds = language_commands(
                    language, CheckKind.INSTALL, cpp_std=cpp_std, cpp_stdlib=cpp_stdlib
                )
                missing, sources = _bootstrap_state(language, repo_root)
                install = _install_stage(
                    language, install_cmds, missing, sources, repo_root=repo_root
                )
                if install is not None:
                    stages.append(install)

            def custom_fn(_ctx: object) -> None:
                _check_custom_validator(Path(custom))
                print(f"Running: {custom}")
                rc = progress.run((custom,), check=False)
                if rc != 0:
                    msg = f"custom validator exited {rc}"
                    raise ValidationError(msg)

            stages.append(Stage("custom", custom_fn, mode="fail_defer"))

    return stages


if __name__ == "__main__":
    sys.exit(main())
