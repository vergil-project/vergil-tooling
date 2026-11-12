"""Tests for the batch marker (epic vergil-project/.github#1019, Task 2b).

Each test uses a temporary directory as the main clone; nothing reads git or
GitHub.
"""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.pr_workflow.batch_marker import (
    BatchMarkerError,
    Marker,
    existing_batch,
    extract,
    marker_path,
    read_marker,
    remove_markers,
    write_markers,
)
from vergil_tooling.lib.pr_workflow.errors import WorkflowError

if TYPE_CHECKING:
    from pathlib import Path

SHA = "a" * 40
OTHER_SHA = "b" * 40


def _issue_dir(root: Path, number: int) -> Path:
    return root / "build" / ".vergil-state" / "issue-implement" / str(number)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_marker_path_is_the_issue_state_dir(tmp_path: Path) -> None:
    assert marker_path(tmp_path, "#10") == _issue_dir(tmp_path, 10) / "batch"


def test_marker_path_refuses_a_non_canonical_ref(tmp_path: Path) -> None:
    for bad in ("10", "#0", "#../1", "acme/tools#10", "#10/x"):
        with pytest.raises(BatchMarkerError, match="not a canonical issue ref"):
            marker_path(tmp_path, bad)


def test_write_markers_writes_every_member_with_one_base(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    for number in (10, 11, 12):
        path = _issue_dir(tmp_path, number) / "batch"
        assert json.loads(path.read_text()) == {"issues": ["#10", "#11", "#12"], "base_sha": SHA}
        assert _mode(path) == 0o600
        assert read_marker(tmp_path, f"#{number}") == Marker(["#10", "#11", "#12"], SHA)
    state = tmp_path / "build" / ".vergil-state"
    for level in (state, state / "issue-implement", _issue_dir(tmp_path, 10)):
        assert _mode(level) == 0o700
    # No temporary file is left beside a marker.
    assert sorted(p.name for p in _issue_dir(tmp_path, 10).iterdir()) == ["batch"]


@pytest.mark.parametrize(
    ("issues", "match"),
    [
        (["#10"], "2 to 8 issues"),
        ([f"#{n}" for n in range(10, 19)], "2 to 8 issues"),
        (["#10", "#10"], "more than once"),
        (["#10", "11"], "not a canonical issue ref"),
    ],
)
def test_write_markers_refuses_a_bad_list(tmp_path: Path, issues: list[str], match: str) -> None:
    with pytest.raises(BatchMarkerError, match=match):
        write_markers(tmp_path, issues, base_sha=SHA)
    assert not (tmp_path / "build").exists()


def test_write_markers_refuses_a_bad_base(tmp_path: Path) -> None:
    with pytest.raises(BatchMarkerError, match="not a full commit SHA"):
        write_markers(tmp_path, ["#10", "#11"], base_sha="origin/develop")


def test_write_markers_refuses_a_public_level_and_writes_nothing(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#20", "#21"], base_sha=SHA)
    _issue_dir(tmp_path, 21).chmod(0o755)
    with pytest.raises(BatchMarkerError, match="has mode 755, not 700"):
        write_markers(tmp_path, ["#10", "#21"], base_sha=SHA)
    # #10's directory was checked first, but no marker was written to it.
    assert not (_issue_dir(tmp_path, 10) / "batch").exists()


def test_write_markers_replaces_a_symlink_rather_than_follow_it(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    target = tmp_path / "outside"
    target.write_text("keep\n")
    path = _issue_dir(tmp_path, 10) / "batch"
    path.unlink()
    path.symlink_to(target)
    write_markers(tmp_path, ["#10", "#11"], base_sha=OTHER_SHA)
    assert target.read_text() == "keep\n"
    assert not path.is_symlink()
    assert read_marker(tmp_path, "#10") == Marker(["#10", "#11"], OTHER_SHA)


def test_read_marker_none_without_a_marker(tmp_path: Path) -> None:
    assert read_marker(tmp_path, "#10") is None
    write_markers(tmp_path, ["#11", "#12"], base_sha=SHA)
    assert read_marker(tmp_path, "#10") is None
    _issue_dir(tmp_path, 10).mkdir(mode=0o700)
    assert read_marker(tmp_path, "#10") is None


def test_read_marker_refuses_a_symlinked_marker(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    path = _issue_dir(tmp_path, 10) / "batch"
    real = tmp_path / "real"
    real.write_text(path.read_text())
    path.unlink()
    path.symlink_to(real)
    with pytest.raises(BatchMarkerError, match="could not read the batch marker"):
        read_marker(tmp_path, "#10")


def test_read_marker_refuses_a_symlinked_level(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    elsewhere = tmp_path / "elsewhere"
    _issue_dir(tmp_path, 10).rename(elsewhere)
    _issue_dir(tmp_path, 10).symlink_to(elsewhere)
    with pytest.raises(BatchMarkerError, match="is a symlink, not a directory"):
        read_marker(tmp_path, "#10")


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("{", "not valid JSON"),
        ("[]", "exactly the keys"),
        (json.dumps({"issues": ["#10", "#11"]}), "exactly the keys"),
        (json.dumps({"issues": ["#10", "#11"], "base_sha": SHA, "x": 1}), "exactly the keys"),
        (json.dumps({"issues": "#10", "base_sha": SHA}), "list of issue refs"),
        (json.dumps({"issues": ["#10", 11], "base_sha": SHA}), "list of issue refs"),
        (json.dumps({"issues": ["#10", "#11"], "base_sha": 1}), "must be a string"),
        (json.dumps({"issues": ["#10", "#11"], "base_sha": "abc"}), "not a full commit SHA"),
        (json.dumps({"issues": ["#10"], "base_sha": SHA}), "2 to 8 issues"),
        (json.dumps({"issues": ["#11", "#12"], "base_sha": SHA}), "does not list #10"),
    ],
)
def test_read_marker_refuses_a_malformed_marker(tmp_path: Path, content: str, match: str) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    (_issue_dir(tmp_path, 10) / "batch").write_text(content)
    with pytest.raises(BatchMarkerError, match=match):
        read_marker(tmp_path, "#10")


def test_existing_batch_none_when_no_member_has_a_marker(tmp_path: Path) -> None:
    assert existing_batch(tmp_path, ["#10", "#11"]) is None


def test_existing_batch_returns_an_identical_batch(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    assert existing_batch(tmp_path, ["#10", "#11"]) == Marker(["#10", "#11"], SHA)


def test_existing_batch_refuses_another_order(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    with pytest.raises(BatchMarkerError, match="#11 is already in the batch #10, #11"):
        existing_batch(tmp_path, ["#11", "#10"])


def test_existing_batch_refuses_a_member_without_a_marker(tmp_path: Path) -> None:
    # A start interrupted before every member's marker was written.
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    (_issue_dir(tmp_path, 12) / "batch").unlink()
    with pytest.raises(BatchMarkerError, match="#12 has no batch marker"):
        existing_batch(tmp_path, ["#10", "#11", "#12"])


def test_existing_batch_refuses_a_member_of_another_batch(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    with pytest.raises(BatchMarkerError, match="#10 is already in the batch #10, #11"):
        existing_batch(tmp_path, ["#10", "#11", "#12"])


def test_write_markers_refuses_when_the_marker_name_is_a_directory(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    path = _issue_dir(tmp_path, 10) / "batch"
    path.unlink()
    path.mkdir()
    (path / "keep").write_text("x")
    with pytest.raises(BatchMarkerError, match="could not write the batch marker"):
        write_markers(tmp_path, ["#10", "#11"], base_sha=OTHER_SHA)
    # The temporary file was cleaned up.
    assert sorted(p.name for p in _issue_dir(tmp_path, 10).iterdir()) == ["batch"]


def test_read_marker_refuses_a_directory_marker(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    path = _issue_dir(tmp_path, 10) / "batch"
    path.unlink()
    path.mkdir()
    with pytest.raises(BatchMarkerError, match="is not a regular file"):
        read_marker(tmp_path, "#10")


def test_read_marker_refuses_undecodable_bytes(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    (_issue_dir(tmp_path, 10) / "batch").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(BatchMarkerError, match="could not read the batch marker"):
        read_marker(tmp_path, "#10")


def test_remove_markers_refuses_an_unremovable_marker(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    path = _issue_dir(tmp_path, 10) / "batch"
    path.unlink()
    path.mkdir()
    with pytest.raises(BatchMarkerError, match="could not remove #10's batch marker"):
        remove_markers(tmp_path, ["#10"])


def test_extract_reports_a_failed_directory_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)

    def refuse(_path: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(batch_marker.shutil, "rmtree", refuse)
    with pytest.raises(BatchMarkerError, match="Re-run batch-extract to finish"):
        extract(tmp_path, "#11")
    monkeypatch.undo()
    # The re-run finishes it.
    assert extract(tmp_path, "#11") == ["#10", "#12"]
    assert not _issue_dir(tmp_path, 11).exists()


def test_existing_batch_refuses_disagreeing_bases(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    path = _issue_dir(tmp_path, 11) / "batch"
    path.write_text(json.dumps({"issues": ["#10", "#11"], "base_sha": OTHER_SHA}))
    with pytest.raises(BatchMarkerError, match="the markers disagree"):
        existing_batch(tmp_path, ["#10", "#11"])


def test_extract_removes_whole_state_dir_and_rewrites_lists(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    review = _issue_dir(tmp_path, 11) / "merge-review"
    review.mkdir(mode=0o700)
    (review / "open-request.json").write_text("{}")
    assert extract(tmp_path, "#11") == ["#10", "#12"]
    assert not _issue_dir(tmp_path, 11).exists()
    for issue in ("#10", "#12"):
        assert read_marker(tmp_path, issue) == Marker(["#10", "#12"], SHA)


def test_extract_refuses_while_a_security_details_file_exists(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    review = _issue_dir(tmp_path, 11) / "merge-review"
    review.mkdir(mode=0o700)
    details = review / "security-R2.md"
    details.write_text("details\n")
    with pytest.raises(BatchMarkerError) as raised:
        extract(tmp_path, "#11")
    message = str(raised.value)
    assert str(details) in message
    assert "record step" in message
    assert "vrg-security-record create" in message
    assert details.read_text() == "details\n"
    for issue in ("#10", "#11", "#12"):
        assert read_marker(tmp_path, issue) == Marker(["#10", "#11", "#12"], SHA)


def test_extract_refuses_a_security_file_in_the_issue_dir_itself(tmp_path: Path) -> None:
    # issue-implement's own record-ruling details file sits directly in <N>/.
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    details = _issue_dir(tmp_path, 10) / "security-1.md"
    details.write_text("details\n")
    with pytest.raises(BatchMarkerError, match="security-1.md"):
        extract(tmp_path, "#10")
    assert details.exists()
    assert read_marker(tmp_path, "#11") == Marker(["#10", "#11"], SHA)


def test_extract_to_one_member_removes_last_marker(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    assert extract(tmp_path, "#10") == ["#11"]
    assert not _issue_dir(tmp_path, 10).exists()
    assert read_marker(tmp_path, "#11") is None
    # The last member's directory held only the marker, so it is gone too.
    assert not _issue_dir(tmp_path, 11).exists()


def test_extract_to_one_member_keeps_its_merge_review_state(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    review = _issue_dir(tmp_path, 11) / "merge-review"
    review.mkdir(mode=0o700)
    assert extract(tmp_path, "#10") == ["#11"]
    assert read_marker(tmp_path, "#11") is None
    assert review.is_dir()


def test_extract_refuses_an_issue_without_a_marker(tmp_path: Path) -> None:
    with pytest.raises(BatchMarkerError, match="not in a batch"):
        extract(tmp_path, "#10")


def test_extract_refuses_disagreeing_markers_and_changes_nothing(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    write_markers(tmp_path, ["#12", "#13"], base_sha=SHA)
    with pytest.raises(BatchMarkerError, match="#12's batch marker lists #12, #13"):
        extract(tmp_path, "#10")
    assert read_marker(tmp_path, "#10") == Marker(["#10", "#11", "#12"], SHA)
    assert read_marker(tmp_path, "#11") == Marker(["#10", "#11", "#12"], SHA)


def test_extract_refuses_a_member_with_another_base(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    path = _issue_dir(tmp_path, 12) / "batch"
    path.write_text(json.dumps({"issues": ["#10", "#11", "#12"], "base_sha": OTHER_SHA}))
    with pytest.raises(BatchMarkerError, match="base_sha"):
        extract(tmp_path, "#10")
    assert _issue_dir(tmp_path, 10).is_dir()


def test_extract_refuses_a_missing_member_marker_in_a_batch_that_remains(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    (_issue_dir(tmp_path, 12) / "batch").unlink()
    with pytest.raises(BatchMarkerError, match="lists none"):
        extract(tmp_path, "#10")


def test_extract_finishes_an_interrupted_extraction(tmp_path: Path) -> None:
    # The others were rewritten, but #11's own directory was never removed.
    write_markers(tmp_path, ["#10", "#11", "#12"], base_sha=SHA)
    write_markers(tmp_path, ["#10", "#12"], base_sha=SHA)
    (_issue_dir(tmp_path, 11) / "batch").write_text(
        json.dumps({"issues": ["#10", "#11", "#12"], "base_sha": SHA})
    )
    assert extract(tmp_path, "#11") == ["#10", "#12"]
    assert not _issue_dir(tmp_path, 11).exists()


def test_extract_finishes_an_interrupted_extraction_to_one(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    remove_markers(tmp_path, ["#11"])
    assert extract(tmp_path, "#10") == ["#11"]
    assert not _issue_dir(tmp_path, 10).exists()


def test_remove_markers_removes_markers_and_empty_dirs(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    review = _issue_dir(tmp_path, 11) / "merge-review"
    review.mkdir(mode=0o700)
    remove_markers(tmp_path, ["#10", "#11", "#12"])
    assert not _issue_dir(tmp_path, 10).exists()
    assert read_marker(tmp_path, "#11") is None
    assert review.is_dir()


def test_remove_markers_refuses_a_bad_ref_before_removing_any(tmp_path: Path) -> None:
    write_markers(tmp_path, ["#10", "#11"], base_sha=SHA)
    with pytest.raises(BatchMarkerError, match="not a canonical issue ref"):
        remove_markers(tmp_path, ["#10", "11"])
    assert read_marker(tmp_path, "#10") is not None


def test_marker_error_is_a_workflow_error() -> None:
    assert issubclass(BatchMarkerError, WorkflowError)
