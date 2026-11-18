from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.update_deps.changes import PackageChange
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updaters import cargo, npm
from vergil_tooling.lib.update_deps.updaters.npm import NpmUpdater

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.update_deps.updaters.npm"
UPDATE = ["vrg-container-run", "--", "npm", "update", "--ignore-scripts"]
CI = ("vrg-container-run", "--", "npm", "ci", "--ignore-scripts")
OUTDATED = ("vrg-container-run", "--", "npm", "outdated", "--json")
CONTAINER_HEADER = "Language: typescript\nImage:    ghcr.io/x/dev-ts-node:26\nCommand:  npm\n---\n"
MANIFEST = {"name": "app", "version": "1.0.0", "devDependencies": {"vitest": "^5.0.0"}}


def _ctx(root: Path, worktree: Path | None = None) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=root, worktree_path=worktree)


def _lock(*packages: tuple[str, str] | tuple[str, str, str | None], version: int = 3) -> str:
    """A lockfile; each package is ``(key or name, version[, license])``, license MIT by default."""
    entries: dict[str, Any] = {"": {"name": "app", "version": "1.0.0"}}
    for pkg in packages:
        key = pkg[0] if pkg[0].startswith("node_modules/") else f"node_modules/{pkg[0]}"
        entry: dict[str, Any] = {"version": pkg[1]}
        lic = pkg[2] if len(pkg) == 3 else "MIT"
        if lic is not None:
            entry["license"] = lic
        entries[key] = entry
    return json.dumps({"name": "app", "lockfileVersion": version, "packages": entries}, indent=2)


def _project(root: Path, lock: str, manifest: dict[str, Any] | None = None) -> None:
    (root / "package.json").write_text(json.dumps(manifest or MANIFEST, indent=2) + "\n")
    (root / "package-lock.json").write_text(lock)


class _Runner:
    """A fake container runner for ``npm ci`` and ``npm outdated --json``."""

    def __init__(
        self,
        report: dict[str, Any] | None = None,
        *,
        code: int | None = None,
        stdout: str | None = None,
        ci_code: int = 0,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        report = report or {}
        self.code = (1 if report else 0) if code is None else code
        self.stdout = CONTAINER_HEADER + json.dumps(report, indent=2) if stdout is None else stdout
        self.ci_code = ci_code

    def __call__(self, cmd: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        if cmd == CI:
            return subprocess.CompletedProcess(cmd, self.ci_code, "added 3 packages", "ci boom")
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, "")


def _outdated_entry(current: str | None, wanted: str, latest: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"wanted": wanted, "latest": latest, "dependent": "app"}
    if current is not None:
        entry["current"] = current
    return entry


def _apply(
    tmp_path: Path,
    monkeypatch,
    *,
    before: str,
    after: str,
    manifest_after: str | None = None,
    runner: _Runner | None = None,
):
    _project(tmp_path, before)
    calls: list[list[str]] = []

    def update(cmd: list[str], **kw: object) -> int:
        calls.append(list(cmd))
        (tmp_path / "package-lock.json").write_text(after)
        if manifest_after is not None:
            (tmp_path / "package.json").write_text(manifest_after)
        return 0

    monkeypatch.setattr(_MOD + ".progress.run", update)
    result = NpmUpdater(run=runner or _Runner()).apply(_ctx(tmp_path))
    assert calls == [UPDATE]
    return result


# ---- applies ---------------------------------------------------------------


def test_npm_applies_needs_both_files(tmp_path: Path) -> None:
    assert NpmUpdater().applies(_ctx(tmp_path)) is False
    (tmp_path / "package.json").write_text("{}")
    assert NpmUpdater().applies(_ctx(tmp_path)) is False
    (tmp_path / "package-lock.json").write_text("{}")
    assert NpmUpdater().applies(_ctx(tmp_path)) is True
    (tmp_path / "package.json").unlink()
    assert NpmUpdater().applies(_ctx(tmp_path)) is False


def test_default_runner_is_cargos() -> None:
    assert NpmUpdater()._run is cargo.run_captured
    assert npm.HELD_BY_RESOLVER is cargo.HELD_BY_RESOLVER


# ---- apply -------------------------------------------------------------------


def test_update_runs_in_container_with_ignore_scripts(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("vitest", "5.0.0"))
    runner = _Runner()
    _apply(tmp_path, monkeypatch, before=lock, after=lock, runner=runner)
    assert runner.calls == [CI, OUTDATED]
    for cmd in (UPDATE, list(CI)):
        assert cmd[:2] == ["vrg-container-run", "--"]
        assert "--ignore-scripts" in cmd


def test_update_reports_moved_packages(tmp_path: Path, monkeypatch) -> None:
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(("@babel/parser", "7.29.13"), ("gone", "0.1.0"), ("same", "3.0.0")),
        after=_lock(("@babel/parser", "7.29.14"), ("same", "3.0.0"), ("fresh", "0.5.0")),
    )
    assert result.updater == "npm"
    assert result.changed is True
    assert result.packages == [
        PackageChange(name="@babel/parser", old="7.29.13", new="7.29.14"),
        PackageChange(name="fresh", old=None, new="0.5.0"),
        PackageChange(name="gone", old="0.1.0", new=None),
    ]
    assert result.summary == (
        "@babel/parser 7.29.13 -> 7.29.14; fresh (new) -> 0.5.0; gone 0.1.0 -> (removed)"
    )
    assert result.commit_message == (
        "chore(deps): npm update\n\n"
        "- @babel/parser 7.29.13 -> 7.29.14\n- fresh (new) -> 0.5.0\n- gone 0.1.0 -> (removed)"
    )
    assert result.held_back == []


