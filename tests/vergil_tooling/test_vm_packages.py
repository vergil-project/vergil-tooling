from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from tests.vergil_tooling.tooling_install_stubs import stub_bin, write_stub
from vergil_tooling.lib import tooling_install, vm_packages
from vergil_tooling.lib.atomic_write import atomic_write_cmd
from vergil_tooling.lib.tooling_install import GIT_URL, ResolvedRef, check_lines, install_lines

if TYPE_CHECKING:
    from pathlib import Path

SHA = "a" * 40
_UV_PATH = 'export PATH="$HOME/.local/bin:$PATH"'
_FAST = (
    "-o Acquire::Retries=3 -o Acquire::Retries::Delay=false"
    " -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20"
    " -o DPkg::Lock::Timeout=60 -o APT::Update::Error-Mode=any"
)
_PURGE = f"sudo apt-get {_FAST} purge -y"
_SOURCE = "/etc/apt/sources.list.d/vergil.sources"
_KEY = "/usr/share/keyrings/vergil-archive-keyring.asc"
_PIN = "/etc/apt/preferences.d/vergil-tooling"
_DPKG = "dpkg-query -W -f=${db:Status-Abbrev} ${Package}\n"
_DEV_MARKER = "rm -f ~/.config/vergil/tooling-dev-ref"


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)


def _flat(t: MagicMock) -> list[str]:
    return [" ".join(str(a) for a in c[0]) for c in t.run.call_args_list]


def _index(flat: list[str], needle: str) -> int:
    return next(i for i, c in enumerate(flat) if needle in c)


def _script(lines: list[str]) -> str:
    return "\n".join([_UV_PATH, *lines])


def _install_script(resolved: ResolvedRef) -> str:
    return _script(install_lines(resolved, python_dir_env=None, tool_dir=None, bin_dir=None))


_CHECK = _script(check_lines("$HOME/.local/bin"))


