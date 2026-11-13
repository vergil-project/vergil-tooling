"""Local repository configuration audit checks.

Checks files on disk — vergil.toml, CLAUDE.md, .claude/settings.json,
.claude/hooks/guard.sh, .gitignore, .github/dependabot.yml and
.github/workflows/ops.yml — without making any API calls.

A managed file that ``vrg-github-repo-config update`` can fix has one
``<name>_drift`` function here, which both its ``_check_*`` and its fixer in
:mod:`vergil_tooling.lib.repo_config_fixes` call. It returns the
:class:`ManagedFileDrift` (the audit item and the write that removes it), or
``None`` when the file is correct, so the check and the fixer share one
definition of the correct file and cannot disagree (epic
vergil-project/.github#1064, Task 3a).
"""

from __future__ import annotations

import importlib.resources
import json
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import dependabot, gitignore
from vergil_tooling.lib.config import ConfigError, declared_primary_language, read_config
from vergil_tooling.lib.github_config import ConfigDiff, DiffItem
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.vergil_refs import iter_workflow_refs, read_source_version

if TYPE_CHECKING:
    from pathlib import Path


#: Packaged template files.
CLAUDE_MD_TEMPLATE = "claude_md_consumer.md"
SETTINGS_TEMPLATE = "claude_settings.json"
HOOK_GUARD_SHIM_TEMPLATE = "hook_guard_shim.sh"

#: Where the hook guard shim lives in a repo, relative to its root.
HOOK_GUARD_SHIM_PATH = ".claude/hooks/guard.sh"

#: Where the Claude settings live in a repo, relative to its root.
CLAUDE_SETTINGS_PATH = ".claude/settings.json"

#: Where CLAUDE.md lives in a repo, relative to its root.
CLAUDE_MD_PATH = "CLAUDE.md"

#: The mode the hook guard shim is written with.
HOOK_GUARD_SHIM_MODE = 0o755

#: The mode a newly created managed file other than the shim is written with.
NEW_FILE_MODE = 0o644

#: Every execute bit; a shim missing any of them is drift.
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

#: The CLAUDE.md line budget (epic vergil-project/.github#558), counted as
#: ``len(text.splitlines())`` so a trailing newline is not an extra line.
CLAUDE_MD_MAX_LINES = 200


@dataclass(frozen=True)
class FileFix:
    """The write that makes one managed file correct.

    ``path`` is relative to the repo root. The fixer replaces the whole file
    with ``data`` and leaves it at ``mode``.
    """

    path: str
    data: bytes
    mode: int


@dataclass(frozen=True)
class ManagedFileDrift:
    """Why a managed file is not correct, and how to make it so.

    ``item`` is what the audit reports. ``fix`` is the write that makes the
    file correct, or ``None`` when no fix can be computed (for example, a
    CLAUDE.md with no template markers).
    """

    item: DiffItem
    fix: FileFix | None


def _read_data_file(name: str) -> str:
    return (
        importlib.resources.files("vergil_tooling.data").joinpath(name).read_text(encoding="utf-8")
    )


def render_hook_guard_shim() -> str:
    """Return the ``.claude/hooks/guard.sh`` text: the packaged hook guard shim.

    The file is fully managed: the audit requires a repo's copy to match it
    byte for byte, ``vrg-github-repo-init`` writes it for a new repo, and
    ``vrg-hook-guard-shim-sync`` writes it for an existing one. A missing
    packaged file raises; it is never treated as an empty shim.
    """
    return _read_data_file(HOOK_GUARD_SHIM_TEMPLATE)


def render_claude_settings() -> str:
    """Return the ``.claude/settings.json`` text: the packaged template.

    The audit requires the repo file to equal the template after JSON
    parsing; this is the text ``vrg-github-repo-init`` and the fixer write.
    """
    return json.dumps(_load_settings_template(), indent=2) + "\n"


