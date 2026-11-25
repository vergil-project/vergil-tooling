"""Safe gh wrapper for AI agent sessions.

Enforces identity-aware subcommand allowlists and flag deny lists.
Injects GitHub App installation tokens when available.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib import (
    epics,
    gh_version,
    github,
    identity_mode,
    operational_close,
    retry,
    trust,
)
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Callable

_ALLOWED: dict[str, set[str]] = {
    "issue": {"view", "close", "reopen", "edit", "list", "comment", "transfer"},
    # `update-branch` (optionally `--rebase`) brings a PR branch up to date
    # with its base server-side, with no checkout or force-push, and crosses
    # no human gate. It writes, so it stays off the audit allowlist. See
    # issue #3542.
    "pr": {
        "view",
        "checks",
        "list",
        "diff",
        "comment",
        "edit",
        "review",
        "merge",
        "update-branch",
    },
    # `rerun` lets an agent retry a failed CI job without a human round trip:
    # it repeats an existing run and crosses no human gate. It starts CI
    # work, so it stays off the audit allowlist. See issue #3420.
    "run": {"list", "view", "watch", "rerun"},
    # `workflow` and `cache` are read-only here. `workflow run` is denied
    # outright (see _DENIED_ALWAYS); enable/disable and cache delete fall to
    # the action-level allowlist's deny-by-default. See issue #3420.
    "workflow": {"list", "view"},
    "cache": {"list"},
    "repo": {"view", "list"},
    # `release` is read-only here: `list` and `view` query release facts
    # directly (#2539), and `download` fetches a release's assets, such as its
    # evidence bundle and manifest, so a validation run reads them through the
    # wrapper rather than curl (#5002). It writes only local files. Write
    # actions (create/edit/delete/upload) are denied by the action-level
    # allowlist's deny-by-default.
    "release": {"list", "view", "download"},
    "label": {"list", "create"},
    # `variable` is read-only here: `list` and `get` read a repo's or org's
    # Actions variables (not secrets). `set` and `delete` are denied by the
    # action-level allowlist's deny-by-default. See issue #5001.
    "variable": {"list", "get"},
    # `gh search` is read-only across all its subcommands (code/commits/
    # issues/prs/repos), so it fits the wrapper's no-mutation safety model.
    # Enables cross-org GitHub code search for fleet-wide sweeps (#2505).
    "search": {"code", "commits", "issues", "prs", "repos"},
}

_ALLOWED_AUDIT: dict[str, set[str]] = {
    "pr": {"view", "diff", "list", "checks", "comment", "review"},
    # Listing and viewing workflows and caches only reads CI configuration
    # and state, which a reviewer may need to judge a PR's checks, so the
    # least-privilege audit identity gets them too. See issue #3420.
    "workflow": {"list", "view"},
    "cache": {"list"},
}

_DENIED_ALWAYS: dict[str, dict[str, str]] = {
    "issue": {
        # Issue creation must go through a shape-specific tool so there is no
        # bypass around linkage. Denied for every identity. See issues #2017,
        # #2069. Each tool forces its shape: a task is born linked to an epic, an
        # epic is top-level in <org>/.github, an intake capture is born linked
        # under its home's intake epic.
        "create": (
            "Use vrg-issue-create (task under an epic), vrg-epic-create "
            "(top-level epic), or vrg-intake-create (intake issue linked under "
            "the intake epic) instead of gh issue create."
        ),
    },
    "pr": {
        "close": "gh pr close is denied by vrg-gh.",
    },
    "repo": {
        "edit": "gh repo edit is denied by vrg-gh.",
        "create": "gh repo create is denied by vrg-gh.",
        "delete": "gh repo delete is denied by vrg-gh.",
    },
    # Dispatching a workflow can start CD or a release, which is a human
    # action. Denied for every identity. See issue #3420.
    "workflow": {
        "run": (
            "Dispatching a workflow can start CD or a release, which is a "
            "human action; ask the human to run it."
        ),
    },
}

_DENIED_AGENT: dict[str, dict[str, str]] = {
    # The agent opens and merges its own PR only through the gated path:
    # report-ready records it, vrg-submit-pr runs the gates, opens it and turns
    # auto-merge on (epic vergil-project/.github#1384). Raw gh stays denied.
    "pr": {
        "create": (
            "The user agent opens a PR only through the gated path: record it with "
            "'vrg-pr-workflow report-ready', then run vrg-submit-pr."
        ),
        "edit": (
            "No agent edits a PR with gh; the user agent corrects the PR metadata with "
            "'vrg-pr-workflow report-ready' before vrg-submit-pr opens the PR."
        ),
        "merge": (
            "The user agent merges its PR only through the gated path: vrg-submit-pr turns "
            "auto-merge on after 'vrg-pr-workflow report-ready'."
        ),
    },
    # `issue` has no agent-denied pairs: `close` and `reopen` are both
    # non-destructive and reversible, so both are allowed for the USER agent
    # (closing is automated/mechanical; reopening is trivially re-closed and
    # lets an agent run reopen remediation directly). AUDIT stays barred from
    # both because `issue` is not in its allowlist at all (least privilege).
    # See issues #1966 (original close/reopen split) and #2260 (un-denying
    # reopen).
}

_DENIED_HUMAN: dict[str, dict[str, str]] = {
    "pr": {
        "create": "Use vrg-submit-pr instead of gh pr create.",
    },
}

_DENIED_TOP: dict[str, str] = {
    "auth": "gh auth is denied by vrg-gh.",
}

# `gh api` is identity-aware (handled in main, not in _DENIED_TOP):
#   human -> full; audit -> read-only GET; user/other agent -> denied.
# These flags flip gh's default verb from GET to POST.
_API_WRITE_FLAGS: set[str] = {"-f", "-F", "--field", "--raw-field", "--input"}

_MIN_MERGE_ARGS = 3

# `run job-log` is a wrapper-native command, not a gh subcommand: gh can read
# one job's log only by downloading the whole run's log archive (`gh run view
# --job <id> --log`), which fails on a run with many jobs. It maps to exactly
# one read-only REST endpoint, built here from validated parts, so it reopens
# none of the `gh api` access the user identity is denied. It is available to
# every identity, audit included, because it only reads. See issue #4597.
_JOB_LOG_SUB = "job-log"
_JOB_LOG_USAGE = "usage: vrg-gh run job-log --repo <owner>/<repo> --job <job-id>"
# GitHub owner (user or org) names: alphanumerics and hyphens, at most 39
# characters, not starting with a hyphen. Repository names: alphanumerics,
# '.', '-' and '_', at most 100 characters. Neither admits '/', '?', '#', '%',
# '{' or whitespace, so the endpoint path cannot be redirected or extended.
_OWNER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_JOB_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
# Terminal escape sequences: CSI (ESC [ ... final byte), OSC (ESC ] ... BEL or
# ESC \), and any other two-byte ESC sequence. Then every remaining C0 control
# character except tab and newline, DEL, and the C1 controls (which include
# the 8-bit CSI and OSC introducers).
_ESCAPE_SEQUENCE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?|\x1b[^\n]?"
)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@cli_main
def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("usage: vrg-gh <subcommand> <action> [args...]", file=sys.stderr)
        return 2

    if argv[0] in ("-h", "--help"):
        _print_help()
        return 0

    top = argv[0]

    if top in _DENIED_TOP:
        msg = _DENIED_TOP[top]
        print(f"vrg-gh: {top} is denied. {msg}", file=sys.stderr)
        return 1

    if top == "run" and len(argv) >= 2 and argv[1] == _JOB_LOG_SUB:  # noqa: PLR2004
        return _job_log(argv[2:])

    # Identity-aware `gh api`: the broad escape hatch is gated per identity.
    if top == "api":
        mode = identity_mode.current_mode()
        if mode == identity_mode.IdentityMode.USER:
            print(
                "vrg-gh: gh api is denied for the user identity "
                "(broad write-capable escape hatch).",
                file=sys.stderr,
            )
            return 1
        if mode == identity_mode.IdentityMode.AUDIT and not _api_is_get(argv):
            print(
                "vrg-gh: gh api is restricted to read-only GET calls for the audit identity.",
                file=sys.stderr,
            )
            return 1
        # human (full) or audit GET: execute directly, bypassing the
        # subcommand-pair allowlist (api has no fixed sub-actions).
        return _exec_gh(argv)

    allowed = _get_allowed()

    if top not in allowed:
        print(
            f"vrg-gh: {top} is not recognized. Allowed: {', '.join(sorted(allowed))}",
            file=sys.stderr,
        )
        return 1

    if len(argv) < 2:  # noqa: PLR2004
        print(
            f"vrg-gh: {top} requires a subcommand. Allowed: {', '.join(sorted(allowed[top]))}",
            file=sys.stderr,
        )
        return 1

    sub = argv[1]

    denied_pairs = _get_denied_pairs()
    if top in denied_pairs and sub in denied_pairs[top]:
        msg = denied_pairs[top][sub]
        print(f"vrg-gh: {top} {sub} is denied. {msg}", file=sys.stderr)
        return 1

    if sub not in allowed[top]:
        print(
            f"vrg-gh: {top} {sub} is not recognized. Allowed: {', '.join(sorted(allowed[top]))}",
            file=sys.stderr,
        )
        return 1

    mode = identity_mode.current_mode()

    if (
        top == "pr"
        and sub == "review"
        and "--approve" in argv
        and mode not in (identity_mode.IdentityMode.AUDIT, identity_mode.IdentityMode.HUMAN)
    ):
        print(
            "vrg-gh: pr review --approve is denied. "
            "PR approval is restricted to the audit identity or a human maintainer.",
            file=sys.stderr,
        )
        return 1

    if (
        top == "pr"
        and sub == "merge"
        and not identity_mode.is_agent()
        and len(argv) < _MIN_MERGE_ARGS
    ):
        print(
            "vrg-gh: pr merge is denied. pr merge requires a PR number or URL.",
            file=sys.stderr,
        )
        return 1

    if top == "issue" and sub == "close" and identity_mode.is_agent():
        refused = _operational_close_refusal(argv[2:])
        if refused is not None:
            print(f"vrg-gh: issue close is refused. {refused}", file=sys.stderr)
            return 1

    rc = _exec_gh(argv)
    # `--help` forwards to gh's own help and exits 0 with no rerun requested.
    if (
        top == "run"
        and sub == "rerun"
        and rc == 0
        and not any(arg in ("-h", "--help") for arg in argv[2:])
    ):
        print(_rerun_confirmation(argv[2:]))
    return rc


def _rerun_confirmation(args: list[str]) -> str:
    """Return the line confirming a successful ``run rerun`` of *args*.

    gh prints its own "Requested rerun" line only when its stdout is a
    terminal, and vrg-gh captures gh's output, so a successful rerun would
    otherwise print nothing and look the same as a no-op (#5175). A run or
    job ID is named only when it is all digits, so no argument text reaches
    the terminal unchecked.
    """
    job: str | None = None
    run: str | None = None
    failed = False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg in ("--job", "-j") and i < len(args):
            job = args[i]
            i += 1
        elif arg.startswith("--job="):
            job = arg.removeprefix("--job=")
        elif arg in ("--repo", "-R"):
            i += 1
        elif arg == "--failed":
            failed = True
        elif not arg.startswith("-") and run is None:
            run = arg
    if job is not None:
        return f"Requested rerun of job {job}" if _JOB_ID_RE.fullmatch(job) else "Requested rerun"
    scope = " (failed jobs)" if failed else ""
    if run is not None and _JOB_ID_RE.fullmatch(run):
        return f"Requested rerun{scope} of run {run}"
    return f"Requested rerun{scope}"


def _get_allowed() -> dict[str, set[str]]:
    if identity_mode.current_mode() == identity_mode.IdentityMode.AUDIT:
        return _ALLOWED_AUDIT
    return _ALLOWED


def _get_denied_pairs() -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for top, subs in _DENIED_ALWAYS.items():
        merged.setdefault(top, {}).update(subs)
    extra = _DENIED_AGENT if identity_mode.is_agent() else _DENIED_HUMAN
    for top, subs in extra.items():
        merged.setdefault(top, {}).update(subs)
    return merged


def _api_is_get(argv: list[str]) -> bool:
    """Return True if a ``gh api`` invocation is a read-only GET.

    gh defaults to GET, flips to POST when fields are present, and
    honors an explicit ``-X``/``--method``.
    """
    method: str | None = None
    has_fields = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-X", "--method"):
            if i + 1 < len(argv):
                method = argv[i + 1].upper()
            i += 2
            continue
        if arg.startswith("--method="):
            method = arg.split("=", 1)[1].upper()
        elif arg in _API_WRITE_FLAGS or arg.startswith(("--field=", "--raw-field=")):
            has_fields = True
        i += 1
    if method is not None:
        return method == "GET"
    return not has_fields


def _parse_repo_slug(value: str) -> str | None:
    """``OWNER/REPO`` of a repository value (``-R``/``--repo``, ``repo view``).

    gh accepts ``OWNER/REPO``, ``HOST/OWNER/REPO``, and full URL forms.
    """
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
            break
    parts = [part for part in value.split("/") if part]
    if len(parts) >= 3:  # noqa: PLR2004 -- HOST/OWNER/REPO
        return f"{parts[1]}/{parts[2]}"
    if len(parts) == 2:  # noqa: PLR2004 -- OWNER/REPO
        return f"{parts[0]}/{parts[1]}"
    return None


def _parse_repo_owner(value: str) -> str | None:
    """Extract the owner from a repository value (``-R``/``--repo``, ``repo view``)."""
    slug = _parse_repo_slug(value)
    return slug.split("/", 1)[0] if slug is not None else None


# The `issue close` reasons that do not claim the task was done; any other
# close (gh's default reason is "completed") is gated for an operational task.
_NOT_DONE_REASONS = frozenset({"not planned", "duplicate"})
_ISSUE_NUMBER_RE = re.compile(r"#?([1-9][0-9]*)")
_ISSUE_URL_RE = re.compile(
    r"https?://[^/\s]+/([^/\s]+)/([^/\s]+)/issues/([1-9][0-9]*)/?(?:[?#]\S*)?"
)


def _operational_close_refusal(args: list[str]) -> str | None:
    """Why an agent's ``issue close`` *args* must not run, or ``None`` when they may.

    An operational task closes as completed only once a trusted comment on it
    records ``Outcome: SUCCESS`` (:func:`operational_close.refusal`), so a close
    sent in one batch with a result comment that failed is refused rather than
    closing the task with no result above it (#4997). A close as ``not
    planned`` or a duplicate claims no result and is not gated, and neither is
    ``--help``. A target this cannot resolve, or a read that cannot complete,
    refuses the close rather than letting it through unchecked.
    """
    if any(arg in ("-h", "--help") for arg in args):
        return None
    parsed = _parse_args(args, _VALUE_FLAGS[("issue", "close")])
    reason = "completed"
    for flag, value in parsed.flag_values:
        if flag in ("--reason", "-r"):
            reason = value.strip().lower()
    if any(flag == "--duplicate-of" for flag, _ in parsed.flag_values):
        reason = "duplicate"
    if reason in _NOT_DONE_REASONS:
        return None
    try:
        ref = _close_target(parsed)
        return operational_close.refusal(ref)
    except (
        subprocess.CalledProcessError,
        trust.TrustReadError,
        TrustedAuthorsNotConfiguredError,
        ValueError,
        github.NoInstallationError,
        github.AppAuthError,
    ) as exc:
        return (
            "vrg-gh could not check whether the issue is an operational task with a "
            f"recorded 'Outcome: SUCCESS', so it does not close it: {exc}"
        )


def _close_target(parsed: _ParsedArgs) -> epics.IssueRef:
    """The issue an ``issue close`` names: a URL, or a number in ``--repo`` or the cwd repo."""
    if len(parsed.positionals) != 1:
        msg = "issue close takes exactly one issue number or URL"
        raise ValueError(msg)
    target = parsed.positionals[0]
    url = _ISSUE_URL_RE.fullmatch(target)
    if url is not None:
        return epics.IssueRef(owner=url[1], repo=url[2], number=int(url[3]))
    number = _ISSUE_NUMBER_RE.fullmatch(target)
    if number is None:
        msg = "the issue close target is not an issue number or URL"
        raise ValueError(msg)
    if parsed.repo_values:
        slug = _parse_repo_slug(parsed.repo_values[-1])
        if slug is None:
            msg = "the issue close --repo value is not OWNER/REPO"
            raise ValueError(msg)
    else:
        slug = github.current_repo()
    owner, repo = slug.split("/", 1)
    return epics.IssueRef(owner=owner, repo=repo, number=int(number[1]))


@dataclass
class _ParsedArgs:
    """The parts of a wrapped argv that can name a target owner, and every flag's value."""

    repo_values: list[str] = field(default_factory=list)
    owner_values: list[str] = field(default_factory=list)
    positionals: list[str] = field(default_factory=list)
    # Every value-taking flag's value, as (flag, value), in argv order.
    flag_values: list[tuple[str, str]] = field(default_factory=list)


def _parse_args(
    args: list[str],
    value_flags: frozenset[str],
    owner_flags: frozenset[str] = frozenset({"--owner"}),
) -> _ParsedArgs:
    """Split *args* into ``-R``/``--repo`` values, owner values and positionals.

    *value_flags* names the flags (long ``--x`` and short ``-x``) that take a
    value, so a flag's value is never mistaken for a positional. *owner_flags*
    names the flags whose value is an owner (``--owner`` by default; a
    command's ``--org``/``-o`` where it names one). ``-R``/``--repo`` and the
    owner flags always take a value. A flag not in the table is
    treated as boolean: that errs toward reading a token as a positional, so
    an owner it names is found rather than skipped. Handles separate-value
    (``-R x``), equals (``--repo=x``, ``-R=x``), glued-shorthand (``-Rx``) and
    clustered-shorthand (``-wR x``) forms, and ``--`` ending the flags.
    """
    value_flags = value_flags | {"-R", "--repo"} | owner_flags
    parsed = _ParsedArgs()

    def record(flag: str, value: str) -> None:
        parsed.flag_values.append((flag, value))
        if flag in ("-R", "--repo"):
            parsed.repo_values.append(value)
        elif flag in owner_flags:
            parsed.owner_values.append(value)

    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if arg == "--":
            parsed.positionals.extend(args[i:])
            break
        if arg.startswith("--"):
            name, sep, value = arg.partition("=")
            if sep:
                record(name, value)
            elif name in value_flags and i < len(args):
                record(name, args[i])
                i += 1
            continue
        if arg.startswith("-") and len(arg) > 1:
            for pos, letter in enumerate(arg[1:], start=2):
                short = f"-{letter}"
                if short not in value_flags:
                    continue
                rest = arg[pos:].removeprefix("=")
                if rest:
                    record(short, rest)
                elif i < len(args):
                    record(short, args[i])
                    i += 1
                break
            continue
        parsed.positionals.append(arg)
    return parsed


def _flags(*names: str) -> frozenset[str]:
    return frozenset(names)


# The value-taking flags of every command whose positionals can name an owner,
# as `gh <command> --help` lists them (gh 2.8x). `-R`/`--repo` and `--owner`
# are added for every command by _parse_args.
_OUTPUT_FLAGS = ("--jq", "-q", "--json", "--template", "-t")
_SEARCH_COMMON = (*_OUTPUT_FLAGS, "--limit", "-L", "--match", "--order", "--sort", "--visibility")
_VALUE_FLAGS: dict[tuple[str, str], frozenset[str]] = {
    ("issue", "view"): _flags(*_OUTPUT_FLAGS),
    ("issue", "close"): _flags("--comment", "-c", "--duplicate-of", "--reason", "-r"),
    ("issue", "reopen"): _flags("--comment", "-c"),
    ("issue", "edit"): _flags(
        "--add-assignee",
        "--add-blocked-by",
        "--add-blocking",
        "--add-label",
        "--add-project",
        "--add-sub-issue",
        "--attach",
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--milestone",
        "-m",
        "--parent",
        "--remove-assignee",
        "--remove-blocked-by",
        "--remove-blocking",
        "--remove-label",
        "--remove-project",
        "--remove-sub-issue",
        "--title",
        "-t",
        "--type",
    ),
    ("issue", "comment"): _flags("--attach", "--body", "-b", "--body-file", "-F"),
    ("issue", "transfer"): _flags(),
    ("pr", "view"): _flags(*_OUTPUT_FLAGS),
    ("pr", "checks"): _flags(*_OUTPUT_FLAGS, "--interval", "-i"),
    ("pr", "diff"): _flags("--color", "--exclude", "-e"),
    ("pr", "comment"): _flags("--attach", "--body", "-b", "--body-file", "-F"),
    ("pr", "edit"): _flags(
        "--add-assignee",
        "--add-label",
        "--add-project",
        "--add-reviewer",
        "--base",
        "-B",
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--milestone",
        "-m",
        "--remove-assignee",
        "--remove-label",
        "--remove-project",
        "--remove-reviewer",
        "--title",
        "-t",
    ),
    ("pr", "review"): _flags("--body", "-b", "--body-file", "-F"),
    ("pr", "merge"): _flags(
        "--author-email",
        "-A",
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--match-head-commit",
        "--subject",
        "-t",
    ),
    ("pr", "update-branch"): _flags(),
    ("release", "download"): _flags(
        "--archive", "-A", "--dir", "-D", "--output", "-O", "--pattern", "-p"
    ),
    ("variable", "list"): _flags(*_OUTPUT_FLAGS, "--env", "-e"),
    ("variable", "get"): _flags(*_OUTPUT_FLAGS, "--env", "-e"),
    ("repo", "view"): _flags(*_OUTPUT_FLAGS, "--branch", "-b"),
    ("repo", "list"): _flags(
        *_OUTPUT_FLAGS, "--language", "-l", "--limit", "-L", "--topic", "--visibility"
    ),
    ("search", "code"): _flags(
        *_OUTPUT_FLAGS,
        "--extension",
        "--filename",
        "--language",
        "--limit",
        "-L",
        "--match",
        "--size",
    ),
    ("search", "commits"): _flags(
        *_SEARCH_COMMON,
        "--author",
        "--author-date",
        "--author-email",
        "--author-name",
        "--committer",
        "--committer-date",
        "--committer-email",
        "--committer-name",
        "--hash",
        "--parent",
        "--tree",
    ),
    ("search", "issues"): _flags(
        *_SEARCH_COMMON,
        "--app",
        "--assignee",
        "--author",
        "--closed",
        "--commenter",
        "--comments",
        "--created",
        "--interactions",
        "--involves",
        "--label",
        "--language",
        "--mentions",
        "--milestone",
        "--project",
        "--reactions",
        "--search-type",
        "--state",
        "--team-mentions",
        "--updated",
    ),
    ("search", "prs"): _flags(
        *_SEARCH_COMMON,
        "--app",
        "--assignee",
        "--author",
        "--base",
        "-B",
        "--checks",
        "--closed",
        "--commenter",
        "--comments",
        "--created",
        "--head",
        "-H",
        "--interactions",
        "--involves",
        "--label",
        "--language",
        "--mentions",
        "--merged-at",
        "--milestone",
        "--project",
        "--reactions",
        "--review",
        "--review-requested",
        "--reviewed-by",
        "--state",
        "--team-mentions",
        "--updated",
    ),
    ("search", "repos"): _flags(
        *_SEARCH_COMMON,
        "--created",
        "--followers",
        "--forks",
        "--good-first-issues",
        "--help-wanted-issues",
        "--include-forks",
        "--language",
        "--license",
        "--number-topics",
        "--size",
        "--stars",
        "--topic",
        "--updated",
    ),
}
# The flags whose value is an owner, per command, where they are not just
# `--owner`. `gh variable list|get --org/-o <org>` reads an organization's
# variables, so the token must come from that org's installation (#5001).
_OWNER_FLAGS: dict[str, frozenset[str]] = {
    "variable": _flags("--org", "-o"),
}
_API_VALUE_FLAGS = _flags(
    "--method",
    "-X",
    "--header",
    "-H",
    "--field",
    "-F",
    "--raw-field",
    "-f",
    "--input",
    "--jq",
    "-q",
    "--template",
    "-t",
    "--cache",
    "--preview",
    "-p",
    "--hostname",
)

# An issue or PR URL: http(s)://<host>/<owner>/<repo>[/...].
_URL_OWNER_RE = re.compile(r"https?://[^/\s]+/([^/\s]+)/[^/\s]+(?:/.*)?")
# A search qualifier naming an owner: repo:<owner>/<repo>, org:<owner>, user:<owner>.
_QUALIFIER_RE = re.compile(r"(repo|org|user):\"?([^\s\"]+)\"?")
# An API endpoint scoped to one owner: [/]repos/<owner>/..., [/]orgs/<owner>, [/]users/<owner>.
_API_OWNER_RE = re.compile(r"/?(?:repos|orgs|users)/([^/?\s]+)")
# A full-URL API endpoint's scheme and host, and a GHES /api/v3 prefix: gh
# uses an endpoint holding "://" as-is, so its path is what names the owner.
_API_URL_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/]*(?:/api/v3)?")


def _api_endpoint(argv: list[str]) -> str:
    """The path of a ``gh api`` call's endpoint, a full URL reduced to its path."""
    parsed = _parse_args(argv[1:], _API_VALUE_FLAGS)
    endpoint = parsed.positionals[0] if parsed.positionals else ""
    prefix = _API_URL_PREFIX_RE.match(endpoint)
    return endpoint[prefix.end() :] if prefix else endpoint


def _is_graphql(argv: list[str]) -> bool:
    """True for a ``gh api graphql`` call, by name or by URL."""
    return argv[0] == "api" and _api_endpoint(argv).strip("/") in ("graphql", "api/graphql")


def _owner_if_valid(owner: str | None) -> str | None:
    """*owner* when it is a GitHub owner name; a ``{owner}`` placeholder or junk is ``None``."""
    if owner is not None and _OWNER_RE.fullmatch(owner):
        return owner
    return None


def _positional_owners(top: str, sub: str, positionals: list[str]) -> list[str | None]:
    """The owners the command's positional arguments name, per gh's grammar for it."""
    if top == "repo" and sub == "view":
        return [_parse_repo_owner(positionals[0])] if positionals else []
    if top == "repo" and sub == "list":
        return [_owner_if_valid(positionals[0])] if positionals else []
    if top in ("issue", "pr"):
        # A number or branch names no owner; a branch may contain '/', so only
        # a full URL is read, never a bare OWNER/REPO.
        owners: list[str | None] = []
        for value in positionals:
            match = _URL_OWNER_RE.fullmatch(value)
            owners.append(match.group(1) if match else None)
        if top == "issue" and sub == "transfer" and len(positionals) >= 2:  # noqa: PLR2004
            owners[1] = _parse_repo_owner(positionals[1])
        return owners
    if top == "search":
        owners = []
        for word in " ".join(positionals).split():
            # Grouping parentheses may hug a qualifier: (org:a OR org:b).
            match = _QUALIFIER_RE.fullmatch(word.strip("()"))
            if match:
                kind, value = match.groups()
                owners.append(_parse_repo_owner(value) if kind == "repo" else value)
        return owners
    return []


def _target_owners(argv: list[str]) -> list[str]:
    """Every distinct owner the wrapped *argv* names, in the order first named.

    Owners are compared case-insensitively, as GitHub does; the first spelling
    is kept. Sources: ``-R``/``--repo``, ``--owner``, and the command's
    positional target (a ``repo view``/``repo list`` owner, an issue or PR
    URL, an ``issue transfer`` destination, a ``repo:``/``org:``/``user:``
    search qualifier, or an ``api`` endpoint's ``repos|orgs|users/<owner>``,
    given as a path or a full URL).
    """
    if argv[0] == "api":
        parsed = _parse_args(argv[1:], _API_VALUE_FLAGS)
        match = _API_OWNER_RE.match(_api_endpoint(argv))
        candidates: list[str | None] = [match.group(1) if match else None]
    else:
        top, sub = argv[0], argv[1]
        parsed = _parse_args(
            argv[2:],
            _VALUE_FLAGS.get((top, sub), _flags()),
            _OWNER_FLAGS.get(top, _flags("--owner")),
        )
        candidates = _positional_owners(top, sub, parsed.positionals)
        if top != "search" and parsed.repo_values:
            # Outside search, -R/--repo takes one value and the last one wins.
            parsed.repo_values = parsed.repo_values[-1:]
    candidates += [_parse_repo_owner(value) for value in parsed.repo_values]
    candidates += parsed.owner_values

    owners: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        owner = _owner_if_valid(candidate)
        if owner is not None and owner.lower() not in seen:
            seen.add(owner.lower())
            owners.append(owner)
    return owners


def _exec_gh(argv: list[str]) -> int:
    """Inject the installation token and execute ``gh`` with retry.

    The App installation is selected by the owner the command targets, in
    whatever form it names it (see :func:`_target_owners`), and falls back to
    cwd remote detection only when the command names no owner. A targeted
    owner with no installation fails loudly: a token minted for a different
    installation cannot write to that owner's repos or use the permissions
    granted there (#1413, #4762). In App mode, a command that names more than
    one owner is refused, since no single installation token acts for them all,
    and so is ``api graphql``, which names its owner inside the query.
    """
    if _is_graphql(argv) and github.is_app_mode():
        print(
            "vrg-gh: api graphql is refused in GitHub App mode: a GraphQL call names "
            "its target owner inside the query, where vrg-gh cannot read it, so the "
            "token would silently come from the working directory's org. Use a REST "
            "endpoint (repos/<owner>/..., orgs/<owner>/...) or a gh subcommand instead.",
            file=sys.stderr,
        )
        return 1
    owners = _target_owners(argv)
    if len(owners) > 1 and github.is_app_mode():
        print(
            f"vrg-gh: this command targets more than one owner ({', '.join(owners)}); "
            "one GitHub App installation token cannot act for all of them, so "
            "writes to the others would fail. Run it once per owner.",
            file=sys.stderr,
        )
        return 1
    return _run_gh(argv, owners[0] if owners else None)


def _parse_job_log_args(args: list[str]) -> tuple[str, str, str] | str:
    """Parse and validate ``run job-log``'s arguments.

    Returns ``(owner, repo, job_id)``, or an error message. Only
    ``--repo``/``-R`` and ``--job`` are accepted, each exactly once, in
    separate-value or ``=`` form. Anything else is refused rather than
    forwarded: no argument is passed through to gh.
    """
    names = {"--repo": "repo", "-R": "repo", "--job": "job"}
    values: dict[str, str] = {}
    i = 0
    while i < len(args):
        arg = args[i]
        flag, sep, value = arg.partition("=")
        if flag not in names:
            return f"unexpected argument {arg!r}."
        if not sep:
            if i + 1 >= len(args):
                return f"{flag} requires a value."
            value = args[i + 1]
            i += 1
        key = names[flag]
        if key in values:
            return f"--{key} was given more than once."
        values[key] = value
        i += 1
    missing = [f"--{key}" for key in ("repo", "job") if key not in values]
    if missing:
        return f"missing {' and '.join(missing)}."
    owner, sep, repo = values["repo"].partition("/")
    if not (
        sep and _OWNER_RE.fullmatch(owner) and _REPO_RE.fullmatch(repo) and repo not in (".", "..")
    ):
        return f"--repo must be <owner>/<repo>, got {values['repo']!r}."
    if not _JOB_ID_RE.fullmatch(values["job"]):
        return f"--job must be a numeric job ID, got {values['job']!r}."
    return owner, repo, values["job"]


def _job_log(args: list[str]) -> int:
    """Print one Actions job's log from the read-only per-job log endpoint."""
    if any(arg in ("-h", "--help") for arg in args):
        print(
            f"{_JOB_LOG_USAGE}\n"
            "\n"
            "Print the log of one GitHub Actions job, by its numeric job ID, from\n"
            "the read-only per-job log endpoint. Unlike 'run view --job <id> --log',\n"
            "it does not download the whole run's log archive. Terminal escape\n"
            "sequences and other control characters are stripped. Available to every\n"
            "identity."
        )
        return 0
    parsed = _parse_job_log_args(args)
    if isinstance(parsed, str):
        print(f"vrg-gh: run job-log: {parsed}\n{_JOB_LOG_USAGE}", file=sys.stderr)
        return 2
    owner, repo, job_id = parsed
    endpoint = f"repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
    # Job logs routinely carry ANSI color codes, which gh refuses to print
    # unless told to. The wrapper accepts them from gh and strips every
    # terminal control sequence itself, so none reaches the terminal.
    return _run_gh(
        ["api", "--method", "GET", "--allow-escape-sequences", endpoint],
        owner,
        clean=_strip_terminal_controls,
    )


def _strip_terminal_controls(text: str) -> str:
    """Remove terminal escape sequences and other control characters from *text*.

    Keeps tab and newline; a carriage return is dropped, so CRLF becomes LF.
    """
    return _CONTROL_CHAR_RE.sub("", _ESCAPE_SEQUENCE_RE.sub("", text))


def _unchanged(text: str) -> str:
    return text


def _run_gh(argv: list[str], owner: str | None, *, clean: Callable[[str], str] = _unchanged) -> int:
    """Execute ``gh`` with *owner*'s installation token (cwd detection when None).

    *clean* is applied to gh's output before it is replayed. gh must be at
    least :data:`gh_version.MIN_GH_VERSION`, checked before any token is
    minted: an older gh is refused here with the version it needs, never
    left to fail with its own unknown-flag error (#4613).
    """
    try:
        gh_version.require_minimum()
    except gh_version.GhVersionError as exc:
        print(f"vrg-gh: {exc}", file=sys.stderr)
        return 1
    try:
        token = github.get_installation_token(org=owner, require_installation=owner is not None)
    except github.NoInstallationError as exc:
        known = ", ".join(exc.known) if exc.known else "none"
        print(
            f"vrg-gh: the GitHub App has no installation for owner "
            f"'{exc.org}' (the command's target). Known installations: {known}.",
            file=sys.stderr,
        )
        return 1
    except github.AppAuthError as exc:
        print(f"vrg-gh: {github.app_auth_message(exc)}", file=sys.stderr)
        return 1
    env: dict[str, str] | None = None
    if token is not None:
        env = {**os.environ, "GH_TOKEN": token}
    try:
        result = retry.run_with_retry(
            ["gh", *argv],  # noqa: S607
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stdout.write(clean(exc.stdout))
        if exc.stderr:
            sys.stderr.write(clean(exc.stderr))
        return exc.returncode
    if result.stdout:
        sys.stdout.write(clean(result.stdout))
    if result.stderr:
        sys.stderr.write(clean(result.stderr))
    return 0


def _print_help() -> None:
    """Print vrg-gh's own help — what the wrapper enforces before forwarding."""
    allowed = ", ".join(
        f"{top} ({'/'.join(sorted(subs))})" for top, subs in sorted(_ALLOWED.items())
    )
    print(
        "usage: vrg-gh <subcommand> <action> [args...]\n"
        "\n"
        "A safe, identity-aware gh wrapper for agent sessions: it forwards to gh\n"
        "only after enforcing policy, injecting the GitHub App installation token\n"
        "for the target owner.\n"
        "\n"
        "What it enforces:\n"
        f"  - Allowed (human/user): {allowed}.\n"
        "    The audit identity is narrower (pr read/review actions, workflow\n"
        "    list/view, cache list); agents are barred from pr create/edit/merge.\n"
        "  - Denied outright: gh auth, issue create, pr close, repo\n"
        "    edit/create/delete, workflow run.\n"
        "  - gh api is gated by identity: human full, audit read-only GET, user\n"
        "    denied.\n"
        "  - An agent's issue close of an operational task as completed is\n"
        "    refused until a trusted comment on it records 'Outcome: SUCCESS'.\n"
        "  - run job-log --repo <owner>/<repo> --job <id> prints one Actions\n"
        "    job's log from the read-only per-job log endpoint, for every identity.\n"
        "\n"
        "Identity mode is resolved via vrg-whoami. Per-subcommand gh help still\n"
        "forwards, e.g. 'vrg-gh pr view --help'."
    )


if __name__ == "__main__":
    sys.exit(main())
