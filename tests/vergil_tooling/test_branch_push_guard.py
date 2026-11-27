"""Every tooling push to a branch is at an allowed site (epic vergil-project/.github#1664).

Spec §5.4: every push the tooling makes to the branch of an open PR into
``develop`` goes through the one PR path. ``vrg-git`` refuses such a push
made through the wrapper (plan Task 14); this guard covers the tooling's own
pushes, which go through ``lib.git`` directly (plan Task 14a).

It scans the whole of ``src/vergil_tooling/`` for ``lib.git`` push calls
(``git.run("push", …)``, ``git.read_output("push", …)`` and the module-local
helpers that forward their arguments to them) and classifies each by its
destination. A literal destination that is ``refs/heads/*`` or a bare branch
name is a branch push; a literal tag or other non-branch ref is not. A
destination it cannot determine statically (a refspec built at run time,
such as ``f"{commit}:{ref}"``, a starred argument, or no refspec at all)
counts as a branch push. Every branch push must be at one of
:data:`ALLOWED_SITES`, each named with its reason; any other fails, naming
the file and line.

It also classifies the server-side branch updates, ``github.update_branch``
and ``github.update_branch_rebase``, which make a new head of a PR's branch
on GitHub (plan Task 14b): every use must be at one of
:data:`ALLOWED_UPDATE_SITES`.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

import vergil_tooling

SRC = Path(vergil_tooling.__file__).resolve().parent

#: Where a branch push is allowed, each with its reason. A key is a directory
#: (ending in ``/``), a file, or ``<file>::<function>`` for one function's pushes.
ALLOWED_SITES: dict[str, str] = {
    "lib/pr_path.py": "the one PR path itself",
    "lib/release/": "the release's own branches",
    "lib/mechanized_pr.py": "a branch pushed before its PR is opened, then armed through the path",
    "lib/update_deps/": "a branch pushed before its PR is opened, then armed through the path",
    "bin/vrg_submit_pr.py": "the branch pushed before its PR exists",
    "bin/vrg_pr_workflow.py::_push_branch": "before submit, behind its open-PR refusal",
    "bin/vrg_reword.py::_maybe_push": "behind its open-PR routing through the path",
    "lib/repo_init.py": "the bootstrap pushes of develop, main and gh-pages to a new repo",
    "lib/promote.py": "tags",
    "lib/rulings/draft_ref.py": "a ruling draft ref, never refs/heads/*",
    "lib/pr_workflow/github_transport.py": (
        "relay refs and the one-file-commit builder, never refs/heads/*"
    ),
    "lib/release_lock/": (
        "the release-lock, history and arming refs under refs/vergil/release-lock*,"
        " never refs/heads/*"
    ),
}

#: The ``lib.git`` functions that run a git command.
_GIT_RUNNERS = frozenset({"run", "read_output", "read_output_stdin"})
#: A literal non-branch ref namespace, such as ``refs/tags/`` or ``refs/vergil/``.
_OTHER_NAMESPACE = re.compile(r"refs/(?!heads/)[^/{}]+/")


@dataclass(frozen=True)
class Push:
    """One ``lib.git`` push call: where it is, and whether it pushes to a branch."""

    path: str
    line: int
    function: str
    branch: bool
    why: str


def _git_names(tree: ast.Module) -> tuple[set[str], set[str]]:
    """The names this module binds to ``lib.git``: the module itself, and its runners."""
    modules: set[str] = set()
    runners: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        source = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if alias.name == "git" and (source.endswith("vergil_tooling.lib") or node.level):
                modules.add(bound)
            elif source.endswith("lib.git") and alias.name in _GIT_RUNNERS:
                runners.add(bound)
    return modules, runners


def _is_git_runner(call: ast.Call, modules: set[str], runners: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id in modules and func.attr in _GIT_RUNNERS
    return isinstance(func, ast.Name) and func.id in runners


def _helpers(tree: ast.Module, modules: set[str], runners: set[str]) -> set[str]:
    """Module-level functions that forward a starred argument list to a ``lib.git`` runner."""
    found: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and _is_git_runner(call, modules, runners)
                and any(isinstance(arg, ast.Starred) for arg in call.args)
            ):
                found.add(node.name)
    return found


def _is_option(arg: ast.expr) -> bool:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value.startswith("-")
    if isinstance(arg, ast.JoinedStr) and arg.values:
        first = arg.values[0]
        return isinstance(first, ast.Constant) and str(first.value).startswith("-")
    return False


def _literal_destination(text: str) -> tuple[bool, str]:
    """Classify a refspec's destination known in full or by its literal start."""
    if text.startswith("refs/heads/"):
        return True, f"to {text!r}"
    if _OTHER_NAMESPACE.match(text):
        return False, f"to {text!r}"
    if text.startswith("refs/"):
        return True, f"to {text!r}, a ref whose namespace is not literal"
    return True, f"to the bare branch name {text!r}"


