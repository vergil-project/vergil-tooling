from __future__ import annotations

import datetime
import json
import os
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_vm_resolve as r

if TYPE_CHECKING:
    from pathlib import Path

UTC = datetime.timezone.utc


def test_last_activity_reads_last_timestamped_entry(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"user","timestamp":"2026-05-01T00:00:00.000Z"}\n'
        '{"type":"assistant","timestamp":"2026-05-02T00:00:00.000Z"}\n'
        '{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s"}\n'
    )
    assert r._last_activity(f) == datetime.datetime(2026, 5, 2, tzinfo=UTC).timestamp()


def test_last_activity_none_when_no_timestamp(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s"}\n')
    assert r._last_activity(f) is None


def test_last_activity_missing_file(tmp_path: Path) -> None:
    assert r._last_activity(tmp_path / "nope.jsonl") is None


def test_last_activity_skips_malformed_json(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"x","timestamp": BROKEN}\n{"type":"user","timestamp":"2026-05-02T00:00:00.000Z"}\n'
    )
    assert r._last_activity(f) == datetime.datetime(2026, 5, 2, tzinfo=UTC).timestamp()


def test_last_activity_handles_large_file_via_tail(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    pad = "x" * 1000
    lines = [
        '{"type":"user","timestamp":"2020-01-01T00:00:00.000Z","pad":"%s"}' % pad
        for _ in range(5000)
    ]
    lines.append('{"type":"assistant","timestamp":"2026-05-30T12:00:00.000Z"}')
    f.write_text("\n".join(lines) + "\n")
    assert r._last_activity(f) == datetime.datetime(2026, 5, 30, 12, tzinfo=UTC).timestamp()


def test_parse_ts_invalid_returns_none() -> None:
    assert r._parse_ts("not a date") is None
    assert r._parse_ts(12345) is None


def test_archive_session_appends_archived_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    t = projects / "s1.jsonl"
    t.write_text('{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s1"}\n')
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    r._archive_session("s1", "2026-05-30T14:23:07Z")
    assert r._last_agent_name(t) == "archived@2026-05-30T14:23:07Z@vergil:01:p"


def test_archive_session_missing_transcript_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    r._archive_session("ghost", "2026-05-30T14:23:07Z")  # must not raise


# --- _last_agent_name ---


def test_last_agent_name_last_wins(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text(
        "\n".join(
            [
                '{"type":"agent-name","agentName":"id:01:a","sessionId":"s"}',
                '{"type":"user","message":"hi"}',
                "",  # blank line skipped
                "not even close to json but has agent-name token? no",
                '{"type":"agent-name","agentName":"id:02:b","sessionId":"s"}',
            ]
        )
    )
    assert r._last_agent_name(f) == "id:02:b"


def test_last_agent_name_skips_malformed_json(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"agent-name", BROKEN "agentName":"x"}\n')
    assert r._last_agent_name(f) is None


def test_last_agent_name_ignores_non_string_value(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"agent-name","agentName":123}\n')
    assert r._last_agent_name(f) is None


def test_last_agent_name_missing_file_returns_none(tmp_path: Path) -> None:
    assert r._last_agent_name(tmp_path / "nope.jsonl") is None


def test_last_agent_name_ignores_other_type_with_token(tmp_path: Path) -> None:
    # line carries the "agent-name" substring but is not an agent-name entry
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","agent-name":"not really"}\n')
    assert r._last_agent_name(f) is None


def test_claude_dir_points_at_dot_claude() -> None:
    assert r._claude_dir().name == ".claude"


# --- name_by_session ---


def test_name_by_session_missing_dir(tmp_path: Path) -> None:
    assert r.name_by_session(tmp_path / "absent") == {}


def test_name_by_session_maps_stem_to_name(tmp_path: Path) -> None:
    slug = tmp_path / "slug"
    slug.mkdir()
    (slug / "s1.jsonl").write_text('{"type":"agent-name","agentName":"id:01:a","sessionId":"s1"}\n')
    (slug / "s2.jsonl").write_text('{"type":"user"}\n')  # no name -> skipped
    assert r.name_by_session(tmp_path) == {"s1": "id:01:a"}


# --- read_roster ---


def test_read_roster_missing_dir(tmp_path: Path) -> None:
    assert r.read_roster(tmp_path / "absent") == []


def test_read_roster_filters_bad_files(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text('{"pid": 1, "sessionId": "s"}')
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "list.json").write_text("[1, 2, 3]")  # not a dict
    entries = r.read_roster(tmp_path)
    assert entries == [{"pid": 1, "sessionId": "s"}]


# --- _parse_starttime ---


def test_parse_starttime_extracts_field() -> None:
    # synthetic: "(comm)" then fields; state 'S' is field 3, starttime field 22
    tail = " ".join(["x"] * 19 + ["START"] + ["y"] * 5)
    assert r._parse_starttime(f"123 (claude) {tail}") == "START"


def test_parse_starttime_no_paren() -> None:
    assert r._parse_starttime("garbage with no paren") is None


def test_parse_starttime_too_short() -> None:
    assert r._parse_starttime("123 (c) S 1 2 3") is None


# --- _proc_start / _is_live ---


def test_proc_start_real_process_is_digits() -> None:
    val = r._proc_start(os.getpid())
    assert val is not None and val.isdigit()


def test_proc_start_missing_pid() -> None:
    assert r._proc_start(2**30) is None


def test_is_live_dead_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "_proc_start", lambda _pid: None)
    assert r._is_live(123, "999") is False


def test_is_live_without_procstart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "_proc_start", lambda _pid: "555")
    assert r._is_live(123, None) is True


