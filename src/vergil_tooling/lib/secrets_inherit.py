"""The ``secrets: inherit`` allow-list (epic vergil-project/.github#1384).

The automation App keys, and every other secret that can write or act, live
only in each repo's ``automation`` deployment environment. Environment
secrets reach a reusable workflow in another repository only when the caller
passes ``secrets: inherit`` (the probe recorded at vergil-actions
``cd-release.yml``, vergil-project/packages#6), and a job that calls a
reusable workflow cannot declare an environment. So the callers of the
vergil-actions workflows that read those secrets pass ``secrets: inherit``,
and every other caller keeps an explicit secrets map: inheriting hands the
callee every secret the caller can read.

:data:`INHERIT_ALLOWED` names those workflows, from the inventory of
vergil-project/vergil-actions#1339 plus the audit workflow of the epic's
spec. :func:`inherit_violations` finds each ``secrets: inherit`` in a
workflow whose job calls anything else. ``vrg-validate``'s common stage runs
it over ``.github/workflows/`` as the ``secrets-inherit`` check, which
replaces semgrep's registry rule of the same name (excluded fleet-wide in
:data:`~vergil_tooling.lib.semgrep.DEFAULT_EXCLUDED_RULES`), so the rule has
one definition.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

#: The vergil-actions reusable workflows a caller may pass ``secrets: inherit``
#: to: those that read the automation App keys or another secret from the
#: ``automation`` environment, or a signing key from their own environment.
INHERIT_ALLOWED: tuple[str, ...] = (
    "cd-docs-refresh.yml",
    "cd-docs.yml",
    "cd-release.yml",
    "ops-agent-audit.yml",
    "ops-epic-rollup.yml",
    "ops-epic-sweep.yml",
    "ops-github-config.yml",
    "ops-upstream-refresh.yml",
    "publish-index.yml",
)

#: The repository that holds the allow-listed workflows, lower-cased (GitHub
#: owner and repository names are case-insensitive).
ACTIONS_REPO = "vergil-project/vergil-actions"

_WORKFLOWS_DIR = ".github/workflows/"


@dataclass(frozen=True)
class InheritViolation:
    """A ``secrets: inherit`` the allow-list refuses."""

    #: The 1-based line of the ``inherit`` value.
    line: int
    #: The job's id.
    job: str
    #: The job's ``uses:`` value, or ``None`` when it has none.
    uses: str | None


def inherit_allowed(uses: str) -> bool:
    """Whether a job calling *uses* may pass ``secrets: inherit``.

    *uses* must name an allow-listed workflow, either in
    :data:`ACTIONS_REPO` at any ref
    (``vergil-project/vergil-actions/.github/workflows/<name>@<ref>``) or,
    for vergil-actions calling its own workflows, as a local path
    (``./.github/workflows/<name>``). A workflow of that name in any other
    repository is refused.
    """
    if uses.startswith("./"):
        path = uses[2:]
    else:
        target, sep, ref = uses.partition("@")
        if not sep or not ref:
            return False
        owner, _, rest = target.partition("/")
        repo, _, path = rest.partition("/")
        if f"{owner}/{repo}".lower() != ACTIONS_REPO:
            return False
    if not path.startswith(_WORKFLOWS_DIR):
        return False
    return path.removeprefix(_WORKFLOWS_DIR) in INHERIT_ALLOWED


def inherit_violations(text: str) -> list[InheritViolation]:
    """Return every ``secrets: inherit`` in the workflow *text* that the
    allow-list refuses, in line order.

    A job's ``secrets`` that is exactly ``inherit`` is checked against its
    ``uses`` (:func:`inherit_allowed`); one with no string ``uses`` is always
    refused. An explicit secrets map is never refused. A workflow with no
    ``jobs`` mapping has nothing to check (actionlint reports its shape).
    Raises :class:`yaml.YAMLError` when *text* is not YAML, so a workflow
    that cannot be read never passes unread.
    """
    violations: list[InheritViolation] = []
    for document in yaml.compose_all(text, Loader=yaml.SafeLoader):
        jobs = _mapping_value(document, "jobs")
        if not isinstance(jobs, yaml.MappingNode):
            continue
        for job_key, job in jobs.value:
            if not isinstance(job, yaml.MappingNode):
                continue
            secrets = _mapping_value(job, "secrets")
            if not isinstance(secrets, yaml.ScalarNode) or secrets.value != "inherit":
                continue
            uses_node = _mapping_value(job, "uses")
            uses = uses_node.value if isinstance(uses_node, yaml.ScalarNode) else None
            if uses is not None and inherit_allowed(uses):
                continue
            violations.append(
                InheritViolation(
                    line=secrets.start_mark.line + 1, job=str(job_key.value), uses=uses
                )
            )
    return sorted(violations, key=lambda violation: violation.line)


def _mapping_value(node: yaml.Node | None, key: str) -> yaml.Node | None:
    """The value under the scalar *key* of the mapping *node*, or ``None``."""
    if not isinstance(node, yaml.MappingNode):
        return None
    pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
    for key_node, value in pairs:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value
    return None
