"""Resolve ecosystem metadata for a language.

Resolves build command, publish command, and publish environment
variable for the given language identifier.
"""

from __future__ import annotations

import argparse
import json
import sys

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

    build_str = json.dumps(info.build_cmd) if info.build_cmd else ""
    publish_str = json.dumps(info.publish_cmd) if info.publish_cmd else ""
    env_var = info.publish_env_var or ""

    if is_ci():
        write_output("build_cmd", build_str)
        write_output("publish_cmd", publish_str)
        write_output("publish_env_var", env_var)
    else:
        print(f"build_cmd: {build_str}")
        print(f"publish_cmd: {publish_str}")
        print(f"publish_env_var: {env_var}")
        print(f"publish_requires_auth: {bool(env_var)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