@pytest.fixture
def ls_remote(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The host's ``git ls-remote``: v2.1.9, v2.1.10 and v2.1 on v2.1.10; records its calls."""
    calls: list[tuple[str, str]] = []

    def fake(url: str, pattern: str) -> str:
        calls.append((url, pattern))
        return f"{'9' * 40}\trefs/tags/v2.1.9\n{SHA}\trefs/tags/v2.1.10\n{SHA}\trefs/tags/v2.1\n"

    monkeypatch.setattr(tooling_install, "git_ls_remote", fake)
    return calls


class FakeVm:
    """A Transport double holding a VM's vergil packages and apt files as state.

    ``dpkg-query`` lists :attr:`packages` (name → status abbreviation), the file
    probe lists :attr:`files`, and ``apt-get purge`` / ``rm -f`` change them.
    Any command named in *fail* raises.
    """

    def __init__(
        self,
        packages: dict[str, str] | None = None,
        files: set[str] | None = None,
        *,
        fail: tuple[str, ...] = (),
        sticky: tuple[str, ...] = (),
    ) -> None:
        self.packages = dict(packages or {})
        self.files = set(files or set())
        self.fail = fail
        self.sticky = sticky
        self.t = MagicMock()
        self.t.run.side_effect = self._run
        self.t.pipe.return_value = None

    def _run(self, *args: str, **_kw: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        for needle in self.fail:
            if needle in joined:
                raise subprocess.CalledProcessError(1, list(args))
        if args[0] == "dpkg-query":
            listing = "".join(f"{s} {n}\n" for n, s in self.packages.items())
            return _ok("ii  libc6\nun  vergil-unrelated-un\nmalformed line here\n" + listing)
        if "test -e" in joined:
            return _ok("".join(f"{f}\n" for f in sorted(self.files)))
        if "purge" in args:
            for name in args[args.index("-y") + 1 :]:
                if name not in self.sticky:
                    self.packages.pop(name, None)
        elif args[:3] == ("sudo", "rm", "-f"):
            for f in args[4:]:
                if f not in self.sticky:
                    self.files.discard(f)
        return _ok()


# -- classify_ref and the pin the container cache still uses -----------------


@pytest.mark.parametrize(
    ("ref", "kind"),
    [
        ("v2.1", "line"),
        ("v2.1.226", "exact"),
        ("develop", "dev"),
        ("feature/123-x", "dev"),
        ("2.1", "dev"),
        ("v2", "dev"),
        ("v2.1.226-rc1", "dev"),
        ("v2.1.x", "dev"),
    ],
)
def test_classify(ref: str, kind: str) -> None:
    assert vm_packages.classify_ref(ref) == kind


def test_apt_pin_text() -> None:
    assert vm_packages.apt_pin("v2.1") == (
        "Package: vergil-tooling\nPin: version 2.1.*\nPin-Priority: 1001\n"
    )
    assert vm_packages.apt_pin("v2.1.226") == (
        "Package: vergil-tooling\nPin: version 2.1.226-1\nPin-Priority: 1001\n"
    )


def test_apt_pin_rejects_dev_ref() -> None:
    with pytest.raises(ValueError, match="develop"):
        vm_packages.apt_pin("develop")


# -- released_install --------------------------------------------------------


class TestReleasedInstall:
    def test_released_install_runs_tooling_install_lines(
        self, ls_remote: list[tuple[str, str]]
    ) -> None:
        t = FakeVm().t
        vm_packages.released_install(t, "v2.1")
        resolved = ResolvedRef(ref="v2.1", kind="line", tag="v2.1.10", sha=SHA)
        assert t.run.call_args_list[0][0] == ("bash", "-c", _install_script(resolved))
        install = t.run.call_args_list[0][0][2]
        assert "uv tool install --force --no-config --python 3.15 --managed-python" in install
        assert f"vt_sha={SHA}\n" in install
        assert f'git+{GIT_URL}@$vt_sha"' in install

    def test_vm_resolve_follows_demoted_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # v2.1 was demoted to v2.1.9 with vrg-promote; v2.1.10 is newer.
        listing = f"{'9' * 40}\trefs/tags/v2.1.9\n{SHA}\trefs/tags/v2.1.10\n"
        listing += f"{'9' * 40}\trefs/tags/v2.1\n"
        monkeypatch.setattr(tooling_install, "git_ls_remote", lambda _u, _p: listing)
        t = FakeVm().t
        vm_packages.released_install(t, "v2.1")
        resolved = ResolvedRef(ref="v2.1", kind="line", tag="v2.1.9", sha="9" * 40)
        assert t.run.call_args_list[0][0] == ("bash", "-c", _install_script(resolved))

    def test_line_resolves_once_on_the_host(self, ls_remote: list[tuple[str, str]]) -> None:
        vm_packages.released_install(FakeVm().t, "v2.1")
        assert ls_remote == [(GIT_URL, "refs/tags/v2.1*")]

    def test_exact_installs_its_own_commit(self, ls_remote: list[tuple[str, str]]) -> None:
        t = FakeVm().t
        vm_packages.released_install(t, "v2.1.9")
        assert f"vt_sha={'9' * 40}\n" in t.run.call_args_list[0][0][2]

    def test_released_install_checks_the_install(self, ls_remote: list[tuple[str, str]]) -> None:
        t = FakeVm().t
        vm_packages.released_install(t, "v2.1")
        assert t.run.call_args_list[1][0] == ("bash", "-c", _CHECK)

    def test_released_install_clears_dev_ref_marker(self, ls_remote: list[tuple[str, str]]) -> None:
        t = FakeVm().t
        vm_packages.released_install(t, "v2.1")
        flat = _flat(t)
        assert _index(flat, "vrg-whoami --mode") < _index(flat, _DEV_MARKER)

    def test_failed_check_keeps_the_dev_ref_marker(self, ls_remote: list[tuple[str, str]]) -> None:
        vm = FakeVm(fail=("vrg-whoami --mode",))
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.released_install(vm.t, "v2.1")
        assert not any(_DEV_MARKER in c for c in _flat(vm.t))

    def test_prints_the_logged_tag_and_sha(
        self, ls_remote: list[tuple[str, str]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        t = MagicMock()
        t.run.return_value = _ok(
            stderr=f"Resolved 30 packages\nvergil-tooling: installed v2.1.10 (commit {SHA})\n"
        )
        vm_packages.released_install(t, "v2.1")
        out = capsys.readouterr().out
        assert f"vergil-tooling: installed v2.1.10 (commit {SHA})" in out
        assert "Resolved 30 packages" not in out

    def test_unresolvable_ref_exits_and_installs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(tooling_install, "git_ls_remote", lambda _u, _p: "")
        t = FakeVm().t
        with pytest.raises(SystemExit):
            vm_packages.released_install(t, "v2.2")
        assert "no release tag for v2.2" in capsys.readouterr().err
        t.run.assert_not_called()

    def test_clears_cache_and_retries_once_on_failure(
        self, ls_remote: list[tuple[str, str]]
    ) -> None:
        t = MagicMock()
        t.run.side_effect = [subprocess.CalledProcessError(1, "uv"), _ok(), _ok(), _ok(), _ok()]
        vm_packages.released_install(t, "v2.1")
        flat = _flat(t)
        assert "uv cache clean" in flat[1]
        assert t.run.call_args_list[0] == t.run.call_args_list[2]

    def test_propagates_when_retry_also_fails(self, ls_remote: list[tuple[str, str]]) -> None:
        t = MagicMock()
        t.run.side_effect = [
            subprocess.CalledProcessError(1, "uv"),
            _ok(),
            subprocess.CalledProcessError(1, "uv"),
        ]
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.released_install(t, "v2.1")
        assert not any(_DEV_MARKER in c for c in _flat(t))

    def test_dev_ref_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="develop"):
            vm_packages.released_install(FakeVm().t, "develop")

    def test_never_touches_apt(self, ls_remote: list[tuple[str, str]]) -> None:
        t = FakeVm().t
        vm_packages.released_install(t, "v2.1")
        assert not any("apt" in c for c in _flat(t))
        t.pipe.assert_not_called()


def _guest(tmp_path: Path, *, shim: bool) -> MagicMock:
    """A Transport running guest commands in a real bash, with stub git/uv.

    ``~/.local/bin/vrg-whoami`` exists, as after a uv install. With *shim*, a
    ``vrg-whoami`` stub also sits ahead of it on ``PATH``, as a stale package
    shim would on a VM whose ``PATH`` puts it first.
    """
    home = tmp_path / "home"
    write_stub(home / ".local" / "bin", "vrg-whoami", "#!/bin/bash\nexit 0\n")
    stubs = stub_bin(tmp_path)
    (stubs / "vrg-whoami").unlink()
    shim_dir = tmp_path / "shim"
    if shim:
        write_stub(shim_dir, "vrg-whoami", "#!/bin/bash\nexit 0\n")
    log = tmp_path / "stub.log"
    log.write_text("")
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(stubs), "/usr/bin", "/bin"]),
        "STUB_LOG": str(log),
        "TMPDIR": str(tmp_path),
    }

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        if shim and argv[:2] == ["bash", "-c"]:
            # A PATH that puts the shim ahead of ~/.local/bin even after the prepend.
            argv[2] = argv[2].replace(_UV_PATH, f'{_UV_PATH}; export PATH="{shim_dir}:$PATH"', 1)
        return subprocess.run(argv, capture_output=True, text=True, check=True, env=env)  # noqa: S603

    t = MagicMock()
    t.run.side_effect = run
    return t


