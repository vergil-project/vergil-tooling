"""The guard: no GitHub content read outside the trust layer (spec §6.3).

Epic vergil-project/.github#1467. A behaviour-driving read of GitHub content
(a body, comments, reviews, edit history, event actors, the closing references
GitHub derives from bodies, or a PR found or chosen by its head branch) goes
through :mod:`vergil_tooling.lib.trust`. This module scans
``src/vergil_tooling/`` with :mod:`ast` and fails on any such read anywhere
else, unless ``trust_guard_exemptions.toml`` lists it with a reason.

**What is scanned.** Everything but ``lib/trust.py``, ``bin/vrg_gh.py`` (the
agents' passthrough wrapper: its arguments are the agent's own, and the hook
covers them) and the bodies of the low-level ``gh`` runners of
``lib/github.py`` (:data:`RUNNERS`). The rest of ``lib/github.py`` is scanned
like any module.

**Two channels.** A call to one of the runners, and a ``gh`` or ``vrg-gh``
command written out as a list or tuple. Of these, a read is ``issue view``,
``issue list``, ``pr view``, ``pr list``, ``search …``, ``api`` with no write
method, or a GraphQL query (not a mutation). A function of ``lib/github.py``
that holds a content read is a reader too (:func:`pr_closing_references`,
:func:`head_ref`, :func:`open_prs_for_head`): a call to it, anywhere, is a
read of the same fields.

**Resolution.** The scanner resolves each argument from literals, f-strings,
``+``, ``",".join(...)``, module constants (its own and those it imports from
another module of the package), every assignment to a local name in the same
function (so a conditional ``cmd += (...)`` counts) and module functions that
only ``return`` an expression. A variable inside a value (``repo``, a number)
is a hole. A read whose ``--json`` fields, ``--jq`` filter, API path or
GraphQL query cannot be resolved is *unresolved*.

**The exemption file** has three tables, each entry with a reason:

- ``[[registered]]``: an unresolved read, named by its file, function and the
  unresolved argument's source text, with the fields it requests. The
  declared fields are then checked like resolved ones. This is the call-site
  registry spec §6.3 allows, for the reads constant resolution cannot see.
- ``[[judged]]``: a read whose content fields only feed the trust layer's own
  judgement, named by its file and function, with the fields and the layer
  function (``judge``) that its module calls on them.
- ``[[exempt]]``: a display-only read (its fields are shown to a human and
  drive no decision), named by its file and function, with its fields.
"""

from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

SRC = Path(__file__).resolve().parents[2] / "src" / "vergil_tooling"
EXEMPTIONS = Path(__file__).resolve().parent / "trust_guard_exemptions.toml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "trust_guard"

GITHUB_MODULE = "lib/github.py"
EXCLUDED = frozenset({"lib/trust.py", "bin/vrg_gh.py"})
PACKAGE = "vergil_tooling"

# The low-level ``gh`` runners of lib/github.py. Their bodies are not scanned
# (they run whatever argv they are given); every call to one is.
RUNNERS = frozenset(
    {
        "graphql",
        "read_json",
        "read_json_paginated",
        "read_output",
        "run",
        "run_tolerating",
        "_read_output_once",
        "_run_with_retry",
    }
)
# The runners that take the whole command, ``gh`` included, as one sequence.
_ARGV_RUNNERS = frozenset({"run_tolerating", "_run_with_retry"})

# The fields that carry content a trust decision is needed for: text an
# author or editor wrote, its history, who acted on an item, the references
# GitHub derives from bodies, and a PR's head branch name (its author's
# choice), whether read or used to find the PR.
CONTENT_FIELDS = frozenset(
    {
        "body",
        "bodyText",
        "bodyHTML",
        "comments",
        "reviews",
        "latestReviews",
        "reviewThreads",
        "userContentEdits",
        "timelineItems",
        "closingIssuesReferences",
        "closedByPullRequestsReferences",
        "headRefName",
        "--comments",
        "--head",
    }
)
# REST names of the same content, as a ``--jq`` filter selects them.
_REST_CONTENT_FIELDS = {"body": "body", "head": "headRefName"}

# A REST path whose answer is content: an issue or PR itself, a list of them,
# a search for them, their comments, reviews, timeline or events, and the PRs
# of a commit.
_CONTENT_PATHS = (
    re.compile(r"(^|/)(comments|reviews|timeline|events)(\?|$)"),
    re.compile(r"(^|/)(issues|pulls)(/[^/?]+)?(\?|$)"),
    re.compile(r"(^|/)search/issues"),
    re.compile(r"/commits/[^/]+/pulls(\?|$)"),
)
_GRAPHQL_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_READ_COMMANDS = {"issue": {"view", "list"}, "pr": {"view", "list"}}
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_API_VALUE_FLAGS = frozenset(
    {
        "-X",
        "--method",
        "--jq",
        "-q",
        "-H",
        "--header",
        "-f",
        "-F",
        "--field",
        "--raw-field",
        "--input",
        "--hostname",
        "--cache",
        "-t",
        "--template",
    }
)
_FIELD_FLAGS = frozenset({"-f", "-F", "--field", "--raw-field", "--input"})