def test_update_summary_counts_past_the_limit(tmp_path: Path, monkeypatch) -> None:
    names = [f"p{i}" for i in range(7)]
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(*[(n, "1.0.0") for n in names]),
        after=_lock(*[(n, "1.0.1") for n in names]),
    )
    assert result.summary.endswith("; p4 1.0.0 -> 1.0.1; and 2 more")
    assert result.commit_message.count("\n- ") == 7


def test_nested_copies_are_itemized_together(tmp_path: Path, monkeypatch) -> None:
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(("semver", "7.6.0"), ("node_modules/a/node_modules/semver", "6.3.0")),
        after=_lock(("semver", "7.7.0"), ("node_modules/a/node_modules/semver", "6.3.0")),
    )
    assert result.packages == [PackageChange(name="semver", old="6.3.0, 7.6.0", new="6.3.0, 7.7.0")]


def test_lock_rewritten_without_moves(tmp_path: Path, monkeypatch) -> None:
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(("vitest", "5.0.0")) + "\n",
        after=_lock(("vitest", "5.0.0")),
    )
    assert result.changed is True
    assert result.packages == []
    assert result.summary == "refreshed package-lock.json (no package versions changed)"
    assert result.commit_message == "chore(deps): refresh package-lock.json"


def test_update_no_changes(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("vitest", "5.0.0"))
    result = _apply(tmp_path, monkeypatch, before=lock, after=lock)
    assert result.changed is False
    assert result.packages == []
    assert result.summary == "npm update (no changes)"
    assert result.commit_message == "chore(deps): npm update"


def test_root_workspace_and_link_entries_are_not_packages(tmp_path: Path, monkeypatch) -> None:
    lock = json.loads(_lock(("vitest", "5.0.0")))
    lock["packages"]["packages/lib"] = {"name": "lib", "version": "0.1.0"}
    lock["packages"]["node_modules/lib"] = {"resolved": "packages/lib", "link": True}
    text = json.dumps(lock)
    result = _apply(tmp_path, monkeypatch, before=text, after=text)
    assert result.packages == []
    assert npm.locked_package_versions(lock) == {"vitest": ["5.0.0"]}


def test_apply_reads_the_worktree(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    _project(worktree, _lock(("vitest", "5.0.0")))
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: 0)
    result = NpmUpdater(run=_Runner()).apply(_ctx(tmp_path, worktree))
    assert result.changed is False


def test_apply_fills_held_back(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("vitest", "5.0.0"))
    runner = _Runner({"vitest": _outdated_entry("5.0.0", "5.0.0", "6.0.0")})
    result = _apply(tmp_path, monkeypatch, before=lock, after=lock, runner=runner)
    assert [(e.name, e.current, e.latest) for e in result.held_back] == [
        ("vitest", "5.0.0", "6.0.0")
    ]


def test_changed_package_json_fails(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("vitest", "5.0.0"))
    rewritten = json.dumps({**MANIFEST, "devDependencies": {"vitest": "^5.1.0"}}, indent=2) + "\n"
    with pytest.raises(UpdateDepsError, match=r"npm update changed package\.json") as exc_info:
        _apply(tmp_path, monkeypatch, before=lock, after=lock, manifest_after=rewritten)
    assert ".npmrc" in str(exc_info.value)
    assert exc_info.value.command == "vrg-container-run -- npm update --ignore-scripts"


