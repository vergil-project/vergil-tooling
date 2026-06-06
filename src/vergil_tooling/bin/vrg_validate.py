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
import sys
from pathlib import Path

from vergil_tooling.lib import config, git, progress
from vergil_tooling.lib.languages import CheckKind, language_commands
from vergil_tooling.lib.progress import Stage, StageMode

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


class ValidationFailure(Exception):
    """One or more validation commands failed."""


def _command_stage(label: str, cmds: list[list[str]], *, mode: StageMode) -> Stage:
    def fn(_ctx: object) -> None:
        failed = 0
        for cmd in cmds:
            print(f"Running ({label}): {' '.join(cmd)}")
            rc = progress.run(cmd, check=False)
            if rc != 0:
                failed += 1
                if mode == "fail_fast":
                    break
        if failed:
            msg = f"{failed} of {len(cmds)} {label} command(s) failed"
            raise ValidationFailure(msg)

    return Stage(label, fn, mode=mode)


def _build_stages(check: str | None, language: str | None, repo_root: Path) -> list[Stage]:
    stages: list[Stage] = []

    if check in (None, "common"):

        def common_fn(_ctx: object) -> None:
            rc = _run_common_checks(repo_root)
            if rc != 0:
                msg = f"common checks exited {rc}"
                raise ValidationFailure(msg)

        stages.append(Stage("common", common_fn, mode="fail_defer"))

    if language is not None and check != "common":
        kinds = [
            kind
            for kind in _LANGUAGE_CHECK_ORDER
            if check is None or check == kind.value
        ]
        kind_cmds = [(kind, language_commands(language, kind)) for kind in kinds]
        kind_cmds = [(kind, cmds) for kind, cmds in kind_cmds if cmds]
        if kind_cmds:
            install_cmds = language_commands(language, CheckKind.INSTALL)
            if install_cmds:
                stages.append(_command_stage("install", install_cmds, mode="fail_fast"))
            stages.extend(
                _command_stage(kind.value, cmds, mode="fail_defer")
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
                    raise ValidationFailure(msg)

            stages.append(Stage("custom", custom_fn, mode="fail_defer"))

    return stages


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

    venv_bin = Path.cwd() / ".venv" / "bin"
    if venv_bin.is_dir() and str(venv_bin) not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"

    repo_root = git.repo_root()

    try:
        vergil_config = config.read_config(repo_root)
        language = vergil_config.project.primary_language
    except FileNotFoundError:
        language = None
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    stages = _build_stages(args.check, language, repo_root)
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


if __name__ == "__main__":
    sys.exit(main())