# Methods that change a list or dict in place, so its assignments no longer
# tell its value.
_MUTATORS = frozenset({"append", "extend", "insert", "update", "setdefault", "__iadd__"})

HOLE = "\x00"
UNRESOLVED = "?"
_MAX_DEPTH = 10


class _Unknown:
    """An argument the scanner cannot resolve, with its source text."""

    def __init__(self, source: str) -> None:
        self.source = source

    @property
    def spread(self) -> bool:
        return self.source.startswith("*")


# One argv element: a string (HOLE marks a variable part) or an _Unknown.
Atom = str | _Unknown


@dataclass(frozen=True)
class Read:
    """One GitHub read the scanner found."""

    path: str
    function: str
    line: int
    fields: frozenset[str]
    unresolved: tuple[str, ...] = ()

    @property
    def where(self) -> str:
        return f"{self.path}:{self.line} ({self.function})"


@dataclass
class _Module:
    path: str
    name: str
    tree: ast.Module
    consts: dict[str, object] = field(default_factory=dict)
    returns: dict[str, ast.FunctionDef] = field(default_factory=dict)
    # A name bound by ``from X import Y [as Z]``: Z -> (X, Y).
    from_imports: dict[str, tuple[str, str]] = field(default_factory=dict)
    # A name bound to a module of the package: alias -> dotted module name.
    module_aliases: dict[str, str] = field(default_factory=dict)
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)


@dataclass
class _Package:
    modules: dict[str, _Module]  # by dotted name

    def const(self, module: str, name: str, depth: int) -> object:
        target = self.modules.get(module)
        if target is None or depth > _MAX_DEPTH:
            return None
        if name in target.consts:
            return target.consts[name]
        if name in target.from_imports:
            return self.const(*target.from_imports[name], depth + 1)
        return None


# --- resolution ------------------------------------------------------------------