def _load_template() -> str:
    return _read_data_file(CLAUDE_MD_TEMPLATE)


def _load_settings_template() -> dict[str, Any]:
    template = json.loads(_read_data_file(SETTINGS_TEMPLATE))
    if not isinstance(template, dict):  # pragma: no cover - packaging error
        msg = f"{SETTINGS_TEMPLATE} template is not a JSON object"
        raise TypeError(msg)
    return template


def _gitignore_language(repo_root: Path) -> str | None:
    """Resolve the fence language for the ``.gitignore`` audit.

    Uses the asserted ``[project].primary-language`` — the same source the rest
    of the validation stack trusts — normalized to ``None`` (base-only) for any
    language without a managed fragment, when no language is declared, or when
    there is no ``vergil.toml``. A base-only fence is exactly what is expected
    there (e.g. ``.github`` is ``shell`` and ``docs`` declares nothing — both
    take the base fragment only).

    A config that fails to load raises
    :class:`~vergil_tooling.lib.config.ConfigError` rather than resolving to
    base-only: a silent base-only would hide the real failure behind a fence
    verdict (#3451). Every caller must surface it.
    """
    lang = declared_primary_language(repo_root)
    return lang if lang in gitignore.FRAGMENT_LANGS else None


def _check_gitignore(repo_root: Path, items: list[DiffItem]) -> None:
    """Assert the repo ``.gitignore`` is a correct vergil-managed fence (epic #325).

    Only a correct fenced ``base + <language>`` managed block for the repo's
    resolved language passes. The transitional legacy baseline-superset arm was
    removed in Task 10 once the whole fleet was fenced, so ``base +
    all-fragments`` (via :func:`gitignore.check`) is the single source of truth
    for acceptance. Each fence-compliance failure is appended as a ``DiffItem``.

    When ``vergil.toml`` fails to load, the fence language is unknown, so the
    fence is not judged at all: the check appends one explicit failure naming
    the config error instead of a false compliant/differs verdict (#3451).
    """
    gitignore_path = repo_root / ".gitignore"
    text = gitignore_path.read_text(encoding="utf-8") if gitignore_path.is_file() else ""

    try:
        lang = _gitignore_language(repo_root)
    except ConfigError as exc:
        items.append(
            DiffItem(
                field="local.gitignore",
                expected="resolvable [project].primary-language",
                actual=f"cannot resolve the fence language: {exc}",
            )
        )
        return
    compliance = gitignore.check(text, lang)
    if compliance.compliant:
        return

    for reason in compliance.reasons:
        items.append(
            DiffItem(field="local.gitignore", expected="vergil-managed fence", actual=reason)
        )


def _check_dependabot_yml(repo_root: Path, items: list[DiffItem]) -> None:
    """Assert ``.github/dependabot.yml`` is the canonical report-only file (epic #420).

    Exact-match against :func:`dependabot.render` for the repo's resolved
    primary language (via :func:`dependabot.check`), so a missing file, a local
    addition, or a stale render all fail. Each reason is appended as a
    ``DiffItem``.

    When ``vergil.toml`` fails to load, the language is unknown, so the file is
    not judged at all: the check appends one explicit failure naming the config
    error instead of comparing against a silent actions-only render (#3451).
    """
    path = repo_root / dependabot.DEPENDABOT_PATH
    text = path.read_text(encoding="utf-8") if path.is_file() else None

    try:
        lang = dependabot.resolve_language(repo_root)
    except ConfigError as exc:
        items.append(
            DiffItem(
                field="local.dependabot",
                expected="resolvable [project].primary-language",
                actual=f"cannot resolve the dependabot language: {exc}",
            )
        )
        return
    for reason in dependabot.check(text, lang):
        items.append(
            DiffItem(
                field="local.dependabot",
                expected="canonical report-only dependabot.yml",
                actual=reason,
            )
        )


