"""Validate release workflow inputs.

Checks that the language is supported, that ``--registry-publish`` is
compatible with it, and that ``--container-tag`` has an image tag's shape.
"""

from __future__ import annotations

import argparse
import re
import sys

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.languages import ecosystem_metadata, supported_languages
from vergil_tooling.lib.output import emit_error

# The shape of a language image tag. cd-release passes the resolver's
# primary-container-tag (vrg-ci-resolve; epic vergil-project/.github#407): the
# window's version with any family prefix dropped, since the family lives in
# the image suffix ("clang-20" -> prod-cpp-clang:20, "node-24" ->
# prod-ts-node:24, "3.14" -> prod-python:3.14). So every language's tag is a
# dotted-numeric version. Language images publish no ":latest" tag, and a
# family-prefixed token such as "clang-20" is a version token, not a tag.
_IMAGE_TAG = re.compile(r"[0-9]+(?:\.[0-9]+)*")


@cli_main
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
        help="Programming language (a supported language). Omit for non-language projects.",
    )
    parser.add_argument(
        "--container-tag",
        default="",
        help=(
            "Release image tag: the resolver's primary-container-tag, a "
            "dotted-numeric version such as 3.14 or 20 (empty = not checked)"
        ),
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
        if args.container_tag and not _IMAGE_TAG.fullmatch(args.container_tag):
            errors.append(
                f"--container-tag {args.container_tag!r} is not a {language} image tag: "
                f"expected a dotted-numeric version such as the resolver's "
                f"primary-container-tag (e.g. 3.14, 21, 20), without a family prefix "
                f"such as clang- or node-"
            )

    if errors:
        for msg in errors:
            emit_error(msg)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