def test_package_json_whitespace_change_fails(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("vitest", "5.0.0"))
    same_json = json.dumps(MANIFEST) + "\n"
    with pytest.raises(UpdateDepsError, match=r"npm update changed package\.json"):
        _apply(tmp_path, monkeypatch, before=lock, after=lock, manifest_after=same_json)


def test_moved_entry_without_license_fails(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(UpdateDepsError, match=r"no license field: left-pad@1\.3\.1\b"):
        _apply(
            tmp_path,
            monkeypatch,
            before=_lock(("left-pad", "1.3.0"), ("old", "1.0.0", None)),
            after=_lock(("left-pad", "1.3.1", None), ("old", "1.0.0", None)),
        )


def test_added_entry_without_license_fails(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(
        UpdateDepsError, match=r"no license field: fresh@0\.5\.0, other@1\.0\.0\."
    ) as exc_info:
        _apply(
            tmp_path,
            monkeypatch,
            before=_lock(("vitest", "5.0.0")),
            after=_lock(("vitest", "5.0.0"), ("other", "1.0.0", "  "), ("fresh", "0.5.0", None)),
        )
    assert exc_info.value.command == "package-lock.json"


def test_unchanged_entry_without_license_passes(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("old", "1.0.0", None))
    assert _apply(tmp_path, monkeypatch, before=lock, after=lock).changed is False


def test_update_failure_raises(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, _lock())

    def fail(cmd: list[str], **kw: object) -> int:
        raise subprocess.CalledProcessError(1, tuple(cmd), output="npm", stderr="ETIMEDOUT")

    monkeypatch.setattr(_MOD + ".progress.run", fail)
    with pytest.raises(UpdateDepsError, match=r"npm update failed \(exit 1\)") as exc_info:
        NpmUpdater(run=_Runner()).apply(_ctx(tmp_path))
    assert exc_info.value.command == "vrg-container-run -- npm update --ignore-scripts"
    assert exc_info.value.detail == "npm\nETIMEDOUT"


@pytest.mark.parametrize("version", [1, None, "3", True])
def test_pre_v2_lockfile_fails(tmp_path: Path, monkeypatch, version: object) -> None:
    lock = json.loads(_lock(("vitest", "5.0.0")))
    lock["lockfileVersion"] = version
    _project(tmp_path, json.dumps(lock))
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: pytest.fail("ran npm"))
    with pytest.raises(
        UpdateDepsError, match=r"package-lock\.json: lockfileVersion .* older than 2"
    ):
        NpmUpdater(run=_Runner()).apply(_ctx(tmp_path))


@pytest.mark.parametrize(
    ("packages", "why"),
    [
        (None, "packages map is missing"),
        ([], "packages map is missing"),
        ({"node_modules/x": "1.0.0"}, "entry 'node_modules/x' is not an object"),
        ({"node_modules/x": {"license": "MIT"}}, "entry 'node_modules/x' has no string version"),
    ],
)
def test_malformed_lock_packages_fail(tmp_path: Path, packages: object, why: str) -> None:
    lock: dict[str, Any] = {"lockfileVersion": 3}
    if packages is not None:
        lock["packages"] = packages
    _project(tmp_path, json.dumps(lock))
    with pytest.raises(UpdateDepsError, match=why):
        NpmUpdater(run=_Runner()).apply(_ctx(tmp_path))


@pytest.mark.parametrize("text", ["{", "[]", b"\xff\xfe".decode("latin-1")])
def test_unreadable_lockfile_fails(tmp_path: Path, monkeypatch, text: str) -> None:
    _project(tmp_path, "")
    (tmp_path / "package-lock.json").write_text(text, encoding="latin-1")
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: pytest.fail("ran npm"))
    with pytest.raises(UpdateDepsError, match=r"Cannot read package-lock\.json") as exc_info:
        NpmUpdater(run=_Runner()).apply(_ctx(tmp_path))
    assert exc_info.value.command == str(tmp_path / "package-lock.json")


def test_lockfile_directory_fails(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "package-lock.json").mkdir()
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: pytest.fail("ran npm"))
    with pytest.raises(UpdateDepsError, match=r"Cannot read package-lock\.json"):
        NpmUpdater(run=_Runner()).apply(_ctx(tmp_path))