def _check_required_workflows(repo_root: Path, items: list[DiffItem]) -> None:
    """Assert .github/workflows/ops.yml exists, wires the audit, and is scheduled.

    Three first-class non-compliances, each surfaced by
    :func:`audit_local_config` (the aggregate the fleet audits through), so a
    governance gap can't stay silent (epic #338, Task 12):

    1. A **missing** ops.yml — the nightly caller of ``vrg-github-repo-config
       apply`` is absent, so the repo never self-canonicalizes.
    2. A **present but unwired** ops.yml — it does not call the reusable
       ops-github-config workflow, so the nightly run audits nothing.
    3. A **present but un-scheduled** ops.yml — no ``cron`` trigger, so it can
       silently never run nightly.

    The remaining limitation is operational, not a detection gap: a repo
    *missing* ops.yml has no nightly workflow to run this check on itself, so
    for those repos the missing signal surfaces only when the audit is driven
    centrally (a manual run or a fleet sweep) — which is exactly what the
    Task 13 backfill uses to close the gap fleet-wide. (#311, #338)
    """
    ops = repo_root / ".github" / "workflows" / "ops.yml"
    if not ops.is_file():
        items.append(DiffItem(field="local.ops_workflow", expected="present", actual="missing"))
        return
    content = ops.read_text(encoding="utf-8")
    if "ops-github-config.yml" not in content:
        items.append(
            DiffItem(
                field="local.ops_workflow",
                expected="calls ops-github-config.yml",
                actual="ops.yml present but does not wire the config audit",
            )
        )
    if "cron:" not in content:
        items.append(
            DiffItem(
                field="local.ops_workflow",
                expected="scheduled (cron) trigger",
                actual="ops.yml present but has no schedule",
            )
        )


def audit_local_config(repo_root: Path) -> ConfigDiff:
    """Run all local config checks against a repo root directory."""
    items: list[DiffItem] = []
    _check_vergil_toml(repo_root, items)
    _check_hook_guard_shim(repo_root, items)
    _check_claude_md(repo_root, items)
    _check_claude_md_length(repo_root, items)
    _check_agents_md(repo_root, items)
    _check_claude_settings(repo_root, items)
    _check_workflow_refs(repo_root, items)
    _check_gitignore(repo_root, items)
    _check_dependabot_yml(repo_root, items)
    _check_required_workflows(repo_root, items)
    return ConfigDiff(items=items)


def _check_vergil_toml(repo_root: Path, items: list[DiffItem]) -> None:
    toml_path = repo_root / "vergil.toml"
    if not toml_path.is_file():
        items.append(
            DiffItem(
                field="local.vergil_toml",
                expected="present",
                actual="missing",
            )
        )
        return
    try:
        read_config(repo_root)
    except (ConfigError, FileNotFoundError) as exc:
        items.append(
            DiffItem(
                field="local.vergil_toml",
                expected="valid",
                actual=str(exc),
            )
        )


#: Markers delimiting the canonical-template region in a consuming
#: repo's CLAUDE.md. Exactly one begin/end pair; the lines between
#: them (ignoring leading/trailing blank lines) must equal the
#: template verbatim. Repo-local content outside the markers never
#: affects compliance. Issue #1439.
CLAUDE_MD_MARKER_BEGIN = "<!-- vergil:template:claude-md:begin -->"
CLAUDE_MD_MARKER_END = "<!-- vergil:template:claude-md:end -->"


def _check_claude_md(repo_root: Path, items: list[DiffItem]) -> None:
    drift = claude_md_drift(repo_root)
    if drift is not None:
        items.append(drift.item)