_RUN_TIME = (True, "to a destination built at run time")


def _fstring_destination(parts: list[ast.expr]) -> tuple[bool, str]:
    """Classify an f-string refspec's destination by the literal text it starts with."""
    tail, rest = "", parts
    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]
        if isinstance(part, ast.Constant) and ":" in str(part.value):
            tail, rest = str(part.value).rpartition(":")[2], parts[index + 1 :]
            break
    else:
        if parts and isinstance(parts[0], ast.Constant):
            tail, rest = str(parts[0].value).lstrip("+"), parts[1:]
    if not tail:
        return _RUN_TIME
    if not rest:
        return _literal_destination(tail)
    if tail.startswith("refs/heads/") or _OTHER_NAMESPACE.match(tail):
        return tail.startswith("refs/heads/"), f"to a ref starting {tail!r}"
    if tail.startswith("refs"):
        return _RUN_TIME
    return True, f"to a branch name starting {tail!r}"


def _refspec(arg: ast.expr) -> tuple[bool, str]:
    """Whether a refspec argument pushes to a branch, and why."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return _literal_destination(arg.value.lstrip("+").rpartition(":")[2])
    if isinstance(arg, ast.JoinedStr):
        return _fstring_destination(list(arg.values))
    return _RUN_TIME


def classify(args: list[ast.expr]) -> tuple[bool, str]:
    """Whether a push with *args* (those after ``"push"``) pushes to a branch, and why."""
    if any(isinstance(arg, ast.Starred) for arg in args):
        return True, "a starred argument list, whose destination cannot be told"
    positional = [arg for arg in args if not _is_option(arg)]
    refspecs = positional[1:]
    if not refspecs:
        return True, "no refspec, so the current branch's push target"
    judged = [_refspec(spec) for spec in refspecs]
    branch = [why for is_branch, why in judged if is_branch]
    if branch:
        return True, "; ".join(branch)
    return False, "; ".join(why for _branch, why in judged)


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, modules: set[str], runners: set[str], helpers: set[str]) -> None:
        self.path = path
        self.modules = modules
        self.runners = runners
        self.helpers = helpers
        self.scope: list[str] = []
        self.pushes: list[Push] = []

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_scope  # noqa: N815 - the NodeVisitor protocol
    visit_AsyncFunctionDef = _visit_scope  # noqa: N815
    visit_ClassDef = _visit_scope  # noqa: N815

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - the NodeVisitor protocol
        func = node.func
        helper = isinstance(func, ast.Name) and func.id in self.helpers
        if helper or _is_git_runner(node, self.modules, self.runners):
            for index, arg in enumerate(node.args):
                if isinstance(arg, ast.Constant) and arg.value == "push":
                    branch, why = classify(list(node.args[index + 1 :]))
                    function = ".".join(self.scope)
                    self.pushes.append(Push(self.path, node.lineno, function, branch, why))
                    break
        self.generic_visit(node)


def scan_source(text: str, path: str) -> list[Push]:
    """Every ``lib.git`` push call in one module's source *text*."""
    tree = ast.parse(text, filename=path)
    modules, runners = _git_names(tree)
    scanner = _Scanner(path, modules, runners, _helpers(tree, modules, runners))
    scanner.visit(tree)
    return scanner.pushes


def scan_tree(root: Path = SRC) -> list[Push]:
    """Every ``lib.git`` push call under *root*."""
    pushes: list[Push] = []
    for file in sorted(root.rglob("*.py")):
        pushes.extend(
            scan_source(file.read_text(encoding="utf-8"), file.relative_to(root).as_posix())
        )
    return pushes