def test_is_live_matching_and_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r, "_proc_start", lambda _pid: "555")
    assert r._is_live(123, "555") is True
    assert r._is_live(123, "777") is False


# --- active_session_ids ---


def test_active_session_ids_filters_and_checks_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(r, "_is_live", lambda pid, _ps: pid == 10)
    roster: list[dict[str, object]] = [
        {"pid": 10, "sessionId": "live", "procStart": "1"},
        {"pid": 20, "sessionId": "dead", "procStart": "2"},
        {"pid": "x", "sessionId": "bad-pid"},  # wrong type
        {"pid": 30},  # no sessionId
    ]
    assert r.active_session_ids(roster) == {"live"}


# --- resolve ---


@pytest.fixture()
def capture_exec(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(os, "execvp", lambda _f, argv: calls.append(argv))
    return calls


DAY = 86400.0


def _resolve(identity: str, path: str, **kw: object) -> int:
    defaults: dict[str, object] = {
        "requested_slot": None,
        "fork": False,
        "fresh": False,
        "extra": [],
        "stale_days": 7,
        "archive_days": 14,
    }
    defaults.update(kw)
    return r.resolve(identity, path, **defaults)  # type: ignore[arg-type]


def test_resolve_create(monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]) -> None:
    monkeypatch.setattr(r, "_read_state", lambda: ({}, set(), {}))
    assert _resolve("id", "p", extra=["--model", "opus"]) == 0
    assert capture_exec == [["claude", "-n", "id:01:p", "--model", "opus"]]


def test_resolve_resume(monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]) -> None:
    monkeypatch.setattr(r, "_read_state", lambda: ({"s1": "id:01:p"}, set(), {}))
    assert _resolve("id", "p") == 0
    assert capture_exec == [["claude", "--resume", "s1"]]


def test_resolve_fork(monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]) -> None:
    monkeypatch.setattr(r, "_read_state", lambda: ({"s1": "id:01:p"}, {"s1"}, {}))
    assert _resolve("id", "p", requested_slot=1, fork=True) == 0
    assert capture_exec == [["claude", "--resume", "s1", "--fork-session", "-n", "id:02:p"]]


def test_resolve_refuse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(r, "_read_state", lambda: ({}, set(), {}))
    assert _resolve("id", "p", fork=True) == 1  # fork without slot
    assert "ERROR" in capsys.readouterr().err


def test_resolve_sweeps_stale_and_creates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    capture_exec: list[list[str]],
) -> None:
    now = 100 * DAY
    monkeypatch.setattr(
        r, "_read_state", lambda: ({"old": "vergil:01:p"}, set(), {"old": now - 20 * DAY})
    )
    monkeypatch.setattr(r, "_now", lambda: now)
    monkeypatch.setattr(r, "_now_iso", lambda: "2026-05-30T00:00:00Z")
    archived: list[str] = []
    monkeypatch.setattr(r, "_archive_session", lambda sid, _ts: archived.append(sid))
    assert _resolve("vergil", "p") == 0
    assert archived == ["old"]
    assert capture_exec == [["claude", "-n", "vergil:01:p"]]
    assert "auto-archiving" in capsys.readouterr().err


def test_resolve_warn_prompt_resume(
    monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]
) -> None:
    now = 100 * DAY
    monkeypatch.setattr(
        r, "_read_state", lambda: ({"s1": "vergil:01:p"}, set(), {"s1": now - 9 * DAY})
    )
    monkeypatch.setattr(r, "_now", lambda: now)
    monkeypatch.setattr(r, "_prompt_stale", lambda *_a: "r")
    assert _resolve("vergil", "p") == 0
    assert capture_exec == [["claude", "--resume", "s1"]]


def test_resolve_warn_prompt_fresh(
    monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]
) -> None:
    now = 100 * DAY
    monkeypatch.setattr(
        r, "_read_state", lambda: ({"s1": "vergil:01:p"}, set(), {"s1": now - 9 * DAY})
    )
    monkeypatch.setattr(r, "_now", lambda: now)
    monkeypatch.setattr(r, "_now_iso", lambda: "2026-05-30T00:00:00Z")
    monkeypatch.setattr(r, "_prompt_stale", lambda *_a: "f")
    archived: list[str] = []
    monkeypatch.setattr(r, "_archive_session", lambda sid, _ts: archived.append(sid))
    assert _resolve("vergil", "p") == 0
    assert archived == ["s1"]
    assert capture_exec == [["claude", "-n", "vergil:01:p"]]


