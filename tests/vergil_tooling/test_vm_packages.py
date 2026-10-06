from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from vergil_tooling.lib import vm_packages
from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from vergil_tooling.lib.package.orgs import OrgRepo

_REPO_URL = "https://vergil-project.github.io/packages"


def _transport_answering(answers: dict[str, str]) -> MagicMock:
    """A Transport double answering ``run`` by substring of the joined argv."""

    def run(*args: str, **_kw: Any) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        stdout = next((v for k, v in answers.items() if k in joined), "")
        return subprocess.CompletedProcess(list(args), 0, stdout=stdout, stderr="")

    t = MagicMock()
    t.run.side_effect = run
    t.pipe.return_value = None
    return t


def _flat(t: MagicMock) -> list[str]:
    return [" ".join(str(a) for a in c[0]) for c in t.run.call_args_list]


def _piped(t: MagicMock) -> list[str]:
    return [str(c[0][0]) for c in t.pipe.call_args_list]


def _index(flat: list[str], needle: str) -> int:
    return next(i for i, c in enumerate(flat) if needle in c)


_NOBLE = "NAME=Ubuntu\nVERSION_CODENAME=noble\nID=ubuntu\n"


def _policy(candidate: str) -> str:
    return f"vergil-tooling:\n  Installed: (none)\n  Candidate: {candidate}\n"