def test_released_install_under_bash(tmp_path: Path, ls_remote: list[tuple[str, str]]) -> None:
    t = _guest(tmp_path, shim=False)
    vm_packages.released_install(t, "v2.1")
    calls = (tmp_path / "stub.log").read_text()
    assert f"git+{GIT_URL}@{SHA}" in calls
    assert "\tls-remote\t" not in calls  # resolved on the host, once


def test_check_fails_on_a_shadowing_shim_under_bash(
    tmp_path: Path, ls_remote: list[tuple[str, str]]
) -> None:
    t = _guest(tmp_path, shim=True)
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        vm_packages.released_install(t, "v2.1")
    assert "vrg-whoami resolves to" in excinfo.value.stderr


# -- dev_install -------------------------------------------------------------


class TestDevInstall:
    def test_dev_install_runs_tooling_install_lines_and_records_ref(
        self, ls_remote: list[tuple[str, str]]
    ) -> None:
        t = FakeVm().t
        vm_packages.dev_install(t, "develop")
        resolved = ResolvedRef(ref="develop", kind="dev", tag=None, sha=None)
        assert t.run.call_args_list[0][0] == ("bash", "-c", _install_script(resolved))
        assert t.run.call_args_list[1][0] == ("bash", "-c", _CHECK)
        assert t.pipe.call_args[0] == (
            atomic_write_cmd("~/.config/vergil/tooling-dev-ref", 0o644),
            "develop\n",
        )
        assert ls_remote == []

    def test_dev_install_follows_the_interpreter_rule(
        self, ls_remote: list[tuple[str, str]]
    ) -> None:
        t = FakeVm().t
        vm_packages.dev_install(t, "feature/1-x")
        install = t.run.call_args_list[0][0][2]
        assert "unset UV_PYTHON UV_PYTHON_PREFERENCE UV_NO_MANAGED_PYTHON" in install
        assert "--reinstall" in install
        assert "--no-config --python 3.15 --managed-python" in install

    def test_failed_dev_install_records_no_marker(self, ls_remote: list[tuple[str, str]]) -> None:
        t = MagicMock()
        t.run.side_effect = subprocess.CalledProcessError(1, "uv")
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.dev_install(t, "develop")
        t.pipe.assert_not_called()

    def test_failed_check_records_no_marker(self, ls_remote: list[tuple[str, str]]) -> None:
        vm = FakeVm(fail=("vrg-whoami --mode",))
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.dev_install(vm.t, "develop")
        vm.t.pipe.assert_not_called()

    def test_dev_ref_is_rejected_for_release_versions(self) -> None:
        with pytest.raises(ValueError, match="v2.1"):
            vm_packages.dev_install(FakeVm().t, "v2.1")


