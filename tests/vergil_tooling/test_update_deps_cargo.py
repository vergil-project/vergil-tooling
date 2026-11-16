from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.update_deps import sparse_index
from vergil_tooling.lib.update_deps.changes import PackageChange
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.sparse_index import SparseIndexError
from vergil_tooling.lib.update_deps.updaters import cargo
from vergil_tooling.lib.update_deps.updaters.cargo import CargoUpdater

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.update_deps.updaters.cargo"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"
METADATA = [
    "vrg-container-run",
    "--",
    "cargo",
    "metadata",
    "--format-version",
    "1",
    "--locked",
]
CONTAINER_HEADER = "Language: rust\nImage:    ghcr.io/x/dev-rust:1\nCommand:  cargo metadata\n---\n"


def _ctx(root: Path, worktree: Path | None = None) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=root, worktree_path=worktree)


def _lock(*packages: tuple[str, str] | tuple[str, str, str | None]) -> str:
    out = ["version = 4", ""]
    for pkg in packages:
        name, version = pkg[0], pkg[1]
        source = pkg[2] if len(pkg) == 3 else CRATES_IO
        out += ["[[package]]", f'name = "{name}"', f'version = "{version}"']
        if source is not None:
            out.append(f'source = "{source}"')
        out.append("")
    return "\n".join(out)


def _dep(
    name: str, req: str, *, source: str | None = CRATES_IO, kind: str | None = None
) -> dict[str, Any]:
    return {"name": name, "req": req, "source": source, "kind": kind, "rename": None}


def _member(
    name: str, deps: list[dict[str, Any]], *, rust_version: str | None = None
) -> dict[str, Any]:
    return {
        "id": f"path+file:///w/{name}#{name}@0.1.0",
        "name": name,
        "version": "0.1.0",
        "rust_version": rust_version,
        "dependencies": deps,
    }


def _metadata(*members: dict[str, Any]) -> dict[str, Any]:
    return {
        "packages": list(members),
        "workspace_members": [m["id"] for m in members],
    }


def _index(table: dict[str, list[tuple[str, bool, str | None]]]) -> sparse_index.Fetch:
    def fetch(url: str) -> bytes:
        crate = url.rsplit("/", 1)[-1]
        lines = [
            json.dumps({"name": crate, "vers": v, "yanked": y, "rust_version": r})
            for v, y, r in table[crate]
        ]
        return "\n".join(lines).encode()

    return fetch


def _stable(*versions: str) -> list[tuple[str, bool, str | None]]:
    return [(v, False, None) for v in versions]


class _Runner:
    """A fake container runner for ``cargo metadata``."""

    def __init__(self, metadata: dict[str, Any] | None = None, *, code: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.metadata = metadata
        self.code = code
        self.stdout = CONTAINER_HEADER + (json.dumps(metadata) + "\n" if metadata else "")

    def __call__(self, cmd: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, self.code, self.stdout, "boom" if self.code else "")


def _project(root: Path, lock: str) -> Path:
    (root / "Cargo.toml").write_text('[package]\nname = "app"\nversion = "0.1.0"\n')
    path = root / "Cargo.lock"
    path.write_text(lock)
    return path


def _install(monkeypatch, runner: _Runner) -> None:
    monkeypatch.setattr(_MOD + ".run_captured", runner)


# ---- applies ---------------------------------------------------------------


def test_cargo_applies_needs_lockfile(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert CargoUpdater().applies(_ctx(tmp_path)) is False
    (tmp_path / "Cargo.lock").write_text("")
    assert CargoUpdater().applies(_ctx(tmp_path)) is True


def test_cargo_applies_needs_manifest(tmp_path: Path) -> None:
    (tmp_path / "Cargo.lock").write_text("")
    assert CargoUpdater().applies(_ctx(tmp_path)) is False


def test_default_fetch_is_the_sparse_index() -> None:
    assert CargoUpdater()._fetch is sparse_index.fetch_url


# ---- cargo metadata --------------------------------------------------------


def test_metadata_runs_in_container(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, _lock())
    runner = _Runner(_metadata(_member("app", [])))
    _install(monkeypatch, runner)
    assert cargo.held_back(_ctx(tmp_path), _index({})) == []
    assert runner.calls == [METADATA]
    assert all(call[0] == "vrg-container-run" for call in runner.calls)


def test_metadata_failure_raises(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, _lock())
    _install(monkeypatch, _Runner(code=101))
    with pytest.raises(UpdateDepsError, match=r"cargo metadata failed \(exit 101\)") as exc_info:
        cargo.held_back(_ctx(tmp_path), _index({}))
    assert exc_info.value.command == " ".join(METADATA)
    assert exc_info.value.detail is not None
    assert "boom" in exc_info.value.detail


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        (CONTAINER_HEADER, "no JSON document"),
        ("", "no JSON object"),
        (CONTAINER_HEADER + "[1]\n", "no JSON object"),
    ],
)
def test_metadata_without_json_raises(tmp_path: Path, monkeypatch, stdout: str, why: str) -> None:
    _project(tmp_path, _lock())
    runner = _Runner()
    runner.stdout = stdout
    _install(monkeypatch, runner)
    with pytest.raises(UpdateDepsError, match=why):
        cargo.held_back(_ctx(tmp_path), _index({}))