class _Scope:
    """Resolves expressions in one function (or at the top) of one module."""

    def __init__(
        self,
        package: _Package,
        module: _Module,
        function: ast.AST | None,
        bound: Mapping[str, object] | None = None,
    ) -> None:
        self.package = package
        self.module = module
        self.locals: dict[str, list[ast.AST]] = {}
        # Names whose value the scanner cannot follow: parameters (unless
        # *bound* gives them a value), loop and ``with`` targets, and names
        # changed in place (``x.append(...)``).
        self.opaque: set[str] = set()
        self.bound: dict[str, object] = dict(bound or {})
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            args = function.args
            params = [*args.posonlyargs, *args.args, *args.kwonlyargs]
            params += [a for a in (args.vararg, args.kwarg) if a is not None]
            self.opaque |= {p.arg for p in params} - set(self.bound)
            for node in _own_nodes(function):
                self._record(node)

    def _record(self, node: ast.AST) -> None:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.locals.setdefault(target.id, []).append(node.value)
                else:
                    self.opaque |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                self.locals.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.op, ast.Add):
                self.locals.setdefault(node.target.id, []).append(node.value)
            else:
                self.opaque.add(node.target.id)
        elif isinstance(node, ast.For | ast.AsyncFor | ast.comprehension):
            self.opaque |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            self.opaque |= {n.id for n in ast.walk(node.optional_vars) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.NamedExpr):
            self.opaque.add(node.target.id)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.attr in _MUTATORS
        ):
            name, method = node.func.value.id, node.func.attr
            # A list grown in place: what it gains counts like a ``+=``.
            if method == "extend" and len(node.args) == 1:
                self.locals.setdefault(name, []).append(node.args[0])
            elif method == "append" and len(node.args) == 1:
                self.locals.setdefault(name, []).append(ast.List(elts=[node.args[0]]))
            else:
                self.opaque.add(name)

    def _global(self, name: str, depth: int) -> object:
        if name in self.module.consts:
            return self.module.consts[name]
        if name in self.module.from_imports:
            return self.package.const(*self.module.from_imports[name], depth + 1)
        return None

    def string(self, node: ast.AST, depth: int = 0) -> str | None:
        """*node* as a string, HOLE for each variable part, or None."""
        if depth > _MAX_DEPTH:
            return None
        if isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                return node.value
            if isinstance(node.value, int) and not isinstance(node.value, bool):
                return str(node.value)
            return None
        if isinstance(node, ast.JoinedStr):
            parts: list[str] = []
            for part in node.values:
                if isinstance(part, ast.Constant):
                    parts.append(str(part.value))
                elif isinstance(part, ast.FormattedValue):
                    resolved = self.string(part.value, depth + 1)
                    parts.append(HOLE if resolved is None else resolved)
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.string(node.left, depth + 1)
            right = self.string(node.right, depth + 1)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            if node.id in self.bound:
                value = self.bound[node.id]
                return value if isinstance(value, str) else None
            if node.id in self.opaque:
                return None
            if node.id in self.locals:
                values = self.locals[node.id]
                return self.string(values[0], depth + 1) if len(values) == 1 else None
            value = self._global(node.id, depth)
            return value if isinstance(value, str) else None
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = self.module.module_aliases.get(node.value.id)
            if module is not None:
                value = self.package.const(module, node.attr, depth + 1)
                return value if isinstance(value, str) else None
            return None
        if isinstance(node, ast.Call):
            return self._call_string(node, depth)
        return None

    def _call_string(self, node: ast.Call, depth: int) -> str | None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "join"
            and isinstance(func.value, ast.Constant)
            and isinstance(func.value.value, str)
            and len(node.args) == 1
        ):
            atoms = self.sequence(node.args[0], depth + 1)
            if atoms is None or not all(isinstance(a, str) for a in atoms):
                return None
            return func.value.value.join(str(a) for a in atoms)
        if isinstance(func, ast.Name) and func.id in ("str", "int"):
            return HOLE  # a number or a name, never a field
        if isinstance(func, ast.Name) and func.id in self.module.returns:
            target = self.module.returns[func.id]
            ret = target.body[-1]
            if isinstance(ret, ast.Return) and ret.value is not None:
                bound = self.bind(target, node, depth)
                inner = _Scope(self.package, self.module, target, bound)
                return inner.string(ret.value, depth + 1)
        return None

    def bind(self, target: ast.FunctionDef, call: ast.Call, depth: int = 0) -> dict[str, object]:
        """*target*'s parameters bound to *call*'s string arguments, or their defaults.

        A parameter given a value the scanner cannot read as a string is
        bound to HOLE, so it reads as a variable part, never as a field.
        """
        args = target.args
        positional = [*args.posonlyargs, *args.args]
        bound: dict[str, object] = {}
        top = _Scope(self.package, self.module, None)
        for param, default in zip(positional[::-1], args.defaults[::-1], strict=False):
            bound[param.arg] = top.string(default, depth + 1)
        for param, kw_default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
            if kw_default is not None:
                bound[param.arg] = top.string(kw_default, depth + 1)
        for param, value in zip(positional, call.args, strict=False):
            bound[param.arg] = self.string(value, depth + 1)
        for keyword in call.keywords:
            if keyword.arg is not None:
                bound[keyword.arg] = self.string(keyword.value, depth + 1)
        return {name: HOLE if value is None else value for name, value in bound.items()}

    def sequence(self, node: ast.AST, depth: int = 0) -> list[Atom] | None:
        """*node* as a sequence of atoms, or None when it cannot be read as one."""
        if depth > _MAX_DEPTH:
            return None
        if isinstance(node, ast.Tuple | ast.List):
            atoms: list[Atom] = []
            for element in node.elts:
                atoms.extend(self.atoms(element, depth + 1))
            return atoms
        if isinstance(node, ast.ListComp | ast.GeneratorExp):
            # The constants it can emit, and a hole for each variable part.
            consts: list[Atom] = [
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            return [*consts, HOLE]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self.sequence(node.left, depth + 1)
            right = self.sequence(node.right, depth + 1)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.Name):
            if node.id in self.opaque or node.id in self.bound:
                return None
            if node.id in self.locals:
                # Every assignment, in order: a union over the branches.
                result: list[Atom] = []
                for assigned in self.locals[node.id]:
                    part = self.sequence(assigned, depth + 1)
                    if part is None:
                        return None
                    result.extend(part)
                return result
            constant = self._global(node.id, depth)
            if isinstance(constant, tuple):
                return list(constant)
        return None

    def atoms(self, node: ast.AST, depth: int = 0) -> list[Atom]:
        """The argv atoms *node* contributes as one argument."""
        if isinstance(node, ast.Starred):
            spread = self.sequence(node.value, depth + 1)
            if spread is None:
                return [_Unknown("*" + ast.unparse(node.value))]
            return spread
        value = self.string(node, depth)
        if value is not None:
            return [value]
        return [_Unknown(ast.unparse(node))]


def _module_name(rel: str) -> str:
    parts = rel.removesuffix(".py").split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([PACKAGE, *parts])


def _load(path: Path, rel: str) -> _Module:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    module = _Module(rel, _module_name(rel), tree)
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            module.parents[child] = parent
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                bound = alias.asname or alias.name
                if node.module.startswith(PACKAGE):
                    module.from_imports[bound] = (node.module, alias.name)
                    module.module_aliases[bound] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PACKAGE) and alias.asname:
                    module.module_aliases[alias.asname] = alias.name
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and isinstance(stmt.body[-1], ast.Return):
            module.returns[stmt.name] = stmt
    return module