@pytest.mark.parametrize("text", ["{", "[]"])
def test_unreadable_package_json_fails(tmp_path: Path, monkeypatch, text: str) -> None:
    _project(tmp_path, _lock())
    (tmp_path / "package.json").write_text(text)
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: pytest.fail("ran npm"))
    with pytest.raises(UpdateDepsError, match=r"Cannot read package\.json") as exc_info:
        NpmUpdater(run=_Runner()).apply(_ctx(tmp_path))
    assert exc_info.value.command == str(tmp_path / "package.json")


# ---- npm outdated ------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "report", "error"),
    [
        (0, {}, None),
        (1, {"x": _outdated_entry("1.0.0", "1.0.0", "2.0.0")}, None),
        (1, {}, "exited 1 reporting nothing outdated"),
        (0, {"x": _outdated_entry("1.0.0", "1.0.0", "2.0.0")}, "exited 0 reporting packages"),
        (1, {"error": {"code": "ENOTFOUND", "summary": "registry down"}}, "reported an error"),
        (2, {"x": _outdated_entry("1.0.0", "1.0.0", "2.0.0")}, "exited 2"),
    ],
)
def test_outdated_exit_contract(code: int, report: dict[str, Any], error: str | None) -> None:
    runner = _Runner(report, code=code)
    if error is None:
        assert npm.outdated(runner) == report
        assert runner.calls == [CI, OUTDATED]
        return
    with pytest.raises(UpdateDepsError, match=error) as exc_info:
        npm.outdated(runner)
    assert exc_info.value.command == "vrg-container-run -- npm outdated --json"


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        (CONTAINER_HEADER, "printed no JSON document"),
        ("", "printed no JSON document"),
        (CONTAINER_HEADER + '{"x": ', "printed malformed JSON"),
        (CONTAINER_HEADER + '{"x": 1}', "reported x as int, not an object"),
    ],
)
def test_outdated_without_a_report_fails(stdout: str, why: str) -> None:
    with pytest.raises(UpdateDepsError, match=why):
        npm.outdated(_Runner(code=1, stdout=stdout))


def test_outdated_after_header_lines() -> None:
    report = {"x": _outdated_entry("1.0.0", "1.0.0", "2.0.0")}
    stdout = CONTAINER_HEADER + "npm warn config something\n" + json.dumps(report, indent=2) + "\n"
    assert npm.outdated(_Runner(code=1, stdout=stdout)) == report


def test_outdated_json_array_is_not_an_object() -> None:
    # A line beginning "{" starts the document; what follows must parse to an object.
    with pytest.raises(UpdateDepsError, match="malformed JSON"):
        npm.outdated(_Runner(code=1, stdout='{"a": 1}\n]'))


def test_failed_npm_ci_raises_before_outdated() -> None:
    runner = _Runner(ci_code=1)
    with pytest.raises(UpdateDepsError, match=r"npm ci failed \(exit 1\)") as exc_info:
        npm.outdated(runner)
    assert runner.calls == [CI]
    assert exc_info.value.command == "vrg-container-run -- npm ci --ignore-scripts"
    assert exc_info.value.detail == "added 3 packages\nci boom"


# ---- held back ---------------------------------------------------------------


def _held(
    tmp_path: Path,
    manifest: dict[str, Any],
    report: dict[str, Any],
    lock: str | None = None,
) -> list[HeldBack]:
    _project(tmp_path, lock or _lock(), manifest)
    return npm.held_back(_ctx(tmp_path), _Runner(report))


def test_held_back_outside_range(tmp_path: Path) -> None:
    manifest = {"devDependencies": {"@types/node": "^24"}}
    report = {"@types/node": _outdated_entry("24.19.2", "24.19.2", "26.6.6")}
    assert _held(tmp_path, manifest, report) == [
        HeldBack(
            source="npm",
            name="@types/node",
            current="24.19.2",
            latest="26.6.6",
            note="outside the package.json range",
        )
    ]


def test_held_back_vergil_major(tmp_path: Path) -> None:
    manifest = {"devDependencies": {"typescript": "^6.0.3"}}
    report = {"typescript": _outdated_entry("6.0.3", "6.0.3", "7.0.2")}
    [entry] = _held(tmp_path, manifest, report)
    assert (entry.current, entry.latest) == ("6.0.3", "7.0.2")
    assert entry.note == (
        "outside the package.json range; Vergil accepts typescript 6 only (dev-dependencies.json)"
    )


def test_vergil_major_note() -> None:
    assert npm.vergil_major_note("eslint", 10) == (
        "Vergil accepts eslint 10 only (dev-dependencies.json)"
    )