@pytest.mark.parametrize(
    ("metadata", "why"),
    [
        ({"packages": []}, "no workspace_members"),
        ({"packages": [], "workspace_members": ["x"]}, "workspace member 'x' with no package"),
        (
            {"packages": [{"id": "a", "name": "a"}], "workspace_members": ["a"]},
            "a: cargo metadata has no dependencies list",
        ),
        (
            {
                "packages": [{"id": "a", "name": "a", "dependencies": [{}]}],
                "workspace_members": ["a"],
            },
            "malformed dependency entry",
        ),
        (
            _metadata(_member("a", [{"name": "serde", "source": CRATES_IO, "req": None}])),
            "serde has no requirement",
        ),
        (_metadata({**_member("a", []), "rust_version": 1}), "rust_version is not a string"),
    ],
)
def test_malformed_metadata_raises(tmp_path: Path, monkeypatch, metadata, why: str) -> None:
    _project(tmp_path, _lock())
    _install(monkeypatch, _Runner(metadata))
    with pytest.raises(UpdateDepsError, match=why):
        cargo.held_back(_ctx(tmp_path), _index({}))


# ---- the held-back report --------------------------------------------------


def _held(tmp_path: Path, monkeypatch, metadata, lock: str, index) -> list[HeldBack]:
    _project(tmp_path, lock)
    _install(monkeypatch, _Runner(metadata))
    return cargo.held_back(_ctx(tmp_path), _index(index))


def test_held_back_outside_requirement(tmp_path: Path, monkeypatch) -> None:
    entries = _held(
        tmp_path,
        monkeypatch,
        _metadata(_member("app", [_dep("serde", "^1")])),
        _lock(("serde", "1.0.200")),
        {"serde": _stable("1.0.200", "2.0.0")},
    )
    assert entries == [
        HeldBack(
            source="cargo",
            name="serde",
            current="1.0.200",
            latest="2.0.0",
            note="outside the Cargo.toml requirement",
        )
    ]


def test_held_back_rust_version(tmp_path: Path, monkeypatch) -> None:
    entries = _held(
        tmp_path,
        monkeypatch,
        _metadata(_member("app", [_dep("serde", "^1")], rust_version="1.80")),
        _lock(("serde", "1.0.200")),
        {"serde": [("1.0.200", False, "1.70"), ("1.0.210", False, "1.85.0")]},
    )
    assert [e.note for e in entries] == ["needs a newer Rust than rust-version"]
    assert (entries[0].current, entries[0].latest) == ("1.0.200", "1.0.210")


def test_held_back_both_reasons(tmp_path: Path, monkeypatch) -> None:
    entries = _held(
        tmp_path,
        monkeypatch,
        _metadata(_member("app", [_dep("serde", "^1")], rust_version="1.80")),
        _lock(("serde", "1.0.200")),
        {"serde": [("1.0.200", False, None), ("2.0.0", False, "1.90")]},
    )
    assert [e.note for e in entries] == [
        "outside the Cargo.toml requirement; needs a newer Rust than rust-version"
    ]


def test_held_back_by_resolver(tmp_path: Path, monkeypatch) -> None:
    """A sibling's ``=1.0.150`` holds the crate below a newest version both reqs could reach."""
    metadata = _metadata(
        _member("app", [_dep("serde", "^1.0.100")], rust_version="1.90"),
        _member("pinned", [_dep("serde", "=1.0.150")]),
    )
    entries = _held(
        tmp_path,
        monkeypatch,
        metadata,
        _lock(("serde", "1.0.150")),
        {"serde": [("1.0.150", False, None), ("1.0.200", False, "1.80")]},
    )
    assert entries == [
        HeldBack(
            source="cargo",
            name="serde",
            current="1.0.150",
            latest="1.0.200",
            note="held by the resolver: another requirement in the dependency graph",
        ),
        HeldBack(
            source="cargo",
            name="serde",
            current="1.0.150",
            latest="1.0.200",
            note="outside the Cargo.toml requirement",
        ),
    ]