def _resolve_consts(package: _Package) -> None:
    """Every module's string and string-tuple constants, to a fixed point."""
    for _ in range(4):
        changed = False
        for module in package.modules.values():
            scope = _Scope(package, module, None)
            for stmt in module.tree.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                    target, value = stmt.targets[0], stmt.value
                elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
                    target, value = stmt.target, stmt.value
                else:
                    continue
                if not isinstance(target, ast.Name):
                    continue
                resolved: object = scope.string(value)
                if resolved is None:
                    seq = scope.sequence(value)
                    if seq is not None and all(isinstance(a, str) for a in seq):
                        resolved = tuple(seq)
                if resolved is not None and module.consts.get(target.id) != resolved:
                    module.consts[target.id] = resolved
                    changed = True
        if not changed:
            return


# --- classifying one read -------------------------------------------------------


@dataclass(frozen=True)
class _Verdict:
    fields: frozenset[str]
    unresolved: tuple[str, ...] = ()


def _text(atom: Atom) -> str | None:
    return atom if isinstance(atom, str) else None


def _source(atom: Atom) -> str:
    return atom.source if isinstance(atom, _Unknown) else UNRESOLVED


def _json_fields(value: Atom) -> set[str] | None:
    text = _text(value)
    if text is None or HOLE in text:
        return None
    return {name.strip() for name in text.split(",") if name.strip()}


def _jq_fields(value: Atom) -> set[str] | None:
    text = _text(value)
    if text is None or HOLE in text:
        return None
    names = set(re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)", text))
    names |= set(re.findall(r"[{,]\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?=[,}:])", text))
    return names


def _path_is_content(path: str) -> bool:
    bare = path.replace(HOLE, "N")
    return any(pattern.search(bare) for pattern in _CONTENT_PATHS)


def _rest_fields(path: str) -> frozenset[str]:
    """The content fields a REST *path*'s whole answer carries."""
    bare = path.split("?", 1)[0].rstrip("/")
    last = bare.rsplit("/", 1)[-1]
    if last in ("comments", "reviews"):
        return frozenset({last})
    if last in ("timeline", "events"):
        return frozenset({"timelineItems"})
    if re.search(r"(^|/)pulls(/[^/]+)?$", bare):
        return frozenset({"body", "headRefName"})  # a PR object carries head.ref
    return frozenset({"body"})


def classify_cli(argv: Sequence[Atom]) -> _Verdict | None:
    """The verdict on a ``gh`` argv (``gh`` itself left off), or None for no read."""
    if not argv:
        return None
    head = argv[0]
    if isinstance(head, _Unknown):
        return _Verdict(frozenset(), (head.source,))
    if head == "api":
        return _classify_api(argv[1:])
    if head == "search":
        return _classify_listing(argv[2:])
    if head in _READ_COMMANDS:
        if len(argv) < 2:  # noqa: PLR2004
            return None
        verb = argv[1]
        if isinstance(verb, _Unknown):
            return _Verdict(frozenset(), (verb.source,))
        return _classify_listing(argv[2:]) if verb in _READ_COMMANDS[head] else None
    return None


def _classify_listing(rest: Sequence[Atom]) -> _Verdict:
    fields: set[str] = set()
    unresolved: list[str] = []
    saw_json = False
    for index, atom in enumerate(rest):
        if isinstance(atom, _Unknown):
            # A plain variable is a value; only a spread could hide a flag.
            if atom.spread:
                unresolved.append(atom.source)
            continue
        if atom in ("--comments", "--head"):
            fields.add(atom)
        if atom == "--json" and index + 1 < len(rest):
            saw_json = True
            names = _json_fields(rest[index + 1])
            if names is None:
                unresolved.append(_source(rest[index + 1]))
            else:
                fields |= names & CONTENT_FIELDS
    if not saw_json and not unresolved:
        fields.add("body")  # without --json, gh prints the item's text
    return _Verdict(frozenset(fields), tuple(unresolved))


def _classify_api(rest: Sequence[Atom]) -> _Verdict | None:
    path: Atom | None = None
    jq: Atom | None = None
    method: str | None = None
    has_fields = False
    index = 0
    while index < len(rest):
        atom = rest[index]
        if isinstance(atom, _Unknown) and atom.spread:
            return _Verdict(frozenset(), (atom.source,))
        if atom in _API_VALUE_FLAGS and index + 1 < len(rest):
            value = rest[index + 1]
            if atom in ("-X", "--method"):
                method = _text(value) or UNRESOLVED
            elif atom in ("--jq", "-q"):
                jq = value
            elif atom in _FIELD_FLAGS:
                has_fields = True
            index += 2
            continue
        if isinstance(atom, str) and atom.startswith("-"):
            index += 1
            continue
        if path is None:
            path = atom
        index += 1
    if method is not None and method.upper() in _WRITE_METHODS:
        return None
    if has_fields and (method is None or method == UNRESOLVED):
        # gh api with fields and no method is a POST; a body sent with a
        # method the caller chooses (write_json) is a write too.
        return None
    if path is None:
        return _Verdict(frozenset(), (UNRESOLVED,))
    if path == "graphql":
        return None  # GraphQL goes through graphql(), checked there
    if isinstance(path, _Unknown) or path.startswith(HOLE):
        return _Verdict(frozenset(), (_source(path),))
    if not _path_is_content(path):
        return _Verdict(frozenset())
    if jq is not None:
        names = _jq_fields(jq)
        if names is None:
            return _Verdict(frozenset(), (_source(jq),))
        return _Verdict(
            frozenset(_REST_CONTENT_FIELDS[n] for n in names if n in _REST_CONTENT_FIELDS)
        )
    return _Verdict(_rest_fields(path))


