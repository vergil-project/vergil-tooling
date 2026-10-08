"""Tests for vergil_tooling.lib.package.python_builder (the ``python`` builder, spec §6.1)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vergil_tooling.lib.config import PackageConfig, PackagePythonConfig
from vergil_tooling.lib.package import PackageError, build, orgs, python_builder
from vergil_tooling.lib.package.build import BuildContext
from vergil_tooling.lib.package.matrix import BuildCell

_SHARED_AMD64 = BuildCell(
    "shared-amd64",
    "amd64",
    "ubuntu-24.04",
    "ubuntu:24.04",
    ("deb", "rpm"),
    ("rhel/9/amd64", "ubuntu/24.04/amd64"),
    native=False,
    suite=None,
)
_NATIVE_RHEL9 = BuildCell(
    "native-rhel-9-arm64",
    "arm64",
    "ubuntu-24.04-arm",
    "registry.access.redhat.com/ubi9/ubi",
    ("rpm",),
    ("rhel/9/arm64",),
    native=True,
    suite="el9",
)
_NATIVE_RESOLUTE = BuildCell(
    "native-ubuntu-26.04-amd64",
    "amd64",
    "ubuntu-24.04",
    "ubuntu:26.04",
    ("deb",),
    ("ubuntu/26.04/amd64",),
    native=True,
    suite="resolute",
)

_PYPROJECT = '[project]\nname = "vergil-tooling"\n[project.scripts]\nvrg-b = "m:b"\nvrg-a = "m:a"\n'


def _pkg(
    *, python: PackagePythonConfig | None = None, vendor: str = "vergil", default_py: bool = True
) -> PackageConfig:
    if python is None and default_py:
        python = PackagePythonConfig(runtime="3.14.4")
    return PackageConfig(builder="python", vendor=vendor, summary="s", smoke="true", python=python)


def _py_ctx(
    tmp_path: Path, *, cell: BuildCell = _SHARED_AMD64, pkg: PackageConfig | None = None
) -> BuildContext:
    staging = tmp_path / "s"
    staging.mkdir(exist_ok=True)
    return BuildContext(
        tmp_path, pkg or _pkg(), cell, "vergil-tooling", "2.1.0", staging, tmp_path / "o"
    )


_FAST = "-o Acquire::Retries=3 -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20"


def _verb(argv: tuple[str, ...]) -> str:
    """``tool subcommand``, skipping ``-o Key=Value`` options (``apt-get install``)."""
    words = [a for a in argv[1:] if not a.startswith("-") and "::" not in a]
    return f"{argv[0]} {words[0]}"


class _Fake:
    """Records every command (bootstrap included) and fakes the runtime install."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, vendor: str = "vergil"
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.envs: dict[str, dict[str, str] | None] = {}
        self.cwds: dict[str, Path | None] = {}
        self.boot: list[tuple[object, ...]] = []
        self.installed = "3.14.4+20261001-1"
        self.fail: str | None = None
        self.opt = tmp_path / "fake-opt"
        self.opt.mkdir()
        self.vendor = vendor

        def fake_bootstrap(
            run: object, org: orgs.OrgRepo, fmt: str, suite: str, *, sudo: bool
        ) -> None:
            self.calls.append(("bootstrap",))
            self.boot.append((run, org, fmt, suite, sudo))

        monkeypatch.setattr(python_builder.repo_setup, "bootstrap", fake_bootstrap)
        monkeypatch.setattr(python_builder, "_run", self.run)
        monkeypatch.setattr(python_builder, "_OPT", str(self.opt))

    def run(
        self, *argv: str, env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.envs[_verb(argv)] = env
        self.cwds[_verb(argv)] = cwd
        if self.fail is not None and argv[:2] == tuple(self.fail.split()):
            raise subprocess.CalledProcessError(2, list(argv), "", "boom on stderr")
        out = ""
        if argv[0] == "dpkg-query":
            out = self.installed + "\n"
        elif argv[0] == "rpm":
            out = self.installed.rsplit("-", 1)[0] + "\n"
        elif argv[:2] == ("uv", "venv"):
            # The venv lands at the final path (under the patched _OPT).
            venv = Path(argv[-1])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").symlink_to("/opt/vergil/python/3.14.4/bin/python3.14")
        elif argv[:2] == ("uv", "sync"):
            venv = self.opt / self.vendor / "vergil-tooling/venv"
            for cmd in ("vrg-a", "vrg-b"):
                (venv / "bin" / cmd).write_text("#!x\n")
        return subprocess.CompletedProcess(argv, 0, out, "")

    @property
    def flat(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


def _repo(tmp_path: Path, pyproject: str = _PYPROJECT, *, lock: bool = True) -> None:
    (tmp_path / "pyproject.toml").write_text(pyproject)
    if lock:
        (tmp_path / "uv.lock").write_text("version = 1\n")


# --- pure helpers -------------------------------------------------------------


def test_runtime_depends_formats() -> None:
    assert python_builder.runtime_depends("3.14.4", "3.14.4+20261001") == {
        "deb": ["vergil-python3.14.4 (>= 3.14.4+20261001)"],
        "rpm": ["vergil-python3.14.4 >= 3.14.4+20261001"],
    }


def test_runtime_dir() -> None:
    assert python_builder.runtime_dir("vergil", "3.14.4") == "/opt/vergil/python/3.14.4"


def test_shims_default_to_all_project_scripts_sorted(tmp_path: Path) -> None:
    _repo(tmp_path)
    assert python_builder.shim_commands(tmp_path, _pkg()) == ["vrg-a", "vrg-b"]


def test_shims_subset_keeps_given_commands(tmp_path: Path) -> None:
    _repo(tmp_path)
    pkg = _pkg(python=PackagePythonConfig(runtime="3.14.4", commands=["vrg-b"]))
    assert python_builder.shim_commands(tmp_path, pkg) == ["vrg-b"]


def test_shims_subset_must_exist(tmp_path: Path) -> None:
    _repo(tmp_path, '[project]\nname="t"\n[project.scripts]\nvrg-a="m:a"\n')
    pkg = _pkg(python=PackagePythonConfig(runtime="3.14.4", commands=["vrg-z"]))
    with pytest.raises(
        PackageError,
        match=r"\[package\.python\]\.commands: 'vrg-z' is not in \[project\.scripts\]",
    ):
        python_builder.shim_commands(tmp_path, pkg)


def test_shims_without_python_table_default_to_all(tmp_path: Path) -> None:
    _repo(tmp_path)
    assert python_builder.shim_commands(tmp_path, _pkg(default_py=False)) == ["vrg-a", "vrg-b"]


def test_shims_no_scripts_is_empty(tmp_path: Path) -> None:
    _repo(tmp_path, '[project]\nname="t"\n')
    assert python_builder.shim_commands(tmp_path, _pkg()) == []


def test_shims_missing_pyproject_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match=r"pyproject\.toml not found"):
        python_builder.shim_commands(tmp_path, _pkg())


def test_shims_invalid_pyproject_is_fatal(tmp_path: Path) -> None:
    _repo(tmp_path, "[project\n")
    with pytest.raises(PackageError, match=r"is not valid TOML"):
        python_builder.shim_commands(tmp_path, _pkg())


@pytest.mark.parametrize(
    "pyproject", ['project = "x"\n', '[project]\nscripts = "x"\n', "[project.scripts]\na = 1\n"]
)
def test_shims_malformed_scripts_table_is_fatal(tmp_path: Path, pyproject: str) -> None:
    _repo(tmp_path, pyproject)
    with pytest.raises(PackageError, match=r"\[project\.scripts\] must be a table"):
        python_builder.shim_commands(tmp_path, _pkg())


# --- build_python ------------------------------------------------------------


def test_python_builder_is_registered() -> None:
    assert build.BUILDERS["python"] is python_builder.build_python


def test_missing_lock_is_fatal(tmp_path: Path) -> None:
    _repo(tmp_path, lock=False)
    with pytest.raises(PackageError, match=r"uv\.lock is required"):
        python_builder.build_python(_py_ctx(tmp_path))


def test_missing_python_table_is_fatal(tmp_path: Path) -> None:
    _repo(tmp_path)
    with pytest.raises(PackageError, match=r"\[package\.python\] is required"):
        python_builder.build_python(_py_ctx(tmp_path, pkg=_pkg(default_py=False)))


def test_build_sequence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    ctx = _py_ctx(tmp_path)
    res = python_builder.build_python(ctx)

    venv = f"{fake.opt}/vergil/vergil-tooling/venv"
    interp = "/opt/vergil/python/3.14.4/bin/python3.14"
    assert fake.flat == [
        "bootstrap",
        # Only the vergil source the keyring package wrote; never the Ubuntu archive.
        f"apt-get {_FAST} -o Dir::Etc::sourcelist=/etc/apt/sources.list.d/vergil.sources"
        " -o Dir::Etc::sourceparts=- -o APT::Get::List-Cleanup=0 update",
        f"apt-get {_FAST} install -y vergil-python3.14.4",
        "dpkg-query -W -f=${Version} vergil-python3.14.4",
        f"uv venv --python {interp} {venv}",
        "uv sync --frozen --no-dev --no-editable --compile-bytecode",
    ]
    # The shared cell builds in ubuntu:24.04: always apt/noble, whatever the targets.
    run, org, fmt, suite, sudo = fake.boot[0]
    assert run is python_builder.repo_setup.local_run
    assert org is orgs.ORGS["vergil"]
    assert (fmt, suite, sudo) == ("deb", "noble", False)
    apt_env = fake.envs["apt-get install"]
    assert apt_env is not None
    assert apt_env["DEBIAN_FRONTEND"] == "noninteractive"
    update_env = fake.envs["apt-get update"]
    assert update_env is not None
    assert update_env["DEBIAN_FRONTEND"] == "noninteractive"
    sync_env = fake.envs["uv sync"]
    assert sync_env is not None
    assert sync_env["UV_PROJECT_ENVIRONMENT"] == venv
    assert sync_env["UV_PYTHON"] == interp
    assert sync_env["UV_PYTHON_DOWNLOADS"] == "never"
    assert fake.cwds["uv sync"] == tmp_path

    assert res.depends == {
        "deb": ["vergil-python3.14.4 (>= 3.14.4+20261001)"],
        "rpm": ["vergil-python3.14.4 >= 3.14.4+20261001"],
    }
    staged_bin = ctx.staging_root / "opt/vergil/vergil-tooling/venv/bin"
    assert (staged_bin / "vrg-a").read_text() == "#!x\n"
    assert (staged_bin / "python").is_symlink()
    assert {
        "src": "/opt/vergil/python/3.14.4/bin/python3.14",
        "dst": "/opt/vergil/vergil-tooling/venv/bin/python",
        "type": "symlink",
    } in res.contents
    # Ownership strictly below /opt/<vendor>, never /opt/<vendor> itself.
    assert {"dst": "/opt/vergil/vergil-tooling", "type": "dir"} in res.contents
    assert not any(e["dst"] == "/opt/vergil" for e in res.contents)
    shims = [e for e in res.contents if e["dst"].startswith("/usr/bin/")]
    final_bin = "/opt/vergil/vergil-tooling/venv/bin"
    assert shims == [
        {"src": f"{final_bin}/{c}", "dst": f"/usr/bin/{c}", "type": "symlink"}
        for c in ("vrg-a", "vrg-b")
    ]


def test_runtime_always_comes_from_vergil_org(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch, vendor="acme")
    res = python_builder.build_python(_py_ctx(tmp_path, pkg=_pkg(vendor="acme")))
    assert fake.boot[0][1] is orgs.ORGS["vergil"]
    assert any(c.startswith("uv venv --python /opt/vergil/python/3.14.4/") for c in fake.flat)
    assert {"dst": "/opt/acme/vergil-tooling", "type": "dir"} in res.contents
    assert {
        "src": "/opt/acme/vergil-tooling/venv/bin/vrg-a",
        "dst": "/usr/bin/vrg-a",
        "type": "symlink",
    } in res.contents


def test_native_rhel_cell_uses_dnf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    res = python_builder.build_python(_py_ctx(tmp_path, cell=_NATIVE_RHEL9))
    assert fake.boot[0][2:4] == ("rpm", "el9")
    assert fake.flat[1:3] == [
        "dnf install -y vergil-python3.14.4",
        "rpm -q --qf %{VERSION} vergil-python3.14.4",
    ]
    assert not any(c.startswith("apt-get") for c in fake.flat)
    assert res.depends["rpm"] == ["vergil-python3.14.4 >= 3.14.4+20261001"]


def test_native_ubuntu_cell_uses_its_own_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    python_builder.build_python(_py_ctx(tmp_path, cell=_NATIVE_RESOLUTE))
    assert fake.boot[0][2:4] == ("deb", "resolute")


def test_python_minor_comes_from_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    pkg = _pkg(python=PackagePythonConfig(runtime="3.13.12"))
    python_builder.build_python(_py_ctx(tmp_path, pkg=pkg))
    assert f"apt-get {_FAST} install -y vergil-python3.13.12" in fake.flat
    interp = "/opt/vergil/python/3.13.12/bin/python3.13"
    assert any(c.startswith(f"uv venv --python {interp} ") for c in fake.flat)


def test_shim_subset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path)
    _Fake(tmp_path, monkeypatch)
    pkg = _pkg(python=PackagePythonConfig(runtime="3.14.4", commands=["vrg-b"]))
    res = python_builder.build_python(_py_ctx(tmp_path, pkg=pkg))
    shims = [e["dst"] for e in res.contents if e["dst"].startswith("/usr/bin/")]
    assert shims == ["/usr/bin/vrg-b"]


