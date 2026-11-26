"""Guard: nothing but the one PR path arms or merges a PR into ``develop``.

Epic vergil-project/.github#1664, spec §5.4 and plan Task 9. Every tool gets
its PR into ``develop`` through ``lib/pr_path.py``. The release PR into
``main`` is the one direct merge left, and each site that serves it asserts
a base of ``main``.

The scan reads the AST of every module under ``src/vergil_tooling/`` and
reports each

- call to ``github.merge`` (and any import of ``merge`` from ``github``);
- string holding ``enablePullRequestAutoMerge`` (docstrings aside);
- ``"pr", "merge"`` argument list (a list, tuple or call's arguments with the
  two strings side by side);
- call to ``wait_and_merge``.

Only the sites in :data:`_ALLOWED` may hold one, each with its reason. A new
hit anywhere else fails, naming the file and line; an allowed site that no
longer holds its hit fails too, so the list never outlives the code.
"""

from __future__ import annotations

import ast
import itertools
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import pr_merge
from vergil_tooling.lib.release import merge as release_merge
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import merge_release

if TYPE_CHECKING:
    from collections.abc import Iterator

_SRC = Path(__file__).resolve().parents[2] / "src" / "vergil_tooling"

_GITHUB_MERGE = "github.merge call"
_ENABLE = "enablePullRequestAutoMerge string"
_PR_MERGE_ARGS = '"pr", "merge" argument list'
_WAIT_AND_MERGE = "wait_and_merge call"

#: Every scope of a file: the path's own modules, which may arm and merge.
_ANY = "*"

#: The allowed sites: (file under src/vergil_tooling, enclosing function or
#: "<module>", kind of hit) -> why it is allowed.
_ALLOWED: dict[tuple[str, str, str], str] = {
    ("lib/github.py", "merge", _PR_MERGE_ARGS): (
        "defines the github.merge primitive, which only the sites below call"
    ),
    ("lib/pr_path.py", _ANY, _GITHUB_MERGE): "the one PR path",
    ("lib/pr_path.py", _ANY, _ENABLE): "the one PR path",
    ("lib/pr_path.py", _ANY, _PR_MERGE_ARGS): "the one PR path",
    ("lib/auto_merge.py", _ANY, _ENABLE): (
        "the auto-merge enable, private to the path (pr_path.arm is its one caller)"
    ),
    ("lib/auto_merge.py", _ANY, _GITHUB_MERGE): (
        "the direct merge of a clean-status PR, private to the path"
    ),
    ("lib/pr_merge.py", "wait_and_merge", _GITHUB_MERGE): (
        "the merge engine of the release PR into main, behind a base-is-main assertion"
    ),
    ("lib/release/merge.py", "wait_and_merge", _WAIT_AND_MERGE): (
        "the release's wrapper of the merge into main, behind a base-is-main assertion"
    ),
    ("lib/release/orchestrator.py", "merge_release", _WAIT_AND_MERGE): (
        "merge-release: the release PR into main, through release/merge.wait_and_merge"
    ),
    ("bin/vrg_gh.py", "<module>", _PR_MERGE_ARGS): (
        "vrg-gh's flag deny-list names the command it restricts; it merges nothing"
    ),
}


@dataclass(frozen=True)
class _Hit:
    path: str
    line: int
    scope: str
    kind: str

    def allowed_by(self) -> tuple[str, str, str] | None:
        for key in ((self.path, self.scope, self.kind), (self.path, _ANY, self.kind)):
            if key in _ALLOWED:
                return key
        return None


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the docstring nodes in *tree*: text about the code, not code."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _is_str(node: ast.AST, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def _has_pr_merge(items: list[ast.expr]) -> bool:
    return any(_is_str(a, "pr") and _is_str(b, "merge") for a, b in itertools.pairwise(items))


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, docstrings: set[int]) -> None:
        self.path = path
        self.docstrings = docstrings
        self.scopes: list[str] = []
        self.hits: list[_Hit] = []

    def _hit(self, node: ast.AST, kind: str) -> None:
        scope = self.scopes[-1] if self.scopes else "<module>"
        self.hits.append(_Hit(self.path, getattr(node, "lineno", 0), scope, kind))

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    visit_FunctionDef = _function  # noqa: N815
    visit_AsyncFunctionDef = _function  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "merge"
            and isinstance(func.value, ast.Name)
            and func.value.id == "github"
        ):
            self._hit(node, _GITHUB_MERGE)
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name == "wait_and_merge":
            self._hit(node, _WAIT_AND_MERGE)
        if _has_pr_merge(node.args):
            self._hit(node, _PR_MERGE_ARGS)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        if (node.module or "").endswith("github") and any(a.name == "merge" for a in node.names):
            self._hit(node, _GITHUB_MERGE)
        self.generic_visit(node)

    def _sequence(self, node: ast.List | ast.Tuple | ast.Set) -> None:
        if _has_pr_merge(node.elts):
            self._hit(node, _PR_MERGE_ARGS)
        self.generic_visit(node)

    visit_List = _sequence  # noqa: N815
    visit_Tuple = _sequence  # noqa: N815
    visit_Set = _sequence  # noqa: N815

    def visit_Constant(self, node: ast.Constant) -> None:  # noqa: N802
        if (
            isinstance(node.value, str)
            and "enablePullRequestAutoMerge" in node.value
            and id(node) not in self.docstrings
        ):
            self._hit(node, _ENABLE)


