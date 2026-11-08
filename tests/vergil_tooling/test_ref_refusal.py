"""Tests for the ruleset-refusal note on a bare 422 from the REST refs API (#3997)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.ref_refusal import ruleset_hint

if TYPE_CHECKING:
    from collections.abc import Sequence

# What gh prints when GitHub refuses a refs write with a bare 422.
_STDERR = "gh: Reference update failed (HTTP 422)"
_STDOUT = '{"message":"Reference update failed","status":"422"}'


def _exc(
    cmd: str | Sequence[str],
    *,
    stderr: str | bytes | None = _STDERR,
    stdout: str | bytes | None = _STDOUT,
) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, cmd, stdout, stderr)


def test_branch_deletion_names_the_branch_the_repo_and_how_to_see_the_rules() -> None:
    hint = ruleset_hint(_exc(("gh", "api", "repos/o/r/git/refs/heads/feature/1-x", "-X", "DELETE")))
    assert hint == (
        "Likely cause: a repository ruleset blocks the deletion of "
        "refs/heads/feature/1-x in o/r.\n"
        "GitHub's REST refs API reports a ruleset refusal as a bare 422 'Reference update "
        "failed' with no rule text (vergil-tooling#3997); a git push of the same ref prints it.\n"
        "See the rules on that branch: gh api repos/o/r/rules/branches/feature/1-x\n"
        "See the repo's rulesets: gh api repos/o/r/rulesets, "
        "or compare them with the desired set: vrg-github-repo-config audit --repo o/r"
    )


def test_a_url_encoded_branch_is_named_decoded() -> None:
    hint = ruleset_hint(_exc(["gh", "api", "repos/o/r/git/refs/heads/a%20b", "-X", "DELETE", "-i"]))
    assert hint is not None
    assert "deletion of refs/heads/a b in o/r" in hint


def test_a_create_on_the_collection_names_the_ref_generically() -> None:
    hint = ruleset_hint(_exc(["gh", "api", "/repos/o/r/git/refs", "--method", "post"]))
    assert hint is not None
    assert "blocks the creation of the ref named in the request in o/r." in hint
    assert "rules/branches" not in hint
    assert "gh api repos/o/r/rulesets" in hint


def test_a_tag_update_gets_no_branch_rules_line() -> None:
    hint = ruleset_hint(_exc(["gh", "api", "repos/o/r/git/refs/tags/v1.2", "-X", "PATCH"]))
    assert hint is not None
    assert "blocks the update of refs/tags/v1.2 in o/r." in hint
    assert "rules/branches" not in hint


def test_bytes_output_is_read() -> None:
    exc = _exc(
        ["gh", "api", "repos/o/r/git/refs/heads/x", "-X", "DELETE"],
        stderr=_STDERR.encode(),
        stdout=None,
    )
    assert ruleset_hint(exc) is not None


@pytest.mark.parametrize(
    ("cmd", "stderr", "stdout"),
    [
        pytest.param("gh api repos/o/r/git/refs/heads/x -X DELETE", _STDERR, _STDOUT, id="str-cmd"),
        pytest.param(["git", "push"], _STDERR, _STDOUT, id="not-gh-api"),
        pytest.param(
            ["gh", "api", "repos/o/r/pulls", "-X", "POST"], _STDERR, _STDOUT, id="no-refs"
        ),
        pytest.param(["gh", "api", "repos/o/r/git/refs/heads/x"], _STDERR, _STDOUT, id="get"),
        pytest.param(
            ["gh", "api", "repos/o/r/git/ref/heads/x", "-X", "DELETE"], _STDERR, _STDOUT, id="ref"
        ),
        pytest.param(
            ["gh", "api", "repos/o/r/git/refs/heads/x", "-X", "DELETE"],
            "gh: Reference does not exist (HTTP 422)",
            None,
            id="other-422",
        ),
        pytest.param(
            ["gh", "api", "repos/o/r/git/refs/heads/x", "-X", "DELETE"],
            "gh: Reference update failed",
            None,
            id="no-status",
        ),
    ],
)
def test_no_note_for_any_other_failure(
    cmd: str | Sequence[str], stderr: str, stdout: str | None
) -> None:
    assert ruleset_hint(_exc(cmd, stderr=stderr, stdout=stdout)) is None


def test_github_api_error_appends_the_note_to_its_message() -> None:
    exc = github.GitHubAPIError(
        1, ["gh", "api", "repos/o/r/git/refs/heads/x", "-X", "DELETE"], _STDOUT, _STDERR
    )
    text = str(exc)
    assert text.startswith(
        "Command 'gh api repos/o/r/git/refs/heads/x -X DELETE' returned non-zero exit status 1.\n"
        f"stderr: {_STDERR}\nstdout: {_STDOUT}\n"
        "Likely cause: a repository ruleset blocks the deletion of refs/heads/x in o/r."
    )
