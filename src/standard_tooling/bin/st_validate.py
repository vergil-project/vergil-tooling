"""Unified validation command.

Reads primary_language from standard-tooling.toml, then either runs a
specific check type (--check) or all checks in sequence:
  common -> install -> lint -> typecheck -> test -> audit -> custom
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from standard_tooling.lib import config, git
from standard_tooling.lib.validate_commands import CheckKind, language_commands

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
    return Path("/.dockerenv").exists() or bool(os.environ.get("ST_IN_DEV_CONTAINER"))


def _run_commands(cmds: list[str], label: str) -> int:
    for cmd in cmds:
        print(f"Running ({label}): {cmd}")
        result = subprocess.run(cmd, shell=True, check=False)  # noqa: S602
        if result.returncode != 0:
            return result.returncode
    return 0


def _run_common_checks(repo_root: Path) -> int:  # noqa: ARG001
    from standard_tooling.bin.validate_local_common_container import main as common_main

    return common_main()


def _find_custom_validator(repo_root: Path) -> str | None:
    scripts_bin = repo_root / "scripts" / "bin"
    entry_point = shutil.which("st-validate-local-custom")
    if entry_point is not None:
        return entry_point
    local = scripts_bin / "validate-local-custom"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return None


def _run_custom_validator(path: str) -> int:
    print(f"Running: {path}")
    result = subprocess.run((path,), check=False)  # noqa: S603
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="st-validate",
        description="Run validation checks from the command registry.",
    )
    parser.add_argument(
        "--check",
        choices=list(_CHECK_KINDS.keys()),
        default=None,
        help="Run only this check type. Omit to run all.",
    )
    args = parser.parse_args(argv)

    if not _in_dev_container():
        print(
            "ERROR: st-validate must run inside a dev container.\n"
            "       Run: st-docker-run -- st-validate",
            file=sys.stderr,
        )
        return 1

    repo_root = git.repo_root()

    try:
        st_config = config.read_config(repo_root)
        language = st_config.project.primary_language
    except FileNotFoundError:
        language = ""
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.check is not None:
        return _run_single_check(args.check, language, repo_root)
    return _run_all_checks(language, repo_root)


def _run_single_check(check: str, language: str, repo_root: Path) -> int:
    if check == "common":
        return _run_common_checks(repo_root)

    kind = _CHECK_KINDS[check]
    assert kind is not None
    cmds = language_commands(language, kind)
    if not cmds:
        print(f"No {check} commands for language '{language}'")
        return 0

    install_cmds = language_commands(language, CheckKind.INSTALL)
    if install_cmds:
        rc = _run_commands(install_cmds, "install")
        if rc != 0:
            return rc

    return _run_commands(cmds, check)


def _run_all_checks(language: str, repo_root: Path) -> int:
    print("=" * 40)
    print("st-validate")
    print(f"primary_language: {language or '<not set>'}")
    print("=" * 40)
    print()

    rc = _run_common_checks(repo_root)
    if rc != 0:
        return rc

    if language and language != "none":
        install_cmds = language_commands(language, CheckKind.INSTALL)
        if install_cmds:
            print()
            rc = _run_commands(install_cmds, "install")
            if rc != 0:
                return rc

        for kind in _LANGUAGE_CHECK_ORDER:
            cmds = language_commands(language, kind)
            if cmds:
                print()
                rc = _run_commands(cmds, kind.value)
                if rc != 0:
                    return rc

    custom = _find_custom_validator(repo_root)
    if custom is not None:
        print()
        rc = _run_custom_validator(custom)
        if rc != 0:
            return rc

    print()
    print("=" * 40)
    print("st-validate: all checks passed")
    print("=" * 40)
    return 0


if __name__ == "__main__":
    sys.exit(main())
