"""Resolve ecosystem metadata for a language.

Resolves build command, publish command, and credential requirements
for the given language identifier. In CI mode, outputs the credential
secret name to $GITHUB_OUTPUT; interactively, only reports whether
a credential is required.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib.languages import ecosystem_metadata, supported_languages
from vergil_tooling.lib.output import emit_error, is_ci, write_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-ecosystem-resolve",
        description="Resolve ecosystem metadata for a language.",
    )
    parser.add_argument("language", help="Language identifier")
    args = parser.parse_args(argv)

    try:
        info = ecosystem_metadata(args.language)
    except ValueError:
        emit_error(
            f"unsupported language: {args.language} "
            f"(supported: {', '.join(sorted(supported_languages()))})"
        )
        return 1

    eco = dataclasses.asdict(info)
    build_str = json.dumps(eco["build_cmd"]) if eco["build_cmd"] else ""
    publish_str = json.dumps(eco["publish_cmd"]) if eco["publish_cmd"] else ""
    cred_name: str = eco["credential_secret_name"] or ""

    if is_ci():
        write_output("build_cmd", build_str)
        write_output("publish_cmd", publish_str)
        _write_github_output("credential_secret_name", cred_name)
    else:
        print(f"build_cmd: {build_str}")
        print(f"publish_cmd: {publish_str}")
        print(f"credential_required: {bool(cred_name)}")

    return 0


def _write_github_output(key: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if output_path:
        with Path(output_path).open("a") as f:
            f.write(f"{key}={value}\n")


if __name__ == "__main__":
    sys.exit(main())
