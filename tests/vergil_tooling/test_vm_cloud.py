from __future__ import annotations

import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.vm_cloud import (
    bootstrap_volume,
    cloud_labels,
    cloud_resource_name,
    fetch_modules,
    preflight,
    provision_params,
    render_provision_env,
)

_RFC1035 = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


class TestCloudName:
    def test_lowercases_and_replaces_dots(self) -> None:
        name = cloud_resource_name("vergil-user", "Logical-Minds", "MQ.Cluster")
        assert _RFC1035.fullmatch(name)
        assert "." not in name and name == name.lower()

    def test_deterministic(self) -> None:
        a = cloud_resource_name("vergil-user", "o", "r")
        b = cloud_resource_name("vergil-user", "o", "r")
        assert a == b

    def test_truncates_long_names_to_59_with_hash(self) -> None:
        name = cloud_resource_name("vergil-user", "a" * 40, "b" * 40)
        assert len(name) <= 59
        assert _RFC1035.fullmatch(name)

    def test_distinct_inputs_distinct_names_even_when_truncated(self) -> None:
        n1 = cloud_resource_name("vergil-user", "a" * 40, "b" * 40)
        n2 = cloud_resource_name("vergil-user", "a" * 40, "c" * 40)
        assert n1 != n2

    def test_prefixes_non_alpha_leading_name(self) -> None:
        name = cloud_resource_name("9user", "org", "repo")
        assert _RFC1035.fullmatch(name)
        assert name.startswith("v-")

    def test_empty_components_fall_back_to_placeholder(self) -> None:
        name = cloud_resource_name("...", "...", "...")
        assert _RFC1035.fullmatch(name)


class TestCloudLabels:
    def test_structured_recovery_labels(self) -> None:
        labels = cloud_labels("vergil-audit", "org", "repo")
        assert labels == {
            "vergil-identity": "vergil-audit",
            "vergil-org": "org",
            "vergil-repo": "repo",
        }


