"""Tests for the merge-review result check and ``vrg-merge-review-check``."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_merge_review_check
from vergil_tooling.lib.merge_review.result import (
    ResultError,
    check_request,
    parse_result,
)
from vergil_tooling.lib.merge_review.schema import (
    MAX_READ_LINES,
    earlier_steps,
    finding_id_re,
    is_check,
    step_id_prefix,
)

if TYPE_CHECKING:
    from pathlib import Path

REQUEST_ID = "3f2a9c0e5b7d41a8c6e2f0b9d3a7c514"
SECRET = "hunter2-secret-detail"
#: The line counts of the small diffs the helpers describe by default.
DIFF_LINES = 10
FIX_LINES = 4
#: A diff longer than one Read returns, so it must be read in parts.
LARGE = 2500


def _request(step: str = "review", **over: Any) -> dict[str, object]:
    request: dict[str, object] = {
        "schema": "merge-review-request/1",
        "request_id": REQUEST_ID,
        "step": step,
        "change_class": "code",
        "diff_lines": DIFF_LINES,
    }
    if step != "review":
        request["earlier_finding_ids"] = ["R1", "R2"]
        request["fix_diff_lines"] = FIX_LINES
    request.update(over)
    return request


def _parts(total: int, name: str) -> list[dict[str, object]]:
    """A manifest of *total* lines in parts of at most ``MAX_READ_LINES``, as built."""
    sizes = [min(MAX_READ_LINES, total - start) for start in range(0, total, MAX_READ_LINES)]
    return [
        {"path": f"/private/{name}.part-{number:02d}.patch", "lines": size}
        for number, size in enumerate(sizes, start=1)
    ]


def _finding(finding_id: object = "R1", **over: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": finding_id,
        "question": "merge-quality",
        "kind": "defect",
        "security": False,
        "location": "src/example/module.py:42",
        "summary": "The new flag is parsed but never passed to run().",
    }
    finding.update(over)
    return finding


def _block(**over: Any) -> dict[str, Any]:
    block: dict[str, Any] = {
        "schema": "merge-review-result/1",
        "step": "review",
        "request_id": REQUEST_ID,
        "findings": [_finding()],
    }
    block.update(over)
    return block


def _check_block(**over: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "step": "check",
        "findings": [],
        "fix_status": [{"id": "R1", "status": "fixed"}, {"id": "R2", "status": "not-fixed"}],
    }
    fields.update(over)
    return _block(**fields)


def _coverage(step: str = "review", *, diff: int = DIFF_LINES, fix: int = FIX_LINES) -> str:
    """The reply's closing coverage lines, through line *diff* (and *fix* in a check).

    The fix line comes before the diff line.
    """
    lines = [f"Diff read: through line {diff}"]
    if is_check(step):
        lines.insert(0, f"Fix diff read: through line {fix}")
    return "\n".join(lines)


def _wrap(body: str, step: str = "review", coverage: str | None = None) -> str:
    """A reply with *body* as its block; *coverage* defaults to a full read for *step*."""
    tail = _coverage(step) if coverage is None else coverage
    return f"```merge-review-result\n{body}\n```\n\nR1: prose for the human.\n{tail}\n"


def _result(findings: list[Any] | None = None, **over: Any) -> str:
    if findings is not None:
        over["findings"] = findings
    return _render(_block(**over))


def _render(block: dict[str, Any]) -> str:
    return _wrap(json.dumps(block, indent=2), step=str(block["step"]))


def _refused(
    text: str, request: dict[str, object] | None = None, *, patch: _Patch | None = None
) -> str:
    with pytest.raises(ResultError) as excinfo:
        parse_result(
            text,
            request if request is not None else _request(),
            patch_ends_with_newline=patch,
        )
    message = str(excinfo.value)
    assert SECRET not in message
    return message


# --- schema helpers -------------------------------------------------------


def test_step_id_prefixes() -> None:
    assert step_id_prefix("review") == "R"
    assert step_id_prefix("check") == "C"
    assert step_id_prefix("check-2") == "C2."
    assert step_id_prefix("check-9") == "C9."


@pytest.mark.parametrize("step", ["check-1", "check-10", "Review", "", "check-"])
def test_step_id_prefix_refuses_non_step(step: str) -> None:
    with pytest.raises(ValueError, match="not a merge-review step"):
        step_id_prefix(step)


def test_finding_id_re() -> None:
    assert finding_id_re("check-3").fullmatch("C3.999")
    assert not finding_id_re("check-3").fullmatch("C3.1000")
    assert not finding_id_re("review").fullmatch("R0")
    assert not finding_id_re("review").fullmatch("R01")


def test_earlier_steps() -> None:
    assert earlier_steps("review") == ()
    assert earlier_steps("check") == ("review",)
    assert earlier_steps("check-3") == ("review", "check", "check-2")


# --- a valid result -------------------------------------------------------


def test_valid_review_parses() -> None:
    r = parse_result(_result(), _request())
    assert r.step == "review" and r.findings[0].id == "R1"
    assert r.request_id == REQUEST_ID
    assert r.fix_status == {}
    assert r.findings[0].summary == "The new flag is parsed but never passed to run()."


def test_leading_whitespace_and_no_findings_accepted() -> None:
    r = parse_result("\n  \n" + _result(findings=[]), _request())
    assert r.findings == ()


def test_crlf_reply_accepted() -> None:
    text = _result().replace("\n", "\r\n")
    assert parse_result(text, _request()).findings[0].id == "R1"


def test_security_finding_without_summary_accepted() -> None:
    finding = _finding("R2", security=True)
    del finding["summary"]
    r = parse_result(_result(findings=[_finding(), finding]), _request())
    assert r.findings[1].security and r.findings[1].summary is None


def test_review_with_every_kind_accepted() -> None:
    findings = [
        _finding(f"R{n}", kind=kind)
        for n, kind in enumerate(["defect", "design-question", "out-of-scope", "pre-existing"], 1)
    ]
    r = parse_result(_result(findings=findings), _request())
    assert [f.kind for f in r.findings] == [
        "defect",
        "design-question",
        "out-of-scope",
        "pre-existing",
    ]


def test_location_path_only_accepted() -> None:
    r = parse_result(_result(findings=[_finding(location="README.md")]), _request())
    assert r.findings[0].location == "README.md"


# --- rule 1: shape --------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "prose first\n" + _result(),  # block not first
        _result() + "\n" + _result(),  # two blocks
        "```merge-review-result\n{not json\n```\n",
        "",
        "```json\n{}\n```\n",  # wrong info string
        "````merge-review-result\n{}\n````\n",  # four-backtick fence
        "```merge-review-result\n{}\n",  # never closed
        "```merge-review-result\n[]\n```\n",  # not an object
        "```merge-review-result\n```\n",  # empty
        "﻿" + _result(),  # a byte-order mark is not whitespace
    ],
)
def test_bad_shape_rejected(text: str) -> None:
    assert _refused(text).startswith("rule 1 (shape)")


@pytest.mark.parametrize(
    "second",
    ["~~~merge-review-result", "  ````merge-review-result", "```merge-review-result extra"],
)
def test_second_block_in_any_fence_rejected(second: str) -> None:
    text = _result() + f"\n{second}\n{{}}\n```\n"
    assert "second merge-review-result block" in _refused(text)


def test_other_fenced_blocks_in_prose_accepted() -> None:
    body = json.dumps(_block(), indent=2)
    text = (
        f"```merge-review-result\n{body}\n```\n\n"
        "```merge-review-results-ish\nx\n```\n```python\ny\n```\n"
        f"{_coverage()}\n"
    )
    assert parse_result(text, _request()).step == "review"


def test_duplicate_key_rejected_without_naming_it() -> None:
    body = json.dumps(_block()).replace('"schema"', f'"{SECRET}": 1, "{SECRET}"', 1)
    message = _refused(_wrap(body))
    assert "duplicate key" in message


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nan_rejected(constant: str) -> None:
    body = json.dumps(_block()).replace('"findings"', f'"x": {constant}, "findings"')
    assert "NaN or Infinity" in _refused(_wrap(body))


def test_invalid_json_message_has_position() -> None:
    assert "at line 2 column" in _refused("```merge-review-result\n{\n  oops\n}\n```\n")


# --- rule 2: fields -------------------------------------------------------


def test_unknown_top_level_field_rejected_without_naming_it() -> None:
    message = _refused(_render(_block(**{SECRET: "x"})))
    assert message.startswith("rule 2 (fields): the result block")
    assert "1 other key(s)" in message


def test_missing_top_level_field_rejected() -> None:
    block = _block()
    del block["findings"]
    assert "is missing findings" in _refused(_render(block))


def test_review_with_fix_status_rejected() -> None:
    message = _refused(_render(_block(fix_status=[])))
    assert message == "rule 9 (check steps): a review result has no fix_status"


def test_check_without_fix_status_rejected() -> None:
    block = _check_block()
    del block["fix_status"]
    assert "is missing fix_status" in _refused(_render(block), _request("check"))


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"schema": 1}, "schema must be a string"),
        ({"step": None}, "step must be a string"),
        ({"request_id": ["x"]}, "request_id must be a string"),
        ({"findings": {}}, "findings must be a list"),
    ],
)
def test_top_level_type_rejected(over: dict[str, Any], expected: str) -> None:
    message = _refused(_render(_block(**over)))
    assert message == f"rule 2 (fields): the result's {expected}"


def test_check_fix_status_not_list_rejected() -> None:
    message = _refused(_render(_check_block(fix_status={})), _request("check"))
    assert message == "rule 2 (fields): the result's fix_status must be a list"


def test_finding_not_object_rejected() -> None:
    assert "finding 1 is not a JSON object" in _refused(_result(findings=["R1"]))


def test_unknown_finding_field_rejected() -> None:
    message = _refused(_result(findings=[_finding(**{SECRET: 1})]))
    assert message.startswith("rule 2 (fields): finding 1 may carry only")


def test_missing_finding_field_rejected() -> None:
    finding = _finding()
    del finding["location"]
    assert "finding 1 is missing location" in _refused(_result(findings=[finding]))


@pytest.mark.parametrize("field", ["question", "kind", "location"])
def test_finding_string_field_type_rejected(field: str) -> None:
    message = _refused(_result(findings=[_finding(**{field: 3})]))
    assert message == f"rule 2 (fields): finding R1: {field} must be a string"


@pytest.mark.parametrize("value", [1, 0, "false", None])
def test_security_not_boolean_rejected(value: object) -> None:
    message = _refused(_result(findings=[_finding(security=value)]))
    assert "security must be true or false" in message


def test_summary_not_string_rejected() -> None:
    message = _refused(_result(findings=[_finding(summary=7)]))
    assert message == "rule 2 (fields): finding R1: summary must be a string"


def test_unknown_fix_status_field_rejected() -> None:
    entries = [{"id": "R1", "status": "fixed", "note": SECRET}, {"id": "R2", "status": "fixed"}]
    message = _refused(_render(_check_block(fix_status=entries)), _request("check"))
    assert message.startswith("rule 2 (fields): fix_status entry 1 may carry only")


def test_fix_status_entry_not_object_rejected() -> None:
    message = _refused(_render(_check_block(fix_status=["R1"])), _request("check"))
    assert "fix_status entry 1 is not a JSON object" in message


# --- rule 3: identity -----------------------------------------------------


def test_request_id_mismatch_rejected() -> None:
    message = _refused(_result(request_id="0" * 32))
    assert message == "rule 3 (identity): the result's request_id is not the request's"


def test_step_mismatch_rejected() -> None:
    message = _refused(_result(step="check"))
    assert message == "rule 3 (identity): the result's step is not the request's step (review)"


def test_schema_mismatch_rejected() -> None:
    assert _refused(_result(schema="merge-review-result/2")).startswith("rule 3 (identity)")


# --- rule 4: values -------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"kind": "bug"}, "kind"),
        ({"kind": "Defect"}, "kind"),
        ({"question": "style"}, "question"),
    ],
)
def test_unknown_kind_or_question_rejected(over: dict[str, Any], field: str) -> None:
    message = _refused(_result(findings=[_finding(**over)]))
    assert message.startswith(f"rule 4 (values): finding R1: {field} must be one of")


def test_unknown_fix_status_rejected() -> None:
    entries = [{"id": "R1", "status": "partly"}, {"id": "R2", "status": "fixed"}]
    message = _refused(_render(_check_block(fix_status=entries)), _request("check"))
    assert message.startswith("rule 4 (values): the fix_status entry for R1")


# --- rule 5: finding IDs --------------------------------------------------


@pytest.mark.parametrize(
    ("step", "bad_id"),
    [
        ("review", "C1"),  # a C ID in a review
        ("review", "R0"),
        ("review", "R1000"),
        ("review", "R01"),
        ("review", 1),
        ("check", "R1"),  # an R ID in a check
        ("check", "C2.1"),
        ("check-2", "C1"),  # a C<n> ID in check-2
        ("check-2", "C3.1"),
        ("check-2", "C2."),
    ],
)
def test_misprefixed_id_rejected(step: str, bad_id: object) -> None:
    finding = _finding(bad_id)
    over: dict[str, Any] = {"step": step, "findings": [finding]}
    block = _check_block(**over) if step != "review" else _block(**over)
    message = _refused(_render(block), _request(step))
    assert message.startswith("rule 5 (finding IDs): finding 1 has no valid id")
    assert f"step {step}" in message


def test_bad_id_is_named_by_position_only() -> None:
    message = _refused(_result(findings=[_finding(), _finding(SECRET)]))
    assert "finding 2 has no valid id" in message


def test_duplicate_id_rejected() -> None:
    message = _refused(_result(findings=[_finding(), _finding()]))
    assert message == "rule 5 (finding IDs): finding R1 appears more than once"


def test_check_2_ids_accepted() -> None:
    request = _request("check-2", earlier_finding_ids=["R1", "C1"])
    block = _check_block(
        step="check-2",
        findings=[_finding("C2.1"), _finding("C2.999", kind="design-question")],
        fix_status=[{"id": "C1", "status": "fixed"}, {"id": "R1", "status": "fixed"}],
    )
    r = parse_result(_render(block), request)
    assert [f.id for f in r.findings] == ["C2.1", "C2.999"]
    assert r.fix_status == {"C1": "fixed", "R1": "fixed"}


# --- rule 6: summaries ----------------------------------------------------


def test_security_with_summary_rejected() -> None:
    message = _refused(_result(findings=[_finding(security=True, summary=SECRET)]))
    assert message == "rule 6 (summaries): finding R1: a security finding carries no summary"


def test_non_security_without_summary_rejected() -> None:
    finding = _finding()
    del finding["summary"]
    message = _refused(_result(findings=[finding]))
    assert message.startswith("rule 6 (summaries): finding R1: a finding that is not security")


@pytest.mark.parametrize(
    ("summary", "code", "offset"),
    [
        ("abc‮def", "U+202E", 3),
        ("x — y", "U+2014", 2),
        ("line\nbreak", "U+000A", 4),
        ("tab\there", "U+0009", 3),
        ("\x7f", "U+007F", 0),
    ],
)
def test_non_ascii_summary_rejected(summary: str, code: str, offset: int) -> None:
    message = _refused(_result(findings=[_finding(summary=SECRET + summary)]))
    assert message == (
        f"rule 6 (summaries): finding R1: the summary has character {code} at offset"
        f" {len(SECRET) + offset}; only printable ASCII (U+0020 to U+007E) is allowed"
    )


def test_summary_over_300_rejected() -> None:
    message = _refused(_result(findings=[_finding(summary="x" * 301)]))
    assert message == "rule 6 (summaries): finding R1: the summary is over 300 characters"


def test_summary_of_300_accepted() -> None:
    r = parse_result(_result(findings=[_finding(summary="x" * 300)]), _request())
    assert len(r.findings[0].summary or "") == 300


@pytest.mark.parametrize("summary", ["", "   "])
def test_empty_summary_rejected(summary: str) -> None:
    message = _refused(_result(findings=[_finding(summary=summary)]))
    assert message == "rule 6 (summaries): finding R1: the summary is empty"


def test_summary_with_marker_prefix_rejected() -> None:
    message = _refused(_result(findings=[_finding(summary="see <!-- VRG-RULING x -->")]))
    assert message.startswith("rule 6 (summaries): finding R1: the summary contains")
    assert message.endswith("the vergil marker prefix at offset 4")


# --- rule 7: location -----------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("", "the location is empty"),
        ("src/a.py\n:3", "the location has character U+000A at offset 8"),
        ("src/é.py", "the location has character U+00E9 at offset 4"),
        ("<!-- vrg-ruling", "the location contains the vergil marker prefix at offset 0"),
    ],
)
def test_bad_location_rejected(location: str, expected: str) -> None:
    message = _refused(_result(findings=[_finding(location=location)]))
    assert message.startswith(f"rule 7 (location): finding R1: {expected}")


# --- rule 8: change class -------------------------------------------------


def test_docs_only_production_safety_rejected() -> None:
    message = _refused(
        _result(findings=[_finding(question="production-safety")]),
        _request(change_class="docs-only"),
    )
    assert message.startswith("rule 8 (change class): finding R1")


def test_docs_only_merge_quality_accepted() -> None:
    r = parse_result(_result(), _request(change_class="docs-only"))
    assert r.findings[0].question == "merge-quality"


def test_code_production_safety_accepted() -> None:
    r = parse_result(_result(findings=[_finding(question="production-safety")]), _request())
    assert r.findings[0].question == "production-safety"


# --- rule 9: check steps --------------------------------------------------


def test_check_valid_with_new_finding() -> None:
    block = _check_block(findings=[_finding("C1", kind="design-question")])
    r = parse_result(_render(block), _request("check"))
    assert r.step == "check"
    assert r.findings[0].id == "C1"
    assert r.fix_status == {"R1": "fixed", "R2": "not-fixed"}


def test_check_with_no_listed_ids_accepted() -> None:
    block = _check_block(fix_status=[])
    r = parse_result(_render(block), _request("check", earlier_finding_ids=[]))
    assert r.fix_status == {}


def test_check_pre_existing_rejected() -> None:
    block = _check_block(findings=[_finding("C1", kind="pre-existing")])
    message = _refused(_render(block), _request("check"))
    assert message.startswith("rule 9 (check steps): finding C1: a check never raises")


def test_check_missing_fix_status_rejected() -> None:
    block = _check_block(fix_status=[{"id": "R1", "status": "fixed"}])
    message = _refused(_render(block), _request("check"))
    assert message == "rule 9 (check steps): fix_status has no entry for R2"


@pytest.mark.parametrize("unlisted", ["R3", "R9", SECRET, 5])
def test_check_unlisted_fix_status_id_rejected(unlisted: object) -> None:
    # R3 was raised in the review but not listed (a pre-existing note or a
    # "keep" ruling); R9 was never raised.
    entries = [
        {"id": "R1", "status": "fixed"},
        {"id": "R2", "status": "fixed"},
        {"id": unlisted, "status": "fixed"},
    ]
    message = _refused(_render(_check_block(fix_status=entries)), _request("check"))
    assert message.startswith("rule 9 (check steps): fix_status entry 3 is for an ID the")


def test_check_duplicate_fix_status_rejected() -> None:
    entries = [
        {"id": "R1", "status": "fixed"},
        {"id": "R1", "status": "not-fixed"},
        {"id": "R2", "status": "fixed"},
    ]
    message = _refused(_render(_check_block(fix_status=entries)), _request("check"))
    assert message == "rule 9 (check steps): fix_status has more than one entry for R1"


# --- the request ----------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"schema": "merge-review-request/2"}, "schema must be"),
        ({"step": "check-1"}, "step must be"),
        ({"step": 3}, "step must be"),
        ({"request_id": "ABC"}, "request_id must be"),
        ({"request_id": "A" * 32}, "request_id must be"),
        ({"change_class": "mixed"}, "change_class must be"),
        ({"change_class": ["code"]}, "change_class must be"),
    ],
)
def test_bad_request_rejected(over: dict[str, Any], expected: str) -> None:
    message = _refused(_result(), _request(**over))
    assert message.startswith("the request: ") and expected in message


@pytest.mark.parametrize(
    ("earlier", "expected"),
    [
        (None, "must be a list of finding IDs"),
        ("R1", "must be a list of finding IDs"),
        (["R1", 2], "entry 2 is not a finding ID of a step before check"),
        (["C1"], "entry 1 is not a finding ID of a step before check"),
        (["R1", "R1"], "lists R1 more than once"),
    ],
)
def test_bad_earlier_finding_ids_rejected(earlier: object, expected: str) -> None:
    request = _request("check", earlier_finding_ids=earlier)
    assert expected in _refused(_render(_check_block()), request)


def test_check_2_request_may_list_check_ids() -> None:
    request = _request("check-3", earlier_finding_ids=["R1", "C1", "C2.4"])
    entries = [{"id": i, "status": "fixed"} for i in ("R1", "C1", "C2.4")]
    block = _check_block(step="check-3", fix_status=entries)
    assert parse_result(_render(block), request).fix_status == dict.fromkeys(
        ("R1", "C1", "C2.4"), "fixed"
    )


# --- the CLI --------------------------------------------------------------


def _capture_record(data: bytes, step: str = "review", **over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema": "merge-review-capture/1",
        "step": step,
        "request_id": REQUEST_ID,
        "result_sha256": hashlib.sha256(data).hexdigest(),
        "agent_id": "a1b2c3d4e5f60718",
    }
    record.update(over)
    return record


def _write_capture(tmp_path: Path, record: object, step: str = "review") -> Path:
    path = tmp_path / f"capture-{step}.json"
    path.write_text(json.dumps(record))
    return path


def _files(tmp_path: Path, result: str, request: dict[str, object] | None = None) -> list[str]:
    """Write the request, the result and the capture record the hook would write."""
    request = request if request is not None else _request()
    request_path = tmp_path / "request-review.json"
    request_path.write_text(json.dumps(request))
    result_path = tmp_path / "result-review.md"
    result_path.write_text(result)
    step = str(request["step"])
    _write_capture(tmp_path, _capture_record(result_path.read_bytes(), step), step)
    return ["--request", str(request_path), "--result", str(result_path)]


def test_cli_prints_counts(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    security = _finding("R2", security=True, kind="design-question")
    del security["summary"]
    text = _result(findings=[_finding(), security, _finding("R3", kind="pre-existing")])
    assert vrg_merge_review_check.main(_files(tmp_path, text)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "step": "review",
        "counts": {"defect": 1, "design-question": 1, "out-of-scope": 0, "pre-existing": 1},
        "security": 1,
        "fix_status": {},
    }


def test_cli_prints_check_fix_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _files(tmp_path, _render(_check_block()), _request("check"))
    assert vrg_merge_review_check.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["step"] == "check"
    assert out["fix_status"] == {"R1": "fixed", "R2": "not-fixed"}
    assert out["counts"] == dict.fromkeys(
        ("defect", "design-question", "out-of-scope", "pre-existing"), 0
    )


def test_cli_invalid_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = _result(findings=[_finding(security=True, summary=SECRET)])
    assert vrg_merge_review_check.main(_files(tmp_path, text)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR: rule 6 (summaries)")
    assert SECRET not in captured.err


def test_cli_missing_result_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _files(tmp_path, _result())
    (tmp_path / "result-review.md").unlink()
    assert vrg_merge_review_check.main(args) == 1
    assert capsys.readouterr().err.startswith("ERROR: cannot read the result file")


def test_cli_result_not_utf8(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = _files(tmp_path, "")
    data = b"```merge-review-result\n\xff\n```\n"
    (tmp_path / "result-review.md").write_bytes(data)
    _write_capture(tmp_path, _capture_record(data))
    assert vrg_merge_review_check.main(args) == 1
    assert "is not UTF-8 at byte offset 23" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("content", "expected"),
    [("{oops", "is not valid JSON"), ("[]", "must hold a JSON object")],
)
def test_cli_bad_request_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, expected: str
) -> None:
    args = _files(tmp_path, _result())
    (tmp_path / "request-review.json").write_text(content)
    assert vrg_merge_review_check.main(args) == 1
    err = capsys.readouterr().err
    assert err.startswith("ERROR: the request file") and expected in err


def test_cli_requires_both_paths(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        vrg_merge_review_check.main(["--request", "x"])
    assert excinfo.value.code == 2
    assert "--result" in capsys.readouterr().err


# --- rule 10: the capture record ------------------------------------------


def _capture_refused(capsys: pytest.CaptureFixture[str], args: list[str]) -> str:
    assert vrg_merge_review_check.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR: rule 10 (capture): ")
    assert SECRET not in captured.err
    return captured.err


def test_capture_passes_with_its_record(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_merge_review_check.main(_files(tmp_path, _result())) == 0
    assert json.loads(capsys.readouterr().out)["step"] == "review"


def test_capture_is_read_from_the_result_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The request lives elsewhere; the record is found beside the result.
    private = tmp_path / "private"
    private.mkdir()
    args = _files(private, _result())
    elsewhere = tmp_path / "request-review.json"
    (private / "request-review.json").rename(elsewhere)
    args[1] = str(elsewhere)
    assert vrg_merge_review_check.main(args) == 0
    assert json.loads(capsys.readouterr().out)["step"] == "review"


def test_capture_one_byte_changed_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    result_path = tmp_path / "result-review.md"
    result_path.write_bytes(result_path.read_bytes() + b" ")
    err = _capture_refused(capsys, args)
    assert "the result was changed after the capture" in err


def test_capture_tidied_result_still_valid_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A retyped result that would pass every other rule is still refused.
    args = _files(tmp_path, _result())
    (tmp_path / "result-review.md").write_text(
        _result(findings=[_finding(summary="A tidier summary.")])
    )
    _capture_refused(capsys, args)


def test_capture_missing_record_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    (tmp_path / "capture-review.json").unlink()
    assert "the result has no capture record" in _capture_refused(capsys, args)


def test_capture_unreadable_record_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    record = tmp_path / "capture-review.json"
    record.unlink()
    record.mkdir()
    assert "cannot read the capture record" in _capture_refused(capsys, args)


def test_capture_record_not_utf8_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    (tmp_path / "capture-review.json").write_bytes(b'{"schema": "\xff"}')
    assert "is not UTF-8 at byte offset 12" in _capture_refused(capsys, args)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("{oops", "is not valid JSON"),
        ('{"schema": NaN}', "uses NaN or Infinity"),
        ('{"schema": "a", "schema": "b"}', "has an object with a duplicate key"),
        ("[]", "must hold one JSON object"),
        ('"merge-review-capture/1"', "must hold one JSON object"),
    ],
)
def test_capture_record_not_a_json_object_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], content: str, expected: str
) -> None:
    args = _files(tmp_path, _result())
    (tmp_path / "capture-review.json").write_text(content)
    assert expected in _capture_refused(capsys, args)


def test_capture_record_extra_key_is_refused_unnamed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    record = _capture_record((tmp_path / "result-review.md").read_bytes())
    record[SECRET] = "x"
    _write_capture(tmp_path, record)
    assert "it has 1 other key(s)" in _capture_refused(capsys, args)


@pytest.mark.parametrize("field", ["schema", "step", "request_id", "result_sha256", "agent_id"])
def test_capture_record_missing_field_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str
) -> None:
    args = _files(tmp_path, _result())
    record = _capture_record((tmp_path / "result-review.md").read_bytes())
    del record[field]
    _write_capture(tmp_path, record)
    assert f"the capture record is missing {field}" in _capture_refused(capsys, args)


@pytest.mark.parametrize("field", ["schema", "step", "request_id", "result_sha256", "agent_id"])
@pytest.mark.parametrize("value", [None, 1, [], {}, ""])
def test_capture_record_field_not_a_string_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], field: str, value: object
) -> None:
    args = _files(tmp_path, _result())
    record = _capture_record((tmp_path / "result-review.md").read_bytes())
    record[field] = value
    _write_capture(tmp_path, record)
    err = _capture_refused(capsys, args)
    assert f"the capture record's {field} must be a non-empty string" in err


def test_capture_record_other_schema_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    data = (tmp_path / "result-review.md").read_bytes()
    _write_capture(tmp_path, _capture_record(data, schema="merge-review-capture/2"))
    err = _capture_refused(capsys, args)
    assert "schema must be merge-review-capture/1" in err
    assert "capture/2" not in err


def test_capture_record_other_step_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    data = (tmp_path / "result-review.md").read_bytes()
    _write_capture(tmp_path, _capture_record(data, step="check"))
    err = _capture_refused(capsys, args)
    assert "step is not the request's step (review)" in err


def test_capture_record_of_another_steps_file_is_not_used(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A check's request is answered by capture-check.json, never capture-review.json.
    args = _files(tmp_path, _render(_check_block()), _request("check"))
    record = tmp_path / "capture-check.json"
    record.rename(tmp_path / "capture-review.json")
    assert "the result has no capture record" in _capture_refused(capsys, args)


def test_capture_record_other_request_id_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    data = (tmp_path / "result-review.md").read_bytes()
    other = "0" * 32
    _write_capture(tmp_path, _capture_record(data, request_id=other))
    err = _capture_refused(capsys, args)
    assert "request_id is not the request's" in err
    assert other not in err
    assert REQUEST_ID not in err


@pytest.mark.parametrize(
    "digest",
    ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "\n", "sha256:" + "a" * 57],
)
def test_capture_record_malformed_digest_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], digest: str
) -> None:
    args = _files(tmp_path, _result())
    data = (tmp_path / "result-review.md").read_bytes()
    _write_capture(tmp_path, _capture_record(data, result_sha256=digest))
    err = _capture_refused(capsys, args)
    assert "result_sha256 must be 64 lowercase hex characters" in err


def test_capture_record_uppercase_of_the_right_digest_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _result())
    data = (tmp_path / "result-review.md").read_bytes()
    digest = hashlib.sha256(data).hexdigest().upper()
    _write_capture(tmp_path, _capture_record(data, result_sha256=digest))
    _capture_refused(capsys, args)


def test_capture_is_checked_before_the_result_rules(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A result broken after the capture is named as a capture failure.
    args = _files(tmp_path, _result())
    (tmp_path / "result-review.md").write_text("not a result at all")
    _capture_refused(capsys, args)


def test_capture_bad_request_is_refused_before_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A malformed request step never names a capture file.
    args = _files(tmp_path, _result())
    (tmp_path / "request-review.json").write_text(json.dumps(_request(step="../x")))
    assert vrg_merge_review_check.main(args) == 1
    assert capsys.readouterr().err.startswith("ERROR: the request: step must be")


# --- rule 11: coverage ----------------------------------------------------


def _large_review(**over: Any) -> dict[str, object]:
    """A review of a diff of ``LARGE`` lines, which carries its parts; *over* replaces fields."""
    fields: dict[str, Any] = {
        "diff_lines": LARGE,
        "diff_parts": _parts(LARGE, "diff-review"),
    }
    fields.update(over)
    return _request(**fields)


def _large_check(**over: Any) -> dict[str, object]:
    """A check of a ``LARGE``-line diff whose fix diff is ``LARGE`` lines, both in parts."""
    fields: dict[str, Any] = {
        "diff_lines": LARGE,
        "diff_parts": _parts(LARGE, "diff-check"),
        "fix_diff_lines": LARGE,
        "fix_diff_parts": _parts(LARGE, "fix-check"),
    }
    fields.update(over)
    return _request("check", **fields)


def _bad_request(request: dict[str, object], expected: str) -> None:
    with pytest.raises(ResultError) as excinfo:
        check_request(request)
    message = str(excinfo.value)
    assert message.startswith("the request: ") and expected in message


class _Patch:
    """Stands in for the patch check: records each call and answers *ends* for each."""

    def __init__(self, ends: bool) -> None:
        self.ends = ends
        self.calls: list[str] = []

    def __call__(self, which: str) -> bool:
        self.calls.append(which)
        return self.ends


def _reply(coverage: str, step: str = "review") -> str:
    """A reply whose block is the default block for *step*, closed by *coverage*."""
    body = _check_block() if is_check(step) else _block()
    return _wrap(json.dumps(body, indent=2), step=step, coverage=coverage)


def test_through_line_full_read_of_a_large_diff_accepted() -> None:
    checked = parse_result(_reply(f"Diff read: through line {LARGE}"), _large_review())
    assert checked.step == "review"
    assert checked.normalised == ()


def test_through_line_full_read_of_a_small_diff_accepted_with_trailing_space_and_cr() -> None:
    patch = _Patch(ends=True)
    checked = parse_result(
        _reply(f"Diff read: through line {DIFF_LINES} \r"),
        _request(),
        patch_ends_with_newline=patch,
    )
    # N is read as it stands: the patch is not looked at.
    assert checked.normalised == ()
    assert patch.calls == []


def test_old_form_full_read_of_a_small_diff_accepted_with_trailing_space_and_cr() -> None:
    line = f"Diff read: {DIFF_LINES} of {DIFF_LINES} lines \r"
    assert parse_result(_reply(line), _request()).normalised == ()


def test_old_form_total_is_never_consulted() -> None:
    # The read, 2500, is judged against N = 2500; the stated 2600 total is ignored.
    checked = parse_result(_reply(f"Diff read: {LARGE} of 2600 lines"), _large_review())
    assert checked.normalised == ()


def test_old_form_80_of_80_on_a_114_line_diff_refused() -> None:
    # A reply that stopped early and gave its own total: the total does not make it whole.
    message = _refused(_reply("Diff read: 80 of 80 lines"), _request(diff_lines=114))
    assert message == (
        "rule 11 (coverage): the Diff read line reads through line 80, but the diff has"
        " 114 lines: the whole diff was not read"
    )


def test_through_line_stopped_early_refused() -> None:
    message = _refused(_reply("Diff read: through line 80"), _request(diff_lines=114))
    assert message == (
        "rule 11 (coverage): the Diff read line reads through line 80, but the diff has"
        " 114 lines: the whole diff was not read"
    )


def test_through_line_n_accepted_without_looking_at_the_patch() -> None:
    patch = _Patch(ends=True)
    checked = parse_result(
        _reply(f"Diff read: through line {DIFF_LINES}"), _request(), patch_ends_with_newline=patch
    )
    assert checked.normalised == ()
    assert patch.calls == []


def test_through_line_n_plus_one_normalised_on_a_newline_ended_patch() -> None:
    patch = _Patch(ends=True)
    checked = parse_result(
        _reply(f"Diff read: through line {DIFF_LINES + 1}"),
        _request(),
        patch_ends_with_newline=patch,
    )
    assert checked.normalised == ("Diff read",)
    assert patch.calls == ["diff"]


def test_old_form_368_of_368_normalised_on_a_newline_ended_patch() -> None:
    # 368 is N+1 for a 367-line diff whose patch ends in a newline: the phantom line.
    patch = _Patch(ends=True)
    checked = parse_result(
        _reply("Diff read: 368 of 368 lines"),
        _request(diff_lines=367),
        patch_ends_with_newline=patch,
    )
    assert checked.normalised == ("Diff read",)
    assert patch.calls == ["diff"]


def test_old_form_368_of_368_refused_on_a_patch_without_a_final_newline() -> None:
    message = _refused(
        _reply("Diff read: 368 of 368 lines"),
        _request(diff_lines=367),
        patch=_Patch(ends=False),
    )
    assert message == (
        "rule 11 (coverage): the Diff read line reads through line 368, one past the 367"
        " lines of the diff, and the diff does not end in a newline: its last line is 367"
    )


def test_old_form_368_of_368_refused_without_a_patch_check() -> None:
    message = _refused(_reply("Diff read: 368 of 368 lines"), _request(diff_lines=367))
    assert message == (
        "rule 11 (coverage): the Diff read line reads through line 368, one past the 367"
        " lines of the diff, and the diff cannot be checked: its last line is 367"
    )


def test_through_line_n_plus_one_refused_on_a_patch_without_a_final_newline() -> None:
    message = _refused(_reply(f"Diff read: through line {DIFF_LINES + 1}"), patch=_Patch(False))
    assert message == (
        "rule 11 (coverage): the Diff read line reads through line 11, one past the 10"
        " lines of the diff, and the diff does not end in a newline: its last line is 10"
    )


def test_through_line_n_plus_one_refused_without_a_patch_check() -> None:
    message = _refused(_reply(f"Diff read: through line {DIFF_LINES + 1}"))
    assert message == (
        "rule 11 (coverage): the Diff read line reads through line 11, one past the 10"
        " lines of the diff, and the diff cannot be checked: its last line is 10"
    )


@pytest.mark.parametrize(
    ("read", "expected"),
    [
        (9, "reads through line 9, but the diff has 10 lines: the whole diff was not read"),
        (0, "reads through line 0, but the diff has 10 lines: the whole diff was not read"),
        (12, "reads through line 12, past the 10 lines of the diff"),
        (2500, "reads through line 2500, past the 10 lines of the diff"),
    ],
)
def test_through_line_neither_n_nor_n_plus_one_refused(read: int, expected: str) -> None:
    # The patch is never consulted for a line that is not N+1.
    patch = _Patch(ends=True)
    message = _refused(_reply(f"Diff read: through line {read}"), patch=patch)
    assert message.startswith("rule 11 (coverage): the Diff read line ")
    assert expected in message
    assert patch.calls == []


def test_check_fix_line_n_plus_one_normalised_alone() -> None:
    patch = _Patch(ends=True)
    coverage = f"Fix diff read: through line {FIX_LINES + 1}\nDiff read: through line {DIFF_LINES}"
    checked = parse_result(
        _reply(coverage, "check"), _request("check"), patch_ends_with_newline=patch
    )
    assert checked.normalised == ("Fix diff read",)
    assert patch.calls == ["fix"]


def test_check_both_lines_n_plus_one_normalised() -> None:
    patch = _Patch(ends=True)
    coverage = (
        f"Fix diff read: through line {FIX_LINES + 1}\nDiff read: through line {DIFF_LINES + 1}"
    )
    checked = parse_result(
        _reply(coverage, "check"), _request("check"), patch_ends_with_newline=patch
    )
    assert checked.normalised == ("Diff read", "Fix diff read")
    assert patch.calls == ["diff", "fix"]


def test_check_fix_line_n_plus_one_refused_on_a_patch_without_a_final_newline() -> None:
    coverage = f"Fix diff read: through line {FIX_LINES + 1}\nDiff read: through line {DIFF_LINES}"
    message = _refused(_reply(coverage, "check"), _request("check"), patch=_Patch(ends=False))
    assert message == (
        "rule 11 (coverage): the Fix diff read line reads through line 5, one past the 4"
        " lines of the diff, and the diff does not end in a newline: its last line is 4"
    )


def test_check_fix_line_n_plus_one_refused_without_a_patch_check() -> None:
    coverage = f"Fix diff read: through line {FIX_LINES + 1}\nDiff read: through line {DIFF_LINES}"
    message = _refused(_reply(coverage, "check"), _request("check"))
    assert message == (
        "rule 11 (coverage): the Fix diff read line reads through line 5, one past the 4"
        " lines of the diff, and the diff cannot be checked: its last line is 4"
    )


def test_check_old_form_totals_are_never_consulted() -> None:
    coverage = "Fix diff read: 4 of 9 lines\nDiff read: 10 of 2 lines"
    checked = parse_result(_reply(coverage, "check"), _request("check"))
    assert checked.normalised == ()


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        (
            "Fix diff read: through line 3\nDiff read: through line 10",
            "the Fix diff read line reads through line 3, but the diff has 4 lines",
        ),
        (
            "Fix diff read: 3 of 4 lines\nDiff read: through line 10",
            "the Fix diff read line reads through line 3, but the diff has 4 lines",
        ),
        (
            "Fix diff read: through line 6\nDiff read: through line 10",
            "the Fix diff read line reads through line 6, past the 4 lines of the diff",
        ),
        (
            "Fix diff read: through line 4\nDiff read: through line 9",
            "the Diff read line reads through line 9, but the diff has 10 lines",
        ),
        (
            "Fix diff read: through line 4\nDiff read: through line 12",
            "the Diff read line reads through line 12, past the 10 lines of the diff",
        ),
        (
            "Diff read: through line 10",
            "the non-empty line before the last must read 'Fix diff read:",
        ),
        (
            "Fix diff read: through line 4\nafter\nDiff read: through line 10",
            "the non-empty line before the last must read 'Fix diff read:",
        ),
        (
            "Fix diff read: through line 4\nDiff read: through line 10\nafter",
            "the last non-empty line must read 'Diff read:",
        ),
    ],
)
def test_check_short_or_missing_coverage_refused(coverage: str, expected: str) -> None:
    message = _refused(_reply(coverage, "check"), _request("check"), patch=_Patch(ends=True))
    assert message.startswith("rule 11 (coverage): ") and expected in message


@pytest.mark.parametrize(
    "line",
    [
        "Diff read: through line 08",
        "Diff read: through line",
        "Diff read: Through line 5",
        "Diff read: through line 5 and more",
        "Diff read: through line 5.",
        "Diff read: through line +5",
        "Diff read: through line -1",
        "Diff read: through  line 5",
        "Diff read: through line 10 lines",
        "Diff read: through line 10 of 10 lines",
        "diff read: through line 10",
        "Diff read:",
    ],
)
def test_malformed_through_line_refused(line: str) -> None:
    message = _refused(_reply(line), patch=_Patch(ends=True))
    assert message == (
        "rule 11 (coverage): the last non-empty line must read 'Diff read: through line <n>'"
    )


@pytest.mark.parametrize(
    "line",
    [
        "Fix diff read: through line 04",
        "Fix diff read: through line",
        "Fix diff read: Through line 4",
        "Fix diff read: through line 4 lines",
        "fix diff read: through line 4",
    ],
)
def test_check_malformed_fix_line_refused(line: str) -> None:
    coverage = f"{line}\nDiff read: through line {DIFF_LINES}"
    message = _refused(_reply(coverage, "check"), _request("check"), patch=_Patch(ends=True))
    assert message == (
        "rule 11 (coverage): the non-empty line before the last must read"
        " 'Fix diff read: through line <n>'"
    )


def test_check_diff_line_is_judged_before_the_fix_line() -> None:
    # A bad diff line is refused whatever the fix line says.
    coverage = "Fix diff read: through line 4\nDiff read: through line 2"
    message = _refused(_reply(coverage, "check"), _request("check"), patch=_Patch(ends=True))
    assert "the Diff read line reads through line 2, but the diff has 10 lines" in message


@pytest.mark.parametrize(
    ("coverage", "expected"),
    [
        ("Diff read: through line 2000", "reads through line 2000, but the diff has 2500"),
        ("Diff read: through line 0", "reads through line 0, but the diff has 2500"),
        ("Diff read: 2000 of 2500 lines", "reads through line 2000, but the diff has 2500"),
        ("Diff read: 2000 of 2000 lines", "reads through line 2000, but the diff has 2500"),
        ("Diff read: through line 2502", "reads through line 2502, past the 2500 lines"),
        ("", "the last non-empty line must read 'Diff read:"),
        ("Diff read: 2500 lines", "the last non-empty line must read 'Diff read:"),
        ("Diff read: +2500 of 2500 lines", "the last non-empty line must read 'Diff read:"),
        ("Diff read: 02500 of 2500 lines", "the last non-empty line must read 'Diff read:"),
        ("Diff read: 2500 of 2500 lines.", "the last non-empty line must read 'Diff read:"),
        ("diff read: 2500 of 2500 lines", "the last non-empty line must read 'Diff read:"),
        ("Diff read: through line 02500", "the last non-empty line must read 'Diff read:"),
        ("Diff read: through line 2500.", "the last non-empty line must read 'Diff read:"),
        (
            f"Diff read: through line {LARGE}\nAfterthought.",
            "the last non-empty line must read 'Diff read:",
        ),
    ],
)
def test_short_missing_or_malformed_diff_read_refused(coverage: str, expected: str) -> None:
    message = _refused(_reply(coverage), _large_review())
    assert message.startswith("rule 11 (coverage): ") and expected in message


def test_large_phantom_line_judged_against_the_large_count() -> None:
    patch = _Patch(ends=True)
    checked = parse_result(
        _reply(f"Diff read: through line {LARGE + 1}"),
        _large_review(),
        patch_ends_with_newline=patch,
    )
    assert checked.normalised == ("Diff read",)


def test_small_short_read_refused() -> None:
    message = _refused(_reply("Diff read: through line 9"))
    assert message == (
        "rule 11 (coverage): the Diff read line reads through line 9, but the diff has 10"
        " lines: the whole diff was not read"
    )


def test_full_read_of_a_large_fix_diff_accepted() -> None:
    coverage = f"Fix diff read: through line {LARGE}\nDiff read: through line {LARGE}"
    checked = parse_result(_reply(coverage, "check"), _large_check())
    assert checked.fix_status == {"R1": "fixed", "R2": "not-fixed"}
    assert checked.normalised == ()


def test_check_small_fix_short_read_refused() -> None:
    coverage = "Fix diff read: through line 3\nDiff read: through line 10"
    message = _refused(_reply(coverage, "check"), _request("check"))
    assert "the Fix diff read line reads through line 3, but the diff has 4 lines" in message


def test_check_full_small_read_accepted() -> None:
    assert parse_result(_render(_check_block()), _request("check")).step == "check"


def test_check_coverage_must_follow_the_prose_line() -> None:
    # The lines are read from the bottom: a Fix line above prose is not the one before the last.
    coverage = "Fix diff read: through line 4\nprose\nDiff read: through line 10"
    message = _refused(_reply(coverage, "check"), _request("check"))
    assert "the non-empty line before the last must read 'Fix diff read:" in message


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"drop": ("diff_lines",)}, "diff_lines must be a whole number of lines"),
        ({"diff_lines": "10"}, "diff_lines must be a whole number of lines"),
        ({"diff_lines": True}, "diff_lines must be a whole number of lines"),
        ({"diff_lines": -1}, "diff_lines must be a whole number of lines"),
        ({"diff_lines": 10.0}, "diff_lines must be a whole number of lines"),
        ({"diff_lines": LARGE}, "diff_parts must list the parts of a diff over 2000 lines"),
        ({"diff_lines": 10, "diff_parts": []}, "diff_parts is present, but the diff is not over"),
        (
            {"diff_lines": LARGE, "diff_parts": _parts(2000, "diff-review")},
            "diff_parts lines sum to 2000, not the 2500 lines of the diff",
        ),
        (
            {
                "diff_lines": LARGE,
                "diff_parts": [{"path": "/p/1", "lines": 2001}, {"path": "/p/2", "lines": 499}],
            },
            "diff_parts entry 1 must hold from 1 to 2000 lines",
        ),
        (
            {
                "diff_lines": LARGE,
                "diff_parts": [{"path": "/p/1", "lines": 0}, {"path": "/p/2", "lines": 2500}],
            },
            "diff_parts entry 1 must hold from 1 to 2000 lines",
        ),
        (
            {"diff_lines": LARGE, "diff_parts": [{"path": "/p/1", "lines": "2000"}]},
            "diff_parts entry 1 must hold a path and a line count",
        ),
        (
            {"diff_lines": LARGE, "diff_parts": [{"path": 1, "lines": 2500}]},
            "diff_parts entry 1 must hold a path and a line count",
        ),
        (
            {"diff_lines": LARGE, "diff_parts": [{"path": "/p/1", "lines": True}]},
            "diff_parts entry 1 must hold a path and a line count",
        ),
        (
            {"diff_lines": LARGE, "diff_parts": [{"path": "/p/1", "lines": 2500, "x": 1}]},
            "diff_parts entry 1 must hold exactly path and lines",
        ),
        (
            {"diff_lines": LARGE, "diff_parts": ["/p/1"]},
            "diff_parts entry 1 must hold exactly path and lines",
        ),
        ({"diff_lines": LARGE, "diff_parts": {}}, "must list the parts of a diff over 2000"),
        ({"diff_lines": LARGE, "diff_parts": []}, "must list the parts of a diff over 2000"),
    ],
)
def test_bad_review_line_count_or_manifest_refused(over: dict[str, Any], expected: str) -> None:
    drop = over.pop("drop", ())
    request = _request(**over)
    for key in drop:
        del request[key]
    _bad_request(request, expected)


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"fix_diff_lines": None}, "fix_diff_lines must be a whole number of lines"),
        ({"fix_diff_lines": "4"}, "fix_diff_lines must be a whole number of lines"),
        ({"fix_diff_lines": -4}, "fix_diff_lines must be a whole number of lines"),
        ({"fix_diff_lines": LARGE}, "fix_diff_parts must list the parts of a diff over 2000"),
        (
            {"fix_diff_lines": 4, "fix_diff_parts": [{"path": "/p/f", "lines": 4}]},
            "fix_diff_parts is present, but the diff is not over 2000 lines",
        ),
        (
            {
                "fix_diff_lines": LARGE,
                "fix_diff_parts": [{"path": "/p/1", "lines": 2000}, {"path": "/p/2", "lines": 400}],
            },
            "fix_diff_parts lines sum to 2400, not the 2500 lines of the diff",
        ),
        (
            {
                "fix_diff_lines": LARGE,
                "fix_diff_parts": [{"path": "/p/1", "lines": 2500}, {"path": "/p/2", "lines": 0}],
            },
            "fix_diff_parts entry 1 must hold from 1 to 2000 lines",
        ),
    ],
)
def test_bad_check_fix_line_count_or_manifest_refused(over: dict[str, Any], expected: str) -> None:
    request = _request("check", **over)
    _bad_request(request, expected)


def test_check_without_fix_diff_lines_refused() -> None:
    request = _request("check")
    del request["fix_diff_lines"]
    _bad_request(request, "fix_diff_lines must be a whole number of lines")


def test_review_needs_no_fix_diff_lines() -> None:
    # A review has no fix diff, so its request needs no fix_diff_lines.
    request = _request()
    assert check_request(request).fix_diff_lines is None


def test_valid_manifests_accepted_by_check_request() -> None:
    parsed = check_request(_large_check())
    assert (parsed.diff_lines, parsed.fix_diff_lines) == (LARGE, LARGE)
    split = [{"path": "/p/1", "lines": 1000}, {"path": "/p/2", "lines": 1500}]
    assert check_request(_large_review(diff_parts=split)).diff_lines == LARGE


def test_cli_short_read_of_a_large_diff_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _reply("Diff read: through line 2000")
    assert vrg_merge_review_check.main(_files(tmp_path, text, _large_review())) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "ERROR: rule 11 (coverage): the Diff read line reads through line 2000,"
        " but the diff has 2500 lines: the whole diff was not read\n"
    )


def test_cli_full_read_of_a_large_diff_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = _reply(f"Diff read: through line {LARGE}")
    assert vrg_merge_review_check.main(_files(tmp_path, text, _large_review())) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["step"] == "review"
    assert "count_normalised" not in out


def test_cli_short_read_of_a_large_fix_diff_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    coverage = f"Fix diff read: through line 2000\nDiff read: through line {LARGE}"
    text = _reply(coverage, "check")
    assert vrg_merge_review_check.main(_files(tmp_path, text, _large_check())) == 1
    err = capsys.readouterr().err
    assert err.startswith(
        "ERROR: rule 11 (coverage): the Fix diff read line reads through line 2000"
    )


def test_cli_full_read_of_a_large_fix_diff_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    coverage = f"Fix diff read: through line {LARGE}\nDiff read: through line {LARGE}"
    text = _reply(coverage, "check")
    assert vrg_merge_review_check.main(_files(tmp_path, text, _large_check())) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fix_status"] == {"R1": "fixed", "R2": "not-fixed"}
    assert "count_normalised" not in out


def test_cli_no_patch_is_read_unless_a_count_reads_one_past(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # No patch file exists: a reply at N never asks for one.
    args = _files(tmp_path, _reply(f"Diff read: through line {DIFF_LINES}"))
    assert vrg_merge_review_check.main(args) == 0
    assert "count_normalised" not in json.loads(capsys.readouterr().out)


def test_cli_phantom_line_normalised_on_a_newline_ended_patch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _reply(f"Diff read: through line {DIFF_LINES + 1}"))
    _write_patch(tmp_path, "diff-review.patch", b"+x\n" * DIFF_LINES)
    assert vrg_merge_review_check.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count_normalised"] == "Diff read"
    assert out["step"] == "review"


def test_cli_old_form_368_of_368_normalised_against_367(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _reply("Diff read: 368 of 368 lines"), _request(diff_lines=367))
    _write_patch(tmp_path, "diff-review.patch", b"\n" * 367)
    assert vrg_merge_review_check.main(args) == 0
    assert json.loads(capsys.readouterr().out)["count_normalised"] == "Diff read"


def test_cli_old_form_368_of_368_refused_on_a_patch_without_a_final_newline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _reply("Diff read: 368 of 368 lines"), _request(diff_lines=367))
    _write_patch(tmp_path, "diff-review.patch", b"\n" * 366 + b"x")
    assert vrg_merge_review_check.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(
        "ERROR: rule 11 (coverage): the Diff read line reads through line 368, one past the 367"
    )


def test_cli_old_form_80_of_80_on_a_114_line_diff_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _reply("Diff read: 80 of 80 lines"), _request(diff_lines=114))
    assert vrg_merge_review_check.main(args) == 1
    assert capsys.readouterr().err.startswith(
        "ERROR: rule 11 (coverage): the Diff read line reads through line 80, but the diff has 114"
    )


def test_cli_phantom_line_on_a_patch_without_a_final_newline_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _reply(f"Diff read: through line {DIFF_LINES + 1}"))
    _write_patch(tmp_path, "diff-review.patch", b"\n" * (DIFF_LINES - 1) + b"x")
    assert vrg_merge_review_check.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith(
        "ERROR: rule 11 (coverage): the Diff read line reads through line 11, one past the 10"
        " lines of the diff, and the diff does not end in a newline"
    )


def test_cli_empty_patch_does_not_end_in_a_newline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _reply(f"Diff read: through line {DIFF_LINES + 1}"))
    _write_patch(tmp_path, "diff-review.patch", b"")
    assert vrg_merge_review_check.main(args) == 1
    assert "does not end in a newline" in capsys.readouterr().err


def test_cli_missing_patch_for_a_phantom_line_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _files(tmp_path, _reply(f"Diff read: through line {DIFF_LINES + 1}"))
    assert vrg_merge_review_check.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR: cannot read the diff patch file ")
    assert "diff-review.patch" in captured.err


def test_cli_both_counts_normalised_summarised_as_both(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    coverage = (
        f"Fix diff read: through line {FIX_LINES + 1}\nDiff read: through line {DIFF_LINES + 1}"
    )
    args = _files(tmp_path, _reply(coverage, "check"), _request("check"))
    _write_patch(tmp_path, "diff-check.patch", b"\n" * DIFF_LINES)
    _write_patch(tmp_path, "fix-check.patch", b"\n" * FIX_LINES)
    assert vrg_merge_review_check.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count_normalised"] == "both"
    assert out["fix_status"] == {"R1": "fixed", "R2": "not-fixed"}


def test_cli_fix_count_normalised_alone_summarised(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    coverage = f"Fix diff read: through line {FIX_LINES + 1}\nDiff read: through line {DIFF_LINES}"
    args = _files(tmp_path, _reply(coverage, "check"), _request("check"))
    _write_patch(tmp_path, "fix-check.patch", b"\n" * FIX_LINES)
    assert vrg_merge_review_check.main(args) == 0
    assert json.loads(capsys.readouterr().out)["count_normalised"] == "Fix diff read"


def test_cli_patch_is_read_from_the_result_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The request lives elsewhere; the patch is found beside the result, not beside the request.
    private = tmp_path / "private"
    private.mkdir()
    args = _files(private, _reply(f"Diff read: through line {DIFF_LINES + 1}"))
    elsewhere = tmp_path / "request-review.json"
    (private / "request-review.json").rename(elsewhere)
    args[1] = str(elsewhere)
    _write_patch(tmp_path, "diff-review.patch", b"\n" * DIFF_LINES)
    assert vrg_merge_review_check.main(args) == 1
    assert capsys.readouterr().err.startswith("ERROR: cannot read the diff patch file ")
    _write_patch(private, "diff-review.patch", b"\n" * DIFF_LINES)
    assert vrg_merge_review_check.main(args) == 0
    assert json.loads(capsys.readouterr().out)["count_normalised"] == "Diff read"


def test_summarize_omits_the_key_when_nothing_normalised() -> None:
    checked = parse_result(_reply(f"Diff read: through line {DIFF_LINES}"), _request())
    assert "count_normalised" not in vrg_merge_review_check.summarize(checked)


def _write_patch(directory: Path, name: str, data: bytes) -> None:
    (directory / name).write_bytes(data)