def test_not_held_back_when_only_yanked_newer(tmp_path: Path, monkeypatch) -> None:
    entries = _held(
        tmp_path,
        monkeypatch,
        _metadata(_member("app", [_dep("serde", "^1"), _dep("tokio", "^1")])),
        _lock(("serde", "1.0.200"), ("tokio", "1.40.0")),
        {
            "serde": [("1.0.200", False, None), ("1.0.201", True, None), ("2.0.0", True, None)],
            "tokio": [("1.40.0", False, None), ("2.0.0-alpha.1", False, None)],
        },
    )
    assert entries == []


def test_not_held_back_when_nothing_stable(tmp_path: Path, monkeypatch) -> None:
    entries = _held(
        tmp_path,
        monkeypatch,
        _metadata(_member("app", [_dep("pre", "^0.1.0-alpha")])),
        _lock(("pre", "0.1.0-alpha")),
        {"pre": [("0.1.0-alpha", False, None)]},
    )
    assert entries == []


def test_inherited_dependency_uses_metadata_requirement(tmp_path: Path, monkeypatch) -> None:
    """``serde.workspace = true`` reaches cargo metadata as the workspace's requirement."""
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["app"]\n[workspace.dependencies]\nserde = "~1.0.100"\n'
    )
    (tmp_path / "Cargo.lock").write_text(_lock(("serde", "1.0.190")))
    _install(monkeypatch, _Runner(_metadata(_member("app", [_dep("serde", "~1.0.100")]))))
    entries = cargo.held_back(_ctx(tmp_path), _index({"serde": _stable("1.0.190", "1.1.0")}))
    # 1.1.0 is outside ~1.0.100 (the inherited requirement), though inside a bare "1".
    assert [e.note for e in entries] == ["outside the Cargo.toml requirement"]


def test_path_and_git_dependencies_skipped(tmp_path: Path, monkeypatch) -> None:
    def fetch(url: str) -> bytes:
        pytest.fail(f"fetched {url}")

    _project(tmp_path, _lock(("local", "0.1.0", None)))
    deps = [
        _dep("local", "*", source=None),
        _dep("forked", "^1", source="git+https://github.com/o/forked#abc"),
    ]
    _install(monkeypatch, _Runner(_metadata(_member("app", deps))))
    assert cargo.held_back(_ctx(tmp_path), fetch) == []


@pytest.mark.parametrize("patched_source", [None, "git+https://github.com/o/serde#abc"])
def test_patched_crates_io_dependency_skipped(
    tmp_path: Path, monkeypatch, patched_source: str | None
) -> None:
    """A crates.io dependency that [patch.crates-io] points at a path or git crate is skipped."""
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return _index({"tokio": _stable("1.0.0", "2.0.0")})(url)

    _project(tmp_path, _lock(("serde", "1.0.200", patched_source), ("tokio", "1.0.0")))
    deps = [_dep("serde", "^1"), _dep("tokio", "^1")]
    _install(monkeypatch, _Runner(_metadata(_member("app", deps))))
    entries = cargo.held_back(_ctx(tmp_path), fetch)
    assert [(e.name, e.latest) for e in entries] == [("tokio", "2.0.0")]
    assert fetched == ["https://index.crates.io/to/ki/tokio"]


def test_other_registry_dependency_fails_loud(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, _lock())
    deps = [_dep("internal", "^1", source="sparse+https://registry.example.com/")]
    _install(monkeypatch, _Runner(_metadata(_member("app", deps))))
    with pytest.raises(UpdateDepsError, match="not crates.io"):
        cargo.held_back(_ctx(tmp_path), _index({}))


def test_dev_and_build_dependencies_are_checked_and_deduplicated(
    tmp_path: Path, monkeypatch
) -> None:
    fetched: list[str] = []
    table = {"cc": _stable("1.0.0", "2.0.0"), "serde": _stable("1.0.0", "2.0.0")}

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return _index(table)(url)

    _project(tmp_path, _lock(("cc", "1.0.0"), ("serde", "1.0.0")))
    deps = [
        _dep("serde", "^1"),
        _dep("serde", "^1", kind="dev"),
        _dep("cc", "^1", kind="build"),
    ]
    _install(
        monkeypatch, _Runner(_metadata(_member("a", deps), _member("b", [_dep("serde", "^1")])))
    )
    entries = cargo.held_back(_ctx(tmp_path), fetch)
    assert [(e.name, e.current, e.latest) for e in entries] == [
        ("serde", "1.0.0", "2.0.0"),
        ("cc", "1.0.0", "2.0.0"),
    ]
    assert len(fetched) == 2


