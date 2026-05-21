# Rationalize [publish] Config and Fix confirm-publish Phase

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the `[publish]` config in `vergil.toml` and the `confirm_publish` release phase to match the actual CD workflow architecture (`cd.yml` calling reusable `cd-docs.yml` and `cd-release.yml`).

**Architecture:** The old code watched two separate workflows (`publish.yml` and `Documentation`) that no longer exist. The new code watches a single "CD" workflow (`cd.yml`) and gates artifact verification on the `publish.release` and `publish.docs` booleans. The `docs_workflow` config field is removed since all repos use the same standardized CD workflow.

**Tech Stack:** Python 3.14, pytest, `gh` CLI for GitHub API queries

**Tracking issue:** #963

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/lib/config.py` | Modify | Remove `docs_workflow` field from `PublishConfig` |
| `src/vergil_tooling/lib/release/context.py` | Modify | Replace per-workflow run fields with unified CD run fields |
| `src/vergil_tooling/lib/release/confirm.py` | Rewrite | Watch single CD workflow, gate checks on publish booleans |
| `src/vergil_tooling/lib/release/finalize.py` | Modify | Update summary to use new context fields |
| `src/vergil_tooling/lib/release/orchestrator.py` | Modify | Update phase details to use new context fields |
| `src/vergil_tooling/bin/vrg_finalize_repo.py` | Modify | Change workflow name from `"Documentation"` to `"CD"` |
| `tests/vergil_tooling/test_config.py` | Modify | Remove `docs_workflow` tests |
| `tests/vergil_tooling/test_release_confirm.py` | Rewrite | Test new CD workflow architecture |
| `tests/vergil_tooling/test_release_finalize.py` | Modify | Update context field names in test setup |
| `tests/vergil_tooling/test_release_orchestrator.py` | Modify | Update context field names and assertions |
| `tests/vergil_tooling/test_vrg_finalize_repo.py` | Modify | Update workflow name in assertions |

**Out of scope** (separate issues in consuming repos):
- vergil-vm: set `release = false` (no release job in cd.yml)
- vergil-claude-plugin: add `[publish]` section

---

## Task 1: Remove `docs_workflow` from `PublishConfig`

**Files:**
- Modify: `src/vergil_tooling/lib/config.py:57-62` (PublishConfig dataclass)
- Modify: `src/vergil_tooling/lib/config.py:120-126` (parsing)
- Modify: `tests/vergil_tooling/test_config.py:320-330` (test removal)

## Task 2: Replace per-workflow context fields with unified CD fields

**Files:**
- Modify: `src/vergil_tooling/lib/release/context.py:32-35`

## Task 3: Rewrite `confirm.py` for CD workflow architecture

**Files:**
- Rewrite: `src/vergil_tooling/lib/release/confirm.py`
- Rewrite: `tests/vergil_tooling/test_release_confirm.py`

## Task 4: Update release summary in `finalize.py`

**Files:**
- Modify: `src/vergil_tooling/lib/release/finalize.py:40-57`
- Modify: `tests/vergil_tooling/test_release_finalize.py:15-31`

## Task 5: Update phase details in `orchestrator.py`

**Files:**
- Modify: `src/vergil_tooling/lib/release/orchestrator.py:64-72`
- Modify: `tests/vergil_tooling/test_release_orchestrator.py:67-68,193-203`

## Task 6: Fix `vrg_finalize_repo.py` workflow name

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_repo.py:21,91-146,289-301`
- Modify: `tests/vergil_tooling/test_vrg_finalize_repo.py`

## Task 7: Full validation