@pytest.fixture
def boot(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def fake(run: Any, org: OrgRepo, fmt: str, suite: str, *, sudo: bool) -> None:
        calls.append((run, org, fmt, suite, sudo))

    monkeypatch.setattr(vm_packages.repo_setup, "bootstrap", fake)
    return calls


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


class TestPackagedInstall:
    def test_packaged_install_sequence(self, boot: list[tuple[Any, ...]]) -> None:
        t = _transport_answering(
            {
                "cat /etc/os-release": _NOBLE,
                "apt-cache policy vergil-tooling": _policy("2.1.240-1"),
                "uv tool list": "",
            }
        )
        vm_packages.packaged_install(t, "v2.1")
        flat = _flat(t)
        assert len(boot) == 1
        run, org, fmt, suite, sudo = boot[0]
        assert run is t.run
        assert org.vendor == "vergil"
        assert (fmt, suite, sudo) == ("deb", "noble", True)
        assert _piped(t) == ["sudo tee /etc/apt/preferences.d/vergil-tooling >/dev/null"]
        assert t.pipe.call_args[0][1] == vm_packages.apt_pin("v2.1")
        assert "sudo apt-get install -y --allow-downgrades vergil-tooling" in flat
        # Order: pin, refresh, candidate check, install.
        assert (
            _index(flat, "sudo apt-get update")
            < _index(flat, "apt-cache policy")
            < _index(flat, "sudo apt-get install")
        )
        # No legacy copy listed: nothing to uninstall, the dev marker is still cleared.
        assert not any("uv tool uninstall" in c for c in flat)
        assert any("rm -f ~/.config/vergil/tooling-dev-ref" in c for c in flat)

    def test_exact_installs_exact_version(self, boot: list[tuple[Any, ...]]) -> None:
        t = _transport_answering(
            {
                "cat /etc/os-release": _NOBLE,
                "apt-cache policy vergil-tooling": _policy("2.1.226-1"),
            }
        )
        vm_packages.packaged_install(t, "v2.1.226")
        flat = _flat(t)
        assert "sudo apt-get install -y --allow-downgrades vergil-tooling=2.1.226-1" in flat
        assert t.pipe.call_args[0][1] == vm_packages.apt_pin("v2.1.226")
        assert boot

    def test_line_with_no_candidate_fails_with_actionable_message(
        self, boot: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Review Focus 3: a line with no published package fails, naming line and repo."""
        t = _transport_answering(
            {
                "cat /etc/os-release": _NOBLE,
                "apt-cache policy vergil-tooling": _policy("(none)"),
                "uv tool list": "vergil-tooling v2.1.200\n- vrg-git\n",
            }
        )
        with pytest.raises(SystemExit) as exc:
            vm_packages.packaged_install(t, "v2.2")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "v2.2" in err
        assert _REPO_URL in err
        assert "--tag" in err
        flat = _flat(t)
        # Nothing installed, and the existing tooling is left in place.
        assert not any("--allow-downgrades" in c for c in flat)
        assert not any("uv tool uninstall" in c for c in flat)
        assert not any("tooling-dev-ref" in c for c in flat)
        assert boot

    def test_line_whose_only_candidate_is_another_line_fails(
        self, boot: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The pin only raises the priority of matching versions; a non-matching
        # newest version would still be the candidate. It must not be installed.
        t = _transport_answering(
            {
                "cat /etc/os-release": _NOBLE,
                "apt-cache policy vergil-tooling": _policy("2.1.240-1"),
            }
        )
        with pytest.raises(SystemExit):
            vm_packages.packaged_install(t, "v2.2")
        assert not any("--allow-downgrades" in c for c in _flat(t))
        err = capsys.readouterr().err
        assert "v2.2" in err
        assert _REPO_URL in err
        assert boot

    def test_exact_with_other_candidate_fails(
        self, boot: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        t = _transport_answering(
            {
                "cat /etc/os-release": _NOBLE,
                "apt-cache policy vergil-tooling": _policy("2.1.240-1"),
            }
        )
        with pytest.raises(SystemExit):
            vm_packages.packaged_install(t, "v2.1.226")
        assert not any("--allow-downgrades" in c for c in _flat(t))
        assert "v2.1.226" in capsys.readouterr().err
        assert boot

    def test_policy_without_candidate_line_fails(
        self, boot: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        # apt-cache policy prints nothing for a package no source knows about.
        t = _transport_answering({"cat /etc/os-release": _NOBLE})
        with pytest.raises(SystemExit):
            vm_packages.packaged_install(t, "v2.1")
        assert _REPO_URL in capsys.readouterr().err
        assert boot

    def test_packaged_install_removes_legacy_uv_install(self, boot: list[tuple[Any, ...]]) -> None:
        """Review Focus 5: the shadowing uv copy goes once the apt install succeeds."""
        t = _transport_answering(
            {
                "cat /etc/os-release": _NOBLE,
                "apt-cache policy vergil-tooling": "  Candidate: 2.1.240-1\n",
                "uv tool list": "vergil-tooling v2.1.200\n- vrg-git\n",
            }
        )
        vm_packages.packaged_install(t, "v2.1")
        flat = _flat(t)
        assert any("uv tool uninstall vergil-tooling" in c for c in flat)
        assert any("rm -f ~/.config/vergil/tooling-dev-ref" in c for c in flat)
        # Only after a successful apt install, so a failed packaged install
        # never leaves the VM without tooling.
        install = _index(flat, "sudo apt-get install -y --allow-downgrades vergil-tooling")
        assert install < _index(flat, "uv tool uninstall vergil-tooling")
        assert install < _index(flat, "rm -f ~/.config/vergil/tooling-dev-ref")
        assert boot

    def test_failed_apt_install_keeps_legacy_uv_install(self, boot: list[tuple[Any, ...]]) -> None:
        answers = {
            "cat /etc/os-release": _NOBLE,
            "apt-cache policy vergil-tooling": _policy("2.1.240-1"),
            "uv tool list": "vergil-tooling v2.1.200\n",
        }
        base = _transport_answering(answers).run.side_effect

        def run(*args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
            if args[:3] == ("sudo", "apt-get", "install"):
                raise subprocess.CalledProcessError(100, list(args))
            return base(*args, **kw)

        t = _transport_answering(answers)
        t.run.side_effect = run
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.packaged_install(t, "v2.1")
        flat = _flat(t)
        assert not any("uv tool uninstall" in c for c in flat)
        assert not any("tooling-dev-ref" in c for c in flat)
        assert boot

    def test_unrelated_uv_tool_is_not_uninstalled(self, boot: list[tuple[Any, ...]]) -> None:
        t = _transport_answering(
            {
                "cat /etc/os-release": _NOBLE,
                "apt-cache policy vergil-tooling": _policy("2.1.240-1"),
                "uv tool list": "vergil-tooling-extras v1.0\nruff v0.1\n",
            }
        )
        vm_packages.packaged_install(t, "v2.1")
        assert not any("uv tool uninstall" in c for c in _flat(t))
        assert boot

    def test_unsupported_codename_is_fatal(
        self, boot: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        t = _transport_answering({"cat /etc/os-release": "VERSION_CODENAME=jammy\n"})
        with pytest.raises(SystemExit) as exc:
            vm_packages.packaged_install(t, "v2.1")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "jammy" in err
        assert "noble, resolute" in err
        assert boot == []
        t.pipe.assert_not_called()

    def test_missing_codename_is_fatal(
        self, boot: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        t = _transport_answering({"cat /etc/os-release": "NAME=Something\n"})
        with pytest.raises(SystemExit):
            vm_packages.packaged_install(t, "v2.1")
        assert "(unknown)" in capsys.readouterr().err
        assert boot == []

    def test_quoted_codename_is_accepted(self, boot: list[tuple[Any, ...]]) -> None:
        t = _transport_answering(
            {
                "cat /etc/os-release": 'VERSION_CODENAME="resolute"\n',
                "apt-cache policy vergil-tooling": _policy("2.1.240-1"),
            }
        )
        vm_packages.packaged_install(t, "v2.1")
        assert boot[0][3] == "resolute"

    def test_bootstrap_failure_is_fatal(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def fail(*_a: Any, **_k: Any) -> None:
            msg = "vergil package key fingerprint mismatch"
            raise PackageError(msg)

        monkeypatch.setattr(vm_packages.repo_setup, "bootstrap", fail)
        t = _transport_answering({"cat /etc/os-release": _NOBLE})
        with pytest.raises(SystemExit) as exc:
            vm_packages.packaged_install(t, "v2.1")
        assert exc.value.code == 1
        assert "fingerprint mismatch" in capsys.readouterr().err
        t.pipe.assert_not_called()

    def test_dev_ref_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="develop"):
            vm_packages.packaged_install(_transport_answering({}), "develop")


class TestDevInstall:
    def test_dev_install_records_ref_and_uses_uv(self) -> None:
        t = _transport_answering({})
        vm_packages.dev_install(t, "develop")
        flat = _flat(t)
        assert any(
            "uv tool install" in c
            and "git+https://github.com/vergil-project/vergil-tooling@develop" in c
            for c in flat
        )
        assert _piped(t) == ["cat > ~/.config/vergil/tooling-dev-ref"]
        assert t.pipe.call_args[0][1] == "develop\n"
        assert _index(flat, "uv tool install") < _index(flat, "mkdir -p")

    def test_dev_install_reinstalls(self) -> None:
        t = _transport_answering({})
        vm_packages.dev_install(t, "feature/1-x")
        install = next(c for c in _flat(t) if "uv tool install" in c)
        assert "--reinstall" in install
        assert "--force" not in install

    def test_failed_dev_install_records_no_marker(self) -> None:
        t = _transport_answering({})
        t.run.side_effect = subprocess.CalledProcessError(1, "uv")
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.dev_install(t, "develop")
        t.pipe.assert_not_called()

    def test_clears_cache_and_retries_with_force_on_failure(self) -> None:
        t = _transport_answering({})
        t.run.side_effect = [
            subprocess.CalledProcessError(1, "uv"),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        vm_packages.dev_install(t, "develop")
        flat = _flat(t)
        assert any("uv cache clean" in c for c in flat)
        installs = [c for c in flat if "uv tool install" in c]
        assert len(installs) == 2
        assert "--force" not in installs[0]
        assert "--force" in installs[1]

    def test_propagates_when_retry_also_fails(self) -> None:
        t = _transport_answering({})
        t.run.side_effect = [
            subprocess.CalledProcessError(1, "uv"),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CalledProcessError(1, "uv"),
        ]
        with pytest.raises(subprocess.CalledProcessError):
            vm_packages.dev_install(t, "develop")
        t.pipe.assert_not_called()

    def test_dev_ref_is_rejected_for_release_versions(self) -> None:
        with pytest.raises(ValueError, match="v2.1"):
            vm_packages.dev_install(_transport_answering({}), "v2.1")


class TestDevRef:
    def test_returns_recorded_ref(self) -> None:
        t = _transport_answering({"tooling-dev-ref": "develop\n"})
        assert vm_packages.dev_ref(t) == "develop"

    def test_returns_none_without_marker(self) -> None:
        assert vm_packages.dev_ref(_transport_answering({})) is None


def test_dev_banner() -> None:
    assert vm_packages.DEV_BANNER.format(ref="develop") == (
        "DEV tooling (ref develop) — not the packaged install"
    )
