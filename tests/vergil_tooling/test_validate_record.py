"""Tests for the vrg-validate record, and vrg-validate writing it."""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_validate
from vergil_tooling.lib import progress
from vergil_tooling.lib.progress import Stage, StageResult
from vergil_tooling.lib.validate_record import (
    RECORD_PATH,
    ValidateRecord,
    ValidateRecordError,
    read_record,
    write_record,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

HEAD = "a" * 40

_RESULTS = [
    StageResult("common", "ok"),
    StageResult("lint", "ok"),
    StageResult("typecheck", "failed", 1.0, "boom"),
    StageResult("audit", "warn"),
    StageResult("custom", "skipped"),
    StageResult("test", "interrupted"),
]


def _write(tmp_path: Path, **over: Any) -> Path:
    fields: dict[str, Any] = {
        "head": HEAD,
        "exit_code": 0,
        "full": True,
        "clean_at_start": True,
        "results": _RESULTS[:2],
    }
    fields.update(over)
    return write_record(tmp_path, **fields)


def test_record_round_trip(tmp_path: Path) -> None:
    path = _write(tmp_path)
    assert path == tmp_path / ".vergil" / "vrg-validate-last.json"
    assert read_record(tmp_path) == ValidateRecord(
        head=HEAD,
        exit_code=0,
        full=True,
        clean_at_start=True,
        passed=("common", "lint"),
        not_passed=(),
    )


def test_record_shape(tmp_path: Path) -> None:
    _write(tmp_path, exit_code=130, full=False, clean_at_start=False, results=_RESULTS)
    data = json.loads((tmp_path / RECORD_PATH).read_text())
    assert data == {
        "head": HEAD,
        "exit_code": 130,
        "full": False,
        "clean_at_start": False,
        "stages": [{"name": r.name, "status": r.status} for r in _RESULTS],
    }


def test_record_splits_passed_and_not_passed(tmp_path: Path) -> None:
    _write(tmp_path, results=_RESULTS)
    record = read_record(tmp_path)
    assert record.passed == ("common", "lint", "audit")
    assert record.not_passed == ("typecheck", "custom", "test")


def test_record_is_replaced(tmp_path: Path) -> None:
    _write(tmp_path)
    _write(tmp_path, head="b" * 40)
    assert read_record(tmp_path).head == "b" * 40
    assert [p.name for p in (tmp_path / ".vergil").iterdir()] == ["vrg-validate-last.json"]


def test_read_record_missing(tmp_path: Path) -> None:
    with pytest.raises(ValidateRecordError, match="no vrg-validate record"):
        read_record(tmp_path)


def test_read_record_unreadable(tmp_path: Path) -> None:
    (tmp_path / RECORD_PATH).mkdir(parents=True)
    with pytest.raises(ValidateRecordError, match="cannot read the vrg-validate record"):
        read_record(tmp_path)


def test_read_record_not_json(tmp_path: Path) -> None:
    (tmp_path / ".vergil").mkdir()
    (tmp_path / RECORD_PATH).write_text("{")
    with pytest.raises(ValidateRecordError, match="is not valid JSON"):
        read_record(tmp_path)


def _good() -> dict[str, Any]:
    return {
        "head": HEAD,
        "exit_code": 0,
        "full": True,
        "clean_at_start": True,
        "stages": [{"name": "common", "status": "ok"}],
    }


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"extra": 1}, "it must hold exactly"),
        ({"head": ""}, "head must be a commit SHA"),
        ({"head": 1}, "head must be a commit SHA"),
        ({"exit_code": "0"}, "exit_code must be an integer"),
        ({"exit_code": False}, "exit_code must be an integer"),
        ({"full": 1}, "full must be true or false"),
        ({"clean_at_start": None}, "clean_at_start must be true or false"),
        ({"stages": {}}, "stages must be a list"),
        ({"stages": ["common"]}, "stage 1 must be"),
        ({"stages": [{"name": "common"}]}, "stage 1 must be"),
        ({"stages": [{"name": 1, "status": "ok"}]}, "stage 1 must be"),
        ({"stages": [{"name": "common", "status": ["ok"]}]}, "stage 1 must be"),
        ({"stages": [{"name": "common", "status": "passed"}]}, "stage 1 must be"),
    ],
)
def test_read_record_malformed(tmp_path: Path, change: dict[str, Any], message: str) -> None:
    (tmp_path / ".vergil").mkdir()
    (tmp_path / RECORD_PATH).write_text(json.dumps({**_good(), **change}))
    with pytest.raises(ValidateRecordError, match=message):
        read_record(tmp_path)


def test_read_record_not_an_object(tmp_path: Path) -> None:
    (tmp_path / ".vergil").mkdir()
    (tmp_path / RECORD_PATH).write_text("[]")
    with pytest.raises(ValidateRecordError, match="it must hold exactly"):
        read_record(tmp_path)


# -- run_pipeline's sink -------------------------------------------------------------


def _boom(_ctx: object) -> None:
    msg = "boom"
    raise RuntimeError(msg)


def test_pipeline_fills_sink(tmp_path: Path) -> None:
    sink: list[StageResult] = []
    stages = [
        Stage("one", lambda _ctx: None, mode="fail_defer"),
        Stage("two", _boom, mode="fail_defer"),
        Stage("three", lambda _ctx: None, mode="fail_defer", skip_flag="skip_three"),
    ]
    args = argparse.Namespace(output_window=5, output_format="plain", skip_three=True)
    code = progress.run_pipeline(
        None,
        stages,
        command="test-cmd",
        label="test",
        args=args,
        repo_root=tmp_path,
        results_sink=sink,
    )
    assert code == 1
    assert [(r.name, r.status) for r in sink] == [
        ("one", "ok"),
        ("two", "failed"),
        ("three", "skipped"),
    ]


# -- vrg-validate writes it ----------------------------------------------------------

_MOD = "vergil_tooling.bin.vrg_validate"


@pytest.fixture
def validate_env(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        '\n[dependencies]\nvergil = "v2.0"\n\n[ci]\nversions = ["3.14"]\n'
    )
    with (
        patch(_MOD + "._in_dev_container", return_value=True),
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.commit_sha", return_value=HEAD),
        patch(_MOD + ".git.working_tree_status", return_value=""),
        patch(_MOD + "._run_common_checks", return_value=0),
        patch(_MOD + "._find_custom_validator", return_value=None),
    ):
        yield tmp_path


def test_validate_records_a_full_run(validate_env: Path) -> None:
    assert vrg_validate.main([]) == 0
    record = read_record(validate_env)
    assert record == ValidateRecord(HEAD, 0, True, True, ("common",), ())


def test_validate_records_a_partial_failing_run(validate_env: Path) -> None:
    with (
        patch(_MOD + "._run_common_checks", return_value=1),
        patch(_MOD + ".git.working_tree_status", return_value=" M src/x.py"),
    ):
        assert vrg_validate.main(["--check", "common"]) == 1
    record = read_record(validate_env)
    assert record == ValidateRecord(HEAD, 1, False, False, (), ("common",))


def test_validate_records_an_interrupted_run(validate_env: Path) -> None:
    with (
        patch(_MOD + ".progress.run_pipeline", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        vrg_validate.main([])
    record = read_record(validate_env)
    assert record.exit_code == 130
    assert record.head == HEAD


def test_validate_records_a_run_that_raised(validate_env: Path) -> None:
    with (
        patch(_MOD + ".progress.run_pipeline", side_effect=RuntimeError("renderer")),
        pytest.raises(RuntimeError),
    ):
        vrg_validate.main([])
    assert read_record(validate_env).exit_code == 1
