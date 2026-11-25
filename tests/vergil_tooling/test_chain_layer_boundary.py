"""The raw sub-issue layer stays inside the chain layer (spec §4.2 of epic .github#1609).

A raw sub-issue writer, or a raw GraphQL or REST selection of an issue's
``parent`` or ``subIssues``, outside ``lib/epics.py`` and ``lib/epic_chain.py``
would act on one container where the caller means the whole chain: it would
see half an epic, or link an item past GitHub's 100-child limit. The source
is parsed with :mod:`ast`, so a comment never counts, and every string form
(plain, f-string and t-string) is seen alike: each literal part of an
f-string or t-string is its own string constant.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[2] / "src" / "vergil_tooling"

# The chain layer: the only modules that may touch the raw sub-issue layer.
_CHAIN_LAYER = frozenset({Path("lib/epics.py"), Path("lib/epic_chain.py")})

# The raw writers, public and private.
_RAW_WRITERS = frozenset(
    {
        "add_child",
        "remove_child",
        "reparent_child",
        "_add_child",
        "_remove_child",
        "_reparent_child",
        "_add_child_unchecked",
    }
)

# A raw selection of the sub-issue layer, as GraphQL or REST.
_RAW_SELECTIONS = (
    re.compile(r"\bsubIssues\s*[({]"),
    re.compile(r"\bparent\s*\{\s*number"),
    re.compile(r"\baddSubIssue\s*\("),
    re.compile(r"\bremoveSubIssue\s*\("),
    re.compile(r"/sub_issues"),
)


def _violations(source: str) -> list[tuple[int, str]]:
    """Every raw-layer use in *source*, as (line, what), in source order."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name) and node.id in _RAW_WRITERS:
            found.append((node.lineno, f"raw writer {node.id}"))
        elif isinstance(node, ast.Attribute) and node.attr in _RAW_WRITERS:
            found.append((node.lineno, f"raw writer {node.attr}"))
        elif isinstance(node, ast.alias) and node.name.rpartition(".")[2] in _RAW_WRITERS:
            found.append((node.lineno, f"raw writer {node.name}"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.extend(
                (node.lineno, f"raw selection {pattern.pattern!r}")
                for pattern in _RAW_SELECTIONS
                if pattern.search(node.value)
            )
    return sorted(found)


def test_no_raw_subissue_access_outside_chain_layer() -> None:
    files = sorted(_SRC.rglob("*.py"))
    assert files, f"no sources found under {_SRC}"
    failures = [
        f"{path.relative_to(_SRC)}:{line}: {what}"
        for path in files
        if path.relative_to(_SRC) not in _CHAIN_LAYER
        for line, what in _violations(path.read_text(encoding="utf-8"))
    ]
    assert not failures, (
        "raw sub-issue access outside lib/epics.py and lib/epic_chain.py; "
        "go through epic_chain (link_into, move_into, unlink_from) or the "
        "chain-aware readers in epics instead:\n" + "\n".join(failures)
    )


def test_chain_layer_modules_exist() -> None:
    # A rename of either module would silently widen the scan's exemption.
    for module in _CHAIN_LAYER:
        assert (_SRC / module).is_file(), module


# -- the scan itself ----------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        # Docstrings and messages that merely name the layer (intake_route,
        # epic_audit, planning_close, vrg-epic-audit and vrg-epic-move today).
        '"""Recover after GitHub refuses every ``addSubIssue`` under *epic*."""',
        "# GitHub's addSubIssue refusal when the parent already lists the child.\nx = 1",
        '"""The replacement for ``gh issue view <epic> --json subIssues --jq``."""',
        'where = "no parent" if parent is None else f"parent {parent.slug}"',
        'msg = "without brittle jq against GitHub\'s raw subIssues shape. "',
        '"""Moves in one atomic re-parent (``addSubIssue`` with ``replaceParent``)."""',
        # A comment is never a use.
        "# epics.add_child(epic, task)\nx = 1",
        # A name that merely contains a writer's name is not the writer.
        "def add_children(): ...\nadd_child_count = 0",
    ],
)
def test_scan_passes_names_that_only_mention_the_layer(source: str) -> None:
    assert _violations(source) == []


@pytest.mark.parametrize(
    ("source", "what"),
    [
        # vrg_security_record's resume lookup before Task 7 (#5213).
        (
            'path = f"repos/{intake.owner}/{intake.repo}/issues/{intake.number}/sub_issues"',
            "/sub_issues",
        ),
        (
            'q = f"""query {{ repository(name: "{repo}") {{ issue(number: {n}) {{\n'
            '  subIssues(first: 100) {{ nodes {{ number }} }} }} }} }}"""',
            "subIssues",
        ),
        ('q = t"""{{ issue(number: {n}) {{ subIssues {{ totalCount }} }} }}"""', "subIssues"),
        ('q = """{ issue(number: 1) { parent { number title } } }"""', "parent"),
        ('q = "mutation { addSubIssue(input: $input) { issue { id } } }"', "addSubIssue"),
        ('q = "mutation { removeSubIssue(input: $input) { issue { id } } }"', "removeSubIssue"),
        ("epics.add_child(epic, task)", "raw writer add_child"),
        ("epics._remove_child(epic, task)", "raw writer _remove_child"),
        ("from vergil_tooling.lib.epics import reparent_child", "raw writer reparent_child"),
        ("write = _add_child", "raw writer _add_child"),
    ],
)
def test_scan_flags_raw_access(source: str, what: str) -> None:
    found = _violations(source)
    assert found, source
    assert any(what in hit for _, hit in found), found


def test_scan_reports_each_line() -> None:
    source = 'x = 1\nepics.add_child(a, b)\ny = "/sub_issues"\n'
    assert [line for line, _ in _violations(source)] == [2, 3]
