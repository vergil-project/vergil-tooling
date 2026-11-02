"""Tests for vergil_tooling.lib.epic_docs."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from vergil_tooling.lib import epic_docs, github
from vergil_tooling.lib.epic_docs import (
    BEGIN,
    END,
    NOT_PLANNED,
    NOT_WRITTEN,
    PENDING,
    PREDATES_RETRO,
    PREDATES_SPEC_PLAN,
    DocLink,
    EpicDocsFormatError,
    parse,
    render,
    strip_legacy,
    upsert,
)

_OWNER = "vergil-project"
_HOME = ".github"


def _link(path: str, repo: str = _HOME, branch: str = "develop") -> DocLink:
    return DocLink(_OWNER, repo, path, branch)


def _entries(
    spec: epic_docs.Entry = PENDING,
    plan: epic_docs.Entry = PENDING,
    retro: epic_docs.Entry = PENDING,
) -> dict[str, epic_docs.Entry]:
    return {"spec": spec, "plan": plan, "retrospective": retro}


def _block(**kwargs: epic_docs.Entry) -> str:
    return render(_entries(**kwargs), _HOME)


# --- render -------------------------------------------------------------------


def test_render_order_and_markers() -> None:
    lines = _block(spec=_link("epics/1-x/spec.md")).split("\n")
    assert lines[0] == BEGIN
    assert lines[1] == "**Documents**"
    assert lines[2] == ""
    assert lines[3].startswith("- Spec: ")
    assert lines[4] == "- Plan: pending"
    assert lines[5] == "- Retrospective: pending"
    assert lines[6] == END
    assert len(lines) == 7


def test_render_link_home_and_other_repo() -> None:
    home = _link("epics/819-x/spec.md")
    assert home.text(_HOME) == "epics/819-x/spec.md"
    assert home.url == (
        "https://github.com/vergil-project/.github/blob/develop/epics/819-x/spec.md"
    )
    other = _link("docs/specs/a.md", repo="vergil-vm", branch="main")
    assert other.text(_HOME) == "vergil-vm/docs/specs/a.md"
    assert other.url == "https://github.com/vergil-project/vergil-vm/blob/main/docs/specs/a.md"
    block = _block(spec=home, plan=other)
    assert (
        "- Spec: [`epics/819-x/spec.md`]"
        "(https://github.com/vergil-project/.github/blob/develop/epics/819-x/spec.md)"
    ) in block
    assert "- Plan: [`vergil-vm/docs/specs/a.md`](" in block


def test_render_rejects_bad_entries() -> None:
    with pytest.raises(ValueError, match="not a Documents note"):
        render(_entries(spec="maybe later"), _HOME)
    with pytest.raises(ValueError, match="exactly"):
        render({"spec": PENDING, "plan": PENDING}, _HOME)
    with pytest.raises(ValueError, match="repo name"):
        _link("a.md").text("vergil-project/.github")


@pytest.mark.parametrize(
    ("field", "value"),
    [("path", ""), ("path", "/abs.md"), ("path", "a b.md"), ("path", "a`b.md"), ("branch", "")],
)
def test_doclink_rejects_bad_fields(field: str, value: str) -> None:
    fields = {"owner": _OWNER, "repo": _HOME, "path": "a.md", "branch": "develop", field: value}
    with pytest.raises(ValueError, match="DocLink"):
        DocLink(**fields)


# --- parse --------------------------------------------------------------------


@pytest.mark.parametrize(
    "entries",
    [
        _entries(_link("epics/1-x/spec.md"), _link("epics/1-x/plan.md"), PENDING),
        _entries(NOT_PLANNED, PREDATES_SPEC_PLAN, PREDATES_RETRO),
        _entries(NOT_WRITTEN, _link("docs/plans/p.md", repo="vergil-vm", branch="main"), PENDING),
        # A branch with a slash, and another repo whose path starts with its own name.
        _entries(
            _link("x/spec.md", branch="release/v2"),
            _link("vergil-vm/plan.md", repo="vergil-vm"),
            _link("epics/1-x/retro(draft).md"),
        ),
    ],
)
def test_parse_roundtrip(entries: dict[str, epic_docs.Entry]) -> None:
    assert parse(render(entries, _HOME)) == entries
    assert parse("Intro.\n\n" + render(entries, _HOME) + "\n\nMore.") == entries
    assert parse(render(entries, _HOME), _HOME) == entries


@pytest.mark.parametrize(
    "link",
    [
        # Another repo's link on a branch ending in /<repo>.
        _link("plan.md", repo="vergil-vm", branch="x/vergil-vm"),
        # A home path that starts with the home repo's own name.
        _link(".github/workflows/notes.md"),
    ],
    ids=["branch-ends-in-repo", "home-path-starts-with-repo"],
)
def test_parse_with_home_repo_is_exact(link: DocLink) -> None:
    block = render(_entries(plan=link), _HOME)
    assert parse(block, _HOME) == _entries(plan=link)
    # Without the home repo the text fits both forms: refused, never guessed.
    with pytest.raises(EpicDocsFormatError, match="ambiguous"):
        parse(block)


def test_parse_with_home_repo_rejects_wrong_text_form() -> None:
    # Another repo's link must show <repo>/<path> as its text.
    inner = (
        "**Documents**\n\n- Spec: [`a.md`](https://github.com/o/vergil-vm/blob/main/a.md)\n"
        "- Plan: pending\n- Retrospective: pending"
    )
    with pytest.raises(EpicDocsFormatError, match="does not match"):
        parse(f"{BEGIN}\n{inner}\n{END}\n", _HOME)
    with pytest.raises(ValueError, match="repo name"):
        parse("", "o/.github")


def test_repo_names_compare_without_case() -> None:
    # A section written under --epic o/.GitHub#N parses back with home ".github".
    link = DocLink(_OWNER, ".GitHub", "epics/1-x/spec.md", "develop")
    assert link.text(".github") == "epics/1-x/spec.md"
    block = render(_entries(spec=link), ".GitHub")
    assert parse(block, _HOME) == _entries(spec=link)


def test_parse_ignores_markers_in_indented_code_block() -> None:
    section = render(_entries(), _HOME)
    indented = "\n".join(f"    {line}" if line else "" for line in section.split("\n"))
    assert parse(f"Example:\n\n{indented}\n") is None
    # Up to three spaces is still the section.
    shallow = "\n".join(f"   {line}" if line else "" for line in section.split("\n"))
    assert parse(shallow) == _entries()


def test_only_newline_ends_a_line() -> None:
    # A form feed or U+2028 renders inside one line, so it never starts a legacy line.
    for sep in (" ", "\x0c", "\x85"):
        body = f"Note text{sep}Spec: `x.md`\n"
        assert strip_legacy(body) == (body, [])
        assert upsert(body, _block())[0].endswith(f"\n\n{body}")


def test_parse_none_without_section() -> None:
    assert parse("") is None
    assert parse("Just a description.\n\nSpec: `epics/1-x/spec.md`\n") is None


def test_parse_ignores_markers_in_code_fence() -> None:
    body = f"Example:\n\n```markdown\n{BEGIN}\n**Documents**\n{END}\n```\n"
    assert parse(body) is None


def test_parse_ignores_markers_in_nested_code_fence() -> None:
    # A three-backtick line inside a four-backtick fence does not close it.
    section = render(_entries(), _HOME)
    body = f"Example:\n\n````markdown\n```text\n{section}\n```\n````\n"
    assert parse(body) is None
    # Nor does a closing run with an info string, or a run of the other character.
    body = f"~~~~\n~~~~ text\n{section}\n```\n~~~~\n"
    assert parse(body) is None


@pytest.mark.parametrize(
    "inner",
    [
        "**Documents**\n\n- Spec: someday\n- Plan: pending\n- Retrospective: pending",
        "**Documents**\n\n- Spec: pending\n- Retrospective: pending",
        "**Documents**\n\n- Plan: pending\n- Spec: pending\n- Retrospective: pending",
        "**Documents**\n\n- Spec: pending\n- Plan: pending\n- Retrospective: pending\n- Extra: x",
        "- Spec: pending\n- Plan: pending\n- Retrospective: pending",
        "**Documents**\n\n- Spec: [`a.md`](https://example.com/a.md)\n- Plan: pending\n"
        "- Retrospective: pending",
        "**Documents**\n\n- Spec: [`b.md`](https://github.com/o/r/blob/develop/a.md)\n"
        "- Plan: pending\n- Retrospective: pending",
        "**Documents**\n\n- Spec: [`a.md`](https://github.com/o/r/blob/dev`x/a.md)\n"
        "- Plan: pending\n- Retrospective: pending",
    ],
    ids=[
        "unknown-note",
        "missing-entry",
        "out-of-order",
        "extra-entry",
        "no-heading",
        "foreign-link",
        "text-url-mismatch",
        "bad-branch",
    ],
)
def test_parse_rejects_unknown_note_and_missing_entry(inner: str) -> None:
    with pytest.raises(EpicDocsFormatError):
        parse(f"{BEGIN}\n{inner}\n{END}\n")


# --- upsert -------------------------------------------------------------------


def test_upsert_prepends_when_absent() -> None:
    body = "An epic description.\n\nPlanning: vergil-project/.github#1\n"
    block = _block()
    assert upsert(body, block) == (block + "\n\n" + body, [])


def test_upsert_replaces_in_place_keeps_rest() -> None:
    before = "Lead text.\n\n"
    after = "\n\nDescription.\n\n| a | b |\n|---|---|\n\nPlanning: #1\n"
    body = before + _block() + after
    block = _block(spec=_link("epics/1-x/spec.md"))
    new, removed = upsert(body, block)
    assert new == before + block + after
    assert removed == []
    # Idempotent: the same block again changes nothing.
    assert upsert(new, block) == (new, [])


@pytest.mark.parametrize(
    "body",
    [
        f"{BEGIN}\n**Documents**\n\nText.\n",
        f"Text.\n{END}\n",
        f"{END}\nText.\n{BEGIN}\n",
        f"{_block()}\n\n{_block()}\n",
    ],
    ids=["only-begin", "only-end", "end-before-begin", "two-sections"],
)
def test_upsert_half_section_raises(body: str) -> None:
    with pytest.raises(EpicDocsFormatError):
        upsert(body, _block())
    with pytest.raises(EpicDocsFormatError):
        parse(body)


def test_upsert_empty_body() -> None:
    block = _block()
    assert upsert("", block)[0] == block
    assert upsert("\n\n", block)[0] == block


def test_upsert_body_of_only_legacy_lines() -> None:
    block = _block()
    assert upsert("Spec: `epics/1-x/spec.md`\n", block) == (block, ["Spec: `epics/1-x/spec.md`"])


# --- strip_legacy -------------------------------------------------------------

_LEGACY_558 = (
    "Each repo's steering files point to the policy.\n"
    "\n"
    "Documents:\n"
    "\n"
    "- Spec: [`epics/558-x/spec.md`](https://github.com/o/.github/blob/develop/epics/558-x/spec.md)\n"
    "- Plan: [`epics/558-x/plan.md`](https://github.com/o/.github/blob/develop/epics/558-x/plan.md)\n"
    "- Retrospective: pending\n"
    "\n"
    "Umbrella: vergil-project/.github#500\n"
)


def test_strip_legacy_documents_list() -> None:
    new, removed = strip_legacy(_LEGACY_558)
    assert new == (
        "Each repo's steering files point to the policy.\n\nUmbrella: vergil-project/.github#500\n"
    )
    assert removed == [
        "Documents:",
        "- Spec: [`epics/558-x/spec.md`](https://github.com/o/.github/blob/develop/epics/558-x/spec.md)",
        "- Plan: [`epics/558-x/plan.md`](https://github.com/o/.github/blob/develop/epics/558-x/plan.md)",
        "- Retrospective: pending",
    ]


def test_strip_legacy_documents_list_at_top() -> None:
    body = (
        "Documents:\n\n- Spec: pending (published by the documentation task)\n"
        "- Plan: pending (published by the documentation task)\n- Retrospective: pending\n\n"
        "Every epic issue opens with a section.\n"
    )
    new, removed = strip_legacy(body)
    assert new == "Every epic issue opens with a section.\n"
    assert len(removed) == 4


def test_strip_legacy_documents_list_with_gaps_between_bullets() -> None:
    body = (
        "Intro.\n\nDocuments:\n\n- Spec: pending\n\n- Plan: pending\n\n- Retrospective: pending\n\n"
        "Description.\n"
    )
    new, removed = strip_legacy(body)
    assert new == "Intro.\n\nDescription.\n"
    assert removed == [
        "Documents:",
        "- Spec: pending",
        "- Plan: pending",
        "- Retrospective: pending",
    ]
    # A list that runs to the end of the body, and one cut off by a fence.
    assert strip_legacy("Intro.\n\nDocuments:\n- Spec: pending\n\n")[0] == "Intro.\n\n"
    fenced = "Documents:\n- Spec: pending\n```\n- Plan: pending\n```\n"
    assert strip_legacy(fenced) == (
        "```\n- Plan: pending\n```\n",
        ["Documents:", "- Spec: pending"],
    )


def test_strip_legacy_keeps_documents_line_without_bullets() -> None:
    body = "Documents:\n\nSee the wiki.\n"
    assert strip_legacy(body) == (body, [])


def test_strip_legacy_standalone_lines() -> None:
    body = (
        "Intro.\n\n"
        "Spec: `epics/550-x/spec.md` (published by the documentation task).\n\n"
        "Middle.\n"
        "Plan: `epics/550-x/plan.md`\n"
        "Retrospective: `epics/550-x/retrospective.md`\n\n"
        "Planning: vergil-project/.github#469\n"
    )
    new, removed = strip_legacy(body)
    assert new == "Intro.\n\nMiddle.\n\nPlanning: vergil-project/.github#469\n"
    assert removed == [
        "Spec: `epics/550-x/spec.md` (published by the documentation task).",
        "Plan: `epics/550-x/plan.md`",
        "Retrospective: `epics/550-x/retrospective.md`",
    ]


def test_strip_legacy_standalone_note_lines() -> None:
    body = "Spec: pending\nPlan: not written (closed as not planned).\nRetrospective: pending.\n"
    new, removed = strip_legacy(body)
    assert new == ""
    assert removed == body.splitlines()


@pytest.mark.parametrize(
    "line",
    [
        "Plan: migrate the four repos first.",
        "Spec: see the wiki page for details",
        "Retrospective: pending review by the team",
        "Plan:",
        "Plan: `vrg-foo` must ship before the migration starts.",
        "Spec: [optional] cover CRLF too",
        "Spec: [the draft](https://example.com/a.md) is out of date.",
    ],
)
def test_strip_legacy_keeps_prose_with_a_label(line: str) -> None:
    body = f"Context.\n\n{line}\n(Ordered by risk.)\n\nMore.\n"
    assert strip_legacy(body) == (body, [])


def test_strip_legacy_continuation_line() -> None:
    body = (
        "Extends the per-repo VM profiles model.\n\n"
        "Spec: `vergil-vm/docs/specs/2026-06-19-off-platform-vm-backend-design.md`\n"
        "(not moved — reference-only).\n\n"
        "### Members (tasks in vergil-vm)\n"
    )
    new, removed = strip_legacy(body)
    assert new == "Extends the per-repo VM profiles model.\n\n### Members (tasks in vergil-vm)\n"
    assert removed == [
        "Spec: `vergil-vm/docs/specs/2026-06-19-off-platform-vm-backend-design.md`",
        "(not moved — reference-only).",
    ]


def test_strip_legacy_documents_list_with_wrapped_bullets() -> None:
    body = (
        "Intro.\n\n"
        "Documents:\n\n"
        "- Spec: `epics/7-x/spec.md` (published by the\n"
        "  documentation task)\n"
        "- Plan: `epics/7-x/plan.md` (published by the\n"
        "\tdocumentation task)\n"
        "  and reviewed twice\n"
        "- Retrospective: pending\n\n"
        "Description.\n"
    )
    new, removed = strip_legacy(body)
    assert new == "Intro.\n\nDescription.\n"
    assert removed == [
        "Documents:",
        "- Spec: `epics/7-x/spec.md` (published by the",
        "  documentation task)",
        "- Plan: `epics/7-x/plan.md` (published by the",
        "\tdocumentation task)",
        "  and reviewed twice",
        "- Retrospective: pending",
    ]
    # An indented line after a blank line, or before any bullet, is not a continuation.
    after_gap = "Documents:\n- Spec: pending\n\n    code\n"
    assert strip_legacy(after_gap) == ("    code\n", ["Documents:", "- Spec: pending"])
    before_bullet = "Documents:\n  indented\n- Spec: pending\n"
    assert strip_legacy(before_bullet) == (before_bullet, [])


def test_deeply_indented_backticks_do_not_open_a_fence() -> None:
    # Four spaces make indented code, not a fence, the same limit as the markers.
    body = f"Example:\n\n    ```\n\n{_block()}\n"
    assert parse(body, _HOME) == _entries()
    new, _ = upsert(body, _block(spec=_link("epics/1-x/spec.md")))
    assert new.count(BEGIN) == 1
    assert parse(new, _HOME) == _entries(spec=_link("epics/1-x/spec.md"))
    # Inside a fence, a deeply indented run does not close it.
    fenced = f"```\n    ```\n{_block()}\n```\n"
    assert parse(fenced) is None
    # Three spaces still open one.
    assert parse(f"   ```\n{_block()}\n```\n") is None


def test_strip_legacy_keeps_code_fence_lines() -> None:
    body = (
        "Example:\n\n"
        "```text\n"
        "Documents:\n\n- Spec: pending\n"
        "Spec: `epics/1-x/spec.md`\n"
        "```\n\n"
        "~~~\nPlan: `epics/1-x/other.md`\n~~~\n"
        "Plan: `epics/1-x/plan.md`\n"
    )
    new, removed = strip_legacy(body)
    assert removed == ["Plan: `epics/1-x/plan.md`"]
    assert new == body.replace("Plan: `epics/1-x/plan.md`\n", "")


def test_strip_legacy_keeps_nested_code_fence_lines() -> None:
    body = "Intro\n\n````markdown\n```text\nSpec: `epics/1-x/spec.md`\n```\n````\n"
    assert strip_legacy(body) == (body, [])


def test_strip_legacy_leaves_marked_section_and_prose() -> None:
    body = _block() + "\n\nThe plan: ship it.\n- Spec: not a legacy list\n"
    assert strip_legacy(body) == (body, [])


def test_crlf_body_preserved() -> None:
    lf = (
        "Spec: `epics/7-x/spec.md` (published by the documentation task).\n\n"
        "Description with trailing space \n\n"
        "Planning: #1\n"
    )
    crlf = lf.replace("\n", "\r\n")
    block = _block(spec=_link("epics/7-x/spec.md"))
    new, removed = upsert(crlf, block)
    assert removed == ["Spec: `epics/7-x/spec.md` (published by the documentation task)."]
    assert new == (
        block.replace("\n", "\r\n")
        + "\r\n\r\nDescription with trailing space \r\n\r\nPlanning: #1\r\n"
    )
    assert "\n" not in new.replace("\r\n", "")
    assert parse(new) == _entries(spec=_link("epics/7-x/spec.md"))
    # In place on the CRLF body: only the region changes, every other byte is kept.
    block2 = _block(spec=_link("epics/7-x/spec.md"), plan=_link("epics/7-x/plan.md"))
    again, removed_again = upsert(new, block2)
    assert removed_again == []
    assert again == new.replace(block.replace("\n", "\r\n"), block2.replace("\n", "\r\n"))


# --- GitHub reads and writes --------------------------------------------------


def test_read_body() -> None:
    with patch.object(epic_docs.github, "read_json", return_value={"body": "B"}) as mock_read:
        assert epic_docs.read_body("o", ".github", 5) == "B"
    mock_read.assert_called_once_with("api", "repos/o/.github/issues/5")


def test_read_body_none_is_empty() -> None:
    with patch.object(epic_docs.github, "read_json", return_value={"body": None}):
        assert epic_docs.read_body("o", ".github", 5) == ""


@pytest.mark.parametrize("payload", [[], {"body": 3}, {"title": "no body field"}])
def test_read_body_rejects_unexpected_response(payload: object) -> None:
    with (
        patch.object(epic_docs.github, "read_json", return_value=payload),
        pytest.raises(ValueError, match="unexpected"),
    ):
        epic_docs.read_body("o", ".github", 5)


def test_write_body() -> None:
    with patch.object(epic_docs.github, "write_json") as mock_write:
        epic_docs.write_body("o", ".github", 5, "new")
    mock_write.assert_called_once_with("PATCH", "repos/o/.github/issues/5", {"body": "new"})


def test_default_branch() -> None:
    with patch.object(
        epic_docs.branch_sweep.github, "read_json", return_value={"default_branch": "main"}
    ) as mock_read:
        assert epic_docs.default_branch("o", "vergil-vm") == "main"
    mock_read.assert_called_once_with("api", "repos/o/vergil-vm")


def _not_found() -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ["gh", "api"], output="", stderr="gh: Not Found (HTTP 404)")


def _server_error() -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ["gh", "api"], output="", stderr="HTTP 502: Bad Gateway")


def test_file_exists() -> None:
    with patch.object(epic_docs.github, "read_json", return_value={"type": "file"}) as mock_read:
        assert epic_docs.file_exists("o", "vm", "docs/specs/a b.md", "x/y")
    mock_read.assert_called_once_with("api", "repos/o/vm/contents/docs/specs/a%20b.md?ref=x%2Fy")
    with patch.object(epic_docs.github, "read_json", side_effect=_not_found()):
        assert not epic_docs.file_exists("o", "vm", "docs/specs/a.md", "main")
    with (
        patch.object(epic_docs.github, "read_json", side_effect=_server_error()),
        pytest.raises(github.GitHubAPIError),
    ):
        epic_docs.file_exists("o", "vm", "docs/specs/a.md", "main")


def test_list_dir() -> None:
    listing = [{"name": "spec.md", "type": "file"}, {"name": "img", "type": "dir"}]
    with patch.object(epic_docs.github, "read_json", return_value=listing):
        assert epic_docs.list_dir("o", ".github", "epics/1-x", "develop") == {
            "spec.md": "file",
            "img": "dir",
        }
    with patch.object(epic_docs.github, "read_json", side_effect=_not_found()):
        assert epic_docs.list_dir("o", ".github", "epics", "develop") is None
    for bad in ({"type": "file"}, [{"name": 3, "type": "file"}]):
        with (
            patch.object(epic_docs.github, "read_json", return_value=bad),
            pytest.raises(ValueError, match="directory|unexpected"),
        ):
            epic_docs.list_dir("o", ".github", "epics", "develop")
    with (
        patch.object(epic_docs.github, "read_json", side_effect=_server_error()),
        pytest.raises(github.GitHubAPIError),
    ):
        epic_docs.list_dir("o", ".github", "epics", "develop")


# --- backfill: which epics, entry rules and discovery ---------------------------


@pytest.mark.parametrize(
    ("title", "labels", "skipped"),
    [
        ("Epic (ad hoc): x", {"epic", "ad-hoc"}, True),
        ("Epic (intake): x", {"epic", "intake"}, True),
        ("Ad-hoc maintenance", {"epic", "standing"}, True),
        ("Epic (standing): Ad-hoc maintenance", {"epic"}, True),
        ("[archived] Epic: x", {"epic"}, True),
        ("Epic: x", {"epic"}, False),
        ("Sync .gitignore to baseline", {"epic"}, False),
    ],
)
def test_is_skipped(title: str, labels: set[str], skipped: bool) -> None:
    assert epic_docs.is_skipped(title, labels) is skipped


_SPEC = _link("epics/9-x/spec.md")


@pytest.mark.parametrize(
    ("doc", "link", "state", "reason", "created", "expected"),
    [
        ("spec", _SPEC, "CLOSED", "NOT_PLANNED", "2026-01-01T00:00:00Z", _SPEC),
        ("spec", None, "OPEN", None, "2026-01-01T00:00:00Z", PENDING),
        ("retrospective", None, "CLOSED", "NOT_PLANNED", "2026-07-01T00:00:00Z", NOT_PLANNED),
        ("spec", None, "closed", "completed", "2026-07-01T00:00:00Z", PREDATES_SPEC_PLAN),
        ("plan", None, "CLOSED", "COMPLETED", "2026-07-11T23:59:59Z", PREDATES_SPEC_PLAN),
        ("plan", None, "CLOSED", "COMPLETED", "2026-07-12T00:00:00Z", NOT_WRITTEN),
        ("retrospective", None, "CLOSED", "COMPLETED", "2026-07-16T00:00:00Z", PREDATES_RETRO),
        ("spec", None, "CLOSED", "COMPLETED", "2026-07-16T00:00:00Z", NOT_WRITTEN),
        ("retrospective", None, "CLOSED", None, "2026-08-25T00:00:00Z", NOT_WRITTEN),
        ("spec", None, "CLOSED", "COMPLETED", "2026-08-25T00:00:00Z", NOT_WRITTEN),
    ],
)
def test_entry_for_table(  # noqa: PLR0913 - one parametrized table row
    doc: str,
    link: DocLink | None,
    state: str,
    reason: str | None,
    created: str,
    expected: epic_docs.Entry,
) -> None:
    entry = epic_docs.entry_for(doc, link, state=state, state_reason=reason, created_at=created)
    assert entry == expected


def test_entry_for_rejects_bad_input() -> None:
    kwargs = {"state_reason": None, "created_at": "2026-08-01T00:00:00Z"}
    with pytest.raises(ValueError, match="not a document"):
        epic_docs.entry_for("notes", None, state="CLOSED", **kwargs)
    with pytest.raises(ValueError, match="unexpected issue state"):
        epic_docs.entry_for("spec", None, state="MERGED", **kwargs)
    with pytest.raises(ValueError, match="unexpected creation time"):
        epic_docs.entry_for("spec", None, state="CLOSED", state_reason=None, created_at="soon")


class _Tree:
    """A fake org: directory listings and files by (repo, path), with default branches."""

    def __init__(self, files: set[tuple[str, str]], branches: dict[str, str] | None = None):
        self.files = files
        self.branches = branches or {_HOME: "develop", "vergil-vm": "main"}
        self.listed: list[str] = []

    def list_dir(self, owner: str, repo: str, path: str, branch: str) -> dict[str, str] | None:
        assert owner == _OWNER
        self.listed.append(path)
        prefix = f"{path}/"
        entries: dict[str, str] = {}
        for file_repo, file_path in self.files:
            if file_repo == repo and file_path.startswith(prefix):
                head, _, rest = file_path[len(prefix) :].partition("/")
                entries[head] = "dir" if rest else "file"
        return entries or None

    def file_exists(self, owner: str, repo: str, path: str, branch: str) -> bool:
        assert branch == self.branches[repo]
        return (repo, path) in self.files

    def default_branch(self, owner: str, repo: str) -> str:
        if repo not in self.branches:
            raise _not_found()
        return self.branches[repo]

    def discover(self, number: int, body: str = "") -> dict[str, DocLink | None]:
        with (
            patch.object(epic_docs, "list_dir", self.list_dir),
            patch.object(epic_docs, "file_exists", self.file_exists),
            patch.object(epic_docs, "default_branch", self.default_branch),
        ):
            return epic_docs.discover(_OWNER, _HOME, number, body, "develop")


def test_epic_directories_skip_archive_and_files() -> None:
    tree = _Tree({(_HOME, "epics/1-x/spec.md"), (_HOME, "epics/archive/9-y/spec.md")})
    tree.files.add((_HOME, "epics/README.md"))
    with patch.object(epic_docs, "list_dir", tree.list_dir):
        assert epic_docs.epic_directories(_OWNER, _HOME, "develop") == {"1-x"}
    with patch.object(epic_docs, "list_dir", return_value=None):
        assert epic_docs.epic_directories(_OWNER, _HOME, "develop") == frozenset()


def test_discover_epic_dir() -> None:
    tree = _Tree(
        {
            (_HOME, "epics/819-x/spec.md"),
            (_HOME, "epics/819-x/plan.md"),
            (_HOME, "epics/819-x/audit-findings.md"),
            (_HOME, "epics/81-y/retrospective.md"),
        }
    )
    assert tree.discover(819) == {
        "spec": _link("epics/819-x/spec.md"),
        "plan": _link("epics/819-x/plan.md"),
        "retrospective": None,
    }


def test_discover_body_mentioned_dir() -> None:
    tree = _Tree({(_HOME, "epics/553-x/spec.md"), (_HOME, "epics/553-x/retrospective.md")})
    body = "Spec: `epics/553-x/spec.md` (published by the documentation task).\n"
    assert tree.discover(560, body) == {
        "spec": _link("epics/553-x/spec.md"),
        "plan": None,
        "retrospective": _link("epics/553-x/retrospective.md"),
    }
    # A mentioned directory that does not exist, or the archive, is no candidate.
    tree.files.add((_HOME, "epics/archive/553-x/spec.md"))
    assert tree.discover(560, "See epics/archive/553-x/ and epics/554-gone/.") == dict.fromkeys(
        epic_docs.DOCS
    )
    assert "epics/archive" not in tree.listed


@pytest.mark.parametrize(
    "mention",
    [
        "vergil-vm/docs/specs/a.md",
        "vergil-project/vergil-vm/docs/specs/a.md",
        "`vergil-vm/docs/specs/a.md`",
        "(see vergil-vm/docs/specs/a.md).",
    ],
)
def test_discover_member_repo_spec(mention: str) -> None:
    tree = _Tree({("vergil-vm", "docs/specs/a.md")})
    assert tree.discover(51, f"Spec: {mention}\n") == {
        "spec": _link("docs/specs/a.md", "vergil-vm", "main"),
        "plan": None,
        "retrospective": None,
    }


def test_discover_superpowers_spec_and_home_plan() -> None:
    tree = _Tree({("vergil-vm", "docs/superpowers/specs/a.md"), (_HOME, "docs/plans/p.md")})
    body = "Spec: vergil-vm/docs/superpowers/specs/a.md\nPlan: docs/plans/p.md\n"
    assert tree.discover(52, body) == {
        "spec": _link("docs/superpowers/specs/a.md", "vergil-vm", "main"),
        "plan": _link("docs/plans/p.md"),
        "retrospective": None,
    }


def test_discover_ignores_missing_other_org_and_unknown_repo() -> None:
    tree = _Tree({("vergil-vm", "docs/specs/a.md")})
    body = (
        "other-org/vergil-vm/docs/specs/a.md\n"
        "no-such-repo/docs/specs/a.md\n"
        "vergil-vm/docs/specs/missing.md\n"
    )
    assert tree.discover(53, body) == dict.fromkeys(epic_docs.DOCS)


def test_discover_same_spec_twice_is_one_candidate() -> None:
    tree = _Tree({("vergil-vm", "docs/specs/a.md")})
    body = "vergil-vm/docs/specs/a.md and vergil-project/vergil-vm/docs/specs/a.md\n"
    assert tree.discover(53, body)["spec"] == _link("docs/specs/a.md", "vergil-vm", "main")


def test_discover_two_dirs_ambiguous() -> None:
    tree = _Tree({(_HOME, "epics/7-a/spec.md"), (_HOME, "epics/7-b/spec.md")})
    with pytest.raises(epic_docs.Ambiguous) as caught:
        tree.discover(7)
    assert caught.value.candidates == ("epics/7-a/", "epics/7-b/")
    # Two mentioned directories, with none of the epic's own, are ambiguous too.
    with pytest.raises(epic_docs.Ambiguous, match="2 candidate directories"):
        tree.discover(8, "epics/7-a/spec.md and epics/7-b/spec.md")


def test_discover_two_specs_ambiguous() -> None:
    tree = _Tree({("vergil-vm", "docs/specs/a.md"), (_HOME, "docs/specs/b.md")})
    with pytest.raises(epic_docs.Ambiguous, match="2 candidate specs") as caught:
        tree.discover(51, "vergil-vm/docs/specs/a.md\ndocs/specs/b.md\n")
    assert set(caught.value.candidates) == {"vergil-vm/docs/specs/a.md", ".github/docs/specs/b.md"}


def test_discover_non_404_error_propagates() -> None:
    tree = _Tree({("vergil-vm", "docs/specs/a.md")})
    with (
        patch.object(epic_docs, "list_dir", tree.list_dir),
        patch.object(epic_docs, "file_exists", side_effect=_server_error()),
        patch.object(epic_docs, "default_branch", tree.default_branch),
        pytest.raises(github.GitHubAPIError),
    ):
        epic_docs.discover(_OWNER, _HOME, 51, "vergil-vm/docs/specs/a.md", "develop")
    with (
        patch.object(epic_docs, "list_dir", tree.list_dir),
        patch.object(epic_docs, "default_branch", side_effect=_server_error()),
        pytest.raises(github.GitHubAPIError),
    ):
        epic_docs.discover(_OWNER, _HOME, 51, "vergil-vm/docs/specs/a.md", "develop")


def test_discover_uses_given_directories_and_branch_cache() -> None:
    tree = _Tree({("vergil-vm", "docs/specs/a.md")})
    branches = {"Vergil-VM": "main"}
    with (
        patch.object(epic_docs, "list_dir", side_effect=AssertionError("listed")),
        patch.object(epic_docs, "file_exists", tree.file_exists),
        patch.object(epic_docs, "default_branch", side_effect=AssertionError("looked up")),
    ):
        body = "vergil-vm/docs/specs/a.md"
        found = epic_docs.discover(
            _OWNER, _HOME, 51, body, "develop", epic_dirs=[], branches=branches
        )
    assert found["spec"] == _link("docs/specs/a.md", "vergil-vm", "main")
    assert branches == {"Vergil-VM": "main", _HOME: "develop"}


def test_discover_directory_gone_since_listing() -> None:
    with (
        patch.object(epic_docs, "list_dir", return_value=None),
        pytest.raises(ValueError, match="no longer exists"),
    ):
        epic_docs.discover(_OWNER, _HOME, 7, "", "develop", epic_dirs=["7-a"])


def _issue(n: int, title: str = "Epic: x", **fields: object) -> dict[str, object]:
    issue: dict[str, object] = {
        "number": n,
        "title": title,
        "state": "closed",
        "stateReason": "completed",
        "createdAt": "2026-08-01T00:00:00Z",
        "labels": ["epic"],
        "body": f"Epic {n}.",
    }
    issue.update(fields)
    return issue


def test_list_epics_paginates() -> None:
    page1 = [_issue(n) for n in range(1, 101)]
    page2 = [_issue(101), _issue(102, "Epic (ad hoc): o", labels=["epic", "ad-hoc"])]
    raw = json.dumps(page1) + json.dumps(page2)
    with patch.object(epic_docs.github, "read_output", return_value=raw) as mock_output:
        records = epic_docs.list_epics("o/.github")
    assert [r["number"] for r in records] == list(range(1, 102))
    assert records[0]["state"] == "CLOSED"
    assert records[0]["stateReason"] == "COMPLETED"
    args = mock_output.call_args.args
    endpoint = "repos/o/.github/issues?labels=epic&state=all&per_page=100"
    assert args[:3] == ("api", "--paginate", endpoint)
    # The archived body is dropped by the query itself, so it is never read.
    jq = args[args.index("--jq") + 1]
    assert 'startswith("[archived]")' in jq
    assert 'select(has("pull_request") | not)' in jq


def test_list_epics_without_skips() -> None:
    raw = json.dumps([_issue(1), _issue(2, "[archived] Epic: y", body=None)])
    with patch.object(epic_docs.github, "read_output", return_value=raw):
        assert [r["number"] for r in epic_docs.list_epics("o/.github")] == [1]
        records = epic_docs.list_epics("o/.github", apply_skips=False)
    assert [r["body"] for r in records] == ["Epic 1.", None]


@pytest.mark.parametrize(
    "bad",
    [
        "not an object",
        _issue(1, number="1"),
        _issue(1, labels=[{"name": "epic"}]),
        _issue(1, stateReason=3),
        _issue(1, body=7),
    ],
)
def test_list_epics_rejects_unexpected_shape(bad: object) -> None:
    with (
        patch.object(epic_docs.github, "read_output", return_value=json.dumps([bad])),
        pytest.raises(ValueError, match="unexpected epic"),
    ):
        epic_docs.list_epics("o/.github")