def test_multi_version_lock_picks_the_version_the_requirement_matches(
    tmp_path: Path, monkeypatch
) -> None:
    entries = _held(
        tmp_path,
        monkeypatch,
        _metadata(_member("app", [_dep("rand", "^0.7")])),
        _lock(("rand", "0.7.3"), ("rand", "0.8.5"), ("rand", "0.9.0", None)),
        {"rand": _stable("0.7.3", "0.9.1")},
    )
    assert [(e.current, e.latest) for e in entries] == [("0.7.3", "0.9.1")]


def test_unlocked_dependency_fails_loud(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(UpdateDepsError, match="no matching version locked"):
        _held(
            tmp_path,
            monkeypatch,
            _metadata(_member("app", [_dep("serde", "^2")])),
            _lock(("serde", "1.0.0")),
            {"serde": _stable("2.0.0")},
        )


def test_unparseable_requirement_wraps_value_error(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(UpdateDepsError, match=r"serde .*'not a req'") as exc_info:
        _held(
            tmp_path,
            monkeypatch,
            _metadata(_member("app", [_dep("serde", "not a req")])),
            _lock(("serde", "1.0.0")),
            {"serde": _stable("1.0.0")},
        )
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_invalid_member_rust_version_fails_loud(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(UpdateDepsError, match="app: not a rust-version"):
        _held(
            tmp_path,
            monkeypatch,
            _metadata(_member("app", [_dep("serde", "^1")], rust_version="latest")),
            _lock(("serde", "1.0.0")),
            {"serde": [("1.0.0", False, None), ("1.1.0", False, "1.80")]},
        )


def test_index_failure_propagates_from_held_back(tmp_path: Path, monkeypatch) -> None:
    def fetch(url: str) -> bytes:
        raise OSError("connection reset")

    _project(tmp_path, _lock(("serde", "1.0.0")))
    _install(monkeypatch, _Runner(_metadata(_member("app", [_dep("serde", "^1")]))))
    with pytest.raises(SparseIndexError, match="connection reset"):
        cargo.held_back(_ctx(tmp_path), fetch)


# ---- apply -------------------------------------------------------------------


def _apply(
    tmp_path: Path,
    monkeypatch,
    *,
    before: str,
    after: str,
    porcelain: str = " M Cargo.lock",
    metadata: dict[str, Any] | None = None,
    index: sparse_index.Fetch | None = None,
):
    path = _project(tmp_path, before)
    calls: list[list[str]] = []

    def update(cmd: list[str], **kw: object) -> int:
        calls.append(list(cmd))
        path.write_text(after)
        return 0

    monkeypatch.setattr(_MOD + ".progress.run", update)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: porcelain)
    _install(monkeypatch, _Runner(metadata or _metadata(_member("app", []))))
    result = CargoUpdater(fetch=index or _index({})).apply(_ctx(tmp_path))
    assert calls == [["vrg-container-run", "--", "cargo", "update"]]
    return result


def test_cargo_update_reports_moved_crates(tmp_path: Path, monkeypatch) -> None:
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(("serde", "1.0.200"), ("gone", "0.1.0"), ("same", "3.0.0")),
        after=_lock(("serde", "1.0.210"), ("same", "3.0.0"), ("fresh", "0.5.0")),
    )
    assert result.updater == "cargo"
    assert result.changed is True
    assert result.packages == [
        PackageChange(name="fresh", old=None, new="0.5.0"),
        PackageChange(name="gone", old="0.1.0", new=None),
        PackageChange(name="serde", old="1.0.200", new="1.0.210"),
    ]
    assert (
        result.summary == "fresh (new) -> 0.5.0; gone 0.1.0 -> (removed); serde 1.0.200 -> 1.0.210"
    )
    assert result.commit_message == (
        "chore(deps): cargo update\n\n"
        "- fresh (new) -> 0.5.0\n- gone 0.1.0 -> (removed)\n- serde 1.0.200 -> 1.0.210"
    )
    assert result.held_back == []


def test_cargo_update_summary_counts_past_the_limit(tmp_path: Path, monkeypatch) -> None:
    names = [f"c{i}" for i in range(7)]
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(*[(n, "1.0.0") for n in names]),
        after=_lock(*[(n, "1.0.1") for n in names]),
    )
    assert result.summary.endswith("; c4 1.0.0 -> 1.0.1; and 2 more")
    assert result.commit_message.count("\n- ") == 7