def allowed_site(push: Push) -> str | None:
    """The :data:`ALLOWED_SITES` key that allows *push*, or ``None``."""
    for site in ALLOWED_SITES:
        path, _, function = site.partition("::")
        if function:
            if push.path == path and push.function == function:
                return site
        elif push.path == path or (path.endswith("/") and push.path.startswith(path)):
            return site
    return None


def violations(pushes: list[Push]) -> list[str]:
    """One line per branch push outside the allowed sites, naming its file and line."""
    return [
        f"src/vergil_tooling/{push.path}:{push.line}: a branch push ({push.why}) "
        "outside the allowed sites; push to an open PR's branch through "
        "pr_path.push_to_open_pr, or add the site to ALLOWED_SITES with its reason"
        for push in pushes
        if push.branch and allowed_site(push) is None
    ]


# --- the guard ----------------------------------------------------------------------


def test_no_branch_push_outside_allowed_sites() -> None:
    assert violations(scan_tree()) == []


def test_every_allowed_site_still_pushes() -> None:
    # A stale entry would allow a future push nobody reviewed.
    used = {allowed_site(push) for push in scan_tree()}
    assert sorted(set(ALLOWED_SITES) - used) == []


def test_the_scan_finds_the_known_pushes() -> None:
    # The scan is not vacuous: the path's own push is found and is a branch push.
    found = [p for p in scan_tree() if p.path == "lib/pr_path.py"]
    assert [p.function for p in found] == ["push_to_open_pr"]
    assert all(p.branch for p in found)


# --- the classifier -----------------------------------------------------------------

_HEADER = "from vergil_tooling.lib import git\n"


def _one(body: str, header: str = _HEADER) -> Push:
    pushes = scan_source(header + body, "lib/example.py")
    assert len(pushes) == 1, pushes
    return pushes[0]


@pytest.mark.parametrize(
    "call",
    [
        'git.run("push", "origin", "refs/heads/feature/1-x")',
        'git.run("push", "-u", "origin", "develop")',
        'git.run("push", "origin", f"{head}:refs/heads/{branch}")',
        'git.run("push", "--force-with-lease", "origin", branch)',
        'git.run("push", "--force", remote, f"{commit}:{ref}")',
        'git.run("push", remote, f":{ref}")',
        'git.run("push", "-u", *lease_args, "origin", f"{ref}:{ref}")',
        'git.run("push")',
        'git.run("-C", str(clone), "push", "origin", f"release/{version}")',
        'git.read_output("push", "origin", f"refs/{kind}/x")',
        'git.read_output("push", "origin", "refs/tags/v1", branch)',
    ],
)
def test_branch_or_undetermined_destination_is_a_branch_push(call: str) -> None:
    assert _one(f"def f():\n    {call}\n").branch


@pytest.mark.parametrize(
    "call",
    [
        'git.run("push", "origin", "refs/tags/v1.2.3")',
        'git.run("push", "origin", f"{o}:refs/tags/{t}", f"--force-with-lease=refs/tags/{t}:{x}")',
        'git.run("push", "--force", "origin", f"{commit}:refs/vergil/pr-workflow/{branch}")',
        'git.run("push", "origin", "+abc123:refs/vergil/release-lock")',
    ],
)
def test_literal_non_branch_ref_is_not_a_branch_push(call: str) -> None:
    assert not _one(f"def f():\n    {call}\n").branch


def test_a_forwarding_helper_is_scanned() -> None:
    source = (
        "def _git(phase, *args):\n"
        "    return git.read_output(*args)\n"
        "def remove(branch):\n"
        '    _git("cleanup", "push", "origin", "--delete", branch)\n'
    )
    push = _one(source)
    assert (push.function, push.branch) == ("remove", True)


def test_a_runner_imported_by_name_is_scanned() -> None:
    push = _one(
        'def f():\n    run("push", "origin", b)\n', "from vergil_tooling.lib.git import run\n"
    )
    assert push.branch


def test_a_push_that_is_not_lib_git_is_ignored() -> None:
    source = 'def f():\n    CopyPlan("push", paths)\n    other.run("push", "origin", b)\n'
    assert scan_source(_HEADER + source, "lib/example.py") == []


