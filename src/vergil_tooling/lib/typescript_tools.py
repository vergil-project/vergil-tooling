"""The TypeScript pre-gate checks: the repo's tools, version, build script and entry points.

TypeScript gates run the tools the repo declares, from ``node_modules/.bin``
(epic vergil-project/.github#1174, spec §6.3). Before they run,
``vrg-validate`` checks that ``package.json`` declares each required
devDependency, and that both its declared range and its installed version sit
inside the accepted major kept in ``configs/typescript/dev-dependencies.json``,
the one place the majors live. It also checks that the ``package.json``
version matches ``VERSION`` and that ``package.json`` defines the
``build`` script the BUILD check and the release build run (spec §6.5);
that the repo's entry points, ``tsconfig.json`` and
``eslint.config.mjs``, build on the managed bases the fleet sync writes
beside them; and that Prettier has one config source (spec §6.4).

Every check in :data:`PRE_GATE_CHECKS` takes the repo root and returns its
problems, empty when all pass; the pre-gate stage fails with every problem from
all of them at once.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, version
from vergil_tooling.lib.container import PackageJsonError, read_package_json

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_REQUIRED_FILE = "dev-dependencies.json"

_INSTALLED_MAJOR = re.compile(r"(\d+)\.")


def load_required() -> dict[str, int]:
    """Return each required devDependency mapped to its accepted major.

    Read from the packaged ``configs/typescript/dev-dependencies.json``. A
    packaged file that is not a JSON object of package names to positive
    integer majors is a defect in vergil-tooling, so it raises ``ValueError``.
    """
    resource = files("vergil_tooling.configs").joinpath("typescript", _REQUIRED_FILE)
    data = json.loads(resource.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        msg = f"configs/typescript/{_REQUIRED_FILE}: not a non-empty JSON object"
        raise ValueError(msg)
    required: dict[str, int] = {}
    for name, major in data.items():
        if isinstance(major, bool) or not isinstance(major, int) or major < 1:
            msg = (
                f"configs/typescript/{_REQUIRED_FILE}: {name!r} maps to {major!r}, "
                "not a positive integer major"
            )
            raise ValueError(msg)
        required[name] = major
    return required


def _range_in_major(declared: str, major: int) -> bool:
    """Whether *declared* is ``^M…``, ``~M…`` or ``M…`` with ``M`` == *major*.

    ``M…`` takes the X-range forms too (``6.x``, ``6.0.*``), which stay
    inside the major; any other range (``latest``, ``*``, ``>=5``) does not.
    """
    pattern = rf"[\^~]?{major}(\.(\d+|[xX*])){{0,2}}"
    return re.fullmatch(pattern, declared.strip()) is not None


def _installed_problem(repo_root: Path, name: str, major: int) -> str | None:
    """The problem with *name*'s installed copy, or ``None`` when it is in range."""
    manifest = repo_root / "node_modules" / name / "package.json"
    if not manifest.is_file():
        return f"{name}: declared but not installed (node_modules/{name}/package.json is absent)"
    try:
        version = read_package_json(manifest).get("version")
    except PackageJsonError as exc:
        return f"{name}: {exc}"
    if not isinstance(version, str):
        return f"{name}: node_modules/{name}/package.json has no version string"
    found = _INSTALLED_MAJOR.match(version)
    if found is None or int(found.group(1)) != major:
        return f"{name}: installed {version}, outside the accepted major {major}"
    return None


