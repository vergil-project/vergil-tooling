# Claude session-management surface — verification spike (T0)

**Epic:** vergil-project/.github#230 (explicit, purpose-named sessions)
**Task:** #2604 (Stage 0, informational — gates only the conditional T8).
**Reproduce:** `scripts/dev/probe-claude-sessions.py`
**Date probed:** 2026-08-07

> This spike is **informational**. It does **not** block T1–T7, which ship on
> the `ScrapeStore` backend regardless. Its only downstream consumer is the
> **conditional T8** (`SdkStore` backend). The question it answers: does the
> installed `claude-agent-sdk` let us (a) **enumerate** sessions with
> id/name/cwd/last-activity, (b) **resolve a name → id**, (c) **rename** a
> detached session, and (d) **see interactively-launched sessions** (not just
> headless SDK `query()` ones)?

Findings are split into **[observed]** (what the installed code actually did on
this machine) and **[documented]** (what the SDK's own docstrings/signatures
state). The verdict rests on the observed evidence.

## Verdict: **GO** for an `SdkStore` backend (T8)

All four capabilities are empirically confirmed against the installed SDK, run
on the host where real interactive transcripts live. One nuance (session
*liveness*) is not covered by the SDK surface and is called out below; it does
not change the verdict but shapes the T8 implementation.

| Capability the seam needs        | Result | Evidence |
|----------------------------------|--------|----------|
| Enumerate sessions (id/name/cwd/last-activity) | **YES** | `list_sessions()` returned 119 real sessions with those fields |
| Resolve name → session id        | **YES** | client-side filter over `custom_title`; 66 distinct names indexed |
| Rename a detached session        | **YES** | real `rename_session()` round-trip succeeded (no live process) |
| See interactively-launched sessions | **YES** | this very interactive session appeared, with its name/cwd/last-activity |
| Session *liveness* (`active`)     | **NO (gap)** | `SDKSessionInfo` exposes no live/pid flag — see "Gap" below |

## Environment

- **[observed]** Claude Code CLI: `2.1.220 (Claude Code)`.
- **[observed]** `claude-agent-sdk`: `0.2.132`, imported from PyPI. It is **not**
  a `vergil-tooling` dependency and is **not** preinstalled in the dev container
  or on the host — the probe installs it on demand (`pip install
  claude-agent-sdk`). Absence-by-default is a fact about our environments, not a
  limit of the SDK. Adopting T8 therefore means **adding a runtime dependency**.
- **[observed]** The SDK is pre-1.0 (`0.2.x`); treat its API as still moving and
  **pin the version** if adopted.

## What the SDK exposes (observed)

`dir(claude_agent_sdk)` lists 145 public attributes, 27 of them session-related.
The session-management functions relevant to the seam (all **present**), each
with a plain synchronous form and `_from_store` / `_via_store` async variants:

| Function | Signature (documented) | Maps to seam op |
|----------|------------------------|-----------------|
| `list_sessions` | `(directory=None, limit=None, offset=0, include_worktrees=True) -> list[SDKSessionInfo]` | `list_sessions()` |
| `get_session_info` | `(session_id, directory=None) -> SDKSessionInfo \| None` | (per-id lookup) |
| `rename_session` | `(session_id, title, directory=None) -> None` | `rename()` |
| `tag_session` | `(session_id, tag, directory=None) -> None` | (organization; future) |
| `delete_session` | `(session_id, directory=None) -> None` | (not used — never delete) |
| `fork_session` | `(session_id, directory=None, up_to_message_id=None, title=None) -> ForkSessionResult` | (fork already via CLI flag) |
| `project_key_for_directory` | `(directory=None) -> str` | cwd → project key |

Supporting types present: `SDKSessionInfo`, `SessionStore`,
`InMemorySessionStore`, `ForkSessionResult`.

### `SDKSessionInfo` fields (observed)

```text
session_id, summary, last_modified, file_size, custom_title,
first_prompt, git_branch, cwd, tag, created_at
```

Mapping to the seam's `SessionInfo(session_id, name, cwd, active, last_active)`:

- `session_id` → `session_id`
- `custom_title` → `name` (the supported name set via `-n` / `/rename`)
- `cwd` → `cwd`
- `last_modified` → `last_active`
- `active` → **not provided** (see Gap)

## Evidence detail

### 1. Enumerate + see interactive sessions (observed)