def test_a_branch_push_outside_the_allowed_sites_names_file_and_line() -> None:
    pushes = scan_source(_HEADER + 'def f(b):\n    git.run("push", "origin", b)\n', "lib/new.py")
    [line] = violations(pushes)
    assert line.startswith("src/vergil_tooling/lib/new.py:3: a branch push (")


def test_a_function_scoped_site_allows_only_that_function() -> None:
    source = (
        "def _push_branch(b):\n"
        '    git.run("push", "origin", b)\n'
        "def other(b):\n"
        '    git.run("push", "origin", b)\n'
    )
    pushes = scan_source(_HEADER + source, "bin/vrg_pr_workflow.py")
    [line] = violations(pushes)
    assert line.startswith("src/vergil_tooling/bin/vrg_pr_workflow.py:5:")


def test_a_tag_push_outside_the_allowed_sites_is_not_flagged() -> None:
    source = 'def f():\n    git.run("push", "origin", "refs/tags/v1")\n'
    assert violations(scan_source(_HEADER + source, "lib/new.py")) == []


# --- server-side branch updates (plan Task 14b) -------------------------------------
#
# GitHub makes a new head of a PR's branch server-side when asked to update
# it, by merging its base in (``github.update_branch``) or by rebasing it onto
# its base (``github.update_branch_rebase``). ``vrg-gh`` refuses an agent's
# ``pr update-branch`` of a PR into ``develop``; this guard covers the
# tooling's own calls. Every use of either function (a call, or the function
# passed on as a value) must be at one of :data:`ALLOWED_UPDATE_SITES`.

#: Where a server-side branch update is allowed, each with its reason. A key is
#: a file, or ``<file>::<qualified name>`` for one class's or function's uses.
ALLOWED_UPDATE_SITES: dict[str, str] = {
    "lib/github.py": "defines them",
    "lib/pr_path.py": "the one PR path itself",
    "lib/pr_merge.py::_BranchUpdater": (
        "the behind-base updates of wait_until_merged, which waits only on PRs the "
        "tooling opened and armed through the path (mechanized, dependency-update "
        "and back-merge PRs)"
    ),
}

#: Allowed sites that need no use of their own: the module that defines the
#: functions, and the path, which the plan names as an allowed site.
_UPDATE_SITES_WITHOUT_A_USE = frozenset({"lib/github.py", "lib/pr_path.py"})

#: The ``lib.github`` functions that update a PR's branch server-side.
_UPDATERS = frozenset({"update_branch", "update_branch_rebase"})


@dataclass(frozen=True)
class Update:
    """One use of a server-side branch update: where it is, and which function."""

    path: str
    line: int
    function: str
    name: str