def check_dev_dependencies(repo_root: Path) -> list[str]:
    """Return the problems with the repo's required devDependencies.

    One problem per failing package, then one ``Fix:`` line naming the
    ``npm install --save-dev <name>@^<M> …`` that fixes them all. A package
    fails when ``devDependencies`` does not declare it, when its declared range
    is not ``^M…``, ``~M…`` or ``M…`` with ``M`` its accepted major, when it is
    not installed, or when the installed version has another major. A
    ``package.json`` that cannot be parsed is the one problem returned.
    """
    try:
        manifest = read_package_json(repo_root / "package.json")
    except PackageJsonError as exc:
        return [str(exc)]
    dev_deps = manifest.get("devDependencies")
    declared_deps = dev_deps if isinstance(dev_deps, dict) else {}
    problems: list[str] = []
    failing: list[str] = []
    for name, major in load_required().items():
        declared = declared_deps.get(name)
        if declared is None:
            problem: str | None = f"{name}: not in devDependencies"
        elif not isinstance(declared, str) or not _range_in_major(declared, major):
            problem = f"{name}: declares {json.dumps(declared)}, outside the accepted major {major}"
        else:
            problem = _installed_problem(repo_root, name, major)
        if problem is not None:
            problems.append(problem)
            failing.append(f"{name}@^{major}")
    if failing:
        problems.append(f"Fix: npm install --save-dev {' '.join(failing)}")
    return problems


def check_version_matches(repo_root: Path) -> list[str]:
    """Return the problem with the repo's versions, empty when they agree.

    The same cross-check ``vrg-version show`` makes, so a ``package.json``
    version that is missing or differs from ``VERSION`` fails on the PR, not
    first at ``vrg-release`` preflight (spec §6.5). A missing ``VERSION`` is
    the problem too. A missing ``package.json`` is left to
    :func:`check_dev_dependencies`, so the pre-gate reports it once (#4992).
    """
    try:
        version.show(repo_root)
    except FileNotFoundError as exc:
        return [str(exc)]
    except version.VersionSyncError as exc:
        if not (repo_root / "package.json").is_file():
            return []
        return [str(exc)]
    return []


def check_build_script(repo_root: Path) -> list[str]:
    """Return the problem with the repo's ``build`` script, empty when it has one.

    The BUILD check and the release build run ``npm run build`` (spec §6.5),
    so ``package.json`` must define ``scripts.build`` as a non-empty string.
    A ``package.json`` that cannot be parsed is the one problem returned.
    """
    try:
        manifest = read_package_json(repo_root / "package.json")
    except PackageJsonError as exc:
        return [str(exc)]
    scripts = manifest.get("scripts")
    build = scripts.get("build") if isinstance(scripts, dict) else None
    if isinstance(build, str) and build.strip():
        return []
    return [
        'package.json: no "build" script (the BUILD check and the release build run it)',
        'Fix: add a "build" script to package.json (for example "tsc -p tsconfig.json")',
    ]


# -- the entry points ---------------------------------------------------------------

_TSCONFIG = "tsconfig.json"
_TSCONFIG_BASE = "./tsconfig.vergil.json"
_ESLINT_CONFIG = "eslint.config.mjs"
_ESLINT_BASE = "./eslint.vergil.config.mjs"
_PRETTIER_CONFIG = ".prettierrc.json"

_TRAILING_COMMA = re.compile(r",(\s*[\]}])")


def _string_end(text: str, start: int) -> int:
    """The index just past the string literal whose quote is at *text[start]*.

    A backslash escapes the next character. An unterminated string runs to
    the end of *text*.
    """
    quote = text[start]
    i = start + 1
    while i < len(text) and text[i] != quote:
        i += 2 if text[i] == "\\" else 1
    return min(i + 1, len(text))


