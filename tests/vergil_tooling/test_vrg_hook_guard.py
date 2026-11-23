"""Tests for vergil_tooling.bin.vrg_hook_guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_hook_guard import _find_vergil_toml, main


def _make_hook_input(command: str, cwd: str = "/repo") -> str:
    return json.dumps(
        {
            "session_id": "test",
            "cwd": cwd,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )


def _run(
    command: str, *, cwd: str = "/repo", managed: bool = True, agent: bool = False
) -> tuple[int, str]:
    hook_input = _make_hook_input(command, cwd=cwd)
    toml_path = Path(cwd) / "vergil.toml"
    with (
        patch("sys.stdin", StringIO(hook_input)),
        patch("vergil_tooling.bin.vrg_hook_guard.Path.exists", return_value=managed),
        patch(
            "vergil_tooling.bin.vrg_hook_guard._find_vergil_toml",
            return_value=toml_path if managed else None,
        ),
        patch(
            "vergil_tooling.bin.vrg_hook_guard.identity_mode.any_agent_signal",
            return_value=agent,
        ),
    ):
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = main()
    return rc, buf.getvalue()


# -- managed-repo gating ------------------------------------------------------


class TestManagedRepoGating:
    def test_unmanaged_repo_allows_everything(self) -> None:
        rc, out = _run("git commit -m 'test'", managed=False)
        assert rc == 0
        assert out == ""

    def test_managed_repo_blocks_raw_git(self) -> None:
        rc, out = _run("git commit -m 'test'")
        assert rc == 0
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- raw git detection --------------------------------------------------------


class TestRawGitDetection:
    def test_direct_git_commit(self) -> None:
        rc, out = _run("git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "vrg-git" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_direct_git_push(self) -> None:
        rc, out = _run("git push origin main")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_direct_git_reset(self) -> None:
        rc, out = _run("git reset --hard HEAD")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_direct_git_status(self) -> None:
        rc, out = _run("git status")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_chained_git_commit(self) -> None:
        rc, out = _run("cd /path && git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sequenced_git_commit(self) -> None:
        rc, out = _run("git add . ; git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_piped_git_commit(self) -> None:
        rc, out = _run("echo y | git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_or_chained_git(self) -> None:
        rc, out = _run("false || git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_c_subshell(self) -> None:
        rc, out = _run('bash -c "git commit -m test"')
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sh_c_subshell(self) -> None:
        rc, out = _run('sh -c "git commit -m test"')
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_parenthesized(self) -> None:
        rc, out = _run("(git reset --hard)")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_dollar_paren(self) -> None:
        rc, out = _run("echo $(git log --oneline)")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- raw gh detection ---------------------------------------------------------


class TestRawGhDetection:
    def test_direct_gh_pr_create(self) -> None:
        rc, out = _run("gh pr create --title 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "vrg-gh" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_direct_gh_api(self) -> None:
        rc, out = _run("gh api repos/foo/bar")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_chained_gh(self) -> None:
        rc, out = _run("cd /repo && gh pr create")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_dollar_paren_gh(self) -> None:
        rc, out = _run("echo $(gh api repos/foo/bar)")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- vrg-git and vrg-gh must NOT match ----------------------------------------


class TestWrapperExclusion:
    def test_vrg_git_allowed(self) -> None:
        rc, out = _run("vrg-git commit -m 'test'")
        assert rc == 0
        assert out == ""

    def test_vrg_gh_allowed(self) -> None:
        rc, out = _run("vrg-gh pr create --title 'test'")
        assert rc == 0
        assert out == ""

    def test_vrg_git_status_allowed(self) -> None:
        rc, out = _run("vrg-git status")
        assert rc == 0
        assert out == ""

    def test_vrg_git_with_path_allowed(self) -> None:
        rc, out = _run("/usr/local/bin/vrg-git log")
        assert rc == 0
        assert out == ""

    def test_chained_vrg_git_allowed(self) -> None:
        rc, out = _run("cd /path && vrg-git push origin main")
        assert rc == 0
        assert out == ""


# -- quoted argument text must not trigger false positives --------------------


class TestQuotedArgumentExclusion:
    def test_vrg_submit_pr_with_git_in_summary(self) -> None:
        rc, out = _run('vrg-submit-pr --summary "Add git rm to allowlist"')
        assert rc == 0
        assert out == ""

    def test_vrg_submit_pr_with_gh_in_notes(self) -> None:
        rc, out = _run('vrg-submit-pr --notes "Use gh api for checks"')
        assert rc == 0
        assert out == ""

    def test_vrg_commit_with_git_in_message(self) -> None:
        rc, out = _run('vrg-commit --message "fix git hook detection"')
        assert rc == 0
        assert out == ""

    def test_echo_with_git_in_quotes(self) -> None:
        rc, out = _run('echo "git push origin main"')
        assert rc == 0
        assert out == ""

    def test_single_quoted_git_arg(self) -> None:
        rc, out = _run("vrg-submit-pr --title 'fix git allowlist'")
        assert rc == 0
        assert out == ""

    def test_bash_c_git_still_blocked(self) -> None:
        rc, out = _run('bash -c "git push origin main"')
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sh_c_gh_still_blocked(self) -> None:
        rc, out = _run("sh -c 'gh pr create --title test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_c_vrg_git_allowed(self) -> None:
        rc, out = _run('bash -c "vrg-git push origin main"')
        assert rc == 0
        assert out == ""


# -- canonical case table (spec section 6.1) -----------------------------------
#
# Encoded verbatim from the command-matcher quoting design spec,
# section 6.1 (vergil-claude-plugin,
# docs/specs/2026-06-05-450-command-matcher-quoting-design.md). The
# rule is implemented twice (jq/Oniguruma in the plugin scripts,
# Python re here); drift between the implementations requires editing
# the spec table — a visible act. Verdicts are for the raw-git
# predicate. Rows 11-12 apply only to vrg-hook-guard.

_SPEC_TABLE = [
    # columns: row number, command, denies, exercises
    (1, "git commit -m x", True, "plain invocation"),
    (2, "cd foo && git commit", True, "separator"),
    (3, "x=$(git commit -m x)", True, "( separator"),
    (4, "{ git commit -m x; }", True, "{ separator"),
    (
        5,
        'vrg-commit --body "line one\ngit commit prose"',
        False,
        "#450 repro",
    ),
    (6, "find . -path ./.git -prune", False, "position anchor"),
    (7, 'echo "git commit"', False, "quoted span stripped"),
    (8, "echo 'say \"git commit\"'", False, "nesting"),
    (9, 'echo "he said \\"git commit\\""', False, "escape handling"),
    (
        10,
        'echo "; git commit',
        True,
        "unbalanced quote with a separator inside the span (spec 2.1)",
    ),
    (11, 'bash -c "git commit"', True, "recheck path"),
    (
        12,
        "bash -c 'true' && vrg-commit --body \"git commit prose\"",
        False,
        "recheck confinement (spec 4.3)",
    ),
    (13, "if git commit; then :; fi", False, "keyword position (spec 2.4)"),
    (
        14,
        'echo "git commit',
        False,
        "unbalanced quote; the unstripped quote is not a separator",
    ),
]


class TestSpecCanonicalTable:
    @pytest.mark.parametrize(
        ("command", "denies"),
        [(command, denies) for _, command, denies, _ in _SPEC_TABLE],
        ids=[f"row-{row}" for row, _, _, _ in _SPEC_TABLE],
    )
    def test_spec_vector(self, command: str, denies: bool) -> None:
        rc, out = _run(command)
        assert rc == 0
        if denies:
            result = json.loads(out)
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        else:
            assert out == ""


# -- accepted false-negative gaps (spec section 2.4) ---------------------------
#
# The canonical anchor (spec section 2.2) matches a tool name only at
# line start or after a command separator. These shapes place the tool
# name elsewhere and are accepted false negatives: the hook guards
# against agent slip-ups, and the vrg-git/vrg-gh wrappers remain the
# real enforcement layer.


class TestAcceptedGaps:
    def test_env_var_prefix(self) -> None:
        rc, out = _run("VRG_COMMIT_CONTEXT=1 git commit -m 'test'")
        assert rc == 0
        assert out == ""

    def test_multiple_env_vars(self) -> None:
        rc, out = _run("FOO=bar BAZ=1 git commit -m 'test'")
        assert rc == 0
        assert out == ""

    def test_env_command_wrapper(self) -> None:
        rc, out = _run("env git commit -m 'test'")
        assert rc == 0
        assert out == ""

    def test_command_wrapper(self) -> None:
        rc, out = _run("command git commit -m 'test'")
        assert rc == 0
        assert out == ""

    def test_backtick_substitution(self) -> None:
        rc, out = _run("echo `git log --oneline`")
        assert rc == 0
        assert out == ""


# -- non-git/gh commands must be allowed --------------------------------------


class TestNonGitCommands:
    def test_ls(self) -> None:
        rc, out = _run("ls -la")
        assert rc == 0
        assert out == ""

    def test_python(self) -> None:
        rc, out = _run("python script.py")
        assert rc == 0
        assert out == ""

    def test_grep_for_git(self) -> None:
        rc, out = _run("grep -r 'git' src/")
        assert rc == 0
        assert out == ""

    def test_empty_command(self) -> None:
        rc, out = _run("")
        assert rc == 0
        assert out == ""


# -- raw content reads by agents (epic vergil-project/.github#1467, §6.4) ------


def _reason(out: str) -> str:
    result = json.loads(out)["hookSpecificOutput"]
    assert result["permissionDecision"] == "deny"
    return str(result["permissionDecisionReason"])


_REFUSED_CONTENT_READS = [
    # single-item body, comment and review reads
    "vrg-gh issue view 5 --comments",
    "vrg-gh pr view 7 --repo o/r --comments",
    "vrg-gh issue view 5 --comments=true",
    "vrg-gh issue view 5 --json body",
    "vrg-gh issue view 5 --json title,body --jq .body",
    "vrg-gh issue view 5 --json=comments",
    'vrg-gh issue view 5 --json "labels,comments"',
    "vrg-gh pr view 7 --json reviews,comments",
    "vrg-gh pr view 7 --json latestReviews",
    "vrg-gh pr view 7 --json number,closingIssuesReferences",
    "vrg-gh issue view 5",
    "vrg-gh issue view 5 --repo o/r",
    "vrg-gh pr view 7",
    "vrg-gh api repos/o/r/issues/5/comments",
    "vrg-gh api /repos/o/r/pulls/7/reviews",
    "vrg-gh api repos/o/r/issues/5/comments?per_page=100 --paginate",
    "vrg-gh api --paginate -H 'Accept: x' repos/o/r/issues/comments/123",
    # searches and lists whose results a skill would act on (and every
    # refused row of _QUERY_TABLE, in both forms)
    'vrg-gh issue list --repo o/r --state open --search "Fixture run in:title" --json number,title',
    "vrg-gh issue ls --repo o/r -S release --json number",
    "vrg-gh pr list --repo o/r --search=token --json number",
    "vrg-gh issue list --repo o/r --comments",
    'vrg-gh search issues "\\"Awaiting a ruling\\"" --owner org --state open --json number,title',
    "vrg-gh search issues Fixture run --owner org",
    'vrg-gh search issues "label:epic in:title release" --owner org --json number',
    'vrg-gh search issues "label:epic" --match title --owner org --json number',
    "vrg-gh search prs --owner org --json number,body",
    'vrg-gh search prs -- "some text"',
    # the guard's command-position rule, on shell words
    "cd /x && vrg-gh issue view 5 --json body",
    "x=$(vrg-gh issue view 5 --json body)",
    "{ vrg-gh issue view 5; }",
    "vrg-gh issue view 5 \\\n  --comments",
    "vrg-gh issue view 5 --json bo\\dy",
    'bash -c "vrg-gh issue view 5 --comments"',
    "sh -c 'vrg-gh pr list --repo o/r --json body'",
]

# Metadata reads, including every raw `vrg-gh` read that the
# vergil-claude-plugin v2.1.86 skills still make (placeholders filled in).
_ALLOWED_READS = [
    "vrg-gh issue view 5 --repo o/r --json labels",
    "vrg-gh issue view 5 --repo o/r --json parent",
    "vrg-gh issue view 5 --repo o/r --json labels,title",
    "vrg-gh issue view 5 --repo o/r --json labels,state",
    "vrg-gh issue view 5 --repo o/r --json state,stateReason",
    "vrg-gh issue view 5 --repo o/r --json state,closedAt",
    "vrg-gh issue view 5 --repo o/r --json createdAt,closedAt",
    "vrg-gh issue view 5 --repo o/r --json number --jq .number",
    "vrg-gh pr view 7 --json state,mergedAt",
    "vrg-gh pr view https://github.com/o/r/pull/7 --json state,title,baseRefName,"
    "headRefName,headRefOid,autoMergeRequest",
    "vrg-gh pr view 7 --repo o/r \\\n"
    "  --json mergeable,mergeStateStatus,statusCheckRollup,autoMergeRequest",
    'vrg-gh search issues --owner org --label epic --closed "2026-10-01..2026-10-31" \\\n'
    "  --limit 1000 --json repository,number,title,closedAt,url",
    'vrg-gh search prs --owner org --merged-at "2026-10-01..2026-10-31" --limit 1000 \\\n'
    "  --json repository,number,author,closedAt",
    'vrg-gh search issues "reason:completed" --owner org \\\n'
    '  --closed "2026-10-01..2026-10-07" --limit 1000 --json repository,number,labels',
    'vrg-gh search prs --owner org --merged-at "2026-10-01..2026-10-07" \\\n'
    "  --limit 1000 --json repository,number,author",
    "vrg-gh search prs --owner org \\\n"
    '  --merged-at "2026-11-22T09:00:00-05:00..2026-11-23T09:00:00-05:00" \\\n'
    "  --limit 1000 --json repository,number,title,author,closedAt,url \\\n"
    "  --jq '.[] | select(.author.type == \"Bot\")'",
    "vrg-gh search prs --owner org --state open --limit 1000 \\\n"
    "  --json repository,number,title,author,createdAt,updatedAt,url \\\n"
    "  --jq '.[] | select(.author.type == \"Bot\")'",
    'vrg-gh search issues "(label:epic OR label:task) -label:archive is:open" \\\n'
    "  --owner org --json number",
    "vrg-gh search issues --owner org --comments '>5' --json number",
    "vrg-gh issue list --repo o/r",
    "vrg-gh issue ls --repo o/r --json number",
    "vrg-gh pr list --repo o/r --state open --json number,title,url",
    "vrg-gh issue list --repo o/r --label epic --search 'is:open -label:archive' --json number",
    "vrg-gh repo list org --limit 1000 --no-archived --json nameWithOwner",
    "vrg-gh repo view vergil-project/python-lib --json name",
    "vrg-gh release list --repo o/r --limit 3",
    "vrg-gh release view v1.2.3 --repo o/r",
    "vrg-gh release list --repo o/r --limit 1000 \\\n"
    "  --json tagName,name,publishedAt,isDraft,isPrerelease \\\n"
    "  --jq '.[] | select(.isDraft | not)'",
    "vrg-gh run list --repo o/r --limit 10",
    "vrg-gh run list --repo o/r --workflow ops.yml \\\n"
    '  --created "2026-11-22..2026-11-23" --limit 20 \\\n'
    "  --json databaseId,conclusion,createdAt,url",
    "vrg-gh run view 123 --repo o/r --json jobs \\\n"
    "  --jq '.jobs[] | {name, conclusion, steps: [.steps[] |\n"
    '  select(.conclusion == "failure") | .name]}\'',
    # degenerate forms
    "vrg-gh",
    "vrg-gh issue",
    "vrg-gh api --paginate",
    "{ echo hi; }",
    "vrg-gh run view 123 --repo o/r --log-failed",
    "vrg-gh run download 123 --repo org/.github --name trusted-authors-values --dir /tmp/x",
    "vrg-gh run rerun 123 --failed",
    "vrg-gh issue comment 5 --repo o/r --body-file /tmp/c.md",
    "vrg-gh issue edit 5 --repo o/r --body-file /tmp/b.md",
    'vrg-gh issue close 5 --repo o/r --reason completed --comment "done"',
    'vrg-gh issue create --repo o/r --title "x" --body-file /tmp/b.md',
    "vrg-gh pr checks 7",
    "vrg-gh pr diff 7",
    "vrg-gh issue view --help",
    "vrg-gh api graphql -f query=@/tmp/q.graphql",
    "vrg-issue-read 'o/r#5' --pr --json",
    "vrg-issue-read --repo o/r --search 'Fixture run in:title' --json",
    # quoted text never counts as a command
    'vrg-commit --message "stop using vrg-gh issue view 5 --comments"',
    "echo 'vrg-gh issue view 5'",
    "grep -n 'vrg-gh pr list' skills/x.md",
    'vrg-pr-workflow report-ready --notes "; vrg-gh issue view 5 --json body"',
    # an unbalanced quote is a shell syntax error that runs nothing
    'echo "vrg-gh issue view 5',
]


# One rule for lists and searches: (qualifier flags, query, --json fields,
# refused). Each row is rendered as an issue and a PR list (the query in
# --search) and as an issue and a PR search (the query positional).
_QUERY_TABLE = [
    ("--label epic", "", "number,title", False),
    ("--state open", "", "number,title,url", False),
    ("", "label:epic", "number", False),
    ("", "is:open -label:archive", "number,labels", False),
    ("", "(label:epic OR label:task) is:open", "number", False),
    ("", "reason:completed closed:2026-10-01..2026-10-31", "number", False),
    ("--author someone", "created:>2026-01-01", "number,author,createdAt", False),
    ("--label epic", "", "number,body", True),
    ("", "label:epic", "number,comments", True),
    ("--state open", "", "reviews", True),
    ("", "is:open", "latestReviews", True),
    ("", "", "number,closingIssuesReferences", True),
    ("", "Fixture run", "number", True),
    ("--label epic", "release", "number", True),
    ("", "label:epic in:title", "number", True),
    ("", "in:body token", "number", True),
    ("", '"Awaiting a ruling" is:open', "number", True),
]


def _query_forms(flags: str, query: str, fields: str) -> list[str]:
    flag_part = f" {flags}" if flags else ""
    forms = []
    for top in ("issue", "pr"):
        search = f" --search '{query}'" if query else ""
        forms.append(f"vrg-gh {top} list --repo o/r{flag_part}{search} --json {fields}")
    for kind in ("issues", "prs"):
        positional = f" '{query}'" if query else ""
        forms.append(f"vrg-gh search {kind}{positional} --repo o/r{flag_part} --json {fields}")
    return forms


_QUERY_CASES = [
    pytest.param(command, refused, id=command)
    for flags, query, fields, refused in _QUERY_TABLE
    for command in _query_forms(flags, query, fields)
]


class TestListsAndSearchesOneRule:
    @pytest.mark.parametrize(("command", "refused"), _QUERY_CASES)
    def test_agent_list_and_search(self, command: str, refused: bool) -> None:
        rc, out = _run(command, agent=True)
        assert rc == 0
        if refused:
            assert "vrg-issue-read" in _reason(out)
        else:
            assert out == ""

    @pytest.mark.parametrize(("command", "refused"), _QUERY_CASES)
    def test_human_list_and_search_unchanged(self, command: str, refused: bool) -> None:
        del refused
        assert _run(command, agent=False) == (0, "")


class TestAgentContentReads:
    def test_agent_issue_view_comments_refused(self) -> None:
        rc, out = _run("vrg-gh issue view 5 --repo o/r --comments", agent=True)
        assert rc == 0
        assert "`vrg-gh issue view --comments`" in _reason(out)

    def test_agent_json_body_refused(self) -> None:
        rc, out = _run("vrg-gh issue view 5 --json number,body", agent=True)
        assert rc == 0
        assert "`vrg-gh issue view --json body`" in _reason(out)

    def test_agent_api_comments_path_refused(self) -> None:
        rc, out = _run("vrg-gh api repos/o/r/issues/5/comments", agent=True)
        assert rc == 0
        assert "`vrg-gh api` on a `comments` path" in _reason(out)

    def test_agent_bare_view_refused(self) -> None:
        rc, out = _run("vrg-gh pr view 7 --repo o/r", agent=True)
        assert rc == 0
        assert "`vrg-gh pr view` without `--json`" in _reason(out)

    def test_agent_text_list_refused(self) -> None:
        rc, out = _run("vrg-gh issue list --repo o/r --search 'Fixture run'", agent=True)
        assert rc == 0
        assert "`vrg-gh issue list --search` with a query matching text" in _reason(out)

    def test_agent_qualifier_list_allowed(self) -> None:
        rc, out = _run("vrg-gh issue list --repo o/r --label epic --json number", agent=True)
        assert (rc, out) == (0, "")

    def test_agent_text_search_refused(self) -> None:
        rc, out = _run('vrg-gh search issues "Fixture run" --owner org --json number', agent=True)
        assert rc == 0
        assert "a free-text term" in _reason(out)

    def test_agent_in_qualifier_search_refused(self) -> None:
        rc, out = _run('vrg-gh search prs "in:body token" --owner org --json number', agent=True)
        assert rc == 0
        assert "an `in:` qualifier" in _reason(out)

    @pytest.mark.parametrize("command", _REFUSED_CONTENT_READS)
    def test_agent_content_read_refused(self, command: str) -> None:
        rc, out = _run(command, agent=True)
        assert rc == 0
        _reason(out)

    def test_agent_display_read_allowed(self) -> None:
        rc, out = _run("vrg-gh issue view 5 --repo o/r --json labels,parent,state", agent=True)
        assert (rc, out) == (0, "")

    @pytest.mark.parametrize("command", _ALLOWED_READS)
    def test_agent_metadata_read_allowed(self, command: str) -> None:
        rc, out = _run(command, agent=True)
        assert (rc, out) == (0, "")

    @pytest.mark.parametrize("command", _REFUSED_CONTENT_READS)
    def test_human_mode_unchanged(self, command: str) -> None:
        rc, out = _run(command, agent=False)
        assert (rc, out) == (0, "")

    @pytest.mark.parametrize("command", _REFUSED_CONTENT_READS)
    def test_refusal_names_vrg_issue_read(self, command: str) -> None:
        _, out = _run(command, agent=True)
        reason = _reason(out)
        assert "reads raw issue or PR content" in reason
        assert "vrg-issue-read OWNER/REPO#N" in reason
        assert "vrg-issue-read (--repo OWNER/REPO | --owner ORG)" in reason

    def test_raw_gh_refusal_still_comes_first(self) -> None:
        _, out = _run("gh issue view 5 --comments", agent=True)
        assert "Use vrg-gh instead" in _reason(out)

    def test_no_identity_lookup_for_other_commands(self) -> None:
        with patch(
            "vergil_tooling.bin.vrg_hook_guard.identity_mode.any_agent_signal",
            side_effect=AssertionError("looked up"),
        ):
            rc, out, err, _ = _run_raw(_make_hook_input("vrg-gh issue view 5 --json labels"))
        assert (rc, out, err) == (0, "", "")

    @pytest.mark.parametrize(
        "error", [OSError("denied"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")]
    )
    def test_unreadable_identity_signal_refuses(self, error: Exception) -> None:
        with patch(
            "vergil_tooling.bin.vrg_hook_guard.identity_mode.any_agent_signal",
            side_effect=error,
        ):
            rc, out, err, _ = _run_raw(_make_hook_input("vrg-gh issue view 5 --comments"))
        assert (rc, err) == (0, "")
        reason = _reason(out)
        assert "cannot read the identity signals" in reason
        assert "vrg-issue-read OWNER/REPO#N" in reason


# -- edge cases and error handling --------------------------------------------


def _run_raw(raw: str, *, managed: bool = True) -> tuple[int, str, str, list[Path]]:
    """Run the guard on raw stdin; return rc, stdout, stderr and scope dirs probed."""
    probed: list[Path] = []

    def fake_find(start: Path) -> Path | None:
        probed.append(start)
        return start / "vergil.toml" if managed else None

    out, err = StringIO(), StringIO()
    with (
        patch("sys.stdin", StringIO(raw)),
        patch("vergil_tooling.bin.vrg_hook_guard._find_vergil_toml", side_effect=fake_find),
        patch("sys.stdout", out),
        patch("sys.stderr", err),
    ):
        rc = main()
    return rc, out.getvalue(), err.getvalue(), probed


def _assert_fail_closed(rc: int, out: str, err: str, problem: str) -> None:
    # Exit 2 is Claude Code's blocking exit code for a PreToolUse hook: the
    # tool call is refused and stderr is fed back to the agent.
    assert rc == 2
    assert out == ""
    assert err.startswith("vrg-hook-guard: ")
    assert problem in err
    assert "fail" in err  # names the fail-closed behaviour


class TestFailClosedOnMalformedInput:
    def test_malformed_json_denied(self) -> None:
        rc, out, err, _ = _run_raw("not json")
        _assert_fail_closed(rc, out, err, "not valid JSON")

    def test_truncated_json_denied(self) -> None:
        rc, out, err, _ = _run_raw('{"tool_input": {"command": "git sta')
        _assert_fail_closed(rc, out, err, "not valid JSON")

    def test_empty_stdin_denied(self) -> None:
        rc, out, err, _ = _run_raw("")
        _assert_fail_closed(rc, out, err, "empty")

    def test_whitespace_only_stdin_denied(self) -> None:
        rc, out, err, _ = _run_raw("  \n\t ")
        _assert_fail_closed(rc, out, err, "empty")

    def test_non_object_json_denied(self) -> None:
        rc, out, err, _ = _run_raw('["git", "status"]')
        _assert_fail_closed(rc, out, err, "not a JSON object")

    def test_missing_tool_input_denied(self) -> None:
        raw = json.dumps({"cwd": "/repo", "hook_event_name": "PreToolUse", "tool_name": "Bash"})
        rc, out, err, _ = _run_raw(raw)
        _assert_fail_closed(rc, out, err, "tool_input")

    def test_non_object_tool_input_denied(self) -> None:
        raw = json.dumps({"cwd": "/repo", "tool_name": "Bash", "tool_input": "git status"})
        rc, out, err, _ = _run_raw(raw)
        _assert_fail_closed(rc, out, err, "tool_input")

    def test_missing_command_denied(self) -> None:
        raw = json.dumps({"cwd": "/repo", "tool_name": "Bash", "tool_input": {}})
        rc, out, err, _ = _run_raw(raw)
        _assert_fail_closed(rc, out, err, "tool_input.command")

    def test_null_command_denied(self) -> None:
        raw = json.dumps({"cwd": "/repo", "tool_name": "Bash", "tool_input": {"command": None}})
        rc, out, err, _ = _run_raw(raw)
        _assert_fail_closed(rc, out, err, "tool_input.command")

    def test_non_string_command_denied(self) -> None:
        raw = json.dumps({"cwd": "/repo", "tool_name": "Bash", "tool_input": {"command": ["git"]}})
        rc, out, err, _ = _run_raw(raw)
        _assert_fail_closed(rc, out, err, "tool_input.command")

    def test_unreadable_stdin_denied(self) -> None:
        class _Broken(StringIO):
            def read(self, size: int | None = -1) -> str:
                raise OSError("stdin closed")

        err = StringIO()
        with (
            patch("sys.stdin", _Broken()),
            patch(
                "vergil_tooling.bin.vrg_hook_guard._find_vergil_toml",
                return_value=Path("/repo/vergil.toml"),
            ),
            patch("sys.stdout", StringIO()) as out,
            patch("sys.stderr", err),
        ):
            rc = main()
        _assert_fail_closed(rc, out.getvalue(), err.getvalue(), "could not read")

    def test_non_utf8_stdin_denied(self) -> None:
        class _BadBytes(StringIO):
            def read(self, size: int | None = -1) -> str:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        err = StringIO()
        with (
            patch("sys.stdin", _BadBytes()),
            patch(
                "vergil_tooling.bin.vrg_hook_guard._find_vergil_toml",
                return_value=Path("/repo/vergil.toml"),
            ),
            patch("sys.stdout", StringIO()) as out,
            patch("sys.stderr", err),
        ):
            rc = main()
        _assert_fail_closed(rc, out.getvalue(), err.getvalue(), "could not read")


class TestMalformedInputScope:
    """Out-of-scope pass-through is unchanged: no vergil.toml, no denial."""

    def test_malformed_json_outside_managed_repo_allowed(self) -> None:
        rc, out, err, probed = _run_raw("not json", managed=False)
        assert (rc, out, err) == (0, "", "")
        # Unparseable input names no cwd, so scope falls back to the
        # hook process's own working directory (the session cwd).
        assert probed == [Path()]

    def test_empty_stdin_outside_managed_repo_allowed(self) -> None:
        rc, out, err, _ = _run_raw("", managed=False)
        assert (rc, out, err) == (0, "", "")

    def test_missing_command_outside_managed_repo_allowed(self) -> None:
        raw = json.dumps({"cwd": "/elsewhere", "tool_name": "Bash", "tool_input": {}})
        rc, out, err, probed = _run_raw(raw, managed=False)
        assert (rc, out, err) == (0, "", "")
        assert probed == [Path("/elsewhere")]

    def test_scope_uses_payload_cwd_when_tool_input_malformed(self) -> None:
        raw = json.dumps({"cwd": "/repo", "tool_name": "Bash", "tool_input": "x"})
        _, _, _, probed = _run_raw(raw)
        assert probed == [Path("/repo")]

    def test_scope_prefers_tool_input_cwd(self) -> None:
        raw = json.dumps(
            {"cwd": "/outer", "tool_name": "Bash", "tool_input": {"cwd": "/inner", "command": "ls"}}
        )
        rc, out, err, probed = _run_raw(raw)
        assert (rc, out, err) == (0, "", "")
        assert probed == [Path("/inner")]

    def test_non_string_cwd_falls_back_to_process_cwd(self) -> None:
        raw = json.dumps({"cwd": 7, "tool_name": "Bash", "tool_input": {"command": "ls"}})
        rc, out, err, probed = _run_raw(raw)
        assert (rc, out, err) == (0, "", "")
        assert probed == [Path()]

    def test_missing_cwd_falls_back_to_process_cwd(self) -> None:
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
        rc, out, _, probed = _run_raw(raw)
        assert rc == 0
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert probed == [Path()]


class TestWellFormedInputUnchanged:
    def test_empty_command_string_allowed(self) -> None:
        raw = json.dumps({"cwd": "/repo", "tool_name": "Bash", "tool_input": {"command": ""}})
        assert _run_raw(raw)[:3] == (0, "", "")

    def test_non_bash_tool_passes_through(self) -> None:
        # The guard governs Bash only; a well-formed payload for another tool
        # (a misconfigured matcher) carries no command and is not malformed.
        raw = json.dumps({"cwd": "/repo", "tool_name": "Read", "tool_input": {"file_path": "/x"}})
        assert _run_raw(raw)[:3] == (0, "", "")

    def test_payload_without_tool_name_still_checked(self) -> None:
        raw = json.dumps({"cwd": "/repo", "tool_input": {"command": "gh pr list"}})
        rc, out, err, _ = _run_raw(raw)
        assert rc == 0
        assert err == ""
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_well_formed_allow_has_no_stderr(self) -> None:
        raw = _make_hook_input("vrg-git status")
        assert _run_raw(raw)[:3] == (0, "", "")


# -- end to end: a real process, a real vergil.toml, no mocks -----------------

_ENTRY = "import sys; from vergil_tooling.bin.vrg_hook_guard import main; sys.exit(main())"


def _spawn(
    raw: str, cwd: Path, *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and argv
        [sys.executable, "-c", _ENTRY],
        input=raw,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestEndToEnd:
    @pytest.fixture
    def managed(self, tmp_path: Path) -> Path:
        (tmp_path / "vergil.toml").write_text("[project]\n")
        return tmp_path

    @pytest.fixture
    def unmanaged(self, tmp_path: Path) -> Path:
        path = tmp_path / "plain"
        path.mkdir()
        return path

    @pytest.mark.parametrize("raw", ["not json", "", '{"tool_name": "Bash", "tool_input": {}}'])
    def test_malformed_input_blocks_in_managed_repo(self, managed: Path, raw: str) -> None:
        proc = _spawn(raw, managed)
        assert proc.returncode == 2
        assert proc.stdout == ""
        assert proc.stderr.startswith("vrg-hook-guard: blocked: ")

    @pytest.mark.parametrize("raw", ["not json", "", '{"tool_name": "Bash", "tool_input": {}}'])
    def test_malformed_input_passes_outside_managed_repo(self, unmanaged: Path, raw: str) -> None:
        assert _find_vergil_toml(unmanaged) is None, "tmp_path sits under a vergil.toml"
        proc = _spawn(raw, unmanaged)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    def test_raw_git_denied_via_json(self, managed: Path) -> None:
        proc = _spawn(_make_hook_input("git status", cwd=str(managed)), managed)
        assert proc.returncode == 0
        assert proc.stderr == ""
        decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny"

    def test_wrapper_allowed(self, managed: Path) -> None:
        proc = _spawn(_make_hook_input("vrg-git status", cwd=str(managed)), managed)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    @pytest.mark.parametrize(
        ("env", "denies"),
        [({"VRG_IDENTITY_MODE": "user"}, True), ({"VRG_IDENTITY_MODE": "human"}, False)],
        ids=["agent", "human"],
    )
    def test_content_read_by_identity(
        self, managed: Path, tmp_path: Path, env: dict[str, str], denies: bool
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        drop = ("VRG_IDENTITY_MODE", "VRG_APP_ID")
        clean = {k: v for k, v in os.environ.items() if k not in drop}
        clean |= {"HOME": str(home), **env}
        raw = _make_hook_input("vrg-gh issue view 5 --comments", cwd=str(managed))
        proc = _spawn(raw, managed, env=clean)
        assert (proc.returncode, proc.stderr) == (0, "")
        if denies:
            assert "vrg-issue-read" in _reason(proc.stdout)
        else:
            assert proc.stdout == ""

    def test_raw_git_allowed_outside_managed_repo(self, unmanaged: Path) -> None:
        assert _find_vergil_toml(unmanaged) is None, "tmp_path sits under a vergil.toml"
        proc = _spawn(_make_hook_input("git status", cwd=str(unmanaged)), unmanaged)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


# -- _find_vergil_toml --------------------------------------------------------


class TestFindVergilToml:
    def test_finds_in_current_dir(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text("[project]\n")
        assert _find_vergil_toml(tmp_path) == tmp_path / "vergil.toml"

    def test_finds_in_parent_dir(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text("[project]\n")
        child = tmp_path / "src" / "pkg"
        child.mkdir(parents=True)
        assert _find_vergil_toml(child) == tmp_path / "vergil.toml"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        child = tmp_path / "some" / "deep" / "path"
        child.mkdir(parents=True)
        assert _find_vergil_toml(child) is None
