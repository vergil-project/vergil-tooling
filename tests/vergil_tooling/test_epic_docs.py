"""Tests for vergil_tooling.lib.epic_docs."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib import epic_docs
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


def test_parse_none_without_section() -> None:
    assert parse("") is None
    assert parse("Just a description.\n\nSpec: `epics/1-x/spec.md`\n") is None


def test_parse_ignores_markers_in_code_fence() -> None:
    body = f"Example:\n\n```markdown\n{BEGIN}\n**Documents**\n{END}\n```\n"
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


def test_strip_legacy_keeps_code_fence_lines() -> None:
    body = (
        "Example:\n\n"
        "```text\n"
        "Documents:\n\n- Spec: pending\n"
        "Spec: `epics/1-x/spec.md`\n"
        "```\n\n"
        "~~~\nPlan: kept\n~~~\n"
        "Plan: `epics/1-x/plan.md`\n"
    )
    new, removed = strip_legacy(body)
    assert removed == ["Plan: `epics/1-x/plan.md`"]
    assert new == body.replace("Plan: `epics/1-x/plan.md`\n", "")


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


@pytest.mark.parametrize("payload", [[], {"body": 3}])
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