def _strip_jsonc_comments(text: str) -> str:
    """*text* with every ``//`` and ``/* */`` comment outside a string blanked.

    Each comment becomes a space, so a comment between two tokens still
    separates them. An unterminated block comment raises ``ValueError``.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            end = _string_end(text, i)
            out.append(text[i:end])
            i = end
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            out.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                msg = "unterminated /* comment"
                raise ValueError(msg)
            out.append(" ")
            i = end + 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _drop_trailing_commas(text: str) -> str:
    """*text* with every comma right before a ``]`` or ``}`` outside a string removed."""
    parts: list[str] = []
    start = i = 0
    while i < len(text):
        if text[i] == '"':
            parts.append(_TRAILING_COMMA.sub(r"\1", text[start:i]))
            end = _string_end(text, i)
            parts.append(text[i:end])
            start = i = end
        else:
            i += 1
    parts.append(_TRAILING_COMMA.sub(r"\1", text[start:]))
    return "".join(parts)


def load_jsonc(text: str) -> object:
    """Parse *text* as ``tsc`` parses a ``tsconfig.json``.

    JSON with ``//`` and ``/* */`` comments outside strings, and trailing
    commas in arrays and objects. Raises ``ValueError`` when it is not that.
    """
    return json.loads(_drop_trailing_commas(_strip_jsonc_comments(text)))


def _read_text(path: Path) -> str:
    """Read *path* as UTF-8; raise ``ValueError`` naming the failure otherwise."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(exc.strerror or str(exc)) from exc
    except UnicodeDecodeError as exc:
        msg = f"not UTF-8 ({exc.reason})"
        raise ValueError(msg) from exc


def _tsconfig_problem(repo_root: Path) -> str | None:
    """The problem with ``tsconfig.json``'s ``extends``, or ``None`` when it builds on the base."""
    path = repo_root / _TSCONFIG
    if not path.is_file():
        return f'{_TSCONFIG}: missing. Fix: add a {_TSCONFIG} with "extends": "{_TSCONFIG_BASE}"'
    try:
        config = load_jsonc(_read_text(path))
    except ValueError as exc:
        return f"{_TSCONFIG}: cannot be parsed: {exc}. Fix: correct the JSON"
    if not isinstance(config, dict):
        return f"{_TSCONFIG}: the top level is not an object. Fix: correct the JSON"
    extends = config.get("extends")
    if extends == _TSCONFIG_BASE or (isinstance(extends, list) and _TSCONFIG_BASE in extends):
        return None
    return (
        f'{_TSCONFIG}: does not extend "{_TSCONFIG_BASE}". Fix: set '
        f'"extends": "{_TSCONFIG_BASE}", or add it to the "extends" array'
    )