def classify_graphql(query: Atom) -> _Verdict | None:
    if isinstance(query, _Unknown) or HOLE in query:
        return _Verdict(frozenset(), (_source(query),))
    if query.lstrip().startswith("mutation"):
        return None
    return _Verdict(frozenset(_GRAPHQL_TOKEN.findall(query)) & CONTENT_FIELDS)


# --- walking the tree ----------------------------------------------------------


def _functions(tree: ast.Module) -> Iterator[tuple[str, ast.AST]]:
    """Every function with its qualified name, and the module's top as ``<module>``."""
    yield "<module>", tree

    def walk(node: ast.AST, prefix: str) -> Iterator[tuple[str, ast.AST]]:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                yield name, child
                yield from walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")

    yield from walk(tree, "")


def _own_nodes(function: ast.AST) -> Iterator[ast.AST]:
    """The nodes of *function*'s own body in source order, not of a nested function or class."""
    for node in ast.iter_child_nodes(function):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef):
            continue
        yield node
        yield from _own_nodes(node)


def _callee(call: ast.Call, module: _Module) -> tuple[str, str] | None:
    """``("runner", name)``, or ``("github", name)`` for another lib/github.py function."""
    func = call.func
    github = f"{PACKAGE}.lib.github"
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if module.module_aliases.get(func.value.id) == github:
            return ("runner" if func.attr in RUNNERS else "github"), func.attr
        return None
    if isinstance(func, ast.Name):
        if module.path == GITHUB_MODULE:
            return ("runner" if func.id in RUNNERS else "github"), func.id
        origin = module.from_imports.get(func.id)
        if origin is not None and origin[0] == github:
            return ("runner" if origin[1] in RUNNERS else "github"), origin[1]
    return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _single(atoms: list[Atom], node: ast.AST) -> Atom:
    return atoms[0] if len(atoms) == 1 else _Unknown(ast.unparse(node))


def _named(verdict: _Verdict | None, source: str) -> _Verdict | None:
    """*verdict* with an unresolved string named by its argument's *source*."""
    if verdict is None:
        return None
    names = tuple(source if u == UNRESOLVED else u for u in verdict.unresolved)
    return _Verdict(verdict.fields, names)


def _runner_verdict(name: str, call: ast.Call, scope: _Scope) -> _Verdict | None:
    if not call.args:
        return _Verdict(frozenset(), (UNRESOLVED,))
    first = call.args[0]
    if name == "graphql":
        verdict = classify_graphql(_single(scope.atoms(first), first))
        return _named(verdict, ast.unparse(first))
    if name == "read_json_paginated":
        args: list[Atom] = [_single(scope.atoms(first), first)]
        jq = _keyword(call, "jq")
        if jq is not None:
            args += ["--jq", _single(scope.atoms(jq), jq)]
        return _named(_classify_api(args), ast.unparse(first))
    if name in _ARGV_RUNNERS:
        argv = scope.sequence(first)
        if argv is None:
            return _Verdict(frozenset(), (ast.unparse(first),))
        return classify_cli(argv[1:]) if argv and argv[0] in ("gh", "vrg-gh") else None
    atoms: list[Atom] = []
    for arg in call.args:
        atoms.extend(scope.atoms(arg))
    return classify_cli(atoms)


def _is_command(seq: ast.AST, module: _Module) -> bool:
    """False for a ``gh`` argv that is only data: an error's record, or a comparison."""
    parent = module.parents.get(seq)
    if isinstance(parent, ast.Compare):
        return False
    if isinstance(parent, ast.Call) and seq in parent.args:
        func = parent.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name.endswith(("Error", "Exception", "_error")):
            return False
    if module.path == GITHUB_MODULE:
        # ("gh", *args): a runner passing on its caller's argv.
        elts = seq.elts if isinstance(seq, ast.Tuple | ast.List) else []
        if len(elts) == 2 and isinstance(elts[1], ast.Starred):  # noqa: PLR2004
            target = elts[1].value
            return not (isinstance(target, ast.Name) and target.id == "args")
    return True


@dataclass
class _Found:
    function: str
    line: int
    verdict: _Verdict


