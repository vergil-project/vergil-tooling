from __future__ import annotations

import argparse
import datetime
import json
import os
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_vm_resolve as r

if TYPE_CHECKING:
    from pathlib import Path

UTC = datetime.UTC


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
        f'{{"type":"user","timestamp":"2020-01-01T00:00:00.000Z","pad":"{pad}"}}'
        for _ in range(5000)
    ]
    lines.append('{"type":"assistant","timestamp":"2026-05-30T12:00:00.000Z"}')
    f.write_text("\n".join(lines) + "\n")
    assert r._last_activity(f) == datetime.datetime(2026, 5, 30, 12, tzinfo=UTC).timestamp()


# --- transcript_cwd (issue #2607) ---


def test_transcript_cwd_returns_first_recorded(tmp_path: Path) -> None:
    # A leading entry without cwd is skipped; the first entry carrying a cwd wins.
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"summary","leafUuid":"x"}\n'
        '{"type":"user","cwd":"/work/repo","timestamp":"2026-05-02T00:00:00.000Z"}\n'
        '{"type":"user","cwd":"/other","timestamp":"2026-05-03T00:00:00.000Z"}\n'
    )
    assert r.transcript_cwd(f) == "/work/repo"


def test_transcript_cwd_skips_malformed_then_matches(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"x","cwd": BROKEN}\n{"type":"user","cwd":"/good"}\n')
    assert r.transcript_cwd(f) == "/good"


def test_transcript_cwd_ignores_non_string_or_empty(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"a","cwd":123}\n{"type":"b","cwd":""}\n{"type":"c","cwd":"/w"}\n')
    assert r.transcript_cwd(f) == "/w"


def test_transcript_cwd_none_when_absent(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","message":"hi"}\n')
    assert r.transcript_cwd(f) is None


def test_transcript_cwd_missing_file(tmp_path: Path) -> None:
    assert r.transcript_cwd(tmp_path / "nope.jsonl") is None


def test_parse_ts_invalid_returns_none() -> None:
    assert r._parse_ts("not a date") is None
    assert r._parse_ts(12345) is None


def test_last_activity_skips_bad_timestamp_lines(tmp_path: Path) -> None:
    # from the end: malformed JSON, then non-string timestamp, then a valid one
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"user","timestamp":"2026-05-02T00:00:00.000Z"}\n'
        '{"type":"a","timestamp":123}\n'
        '{"type":"b","timestamp": BROKEN}\n'
    )
    assert r._last_activity(f) == datetime.datetime(2026, 5, 2, tzinfo=UTC).timestamp()


def test_archive_session_append_oserror_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    (projects / "adir.jsonl").mkdir()  # a directory -> open("a") raises OSError
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(r, "_last_session_name", lambda _t: "vergil:01:p")
    r._archive_session("adir", "2026-05-30T14:23:07Z")  # must not raise


def test_archive_session_appends_archived_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    t = projects / "s1.jsonl"
    t.write_text('{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s1"}\n')
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    r._archive_session("s1", "2026-05-30T14:23:07Z")
    assert r._last_session_name(t) == "archived@2026-05-30T14:23:07Z@vergil:01:p"


def test_archive_session_missing_transcript_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    r._archive_session("ghost", "2026-05-30T14:23:07Z")  # must not raise


# --- _last_session_name ---


def test_last_session_name_last_wins(tmp_path: Path) -> None:
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
    assert r._last_session_name(f) == "id:02:b"


def test_last_session_name_skips_malformed_json(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"agent-name", BROKEN "agentName":"x"}\n')
    assert r._last_session_name(f) is None


def test_last_session_name_ignores_non_string_value(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"agent-name","agentName":123}\n')
    assert r._last_session_name(f) is None


def test_last_session_name_missing_file_returns_none(tmp_path: Path) -> None:
    assert r._last_session_name(tmp_path / "nope.jsonl") is None


def test_last_session_name_ignores_other_type_with_token(tmp_path: Path) -> None:
    # line carries the "agent-name" substring but is not an agent-name entry
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","agent-name":"not really"}\n')
    assert r._last_session_name(f) is None