def claude_md_drift(repo_root: Path) -> ManagedFileDrift | None:
    """Return how CLAUDE.md's template region drifts, or ``None`` when it matches.

    Only the region between the markers is managed. The fix replaces the
    lines between the two marker lines with the template, verbatim, and
    keeps every byte outside them, the marker lines and their line ends
    included, and the file's mode. A region that matches apart from blank
    lines next to the markers is not drift. A missing CLAUDE.md, or one with
    absent or malformed markers, is drift with no fix: where the region
    belongs cannot be known without guessing.
    """
    claude_md = repo_root / CLAUDE_MD_PATH
    if not claude_md.is_file():
        item = DiffItem(field="local.claude_md", expected="present", actual="missing")
        return ManagedFileDrift(item=item, fix=None)

    # Decoded from bytes, never read in text mode, so the line ends the fix
    # keeps survive unchanged. ``splitlines`` with and without ``keepends``
    # split at the same places, so a marker index is valid in both lists.
    text = claude_md.read_bytes().decode("utf-8")
    lines = text.splitlines()
    try:
        begin, end = claude_md_marker_lines(lines)
    except ClaudeMdMarkerError as exc:
        item = DiffItem(
            field="local.claude_md",
            expected="well-formed template markers",
            actual=str(exc),
        )
        return ManagedFileDrift(item=item, fix=None)

    divergence = claude_md_region_divergence(lines, begin, end)
    if divergence is None:
        return None
    raw_lines = text.splitlines(keepends=True)
    template = load_claude_md_template()
    if not template.endswith("\n"):
        template += "\n"
    new_text = "".join(raw_lines[: begin + 1]) + template + "".join(raw_lines[end:])
    fix = FileFix(
        path=CLAUDE_MD_PATH,
        data=new_text.encode("utf-8"),
        mode=claude_md.stat().st_mode & 0o777,
    )
    return ManagedFileDrift(item=divergence, fix=fix)


class ClaudeMdMarkerError(ValueError):
    """A CLAUDE.md whose template markers are absent or malformed.

    The message is the diagnostic the audit reports as the actual value, for
    example ``"no template markers"`` or ``"multiple begin markers"``.
    """


def load_claude_md_template() -> str:
    """Return the packaged consumer template that the marked region must carry."""
    return _load_template()


def claude_md_marker_lines(lines: list[str]) -> tuple[int, int]:
    """Return the indexes of the begin and end marker lines in ``lines``.

    ``lines`` is the CLAUDE.md text split with :meth:`str.splitlines`. A
    marker line matches after stripping surrounding whitespace. Raises
    :class:`ClaudeMdMarkerError` unless there is exactly one begin marker
    followed by exactly one end marker. This is the one marker parser: the
    audit, its fixer and ``vrg-claude-md-sync`` all use it.
    """
    begins = [i for i, line in enumerate(lines) if line.strip() == CLAUDE_MD_MARKER_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == CLAUDE_MD_MARKER_END]
    malformed = _marker_structure_error(begins, ends)
    if malformed is not None:
        raise ClaudeMdMarkerError(malformed)
    return begins[0], ends[0]


def claude_md_region_divergence(lines: list[str], begin: int, end: int) -> DiffItem | None:
    """Describe where the marked region leaves the template, or ``None`` when it matches.

    ``begin`` and ``end`` are the marker line indexes from
    :func:`claude_md_marker_lines`. Leading and trailing blank lines inside
    the markers are ignored; every other line must equal the template's.
    """
    template_lines = _load_template().splitlines()
    start, stop = _region_bounds(lines, begin, end)
    region = lines[start:stop]
    if region == template_lines:
        return None
    return _first_divergence(region, start, stop, template_lines)


def _check_claude_md_length(repo_root: Path, items: list[DiffItem]) -> None:
    """Hold CLAUDE.md to the line budget, template region included.

    A missing CLAUDE.md is reported by :func:`_check_claude_md`, not here.
    """
    claude_md = repo_root / "CLAUDE.md"
    if not claude_md.is_file():
        return
    count = len(claude_md.read_text(encoding="utf-8").splitlines())
    if count > CLAUDE_MD_MAX_LINES:
        items.append(
            DiffItem(
                field="local.claude_md_length",
                expected=f"at most {CLAUDE_MD_MAX_LINES} lines",
                actual=f"{count} lines",
            )
        )