def _scan_js(text: str, *, keep_strings: bool) -> str:
    """*text* with its comments blanked, and its string contents too unless *keep_strings*.

    A light scanner, not a parser: it knows ``'``, ``"`` and template strings
    and ``//`` and ``/* */`` comments, which is enough to read the shape of an
    ESLint config. A blanked string keeps its quotes, so ``"a,b"`` becomes
    ``""`` and never splits an argument list.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            end = _string_end(text, i)
            out.append(text[i:end] if keep_strings else ch + ch)
            i = end
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end
            out.append(" ")
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
            out.append(" ")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


_IDENT = r"[A-Za-z_$][\w$]*"
_BASE_IMPORT = re.compile(
    rf"\bimport\s+(?:(?P<default>{_IDENT})\s*(?:,\s*\{{[^}}]*\}}\s*)?"
    rf"|\{{\s*default\s+as\s+(?P<named>{_IDENT})\s*(?:,[^}}]*)?\}}\s*)"
    rf"from\s*(?P<q>[\"']){re.escape(_ESLINT_BASE)}(?P=q)"
)
_EXPORT_DEFAULT = re.compile(r"\bexport\s+default\s+")
_CALLEE = re.compile(rf"({_IDENT}(?:\s*\.\s*{_IDENT})*)\s*\(")
_CLOSERS = {"[": "]", "(": ")", "{": "}"}


def _top_level_items(text: str, start: int) -> list[str]:
    """The comma-separated items inside the bracket opening at *text[start]*.

    Empty when the bracket never closes or the brackets inside it do not match.
    """
    stack = [_CLOSERS[text[start]]]
    items: list[str] = []
    item_start = start + 1
    for i in range(start + 1, len(text)):
        ch = text[i]
        if ch in _CLOSERS:
            stack.append(_CLOSERS[ch])
        elif ch in ")]}":
            if ch != stack.pop():
                return []
            if not stack:
                items.append(text[item_start:i].strip())
                return items
        elif ch == "," and len(stack) == 1:
            items.append(text[item_start:i].strip())
            item_start = i + 1
    return []


def _exports_base(code: str, binding: str) -> bool:
    """Whether *code*'s default export includes *binding* in a common form.

    Spread into an array (``[...binding, …]``), or passed to ``defineConfig(…)``
    or a ``<name>.config(…)`` such as ``tseslint.config(…)``: plain, spread,
    or spread into an array argument (``defineConfig([...binding, …])``).
    """
    export = _EXPORT_DEFAULT.search(code)
    if export is None:
        return False
    at = export.end()
    spread = re.compile(rf"\.\.\.\s*{re.escape(binding)}")

    def spreads(text: str, start: int) -> bool:
        return any(spread.fullmatch(item) for item in _top_level_items(text, start))

    if code.startswith("[", at):
        return spreads(code, at)
    callee = _CALLEE.match(code, at)
    if callee is None:
        return False
    name = re.sub(r"\s+", "", callee.group(1)).split(".")
    if not (name[-1] == "defineConfig" or (name[-1] == "config" and len(name) > 1)):
        return False
    return any(
        item == binding or spread.fullmatch(item) or (item.startswith("[") and spreads(item, 0))
        for item in _top_level_items(code, callee.end() - 1)
    )


def _eslint_problem(repo_root: Path) -> str | None:
    """The problem with ``eslint.config.mjs``, or ``None`` when it builds on Vergil's config."""
    example = f'import vergil from "{_ESLINT_BASE}"; export default [...vergil];'
    path = repo_root / _ESLINT_CONFIG
    if not path.is_file():
        return (
            f"{_ESLINT_CONFIG}: missing. Fix: add an {_ESLINT_CONFIG} that builds on "
            f"Vergil's config, for example: {example}"
        )
    try:
        text = _read_text(path)
    except ValueError as exc:
        return f"{_ESLINT_CONFIG}: cannot be read: {exc}"
    found = _BASE_IMPORT.search(_scan_js(text, keep_strings=True))
    if found is None:
        return (
            f'{_ESLINT_CONFIG}: does not import a binding from "{_ESLINT_BASE}". '
            f"Fix: import it and spread it, for example: {example}"
        )
    binding = found.group("default") or found.group("named")
    if _exports_base(_scan_js(text, keep_strings=False), binding):
        return None
    return (
        f"{_ESLINT_CONFIG}: its default export does not include {binding} from "
        f'"{_ESLINT_BASE}". Fix: export default [...{binding}, …], or pass {binding} '
        "to defineConfig(…) or tseslint.config(…)"
    )


def _shadowing_eslint_configs(repo_root: Path) -> list[str]:
    """Other ``eslint.config.*`` files at the root: ESLint may read one instead.

    ESLint looks for ``eslint.config.js`` before ``eslint.config.mjs``, and
    for ``.cjs``, ``.ts``, ``.mts`` and ``.cts`` after it, so any of them
    beside ``eslint.config.mjs`` is a config the check above never reads.
    """
    return [
        f"{path.name}: ESLint may read it instead of {_ESLINT_CONFIG}. "
        f"Fix: delete it and keep the repo's ESLint config in {_ESLINT_CONFIG}"
        for path in sorted(repo_root.glob("eslint.config.*"))
        if path.name != _ESLINT_CONFIG
    ]


