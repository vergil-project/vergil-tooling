"""Semgrep scan orchestration: ruleset resolution and scan execution."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# The registry ruleset each language scans with: the one source of this mapping
# for CI's semgrep job and vrg-validate's semgrep stage (issue #4447).
#
# cpp: semgrep gives no C++ coverage here, and the community registry cannot
# make it: `p/cpp` was never published and the registry's `cpp` namespace holds
# no rules. `p/c` is real and correct, but it applies to `.c` files only. Do not
# add `p/security-audit` or `p/default` hoping to cover C++: they reach `.cpp`
# only through generic or regex rules, which is not C++ analysis. C++ static
# analysis comes from the quality stage instead (clang-format, run-clang-tidy,
# cppcheck and the -Werror typecheck build). See vergil-project/vergil-actions#853.
#
# typescript: `p/typescript` is the registry's JS/TS ruleset.
_LANGUAGE_RULESETS: dict[str, str] = {
    "python": "p/python",
    "go": "p/golang",
    "rust": "p/rust",
    "cpp": "p/c",
    "typescript": "p/typescript",
}

# Rules excluded from every fleet scan, by full semgrep rule id.
#
# github-actions-mutable-action-tag flags every `uses: …@vN` action ref. It is
# exempted fleet-wide pending vergil-project/.github#194 (pin third-party action
# SHAs once pin-advancement tooling exists); our own vergil-actions@v2.1 refs are
# a permanent exception (our release line).
#
# secrets-inherit flags every `secrets: inherit`. vrg-validate's common
# `secrets-inherit` check replaces it (epic vergil-project/.github#1384): it
# allows `inherit` for the vergil-actions workflows that read the automation
# environment's secrets and refuses it everywhere else
# (vergil_tooling.lib.secrets_inherit), so the rule has one definition and the
# allowed callers need no `nosemgrep`. All other rules stay enforced.
DEFAULT_EXCLUDED_RULES: tuple[str, ...] = (
    "yaml.github-actions.security.github-actions-mutable-action-tag"
    ".github-actions-mutable-action-tag",
    "yaml.github-actions.security.secrets-inherit.secrets-inherit",
)


@dataclass(frozen=True)
class ScanResult:
    returncode: int
    sarif_produced: bool
    stdout: str = ""
    stderr: str = ""


def resolve_rulesets(
    language: str,
    *,
    has_dockerfiles: bool = False,
    has_workflows: bool = False,
    extra_config: list[str] | None = None,
) -> list[str]:
    """Resolve semgrep rulesets for the given language and repo context.

    Each ruleset appears once, in first-seen order: an *extra_config* entry
    already resolved (a caller still passing a language's own ruleset
    explicitly, say) is not repeated.
    """
    rulesets: list[str] = []

    base = _LANGUAGE_RULESETS.get(language)
    if base:
        rulesets.append(base)

    if has_dockerfiles:
        rulesets.append("p/dockerfile")

    if has_workflows:
        rulesets.append("p/github-actions")

    for ruleset in extra_config or []:
        if ruleset not in rulesets:
            rulesets.append(ruleset)

    return rulesets


def run_scan(
    rulesets: list[str],
    target_dir: Path,
    output_path: Path,
    *,
    exclude_rules: list[str] | None = None,
    metrics: str | None = None,
) -> ScanResult:
    """Execute semgrep scan with the given rulesets.

    Semgrep exit codes: 0 = clean, 1 = findings, >1 = error.
    All three can produce valid SARIF output. Semgrep's captured stdout and
    stderr are returned so a caller can show why a scan failed.

    The fleet defaults in ``DEFAULT_EXCLUDED_RULES`` are always excluded via
    ``--exclude-rule``; any ``exclude_rules`` the caller supplies are added on
    top of (never in place of) the defaults.

    *metrics*, when given, is passed as semgrep's ``--metrics`` value; with
    none, semgrep's own default applies, as in PR CI.
    """
    excluded: list[str] = list(DEFAULT_EXCLUDED_RULES)
    for rule in exclude_rules or []:
        if rule not in excluded:
            excluded.append(rule)

    cmd: list[str] = ["semgrep", "scan", "--sarif", "--output", str(output_path)]
    for ruleset in rulesets:
        cmd.extend(["--config", ruleset])
    for rule in excluded:
        cmd.extend(["--exclude-rule", rule])
    if metrics is not None:
        cmd.extend(["--metrics", metrics])
    cmd.append(str(target_dir))

    result = subprocess.run(  # noqa: S603
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sarif_produced = output_path.is_file() and output_path.stat().st_size > 0

    return ScanResult(
        returncode=result.returncode,
        sarif_produced=sarif_produced,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _last_line(text: str) -> str:
    """Return the last non-empty line of *text*, stripped, or ``""``."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def scan_failure_message(scan_result: ScanResult) -> str:
    """Print semgrep's captured output to stderr; return the failure's message.

    The message names the exit code and, when semgrep printed anything, the
    last line of its stderr (else of its stdout), which carries the cause.
    """
    for name, text in (("stdout", scan_result.stdout), ("stderr", scan_result.stderr)):
        if text.strip():
            print(f"--- semgrep {name} ---", file=sys.stderr)
            print(text.rstrip("\n"), file=sys.stderr)
    msg = f"semgrep scan failed with exit code {scan_result.returncode}"
    detail = _last_line(scan_result.stderr) or _last_line(scan_result.stdout)
    if detail:
        msg = f"{msg}: {detail}"
    return msg