`list_sessions()` (no `directory` → all projects) returned **119** sessions from
the real on-disk store. The 10 most-recently-active included **this running
interactive session**:

```text
id=a374e3ff  title='vergil-user:03:vergil-project/vergil-tooling'
             cwd=/Users/pmoore/dev/projects/vergil-project/vergil-tooling  age~4 min
```

The `a374e3ff` id matches this session's own working directory UUID, so the SDK
is unambiguously surfacing an **interactively-launched TTY session** — the open
question §2.2 flagged — with its name, cwd, and last-activity. Legacy
`vergil-user:NN:workspace` names and `archived@<ts>@…` names also appeared,
i.e. exactly the corpus the seam must list and resolve. 115/119 sessions carried
a `custom_title`.

### 2. Resolve name → id (observed)

Building an index from `list_sessions()` output alone produced **66** distinct
`custom_title` values, **15** of which mapped to more than one session — the
precise ambiguity the seam's `resolve_over()` arbitrates (active-over-idle, then
most-recent, raise on ≥2 co-equal active). No dedicated resolver call exists
(nor is one needed): resolution is a pure client-side filter over the listed
rows, identical in shape to what `ScrapeStore` does today.

### 3. Rename a detached session (observed)

Against a throwaway `CLAUDE_CONFIG_DIR` seeded with one minimal transcript (so no
real session was touched), `rename_session(id, "epic-230:probe-detached-rename")`
changed the session's `custom_title` from `None` to the new value, confirmed by a
follow-up `list_sessions()`. This is a **detached** rename — no live process —
which is exactly what `--fresh` retire-rename (T5) and `store.rename()` (T1)
require.

## Documented mechanism (from the SDK docstrings)

- **[documented]** `list_sessions` scans `~/.claude/projects/<sanitized-cwd>/`
  for `*.jsonl` session files, "metadata extracted from stat + head/tail reads,"
  honoring `CLAUDE_CONFIG_DIR`. This is the **same on-disk transcript store** the
  interactive CLI writes and that our `ScrapeStore` reads today.
- **[documented]** `rename_session` "rename[s] a session by appending a
  custom-title entry … `list_sessions` reads the LAST custom-title from the file
  tail, so repeated calls are safe." This is the **same `custom-title` event**
  our scrape already parses — the SDK is a maintained wrapper over the identical
  internal mechanism, not a different one.
- **[documented]** `tag_session` appends a `{type:'tag',…}` JSONL entry; last
  wins; `None` clears. This is the `tag`-based organization §9 defers.

**Interpretation (judgment, not observed):** the "internal formats can break on
any release" risk (§2.2) is **not eliminated** — the SDK still reads/writes the
same internal transcript JSONL. What changes is **who owns the coupling**: a
versioned, installable, publicly-`__all__`-exported Python API maintained by
Anthropic, instead of our hand-rolled scrape that already broke once
(`agent-name → custom-title`). That relocation of the maintenance burden behind a
supported surface is exactly the isolation §3 asks for, which is why this is a
GO.

## Gap: session liveness (`active`) is not in the SDK surface

`SDKSessionInfo` has **no** live/pid/active field. The SDK reads on-disk
transcripts, not the live roster (`~/.claude/sessions/<pid>.json`) that today
tells us which sessions are *currently running*. The seam's `resolve_over()`
depends on `active` to prefer a live session over an idle one and to fail loud on
two co-equal live sessions.

**Implication for T8 (not a blocker):** an `SdkStore` can source enumeration,
name, cwd, last-activity, and rename entirely from the SDK, but must still obtain
**liveness** separately — either by continuing to read the roster for the
`active` flag, or by redefining `active` in terms of recency. This is an
implementation detail for T8, already handled by `ScrapeStore`; it does not
weaken the GO for the four questions the spike posed.

## Reproducing

```bash
# In the dev container (empty store → proves it reads the real store; also runs
# the detached-rename round-trip in a temp dir):
vrg-container-run -- bash -c \
  'pip install --quiet claude-agent-sdk && python3 scripts/dev/probe-claude-sessions.py'

# On the host / a real VM (where interactive transcripts live → answers the
# interactive-visibility question):
python3 scripts/dev/probe-claude-sessions.py   # with claude-agent-sdk importable
```

The script never assumes a function name exists — it enumerates the real public
surface, probes each capability, and prints `[observed]` lines suitable for
folding back into this document.
