"""Tests for the merge-review result check and ``vrg-merge-review-check``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_merge_review_check
from vergil_tooling.lib.merge_review.result import ResultError, parse_result
from vergil_tooling.lib.merge_review.schema import (
    earlier_steps,
    finding_id_re,
    step_id_prefix,
)

if TYPE_CHECKING:
    from pathlib import Path

REQUEST_ID = "3f2a9c0e5b7d41a8c6e2f0b9d3a7c514"
SECRET = "hunter2-secret-detail"


def _request(step: str = "review", **over: Any) -> dict[str, object]:
    request: dict[str, object] = {
        "schema": "merge-review-request/1",
        "request_id": REQUEST_ID,
        "step": step,
        "change_class": "code",
    }
    if step != "review":
        request["earlier_finding_ids"] = ["R1", "R2"]
    request.update(over)
    return request


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


def _wrap(body: str) -> str:
    return f"```merge-review-result\n{body}\n```\n\nR1: prose for the human.\n"


def _result(findings: list[Any] | None = None, **over: Any) -> str:
    if findings is not None:
        over["findings"] = findings
    return _wrap(json.dumps(_block(**over), indent=2))


def _render(block: dict[str, Any]) -> str:
    return _wrap(json.dumps(block, indent=2))


def _refused(text: str, request: dict[str, object] | None = None) -> str:
    with pytest.raises(ResultError) as excinfo:
        parse_result(text, request if request is not None else _request())
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
    text = _result() + "\n```merge-review-results-ish\nx\n```\n```python\ny\n```\n"
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


def _files(tmp_path: Path, result: str, request: dict[str, object] | None = None) -> list[str]:
    request_path = tmp_path / "request-review.json"
    request_path.write_text(json.dumps(request if request is not None else _request()))
    result_path = tmp_path / "result-review.md"
    result_path.write_text(result)
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
    (tmp_path / "result-review.md").write_bytes(b"```merge-review-result\n\xff\n```\n")
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