def test_cargo_update_multi_version_crate(tmp_path: Path, monkeypatch) -> None:
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(("rand", "0.7.3"), ("rand", "0.8.4")),
        after=_lock(("rand", "0.7.3"), ("rand", "0.8.5")),
    )
    assert result.packages == [PackageChange(name="rand", old="0.7.3, 0.8.4", new="0.7.3, 0.8.5")]
    assert result.summary == "rand 0.7.3, 0.8.4 -> 0.7.3, 0.8.5"


def test_cargo_lock_rewritten_without_moves(tmp_path: Path, monkeypatch) -> None:
    result = _apply(
        tmp_path,
        monkeypatch,
        before=_lock(("serde", "1.0.200"), ("unused", "0.1.0", None)) + "# old format\n",
        after=_lock(("serde", "1.0.200"), ("unused", "0.1.0", None)),
    )
    assert result.changed is True
    assert result.packages == []
    assert result.commit_message == "chore(deps): refresh Cargo.lock"


def test_cargo_update_no_changes(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("serde", "1.0.200"))
    result = _apply(tmp_path, monkeypatch, before=lock, after=lock, porcelain="")
    assert result.changed is False
    assert result.packages == []
    assert result.summary == "cargo update (no changes)"


def test_apply_fills_held_back(tmp_path: Path, monkeypatch) -> None:
    lock = _lock(("serde", "1.0.200"))
    result = _apply(
        tmp_path,
        monkeypatch,
        before=lock,
        after=lock,
        porcelain="",
        metadata=_metadata(_member("app", [_dep("serde", "^1")])),
        index=_index({"serde": _stable("2.0.0")}),
    )
    assert [(e.name, e.latest) for e in result.held_back] == [("serde", "2.0.0")]


def test_apply_reads_the_worktree(tmp_path: Path, monkeypatch) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    lock = _lock(("serde", "1.0.200"))
    _project(worktree, lock)
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: "")
    _install(monkeypatch, _Runner(_metadata(_member("app", []))))
    result = CargoUpdater(fetch=_index({})).apply(_ctx(tmp_path, worktree))
    assert result.changed is False


def test_cargo_update_failure_raises(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, _lock())

    def fail(cmd: list[str], **kw: object) -> int:
        raise subprocess.CalledProcessError(101, tuple(cmd), output="Updating", stderr="no network")

    monkeypatch.setattr(_MOD + ".progress.run", fail)
    with pytest.raises(UpdateDepsError, match=r"cargo update failed \(exit 101\)") as exc_info:
        CargoUpdater(fetch=_index({})).apply(_ctx(tmp_path))
    assert exc_info.value.command == "vrg-container-run -- cargo update"
    assert exc_info.value.detail == "Updating\nno network"


def test_corrupt_manifest_raises_before_update(tmp_path: Path, monkeypatch) -> None:
    _project(tmp_path, _lock())
    (tmp_path / "Cargo.toml").write_text("[package\n")
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: pytest.fail("ran cargo"))
    with pytest.raises(UpdateDepsError, match=r"Cannot read Cargo\.toml") as exc_info:
        CargoUpdater().apply(_ctx(tmp_path))
    assert exc_info.value.command == str(tmp_path / "Cargo.toml")


def test_unreadable_lockfile_raises(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    (tmp_path / "Cargo.lock").mkdir()
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: pytest.fail("ran cargo"))
    with pytest.raises(UpdateDepsError, match=r"Cannot read Cargo\.lock"):
        CargoUpdater().apply(_ctx(tmp_path))


@pytest.mark.parametrize(
    "lock",
    ['package = "x"\n', '[[package]]\nname = "x"\n', '[[package]]\nname = 1\nversion = "1.0.0"\n'],
)
def test_malformed_lock_packages_raise(tmp_path: Path, lock: str) -> None:
    _project(tmp_path, lock)
    with pytest.raises(UpdateDepsError, match="string name and version"):
        CargoUpdater().apply(_ctx(tmp_path))


def test_run_captured_captures_output() -> None:
    result = cargo.run_captured((sys.executable, "-c", "print('hi')"))
    assert (result.returncode, result.stdout) == (0, "hi\n")
