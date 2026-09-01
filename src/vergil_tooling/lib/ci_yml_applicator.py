"""Applicator: strip the hardcoded matrix inputs from a repo's ``ci.yml``.

Removes the ``versions:`` and ``container-tag:`` inputs from **every**
reusable-workflow call in ``.github/workflows/ci.yml``, converging the file onto
the thin-caller shape rendered by
:func:`vergil_tooling.lib.repo_init.render_ci_workflow` (which keeps only
``language:`` / ``container-suffix:``). The reusable workflows now resolve the
version matrix and container tag from ``vergil.toml`` themselves
(epic vergil-project/.github#338), so these two caller inputs are dead weight.

This is a :data:`~vergil_tooling.lib.fleet_sweep.Applicator` — the bespoke
per-repo change the generic fleet-sweep driver injects; the ``vrg-ci-sync`` entry
(:mod:`vergil_tooling.bin.vrg_ci_sync`) wires it into
:func:`~vergil_tooling.lib.fleet_sweep.run_sweep`.

Contract:

- **Idempotent** — a ``ci.yml`` already reduced to the thin shape (or one that
  never carried the inputs) is a no-op returning ``changed=False``.
- **No silent failure** — a ``ci.yml`` whose shape this applicator cannot
  understand (missing file, invalid YAML, or no ``jobs:`` mapping) gets **no**
  edit and is flagged ``needs_followup`` so the sweep surfaces it loudly for a
  human, rather than silently skipping a repo (this repo's "no silent failures"
  policy).

The edit itself is a line filter, not a YAML round-trip: the file is parsed only
to *validate* the shape, then the two input lines are dropped textually so all
comments, ordering, and formatting are preserved untouched. In a ``ci.yml`` these
two keys appear exclusively as reusable-workflow inputs (indented beneath a
job's ``with:``), so an indentation-anchored key match strips precisely them.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from vergil_tooling.lib.fleet_sweep import AppResult

# ci.yml, relative to a repo worktree root.
_CI_RELPATH = Path(".github") / "workflows" / "ci.yml"

# The two reusable-workflow inputs retired by epic #338. Anchored to a leading
# indent so only nested ``with:`` inputs match — never a top-level key — and to
# the key immediately followed by ``:`` so ``container-tag`` cannot match a
# longer neighbour. Value-agnostic: the ``versions`` list and ``container-tag``
# string differ per repo.
_MATRIX_INPUT_RE = re.compile(r"^[ \t]+(?:versions|container-tag):")


def strip_matrix_inputs(worktree: Path) -> AppResult:
    """Strip ``versions:``/``container-tag:`` from *worktree*'s ``ci.yml``.

    Returns an :class:`~vergil_tooling.lib.fleet_sweep.AppResult`:

    - ``changed=True`` when at least one input line was removed.
    - ``changed=False`` (no ``needs_followup``) when the file is already thin —
      the idempotent no-op.
    - ``changed=False, needs_followup=True`` when the file is absent, is not
      valid YAML, or lacks a ``jobs:`` mapping; **no edit is made** in that case.
    """
    ci_path = worktree / _CI_RELPATH

    if not ci_path.exists():
        return AppResult(
            changed=False,
            summary=f"{_CI_RELPATH} not found in {worktree}",
            needs_followup=True,
        )

    original = ci_path.read_text(encoding="utf-8")

    try:
        data = yaml.safe_load(original)
    except yaml.YAMLError as exc:
        return AppResult(
            changed=False,
            summary=f"{_CI_RELPATH} does not parse as YAML, left unchanged: {exc}",
            needs_followup=True,
        )

    if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict):
        return AppResult(
            changed=False,
            summary=f"{_CI_RELPATH} has no jobs: mapping; shape not recognized, left unchanged",
            needs_followup=True,
        )

    kept = [line for line in original.splitlines(keepends=True) if not _MATRIX_INPUT_RE.match(line)]
    new_text = "".join(kept)

    if new_text == original:
        return AppResult(
            changed=False,
            summary=f"{_CI_RELPATH} already thin; no matrix inputs to strip",
        )

    ci_path.write_text(new_text, encoding="utf-8")
    removed = len(original.splitlines()) - len(new_text.splitlines())
    return AppResult(
        changed=True,
        summary=f"stripped {removed} hardcoded matrix input line(s) from {_CI_RELPATH}",
    )
