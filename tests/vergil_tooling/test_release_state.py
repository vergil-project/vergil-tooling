"""Tests for vergil_tooling.lib.release.state (spec §4.2, epic vergil-project/.github#1440)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from vergil_tooling.lib.release import checklist
from vergil_tooling.lib.release.state import (
    BEGIN,
    END,
    FailedJob,
    IssueEntry,
    StateBlock,
    StateBlockError,
    initial_state,
    parse_state,
    render_state,
    upsert_state,
)

_CUT = "c" * 40


def _full_state() -> StateBlock:
    """A state with every field set away from its initial value."""
    return StateBlock(
        version="2.1.340",
        version_bump="minor",
        cut=_CUT,
        issues=(
            IssueEntry(ref="owner/repo#12", prs=(30,)),
            IssueEntry(ref="owner/repo#13", prs=(31, 32)),
        ),
        release_pr="https://github.com/owner/repo/pull/40",
        bump_pr="https://github.com/owner/repo/pull/41",
        tag="v2.1.340",
        tag_commit="d" * 40,
        main_cd_run="111",
        develop_cd_run="222",
        failed_jobs=(FailedJob(branch="main", run_id="111", job="docker-publish"),),
        index_listed=True,
        without_deps=True,
        done=("prepare", "merge-release"),
        install_done=True,
        install_version="2.1.340",
        install_commit="f" * 40,
        records_posted=("owner/repo#12",),
        supersedes="2.1.339",
        absorbed=("owner/repo#12",),
        superseded_main_commit="e" * 40,
        failed_release_run="333",
    )


def _document(state: StateBlock) -> dict[str, Any]:
    return dataclasses.asdict(state)


def _body_with(document: object) -> str:
    return f"## Release\n\n{BEGIN}\n```json\n{json.dumps(document)}\n```\n{END}\n"


def test_state_round_trip() -> None:
    state = _full_state()
    assert parse_state(f"## Release 2.1.340\n\n{render_state(state)}\n") == state


def test_initial_state_round_trips() -> None:
    state = initial_state(version="2.1.340", cut=_CUT, issues=(), without_deps=False)
    assert parse_state(render_state(state)) == state
    assert state.done == ()
    assert state.tag is None
    assert state.version_bump is None


def test_render_state_keys_are_field_names_and_tuples_are_lists() -> None:
    rendered = render_state(_full_state())
    lines = rendered.splitlines()
    assert lines[0] == BEGIN
    assert lines[1] == "```json"
    assert lines[-2] == "```"
    assert lines[-1] == END
    document = json.loads("\n".join(lines[2:-2]))
    assert list(document) == [f.name for f in dataclasses.fields(StateBlock)]
    assert document["issues"][1] == {"ref": "owner/repo#13", "prs": [31, 32]}
    assert document["failed_jobs"] == [{"branch": "main", "run_id": "111", "job": "docker-publish"}]
    assert document["done"] == ["prepare", "merge-release"]


def test_parse_state_missing_block_raises() -> None:
    with pytest.raises(StateBlockError, match="no release state block"):
        parse_state("## Release 2.1.340\n\nno block here\n")


def test_parse_state_two_blocks_raises() -> None:
    block = render_state(_full_state())
    with pytest.raises(StateBlockError, match="exactly once"):
        parse_state(f"{block}\n\n{block}\n")


def test_parse_state_unpaired_markers_raise() -> None:
    with pytest.raises(StateBlockError, match="do not pair up"):
        parse_state(f"{BEGIN}\n```json\n{{}}\n```\n")
    with pytest.raises(StateBlockError, match="do not pair up"):
        parse_state(f"{END}\n```json\n{{}}\n```\n{BEGIN}\n")


def test_parse_state_marker_not_alone_on_its_line_is_not_a_marker() -> None:
    with pytest.raises(StateBlockError, match="no release state block"):
        parse_state(f"see {BEGIN} and {END} inline\n")


def test_parse_state_unknown_key_raises() -> None:
    document = _document(_full_state()) | {"extra": 1}
    with pytest.raises(StateBlockError, match="unknown key\\(s\\): extra"):
        parse_state(_body_with(document))


def test_parse_state_missing_key_raises() -> None:
    document = _document(_full_state())
    del document["tag"]
    with pytest.raises(StateBlockError, match="missing key\\(s\\): tag"):
        parse_state(_body_with(document))


def test_parse_state_bad_json_raises() -> None:
    body = f"{BEGIN}\n```json\n{{not json\n```\n{END}\n"
    with pytest.raises(StateBlockError, match="not valid JSON"):
        parse_state(body)


def test_parse_state_unfenced_raises() -> None:
    body = f"{BEGIN}\n{{}}\n{END}\n"
    with pytest.raises(StateBlockError, match="fenced"):
        parse_state(body)


def test_parse_state_non_object_raises() -> None:
    with pytest.raises(StateBlockError, match="not an object"):
        parse_state(_body_with([1, 2]))


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("version", None, "a string"),
        ("version_bump", 1, "a string or null"),
        ("version_bump", "micro", '"patch", "minor", "major" or null'),
        ("tag", 5, "a string or null"),
        ("install_version", 2, "a string or null"),
        ("install_commit", False, "a string or null"),
        ("index_listed", "yes", "true or false"),
        ("done", "prepare", "a list"),
        ("done", ["prepare", 3], "a string"),
        ("issues", [{"ref": "owner/repo#1", "prs": [True]}], "an integer"),
        ("issues", [{"ref": "owner/repo#1"}], "missing key"),
        ("issues", ["owner/repo#1"], "an object"),
        ("failed_jobs", [{"branch": "release", "run_id": "1", "job": "docs"}], '"main" or'),
        ("failed_jobs", [{"branch": "main", "run_id": 1, "job": "docs"}], "a string"),
    ],
)
def test_parse_state_wrong_type_raises(key: str, value: object, expected: str) -> None:
    document = _document(_full_state()) | {key: value}
    with pytest.raises(StateBlockError, match=expected):
        parse_state(_body_with(document))


def test_upsert_state_keeps_checklist() -> None:
    stages = ["audit", "prepare"]
    body = (
        "## Release 2.1.340\n\nRepo: owner/repo\n\n"
        f"{checklist.render(stages, checked={'audit'})}\n\n"
        f"{render_state(initial_state(version='2.1.340', cut=_CUT, issues=(), without_deps=False))}"
        "\n\ntrailer\n"
    )
    new = dataclasses.replace(_full_state(), version="2.1.340")
    updated = upsert_state(body, new)
    assert parse_state(updated) == new
    assert checklist.parse(updated) == checklist.parse(body)
    assert updated.startswith("## Release 2.1.340\n\nRepo: owner/repo\n\n")
    assert updated.endswith("\n\ntrailer\n")
    assert updated.count(BEGIN) == 1


def test_upsert_state_appends_when_absent() -> None:
    state = _full_state()
    updated = upsert_state("## Release 2.1.340\n\n", state)
    assert updated == f"## Release 2.1.340\n\n{render_state(state)}\n"


def test_upsert_state_refuses_duplicated_block() -> None:
    block = render_state(_full_state())
    with pytest.raises(StateBlockError, match="exactly once"):
        upsert_state(f"{block}\n{block}\n", _full_state())


def test_parse_state_reads_a_crlf_body() -> None:
    """A body saved in GitHub's web editor comes back with CRLF line endings."""
    state = _full_state()
    body = f"## Release 2.1.340\n\n{render_state(state)}\n".replace("\n", "\r\n")
    assert parse_state(body) == state


def test_upsert_state_replaces_the_block_in_a_crlf_body() -> None:
    old = initial_state(version="2.1.340", cut=_CUT, issues=(), without_deps=False)
    body = f"## Release 2.1.340\n\n{render_state(old)}\n".replace("\n", "\r\n")
    updated = upsert_state(body, _full_state())
    assert updated.count(BEGIN) == 1
    assert parse_state(updated) == _full_state()
