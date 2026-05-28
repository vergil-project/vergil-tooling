"""Validate release workflow inputs.

Checks that the language is supported and that flag combinations
(container-tag, registry-publish) are compatible with the language.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.languages import ecosystem_metadata, supported_languages
from vergil_tooling.lib.output import emit_error

_CONTAINER_LANGUAGES = frozenset({"python", "java", "ruby", "rust", "go"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-release-validate-inputs",
        description="Validate release workflow inputs.",
    )
    parser.add_argument(
        "language_positional",
        nargs="?",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--language",
        default="",
        dest="language_flag",
        help="Programming language (go, java, python, ruby, rust). Omit for non-language projects.",
    )
    parser.add_argument(
        "--container-tag",
        default="",
        help="Container image tag (empty = no container publish)",
    )
    parser.add_argument(
        "--registry-publish",
        action="store_true",
        help="Whether to publish to a package registry",
    )
    args = parser.parse_args(argv)

    language = args.language_flag or args.language_positional
    used_flag = bool(args.language_flag)

    if not language:
        return 0

    errors: list[str] = []
    langs = supported_languages()

    if language not in langs:
        if used_flag:
            errors.append(
                f"unsupported language: {language} (supported: {', '.join(sorted(langs))})"
            )
        else:
            return 0
    else:
        info = ecosystem_metadata(language)
        if args.registry_publish and info.publish_cmd is None:
            errors.append(
                f"--registry-publish is not supported for {language} (no publish command defined)"
            )
        if args.container_tag and language not in _CONTAINER_LANGUAGES:
            errors.append(f"--container-tag is not supported for {language}")

    if errors:
        for msg in errors:
            emit_error(msg)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