class TestDevRef:
    def test_returns_recorded_ref(self) -> None:
        t = MagicMock()
        t.run.return_value = _ok("develop\n")
        assert vm_packages.dev_ref(t) == "develop"

    def test_returns_none_without_marker(self) -> None:
        t = MagicMock()
        t.run.return_value = _ok("")
        assert vm_packages.dev_ref(t) is None


def test_dev_banner_text() -> None:
    assert "not a release" in vm_packages.DEV_BANNER
    assert vm_packages.DEV_BANNER.format(ref="develop") == (
        "DEV tooling (ref develop) — dev ref, not a release"
    )


# -- cleanup_packages --------------------------------------------------------

_OLD_VM = {
    "vergil-tooling": "ii",
    "vergil-python3.15": "ii",
    "vergil-python3.14.8": "rc",
    "vergil-archive-keyring": "ii",
}


class TestCleanupPackages:
    def test_cleanup_removes_named_packages_source_keyring_and_pin(self) -> None:
        vm = FakeVm(_OLD_VM, {_SOURCE, _KEY, _PIN})
        removed = vm_packages.cleanup_packages(vm.t)
        flat = _flat(vm.t)
        assert (
            f"{_PURGE} vergil-archive-keyring vergil-python3.14.8 vergil-python3.15 vergil-tooling"
            in flat
        )
        assert f"sudo rm -f -- {_PIN} {_SOURCE} {_KEY}" in flat
        assert not any("autoremove" in c for c in flat)
        assert removed == [
            "vergil-archive-keyring",
            "vergil-python3.14.8",
            "vergil-python3.15",
            "vergil-tooling",
            _PIN,
            _SOURCE,
            _KEY,
        ]
        assert vm.packages == {}
        assert vm.files == set()

    def test_cleanup_reads_package_state_from_dpkg(self) -> None:
        vm = FakeVm()
        vm_packages.cleanup_packages(vm.t)
        assert vm.t.run.call_args_list[0][0] == (
            "dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev} ${Package}\n",
        )
        assert _DPKG in _flat(vm.t)

    def test_not_installed_and_unrelated_packages_are_left_alone(self) -> None:
        vm = FakeVm({"vergil-tooling": "un", "vergilant": "ii", "libvergil": "ii"})
        assert vm_packages.cleanup_packages(vm.t) == []
        assert not any("purge" in c for c in _flat(vm.t))

    def test_nothing_to_remove_changes_nothing(self) -> None:
        vm = FakeVm()
        assert vm_packages.cleanup_packages(vm.t) == []
        flat = _flat(vm.t)
        assert not any("purge" in c or "rm -f" in c or "vrg-whoami" in c for c in flat)

    def test_cleanup_confirms_nothing_left(self, capsys: pytest.CaptureFixture[str]) -> None:
        vm = FakeVm(_OLD_VM, {_SOURCE}, sticky=("vergil-tooling", _SOURCE))
        with pytest.raises(SystemExit):
            vm_packages.cleanup_packages(vm.t)
        err = capsys.readouterr().err
        assert "not all removed" in err
        assert "vergil-tooling" in err
        assert _SOURCE in err

    def test_failed_purge_is_fatal(self, capsys: pytest.CaptureFixture[str]) -> None:
        vm = FakeVm(_OLD_VM, fail=("purge",))
        with pytest.raises(SystemExit):
            vm_packages.cleanup_packages(vm.t)
        assert "could not remove the vergil packages" in capsys.readouterr().err

    def test_rechecks_the_uv_install_after_removing_packages(self) -> None:
        vm = FakeVm(_OLD_VM)
        vm_packages.cleanup_packages(vm.t)
        flat = _flat(vm.t)
        assert _index(flat, "purge") < _index(flat, "vrg-whoami --mode")

    def test_install_broken_by_the_removal_fails_loudly(self) -> None:
        vm = FakeVm(_OLD_VM, fail=("vrg-whoami --mode",))
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.cleanup_packages(vm.t)

    def test_files_alone_need_no_recheck(self) -> None:
        vm = FakeVm(files={_PIN})
        assert vm_packages.cleanup_packages(vm.t) == [_PIN]
        assert not any("vrg-whoami" in c for c in _flat(vm.t))

    def test_second_cleanup_removes_nothing(self) -> None:
        vm = FakeVm(_OLD_VM, {_SOURCE, _KEY, _PIN})
        vm_packages.cleanup_packages(vm.t)
        vm.t.run.reset_mock()
        assert vm_packages.cleanup_packages(vm.t) == []
        assert not any("purge" in c or "rm -f" in c for c in _flat(vm.t))


def test_file_probe_runs_under_bash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    present = tmp_path / "vergil.sources"
    present.write_text("")
    monkeypatch.setattr(vm_packages, "_APT_FILES", (str(present), str(tmp_path / "absent")))
    t = MagicMock()
    t.run.side_effect = lambda *a: subprocess.run(
        list(a), capture_output=True, text=True, check=True
    )  # noqa: S603
    assert vm_packages._vergil_apt_files(t) == [str(present)]