def test_last_session_name_reads_via_bounded_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A large transcript whose only naming event is the last line: the scan must
    # find it from the end via a bounded read, not by reading the whole file
    # (the resume path runs this for every transcript in a slug, so a full
    # forward read of accumulated history is what stalls reconnect).
    f = tmp_path / "s.jsonl"
    pad = "x" * 1000
    lines = [f'{{"type":"user","message":"{pad}"}}' for _ in range(300)]
    lines.append('{"type":"custom-title","customTitle":"id:09:z","sessionId":"s"}')
    f.write_text("\n".join(lines) + "\n")
    total = f.stat().st_size
    assert total > 128 * 1024  # comfortably larger than one tail block

    consumed = 0
    real_open: Any = type(f).open

    class _CountingFile:
        # Counts bytes/chars consumed however the implementation reads — line
        # iteration (a forward scan) or block reads (a tail scan) — so the
        # assertion below measures behavior, not a particular read strategy.
        def __init__(self, fh: Any) -> None:
            self._fh = fh

        def read(self, *a: Any, **k: Any) -> Any:
            nonlocal consumed
            data = self._fh.read(*a, **k)
            consumed += len(data)
            return data

        def __iter__(self) -> Any:
            nonlocal consumed
            for line in self._fh:
                consumed += len(line)
                yield line

        def __getattr__(self, name: str) -> Any:
            return getattr(self._fh, name)

        def __enter__(self) -> _CountingFile:
            self._fh.__enter__()
            return self

        def __exit__(self, *exc: Any) -> Any:
            return self._fh.__exit__(*exc)

    def counting_open(self: Path, *a: Any, **k: Any) -> _CountingFile:
        return _CountingFile(real_open(self, *a, **k))

    monkeypatch.setattr(type(f), "open", counting_open)
    assert r._last_session_name(f) == "id:09:z"
    assert consumed < total  # bounded tail read, not the whole file


# --- custom-title naming events (issue #1493) ---


def test_last_session_name_reads_custom_title(tmp_path: Path) -> None:
    # Claude Code >= 2.1.16x records session names as custom-title events.
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"custom-title","customTitle":"id:01:a","sessionId":"s"}\n')
    assert r._last_session_name(f) == "id:01:a"


def test_last_session_name_mixed_event_types_last_wins(tmp_path: Path) -> None:
    # A transcript spanning the Claude rename carries both event types; the
    # last naming event in file order wins regardless of type.
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"agent-name","agentName":"id:01:a","sessionId":"s"}\n'
        '{"type":"custom-title","customTitle":"id:02:b","sessionId":"s"}\n'
    )
    assert r._last_session_name(f) == "id:02:b"
    f.write_text(
        '{"type":"custom-title","customTitle":"id:02:b","sessionId":"s"}\n'
        '{"type":"agent-name","agentName":"id:03:c","sessionId":"s"}\n'
    )
    assert r._last_session_name(f) == "id:03:c"


def test_last_session_name_ignores_non_string_custom_title(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"custom-title","customTitle":123}\n')
    assert r._last_session_name(f) is None


def test_last_session_name_ignores_non_string_type(tmp_path: Path) -> None:
    # a non-string (unhashable) "type" carrying the token must not crash
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":["agent-name"],"agentName":"id:01:a"}\n')
    assert r._last_session_name(f) is None


def test_last_session_name_ignores_other_type_with_custom_title_token(
    tmp_path: Path,
) -> None:
    # line carries the "custom-title" substring but is not a custom-title entry
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"user","custom-title":"not really"}\n')
    assert r._last_session_name(f) is None


def test_name_by_session_reads_custom_title(tmp_path: Path) -> None:
    slug = tmp_path / "slug"
    slug.mkdir()
    (slug / "s1.jsonl").write_text(
        '{"type":"custom-title","customTitle":"id:01:a","sessionId":"s1"}\n'
    )
    assert r.name_by_session(tmp_path) == {"s1": "id:01:a"}


