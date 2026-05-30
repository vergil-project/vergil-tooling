# Stale-Session Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add age-aware session resume to `vrg-vm session` — silent resume when recent, a stale prompt in a warn band, automatic archive-via-relabel of long-idle sessions, and `list --sessions` state filters with an age column.

**Architecture:** Extends the merged deterministic-naming feature. Pure logic lives in `lib/session.py` (naming, age bands, the slot planner); I/O lives in `bin/vrg_vm_resolve.py` (in-VM: tail-read age, relabel-archive, TTY prompt, sweep) and `bin/vrg_vm.py` (host CLI). Config lives in `lib/identity.py`. Two thresholds (`session_stale_days`=7, `session_archive_days`=14) split idle sessions into fresh / warn / stale bands.

**Tech Stack:** Python 3.12, pytest (100% branch coverage enforced by `vrg-validate`), mypy + ty + ruff. Run everything via `vrg-container-run -- uv run …` from the worktree.

**Spec:** `vergil-vm/docs/specs/2026-05-30-stale-session-lifecycle-design.md` (vergil-vm #82). Issue: vergil-tooling #1323.

**Conventions:** TDD red→green, commit per task with `vrg-commit`. Run `vrg-container-run -- uv run ruff format src/ tests/` before validating. Final gate: `vrg-container-run -- uv run vrg-validate`.

---

## File Structure

- `src/vergil_tooling/lib/identity.py` — add `session_stale_days` / `session_archive_days` to `Identity` + `IdentityConfig`, parse, resolve helpers, validation.
- `src/vergil_tooling/lib/session.py` — `archived@` parsing; `last_active` on `Slot`/`SessionRow`; age-band classifier; `plan_session()` returning a `SessionPlan` (auto-archive list + follow-on action).
- `src/vergil_tooling/bin/vrg_vm_resolve.py` — tail-read age, relabel-archive append, TTY-gated prompt, auto-archive sweep, wire age into detection, age/state in `--list-json`.
- `src/vergil_tooling/bin/vrg_vm.py` — `--fresh` flag, `list --sessions` filters + age column, plumb thresholds.
- Tests mirror each under `tests/vergil_tooling/`.

---

## Task 1: Config — `session_stale_days` / `session_archive_days`

**Files:**
- Modify: `src/vergil_tooling/lib/identity.py`
- Test: `tests/vergil_tooling/test_identity.py`

Mirrors the existing `model` / `resolve_model` cascade pattern.

- [ ] **Step 1: Write failing tests**

```python
def test_session_thresholds_parsed(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent("""\
        session_stale_days = 5
        session_archive_days = 30
        [identities.vergil]
        vm_instance = "vergil-agent"
        session_stale_days = 2
    """))
    cfg = load_config(p)
    assert cfg.session_stale_days == 5
    assert cfg.session_archive_days == 30
    assert cfg.identities["vergil"].session_stale_days == 2
    assert cfg.identities["vergil"].session_archive_days is None


def test_resolve_session_thresholds_cascade() -> None:
    cfg = IdentityConfig(identities={}, session_stale_days=5, session_archive_days=30)
    ident = Identity(vm_instance="x", session_stale_days=2)
    assert resolve_session_stale_days(cfg, ident) == 2          # identity wins
    assert resolve_session_archive_days(cfg, ident) == 30       # falls back to config


def test_resolve_session_thresholds_builtin_defaults() -> None:
    cfg = IdentityConfig(identities={})
    ident = Identity(vm_instance="x")
    assert resolve_session_stale_days(cfg, ident) == 7
    assert resolve_session_archive_days(cfg, ident) == 14


def test_session_archive_days_zero_disables(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent("""\
        session_archive_days = 0
        [identities.vergil]
        vm_instance = "vergil-agent"
    """))
    assert load_config(p).session_archive_days == 0


def test_session_archive_days_must_exceed_stale(tmp_path: Path) -> None:
    p = tmp_path / "identities.toml"
    p.write_text(textwrap.dedent("""\
        [identities.vergil]
        vm_instance = "vergil-agent"
        session_stale_days = 10
        session_archive_days = 5
    """))
    with pytest.raises(SystemExit):
        load_config(p)
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_identity.py -q`
Expected: FAIL (unknown kwargs / missing functions).

- [ ] **Step 3: Implement**

Add fields (trailing, with defaults to preserve positional construction):

```python
# in @dataclass class Identity (after model):
    session_stale_days: int | None = None
    session_archive_days: int | None = None

# in @dataclass class IdentityConfig (after model):
    session_stale_days: int = 7
    session_archive_days: int = 14
```

Constants near top of module:

```python
_DEFAULT_SESSION_STALE_DAYS = 7
_DEFAULT_SESSION_ARCHIVE_DAYS = 14
```

In `load_config`, read top-level keys (defaulting to the constants) and per-identity keys (defaulting to `None`):

```python
    session_stale_days = raw.get("session_stale_days", _DEFAULT_SESSION_STALE_DAYS)
    session_archive_days = raw.get("session_archive_days", _DEFAULT_SESSION_ARCHIVE_DAYS)
```
```python
            session_stale_days=data.get("session_stale_days"),
            session_archive_days=data.get("session_archive_days"),
```
Pass `session_stale_days=session_stale_days, session_archive_days=session_archive_days` into the `IdentityConfig(...)` constructor.

Add a validation call inside the per-identity loop (after `_validate_identity_resources`):

```python
        _validate_session_thresholds(name, identities[name], session_stale_days, session_archive_days)
```

```python
def _validate_session_thresholds(
    name: str, identity: Identity, cfg_stale: int, cfg_archive: int
) -> None:
    stale = identity.session_stale_days if identity.session_stale_days is not None else cfg_stale
    archive = (
        identity.session_archive_days
        if identity.session_archive_days is not None
        else cfg_archive
    )
    if archive != 0 and archive <= stale:
        print(
            f"ERROR: identity '{name}': session_archive_days ({archive}) must be 0 "
            f"or greater than session_stale_days ({stale})",
            file=sys.stderr,
        )
        raise SystemExit(1)
```

Resolve helpers (next to `resolve_model`):

```python
def resolve_session_stale_days(config: IdentityConfig, identity: Identity) -> int:
    if identity.session_stale_days is not None:
        return identity.session_stale_days
    return config.session_stale_days


def resolve_session_archive_days(config: IdentityConfig, identity: Identity) -> int:
    if identity.session_archive_days is not None:
        return identity.session_archive_days
    return config.session_archive_days
```

- [ ] **Step 4: Run tests, verify pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_identity.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "add session_stale_days/session_archive_days config" --body "Cascading thresholds (identity -> config -> 7/14) with validation that archive is 0 or > stale. Ref #1323."
```

---

## Task 2: `session.py` — archived-name parsing

**Files:**
- Modify: `src/vergil_tooling/lib/session.py`
- Test: `tests/vergil_tooling/test_session.py`

- [ ] **Step 1: Write failing tests**

```python
from vergil_tooling.lib.session import make_archived_name, parse_archived

def test_make_archived_name() -> None:
    assert (
        make_archived_name("vergil:01:a/b", "2026-05-30T14:23:07Z")
        == "archived@2026-05-30T14:23:07Z@vergil:01:a/b"
    )


def test_parse_name_rejects_archived_prefix() -> None:
    # would otherwise mis-parse into a bogus slot
    assert parse_name("archived@2026-05-30T14:23:07Z@vergil:01:a/b") is None


def test_parse_archived_roundtrip() -> None:
    label = "archived@2026-05-30T14:23:07Z@vergil:01:a/b"
    assert parse_archived(label) == ("2026-05-30T14:23:07Z", "vergil:01:a/b")


def test_parse_archived_path_with_at_sign() -> None:
    label = "archived@2026-05-30T14:23:07Z@vergil:01:clients/acme@2024"
    assert parse_archived(label) == ("2026-05-30T14:23:07Z", "vergil:01:clients/acme@2024")


def test_parse_archived_returns_none_for_non_archived() -> None:
    assert parse_archived("vergil:01:a/b") is None
    assert parse_archived("archived@only-two-parts") is None
```

- [ ] **Step 2: Run, verify fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_session.py -q` → FAIL.

- [ ] **Step 3: Implement**

Add the guard as the FIRST lines of `parse_name` (before the `:` split):

```python
    if name.startswith(_ARCHIVED_PREFIX):
        return None
```

Add near the constants:

```python
_ARCHIVED_PREFIX = "archived@"


def make_archived_name(name: str, timestamp: str) -> str:
    """Archived label: ``archived@<timestamp>@<original-name>``."""
    return f"{_ARCHIVED_PREFIX}{timestamp}@{name}"


def parse_archived(name: str) -> tuple[str, str] | None:
    """Parse an archived label into ``(timestamp, original_name)`` or ``None``.

    Splits on the first two ``@`` so a workspace path containing ``@`` is safe.
    """
    if not name.startswith(_ARCHIVED_PREFIX):
        return None
    parts = name.split("@", 2)
    if len(parts) != 3:
        return None
    return parts[1], parts[2]
```

- [ ] **Step 4: Run, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "parse archived session labels; guard parse_name" --body "parse_name rejects archived@ prefix (it would otherwise mis-parse into a bogus slot); add make_archived_name/parse_archived. Ref #1323."
```

---

## Task 3: `session.py` — `last_active` on Slot/SessionRow

**Files:**
- Modify: `src/vergil_tooling/lib/session.py`
- Test: `tests/vergil_tooling/test_session.py`

`last_active` is epoch seconds (`float`) or `None` (age unknown). Added as a trailing field so existing positional construction in tests still works.

- [ ] **Step 1: Write failing tests**

```python
def test_build_slots_attaches_last_active() -> None:
    names = {"s1": "vergil:01:p"}
    slots = build_slots("vergil", "p", names, active_sessions=set(), last_active={"s1": 1000.0})
    assert slots[1].last_active == 1000.0


def test_build_slots_last_active_defaults_none() -> None:
    slots = build_slots("vergil", "p", {"s1": "vergil:01:p"}, active_sessions=set())
    assert slots[1].last_active is None


def test_list_rows_attaches_last_active() -> None:
    rows = list_rows({"s1": "vergil:01:p"}, active_sessions=set(), last_active={"s1": 5.0})
    assert rows[0].last_active == 5.0
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

Add trailing field to both dataclasses:

```python
# class Slot:
    last_active: float | None = None
# class SessionRow:
    last_active: float | None = None
```

Thread an optional `last_active` map through. Update `_merge_slot`, `build_slots`, `list_rows`:

```python
def _merge_slot(
    slots: dict[int, Slot], slot: int, session_id: str, active: bool, last_active: float | None
) -> None:
    existing = slots.get(slot)
    if existing is None or (active and not existing.active):
        slots[slot] = Slot(slot, session_id, active, last_active)


def build_slots(
    identity: str,
    path: str,
    name_by_session: dict[str, str],
    active_sessions: set[str],
    last_active: dict[str, float] | None = None,
) -> dict[int, Slot]:
    la = last_active or {}
    slots: dict[int, Slot] = {}
    for session_id, name in name_by_session.items():
        parsed = parse_name(name)
        if parsed is None:
            continue
        row_identity, slot, row_path = parsed
        if row_identity != identity or row_path != path:
            continue
        _merge_slot(slots, slot, session_id, session_id in active_sessions, la.get(session_id))
    return slots
```

In `list_rows`, add the same `last_active: dict[str, float] | None = None` param, `la = last_active or {}`, and construct `SessionRow(identity, slot, path, session_id, active, la.get(session_id))`.

- [ ] **Step 4: Run, verify pass.** Existing Slot/SessionRow tests still pass (trailing defaults). → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "add last_active to Slot and SessionRow" --body "Thread an optional last_active map through build_slots/list_rows. Ref #1323."
```

---

## Task 4: `session.py` — age-band classifier

**Files:**
- Modify: `src/vergil_tooling/lib/session.py`
- Test: `tests/vergil_tooling/test_session.py`

- [ ] **Step 1: Write failing tests**

```python
from vergil_tooling.lib.session import AgeBand, classify_age

DAY = 86400.0

def test_classify_age_fresh() -> None:
    assert classify_age(now=100 * DAY, last_active=99 * DAY, stale_days=7, archive_days=14) == AgeBand.FRESH

def test_classify_age_warn() -> None:
    assert classify_age(now=100 * DAY, last_active=90 * DAY, stale_days=7, archive_days=14) == AgeBand.WARN

def test_classify_age_stale() -> None:
    assert classify_age(now=100 * DAY, last_active=80 * DAY, stale_days=7, archive_days=14) == AgeBand.STALE

def test_classify_age_unknown_is_fresh() -> None:
    # never auto-archive something we cannot date
    assert classify_age(now=100 * DAY, last_active=None, stale_days=7, archive_days=14) == AgeBand.FRESH

def test_classify_age_archive_zero_never_stale() -> None:
    assert classify_age(now=100 * DAY, last_active=0.0, stale_days=7, archive_days=0) == AgeBand.WARN
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

```python
import enum

class AgeBand(enum.Enum):
    FRESH = "fresh"
    WARN = "warn"
    STALE = "stale"


def classify_age(
    now: float, last_active: float | None, stale_days: int, archive_days: int
) -> AgeBand:
    """Classify a session's age. Unknown age is treated as FRESH (never swept)."""
    if last_active is None:
        return AgeBand.FRESH
    age_days = (now - last_active) / 86400.0
    if age_days < stale_days:
        return AgeBand.FRESH
    if archive_days != 0 and age_days >= archive_days:
        return AgeBand.STALE
    return AgeBand.WARN
```

- [ ] **Step 4: Run, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "add session age-band classifier" --body "FRESH/WARN/STALE by stale_days/archive_days; unknown age = FRESH; archive_days=0 disables STALE. Ref #1323."
```

---

## Task 5: `session.py` — `plan_session()` (the planner)

**Files:**
- Modify: `src/vergil_tooling/lib/session.py`
- Test: `tests/vergil_tooling/test_session.py`

Returns a `SessionPlan(auto_archive, action)`. `auto_archive` is the cold idle slots to relabel-archive first. `action` is what to do next.

- [ ] **Step 1: Write failing tests**

```python
from vergil_tooling.lib.session import (
    PromptStale, SessionPlan, plan_session,
)

DAY = 86400.0

def _slot(n, sid, active=False, age_days=0.0, now=100 * DAY):
    return Slot(n, sid, active, now - age_days * DAY)

NOW = 100 * DAY

def test_plan_resume_most_recent_idle_fresh() -> None:
    slots = {1: _slot(1, "old", age_days=3), 2: _slot(2, "new", age_days=0.1)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False)
    assert plan.auto_archive == []
    assert plan.action == Resume("new")  # most-recent, not lowest

def test_plan_warn_band_prompts() -> None:
    slots = {1: _slot(1, "s1", age_days=9)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False)
    assert plan.auto_archive == []
    assert plan.action == PromptStale("s1", "vergil:01:p", 9)

def test_plan_stale_is_swept_then_fresh() -> None:
    slots = {1: _slot(1, "s1", age_days=20)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False)
    assert plan.auto_archive == [slots[1]]
    assert plan.action == Create("vergil:01:p")  # slot reclaimed

def test_plan_sweep_only_stale_keeps_fresh() -> None:
    slots = {1: _slot(1, "s1", age_days=20), 2: _slot(2, "s2", age_days=0.1)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False)
    assert plan.auto_archive == [slots[1]]
    assert plan.action == Resume("s2")

def test_plan_never_sweeps_active() -> None:
    slots = {1: _slot(1, "s1", active=True, age_days=20)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False)
    assert plan.auto_archive == []           # active never swept
    assert plan.action == Create("vergil:02:p")  # 01 active -> next free

def test_plan_explicit_slot_no_sweep_no_prompt() -> None:
    slots = {1: _slot(1, "s1", age_days=20), 2: _slot(2, "s2", age_days=20)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=1, fork=False)
    assert plan.auto_archive == []           # surgical: no sweep
    assert plan.action == Resume("s1")       # resume exactly, no prompt

def test_plan_fresh_archives_target_then_creates() -> None:
    slots = {1: _slot(1, "s1", age_days=1)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=1, fork=True)  # fork flag carries --fresh? see note
```

> Implementation note for the worker: `--fresh` is a distinct flag from `--fork`. Represent it with a new `fresh: bool` parameter on `plan_session` (do NOT overload `fork`). Replace the last test above with the `fresh=True` form below.

```python
def test_plan_fresh_with_slot_archives_then_creates() -> None:
    slots = {1: _slot(1, "s1", age_days=1)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=1, fork=False, fresh=True)
    assert plan.auto_archive == [slots[1]]
    assert plan.action == Create("vergil:01:p")

def test_plan_fresh_no_slot_archives_most_recent_idle() -> None:
    slots = {1: _slot(1, "s1", age_days=5), 2: _slot(2, "s2", age_days=1)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False, fresh=True)
    assert plan.auto_archive == [slots[2]]   # most-recent idle reclaimed
    assert plan.action == Create("vergil:02:p")

def test_plan_fresh_no_idle_creates_lowest_free() -> None:
    plan = plan_session("vergil", "p", {}, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False, fresh=True)
    assert plan.auto_archive == []
    assert plan.action == Create("vergil:01:p")

def test_plan_fresh_active_slot_refused() -> None:
    slots = {1: _slot(1, "s1", active=True, age_days=1)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=1, fork=False, fresh=True)
    assert isinstance(plan.action, Refuse)
    assert plan.auto_archive == []

def test_plan_fork_unchanged() -> None:
    slots = {1: _slot(1, "s1", active=True, age_days=1)}
    plan = plan_session("vergil", "p", slots, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=1, fork=True, fresh=False)
    assert plan.auto_archive == []
    assert plan.action == Fork("s1", "vergil:02:p")

def test_plan_no_slots_creates_first() -> None:
    plan = plan_session("vergil", "p", {}, now=NOW, stale_days=7, archive_days=14,
                        requested_slot=None, fork=False)
    assert plan.action == Create("vergil:01:p")
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

```python
@dataclass(frozen=True)
class PromptStale:
    """Warn-band: resolver must prompt resume/fresh/cancel, then act."""

    session_id: str   # resume this on [r]
    name: str         # reclaim this name on [f] (archive session_id, create name)
    age_days: int


PlanAction = Create | Resume | Fork | Refuse | PromptStale


@dataclass(frozen=True)
class SessionPlan:
    auto_archive: list[Slot]
    action: PlanAction


def _idle_by_recency(slots: dict[int, Slot]) -> list[Slot]:
    """Idle slots, most-recently-active first (None age sorts oldest)."""
    idle = [s for s in slots.values() if not s.active]
    return sorted(idle, key=lambda s: (s.last_active is not None, s.last_active or 0.0), reverse=True)


def plan_session(
    identity: str,
    path: str,
    slots: dict[int, Slot],
    now: float,
    stale_days: int,
    archive_days: int,
    requested_slot: int | None = None,
    fork: bool = False,
    fresh: bool = False,
) -> SessionPlan:
    # --fork: unchanged behavior, no sweep.
    if fork:
        return SessionPlan([], _select_fork(identity, path, slots, requested_slot))

    # --fresh: archive the target (cold) and create fresh in that slot. No sweep.
    if fresh:
        return _plan_fresh(identity, path, slots, requested_slot)

    # Explicit --slot N: surgical, no sweep, no prompt.
    if requested_slot is not None:
        return SessionPlan([], _select_explicit(identity, path, slots, requested_slot))

    # Default auto path: sweep stale cold idle, then pick most-recent of the rest.
    sweep = [
        s
        for s in slots.values()
        if not s.active
        and classify_age(now, s.last_active, stale_days, archive_days) == AgeBand.STALE
    ]
    swept_ids = {s.slot for s in sweep}
    remaining = {n: s for n, s in slots.items() if n not in swept_ids}
    return SessionPlan(sweep, _select_auto(identity, path, remaining, now, stale_days, archive_days))


def _select_auto(
    identity: str,
    path: str,
    slots: dict[int, Slot],
    now: float,
    stale_days: int,
    archive_days: int,
) -> PlanAction:
    for slot in _idle_by_recency(slots):
        band = classify_age(now, slot.last_active, stale_days, archive_days)
        if band == AgeBand.FRESH:
            return Resume(slot.session_id)
        # WARN (STALE was already swept out before this call)
        return PromptStale(slot.session_id, make_name(identity, slot.slot, path),
                           int((now - (slot.last_active or now)) / 86400.0))
    free = _lowest_free(slots)
    if free is None:
        return _all_in_use(identity, path)
    return Create(make_name(identity, free, path))


def _plan_fresh(
    identity: str, path: str, slots: dict[int, Slot], requested_slot: int | None
) -> SessionPlan:
    if requested_slot is not None:
        if not SLOT_MIN <= requested_slot <= SLOT_MAX:
            return SessionPlan([], _bad_range())
        target = slots.get(requested_slot)
        slot_no = requested_slot
    else:
        idle = _idle_by_recency(slots)
        target = idle[0] if idle else None
        slot_no = target.slot if target else (_lowest_free(slots) or 0)
    if slot_no == 0:
        return SessionPlan([], _all_in_use(identity, path))
    if target is not None and target.active:
        return SessionPlan([], Refuse(f"slot {slot_no:02d} is active; cannot start fresh over a live session"))
    archive = [target] if target is not None else []
    return SessionPlan(archive, Create(make_name(identity, slot_no, path)))
```

> Worker note: `_select_default` (the old lowest-idle path) is now unused by `plan_session` but may still be referenced by `select()`. Keep `select()` and its helpers (`_select_explicit`, `_select_fork`) — `plan_session` reuses them. You may leave `_select_default` if other callers/tests use it, or remove it and its test if not (check `grep -rn _select_default`).

- [ ] **Step 4: Run, verify pass** → PASS. Also run the full session test file.

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_session.py -q`

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "add plan_session: bands, sweep, most-recent resume, --fresh" --body "Pure planner returning auto-archive list + action (Resume/Create/Fork/Refuse/PromptStale). Ref #1323."
```

---

## Task 6: Resolver — age source (tail-read last timestamped entry)

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm_resolve.py`
- Test: `tests/vergil_tooling/test_vrg_vm_resolve.py`

- [ ] **Step 1: Write failing tests**

```python
import datetime

def test_last_activity_reads_last_timestamped_entry(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text(
        '{"type":"user","timestamp":"2026-05-01T00:00:00.000Z"}\n'
        '{"type":"assistant","timestamp":"2026-05-02T00:00:00.000Z"}\n'
        '{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s"}\n'  # no timestamp
    )
    ts = r._last_activity(f)
    assert ts == datetime.datetime(2026, 5, 2, tzinfo=datetime.timezone.utc).timestamp()

def test_last_activity_none_when_no_timestamp(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_text('{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s"}\n')
    assert r._last_activity(f) is None

def test_last_activity_missing_file(tmp_path: Path) -> None:
    assert r._last_activity(tmp_path / "nope.jsonl") is None

def test_last_activity_handles_large_file_via_tail(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    lines = ['{"type":"user","timestamp":"2020-01-01T00:00:00.000Z","pad":"%s"}' % ("x" * 1000)
             for _ in range(5000)]
    lines.append('{"type":"assistant","timestamp":"2026-05-30T12:00:00.000Z"}')
    f.write_text("\n".join(lines) + "\n")
    ts = r._last_activity(f)
    assert ts == datetime.datetime(2026, 5, 30, 12, tzinfo=datetime.timezone.utc).timestamp()
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

```python
import datetime

def _parse_ts(value: object) -> float | None:
    """Parse an ISO-8601 (Z or offset) timestamp string to epoch seconds."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _last_activity(transcript: Path) -> float | None:
    """Epoch seconds of the last *timestamped* entry, via a bounded tail read.

    Reads the file end-first in chunks so large transcripts stay cheap, scanning
    backward for the most recent line that carries a ``timestamp``.
    """
    try:
        with transcript.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            block = 64 * 1024
            data = b""
            pos = size
            while pos > 0:
                step = min(block, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
                lines = data.split(b"\n")
                # keep the partial first line for the next iteration
                data = lines[0] if pos > 0 else b""
                candidates = lines[1:] if pos > 0 else lines
                for raw in reversed(candidates):
                    line = raw.strip()
                    if not line or b'"timestamp"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_ts(entry.get("timestamp"))
                    if ts is not None:
                        return ts
    except OSError:
        return None
    return None
```

- [ ] **Step 4: Run, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "tail-read last timestamped entry for session age" --body "Bounded end-first read; skips agent-name (no timestamp); ISO->epoch. Ref #1323."
```

---

## Task 7: Resolver — wire age into detection

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm_resolve.py`
- Test: `tests/vergil_tooling/test_vrg_vm_resolve.py`

`_read_state` must also return a `last_active` map: active sessions use the roster's `updatedAt` (ms epoch); idle use `_last_activity`.

- [ ] **Step 1: Write failing tests**

```python
def test_read_state_returns_last_active(monkeypatch, tmp_path: Path) -> None:
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    (projects / "s1.jsonl").write_text('{"type":"user","timestamp":"2026-05-02T00:00:00.000Z"}\n')
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "100.json").write_text(
        '{"pid":100,"sessionId":"s2","updatedAt":1748000000000}'
    )
    (projects / "s2.jsonl").write_text('{"type":"agent-name","agentName":"vergil:02:p","sessionId":"s2"}\n')
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    monkeypatch.setattr(r, "_is_live", lambda _pid, _ps: True)
    names, active, last_active = r._read_state()
    assert active == {"s2"}
    assert last_active["s2"] == 1748000000.0                      # roster updatedAt (ms->s)
    assert last_active["s1"] == datetime.datetime(2026, 5, 2, tzinfo=datetime.timezone.utc).timestamp()
```

- [ ] **Step 2: Run, verify fail** → FAIL (`_read_state` returns a 2-tuple).

- [ ] **Step 3: Implement**

Replace `_read_state` and add a roster-age helper:

```python
def _roster_updated_at(roster: list[dict[str, object]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for entry in roster:
        sid = entry.get("sessionId")
        upd = entry.get("updatedAt")
        if isinstance(sid, str) and isinstance(upd, (int, float)):
            out[sid] = float(upd) / 1000.0  # roster stores ms
    return out


def _read_state() -> tuple[dict[str, str], set[str], dict[str, float]]:
    cdir = _claude_dir()
    projects = cdir / "projects"
    names = name_by_session(projects)
    roster = read_roster(cdir / "sessions")
    active = active_session_ids(roster)
    last_active = _roster_updated_at(roster)
    for sid in names:
        if sid not in last_active:
            ts = _last_activity(projects_glob(projects, sid))
            if ts is not None:
                last_active[sid] = ts
    return names, active, last_active
```

`name_by_session` keys by transcript stem; we need the transcript path for a sid. Add a small helper:

```python
def projects_glob(projects_dir: Path, session_id: str) -> Path:
    """Path to a session's transcript (``<slug>/<sessionId>.jsonl``)."""
    matches = sorted(projects_dir.glob(f"*/{session_id}.jsonl"))
    return matches[0] if matches else projects_dir / f"{session_id}.jsonl"
```

Update the three existing callers of `_read_state` (`resolve`, `list_json`, and the existing `test_read_state_combines`) to unpack three values; the next tasks rewrite `resolve`/`list_json` anyway, so for now: `names, active, _la = _read_state()`.

- [ ] **Step 4: Run, verify pass.** Fix the existing `test_read_state_combines` to unpack 3-tuple. → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "compute last_active map in resolver state" --body "Active uses roster updatedAt (ms->s); idle uses tail-read transcript timestamp. Ref #1323."
```

---

## Task 8: Resolver — relabel-archive append

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm_resolve.py`
- Test: `tests/vergil_tooling/test_vrg_vm_resolve.py`

- [ ] **Step 1: Write failing tests**

```python
def test_archive_session_appends_archived_name(tmp_path: Path, monkeypatch) -> None:
    projects = tmp_path / "projects" / "slug"
    projects.mkdir(parents=True)
    t = projects / "s1.jsonl"
    t.write_text('{"type":"agent-name","agentName":"vergil:01:p","sessionId":"s1"}\n')
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    r._archive_session("s1", "2026-05-30T14:23:07Z")
    last = r._last_agent_name(t)
    assert last == "archived@2026-05-30T14:23:07Z@vergil:01:p"

def test_archive_session_missing_transcript_is_noop(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "projects").mkdir()
    monkeypatch.setattr(r, "_claude_dir", lambda: tmp_path)
    r._archive_session("ghost", "2026-05-30T14:23:07Z")  # must not raise
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

```python
from vergil_tooling.lib.session import make_archived_name  # add to imports

def _archive_session(session_id: str, timestamp: str) -> None:
    """Relabel a cold session by appending an archived agent-name entry."""
    projects = _claude_dir() / "projects"
    transcript = projects_glob(projects, session_id)
    current = _last_agent_name(transcript)
    if current is None:
        return
    entry = {
        "type": "agent-name",
        "agentName": make_archived_name(current, timestamp),
        "sessionId": session_id,
    }
    try:
        with transcript.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        return
```

- [ ] **Step 4: Run, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "archive a session by appending an archived agent-name" --body "Relabel-in-place; no-op if transcript missing/unnamed. Ref #1323."
```

---

## Task 9: Resolver — execute the plan (sweep + prompt + resolve)

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm_resolve.py`
- Test: `tests/vergil_tooling/test_vrg_vm_resolve.py`

- [ ] **Step 1: Write failing tests**

```python
def test_resolve_sweeps_stale_and_creates(monkeypatch, capsys, capture_exec) -> None:
    NOW = 100 * 86400.0
    monkeypatch.setattr(r, "_read_state",
        lambda: ({"old": "vergil:01:p"}, set(), {"old": NOW - 20 * 86400.0}))
    monkeypatch.setattr(r, "_now", lambda: NOW)
    monkeypatch.setattr(r, "_now_iso", lambda: "2026-05-30T00:00:00Z")
    archived = []
    monkeypatch.setattr(r, "_archive_session", lambda sid, ts: archived.append(sid))
    rc = r.resolve("vergil", "p", None, False, False, [], stale_days=7, archive_days=14)
    assert rc == 0
    assert archived == ["old"]                       # swept
    assert capture_exec == [["claude", "-n", "vergil:01:p"]]   # fresh in reclaimed slot
    assert "auto-archiving" in capsys.readouterr().err

def test_resolve_warn_prompt_resume(monkeypatch, capture_exec) -> None:
    NOW = 100 * 86400.0
    monkeypatch.setattr(r, "_read_state",
        lambda: ({"s1": "vergil:01:p"}, set(), {"s1": NOW - 9 * 86400.0}))
    monkeypatch.setattr(r, "_now", lambda: NOW)
    monkeypatch.setattr(r, "_prompt_stale", lambda *a: "r")    # user picks resume
    rc = r.resolve("vergil", "p", None, False, False, [], stale_days=7, archive_days=14)
    assert capture_exec == [["claude", "--resume", "s1"]]

def test_resolve_warn_prompt_fresh(monkeypatch, capture_exec) -> None:
    NOW = 100 * 86400.0
    monkeypatch.setattr(r, "_read_state",
        lambda: ({"s1": "vergil:01:p"}, set(), {"s1": NOW - 9 * 86400.0}))
    monkeypatch.setattr(r, "_now", lambda: NOW)
    monkeypatch.setattr(r, "_now_iso", lambda: "2026-05-30T00:00:00Z")
    monkeypatch.setattr(r, "_prompt_stale", lambda *a: "f")
    archived = []
    monkeypatch.setattr(r, "_archive_session", lambda sid, ts: archived.append(sid))
    rc = r.resolve("vergil", "p", None, False, False, [], stale_days=7, archive_days=14)
    assert archived == ["s1"]
    assert capture_exec == [["claude", "-n", "vergil:01:p"]]

def test_resolve_warn_prompt_cancel(monkeypatch, capture_exec) -> None:
    NOW = 100 * 86400.0
    monkeypatch.setattr(r, "_read_state",
        lambda: ({"s1": "vergil:01:p"}, set(), {"s1": NOW - 9 * 86400.0}))
    monkeypatch.setattr(r, "_now", lambda: NOW)
    monkeypatch.setattr(r, "_prompt_stale", lambda *a: "c")
    rc = r.resolve("vergil", "p", None, False, False, [], stale_days=7, archive_days=14)
    assert rc == 0
    assert capture_exec == []                          # cancelled, no exec

def test_prompt_stale_non_tty_returns_resume(monkeypatch) -> None:
    monkeypatch.setattr(r.sys.stdin, "isatty", lambda: False)
    assert r._prompt_stale("vergil-project/p", 1, 9) == "r"   # scripted -> resume
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

Add imports + time seams + prompt + new `resolve` signature:

```python
import datetime
from vergil_tooling.lib.session import PromptStale, SessionPlan, plan_session  # add


def _now() -> float:
    return datetime.datetime.now(tz=datetime.timezone.utc).timestamp()


def _now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _prompt_stale(path: str, slot: int, age_days: int) -> str:
    """Return 'r' (resume), 'f' (fresh), or 'c' (cancel). Non-TTY -> 'r'."""
    if not sys.stdin.isatty():
        return "r"
    print(
        f"Slot {slot:02d} for {path} was last active {age_days} days ago.",
        file=sys.stderr,
    )
    answer = input("[r]esume / [f]resh / [c]ancel? ").strip().lower()
    return {"r": "r", "f": "f", "c": "c"}.get(answer[:1], "c")


def resolve(
    identity: str,
    path: str,
    requested_slot: int | None,
    fork: bool,
    fresh: bool,
    extra: list[str],
    stale_days: int,
    archive_days: int,
) -> int:
    names, active, last_active = _read_state()
    slots = build_slots(identity, path, names, active, last_active)
    now = _now()
    plan = plan_session(identity, path, slots, now, stale_days, archive_days,
                        requested_slot, fork, fresh)
    _run_sweep(plan.auto_archive)
    return _execute(identity, path, plan.action, extra)


def _run_sweep(slots: list) -> None:
    ts = _now_iso()
    for slot in slots:
        print(f"auto-archiving slot {slot.slot:02d} ({slot.session_id})…", file=sys.stderr)
        _archive_session(slot.session_id, ts)


def _execute(identity: str, path: str, action: object, extra: list[str]) -> int:
    if isinstance(action, Refuse):
        print(f"ERROR: {action.message}", file=sys.stderr)
        return 1
    if isinstance(action, Create):
        return _exec_claude(["-n", action.name, *extra])
    if isinstance(action, Resume):
        return _exec_claude(["--resume", action.session_id, *extra])
    if isinstance(action, Fork):
        return _exec_claude(
            ["--resume", action.session_id, "--fork-session", "-n", action.name, *extra]
        )
    prompt: PromptStale = action
    choice = _prompt_stale(path, _slot_num(prompt.name), prompt.age_days)
    if choice == "c":
        return 0
    if choice == "f":
        _archive_session(prompt.session_id, _now_iso())
        return _exec_claude(["-n", prompt.name, *extra])
    return _exec_claude(["--resume", prompt.session_id, *extra])


def _slot_num(name: str) -> int:
    parsed = parse_name(name)  # import parse_name
    return parsed[1] if parsed else 0
```

Add `parse_name` to the session imports.

- [ ] **Step 4: Run, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "execute session plan: sweep, stale prompt, resume/create/fork" --body "TTY-gated prompt (non-TTY resumes); auto-archive sweep with notes. Ref #1323."
```

---

## Task 10: Resolver — age + state in `--list-json`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm_resolve.py`
- Test: `tests/vergil_tooling/test_vrg_vm_resolve.py`

State is `active` / `idle` / `archived`. Archived rows come from transcripts whose current name is an `archived@…` label.

- [ ] **Step 1: Write failing tests**

```python
def test_list_json_includes_age_and_state(monkeypatch, capsys) -> None:
    monkeypatch.setattr(r, "_read_state",
        lambda: ({"s1": "vergil:01:p", "a1": "archived@2026-05-01T00:00:00Z@vergil:03:p"},
                 {"s1"}, {"s1": 1748000000.0, "a1": 1746000000.0}))
    r.list_json()
    rows = json.loads(capsys.readouterr().out)
    by = {(x["identity"], x["slot"], x.get("state")): x for x in rows}
    assert ("vergil", 1, "active") in by
    assert by[("vergil", 1, "active")]["lastActive"] == 1748000000.0
    assert ("vergil", 3, "archived") in by
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

Add an archived collector and extend `list_json`. Active/idle rows come from `list_rows` (now with `last_active`); archived rows are parsed separately:

```python
from vergil_tooling.lib.session import parse_archived  # add

def _archived_rows(names: dict[str, str], last_active: dict[str, float]) -> list[dict]:
    out = []
    for sid, name in names.items():
        parsed = parse_archived(name)
        if parsed is None:
            continue
        _ts, original = parsed
        # `original` is a clean <id>:<NN>:<path> (not archived-prefixed), so
        # parse_name accepts it directly.
        slot = parse_name(original)
        if slot is None:
            continue
        ident, num, path = slot
        out.append({"identity": ident, "slot": num, "path": path, "sessionId": sid,
                    "state": "archived", "archivedAt": _ts, "lastActive": last_active.get(sid)})
    return out
```

Rewrite `list_json`:

```python
def list_json() -> int:
    names, active, last_active = _read_state()
    rows = [
        {
            "identity": row.identity, "slot": row.slot, "path": row.path,
            "sessionId": row.session_id,
            "state": "active" if row.active else "idle",
            "lastActive": row.last_active,
        }
        for row in list_rows(names, active, last_active)
    ]
    rows.extend(_archived_rows(names, last_active))
    print(json.dumps(rows))
    return 0
```

- [ ] **Step 4: Run, verify pass.** Update the older `test_list_json` to expect the `state`/`lastActive` keys. → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "emit age + state (active/idle/archived) in --list-json" --body "Archived rows parsed from archived@ labels. Ref #1323."
```

---

## Task 11: Resolver `main` + host CLI

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_vm_resolve.py`, `src/vergil_tooling/bin/vrg_vm.py`
- Test: `tests/vergil_tooling/test_vrg_vm_resolve.py`, `tests/vergil_tooling/test_vrg_vm.py`

- [ ] **Step 1: Write failing tests** (resolver `main`)

```python
def test_main_passes_fresh_and_thresholds(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(r, "resolve", lambda *a, **k: seen.update(args=a) or 0)
    r.main(["--identity", "vergil", "--path", "p", "--fresh",
            "--stale-days", "7", "--archive-days", "14"])
    # resolve(identity, path, slot, fork, fresh, extra, stale_days, archive_days)
    assert seen["args"][4] is True            # fresh
    assert seen["args"][6] == 7 and seen["args"][7] == 14
```

(host CLI)

```python
class TestSessionFresh:
    # decorate like TestSession (patch execvp, link_claude_dirs, copy_claude_config,
    # try_update_tooling, vm_age_days)
    def test_session_fresh_flag_reaches_resolver(self, ..., mock_exec, config_file) -> None:
        main(["session", "--config", str(config_file), "--fresh", "vergil-tooling"])
        inner = self._inner(mock_exec)
        assert "--fresh" in inner
        assert "--stale-days 7" in inner and "--archive-days 14" in inner

def test_list_sessions_age_column(...):  # patch list_vms, shell_run, name_by_session
    # shell_run returns rows incl state + lastActive; assert "LAST ACTIVE" header,
    # default shows active+idle only, --archived shows archived, --all shows all.
```

- [ ] **Step 2: Run, verify fail** → FAIL.

- [ ] **Step 3: Implement**

**Resolver `main`:** add args and pass through:

```python
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--stale-days", type=int, default=7, dest="stale_days")
    parser.add_argument("--archive-days", type=int, default=14, dest="archive_days")
```
```python
    return resolve(args.identity, args.path, args.slot, args.fork, args.fresh,
                   extra, args.stale_days, args.archive_days)
```

**Host `vrg_vm.py`:**

- Import `resolve_session_stale_days`, `resolve_session_archive_days`.
- Add `--fresh` to the `session` subparser; add `--active/--idle/--archived/--all` to `list`.
- In `_session_inner`, after computing the resolver command, append the flags. The function needs the thresholds + fresh; thread them in via params:

```python
    if args.fresh:
        resolve_cmd += ["--fresh"]
    resolve_cmd += ["--stale-days", str(stale_days), "--archive-days", str(archive_days)]
```
Compute in `_cmd_session`:
```python
    stale = resolve_session_stale_days(config, identity)
    archive = resolve_session_archive_days(config, identity)
    ... _session_inner(args, name, rel_path, model, stale, archive)
```

**List architecture (keep the merged host-side approach; extend it).** The
shipped `_list_sessions` reads transcripts **host-side** and queries each running
VM only for active session ids. Preserve that and extend for age + archived —
do **not** switch to per-VM full-row merge (every VM returns all shared
transcripts, so that duplicates rows). Concretely:

- **Host-side, once** (the projects store is shared and host-readable): reuse the
  resolver's `name_by_session`, `_last_activity`, and `parse_archived` against
  `Path.home() / ".claude" / "projects"` to get names, idle ages, and archived
  rows. (Import these from `vergil_tooling.bin.vrg_vm_resolve`.)
- **Per running VM**: extend `_vm_active_session_ids` → `_vm_active_sessions`
  returning `{sessionId: updatedAt_seconds}` (parse the `--list-json` rows where
  `state == "active"`, reading their `lastActive`). Union across VMs; a session
  is active if any VM reports it active.
- **Merge**: `active = set(union.keys())`; `last_active` = host idle ages
  overlaid with the VM `updatedAt` for active ids. Then
  `rows = list_rows(names, active, last_active)` — `list_rows` already dedups by
  `(identity, slot, path)` with active-wins, so **no separate dedup and no
  duplicate-state rows**. Append archived rows from the host-side
  `_archived_rows(names, last_active)`.

Render with a relative-age formatter and the state filters:

```python
def _format_age(last_active: float | None, now: float) -> str:
    if last_active is None:
        return "unknown"
    days = (now - last_active) / 86400.0
    if days < 1:
        return f"{int(days * 24)}h"
    return f"{int(days)}d"
```

State filtering: default keep `state in {"active","idle"}`; `--archived` →
`{"archived"}`; `--all` → all; `--active`/`--idle` narrow accordingly. Header
row gains `LAST ACTIVE`, and the archived view adds an `ARCHIVED` column (the
`archivedAt` timestamp).

- [ ] **Step 4: Run, verify pass** → PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope vm --message "wire --fresh, thresholds, and list --sessions states/age" --body "Host plumbs stale/archive days + --fresh into the resolver; list gains state filters and a LAST ACTIVE column. Ref #1323."
```

---

## Task 12: Full validation + PR

- [ ] **Step 1: Format**

Run: `vrg-container-run -- uv run ruff format src/ tests/`

- [ ] **Step 2: Full validation (100% coverage gate)**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: all checks pass; coverage 100%. Fix any gaps (add targeted tests; common misses: the `parse_archived` `len != 3` branch, `_last_activity` OSError, `_prompt_stale` cancel default, `_format_age` <1d branch).

- [ ] **Step 3: Manual smoke (optional, in this VM)**

Confirm the archive relabel + age read against a throwaway transcript copy (mirrors the design's empirical check).

- [ ] **Step 4: Commit any test-coverage additions, then submit PR**

```bash
vrg-submit-pr --issue 1323 --linkage Ref --base develop \
  --title "feat(vm): stale-session lifecycle (age bands, --fresh, auto-archive, list states)" \
  --summary "Age-aware resume with a stale prompt, --fresh archive-and-restart, 14-day auto-archive sweep, and list --sessions state filters + age column." \
  --notes "Implements vergil-tooling #1323 / design vergil-vm #82. Most-recent idle resume; relabel-archive (never delete); tail-read age; cascading session_stale_days(7)/session_archive_days(14)."
```

- [ ] **Step 5: Watch CI to green**

Run: `vrg-wait-until-green <pr-number>`

---

## Task 13: Comprehensive `docs/site` session user guide (vergil-vm)

**Repo:** vergil-vm (NOT vergil-tooling). Fulfills vergil-vm #81 and extends it
with the stale-lifecycle behavior. Do this **after** the tooling change is
released, so the guide documents real, shipped behavior.

**Files:**
- Explore: `docs/site/` structure + `docs/site/mkdocs.yml` (nav).
- Create/Modify: a dedicated **Sessions** user-guide page under `docs/site/` (follow the existing site layout/`mkdocs.yml` conventions discovered during exploration).
- Test: docs build via the repo's docs pipeline.

This is the single most user-facing surface of the whole feature — write it as a
complete guide a newcomer can follow top to bottom, heavy on copy-pasteable
examples. It is **not** a flag dump.

- [ ] **Step 1: New worktree + branch** (vergil-vm)

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-vm
vrg-git worktree add -b feature/81-session-docs .worktrees/issue-81-session-docs origin/develop
```

- [ ] **Step 2: Explore the site structure**

Read `docs/site/mkdocs.yml` and existing pages (e.g. the `vrg-vm` command pages) to match headings, admonitions, nav placement, and code-block style. Note where a new "Sessions" guide slots into the nav.

- [ ] **Step 3: Write the guide.** It MUST cover, with examples:

  - **Mental model:** the VM sandboxes Claude; `vrg-vm session` is the front door and launches Claude by default. Deterministic names `<identity>:<slot>:<path>` as a visible who/where label. Slots = concurrent agents. States: active / idle / archived.
  - **Everyday use:** `vrg-vm session <workspace>` (required arg; `.` for root), auto-resume of your most-recent session.
  - **Model selection:** `--model`, the `model` identity default, and the top-level cascade.
  - **Multiple agents:** running several on one repo; `--slot N`.
  - **Reconnect/recovery:** after closing a terminal, after host reboot, after `vrg-vm rebuild` (why sessions persist); `vrg-vm list --sessions` with the age column and `--active/--idle/--archived/--all` filters (show sample table output).
  - **Staleness lifecycle (the new part):** the two thresholds and three bands (fresh<7d silent resume; 7–14d warn prompt; ≥14d auto-archive); the `[r]esume/[f]resh/[c]ancel` prompt; `--fresh` to start clean and reclaim a name; what "archived, never deleted" means and how to find/reconnect archived sessions; `session_stale_days`/`session_archive_days` config (incl. `0` = disable auto-archive).
  - **The fork guardrail:** why two live clients on one session is refused; `--fork`.
  - **Escape hatches:** `-- bash`, `-- claude --model opus`.
  - **`/clear` vs fresh:** when to use Claude's in-session `/clear` vs a fresh session (clean context vs zero history).

  Worked examples block (minimum):

  ```bash
  vrg-vm session vergil-project/vergil-tooling          # start or resume most-recent
  vrg-vm session vergil-project/vergil-tooling          # second agent -> next slot
  vrg-vm list --sessions                                # active + idle, with ages
  vrg-vm list --sessions --archived                     # browse archives
  vrg-vm session --slot 02 vergil-project/vergil-tooling # target a slot
  vrg-vm session --fresh vergil-project/vergil-vm        # clean slate, archive old
  vrg-vm session --slot 01 --fork vergil-project/vergil-vm
  vrg-vm session vergil-project/vergil-vm -- bash        # raw shell
  ```

- [ ] **Step 4: Wire into `mkdocs.yml` nav** and build the docs via the repo's pipeline; fix any warnings.

Run: `vrg-container-run -- vrg-validate` (markdownlint etc.), plus the site build if the repo exposes one.
Expected: clean.

- [ ] **Step 5: Commit + PR**

```bash
vrg-commit --type docs --scope site --message "complete vrg-vm session user guide" --body "Full session workflow: naming, slots, resume, --model, list --sessions states/age, stale lifecycle (warn/auto-archive/--fresh), fork guardrail, /clear-vs-fresh, examples. Closes-after-merge tracked via Ref #81."
vrg-submit-pr --issue 81 --linkage Ref --base develop \
  --title "docs(site): complete vrg-vm session user guide" \
  --summary "Thorough, example-rich user guide for the full vrg-vm session workflow including the stale-session lifecycle."
```

---

## Notes for the implementer

- **Run from the worktree:** `cd .worktrees/issue-1323-stale-lifecycle` before any command; `vrg-container-run` mounts the current dir.
- **`vrg-git`/`vrg-gh` only** (raw git/gh are denied). Heredocs are blocked in the shell — write multi-line content to files.
- **TDD discipline:** never write implementation before its failing test. One behavior per test.
- **Coverage is 100% branch** — every `if`/`except`/early-return needs a test that exercises it.
- **Time is injected** via `_now`/`_now_iso` (resolver) and `now` params (pure logic) — never call the clock inside pure functions.
