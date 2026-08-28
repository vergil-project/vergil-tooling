# Fleet survey — collection safety, dev-dep shape, Python floor

> **Epic:** vergil-project/.github#333 · **Task 3** (Phase 0).
> The epic's `plan.md`/`spec.md` are authored in `vergil-project/.github`; this
> **evidence** artifact is committed in the implementing repo (vergil-tooling)
> under a path that mirrors the epic slug, alongside the classifiers it
> exercises: `src/vergil_tooling/lib/test_layout.py`,
> `src/vergil_tooling/lib/dev_deps.py`, and the survey driver
> `scripts/perf/fleet_survey.py`.

Regenerate with:

```bash
uv run python scripts/perf/fleet_survey.py --root <org-clones-dir>
```

## Method and scope

The survey enumerates the Python repos found **among the sibling clones on
disk** (any sibling directory with a root `pyproject.toml`). It is a snapshot of
what is checked out locally, not a query against the full GitHub org — repos not
cloned locally are not surveyed. At capture time the only Python repo among the
local sibling clones was `vergil-tooling`; the other org repos present locally
(`docs`, `vergil-actions`, `vergil-claude-plugin`, `vergil-containers`,
`vergil-vm`) declare no `primary-language = "python"` and ship no root
`pyproject.toml`, so they are correctly out of scope for a Python test-perf
survey.

**Re-run this survey against a fuller set of clones before the gated consumers
ship** (Task 9's xdist sweep, Task 10's import-mode gate). The classifiers are
the durable deliverable; the tables below reflect the clones available when this
was captured.

- Captured: 2026-08-28
- Root surveyed: `/Users/pmoore/dev/projects/vergil-project` (org sibling clones)

## Table 1 — Collection safety (import-mode)

`importlib_safe` is true when the tests are packaged (`tests/__init__.py`, so
every module has a unique dotted path) **or** there are no duplicate test-file
basenames. An unpackaged tree with duplicate basenames is the one unsafe shape
(it collides under `--import-mode=importlib`). Source:
`vergil_tooling.lib.test_layout.classify_test_layout`.

| Repo | Packaged | Duplicate basenames | importlib_safe |
| --- | --- | --- | --- |
| `vergil-tooling` | True | — | True |

**Flags:** none — no surveyed repo is `importlib_safe = False`.

## Table 2 — Dev-dependency shape

Where each repo declares its development dependencies, so the Task-9 xdist sweep
edits the right place (or reports loudly on `UNKNOWN`). Source:
`vergil_tooling.lib.dev_deps.classify_dev_deps`.

| Repo | Dev-dependency shape |
| --- | --- |
| `vergil-tooling` | `uv-groups` |

**Flags:** none — no surveyed repo is `UNKNOWN` shape.

## Table 3 — Python floor (`requires-python`)

Survey-only probe (`fleet_survey.python_floor`), used as a Task-4 sysmon-guard
sanity check: `sys.monitoring` coverage requires 3.12+, so any repo whose floor
dips below `3.12` would be a repo where the `sys.version_info >= (3, 12)` guard
actually matters.

| Repo | requires-python |
| --- | --- |
| `vergil-tooling` | `>=3.12,<4.0` |

**Flags:** none — the one surveyed repo floors at 3.12, at or above the sysmon
requirement.

## Downstream consumers

- `classify_test_layout` → **Task 10** import-mode gate + this survey.
- `classify_dev_deps` / `DevDepShape` → **Task 9** xdist fleet-sweep applicator +
  this survey.
- `python_floor` → survey-only Task-4 sysmon-guard sanity check.