def test_resolve_warn_prompt_cancel(
    monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]
) -> None:
    now = 100 * DAY
    monkeypatch.setattr(
        r, "_read_state", lambda: ({"s1": "vergil:01:p"}, set(), {"s1": now - 9 * DAY})
    )
    monkeypatch.setattr(r, "_now", lambda: now)
    monkeypatch.setattr(r, "_prompt_stale", lambda *_a: "c")
    assert _resolve("vergil", "p") == 0
    assert capture_exec == []


def test_prompt_stale_non_tty_returns_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r.sys.stdin, "isatty", lambda: False)
    assert r._prompt_stale("vergil-project/p", 1, 9) == "r"


def test_prompt_stale_tty_reads_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(r.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _p: "f")
    assert r._prompt_stale("vergil-project/p", 1, 9) == "f"
    monkeypatch.setattr("builtins.input", lambda _p: "")  # unrecognized -> cancel
    assert r._prompt_stale("vergil-project/p", 1, 9) == "c"


def test_exec_claude_invokes_execvp(capture_exec: list[list[str]]) -> None:
    assert r._exec_claude(["-n", "x"]) == 0
    assert capture_exec == [["claude", "-n", "x"]]


# --- list_json ---


def test_list_json_includes_age_and_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        r,
        "_read_state",
        lambda: (
            {"s1": "vergil:01:p", "a1": "archived@2026-05-01T00:00:00Z@vergil:03:p"},
            {"s1"},
            {"s1": 1748000000.0, "a1": 1746000000.0},
        ),
    )
    assert r.list_json() == 0
    rows = json.loads(capsys.readouterr().out)
    by = {(x["identity"], x["slot"], x["state"]): x for x in rows}
    assert ("vergil", 1, "active") in by
    assert by[("vergil", 1, "active")]["lastActive"] == 1748000000.0
    assert ("vergil", 3, "archived") in by
    assert by[("vergil", 3, "archived")]["archivedAt"] == "2026-05-01T00:00:00Z"


def test_list_json_idle_state(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(r, "_read_state", lambda: ({"s1": "vergil:01:p"}, set(), {}))
    assert r.list_json() == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "idle"


def test_archived_rows_skips_unparseable_original(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # archived label whose embedded "original" is not a valid slot name
    monkeypatch.setattr(
        r, "_read_state", lambda: ({"a1": "archived@2026-05-01T00:00:00Z@garbage"}, set(), {})
    )
    assert r.list_json() == 0
    assert json.loads(capsys.readouterr().out) == []


# --- _read_state integration ---


def test_read_state_combines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    (projects / "s1.jsonl").write_text(
        '{"type":"agent-name","agentName":"id:01:p","sessionId":"s1"}\n'
    )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "100.json").write_text('{"pid": 100, "sessionId": "s1"}')
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(r, "_is_live", lambda _pid, _ps: True)
    names, active, _la = r._read_state()
    assert names == {"s1": "id:01:p"}
    assert active == {"s1"}


def test_read_state_returns_last_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    (projects / "s1.jsonl").write_text(
        '{"type":"user","timestamp":"2026-05-02T00:00:00.000Z"}\n'
        '{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s1"}\n'
    )
    (projects / "s2.jsonl").write_text(
        '{"type":"agent-name","agentName":"vergil:02:p","sessionId":"s2"}\n'
    )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "100.json").write_text('{"pid":100,"sessionId":"s2","updatedAt":1748000000000}')
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(r, "_is_live", lambda _pid, _ps: True)
    names, active, last_active = r._read_state()
    assert active == {"s2"}
    assert last_active["s2"] == 1748000000.0
    assert last_active["s1"] == datetime.datetime(2026, 5, 2, tzinfo=UTC).timestamp()


# --- main ---


def test_main_list_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(r, "_read_state", lambda: ({}, set(), {}))
    assert r.main(["--list-json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_requires_identity_and_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert r.main(["--identity", "id"]) == 1
    assert "required" in capsys.readouterr().err


def test_main_dispatches_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_resolve(identity, path, slot, fork, extra):  # noqa: ANN001, ANN202
        seen.update(identity=identity, path=path, slot=slot, fork=fork, extra=extra)
        return 0

    monkeypatch.setattr(r, "resolve", fake_resolve)
    assert r.main(["--identity", "id", "--path", "p", "--slot", "2", "x"]) == 0
    assert seen == {
        "identity": "id",
        "path": "p",
        "slot": 2,
        "fork": False,
        "extra": ["x"],
    }


def test_main_strips_leading_double_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        r,
        "resolve",
        lambda i, p, s, f, extra: captured.update(extra=extra) or 0,  # noqa: ARG005
    )
    r.main(["--identity", "id", "--path", "p", "--", "claude", "--model", "opus"])
    assert captured["extra"] == ["claude", "--model", "opus"]
