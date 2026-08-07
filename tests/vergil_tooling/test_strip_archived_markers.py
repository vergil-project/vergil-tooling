"""Tests for the one-time cosmetic ``archived@`` strip script (epic #230, Task 7).

The script lives in ``scripts/dev/`` (a hyphenated filename, not an importable
package module), so it is loaded here via :mod:`importlib.util` and its pure
planning core plus the fake-store-driven ``run`` are exercised directly. No real
transcript is touched — a fake :class:`SessionStore` records the renames.
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib.session_store import SessionInfo

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "dev" / "strip-archived-markers.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("strip_archived_markers", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so the module's ``@dataclass`` can introspect its own
    # (string, ``from __future__ import annotations``) field annotations.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


strip = _load_script()


class FakeStore:
    """A :class:`SessionStore` that records renames instead of touching a transcript."""

    def __init__(self, rows: list[SessionInfo]) -> None:
        self._rows = rows
        self.renames: list[tuple[str, str]] = []

    def list_sessions(self) -> list[SessionInfo]:
        return list(self._rows)

    def resolve_name(self, name: str) -> SessionInfo | None:  # pragma: no cover - unused here
        raise NotImplementedError

    def rename(self, session_id: str, new_name: str) -> None:
        self.renames.append((session_id, new_name))


def _row(sid: str, name: str | None) -> SessionInfo:
    return SessionInfo(session_id=sid, name=name, cwd="/w", active=False, last_active=1.0)


# --- strip_archived_name (pure) ---------------------------------------------


def test_strip_recovers_original_name() -> None:
    assert strip.strip_archived_name("archived@20260101T00@epic-1:w") == "epic-1:w"


def test_strip_preserves_at_in_original() -> None:
    # The original name may itself contain '@'; only the first two '@' delimit.
    assert strip.strip_archived_name("archived@ts@user@host:w") == "user@host:w"


def test_strip_returns_none_for_clean_name() -> None:
    assert strip.strip_archived_name("epic-1:w") is None


def test_strip_returns_none_for_malformed_marker() -> None:
    # Prefix present but no original segment after the timestamp.
    assert strip.strip_archived_name("archived@tsonly") is None
    assert strip.strip_archived_name("archived@ts@") is None


def test_strip_is_idempotent_on_recovered_name() -> None:
    # Stripping an already-clean name a second time is a no-op.
    recovered = strip.strip_archived_name("archived@ts@epic-1:w")
    assert recovered is not None
    assert strip.strip_archived_name(recovered) is None


# --- plan_strips (pure) -----------------------------------------------------


def test_plan_selects_only_archived_rows() -> None:
    rows = [
        _row("a", "archived@ts@epic-1:w"),
        _row("b", "epic-2:w"),
        _row("c", None),
        _row("d", "archived@ts2@adhoc-x:w"),
    ]
    strips = strip.plan_strips(rows)
    assert [(s.session_id, s.new_name) for s in strips] == [
        ("a", "epic-1:w"),
        ("d", "adhoc-x:w"),
    ]


# --- run (dry-run vs apply) -------------------------------------------------


def test_run_dry_run_renames_nothing() -> None:
    store = FakeStore([_row("a", "archived@ts@epic-1:w")])
    out = io.StringIO()
    rc = strip.run(store, apply=False, out=out)
    assert rc == 0
    assert store.renames == []
    text = out.getvalue()
    assert "epic-1:w" in text
    assert "--apply" in text


def test_run_apply_renames_each() -> None:
    store = FakeStore([_row("a", "archived@ts@epic-1:w"), _row("d", "archived@ts2@adhoc-x:w")])
    out = io.StringIO()
    rc = strip.run(store, apply=True, out=out)
    assert rc == 0
    assert store.renames == [("a", "epic-1:w"), ("d", "adhoc-x:w")]


def test_run_reports_nothing_when_clean() -> None:
    store = FakeStore([_row("b", "epic-2:w"), _row("c", None)])
    out = io.StringIO()
    rc = strip.run(store, apply=True, out=out)
    assert rc == 0
    assert store.renames == []
    assert "nothing" in out.getvalue().lower()


# --- main (CLI glue) --------------------------------------------------------


def test_main_dry_run_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _Store:
        def __init__(self, claude_dir: Path, slug: str | None = None) -> None:
            captured["claude_dir"] = claude_dir
            captured["slug"] = slug

        def list_sessions(self) -> list[SessionInfo]:
            return [_row("a", "archived@ts@epic-1:w")]

        def rename(self, session_id: str, new_name: str) -> None:  # pragma: no cover
            captured["renamed"] = True

    monkeypatch.setattr(strip, "ScrapeStore", _Store)
    rc = strip.main(["--claude-dir", str(tmp_path)])
    assert rc == 0
    assert captured["claude_dir"] == tmp_path
    assert "renamed" not in captured  # dry-run: no rename call


def test_main_apply_passes_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    renamed: list[tuple[str, str]] = []

    class _Store:
        def __init__(self, claude_dir: Path, slug: str | None = None) -> None:
            pass

        def list_sessions(self) -> list[SessionInfo]:
            return [_row("a", "archived@ts@epic-1:w")]

        def rename(self, session_id: str, new_name: str) -> None:
            renamed.append((session_id, new_name))

    monkeypatch.setattr(strip, "ScrapeStore", _Store)
    rc = strip.main(["--apply"])
    assert rc == 0
    assert renamed == [("a", "epic-1:w")]