class TestFetchModules:
    def test_rejects_bad_tag(self) -> None:
        with pytest.raises(SystemExit):
            fetch_modules("not-a-tag")

    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_builds_release_asset_url(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"not-a-tarball"
        with pytest.raises(SystemExit):  # tar extraction of fake bytes fails loudly
            fetch_modules("v2.1.50")
        url = mock_urlopen.call_args[0][0]
        assert url == (
            "https://github.com/vergil-project/vergil-vm/releases/download/"
            "v2.1.50/opentofu-modules-2.1.50.tar.gz"
        )

    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_strips_leading_v_for_two_segment_tag(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"not-a-tarball"
        with pytest.raises(SystemExit):
            fetch_modules("v2.2")
        url = mock_urlopen.call_args[0][0]
        assert url == (
            "https://github.com/vergil-project/vergil-vm/releases/download/"
            "v2.2/opentofu-modules-2.2.tar.gz"
        )

    @patch("vergil_tooling.lib.vm_cloud.tarfile.open")
    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_returns_modules_path_when_present(
        self, mock_urlopen: MagicMock, mock_taropen: MagicMock, tmp_path: Path
    ) -> None:
        resp = MagicMock()
        resp.read.return_value = b"data"
        mock_urlopen.return_value.__enter__.return_value = resp

        created: dict[str, Path] = {}

        class _FakeTar:
            def __enter__(self) -> _FakeTar:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def extractall(self, dest: str, filter: str) -> None:  # noqa: A002
                modules = Path(dest) / "opentofu" / "modules"
                modules.mkdir(parents=True)
                created["modules"] = modules

        mock_taropen.return_value = _FakeTar()
        result = fetch_modules("v2.1.50")
        assert result == created["modules"]
        assert result.is_dir()

    @patch("vergil_tooling.lib.vm_cloud.tarfile.open")
    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_missing_modules_dir_aborts(
        self, mock_urlopen: MagicMock, mock_taropen: MagicMock
    ) -> None:
        resp = MagicMock()
        resp.read.return_value = b"data"
        mock_urlopen.return_value.__enter__.return_value = resp

        class _EmptyTar:
            def __enter__(self) -> _EmptyTar:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def extractall(self, dest: str, filter: str) -> None:  # noqa: A002
                return None

        mock_taropen.return_value = _EmptyTar()
        with pytest.raises(SystemExit):
            fetch_modules("v2.1.50")


class TestProvisionParams:
    def test_assembles_all_encodings(self) -> None:
        params = provision_params(
            packages=["git", "vim"],
            apt_repos=[
                {
                    "name": "n",
                    "key_url": "k",
                    "uri": "u",
                    "suite": "s",
                    "components": "c",
                }
            ],
            vagrant_plugins=["p1", "p2"],
            port_forwards=["8080|host:80", "9090|host:90"],
            nested=True,
            fingerprint="abc",
        )
        assert params == {
            "EXTRA_PACKAGES": "git vim",
            "APT_REPOS": "n|k|u|s|c",
            "VAGRANT_PLUGINS": "p1 p2",
            "PORT_FORWARDS": "8080|host:80;9090|host:90",
            "NESTED_VIRT": "true",
            "SPEC_FINGERPRINT": "abc",
        }

    def test_omits_unset_pieces(self) -> None:
        params = provision_params()
        assert params == {}

    def test_multiple_apt_repos_joined_by_semicolon(self) -> None:
        params = provision_params(
            apt_repos=[
                {"name": "a", "key_url": "k1", "uri": "u1", "suite": "s1", "components": "c1"},
                {"name": "b", "key_url": "k2", "uri": "u2", "suite": "s2", "components": "c2"},
            ]
        )
        assert params["APT_REPOS"] == "a|k1|u1|s1|c1;b|k2|u2|s2|c2"


class TestProvisionEnv:
    def test_renders_key_value_body(self) -> None:
        params = {"EXTRA_PACKAGES": "git vim", "NESTED_VIRT": "true", "SPEC_FINGERPRINT": "abc"}
        body = render_provision_env(params, vergil_user="vergil", home="/home/vergil")
        lines = set(body.splitlines())
        assert "EXTRA_PACKAGES=git vim" in lines
        assert "NESTED_VIRT=true" in lines
        assert "SPEC_FINGERPRINT=abc" in lines
        assert "VERGIL_USER=vergil" in lines
        assert "HOME=/home/vergil" in lines


class TestPreflight:
    @patch("vergil_tooling.lib.vm_cloud.shutil.which", return_value=None)
    def test_missing_tofu_aborts_with_remediation(
        self, _which: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit):
            preflight()
        assert "OpenTofu" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_cloud.subprocess.run")
    @patch("vergil_tooling.lib.vm_cloud.shutil.which")
    def test_old_tofu_aborts(
        self, mock_which: MagicMock, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_which.return_value = "/usr/bin/tofu"
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout='{"terraform_version": "1.7.0"}', stderr=""
        )
        with pytest.raises(SystemExit):
            preflight()
        assert "OpenTofu" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_cloud.subprocess.run")
    @patch("vergil_tooling.lib.vm_cloud.shutil.which")
    def test_unparseable_tofu_version_aborts(
        self, mock_which: MagicMock, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_which.return_value = "/usr/bin/tofu"
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="not-json", stderr="")
        with pytest.raises(SystemExit):
            preflight()
        assert "OpenTofu" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_cloud.subprocess.run")
    @patch("vergil_tooling.lib.vm_cloud.shutil.which")
    def test_tofu_version_query_failure_aborts(
        self, mock_which: MagicMock, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_which.return_value = "/usr/bin/tofu"
        mock_run.side_effect = subprocess.CalledProcessError(1, "tofu")
        with pytest.raises(SystemExit):
            preflight()
        assert "OpenTofu" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_cloud.subprocess.run")
    @patch("vergil_tooling.lib.vm_cloud.shutil.which")
    def test_missing_gcloud_aborts(
        self, mock_which: MagicMock, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_which.side_effect = lambda name: "/usr/bin/tofu" if name == "tofu" else None
        mock_run.return_value = subprocess.CompletedProcess(
            [], 0, stdout='{"terraform_version": "1.8.0"}', stderr=""
        )
        with pytest.raises(SystemExit):
            preflight()
        assert "gcloud CLI" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_cloud.subprocess.run")
    @patch("vergil_tooling.lib.vm_cloud.shutil.which")
    def test_missing_adc_aborts(
        self, mock_which: MagicMock, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_which.return_value = "/usr/bin/x"

        def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "tofu":
                return subprocess.CompletedProcess(
                    [], 0, stdout='{"terraform_version": "1.9.0"}', stderr=""
                )
            raise subprocess.CalledProcessError(1, "gcloud")

        mock_run.side_effect = _run
        with pytest.raises(SystemExit):
            preflight()
        assert "application-default login" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_cloud.subprocess.run")
    @patch("vergil_tooling.lib.vm_cloud.shutil.which")
    def test_all_present_passes(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_which.return_value = "/usr/bin/x"

        def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "tofu":
                return subprocess.CompletedProcess(
                    [], 0, stdout='{"terraform_version": "1.8.0"}', stderr=""
                )
            return subprocess.CompletedProcess([], 0, stdout="token", stderr="")

        mock_run.side_effect = _run
        preflight()  # no raise


class TestBootstrap:
    def test_skips_for_credential_less_identity(self, capsys: pytest.CaptureFixture[str]) -> None:
        transport = MagicMock()
        identity = MagicMock()
        identity.auth_type = "none"
        bootstrap_volume(transport, identity, "org", "repo")
        transport.run.assert_not_called()
        assert "skipping checkout" in capsys.readouterr().out.lower()

    def test_clones_when_absent(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            subprocess.CalledProcessError(1, "test"),  # path absent
            MagicMock(),  # clone
            MagicMock(),  # mkdir
        ]
        identity = MagicMock()
        identity.auth_type = "app"
        bootstrap_volume(transport, identity, "org", "repo")
        cloned = " ".join(c for call in transport.run.call_args_list for c in call.args)
        assert "clone" in cloned
        assert "https://github.com/org/repo.git" in cloned
        assert "/vergil/projects/org/repo" in cloned
        assert "/vergil/claude" in cloned

    def test_fetches_when_present(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            MagicMock(),  # test -d succeeds (present)
            MagicMock(),  # fetch
        ]
        identity = MagicMock()
        identity.auth_type = "app"
        bootstrap_volume(transport, identity, "org", "repo")
        cmds = [list(call.args) for call in transport.run.call_args_list]
        assert ["git", "-C", "/vergil/projects/org/repo", "fetch", "--all"] in cmds
