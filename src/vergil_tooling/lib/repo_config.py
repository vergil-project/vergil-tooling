"""Local repository configuration audit checks.

Checks files on disk — vergil.toml, CLAUDE.md, .claude/settings.json,
.claude/hooks/guard.sh, .gitignore, .github/dependabot.yml and
.github/workflows/ops.yml — without making any API calls.
"""

from __future__ import annotations

import importlib.resources
import json
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import dependabot, gitignore
from vergil_tooling.lib.config import ConfigError, declared_primary_language, read_config
from vergil_tooling.lib.github_config import ConfigDiff, DiffItem
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.vergil_refs import iter_workflow_refs, read_source_version

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


#: Packaged template files and their previous shapes. The previous shapes are
#: the bridge of epic vergil-project/.github#558: until the bridge-removal PR
#: merges, a repo still carrying one passes the audit with a deprecation
#: warning instead of a DiffItem, so no managed repo's nightly run goes red
#: while the fleet is swept.
CLAUDE_MD_TEMPLATE = "claude_md_consumer.md"
CLAUDE_MD_TEMPLATE_PREVIOUS = "claude_md_consumer_previous.md"
SETTINGS_TEMPLATE = "claude_settings.json"
SETTINGS_TEMPLATE_PREVIOUS = "claude_settings_previous.json"

SETTINGS_BRIDGE_WARNING = (
    ".claude/settings.json matches the previous template; replace it with the "
    "current template (epic vergil-project/.github#558)."
)
CLAUDE_MD_BRIDGE_WARNING = (
    "CLAUDE.md template region matches the previous template; replace it with the "
    "current template (epic vergil-project/.github#558)."
)


def _read_data_file(name: str) -> str:
    return (
        importlib.resources.files("vergil_tooling.data").joinpath(name).read_text(encoding="utf-8")
    )


def _load_template(previous: bool = False) -> str:
    return _read_data_file(CLAUDE_MD_TEMPLATE_PREVIOUS if previous else CLAUDE_MD_TEMPLATE)


def _load_settings_template(previous: bool = False) -> dict[str, Any]:
    name = SETTINGS_TEMPLATE_PREVIOUS if previous else SETTINGS_TEMPLATE
    template = json.loads(_read_data_file(name))
    if not isinstance(template, dict):  # pragma: no cover - packaging error
        msg = f"{name} template is not a JSON object"
        raise TypeError(msg)
    return template


def _bridged[T](
    matches: Callable[[T], bool],
    current: T,
    previous: T,
    warning: str,
    warnings: list[str],
) -> bool:
    """Accept the current template cleanly and the previous one with a warning.

    Returns True when either template matches (appending ``warning`` for the
    previous one) and False when neither does; the caller then records one
    DiffItem against the current template.
    """
    if matches(current):
        return True
    if matches(previous):
        warnings.append(warning)
        return True
    return False


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
    warnings: list[str] = []
    _check_vergil_toml(repo_root, items)
    _check_hook_guard_shim(repo_root, items)
    _check_claude_md(repo_root, items, warnings)
    _check_claude_settings(repo_root, items, warnings)
    _check_workflow_refs(repo_root, items)
    _check_gitignore(repo_root, items)
    _check_dependabot_yml(repo_root, items)
    _check_required_workflows(repo_root, items)
    return ConfigDiff(items=items, warnings=warnings)


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


def _check_claude_md(repo_root: Path, items: list[DiffItem], warnings: list[str]) -> None:
    claude_md = repo_root / "CLAUDE.md"
    if not claude_md.is_file():
        items.append(
            DiffItem(
                field="local.claude_md",
                expected="present",
                actual="missing",
            )
        )
        return

    content = claude_md.read_text(encoding="utf-8")
    template = _load_template()
    previous = _load_template(previous=True)
    lines = content.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == CLAUDE_MD_MARKER_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == CLAUDE_MD_MARKER_END]

    if not begins and not ends:
        # Legacy form (transition window): no markers, require a template as
        # a contiguous verbatim substring.
        if not _bridged(
            lambda t: t in content, template, previous, CLAUDE_MD_BRIDGE_WARNING, warnings
        ):
            items.append(
                DiffItem(
                    field="local.claude_md",
                    expected="template present",
                    actual="template not found",
                )
            )
        return

    malformed = _marker_structure_error(begins, ends)
    if malformed is not None:
        items.append(
            DiffItem(
                field="local.claude_md",
                expected="well-formed template markers",
                actual=malformed,
            )
        )
        return

    start, stop = _region_bounds(lines, begins[0], ends[0])
    region = lines[start:stop]
    if _bridged(
        lambda t: region == t.splitlines(), template, previous, CLAUDE_MD_BRIDGE_WARNING, warnings
    ):
        return
    items.append(_first_divergence(region, start, stop, template.splitlines()))


def _marker_structure_error(begins: list[int], ends: list[int]) -> str | None:
    """Return a diagnostic for malformed markers, or None if well-formed."""
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
    shim_path = repo_root / ".claude" / "hooks" / "guard.sh"
    if not shim_path.is_file():
        items.append(
            DiffItem(
                field="local.hook_guard_shim",
                expected="present",
                actual="missing",
            )
        )


def _check_claude_settings(repo_root: Path, items: list[DiffItem], warnings: list[str]) -> None:
    settings_path = repo_root / ".claude" / "settings.json"
    if not settings_path.is_file():
        items.append(
            DiffItem(
                field="local.claude_settings",
                expected="present",
                actual="missing",
            )
        )
        return

    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        items.append(
            DiffItem(
                field="local.claude_settings",
                expected="valid JSON",
                actual=str(exc),
            )
        )
        return

    if not isinstance(raw, dict):
        items.append(
            DiffItem(
                field="local.claude_settings",
                expected="JSON object",
                actual=type(raw).__name__,
            )
        )
        return

    template = _load_settings_template()
    if _bridged(
        lambda t: raw == t,
        template,
        _load_settings_template(previous=True),
        SETTINGS_BRIDGE_WARNING,
        warnings,
    ):
        return
    items.append(
        DiffItem(
            field="local.claude_settings",
            expected=(
                "equal to the template (src/vergil_tooling/data/claude_settings.json) "
                "after JSON parsing"
            ),
            actual=_describe_settings_drift(raw, template),
        )
    )


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