def _node_verdict(
    node: ast.AST, module: _Module, scope: _Scope, readers: Mapping[str, frozenset[str]]
) -> _Verdict | None:
    """The verdict on *node* when it is a GitHub read, or None."""
    if isinstance(node, ast.Call):
        callee = _callee(node, module)
        if callee is None:
            return None
        kind, name = callee
        if kind == "runner":
            return _runner_verdict(name, node, scope)
        return _Verdict(readers[name]) if name in readers else None
    if isinstance(node, ast.Tuple | ast.List) and node.elts:
        if scope.string(node.elts[0]) not in ("gh", "vrg-gh") or not _is_command(node, module):
            return None
        argv = scope.sequence(node)
        return classify_cli(argv[1:]) if argv is not None else None
    return None


def _traced_helpers(
    package: _Package, module: _Module, readers: Mapping[str, frozenset[str]]
) -> dict[str, ast.FunctionDef]:
    """The module's private helpers whose reads take a field list from their caller.

    A helper such as ``_view(ref, fields)`` passes a parameter as its
    ``--json`` fields or its path. Its reads are judged at each call, with
    the parameter bound to the caller's argument, instead of being
    unresolved. A helper that is also used other than by a direct call by
    name in the same module keeps its reads unresolved.
    """
    called: dict[str, int] = {}
    named: dict[str, int] = {}
    for node in ast.walk(module.tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called[node.func.id] = called.get(node.func.id, 0) + 1
        if isinstance(node, ast.Name):
            named[node.id] = named.get(node.id, 0) + 1
    helpers: dict[str, ast.FunctionDef] = {}
    for stmt in module.tree.body:
        if not isinstance(stmt, ast.FunctionDef) or not stmt.name.startswith("_"):
            continue
        if not called.get(stmt.name) or named.get(stmt.name) != called.get(stmt.name):
            continue
        params = {a.arg for a in (*stmt.args.posonlyargs, *stmt.args.args, *stmt.args.kwonlyargs)}
        scope = _Scope(package, module, stmt)
        for node in _own_nodes(stmt):
            verdict = _node_verdict(node, module, scope, readers)
            if verdict is not None and verdict.unresolved and set(verdict.unresolved) <= params:
                helpers[stmt.name] = stmt
                break
    return helpers


def _scan_module(
    package: _Package, module: _Module, readers: Mapping[str, frozenset[str]]
) -> list[_Found]:
    found: list[_Found] = []
    helpers = _traced_helpers(package, module, readers)
    for qualname, function in _functions(module.tree):
        if module.path == GITHUB_MODULE and qualname in RUNNERS:
            continue
        scope = _Scope(package, module, function)
        for node in _own_nodes(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in helpers
            ):
                helper = helpers[node.func.id]
                inner = _Scope(package, module, helper, scope.bind(helper, node))
                for inner_node in _own_nodes(helper):
                    verdict = _node_verdict(inner_node, module, inner, readers)
                    if verdict is not None:
                        found.append(_Found(qualname, node.lineno, verdict))
                continue
            if qualname in helpers:
                continue  # judged at each call instead
            verdict = _node_verdict(node, module, scope, readers)
            if verdict is not None:
                found.append(_Found(qualname, getattr(node, "lineno", 0), verdict))
    return found


def _github_readers(package: _Package, module: _Module) -> dict[str, frozenset[str]]:
    """lib/github.py's functions that read content, with their fields, transitively."""
    readers: dict[str, frozenset[str]] = {}
    while True:
        updated = dict(readers)
        for item in _scan_module(package, module, readers):
            if item.function != "<module>" and "." not in item.function and item.verdict.fields:
                updated[item.function] = (
                    updated.get(item.function, frozenset()) | item.verdict.fields
                )
        if updated == readers:
            return readers
        readers = updated


def scan(root: Path) -> list[Read]:
    """Every GitHub read in the tree *root*, laid out like ``src/vergil_tooling``.

    The package's own modules are always loaded, so a tree of fixtures
    resolves the package's constants and lib/github.py's readers.
    """
    package = _Package({})
    for path in sorted(SRC.rglob("*.py")):
        module = _load(path, path.relative_to(SRC).as_posix())
        package.modules[module.name] = module
    scanned: list[_Module] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in EXCLUDED:
            continue
        module = _load(path, rel)
        if root != SRC:
            module.name = f"{module.name}.__fixture__"
        package.modules[module.name] = module
        scanned.append(module)
    _resolve_consts(package)
    readers = _github_readers(package, package.modules[_module_name(GITHUB_MODULE)])
    return [
        Read(module.path, item.function, item.line, item.verdict.fields, item.verdict.unresolved)
        for module in scanned
        for item in _scan_module(package, module, readers)
        # A reader of lib/github.py is judged at each of its calls instead.
        if not (
            module.path == GITHUB_MODULE
            and item.function in readers
            and not item.verdict.unresolved
        )
    ]


# --- the exemption file --------------------------------------------------------


@dataclass(frozen=True)
class Registered:
    path: str
    function: str
    argument: str
    fields: frozenset[str]
    reason: str


@dataclass(frozen=True)
class Judged:
    path: str
    function: str
    fields: frozenset[str]
    judge: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Exempt:
    path: str
    function: str
    fields: frozenset[str]
    reason: str


@dataclass(frozen=True)
class Exemptions:
    registered: tuple[Registered, ...]
    judged: tuple[Judged, ...]
    exempt: tuple[Exempt, ...]


def load_exemptions(path: Path = EXEMPTIONS) -> Exemptions:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Exemptions(
        registered=tuple(
            Registered(e["path"], e["function"], e["argument"], frozenset(e["fields"]), e["reason"])
            for e in data.get("registered", [])
        ),
        judged=tuple(
            Judged(e["path"], e["function"], frozenset(e["fields"]), tuple(e["judge"]), e["reason"])
            for e in data.get("judged", [])
        ),
        exempt=tuple(
            Exempt(e["path"], e["function"], frozenset(e["fields"]), e["reason"])
            for e in data.get("exempt", [])
        ),
    )


@dataclass
class Findings:
    failures: list[str] = field(default_factory=list)
    used: set[object] = field(default_factory=set)


def check(reads: Sequence[Read], exemptions: Exemptions) -> Findings:
    """Every content read outside the layer that the exemption file does not cover."""
    findings = Findings()
    for read in reads:
        fields = set(read.fields)
        for argument in read.unresolved:
            match = next(
                (
                    r
                    for r in exemptions.registered
                    if (r.path, r.function, r.argument) == (read.path, read.function, argument)
                ),
                None,
            )
            if match is None:
                findings.failures.append(
                    f"{read.where}: cannot resolve {argument!r}; register the call "
                    "with the fields it requests"
                )
                continue
            findings.used.add(match)
            fields |= match.fields & CONTENT_FIELDS
        if not fields:
            continue
        key = (read.path, read.function)
        entries: tuple[Judged | Exempt, ...] = (*exemptions.judged, *exemptions.exempt)
        covering = [e for e in entries if (e.path, e.function) == key]
        allowed: set[str] = set()
        for entry in covering:
            allowed |= entry.fields
        missing = fields - allowed
        if missing:
            findings.failures.append(
                f"{read.where}: reads {', '.join(sorted(missing))} outside the trust layer"
            )
        else:
            findings.used.update(covering)
    return findings


def reaches_trust(judge: str) -> bool:
    """True when *judge* (``path:function``) is a trust-layer function or calls one.

    A function of ``lib/trust.py`` is the layer itself. Any other must call a
    ``trust.<name>`` function, directly or through other functions of its own
    module.
    """
    rel, _, name = judge.partition(":")
    path = SRC / rel
    if not path.is_file() or not name:
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    defs = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    if name not in defs:
        return False
    if rel == "lib/trust.py":
        return not name.startswith("_")
    seen: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for node in ast.walk(defs[current]):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "trust"
            ):
                return True
            if isinstance(func, ast.Name) and func.id in defs:
                pending.append(func.id)
    return False


