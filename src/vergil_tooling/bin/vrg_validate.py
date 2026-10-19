"""Unified validation command.

Reads primary_language from vergil.toml, then either runs a
specific check type (--check) or all checks in sequence:
  common -> install -> lint -> typecheck -> test -> audit -> custom

Checks run as progress-framework stages: install is fail_fast,
everything else is fail_defer so all failures are reported in one run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, progress
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.languages import CheckKind, language_commands
from vergil_tooling.lib.progress import Stage, StageMode

if TYPE_CHECKING:
    from collections.abc import Callable

_CHECK_KINDS = {
    "common": None,
    "lint": CheckKind.LINT,
    "typecheck": CheckKind.TYPECHECK,
    "test": CheckKind.TEST,
    "audit": CheckKind.AUDIT,
}

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
    progress.add_progress_args(parser, ())
    args = parser.parse_args(argv)

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
    )
    if not stages:
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


def _probe_xdist() -> bool:
    """Return whether pytest-xdist is importable where the tests actually run.

    The probe must not look at this process's own interpreter: ``vrg-validate``
    is usually a uv tool with its own venv, while the tests run under the
    project's ``.venv`` (issue #3322). So it asks the interpreter of the
    environment the ``pytest`` on ``PATH`` belongs to — the same ``pytest`` the
    TEST command resolves, with ``.venv/bin`` prepended. It runs at test-stage
    time, after the install stage has populated ``.venv``. A probe that cannot
    run, or errors, raises :class:`ValidationError` instead of degrading to
    serial.
    """
    pytest_path = shutil.which("pytest")
    if pytest_path is None:
        msg = "cannot probe pytest-xdist: pytest is not on PATH"
        raise ValidationError(msg)
    bin_dir = Path(pytest_path).parent
    interpreter = next(
        (bin_dir / name for name in ("python", "python3") if (bin_dir / name).exists()), None
    )
    if interpreter is None:
        msg = f"cannot probe pytest-xdist: no python interpreter next to {pytest_path}"
        raise ValidationError(msg)
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
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None


class ValidationError(Exception):
    """One or more validation commands failed."""


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
            stages.extend(
                _python_test_stage(
                    parallel=test_parallel,
                    workers=test_workers,
                    workers_source=test_workers_source,
                    xdist_probe=xdist_probe or _probe_xdist,
                )
                if language == "python" and kind is CheckKind.TEST
                else _command_stage(kind.value, cmds, mode="fail_defer")
                for kind, cmds in kind_cmds
            )

    if check is None:
        custom = _find_custom_validator(repo_root)
        if custom is not None:

            def custom_fn(_ctx: object) -> None:
                print(f"Running: {custom}")
                rc = progress.run((custom,), check=False)
                if rc != 0:
                    msg = f"custom validator exited {rc}"
                    raise ValidationError(msg)

            stages.append(Stage("custom", custom_fn, mode="fail_defer"))

    return stages


if __name__ == "__main__":
    sys.exit(main())