def check_entry_points(repo_root: Path) -> list[str]:
    """Return the problems with the repo's TypeScript entry points.

    The repo owns ``tsconfig.json`` and ``eslint.config.mjs``, and each must
    build on the managed base the fleet sync writes beside it (epic
    vergil-project/.github#1174, spec §6.4). ``tsconfig.json``, parsed as
    ``tsc`` parses it, must ``extends`` ``"./tsconfig.vergil.json"``, alone
    or in an array. ``eslint.config.mjs`` must import a binding from
    ``"./eslint.vergil.config.mjs"`` and include it in its default export,
    and no other ``eslint.config.*`` may sit beside it. Weakening a rule the
    base sets is an exception (epic vergil-project/.github#375), not
    detected here.
    """
    problems = [
        problem
        for problem in (_tsconfig_problem(repo_root), _eslint_problem(repo_root))
        if problem is not None
    ]
    problems.extend(_shadowing_eslint_configs(repo_root))
    return problems


# -- one Prettier config ------------------------------------------------------------


def _is_prettier_config(name: str) -> bool:
    return name == ".prettierrc" or name.startswith((".prettierrc.", "prettier.config."))


def _repo_prettier_configs(repo_root: Path) -> list[str]:
    """Every Prettier config file in the repo, tracked or untracked, but the root one.

    ``git ls-files`` lists them without walking what ``.gitignore`` excludes,
    such as ``.worktrees/`` (each a checkout with its own managed
    ``.prettierrc.json``), ``build/`` or ``node_modules/``, none of which
    Prettier reads either (as ``_find_repo_sh_files`` does, issue #3830).
    A file under ``node_modules`` is left out even when not ignored. A
    tracked file deleted from the working tree is dropped. A git failure
    raises rather than checking less.
    """
    output = git.read_output(
        "-C",
        str(repo_root),
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
        "--",
        "*prettier*",
    )
    found: set[str] = set()
    for rel in output.split("\0"):
        parts = rel.split("/")
        if (
            rel
            and rel != _PRETTIER_CONFIG
            and _is_prettier_config(parts[-1])
            and "node_modules" not in parts
            and (repo_root / rel).is_file()
        ):
            found.add(rel)
    return sorted(found)


def check_prettier_single_source(repo_root: Path) -> list[str]:
    """Return the problems that give Prettier a second config source.

    The gate runs ``prettier --check . --config .prettierrc.json``, and an
    editor finds ``.prettierrc.json`` on its own (spec §6.4). Any other
    source would let an editor format by a config the gate never reads: a
    ``"prettier"`` key in ``package.json``, or any ``.prettierrc``,
    ``.prettierrc.*`` or ``prettier.config.*`` in the tree other than the
    root ``.prettierrc.json``. What ``.gitignore`` excludes is not searched,
    and neither is ``node_modules``.
    """
    fix = f"Fix: delete it; the repo's one Prettier config is {_PRETTIER_CONFIG}"
    problems: list[str] = []
    package_json = repo_root / "package.json"
    try:
        # A missing package.json has no "prettier" key; check_dev_dependencies
        # reports it missing, once (#4992).
        manifest = read_package_json(package_json) if package_json.exists() else {}
    except PackageJsonError as exc:
        problems.append(f'cannot check package.json for a "prettier" key: {exc}')
    else:
        if "prettier" in manifest:
            problems.append(f'package.json: has a "prettier" key, a second Prettier config. {fix}')
    problems.extend(
        f"{path}: a second Prettier config. {fix}" for path in _repo_prettier_configs(repo_root)
    )
    return problems


PRE_GATE_CHECKS: tuple[Callable[[Path], list[str]], ...] = (
    check_dev_dependencies,
    check_version_matches,
    check_build_script,
    check_entry_points,
    check_prettier_single_source,
)
"""The TypeScript pre-gate checks, run in order; every problem is reported."""


def run_pre_gate_checks(repo_root: Path) -> list[str]:
    """Return every problem from every check in :data:`PRE_GATE_CHECKS`.

    A problem two checks share (a ``package.json`` neither can parse) is
    reported once, where it first appears.
    """
    problems = (problem for check in PRE_GATE_CHECKS for problem in check(repo_root))
    return list(dict.fromkeys(problems))