def _check_agents_md(repo_root: Path, items: list[DiffItem]) -> None:
    """Fail whenever AGENTS.md exists: CLAUDE.md is the one instruction file.

    A symlink counts, dangling or not.
    """
    agents_md = repo_root / "AGENTS.md"
    if agents_md.exists() or agents_md.is_symlink():
        items.append(DiffItem(field="local.agents_md", expected="absent", actual="present"))


def _marker_structure_error(begins: list[int], ends: list[int]) -> str | None:
    """Return a diagnostic for malformed markers, or None if well-formed."""
    if not begins and not ends:
        return "no template markers"
    if len(begins) > 1:
        return "multiple begin markers"
    if len(ends) > 1:
        return "multiple end markers"
    if not ends:
        return "begin marker without end marker"
    if not begins:
        return "end marker without begin marker"
    if ends[0] < begins[0]:
        return "end marker before begin marker"
    return None


def _region_bounds(lines: list[str], begin: int, end: int) -> tuple[int, int]:
    """Return the marked region's line slice, without leading/trailing blank lines."""
    start = begin + 1
    stop = end
    while start < stop and not lines[start].strip():
        start += 1
    while stop > start and not lines[stop - 1].strip():
        stop -= 1
    return start, stop


def _first_divergence(
    region: list[str],
    start: int,
    stop: int,
    template_lines: list[str],
) -> DiffItem:
    """Describe the first line where the marked region leaves the template.

    Reports both the template line number and the absolute CLAUDE.md line
    number, so drift is fixable without manual diffing. Only called once the
    region is known to differ, so a divergent line always exists.
    """
    offset = next(
        i
        for i in range(max(len(region), len(template_lines)))
        if (region[i] if i < len(region) else None)
        != (template_lines[i] if i < len(template_lines) else None)
    )
    expected = (
        f"template line {offset + 1}: {template_lines[offset]}"
        if offset < len(template_lines)
        else "end of template"
    )
    actual = (
        f"CLAUDE.md line {start + offset + 1}: {region[offset]}"
        if offset < len(region)
        else f"CLAUDE.md line {stop + 1}: end of marked region"
    )
    return DiffItem(field="local.claude_md", expected=expected, actual=actual)


def _check_hook_guard_shim(repo_root: Path, items: list[DiffItem]) -> None:
    drift = hook_guard_shim_drift(repo_root)
    if drift is not None:
        items.append(drift.item)


def hook_guard_shim_drift(repo_root: Path) -> ManagedFileDrift | None:
    """Return how ``.claude/hooks/guard.sh`` drifts, or ``None`` when it is correct.

    Correct is the packaged shim byte for byte, with every execute bit set:
    Claude Code cannot run a non-executable hook. A stale copy (one that does
    not fail closed) or a local edit is drift. The comparison is on bytes, so
    a CRLF copy, which bash cannot run, is drift too. The fix writes the
    packaged shim, mode 0755. An unreadable file is reported, never passed,
    and has no fix.
    """
    shim_path = repo_root / HOOK_GUARD_SHIM_PATH
    managed = render_hook_guard_shim().encode("utf-8")
    fix = FileFix(path=HOOK_GUARD_SHIM_PATH, data=managed, mode=HOOK_GUARD_SHIM_MODE)
    if not shim_path.is_file():
        item = DiffItem(field="local.hook_guard_shim", expected="present", actual="missing")
        return ManagedFileDrift(item=item, fix=fix)
    try:
        current = shim_path.read_bytes()
    except OSError as exc:
        item = DiffItem(
            field="local.hook_guard_shim",
            expected="readable",
            actual=f"unreadable ({exc})",
        )
        return ManagedFileDrift(item=item, fix=None)
    if current != managed:
        item = DiffItem(
            field="local.hook_guard_shim",
            expected="matches the packaged shim (src/vergil_tooling/data/hook_guard_shim.sh)",
            actual="differs from the packaged shim (stale or locally edited)",
        )
        return ManagedFileDrift(item=item, fix=fix)
    mode = shim_path.stat().st_mode
    if mode & _EXEC_BITS != _EXEC_BITS:
        item = DiffItem(
            field="local.hook_guard_shim",
            expected="executable (mode 0755)",
            actual=f"not executable (mode {mode & 0o777:04o})",
        )
        return ManagedFileDrift(item=item, fix=fix)
    return None