def test_archive_session_archives_custom_title_named_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Archiving must work on transcripts named only via custom-title events
    # (previously a silent no-op) and must append a custom-title event so
    # Claude's own resume picker shows the archived label too.
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    t = projects / "s1.jsonl"
    t.write_text('{"type":"custom-title","customTitle":"vergil:01:p","sessionId":"s1"}\n')
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    r._archive_session("s1", "2026-06-07T00:00:00Z")
    assert r._last_session_name(t) == "archived@2026-06-07T00:00:00Z@vergil:01:p"
    appended = json.loads(t.read_text().splitlines()[-1])
    assert appended["type"] == "custom-title"


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


# --- roster_names ---


def test_roster_names_maps_session_to_name() -> None:
    roster: list[dict[str, object]] = [
        {"sessionId": "s1", "name": "vergil:02:p"},
        {"sessionId": "s2"},  # no name -> skipped
        {"name": "vergil:03:p"},  # no sessionId -> skipped
        {"sessionId": 5, "name": "vergil:04:p"},  # non-string sessionId -> skipped
    ]
    assert r.roster_names(roster) == {"s1": "vergil:02:p"}


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


def _resolve(
    identity: str,
    path: str,
    resume_name: str | None = None,
    label: str | None = None,
    **kw: object,
) -> int:
    defaults: dict[str, object] = {
        "fork": False,
        "fresh": False,
        "extra": [],
        "stale_days": 7,
        "archive_days": 14,
    }
    defaults.update(kw)
    return r.resolve(  # type: ignore[arg-type]
        identity, path, resume_name=resume_name, label=label, **defaults
    )


# --- --label (named creation, issue #2606) ---


