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


# --- reconcile ------------------------------------------------------------------


def _recorded(*items: tuple[str, bool]) -> str:
    """A block for *items*, as an older stage list would have written it."""
    lines = [f"- [{'x' if done else ' '}] {name}" for name, done in items]
    return "\n".join([checkpoint.begin(EC), *lines, checkpoint.end(EC)])


def _without(stage: str, ticked: int) -> tuple[tuple[str, bool], ...]:
    """The current list minus *stage*, its first *ticked* stages ticked."""
    return tuple((s, i < ticked) for i, s in enumerate(s for s in STAGES if s != stage))


def test_reconcile_keeps_matching_ticks() -> None:
    body = _recorded(*_without("alignment", 3))
    out, changes = checkpoint.reconcile(body, EC, [])
    assert checkpoint.parse(out, EC) == [(s, s in STAGES[:3]) for s in STAGES]
    assert changes == [
        "kept ticked: bootstrap",
        "kept ticked: brainstorm",
        "kept ticked: spec-pushback",
        "new, unticked: alignment",
    ]


def test_reconcile_drops_removed_stage() -> None:
    body = _recorded(("bootstrap", True), ("old-stage", True), *((s, False) for s in STAGES[1:]))
    out, changes = checkpoint.reconcile(body, EC, [])
    assert checkpoint.parse(out, EC) == [(s, s == "bootstrap") for s in STAGES]
    assert changes == ["kept ticked: bootstrap", "dropped: old-stage (was ticked)"]


def test_reconcile_new_stage_unticked_by_default() -> None:
    body = _recorded(*_without("brainstorm", 4))
    out, changes = checkpoint.reconcile(body, EC, [])
    pairs = checkpoint.parse(out, EC)
    assert dict(pairs)["brainstorm"] is False
    assert checkpoint.next_stage(pairs) == "brainstorm"  # before the cursor: run it, or tick it
    assert "new, unticked: brainstorm" in changes


def test_reconcile_new_stage_ticked_when_named() -> None:
    body = _recorded(*_without("brainstorm", 4))
    out, changes = checkpoint.reconcile(body, EC, ["brainstorm"])
    pairs = checkpoint.parse(out, EC)
    assert dict(pairs)["brainstorm"] is True
    assert checkpoint.next_stage(pairs) == "plan-pushback"
    assert "new, ticked: brainstorm" in changes


@pytest.mark.parametrize("stage", ["bootstrap", "old-stage", "nope"])
def test_reconcile_tick_of_existing_stage_refused(stage: str) -> None:
    body = _recorded(("old-stage", False), *_without("alignment", 1))
    with pytest.raises(ValueError, match=r"new stages: alignment"):
        checkpoint.reconcile(body, EC, [stage])


def test_reconcile_tick_with_no_new_stage_refused() -> None:
    body = _recorded(("old-stage", False), *((s, False) for s in STAGES))
    with pytest.raises(ValueError, match=r"new stages: \(none\)"):
        checkpoint.reconcile(body, EC, ["bootstrap"])


def test_reconcile_reports_reorder() -> None:
    body = _recorded(("brainstorm", True), ("bootstrap", True), *((s, False) for s in STAGES[2:]))
    out, changes = checkpoint.reconcile(body, EC, [])
    assert checkpoint.parse(out, EC) == [(s, s in STAGES[:2]) for s in STAGES]
    assert changes == [
        "kept ticked: bootstrap",
        "kept ticked: brainstorm",
        "reordered: the stages now follow the current list",
    ]


def test_reconcile_duplicate_stage_ticked_only_in_every_copy() -> None:
    items = [(s, s == "bootstrap") for s in STAGES]
    body = _recorded(*items, ("bootstrap", False))
    out, changes = checkpoint.reconcile(body, EC, [])
    assert checkpoint.parse(out, EC) == [(s, False) for s in STAGES]
    assert changes == [
        "unticked: bootstrap (recorded 2 times, not ticked in every one)",
        "reordered: the stages now follow the current list",
    ]


def test_reconcile_without_skew_raises() -> None:
    with pytest.raises(ValueError, match="matches"):
        checkpoint.reconcile(checkpoint.render(EC, ["bootstrap"]), EC, [])


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("text\n", checkpoint.Missing),
        (checkpoint.render(EC).replace("- [ ] plan\n", "- [ ] plan\nnote\n"), checkpoint.Malformed),
    ],
)
def test_reconcile_missing_or_malformed_raises(body: str, error: type[Exception]) -> None:
    with pytest.raises(error):
        checkpoint.reconcile(body, EC, [])


def test_reconcile_preserves_rest_of_body() -> None:
    pre = "Planning: x/y#1\n\nSecurity advisory: GHSA-aaaa\n\n## Documents\n\n- spec\n\n"
    post = "\n\nhuman text\n"
    body = pre + _recorded(*_without("alignment", 2)) + post
    out, _ = checkpoint.reconcile(body, EC, [])
    assert out == pre + checkpoint.render(EC, STAGES[:2]) + post


def test_reconcile_crlf() -> None:
    body = ("a\n\n" + _recorded(*_without("alignment", 1)) + "\n\nb\n").replace("\n", "\r\n")
    out, _ = checkpoint.reconcile(body, EC, [])
    expected = "a\n\n" + checkpoint.render(EC, ["bootstrap"]) + "\n\nb\n"
    assert out == expected.replace("\n", "\r\n")