# --- the tests -----------------------------------------------------------------


def test_no_content_read_outside_trust_layer() -> None:
    exemptions = load_exemptions()
    findings = check(scan(SRC), exemptions)
    assert not findings.failures, "\n".join(findings.failures)
    entries: tuple[Registered | Judged | Exempt, ...] = (
        *exemptions.registered,
        *exemptions.judged,
        *exemptions.exempt,
    )
    stale = [
        f"{type(entry).__name__.lower()} {entry.path} {entry.function}"
        for entry in entries
        if entry not in findings.used
    ]
    assert not stale, "entries that match no read: " + "; ".join(stale)


def test_exemptions_are_display_only() -> None:
    """Each exemption names the content fields it shows, and why they drive nothing."""
    exemptions = load_exemptions()
    assert exemptions.exempt
    for entry in exemptions.exempt:
        assert entry.fields, f"{entry.path} {entry.function}: no fields"
        assert entry.fields <= CONTENT_FIELDS, (
            f"{entry.path} {entry.function}: {sorted(entry.fields - CONTENT_FIELDS)} "
            "are not content fields"
        )
        assert entry.reason.strip(), f"{entry.path} {entry.function}: no reason"


def test_judged_reads_reach_the_trust_layer() -> None:
    """Each judged read names layer functions that exist and call into lib/trust.py."""
    for entry in load_exemptions().judged:
        assert entry.fields <= CONTENT_FIELDS, f"{entry.path} {entry.function}"
        assert entry.reason.strip(), f"{entry.path} {entry.function}: no reason"
        assert entry.judge, f"{entry.path} {entry.function}: no judge"
        for judge in entry.judge:
            assert reaches_trust(judge), f"{entry.path} {entry.function}: {judge} is no judge"