def _github_names(tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    """The names this module binds to ``lib.github``: the module, and its updaters by name."""
    modules: set[str] = set()
    updaters: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        source = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if alias.name == "github" and (source.endswith("vergil_tooling.lib") or node.level):
                modules.add(bound)
            elif source.endswith("lib.github") and alias.name in _UPDATERS:
                updaters[bound] = alias.name
    return modules, updaters


class _UpdateScanner(ast.NodeVisitor):
    def __init__(self, path: str, modules: set[str], updaters: dict[str, str]) -> None:
        self.path = path
        self.modules = modules
        self.updaters = updaters
        self.scope: list[str] = []
        self.updates: list[Update] = []

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _visit_scope  # noqa: N815 - the NodeVisitor protocol
    visit_AsyncFunctionDef = _visit_scope  # noqa: N815
    visit_ClassDef = _visit_scope  # noqa: N815

    def _record(self, node: ast.expr, name: str) -> None:
        self.updates.append(Update(self.path, node.lineno, ".".join(self.scope), name))

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802 - the NodeVisitor protocol
        if (
            isinstance(node.value, ast.Name)
            and node.value.id in self.modules
            and node.attr in _UPDATERS
        ):
            self._record(node, node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802 - the NodeVisitor protocol
        if node.id in self.updaters:
            self._record(node, self.updaters[node.id])


def scan_updates_source(text: str, path: str) -> list[Update]:
    """Every use of a server-side branch update in one module's source *text*."""
    tree = ast.parse(text, filename=path)
    modules, updaters = _github_names(tree)
    scanner = _UpdateScanner(path, modules, updaters)
    scanner.visit(tree)
    return scanner.updates


def scan_updates_tree(root: Path = SRC) -> list[Update]:
    """Every use of a server-side branch update under *root*."""
    updates: list[Update] = []
    for file in sorted(root.rglob("*.py")):
        updates.extend(
            scan_updates_source(file.read_text(encoding="utf-8"), file.relative_to(root).as_posix())
        )
    return updates


def allowed_update_site(update: Update) -> str | None:
    """The :data:`ALLOWED_UPDATE_SITES` key that allows *update*, or ``None``."""
    for site in ALLOWED_UPDATE_SITES:
        path, _, scope = site.partition("::")
        if update.path != path:
            continue
        if not scope or update.function == scope or update.function.startswith(f"{scope}."):
            return site
    return None


def update_violations(updates: list[Update]) -> list[str]:
    """One line per server-side branch update outside the allowed sites, naming file and line."""
    return [
        f"src/vergil_tooling/{update.path}:{update.line}: a server-side branch update "
        f"(github.{update.name}) outside the allowed sites; bring the branch up to date "
        "locally and push it through pr_path.push_to_open_pr, or add the site to "
        "ALLOWED_UPDATE_SITES with its reason"
        for update in updates
        if allowed_update_site(update) is None
    ]


def test_no_server_side_branch_update_outside_allowed_sites() -> None:
    assert update_violations(scan_updates_tree()) == []


def test_every_allowed_update_site_is_still_needed() -> None:
    # A stale entry would allow a future update nobody reviewed.
    used = {allowed_update_site(update) for update in scan_updates_tree()}
    stale = set(ALLOWED_UPDATE_SITES) - used - _UPDATE_SITES_WITHOUT_A_USE
    assert sorted(stale) == []


def test_lib_github_still_defines_the_updaters() -> None:
    tree = ast.parse((SRC / "lib/github.py").read_text(encoding="utf-8"))
    defined = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert defined >= _UPDATERS


def test_the_update_scan_finds_the_known_updates() -> None:
    # The scan is not vacuous: _BranchUpdater's two updates are found.
    found = [u for u in scan_updates_tree() if u.path == "lib/pr_merge.py"]
    assert sorted(u.name for u in found) == ["update_branch", "update_branch_rebase"]
    assert {u.function for u in found} == {"_BranchUpdater.update"}


_GITHUB_HEADER = "from vergil_tooling.lib import github\n"


@pytest.mark.parametrize(
    ("header", "body", "name"),
    [
        (_GITHUB_HEADER, "def f(pr):\n    github.update_branch(pr)\n", "update_branch"),
        (
            _GITHUB_HEADER,
            "def f(pr, h):\n    github.update_branch_rebase(pr, expected_head=h)\n",
            "update_branch_rebase",
        ),
        (_GITHUB_HEADER, "def f(pr):\n    run_later(github.update_branch, pr)\n", "update_branch"),
        (
            "from vergil_tooling.lib import github as gh\n",
            "def f(pr):\n    gh.update_branch(pr)\n",
            "update_branch",
        ),
        (
            "from vergil_tooling.lib.github import update_branch_rebase as ubr\n",
            "def f(pr, h):\n    ubr(pr, expected_head=h)\n",
            "update_branch_rebase",
        ),
    ],
)
def test_an_update_outside_the_allowed_sites_names_file_and_line(
    header: str, body: str, name: str
) -> None:
    [line] = update_violations(scan_updates_source(header + body, "lib/new.py"))
    assert line.startswith(
        f"src/vergil_tooling/lib/new.py:3: a server-side branch update (github.{name})"
    )


def test_an_update_scoped_site_allows_only_that_class() -> None:
    source = (
        "class _BranchUpdater:\n"
        "    def update(self):\n"
        "        github.update_branch(self.pr)\n"
        "def other(pr):\n"
        "    github.update_branch(pr)\n"
    )
    [line] = update_violations(scan_updates_source(_GITHUB_HEADER + source, "lib/pr_merge.py"))
    assert line.startswith("src/vergil_tooling/lib/pr_merge.py:6:")


def test_an_unrelated_update_branch_is_ignored() -> None:
    source = "def f(pr):\n    other.update_branch(pr)\n    github.head_sha(pr)\n"
    assert scan_updates_source(_GITHUB_HEADER + source, "lib/new.py") == []
