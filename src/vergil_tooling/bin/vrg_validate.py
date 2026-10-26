"""Unified validation command.

Reads primary_language from vergil.toml, then either runs a
specific check type (--check) or all checks in sequence:
  common -> install -> lint -> typecheck -> test -> audit -> custom

The single checks are common, lint, typecheck, test, audit and custom.
``--check custom`` runs only the repo's custom validator
(``scripts/bin/validate-custom`` or ``vrg-validate-custom``), after the
install stage when the language has one so the validator sees the dev
dependencies. With no custom validator it reports that and exits 0; a
validator that is present but unusable still fails (issue #3650).

Checks run as progress-framework stages: install is fail_fast,
everything else is fail_defer so all failures are reported in one run.

``--all-versions`` (Python only) adds one test stage per resolved CI version
after the test stage, so the coverage gate runs on every interpreter PR-CI
tests, not only the dev container's (issue #3547).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, progress
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.languages import (
    COVERAGE_REPORT,
    JUNIT_REPORT,
    CheckKind,
    build_python_test_argv,
    language_commands,
)
from vergil_tooling.lib.progress import Stage, StageMode
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.floors import check_floors, check_toolchain_selection
from vergil_tooling.lib.versions.resolve import resolve_language
from vergil_tooling.lib.versions.window import load_window

if TYPE_CHECKING:
    from collections.abc import Callable

_CHECK_KINDS = {
    "common": None,
    "lint": CheckKind.LINT,
    "typecheck": CheckKind.TYPECHECK,
    "test": CheckKind.TEST,
    "audit": CheckKind.AUDIT,
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

    return progress.run_pipeline(
        None,
        stages,
        command="vrg-validate",
        label="vrg-validate",
        args=args,
        repo_root=repo_root,
    )


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


def _command_stage(
    label: str,
    cmds: list[list[str]] | Callable[[], list[list[str]]],
    *,
    mode: StageMode,
) -> Stage:
    """Run *cmds* as one stage; a callable is resolved when the stage runs."""

    def fn(_ctx: object) -> None:
        resolved = cmds if isinstance(cmds, list) else cmds()
        failed = 0
        for cmd in resolved:
            print(f"Running ({label}): {' '.join(cmd)}")
            rc = progress.run(cmd, check=False)
            if rc != 0:
                failed += 1
                if mode == "fail_fast":
                    break
        if failed:
            msg = f"{failed} of {len(resolved)} {label} command(s) failed"
            raise ValidationError(msg)

    return Stage(label, fn, mode=mode)


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

    if language is not None and check != "common":
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
        if kind_cmds:
            install_cmds = language_commands(
                language, CheckKind.INSTALL, cpp_std=cpp_std, cpp_stdlib=cpp_stdlib
            )
            if install_cmds:
                stages.append(_command_stage("install", install_cmds, mode="fail_fast"))
            for kind, cmds in kind_cmds:
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

    if check in (None, "custom"):
        custom = _find_custom_validator(repo_root)
        if custom is not None:
            if check == "custom" and language is not None:
                # Run alone, the validator still needs the dev dependencies the
                # full run's install stage would have provided.
                install_cmds = language_commands(
                    language, CheckKind.INSTALL, cpp_std=cpp_std, cpp_stdlib=cpp_stdlib
                )
                if install_cmds:
                    stages.append(_command_stage("install", install_cmds, mode="fail_fast"))

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
