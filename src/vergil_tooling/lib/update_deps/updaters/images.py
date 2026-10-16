"""Container image updater (spec §8.3, epic vergil-project/.github#381).

Scans Dockerfile ``FROM`` lines, ``container:``/``image:`` values in workflow
files, and ``*image`` input defaults in ``action.yml``/``action.yaml``.

- Exact ``x.y.z`` tags (optional ``v`` prefix) bump to the newest tag with the
  same major (major 0 included). A newer major is reported, never bumped.
- Numeric series tags (one or two numeric parts plus an optional suffix:
  ``3.14-slim``, ``24.04``, ``16``) float and are only reported when a newer
  series with the same suffix exists.
- ``:latest``, digests, expressions and ``ghcr.io/vergil-project/*`` are
  skipped (see ``registry.parse_image_ref``), as is any other tag shape
  (codenames such as ``trixie-slim``).

Registry lookups are injected; a lookup failure fails loud.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

import yaml

from vergil_tooling.lib import git
from vergil_tooling.lib.update_deps import registry
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.registry import ImageRef, parse_image_ref
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

_EXACT_RE = re.compile(r"^(v?)(\d+)\.(\d+)\.(\d+)$")
_SERIES_RE = re.compile(r"^(\d+)(?:\.(\d+))?((?:-[A-Za-z0-9._-]+)?)$")
_FROM_RE = re.compile(r"^[ \t]*FROM[ \t]+(?:--\S+[ \t]+)*(\S+)", re.IGNORECASE | re.MULTILINE)
_WF_RE = re.compile(r"""^[ \t]*(?:image|container):[ \t]*["']?([^\s"'#]+)""", re.MULTILINE)
# Characters that may be part of an image ref; a match must not sit inside a longer one.
_REF_CHARS = r"A-Za-z0-9._/:@-"

NOTE_MAJOR = "newer major; never bumped automatically"
NOTE_SERIES = "newer series; a judgement call"


def _is_dockerfile(path: str) -> bool:
    name = PurePosixPath(path).name
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return True
    return name.lower().endswith(".dockerfile")


def _is_workflow(path: str) -> bool:
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))


def _is_action(path: str) -> bool:
    return PurePosixPath(path).name in ("action.yml", "action.yaml")


def _scanned(path: str) -> bool:
    return _is_dockerfile(path) or _is_workflow(path) or _is_action(path)


def _action_image_defaults(text: str) -> list[str]:
    data = yaml.safe_load(text)
    inputs = data.get("inputs") if isinstance(data, dict) else None
    if not isinstance(inputs, dict):
        return []
    return [
        spec["default"]
        for name, spec in inputs.items()
        if str(name).endswith("image")
        and isinstance(spec, dict)
        and isinstance(spec.get("default"), str)
    ]


def find_refs(base: Path, files: list[str]) -> list[tuple[Path, ImageRef]]:
    """Every updatable image ref in the scanned *files* (paths relative to *base*)."""
    found: list[tuple[Path, ImageRef]] = []
    for rel in files:
        if not _scanned(rel):
            continue
        path = base / rel
        text = path.read_text()
        if _is_dockerfile(rel):
            candidates = _FROM_RE.findall(text)
        elif _is_workflow(rel):
            candidates = _WF_RE.findall(text)
        else:
            candidates = _action_image_defaults(text)
        for candidate in candidates:
            ref = parse_image_ref(candidate)
            if ref is not None:
                found.append((path, ref))
    return found


def _display(ref: ImageRef) -> str:
    """The image name as written (the ref without its tag)."""
    return ref.text.rsplit(":", 1)[0]


def _plan_exact(
    ref: ImageRef, exact: re.Match[str], tags: list[str]
) -> tuple[str | None, HeldBack | None]:
    prefix = exact[1]
    cur = (int(exact[2]), int(exact[3]), int(exact[4]))
    versions = [
        ((int(m[2]), int(m[3]), int(m[4])), t)
        for t in tags
        if (m := _EXACT_RE.match(t)) is not None and m[1] == prefix
    ]
    same = [v for v in versions if v[0][0] == cur[0] and v[0] > cur]
    higher = [v for v in versions if v[0][0] > cur[0]]
    new = max(same)[1] if same else None
    entry = (
        HeldBack(
            source="images",
            name=_display(ref),
            current=ref.tag,
            latest=max(higher)[1],
            note=NOTE_MAJOR,
        )
        if higher
        else None
    )
    return new, entry