def test_shim_target_missing_from_venv_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path, _PYPROJECT + 'vrg-c = "m:c"\n')
    _Fake(tmp_path, monkeypatch)
    with pytest.raises(PackageError, match=r"vrg-c.*not in the built venv"):
        python_builder.build_python(_py_ctx(tmp_path))


def test_existing_product_dir_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    (fake.opt / "vergil/vergil-tooling").mkdir(parents=True)
    with pytest.raises(PackageError, match=r"already exists"):
        python_builder.build_python(_py_ctx(tmp_path))
    assert not any(c.startswith("uv ") for c in fake.flat)


@pytest.mark.parametrize("installed", ["", "-1"])
def test_unreadable_runtime_version_is_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, installed: str
) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    fake.installed = installed
    with pytest.raises(PackageError, match=r"could not read the installed vergil-python3\.14\.4"):
        python_builder.build_python(_py_ctx(tmp_path))


def test_deb_version_without_revision_is_used_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    fake.installed = "3.14.4+20261001"
    res = python_builder.build_python(_py_ctx(tmp_path))
    assert res.depends["deb"] == ["vergil-python3.14.4 (>= 3.14.4+20261001)"]


def test_failed_command_becomes_package_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _repo(tmp_path)
    fake = _Fake(tmp_path, monkeypatch)
    fake.fail = "uv sync"
    with pytest.raises(PackageError, match=r"uv sync .* failed \(exit 2\).*boom on stderr"):
        python_builder.build_python(_py_ctx(tmp_path))


def test_failed_command_without_stderr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo(tmp_path)
    _Fake(tmp_path, monkeypatch)

    def boom(*a: object, **k: object) -> None:
        raise subprocess.CalledProcessError(1, ["curl", "x"])

    monkeypatch.setattr(python_builder.repo_setup, "bootstrap", boom)
    with pytest.raises(PackageError, match=r"curl x failed \(exit 1\)$"):
        python_builder.build_python(_py_ctx(tmp_path))


def test_real_run_streams_stderr_and_captures_stdout(tmp_path: Path) -> None:
    proc = python_builder._run("sh", "-c", "pwd; echo $X", env={"X": "y"}, cwd=tmp_path)
    assert proc.stdout.split() == [str(tmp_path.resolve()), "y"]
    with pytest.raises(subprocess.CalledProcessError):
        python_builder._run("false")
