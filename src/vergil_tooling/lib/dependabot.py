"""Canonical report-only ``.github/dependabot.yml`` (epic vergil-project/.github#420).

The fleet runs Dependabot in *report-only* mode: alerts are on, but Dependabot
must never open a pull request. The repo setting "Dependabot security updates"
stays disabled, and every ``updates`` entry in ``dependabot.yml`` carries
``open-pull-requests-limit: 0`` (Dependabot's default is 5).

This module is the single source of truth for the file. Scaffolding
(``vrg-repo-init``), the single-repo applicator (``vrg-dependabot-sync``), the
fleet sweep (``vrg-fleet-sync --target dependabot``) and the ``vrg-validate``
limit-0 guard all go through it, so they cannot drift apart.

* :func:`render` — the exact canonical text for a primary language.
* :func:`check` — exact-match compliance against that render (no local
  additions are allowed).
* :func:`limit_violations` — the narrower structural guard ``vrg-validate``
  runs: every entry must have ``open-pull-requests-limit: 0``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from vergil_tooling.lib.config import declared_primary_language

DEPENDABOT_PATH = Path(".github/dependabot.yml")

# Primary language -> Dependabot ``package-ecosystem`` value. Languages absent
# here (cpp, shell, none, ...) get the ``github-actions`` entry only. Java is
# ``maven`` only: the fleet's Java builds use Maven (pom.xml / mvnw).
LANGUAGE_ECOSYSTEMS: dict[str, str] = {
    "python": "uv",
    "go": "gomod",
    "rust": "cargo",
    "java": "maven",
    "typescript": "npm",
}

_HEADER = """\
# Managed by vergil-tooling (epic vergil-project/.github#420). Do not edit.
# Report-only: Dependabot alerts are on; Dependabot never opens PRs here.
# open-pull-requests-limit: 0 on every entry is enforced by vrg-validate
# and the nightly repo-config audit.
version: 2
updates:
"""

_ENTRY = """\
  - package-ecosystem: "{ecosystem}"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 0
"""

_LIMIT_KEY = "open-pull-requests-limit"


def render(lang: str | None) -> str:
    """Return the canonical ``dependabot.yml`` text for primary language *lang*.

    Built as text (not ``yaml.dump``) so the bytes are stable and exact-match
    comparison is meaningful. ``github-actions`` always comes first; the
    language's ecosystem, if mapped, follows.
    """
    ecosystems = ["github-actions"]
    mapped = LANGUAGE_ECOSYSTEMS.get(lang or "")
    if mapped is not None:
        ecosystems.append(mapped)
    return _HEADER + "".join(_ENTRY.format(ecosystem=eco) for eco in ecosystems)


def resolve_language(repo_root: Path) -> str | None:
    """Return the repo's asserted ``[project].primary-language``, or ``None``.

    An absent ``vergil.toml`` or an undeclared language resolves to ``None``
    (the actions-only render). A config that fails to load raises
    :class:`~vergil_tooling.lib.config.ConfigError`: rendering it as
    actions-only would silently drop the language's ecosystem (#3451), so every
    caller must surface the error. Unmapped languages are returned unchanged;
    :func:`render` maps them to the actions-only file.
    """
    return declared_primary_language(repo_root)


def check(text: str | None, lang: str | None) -> list[str]:
    """Return the reasons *text* is not the canonical file; empty means compliant.

    *text* is ``None`` when the file does not exist.
    """
    if text is None:
        return [f"missing {DEPENDABOT_PATH.as_posix()}"]
    if text != render(lang):
        return ["differs from the canonical render"]
    return []


def limit_violations(text: str) -> list[str]:
    """Return one reason per ``updates`` entry that could open a pull request.

    An entry complies only when ``open-pull-requests-limit`` is present and is
    the integer ``0`` (a missing key means Dependabot's default of 5). A file
    that cannot be parsed, or whose shape is not a mapping with an ``updates``
    list of mappings, yields a single ``unparseable: ...`` reason. A file with
    no ``updates`` at all can open no PRs and is compliant.
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        first_line = (str(exc).strip().splitlines() or [""])[0]
        return [f"unparseable: {first_line}"]
    if data is None:
        return []
    if not isinstance(data, dict):
        return ["unparseable: top level is not a mapping"]
    updates = data.get("updates")
    if updates is None:
        return []
    if not isinstance(updates, list):
        return ["unparseable: 'updates' is not a list"]
    violations: list[str] = []
    for entry in updates:
        if not isinstance(entry, dict):
            return ["unparseable: an 'updates' entry is not a mapping"]
        name = entry.get("package-ecosystem") or "<unknown ecosystem>"
        if _LIMIT_KEY not in entry:
            violations.append(
                f"updates entry {name} is missing {_LIMIT_KEY} "
                "(Dependabot defaults to 5); it must be 0"
            )
            continue
        limit = entry[_LIMIT_KEY]
        if type(limit) is not int or limit != 0:
            violations.append(f"updates entry {name} has {_LIMIT_KEY}: {limit!r}; it must be 0")
    return violations