def test_held_back_by_resolver(tmp_path: Path) -> None:
    manifest = {"dependencies": {"a": "^1.0.0"}, "optionalDependencies": {"b": "^2.0.0"}}
    report = {
        "a": _outdated_entry("1.2.0", "1.4.0", "1.4.0"),
        "b": _outdated_entry("2.0.0", "2.1.0", "3.0.0"),
    }
    assert [(e.name, e.note) for e in _held(tmp_path, manifest, report)] == [
        ("a", "held by the resolver: another requirement in the dependency graph"),
        (
            "b",
            "outside the package.json range; "
            "held by the resolver: another requirement in the dependency graph",
        ),
    ]


def test_current_at_or_above_latest_not_held(tmp_path: Path) -> None:
    manifest = {"dependencies": {"a": "^2.0.0-rc.1", "b": "^1.0.0", "c": "^2.0.0-rc.1"}}
    report = {
        "a": _outdated_entry("2.0.0-rc.1", "2.0.0-rc.1", "1.9.0"),
        "b": _outdated_entry("1.9.0", "1.9.0", "1.9.0"),
        "c": _outdated_entry("2.0.0-rc.1", "2.0.0-rc.1", "2.0.0"),
    }
    assert [(e.name, e.current, e.latest) for e in _held(tmp_path, manifest, report)] == [
        ("c", "2.0.0-rc.1", "2.0.0")
    ]


def test_missing_current_from_lockfile(tmp_path: Path) -> None:
    manifest = {"optionalDependencies": {"fsevents": "^2.3.0"}}
    report = {"fsevents": _outdated_entry(None, "2.3.3", "3.0.0")}
    lock = _lock(("fsevents", "2.3.3"), ("node_modules/x/node_modules/fsevents", "1.0.0"))
    [entry] = _held(tmp_path, manifest, report, lock)
    assert (entry.current, entry.latest, entry.note) == (
        "2.3.3",
        "3.0.0",
        "outside the package.json range",
    )


def test_missing_everywhere_fails(tmp_path: Path) -> None:
    manifest = {"optionalDependencies": {"fsevents": "^2.3.0"}}
    report = {"fsevents": _outdated_entry(None, "2.3.3", "3.0.0")}
    lock = _lock(("node_modules/x/node_modules/fsevents", "2.3.3"))
    with pytest.raises(
        UpdateDepsError, match=r"fsevents is a direct dependency .* no node_modules"
    ):
        _held(tmp_path, manifest, report, lock)


@pytest.mark.parametrize(
    "spec",
    [
        "file:../lib",
        "link:../lib",
        "git+https://github.com/o/r.git",
        "git://github.com/o/r.git",
        "github:o/r",
        "o/r#main",
        "https://example.com/x.tgz",
        "./vendor/x",
        "../x",
    ],
)
def test_non_registry_specs_skipped(tmp_path: Path, spec: str) -> None:
    manifest = {"dependencies": {"x": spec}}
    report = {"x": {"current": "1.0.0", "wanted": "git", "latest": "git"}}
    assert _held(tmp_path, manifest, report) == []


def test_npm_alias_is_a_registry_spec(tmp_path: Path) -> None:
    manifest = {"dependencies": {"x": "npm:@o/x@^1.0.0"}}
    report = {"x": _outdated_entry("1.0.0", "1.0.0", "2.0.0")}
    assert [e.name for e in _held(tmp_path, manifest, report)] == ["x"]


def test_undeclared_dependency_not_reported(tmp_path: Path) -> None:
    manifest = {"peerDependencies": {"react": "^18"}}
    report = {"react": _outdated_entry("18.3.1", "18.3.1", "19.0.0")}
    assert _held(tmp_path, manifest, report) == []


@pytest.mark.parametrize(
    ("entry", "why"),
    [
        ({"current": "1.0.0", "latest": "2.0.0"}, "no wanted version for x"),
        ({"current": "1.0.0", "wanted": "1.0.0", "latest": "git"}, "x latest 'git'"),
        ({"current": 1, "wanted": "1.0.0", "latest": "2.0.0"}, "no current version for x"),
    ],
)
def test_malformed_outdated_versions_fail(tmp_path: Path, entry: dict[str, Any], why: str) -> None:
    with pytest.raises(UpdateDepsError, match=why):
        _held(tmp_path, {"dependencies": {"x": "^1.0.0"}}, {"x": entry})


@pytest.mark.parametrize("table", [[], {"x": 1}])
def test_malformed_dependency_table_fails(tmp_path: Path, table: object) -> None:
    with pytest.raises(UpdateDepsError, match="devDependencies must map package names"):
        _held(tmp_path, {"devDependencies": table}, {})