def _check_claude_settings(repo_root: Path, items: list[DiffItem]) -> None:
    drift = claude_settings_drift(repo_root)
    if drift is not None:
        items.append(drift.item)


def claude_settings_drift(repo_root: Path) -> ManagedFileDrift | None:
    """Return how ``.claude/settings.json`` drifts, or ``None`` when it is correct.

    The file is fully managed: correct is equal to the template after JSON
    parsing, so key order and whitespace never count. The fix replaces the
    whole file with :func:`render_claude_settings` (it never merges keys),
    keeping the file's mode, or 0644 for a new file. A file that is not
    valid JSON or not a JSON object is replaced too. An unreadable file is
    reported and has no fix.
    """
    settings_path = repo_root / CLAUDE_SETTINGS_PATH
    if not settings_path.is_file():
        item = DiffItem(field="local.claude_settings", expected="present", actual="missing")
        return ManagedFileDrift(item=item, fix=_claude_settings_fix(NEW_FILE_MODE))

    try:
        text = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        item = DiffItem(field="local.claude_settings", expected="valid JSON", actual=str(exc))
        return ManagedFileDrift(item=item, fix=None)

    fix = _claude_settings_fix(settings_path.stat().st_mode & 0o777)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        item = DiffItem(field="local.claude_settings", expected="valid JSON", actual=str(exc))
        return ManagedFileDrift(item=item, fix=fix)

    if not isinstance(raw, dict):
        item = DiffItem(
            field="local.claude_settings",
            expected="JSON object",
            actual=type(raw).__name__,
        )
        return ManagedFileDrift(item=item, fix=fix)

    template = _load_settings_template()
    if raw == template:
        return None
    item = DiffItem(
        field="local.claude_settings",
        expected=(
            "equal to the template (src/vergil_tooling/data/claude_settings.json) "
            "after JSON parsing"
        ),
        actual=_describe_settings_drift(raw, template),
    )
    return ManagedFileDrift(item=item, fix=fix)


def _claude_settings_fix(mode: int) -> FileFix:
    data = render_claude_settings().encode("utf-8")
    return FileFix(path=CLAUDE_SETTINGS_PATH, data=data, mode=mode)


def _describe_settings_drift(raw: dict[str, Any], template: dict[str, Any]) -> str:
    """Name the top-level keys that keep ``raw`` from equalling ``template``."""
    parts = [f"missing key: {k}" for k in template if k not in raw]
    parts += [f"unexpected key: {k}" for k in raw if k not in template]
    parts += [f"differs: {k}" for k in template if k in raw and raw[k] != template[k]]
    return "; ".join(parts)


def _check_workflow_refs(repo_root: Path, items: list[DiffItem]) -> None:
    """Assert every vergil-* reusable-workflow pin matches the vergil.toml version.

    Unlike the marketplace ref, workflow pins use the version even for the
    marketplace source repo — the plugin repo still consumes vergil-actions at a
    pinned version.
    """
    try:
        expected = read_source_version(repo_root)
    except (UpdateDepsError, OSError, ValueError):
        return  # vergil.toml problems are reported by _check_vergil_toml
    for path, actual in iter_workflow_refs(repo_root):
        if actual != expected:
            items.append(
                DiffItem(
                    field="local.workflow_ref",
                    expected=f"{path.name}: {expected}",
                    actual=f"{path.name}: {actual}",
                )
            )