def _scan_source(path: str, source: str) -> list[_Hit]:
    tree = ast.parse(source, filename=path)
    scanner = _Scanner(path, _docstrings(tree))
    scanner.visit(tree)
    return scanner.hits


def _scan() -> list[_Hit]:
    hits: list[_Hit] = []
    for file in sorted(_SRC.rglob("*.py")):
        rel = file.relative_to(_SRC).as_posix()
        hits.extend(_scan_source(rel, file.read_text(encoding="utf-8")))
    return hits


def test_no_module_outside_pr_path_arms_or_merges_develop() -> None:
    hits = _scan()
    assert hits, f"the scan found nothing under {_SRC}: it is not reading the source"
    stray = [h for h in hits if h.allowed_by() is None]
    assert not stray, "a PR is armed or merged outside the one PR path (spec §5.4):\n" + "\n".join(
        f"  src/vergil_tooling/{h.path}:{h.line} ({h.scope}): {h.kind}" for h in stray
    )


def test_every_named_site_is_still_there() -> None:
    """An allowed site that no longer holds its hit is removed from the list."""
    used = {h.allowed_by() for h in _scan()}
    stale = [key for key in _ALLOWED if key[1] != _ANY and key not in used]
    assert not stale, f"allowed sites with nothing left to allow: {stale}"


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (
            "from vergil_tooling.lib import github\ndef f():\n    github.merge('1')\n",
            _GITHUB_MERGE,
        ),
        ("from vergil_tooling.lib.github import merge\n", _GITHUB_MERGE),
        ("Q = 'mutation { enablePullRequestAutoMerge(input: {}) }'\n", _ENABLE),
        ("def f(run):\n    run('pr', 'merge', '--auto', '1')\n", _PR_MERGE_ARGS),
        ("ARGS = ['gh', 'pr', 'merge', '--squash']\n", _PR_MERGE_ARGS),
        ("def f(m):\n    m.wait_and_merge('1', strategy='merge')\n", _WAIT_AND_MERGE),
        ("from x import wait_and_merge\ndef f():\n    wait_and_merge('1')\n", _WAIT_AND_MERGE),
    ],
)
def test_the_scan_catches_each_kind(source: str, kind: str) -> None:
    hits = _scan_source("lib/new_tool.py", source)
    assert [h.kind for h in hits] == [kind]
    assert hits[0].allowed_by() is None


def test_the_scan_ignores_a_docstring_and_reports_scope() -> None:
    source = (
        '"""Arms with enablePullRequestAutoMerge, in prose only."""\n'
        "def merge_release(m):\n"
        '    """Calls github.merge? No: wait_and_merge."""\n'
        "    m.wait_and_merge('1')\n"
    )
    hits = _scan_source("lib/release/orchestrator.py", source)
    assert [(h.scope, h.kind, h.line) for h in hits] == [("merge_release", _WAIT_AND_MERGE, 4)]
    assert hits[0].allowed_by() is not None


# --- each main-only site refuses a PR into develop ---------------------------

_PR = "https://github.com/o/r/pull/5"


@pytest.fixture
def develop_pr() -> Iterator[MagicMock]:
    """A human-run release whose PR's base is develop; ``github.merge`` is watched."""
    from vergil_tooling.lib.identity_mode import IdentityMode

    with (
        patch("vergil_tooling.lib.github.base_ref", return_value="develop"),
        patch("vergil_tooling.lib.github.pr_state", return_value="OPEN"),
        patch(
            "vergil_tooling.lib.release.merge.identity_mode.current_mode",
            return_value=IdentityMode.HUMAN,
        ),
        patch(
            "vergil_tooling.lib.release.merge.identity_mode.any_agent_signal", return_value=False
        ),
        patch("vergil_tooling.lib.github.wait_for_checks") as wait,
        patch("vergil_tooling.lib.github.merge") as merge,
    ):
        yield merge
    wait.assert_not_called()


def test_pr_merge_main_only_sites_assert_main_base(develop_pr: MagicMock) -> None:
    with pytest.raises(pr_merge.MergeAbortError, match="its base is 'develop', not 'main'"):
        pr_merge.wait_and_merge(_PR, strategy="merge")
    with pytest.raises(ReleaseError, match="its base is 'develop', not 'main'"):
        release_merge.wait_and_merge(_PR, phase="merge-release")
    ctx = ReleaseContext(
        repo="o/r",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.release_pr_url = _PR
    with pytest.raises(ReleaseError, match="its base is 'develop', not 'main'"):
        merge_release(ctx)
    assert ctx.release_merge_sha is None
    develop_pr.assert_not_called()