def _series_key(match: re.Match[str]) -> tuple[int, ...]:
    return tuple(int(x) for x in match.groups()[:2] if x is not None)


def _plan_series(ref: ImageRef, series: re.Match[str], tags: list[str]) -> HeldBack | None:
    suffix = series[3]
    cur = _series_key(series)
    newer = [
        (key, t)
        for t in tags
        if (m := _SERIES_RE.match(t)) is not None
        and m[3] == suffix
        and len(key := _series_key(m)) == len(cur)
        and key > cur
    ]
    if not newer:
        return None
    return HeldBack(
        source="images",
        name=_display(ref),
        current=ref.tag,
        latest=max(newer)[1],
        note=NOTE_SERIES,
    )


def plan(ref: ImageRef, tags: list[str]) -> tuple[str | None, HeldBack | None]:
    """The tag to bump *ref* to (or None), and its held-back entry (or None)."""
    exact = _EXACT_RE.match(ref.tag)
    if exact is not None:
        return _plan_exact(ref, exact, tags)
    series = _SERIES_RE.match(ref.tag)
    if series is None:
        return None, None
    return None, _plan_series(ref, series, tags)


def _replace_ref(text: str, old: str, new: str) -> tuple[str, int]:
    """Replace every standalone occurrence of *old* (not part of a longer ref).

    Every occurrence changes, so two copies of one pinned ref never diverge.
    Returns the new text and the number of replacements.
    """
    pattern = rf"(?<![{_REF_CHARS}]){re.escape(old)}(?![{_REF_CHARS}])"
    return re.subn(pattern, lambda _: new, text)


def _base(ctx: UpdateDepsContext) -> Path:
    """The directory updaters operate on — the worktree once preflight made it."""
    return ctx.worktree_path if ctx.worktree_path is not None else ctx.repo_root


def _tracked_files(base: Path) -> list[str]:
    return [line for line in git.read_output("-C", str(base), "ls-files").splitlines() if line]


class ImagesUpdater:
    """Bump exact image tags within their major; report newer majors and series."""

    name = "images"

    def __init__(self, list_tags: Callable[[ImageRef], list[str]] = registry.list_tags) -> None:
        self._list_tags = list_tags

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return any(_scanned(f) for f in _tracked_files(_base(ctx)))

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        base = _base(ctx)
        refs = find_refs(base, _tracked_files(base))
        held: list[HeldBack] = []
        cache: dict[tuple[str, str], list[str]] = {}
        # Per file, old ref text -> new ref text (dicts keep discovery order).
        edits: dict[Path, dict[str, str]] = {}
        for path, ref in refs:
            key = (ref.registry, ref.repository)
            if key not in cache:
                cache[key] = self._list_tags(ref)
            new_tag, entry = plan(ref, cache[key])
            if entry is not None and entry not in held:
                held.append(entry)
            if new_tag is not None:
                edits.setdefault(path, {})[ref.text] = f"{_display(ref)}:{new_tag}"
        bumped: list[str] = []
        for path, replacements in edits.items():
            text = path.read_text()
            for old, new in replacements.items():
                text, count = _replace_ref(text, old, new)
                if count == 0:
                    msg = f"Could not locate image ref {old!r} in {path} to bump it to {new!r}."
                    raise UpdateDepsError(phase="update", command="images", message=msg)
                change = f"{old} → {new}"
                if change not in bumped:
                    bumped.append(change)
            path.write_text(text)
        return UpdateResult(
            updater=self.name,
            changed=bool(bumped),
            summary="; ".join(bumped) if bumped else "image tags already newest within their major",
            commit_message="chore(deps): bump image tags",
            held_back=held,
        )