_PLANTED = '''
from __future__ import annotations

import subprocess

from vergil_tooling.lib import github
from vergil_tooling.lib.github import read_json as rj

_BODY_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { title body } }
}
"""
_LABELS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { issue(number: $number) { labels { nodes { name } } } }
}
"""
_FIELDS = ("number", "state")


def direct_gh(number: str) -> str:
    return subprocess.run(
        ["gh", "issue", "view", number, "--json", "title,body"],
        check=True, capture_output=True, text=True,
    ).stdout


def direct_vrg_gh_comments(number: str) -> None:
    subprocess.run(["vrg-gh", "pr", "view", number, "--comments"], check=True)


def graphql_body(owner: str, name: str) -> object:
    return github.graphql(_BODY_QUERY, owner=owner, name=name, number=1)


def paginated_comments(repo: str, number: int) -> list[object]:
    return github.read_json_paginated(f"repos/{repo}/issues/{number}/comments")


def closing_refs(pr: str) -> object:
    return github.pr_closing_references(pr)


def branch_of(pr: str) -> str:
    return github.head_ref(pr)


def aliased_pr_list(repo: str, branch: str) -> object:
    return rj("pr", "list", "--repo", repo, "--head", branch, "--json", ",".join(_FIELDS))


def _view(number: int, fields: str) -> object:
    return github.read_json("issue", "view", str(number), "--json", fields)


def traced_body(number: int) -> object:
    return _view(number, "title,body")


def traced_labels(number: int) -> object:
    return _view(number, "labels")


def unresolved(query: str) -> object:
    return github.graphql(query)


def clean_labels(owner: str, name: str) -> object:
    return github.graphql(_LABELS_QUERY, owner=owner, name=name, number=1)


def clean_state(repo: str, number: int) -> str:
    return github.read_output("api", f"repos/{repo}/issues/{number}", "--jq", ".state")


def write_only(repo: str, number: int) -> None:
    github.run("issue", "edit", str(number), "--repo", repo, "--add-label", "x")
'''


def test_guard_catches_planted_reads(tmp_path: Path) -> None:
    """Each kind of content read is caught; a non-content read and a write are not."""
    planted = tmp_path / "lib" / "planted.py"
    planted.parent.mkdir()
    planted.write_text(_PLANTED, encoding="utf-8")
    reads = {read.function: read for read in scan(tmp_path)}
    expected = {
        "direct_gh": {"body"},
        "direct_vrg_gh_comments": {"--comments", "body"},
        "graphql_body": {"body"},
        "paginated_comments": {"comments"},
        "closing_refs": {"closingIssuesReferences"},
        "branch_of": {"headRefName"},
        "aliased_pr_list": {"--head"},
        "traced_body": {"body"},
    }
    for function, fields in expected.items():
        assert function in reads, function
        assert reads[function].fields == fields, function
        assert reads[function].unresolved == (), function
    assert reads["unresolved"].unresolved == ("query",)
    assert reads["unresolved"].fields == frozenset()
    for clean in ("traced_labels", "clean_labels", "clean_state"):
        assert reads[clean].fields == frozenset(), clean
        assert reads[clean].unresolved == (), clean
    assert "write_only" not in reads
    assert "_view" not in reads  # judged at each call instead

    findings = check(list(reads.values()), Exemptions((), (), ()))
    failed = {line.split(" (", 1)[1].split(")", 1)[0] for line in findings.failures}
    assert failed == {*expected, "unresolved"}


def test_guard_honours_entries(tmp_path: Path) -> None:
    """A registered unresolved read is checked by its declared fields; exempt covers it."""
    planted = tmp_path / "lib" / "planted.py"
    planted.parent.mkdir()
    planted.write_text(_PLANTED, encoding="utf-8")
    reads = [r for r in scan(tmp_path) if r.function == "unresolved"]
    registered = Registered("lib/planted.py", "unresolved", "query", frozenset({"body"}), "r")
    only_registered = check(reads, Exemptions((registered,), (), ()))
    assert only_registered.failures == [
        f"lib/planted.py:{reads[0].line} (unresolved): reads body outside the trust layer"
    ]
    exempt = Exempt("lib/planted.py", "unresolved", frozenset({"body"}), "shown only")
    covered = check(reads, Exemptions((registered,), (), (exempt,)))
    assert covered.failures == []
    assert covered.used == {registered, exempt}


def test_reaches_trust() -> None:
    assert reaches_trust("lib/trust.py:read_issue")
    assert not reaches_trust("lib/trust.py:_graphql")
    assert reaches_trust("lib/epics.py:_trusted_body")
    assert not reaches_trust("lib/github.py:head_ref")
    assert not reaches_trust("lib/no_such_module.py:f")


def test_registered_reads_have_reasons() -> None:
    for entry in load_exemptions().registered:
        assert entry.reason.strip(), f"{entry.path} {entry.function}: no reason"
        assert entry.argument, f"{entry.path} {entry.function}: no argument"
