"""Tests for vergil_tooling.lib.checkpoint."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import checkpoint

EC = "epic-create"
STAGES = (
    "bootstrap",
    "brainstorm",
    "spec-pushback",
    "spec-review",
    "plan",
    "plan-pushback",
    "alignment",
    "file-tasks",
    "docs-pr",
)


def test_epic_create_stage_list() -> None:
    assert checkpoint.WORKFLOWS[EC] == STAGES


def test_markers() -> None:
    assert checkpoint.begin(EC) == "<!-- vrg-checkpoint:epic-create -->"
    assert checkpoint.end(EC) == "<!-- /vrg-checkpoint:epic-create -->"


def test_render_shape() -> None:
    assert checkpoint.render(EC, ["bootstrap"]) == "\n".join(
        [
            checkpoint.begin(EC),
            "- [x] bootstrap",
            *(f"- [ ] {s}" for s in STAGES[1:]),
            checkpoint.end(EC),
        ]
    )


def test_render_refuses_unknown_stage() -> None:
    with pytest.raises(ValueError, match="nope"):
        checkpoint.render(EC, ["nope"])


def test_unknown_workflow_raises_key_error() -> None:
    with pytest.raises(KeyError):
        checkpoint.render("nope")


def test_render_then_parse_round_trips() -> None:
    block = checkpoint.render(EC, ["bootstrap"])
    assert checkpoint.parse(block, EC) == [(s, s == "bootstrap") for s in STAGES]


def test_status_reads_hand_tick() -> None:  # a trusted web-UI tick, uppercase X
    body = checkpoint.render(EC).replace("- [ ] bootstrap", "- [X] bootstrap")
    assert checkpoint.next_stage(checkpoint.parse(body, EC)) == "brainstorm"


def test_next_stage_none_when_all_ticked() -> None:
    assert checkpoint.next_stage(checkpoint.parse(checkpoint.render(EC, STAGES), EC)) is None


def test_parse_finds_block_amid_text() -> None:
    body = "intro\n\n" + checkpoint.render(EC) + "\n\nafter\n"
    assert [name for name, _ in checkpoint.parse(body, EC)] == list(STAGES)


def test_annotation_is_malformed() -> None:
    body = checkpoint.render(EC).replace("- [ ] plan\n", "- [ ] plan (done by hand)\n")
    with pytest.raises(checkpoint.Malformed):
        checkpoint.parse(body, EC)


@pytest.mark.parametrize(
    "line",
    [
        "some prose",
        "",
        "   ",
        "* [ ] plan",
        "-  [ ] plan",
        "- [ ]  plan",
        "- [ ] plan ",
        " - [ ] plan",
        "- [y] plan",
        "- [ ] Plan",
        "<!-- vrg-checkpoint:other -->",
    ],
)
def test_stray_line_is_malformed(line: str) -> None:
    block = checkpoint.render(EC).replace("- [ ] plan\n", "- [ ] plan\n" + line + "\n")
    with pytest.raises(checkpoint.Malformed):
        checkpoint.parse(block, EC)


@pytest.mark.parametrize("where", ["after-begin", "before-end"])
def test_blank_line_next_to_a_marker_is_malformed(where: str) -> None:
    block = checkpoint.render(EC)
    if where == "after-begin":
        block = block.replace(checkpoint.begin(EC) + "\n", checkpoint.begin(EC) + "\n\n")
    else:
        block = block.replace("\n" + checkpoint.end(EC), "\n\n" + checkpoint.end(EC))
    with pytest.raises(checkpoint.Malformed):
        checkpoint.parse(block, EC)


def test_duplicate_block_is_malformed() -> None:
    body = checkpoint.render(EC, ["bootstrap"]) + "\n\n" + checkpoint.render(EC) + "\n"
    with pytest.raises(checkpoint.Malformed, match="2 blocks"):
        checkpoint.parse(body, EC)


def test_unclosed_block_is_malformed() -> None:
    body = "x\n\n" + checkpoint.begin(EC) + "\n- [ ] bootstrap\n"
    with pytest.raises(checkpoint.Malformed, match="no end marker"):
        checkpoint.parse(body, EC)


def test_end_marker_without_begin_is_malformed() -> None:
    with pytest.raises(checkpoint.Malformed, match="no begin marker"):
        checkpoint.parse("x\n" + checkpoint.end(EC) + "\n", EC)


def test_nested_begin_is_malformed() -> None:
    body = checkpoint.begin(EC) + "\n" + checkpoint.render(EC) + "\n"
    with pytest.raises(checkpoint.Malformed):
        checkpoint.parse(body, EC)


def test_missing_block_raises_missing() -> None:
    with pytest.raises(checkpoint.Missing):
        checkpoint.parse("Planning: x/y#1\n\nno block here\n", EC)


def test_other_workflow_block_is_not_this_one() -> None:
    other = checkpoint.render(EC).replace("epic-create", "epic-implement")
    with pytest.raises(checkpoint.Missing):
        checkpoint.parse(other + "\n", EC)


def test_empty_block_parses_empty() -> None:
    body = checkpoint.begin(EC) + "\n" + checkpoint.end(EC)
    assert checkpoint.parse(body, EC) == []


def test_crlf_body_parses_and_ticks() -> None:
    body = ("Planning: x/y#1\n\n" + checkpoint.render(EC) + "\n\ntext\n").replace("\n", "\r\n")
    assert checkpoint.next_stage(checkpoint.parse(body, EC)) == "bootstrap"
    out = checkpoint.tick(body, EC, "bootstrap")
    assert out == body.replace("- [ ] bootstrap", "- [x] bootstrap")
    assert "\n" not in out.replace("\r\n", "")


def test_skew_reports_added_removed_reordered() -> None:
    pairs = [("brainstorm", True), ("bootstrap", True), ("old-stage", False)]
    s = checkpoint.skew(pairs, EC)
    assert s is not None
    assert s.recorded == ("brainstorm", "bootstrap", "old-stage")
    assert s.current == STAGES
    assert s.removed == ("old-stage",)
    assert s.added == STAGES[2:]
    assert s.reordered


def test_skew_added_only_is_not_reordered() -> None:
    pairs = [(s, False) for s in STAGES if s != "alignment"]
    s = checkpoint.skew(pairs, EC)
    assert s is not None
    assert (s.added, s.removed, s.reordered) == (("alignment",), (), False)


def test_skew_duplicate_stage() -> None:
    pairs = [(s, False) for s in (*STAGES, "plan")]
    s = checkpoint.skew(pairs, EC)
    assert s is not None
    assert (s.added, s.removed, s.reordered) == ((), (), True)


def test_no_skew_for_current_list() -> None:
    assert checkpoint.skew(checkpoint.parse(checkpoint.render(EC), EC), EC) is None


def test_tick_refuses_with_earlier_unticked() -> None:
    with pytest.raises(ValueError, match="bootstrap"):
        checkpoint.tick(checkpoint.render(EC), EC, "brainstorm")


def test_tick_refuses_stage_not_in_block() -> None:
    with pytest.raises(ValueError, match="nope"):
        checkpoint.tick(checkpoint.render(EC), EC, "nope")


def test_tick_ticks_next_stage() -> None:
    body = checkpoint.render(EC, ["bootstrap"])
    assert checkpoint.tick(body, EC, "brainstorm") == checkpoint.render(
        EC, ["bootstrap", "brainstorm"]
    )


def test_tick_already_ticked_is_noop() -> None:
    body = checkpoint.render(EC, ["bootstrap"])
    assert checkpoint.tick(body, EC, "bootstrap") == body


def test_tick_keeps_a_hand_tick_uppercase() -> None:
    body = checkpoint.render(EC).replace("- [ ] bootstrap", "- [X] bootstrap")
    out = checkpoint.tick(body, EC, "brainstorm")
    assert "- [X] bootstrap" in out
    assert "- [x] brainstorm" in out


def test_tick_malformed_raises() -> None:
    body = checkpoint.render(EC).replace("- [ ] plan\n", "- [ ] plan\nnote\n")
    with pytest.raises(checkpoint.Malformed):
        checkpoint.tick(body, EC, "bootstrap")


def test_write_preserves_rest_of_body() -> None:
    pre = "Planning: x/y#1\n\nSecurity advisory: GHSA-aaaa\n\n"
    post = "\n\nhuman text\n"
    body = pre + checkpoint.render(EC) + post
    out = checkpoint.tick(body, EC, "bootstrap")
    assert out.startswith(pre)
    assert out.endswith(post)


def test_upsert_replaces_in_place() -> None:
    pre = "a\n\n"
    post = "\n\nb\n"
    body = pre + checkpoint.render(EC) + post
    block = checkpoint.render(EC, ["bootstrap"])
    assert checkpoint.upsert(body, block, EC) == pre + block + post


@pytest.mark.parametrize(
    ("body", "expected_prefix"),
    [
        ("", ""),
        ("text\n", "text\n\n"),
        ("text", "text\n\n"),
        ("text\r\n", "text\r\n\r\n"),
    ],
)
def test_upsert_appends_after_a_blank_line(body: str, expected_prefix: str) -> None:
    block = checkpoint.render(EC)
    newline = "\r\n" if "\r\n" in body else "\n"
    assert checkpoint.upsert(body, block, EC) == (
        expected_prefix + block.replace("\n", newline) + newline
    )


def test_upsert_refuses_malformed_body() -> None:
    with pytest.raises(checkpoint.Malformed):
        checkpoint.upsert(checkpoint.begin(EC) + "\n", checkpoint.render(EC), EC)


def test_remove_round_trips_an_append() -> None:
    body = "Planning: x/y#1\n\ntext\n"
    added = checkpoint.upsert(body, checkpoint.render(EC), EC)
    assert checkpoint.remove(added, EC) == body


def test_remove_mid_body() -> None:
    body = "a\n\n" + checkpoint.render(EC) + "\n\nb\n"
    assert checkpoint.remove(body, EC) == "a\n\nb\n"


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("", "b\n", "b\n"),  # at the top: nothing before it to remove
        ("a\n", "b\n", "a\nb\n"),  # no blank line before it: the line is kept
    ],
)
def test_remove_keeps_a_non_blank_line_before(before: str, after: str, expected: str) -> None:
    body = before + checkpoint.render(EC) + "\n" + after
    assert checkpoint.remove(body, EC) == expected


def test_remove_without_block_is_unchanged() -> None:
    assert checkpoint.remove("a\n", EC) == "a\n"


def test_remove_malformed_and_duplicate_blocks() -> None:
    bad = checkpoint.render(EC).replace("- [ ] plan\n", "- [ ] plan\nnote\n")
    body = "a\n\n" + bad + "\n\n" + checkpoint.render(EC) + "\n"
    assert checkpoint.remove(body, EC) == "a\n"


def test_remove_keeps_other_workflow_block() -> None:
    other = checkpoint.render(EC).replace("epic-create", "epic-implement")
    body = "a\n\n" + other + "\n\n" + checkpoint.render(EC) + "\n"
    assert checkpoint.remove(body, EC) == "a\n\n" + other + "\n"


def test_remove_unclosed_raises() -> None:
    with pytest.raises(checkpoint.Malformed):
        checkpoint.remove("a\n" + checkpoint.begin(EC) + "\n- [ ] bootstrap\n", EC)


def test_remove_crlf() -> None:
    body = "a\r\n"
    added = checkpoint.upsert(body, checkpoint.render(EC), EC)
    assert checkpoint.remove(added, EC) == body