class _FakeStore:
    """Minimal SessionStore stub exposing only resolve_name for the label path."""

    def __init__(self, result: object) -> None:
        self._result = result

    def resolve_name(self, name: str) -> object:  # noqa: ARG002
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def test_resolve_label_creates_when_name_free(
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No visible session holds the composed name -> create label:workspace.
    monkeypatch.setattr(r, "_store", lambda *_a: _FakeStore(None))
    assert _resolve("id", "vergil-project/tooling", label="epic-1") == 0
    assert capture_exec == [["claude", "-n", "epic-1:vergil-project/tooling"]]
    assert "Creating session epic-1:vergil-project/tooling" in capsys.readouterr().err


def test_resolve_label_rejects_existing_name(
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from vergil_tooling.lib.session_store import SessionInfo

    hit = SessionInfo("s1", "epic-1:p", "/w", active=True, last_active=None)
    monkeypatch.setattr(r, "_store", lambda *_a: _FakeStore(hit))
    assert _resolve("id", "p", label="epic-1") == 1
    assert capture_exec == []  # uniqueness collision -> never execs
    assert "already exists" in capsys.readouterr().err


def test_resolve_label_rejects_ambiguous_name(
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Two live sessions hold the name -> the seam fails loud; still a collision.
    monkeypatch.setattr(r, "_store", lambda *_a: _FakeStore(r.AmbiguousSessionError("boom")))
    assert _resolve("id", "p", label="epic-1") == 1
    assert capture_exec == []
    assert "already exists" in capsys.readouterr().err


def test_resolve_label_warns_off_convention_but_creates(
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(r, "_store", lambda *_a: _FakeStore(None))
    assert _resolve("id", "p", label="scratch") == 0
    assert capture_exec == [["claude", "-n", "scratch:p"]]
    assert "convention" in capsys.readouterr().err


def test_resolve_label_rejects_invalid_slug(
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A ':' in the label is structurally invalid -> fail loud before any store read.
    called = False

    def _boom(*_a: object) -> object:
        nonlocal called
        called = True
        return _FakeStore(None)

    monkeypatch.setattr(r, "_store", _boom)
    assert _resolve("id", "p", label="bad:name") == 1
    assert capture_exec == []
    assert called is False  # validation short-circuits before the uniqueness check
    assert "ERROR" in capsys.readouterr().err


def test_resolve_refuse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(r, "_read_state", lambda *_a: ({}, set(), {}))
    assert _resolve("id", "p", fork=True) == 1  # fork without slot refuses (no --slot)
    assert "ERROR" in capsys.readouterr().err


def test_resolve_fresh_creates_after_archiving(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    capture_exec: list[list[str]],
) -> None:
    # --fresh retains the prior behavior (later reconceived by the epic): the most
    # recent idle session is archived and a fresh one created in its place.
    now = 100 * DAY
    monkeypatch.setattr(
        r, "_read_state", lambda *_a: ({"s1": "vergil:01:p"}, set(), {"s1": now - 1 * DAY})
    )
    monkeypatch.setattr(r, "_now", lambda: now)
    monkeypatch.setattr(r, "_now_iso", lambda: "2026-05-30T00:00:00Z")
    archived: list[str] = []
    monkeypatch.setattr(r, "_archive_session", lambda sid, _ts: archived.append(sid))
    assert _resolve("vergil", "p", fresh=True) == 0
    assert archived == ["s1"]
    assert capture_exec == [["claude", "-n", "vergil:01:p"]]


def test_exec_claude_invokes_execvp(capture_exec: list[list[str]]) -> None:
    assert r._exec_claude(["-n", "x"]) == 0
    assert capture_exec == [["claude", "-n", "x"]]


# --- plan_resume (seam-based exact-name attach, issue #2607) ---


class _FakeStore:
    """A minimal SessionStore for plan_resume tests.

    ``resolve_name`` delegates to the pure ``resolve_over`` over the supplied
    rows, so it reproduces the seam's real fail-loud/None semantics without any
    transcript I/O.
    """

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def list_sessions(self) -> list[Any]:
        return list(self._rows)

    def resolve_name(self, name: str) -> Any:
        from vergil_tooling.lib.session_store import resolve_over

        return resolve_over(self._rows, name)

    def rename(self, session_id: str, new_name: str) -> None:  # pragma: no cover
        raise NotImplementedError


def _info(sid: str, name: str | None, active: bool, last: float | None, cwd: str = "/w") -> Any:
    from vergil_tooling.lib.session_store import SessionInfo

    return SessionInfo(sid, name, cwd, active, last)


def test_plan_resume_resolves_name_to_id() -> None:
    store = _FakeStore([_info("a", "epic-1:w", False, 10.0), _info("b", "epic-1:w", True, 5.0)])
    action = r.plan_resume(store, "epic-1:w")
    assert action.session_id == "b"  # the active one wins
    assert action.cwd == "/w"


def test_plan_resume_errors_when_absent(capsys: pytest.CaptureFixture[str]) -> None:
    store = _FakeStore([_info("a", "other:w", True, 1.0)])
    with pytest.raises(SystemExit):
        r.plan_resume(store, "nope:w")
    assert "no session named 'nope:w'" in capsys.readouterr().err


def test_plan_resume_fails_loud_on_ambiguous(capsys: pytest.CaptureFixture[str]) -> None:
    store = _FakeStore([_info("a", "epic-1:w", True, 10.0), _info("b", "epic-1:w", True, 20.0)])
    with pytest.raises(SystemExit):
        r.plan_resume(store, "epic-1:w")
    assert "live sessions" in capsys.readouterr().err


def test_resume_by_name_execs_with_resolved_id_and_cwd(
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --resume resolves the name to a session via the seam, chdirs to the
    # resolved session's cwd (memory-slug parity), then execs claude --resume
    # with -n re-asserting the exact name.
    store = _FakeStore([_info("b", "epic-1:w", False, 5.0, cwd="/work/repo")])
    monkeypatch.setattr(r, "ScrapeStore", lambda *_a, **_k: store)
    chdirs: list[str] = []
    monkeypatch.setattr(os, "chdir", lambda p: chdirs.append(p))
    assert _resolve("id", "p", resume_name="epic-1:w") == 0
    assert chdirs == ["/work/repo"]
    assert capture_exec == [["claude", "--resume", "b", "-n", "epic-1:w"]]
    assert "Resuming session epic-1:w" in capsys.readouterr().err


def test_no_verb_lists_and_guides(
    monkeypatch: pytest.MonkeyPatch,
    capture_exec: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # No --resume (and no fork/fresh): the old auto-resume-most-recent/create
    # default is gone. Print the recency list + the two verbs and exit non-zero;
    # never exec claude.
    store = _FakeStore([_info("b", "epic-1:w", False, 5.0), _info("a", "epic-2:w", True, 50.0)])
    monkeypatch.setattr(r, "ScrapeStore", lambda *_a, **_k: store)
    assert _resolve("id", "p") == 1
    err = capsys.readouterr().err
    assert "--label" in err
    assert "--resume" in err
    assert "epic-2:w" in err  # most-recent first
    assert capture_exec == []


def test_resume_by_name_skips_chdir_when_cwd_unknown(
    monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]
) -> None:
    # An idle session whose cwd could not be recovered (empty) is still resumed;
    # the resolver simply does not chdir (the launch cwd stands in).
    store = _FakeStore([_info("b", "epic-1:w", False, 5.0, cwd="")])
    monkeypatch.setattr(r, "ScrapeStore", lambda *_a, **_k: store)
    chdirs: list[str] = []
    monkeypatch.setattr(os, "chdir", lambda p: chdirs.append(p))
    assert _resolve("id", "p", resume_name="epic-1:w") == 0
    assert chdirs == []
    assert capture_exec == [["claude", "--resume", "b", "-n", "epic-1:w"]]


def test_no_verb_guide_when_no_sessions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # With nothing to list, the guide still names the two verbs and exits nonzero.
    store = _FakeStore([_info("a", None, True, 1.0)])  # unnamed rows are filtered out
    monkeypatch.setattr(r, "ScrapeStore", lambda *_a, **_k: store)
    assert _resolve("id", "p") == 1
    err = capsys.readouterr().err
    assert "No sessions yet" in err
    assert "--label" in err


def test_transcript_cwd_stops_at_line_cap(tmp_path: Path) -> None:
    # No cwd within the bounded line budget ⇒ None (a huge cwd-less transcript is
    # not read in full).
    f = tmp_path / "s.jsonl"
    f.write_text("".join('{"type":"user","message":"x"}\n' for _ in range(250)))
    assert r.transcript_cwd(f) is None


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
    monkeypatch.setattr(r, "_read_state", lambda *_a: ({"s1": "vergil:01:p"}, set(), {}))
    assert r.list_json() == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["state"] == "idle"


def test_archived_rows_skips_unparseable_original(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # archived label whose embedded "original" is not a valid slot name
    monkeypatch.setattr(
        r, "_read_state", lambda *_a: ({"a1": "archived@2026-05-01T00:00:00Z@garbage"}, set(), {})
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


def test_read_state_names_roster_session_without_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A live session named in the roster but with no transcript yet (a freshly
    # created session, zero turns) must still be named and reported active.
    (tmp_path / "projects").mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "100.json").write_text(
        '{"pid":100,"sessionId":"s1","name":"vergil:02:vergil-project/tooling"}'
    )
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(r, "_is_live", lambda _pid, _ps: True)
    names, active, _la = r._read_state()
    assert names == {"s1": "vergil:02:vergil-project/tooling"}
    assert active == {"s1"}


# --- main ---


def test_main_list_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(r, "_read_state", lambda *_a: ({}, set(), {}))
    assert r.main(["--list-json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_requires_identity_and_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert r.main(["--identity", "id"]) == 1
    assert "required" in capsys.readouterr().err


def test_main_dispatches_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_resolve(*args: object) -> int:
        seen["args"] = args
        return 0

    monkeypatch.setattr(r, "resolve", fake_resolve)
    code = r.main(
        [
            "--identity",
            "id",
            "--path",
            "p",
            "--fresh",
            "--stale-days",
            "7",
            "--archive-days",
            "14",
            "x",
        ]
    )
    assert code == 0
    # args order: identity, path, fork, fresh, extra, stale_days,
    # archive_days, resume_name, label
    assert seen["args"] == ("id", "p", False, True, ["x"], 7, 14, None, None)


def test_main_passes_resume_name(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(r, "resolve", lambda *args: seen.update(args=args) or 0)
    r.main(["--identity", "id", "--path", "p", "--resume-name", "epic-85-adhoc"])
    # resume_name is the penultimate positional arg (label is last).
    passed = seen["args"]
    assert isinstance(passed, tuple)
    assert passed[-2] == "epic-85-adhoc"
    assert passed[-1] is None  # label unset


def test_main_passes_label(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(r, "resolve", lambda *args: seen.update(args=args) or 0)
    r.main(["--identity", "id", "--path", "p", "--label", "epic-1"])
    passed = seen["args"]
    assert isinstance(passed, tuple)
    assert passed[-1] == "epic-1"  # label is the final positional arg


def test_main_strips_leading_double_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        r,
        "resolve",
        lambda *args: captured.update(extra=args[4]) or 0,  # noqa: ARG005
    )
    r.main(["--identity", "id", "--path", "p", "--", "claude", "--model", "opus"])
    assert captured["extra"] == ["claude", "--model", "opus"]


# --- cwd-scoped resolution (issue #1339) ---


def test_project_slug_encodes_path() -> None:
    # Claude encodes a cwd into a project slug by replacing every
    # non-alphanumeric character (including the leading slash and dots) with "-".
    assert r._project_slug("/work/tool") == "-work-tool"
    assert r._project_slug("/a.b/c") == "-a-b-c"


def test_name_by_session_scoped_to_one_slug(tmp_path: Path) -> None:
    (tmp_path / "-a").mkdir()
    (tmp_path / "-b").mkdir()
    (tmp_path / "-a" / "s1.jsonl").write_text(
        '{"type":"agent-name","agentName":"id:01:a","sessionId":"s1"}\n'
    )
    (tmp_path / "-b" / "s2.jsonl").write_text(
        '{"type":"agent-name","agentName":"id:01:b","sessionId":"s2"}\n'
    )
    # Scoped to "-a": only that slug's transcript is read.
    assert r.name_by_session(tmp_path, "-a") == {"s1": "id:01:a"}
    # No slug: full scan across every slug (unchanged behavior).
    assert r.name_by_session(tmp_path) == {"s1": "id:01:a", "s2": "id:01:b"}


def test_resume_by_name_scans_all_slugs_and_derives_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_exec: list[list[str]]
) -> None:
    # --resume resolves the exact name across every slug (not just the launch
    # cwd's) and chdirs to the resolved session's transcript-recorded cwd, so the
    # memory slug follows the session rather than any positional (#2607).
    claude = tmp_path / ".claude"
    projects = claude / "projects"
    (projects / "-work-tool").mkdir(parents=True)
    (projects / "-work-tool" / "good.jsonl").write_text(
        '{"type":"user","cwd":"/work/tool","timestamp":"2026-05-02T00:00:00.000Z"}\n'
        '{"type":"custom-title","customTitle":"epic-7:tool","sessionId":"good"}\n'
    )
    (claude / "sessions").mkdir()
    monkeypatch.setattr(r, "_claude_dir", lambda: claude)
    chdirs: list[str] = []
    monkeypatch.setattr(os, "chdir", lambda p: chdirs.append(p))
    assert _resolve("id", "somewhere-else", resume_name="epic-7:tool") == 0
    assert chdirs == ["/work/tool"]
    assert capture_exec == [["claude", "--resume", "good", "-n", "epic-7:tool"]]


# --- _resolve_target / _resolve_instance named-instance tests (issue #1831) ---

_REPO_TOML_HEAD = """\
[project]
repository-type = "tooling"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""

_NAMED_INSTANCE_OFF_PLATFORM_VM = """
[vm]
backend = "off-platform"
provider = "gcp"
region = "us-central1"
instance = "n2-standard-8"
volume = "300GiB"

[vm.vergil-user.instances.cloud-x86]
provider = "gcp"
region = "us-central1"
instance = "n2-standard-8"
volume = "300GiB"
"""

_NAMED_INSTANCE_LOCAL_VM = """
[vm]
packages = ["qemu-system-x86"]

[vm.vergil-user.instances.rdqm-rhel]
"""


def _make_args(
    *,
    workspace: str | None = None,
    name: str | None = None,
    identity: str | None = None,
    command: str = "create",
    config: Path | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        config=config,
        identity=identity,
        workspace=workspace,
        name=name,
        command=command,
    )


def _make_identity_config(tmp_path: Path, projects: Path) -> Path:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent(f"""\
        default_identity = "vergil-user"
        vergil = "v2.0"

        [identities.vergil-user]
        vm_instance = "vergil-user"
        projects_dir = "{projects}"
    """)
    )
    return p


def _make_repo(projects: Path, org: str, repo: str, vm_section: str = "") -> None:
    repo_dir = projects / org / repo
    repo_dir.mkdir(parents=True)
    (repo_dir / "vergil.toml").write_text(_REPO_TOML_HEAD + vm_section)


@pytest.fixture()
def named_instance_config(tmp_path: Path) -> Path:
    """Identity config with lmf/mq declaring an off-platform named instance 'cloud-x86'."""
    projects = tmp_path / "projects"
    _make_repo(projects, "lmf", "mq", _NAMED_INSTANCE_OFF_PLATFORM_VM)
    return _make_identity_config(tmp_path, projects)


@pytest.fixture()
def named_instance_local_config(tmp_path: Path) -> Path:
    """Identity config with lmf/mq declaring a local named instance 'rdqm-rhel'."""
    projects = tmp_path / "projects"
    _make_repo(projects, "lmf", "mq", _NAMED_INSTANCE_LOCAL_VM)
    return _make_identity_config(tmp_path, projects)


def test_resolve_target_named_instance(named_instance_config: Path) -> None:
    from vergil_tooling.bin.vrg_vm import Target, _resolve_target
    from vergil_tooling.lib.vm_cloud import OffPlatformBackend

    args = _make_args(
        workspace="lmf/mq", name="cloud-x86", identity="vergil-user", config=named_instance_config
    )
    target = _resolve_target(args)
    assert isinstance(target, Target)
    assert target.instance_name_arg == "cloud-x86"
    assert target.instance == "vergil-user.lmf.mq.cloud-x86"
    assert target.spec.off_platform
    assert isinstance(target.backend, OffPlatformBackend)
    assert target.backend.slug == "vergil-user--lmf--mq--cloud-x86"


def test_destroy_volume_named_targets_instance_volume(named_instance_config: Path) -> None:
    # destroy-volume --name must resolve the NAMED instance's volume state, not the
    # default's — it is irreversible and billable.
    from vergil_tooling.bin.vrg_vm import _resolve_target
    from vergil_tooling.lib.vm_cloud import OffPlatformBackend

    args = _make_args(
        command="destroy-volume",
        workspace="lmf/mq",
        name="cloud-x86",
        identity="vergil-user",
        config=named_instance_config,
    )
    target = _resolve_target(args)
    assert isinstance(target.backend, OffPlatformBackend)
    assert target.backend.state_key == "vergil-user--lmf--mq--cloud-x86"


def test_update_named_resolves_instance_lima_name(named_instance_local_config: Path) -> None:
    # update (single) routes through _resolve_instance, which must honor --name.
    from vergil_tooling.bin.vrg_vm import _resolve_instance

    args = _make_args(
        command="update",
        workspace="lmf/mq",
        name="rdqm-rhel",
        identity="vergil-user",
        config=named_instance_local_config,
    )
    _name, _identity, _config, instance = _resolve_instance(args)
    assert instance == "vergil-user.lmf.mq.rdqm-rhel"


def test_resolve_target_name_without_workspace_raises(named_instance_config: Path) -> None:
    # --name without an org/repo workspace must raise SpecError.
    from vergil_tooling.bin.vrg_vm import _resolve_target
    from vergil_tooling.lib.vm_spec import SpecError

    args = _make_args(
        workspace=None, name="cloud-x86", identity="vergil-user", config=named_instance_config
    )
    with pytest.raises(SpecError, match="--name requires"):
        _resolve_target(args)
