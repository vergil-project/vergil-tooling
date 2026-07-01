from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import urllib.error
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import vm_cloud
from vergil_tooling.lib.vm_cloud import (
    FALLBACK_SHAPES,
    NESTED_VIRT_FAMILIES,
    OffPlatformBackend,
    _azure_resource_group_from_volume_id,
    apply_vm,
    apply_vm_with_zone_fallback,
    apply_volume,
    await_readiness,
    bootstrap_volume,
    cloud_labels,
    cloud_resource_name,
    destroy_vm,
    destroy_volume,
    ensure_keypair,
    fetch_modules,
    instance_fallback_candidates,
    is_zone_capacity_error,
    link_cloud_claude_dirs,
    nsg_refresh,
    off_platform_transport,
    parse_vm_machine_type,
    parse_volume_state,
    preflight,
    provision_params,
    read_host,
    read_volume_id,
    read_zone,
    region_zones,
    render_provision_env,
    tofu_state_dir,
    zone_to_region,
)
from vergil_tooling.lib.vm_spec import ComposedSpec, spec_fingerprint, state_slug
from vergil_tooling.lib.vm_transport import IapTransport, SshTransport

_RFC1035 = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


@pytest.fixture(autouse=True)
def _default_gcp_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """The off-platform tofu env resolves the GCP project; default it so tests that
    exercise tofu don't shell out to ``gcloud``. The _resolve_project tests clear it."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")


class TestResolveProject:
    def test_uses_env_when_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "from-env")
        assert vm_cloud._resolve_project() == "from-env"

    def test_falls_back_to_gcloud_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        sub = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="from-gcloud\n"))
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        assert vm_cloud._resolve_project() == "from-gcloud"

    def test_aborts_when_no_project(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        sub = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="\n"))
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        with pytest.raises(SystemExit):
            vm_cloud._resolve_project()


class TestCloudName:
    def test_rfc1035_and_fixed_length(self) -> None:
        slug = state_slug("vergil-user", "Logical-Minds", "MQ.Cluster")
        name = cloud_resource_name(slug)
        assert _RFC1035.fullmatch(name)
        assert len(name) == 16  # "vrg-" + 12 hex

    def test_deterministic(self) -> None:
        slug = state_slug("vergil-user", "o", "r")
        a = cloud_resource_name(slug)
        b = cloud_resource_name(slug)
        assert a == b

    def test_always_fits_gcp_limit(self) -> None:
        slug = state_slug("vergil-user", "a" * 40, "b" * 40)
        name = cloud_resource_name(slug)
        # 16 chars always fits within GCP's 63-char cap (and its "-data" suffix).
        assert len(name) == 16
        assert _RFC1035.fullmatch(name)

    def test_distinct_slugs_produce_distinct_names(self) -> None:
        n1 = cloud_resource_name(state_slug("vergil-user", "a" * 40, "b" * 40))
        n2 = cloud_resource_name(state_slug("vergil-user", "a" * 40, "c" * 40))
        assert n1 != n2

    def test_always_starts_with_vrg(self) -> None:
        # Hash prefix "vrg-" guarantees RFC1035-valid first char regardless of input.
        name = cloud_resource_name(state_slug("9user", "org", "repo"))
        assert name.startswith("vrg-")
        assert _RFC1035.fullmatch(name)

    def test_any_slug_produces_valid_name(self) -> None:
        name = cloud_resource_name(state_slug("...", "...", "..."))
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
    def test_builds_archive_url(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"not-a-tarball"
        with pytest.raises(SystemExit):  # tar extraction of fake bytes fails loudly
            fetch_modules("v2.1.50")
        url = mock_urlopen.call_args[0][0]
        assert url == "https://github.com/vergil-project/vergil-vm/archive/refs/tags/v2.1.50.tar.gz"

    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_builds_archive_url_two_segment_tag(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value.__enter__.return_value.read.return_value = b"not-a-tarball"
        with pytest.raises(SystemExit):
            fetch_modules("v2.2")
        url = mock_urlopen.call_args[0][0]
        assert url == "https://github.com/vergil-project/vergil-vm/archive/refs/tags/v2.2.tar.gz"

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
                # GitHub's source archive roots at vergil-vm-<ref>/, not opentofu/ at the top.
                modules = Path(dest) / "vergil-vm-2.1.50" / "opentofu" / "modules"
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
    # The keys every provision/*.sh sources under `set -u` — must always be defined.
    _CANONICAL_KEYS = frozenset(
        {
            "EXTRA_PACKAGES",
            "APT_REPOS",
            "VAGRANT_PLUGINS",
            "SPEC_FINGERPRINT",
            "NESTED_VIRT",
            "PORT_FORWARDS",
        }
    )

    def test_renders_key_value_body(self) -> None:
        params = {"EXTRA_PACKAGES": "git vim", "NESTED_VIRT": "true", "SPEC_FINGERPRINT": "abc"}
        body = render_provision_env(params, vergil_user="vergil", home="/home/vergil")
        lines = set(body.splitlines())
        # values are shell-quoted; a multi-token value gets single-quoted (#1805)
        assert "EXTRA_PACKAGES='git vim'" in lines
        assert "NESTED_VIRT=true" in lines
        assert "SPEC_FINGERPRINT=abc" in lines
        assert "VERGIL_USER=vergil" in lines
        assert "HOME=/home/vergil" in lines

    def test_minimal_params_still_define_every_canonical_key(self) -> None:
        # Regression (#1768): a minimal spec must NOT leave keys undefined, or each
        # provision script aborts on `unbound variable` under `set -u`.
        body = render_provision_env({}, vergil_user="vergil", home="/home/vergil")
        defined = {line.split("=", 1)[0] for line in body.splitlines()}
        assert defined >= self._CANONICAL_KEYS
        # unset keys default to a shell-quoted empty string
        assert "NESTED_VIRT=''" in body.splitlines()
        assert "APT_REPOS=''" in body.splitlines()

    def test_params_override_empty_defaults(self) -> None:
        body = render_provision_env(
            {"NESTED_VIRT": "true"}, vergil_user="vergil", home="/home/vergil"
        )
        lines = body.splitlines()
        assert "NESTED_VIRT=true" in lines
        assert "NESTED_VIRT=''" not in lines  # the default did not leak a duplicate

    def test_sources_cleanly_with_multi_token_values(self) -> None:
        # Regression (#1805): provision scripts do `. provision.env`, so values with
        # spaces / | / ; / : must round-trip as plain assignments, not `VAR=x cmd` lines.
        params = {
            "EXTRA_PACKAGES": "bridge-utils qemu-system-x86 libvirt-clients",
            "APT_REPOS": "hashicorp|https://k.example/k.gpg|https://apt.example|noble|main",
            "PORT_FORWARDS": "3000|10.50.0.2:3000;8080|10.50.0.2:8080",
            "VAGRANT_PLUGINS": "vagrant-libvirt",
        }
        body = render_provision_env(params, vergil_user="ubuntu", home="/home/ubuntu")
        # Source the rendered body under `set -eu` and echo each var back: with the old
        # unquoted output this aborts (`command not found`); quoted, it round-trips exactly.
        script = (
            f"set -eu\n{body}\n"
            'printf "%s\\n" "$EXTRA_PACKAGES" "$APT_REPOS" "$PORT_FORWARDS"'
            ' "$VAGRANT_PLUGINS" "$NESTED_VIRT"'
        )
        out = subprocess.run(  # noqa: S603
            ["bash", "-c", script],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        assert out == [
            "bridge-utils qemu-system-x86 libvirt-clients",
            "hashicorp|https://k.example/k.gpg|https://apt.example|noble|main",
            "3000|10.50.0.2:3000;8080|10.50.0.2:8080",
            "vagrant-libvirt",
            "",  # NESTED_VIRT default — empty
        ]


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
        # reattach fetches via vrg-git (token injection), run inside the repo so the
        # org resolves from its own remote.
        fetch_call = transport.run.call_args_list[-1]
        assert list(fetch_call.args) == ["vrg-git", "fetch", "--all"]
        assert fetch_call.kwargs["workdir"] == "/vergil/projects/org/repo"


def _done(stdout: str = "status: done\n") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class TestParseCloudInitStatus:
    def test_extracts_status_and_detail(self) -> None:
        out = "----\nstatus: running\ndetail: running modules:config\nboot: enabled\n"
        assert vm_cloud._parse_cloud_init_status(out) == ("running", "running modules:config")

    def test_status_only_leaves_detail_empty(self) -> None:
        assert vm_cloud._parse_cloud_init_status("status: done\n") == ("done", "")

    def test_no_status_line_is_empty(self) -> None:
        # A bare SSH/connection failure prints no status line at all.
        assert vm_cloud._parse_cloud_init_status("ssh: connect: connection refused\n") == ("", "")


class TestIsConnectionFailure:
    def test_ssh_exit_255_is_a_connection_failure(self) -> None:
        exc = subprocess.CalledProcessError(255, "ssh")
        assert vm_cloud._is_connection_failure(exc) is True

    def test_cloud_init_error_exit_is_not_a_connection_failure(self) -> None:
        # cloud-init's own error(1)/degraded(2) exits are terminal faults, not
        # transport-connect failures.
        assert vm_cloud._is_connection_failure(subprocess.CalledProcessError(1, "x")) is False
        assert vm_cloud._is_connection_failure(subprocess.CalledProcessError(2, "x")) is False


class TestWaitForSsh:
    def test_returns_once_a_trivial_command_succeeds(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _done("")
        vm_cloud._wait_for_ssh(transport)  # no raise
        # The probe is a quiet trivial command, so a connect-race error is not
        # echoed as misleading noise.
        assert transport.run.call_args.kwargs.get("quiet") is True

    def test_retries_through_connect_failures_then_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The box's sshd is not up at the first probe (IAP 4003 / ssh 255); the
        # wait must retry rather than fail, then return once it answers.
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.time.sleep", lambda _s: None)
        emitted: list[str] = []
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.emit", emitted.append)
        transport = MagicMock()
        transport.run.side_effect = [
            subprocess.CalledProcessError(255, "ssh"),
            subprocess.CalledProcessError(255, "ssh"),
            _done(""),
        ]
        vm_cloud._wait_for_ssh(transport)  # no raise
        assert transport.run.call_count == 3
        assert any("waiting for SSH" in line for line in emitted)

    def test_raises_after_bounded_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # sshd never comes up: bounded by the timeout, the wait raises a message
        # distinct from a cloud-init fault ("never became reachable").
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.time.sleep", lambda _s: None)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.emit", lambda _line: None)
        clock = iter([0.0, 100.0, 250.0])
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.time.monotonic", lambda: next(clock))
        transport = MagicMock()
        transport.run.side_effect = subprocess.CalledProcessError(255, "ssh")
        with pytest.raises(RuntimeError, match="never became reachable"):
            vm_cloud._wait_for_ssh(transport, timeout_secs=200.0)


class TestPollCloudInitStatus:
    def test_parses_successful_call(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _done("status: running\ndetail: foo\n")
        assert vm_cloud._poll_cloud_init_status(transport) == ("running", "foo")

    def test_parses_status_from_nonzero_exit(self) -> None:
        # cloud-init exits 1/2 for error/degraded but still prints the status line.
        transport = MagicMock()
        transport.run.side_effect = subprocess.CalledProcessError(
            1, "cloud-init", output="status: error\ndetail: boom\n"
        )
        assert vm_cloud._poll_cloud_init_status(transport) == ("error", "boom")

    def test_connection_failure_yields_empty_status(self) -> None:
        # ssh exit 255 (IAP connect failure) is distinguished from a cloud-init
        # fault by its return code, not by parsing stdout: it yields no status so
        # the poll loop keeps waiting.
        transport = MagicMock()
        transport.run.side_effect = subprocess.CalledProcessError(255, "ssh")
        assert vm_cloud._poll_cloud_init_status(transport) == ("", "")

    def test_polls_quietly(self) -> None:
        # The poll runs quietly so a transient connect drop mid-provision does
        # not spam the operator with a raw IAP error.
        transport = MagicMock()
        transport.run.return_value = _done("status: done\n")
        vm_cloud._poll_cloud_init_status(transport)
        assert transport.run.call_args.kwargs.get("quiet") is True


class TestAwaitReadiness:
    @pytest.fixture(autouse=True)
    def _skip_ssh_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # _wait_for_ssh is exercised directly in TestWaitForSsh; stub it here so
        # these cloud-init/marker tests need not thread an SSH probe through the
        # transport.run side-effect lists.
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud._wait_for_ssh", lambda *a, **k: None)

    def test_waits_for_ssh_before_polling_cloud_init(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The readiness gate must confirm SSH reachability before it probes
        # cloud-init, so it never conflates a boot race with a provisioning fault.
        calls: list[str] = []
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud._wait_for_ssh",
            lambda *a, **k: calls.append("ssh"),
        )
        transport = MagicMock()

        def _record_run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(f"run:{args[0]}")
            return _done("status: done\n") if args[0] == "cloud-init" else _done("fp123\n")

        transport.run.side_effect = _record_run
        await_readiness(transport, "fp123")
        assert calls[0] == "ssh"
        assert "run:cloud-init" in calls
        assert calls.index("ssh") < calls.index("run:cloud-init")

    def test_passes_when_cloud_init_done_and_marker_matches(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            _done("status: done\n"),
            _done("fp123\n"),
        ]
        await_readiness(transport, "fp123")  # no raise

    def test_raises_when_cloud_init_reports_error(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _done("status: error\ndetail: provision failed\n")
        with pytest.raises(RuntimeError, match="error"):
            await_readiness(transport, "fp123")

    def test_raises_when_cloud_init_degraded(self) -> None:
        transport = MagicMock()
        transport.run.return_value = _done("status: degraded done\n")
        with pytest.raises(RuntimeError, match="degraded done"):
            await_readiness(transport, "fp123")

    def test_polls_through_running_then_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two "running" polls (each emits a heartbeat) before "done", then marker.
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.time.sleep", lambda _s: None)
        emitted: list[str] = []
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.emit", emitted.append)
        transport = MagicMock()
        transport.run.side_effect = [
            _done("status: running\ndetail: config\n"),
            _done("status: running\ndetail: final\n"),
            _done("status: done\n"),
            _done("fp123\n"),
        ]
        await_readiness(transport, "fp123")
        beats = [line for line in emitted if line.startswith("[cloud-init]")]
        assert len(beats) == 2
        assert "elapsed" in beats[0]
        assert "config" in beats[0]

    def test_tolerates_transient_connection_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An early SSH failure (no status) must not abort — keep polling to done.
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.time.sleep", lambda _s: None)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.emit", lambda _line: None)
        transport = MagicMock()
        transport.run.side_effect = [
            subprocess.CalledProcessError(255, "ssh"),
            _done("status: done\n"),
            _done("fp123\n"),
        ]
        await_readiness(transport, "fp123")  # no raise

    def test_raises_when_marker_mismatched(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            _done("status: done\n"),
            _done("different\n"),
        ]
        with pytest.raises(RuntimeError):
            await_readiness(transport, "fp123")

    def test_raises_when_marker_read_fails(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            _done("status: done\n"),
            subprocess.CalledProcessError(1, "cat"),
        ]
        with pytest.raises(RuntimeError):
            await_readiness(transport, "fp123")

    def test_verbose_streams_log_tail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # verbose=True attaches a live tail; its lines are relayed and it is
        # terminated once cloud-init is done.
        emitted: list[str] = []
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.emit", emitted.append)
        transport = MagicMock()
        transport.run.side_effect = [
            _done("status: done\n"),
            _done("fp123\n"),
        ]
        fake_proc = MagicMock()
        # A blank line in the stream is skipped (not relayed as an empty beat).
        fake_proc.stdout = iter(["Cloud-init running module foo\n", "\n", "package installed\n"])
        transport.popen.return_value = fake_proc
        await_readiness(transport, "fp123", verbose=True)
        popen_args = list(transport.popen.call_args.args)
        assert "tail" in popen_args
        assert vm_cloud._CLOUD_INIT_LOG in popen_args
        assert "[cloud-init] Cloud-init running module foo" in emitted
        fake_proc.terminate.assert_called_once()

    def test_non_verbose_does_not_tail(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            _done("status: done\n"),
            _done("fp123\n"),
        ]
        await_readiness(transport, "fp123")
        transport.popen.assert_not_called()

    def test_verbose_tail_failure_degrades_to_heartbeat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If spawning the tail tunnel fails, the gate must still complete on the
        # heartbeat path rather than aborting the build.
        emitted: list[str] = []
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.emit", emitted.append)
        transport = MagicMock()
        transport.popen.side_effect = OSError("no gcloud")
        transport.run.side_effect = [
            _done("status: done\n"),
            _done("fp123\n"),
        ]
        await_readiness(transport, "fp123", verbose=True)  # no raise
        assert any("live tail unavailable" in line for line in emitted)


class TestCloudClaudeLayout:
    def test_symlinks_history_subdirs_to_volume_only(self) -> None:
        transport = MagicMock()
        transport.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        link_cloud_claude_dirs(transport)
        joined = " ".join(c for call in transport.run.call_args_list for c in call.args)
        assert "/vergil/claude/projects" in joined
        assert "/vergil/claude/todos" in joined
        assert "/vergil/claude/.credentials.json" not in joined
        assert ".credentials.json" not in joined

    def test_relinks_existing_real_dir_by_merging(self) -> None:
        # #1999 (Fix A'): when ~/.claude/<sub> already exists as a real directory
        # (Claude created it on a box that was never linked), its contents are merged
        # onto the volume and the dir is replaced by a symlink — not left as a nested
        # broken `ln -sfn` target, and not clobbered.
        transport = MagicMock()
        transport.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        link_cloud_claude_dirs(transport)
        joined = " ".join(c for call in transport.run.call_args_list for c in call.args)
        assert "-L" in joined  # branch on symlink vs real dir
        assert "cp -a" in joined  # merge real-dir contents onto the volume
        assert "rm -rf" in joined  # remove the real dir before linking
        assert "ln -s" in joined


def _tofu_output_json(values: dict[str, str]) -> str:
    return json.dumps({k: {"value": v} for k, v in values.items()})


class TestTofuStateDirs:
    def test_state_dir_under_config_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        path = tofu_state_dir("vergil-user-o-r", "gcp")
        assert path == tmp_path / ".config" / "vergil" / "tofu" / "vergil-user-o-r" / "gcp"
        assert path.is_dir()

    def test_tofu_env_with_explicit_strategy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_tofu_env(strategy) delegates to strategy.tofu_env() (non-None branch)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-test")
        from vergil_tooling.lib.vm_cloud import _tofu_env
        from vergil_tooling.lib.vm_provider import AzureStrategy

        env = _tofu_env(AzureStrategy())
        assert env["ARM_SUBSCRIPTION_ID"] == "sub-test"


class TestRunTofu:
    def _setup_volume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[MagicMock, MagicMock, Path, Path]:
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        run = MagicMock(return_value=0)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"volume_id": "vol-1", "zone": "us-central1-a"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        return run, sub, state_dir, modules

    def test_apply_volume_flags_and_persists_zone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run, sub, state_dir, modules = self._setup_volume(tmp_path, monkeypatch)
        volume_id, zone = apply_volume(
            modules,
            state_dir,
            name="vm-x",
            region="us-central1",
            size_gib=300,
            labels={"vergil-org": "o"},
        )
        assert (volume_id, zone) == ("vol-1", "us-central1-a")
        # zone persisted for the transport
        assert (state_dir / "zone").read_text() == "us-central1-a"
        # tfvars written next to state, with the map kept nested
        var_file = state_dir / "volume.tfstate.tfvars.json"
        data = json.loads(var_file.read_text())
        assert data == {
            "name": "vm-x",
            "region": "us-central1",
            "size_gib": 300,
            "labels": {"vergil-org": "o"},
            "zone": "",
        }
        # init then apply, both non-interactive, apply auto-approved + state/var-file
        init_args = run.call_args_list[0].args[0]
        apply_args = run.call_args_list[1].args[0]
        assert init_args == ["tofu", f"-chdir={modules / 'gcp' / 'volume'}", "init", "-input=false"]
        assert "-input=false" in apply_args
        assert "-auto-approve" in apply_args
        assert f"-state={state_dir / 'volume.tfstate'}" in apply_args
        assert f"-var-file={state_dir / 'volume.tfstate.tfvars.json'}" in apply_args
        # env carries the automation + plugin-cache knobs
        env = run.call_args_list[0].kwargs["env"]
        assert env["TF_IN_AUTOMATION"] == "1"
        assert "plugin-cache" in env["TF_PLUGIN_CACHE_DIR"]
        # output captured via subprocess.run, not progress.run
        out_args = sub.call_args.args[0]
        assert out_args == [
            "tofu",
            f"-chdir={modules / 'gcp' / 'volume'}",
            "output",
            "-json",
            f"-state={state_dir / 'volume.tfstate'}",
        ]

    def test_apply_vm_passes_all_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        run = MagicMock(return_value=0)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"host": "vm-x", "ssh_user": "ubuntu"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        out = apply_vm(
            modules,
            state_dir,
            name="vm-x",
            zone="us-central1-a",
            instance_type="n2-standard-16",
            nested=True,
            volume_id="vol-1",
            ssh_user="ubuntu",
            provision_env="VERGIL_USER=ubuntu",
            labels={"vergil-org": "o"},
        )
        assert out == {"host": "vm-x", "ssh_user": "ubuntu"}
        data = json.loads((state_dir / "vm.tfstate.tfvars.json").read_text())
        assert data["nested"] is True
        assert data["volume_id"] == "vol-1"
        assert data["provision_env"] == "VERGIL_USER=ubuntu"
        # boot_disk_gib not supplied -> stays out of the tfvars (module default holds).
        assert "boot_disk_gib" not in data

    def _run_apply_vm(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boot_disk_gib: int | None
    ) -> dict[str, object]:
        """Run apply_vm with stub tofu and return the written vm tfvars dict."""
        monkeypatch.setenv("HOME", str(tmp_path))
        state_dir = tofu_state_dir("k", "gcp")
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", MagicMock(return_value=0))
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.subprocess.run",
            MagicMock(
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=_tofu_output_json({"host": "vm-x", "ssh_user": "ubuntu"})
                )
            ),
        )
        apply_vm(
            tmp_path / "modules",
            state_dir,
            name="vm-x",
            zone="us-central1-a",
            instance_type="n2-standard-16",
            nested=True,
            volume_id="vol-1",
            ssh_user="ubuntu",
            provision_env="VERGIL_USER=ubuntu",
            labels={},
            boot_disk_gib=boot_disk_gib,
        )
        tfvars = (state_dir / "vm.tfstate.tfvars.json").read_text()
        return cast("dict[str, object]", json.loads(tfvars))

    def test_apply_vm_threads_boot_disk_gib_into_tfvars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = self._run_apply_vm(tmp_path, monkeypatch, boot_disk_gib=100)
        assert data["boot_disk_gib"] == 100

    def test_apply_vm_omits_boot_disk_gib_when_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        data = self._run_apply_vm(tmp_path, monkeypatch, boot_disk_gib=None)
        assert "boot_disk_gib" not in data

    def test_apply_vm_rolls_back_on_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failed VM apply (e.g. capacity stockout on the instance) leaves the
        # global firewall behind in vm.tfstate; without a rollback the next create
        # 409s on the orphan firewall (#1804). apply_vm must tear the partial state
        # down with a `tofu destroy` before re-raising, so the create is retryable.
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        apply_err = subprocess.CalledProcessError(
            1, ("tofu", "apply"), stderr="Error: capacity stockout (n2-standard-16)"
        )

        def _run(cmd: list[str], **_kwargs: object) -> int:
            if "apply" in cmd:
                raise apply_err
            return 0

        run = MagicMock(side_effect=_run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)

        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            apply_vm(
                modules,
                state_dir,
                name="vm-x",
                zone="us-central1-a",
                instance_type="n2-standard-16",
                nested=True,
                volume_id="vol-1",
                ssh_user="ubuntu",
                provision_env="VERGIL_USER=ubuntu",
                labels={"vergil-org": "o"},
            )
        # the ORIGINAL apply error surfaces — the rollback doesn't mask the real cause
        assert excinfo.value is apply_err
        # a rollback destroy ran against the VM state, reusing the just-written tfvars
        destroy_calls = [c for c in run.call_args_list if "destroy" in c.args[0]]
        assert len(destroy_calls) == 1
        destroy_args = destroy_calls[0].args[0]
        assert f"-state={state_dir / 'vm.tfstate'}" in destroy_args
        assert f"-var-file={state_dir / 'vm.tfstate.tfvars.json'}" in destroy_args

    def test_apply_vm_rollback_failure_does_not_mask_original_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the best-effort rollback itself fails, the original apply error must
        # still be the one that surfaces — never the secondary cleanup failure.
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        apply_err = subprocess.CalledProcessError(
            1, ("tofu", "apply"), stderr="Error: capacity stockout"
        )

        def _run(cmd: list[str], **_kwargs: object) -> int:
            if "apply" in cmd:
                raise apply_err
            if "destroy" in cmd:
                raise subprocess.CalledProcessError(1, cmd, stderr="rollback boom")
            return 0

        run = MagicMock(side_effect=_run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)

        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            apply_vm(
                modules,
                state_dir,
                name="vm-x",
                zone="us-central1-a",
                instance_type="n2-standard-16",
                nested=True,
                volume_id="vol-1",
                ssh_user="ubuntu",
                provision_env="VERGIL_USER=ubuntu",
                labels={"vergil-org": "o"},
            )
        assert excinfo.value is apply_err

    def test_apply_vm_rollback_covers_azure_vm_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A forced Azure apply failure must trigger a rollback ``tofu destroy``
        # against the AZURE vm module dir/state (modules/azure/vm) — confirming
        # the rollback is provider-correct, not GCP-hardcoded.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-x")
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "azure")
        apply_err = subprocess.CalledProcessError(
            1, ("tofu", "apply"), stderr="Error: SkuNotAvailable"
        )

        def _run(cmd: list[str], **_kwargs: object) -> int:
            if "apply" in cmd:
                raise apply_err
            return 0

        run = MagicMock(side_effect=_run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)

        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            apply_vm(
                modules,
                state_dir,
                name="vrg-azure-box",
                zone="1",
                instance_type="Standard_D8s_v5",
                nested=True,
                volume_id="/subscriptions/sub-x/resourceGroups/rg-y/providers/d",
                ssh_user="ubuntu",
                provision_env="VERGIL_USER=ubuntu",
                labels={},
                provider="azure",
            )
        # The original azure apply error surfaces
        assert excinfo.value is apply_err
        # The rollback destroy ran against the azure vm module dir/state
        destroy_calls = [c for c in run.call_args_list if "destroy" in c.args[0]]
        assert len(destroy_calls) == 1
        destroy_args = destroy_calls[0].args[0]
        assert f"-chdir={modules / 'azure' / 'vm'}" in destroy_args
        assert f"-state={state_dir / 'vm.tfstate'}" in destroy_args

    def test_destroy_vm_reuses_stored_tfvars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        # A real reuse scenario has both the VM state and its stored tfvars.
        (state_dir / "vm.tfstate").write_text("{}")
        (state_dir / "vm.tfstate.tfvars.json").write_text('{"name": "vm-x"}')
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        destroy_vm(modules, state_dir)
        # tfvars untouched (reused, not rewritten)
        assert (state_dir / "vm.tfstate.tfvars.json").read_text() == '{"name": "vm-x"}'
        destroy_args = run.call_args_list[1].args[0]
        assert "destroy" in destroy_args
        assert "-auto-approve" in destroy_args
        assert f"-var-file={state_dir / 'vm.tfstate.tfvars.json'}" in destroy_args

    def test_non_mutating_action_omits_auto_approve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A read-only action (e.g. "plan") must not carry -auto-approve.
        from vergil_tooling.lib.vm_cloud import _run_tofu

        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        state = state_dir / "vm.tfstate"
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        _run_tofu(modules / "gcp" / "vm", state, "plan", {"name": "vm-x"})
        plan_args = run.call_args_list[1].args[0]
        assert "plan" in plan_args
        assert "-auto-approve" not in plan_args

    def test_destroy_vm_raises_when_state_present_but_tfvars_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A VM state exists but its tfvars were lost: destroy must still fail
        # loudly rather than run an under-specified destroy (the original guard).
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        (state_dir / "vm.tfstate").write_text("{}")
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        with pytest.raises(RuntimeError, match="no tofu vars supplied"):
            destroy_vm(modules, state_dir)

    def test_destroy_vm_noop_when_no_vm_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A volume-only box has no vm.tfstate: nothing to destroy, so destroy_vm
        # is a clean no-op — it must not reach tofu or raise. (#1845)
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        destroy_vm(modules, state_dir)
        run.assert_not_called()

    def test_destroy_volume_cleans_state_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        (state_dir / "volume.tfstate.tfvars.json").write_text('{"name": "vm-x"}')
        (state_dir / "zone").write_text("us-central1-a")
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        destroy_volume(modules, state_dir)
        assert not state_dir.exists()

    def test_destroy_volume_reports_no_disk_when_state_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An empty/placeholder volume.tfstate (no google_compute_disk resource):
        # tofu destroys nothing, so destroy_volume reports False so the caller can
        # warn instead of claiming a disk was destroyed. (#1846)
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        (state_dir / "volume.tfstate").write_text(json.dumps({"resources": []}))
        (state_dir / "volume.tfstate.tfvars.json").write_text('{"name": "vol-x"}')
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        assert destroy_volume(modules, state_dir) is False
        assert not state_dir.exists()

    def test_destroy_volume_reports_disk_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A volume.tfstate carrying a google_compute_disk: a real teardown, so
        # destroy_volume reports True. (#1846)
        monkeypatch.setenv("HOME", str(tmp_path))
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")
        (state_dir / "volume.tfstate").write_text(
            json.dumps(
                {
                    "resources": [
                        {
                            "type": "google_compute_disk",
                            "instances": [
                                {
                                    "attributes": {
                                        "name": "vol-x",
                                        "size": 200,
                                        "zone": "us-central1-f",
                                    }
                                }
                            ],
                        }
                    ]
                }
            )
        )
        (state_dir / "volume.tfstate.tfvars.json").write_text('{"name": "vol-x"}')
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        assert destroy_volume(modules, state_dir) is True
        assert not state_dir.exists()

    def test_azure_apply_volume_uses_azure_module_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """apply_volume with provider="azure" must pass -chdir=.../azure/volume to tofu."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-test")
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "azure")
        run = MagicMock(return_value=0)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"volume_id": "disk-1", "zone": "eastus"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        apply_volume(
            modules,
            state_dir,
            name="n",
            region="eastus",
            size_gib=64,
            labels={},
            provider="azure",
        )
        init_args = run.call_args_list[0].args[0]
        assert f"-chdir={modules / 'azure' / 'volume'}" in init_args
        assert f"-chdir={modules / 'gcp' / 'volume'}" not in init_args


class TestModulePathProvider:
    """apply_*/destroy_* must resolve the module dir under the spec's provider, not "gcp"."""

    def test_apply_volume_uses_provider_kwarg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        state_dir = tofu_state_dir("k", "azure")
        run = MagicMock(return_value=0)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"volume_id": "disk-1", "zone": "eastus-1"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        apply_volume(
            tmp_path / "modules",
            state_dir,
            name="n",
            region="eastus",
            size_gib=64,
            labels={},
            provider="azure",
        )
        # init chdir must point at <modules>/azure/volume
        init_args = run.call_args_list[0].args[0]
        assert f"-chdir={tmp_path / 'modules' / 'azure' / 'volume'}" in init_args

    def test_apply_vm_uses_provider_kwarg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        state_dir = tofu_state_dir("k", "azure")
        run = MagicMock(return_value=0)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"host": "vm-1", "ssh_user": "azureuser"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        apply_vm(
            tmp_path / "modules",
            state_dir,
            name="n",
            zone="eastus-1",
            instance_type="Standard_D8s_v3",
            nested=False,
            volume_id="disk-1",
            ssh_user="azureuser",
            provision_env="VERGIL_USER=azureuser",
            labels={},
            provider="azure",
        )
        init_args = run.call_args_list[0].args[0]
        assert f"-chdir={tmp_path / 'modules' / 'azure' / 'vm'}" in init_args

    def test_destroy_vm_uses_provider_kwarg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-test")
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "azure")
        (state_dir / "vm.tfstate").write_text("{}")
        (state_dir / "vm.tfstate.tfvars.json").write_text('{"name": "n"}')
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        destroy_vm(modules, state_dir, provider="azure")
        destroy_args = run.call_args_list[1].args[0]
        assert f"-chdir={modules / 'azure' / 'vm'}" in destroy_args

    def test_destroy_volume_uses_provider_kwarg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-test")
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "azure")
        (state_dir / "volume.tfstate.tfvars.json").write_text('{"name": "n"}')
        run = MagicMock(return_value=0)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        destroy_volume(modules, state_dir, provider="azure")
        # init ran against azure/volume
        init_args = run.call_args_list[0].args[0]
        assert f"-chdir={modules / 'azure' / 'volume'}" in init_args

    def test_gcp_default_is_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default (no provider kwarg) still resolves gcp/ — GCP regression guard."""
        monkeypatch.setenv("HOME", str(tmp_path))
        state_dir = tofu_state_dir("k", "gcp")
        run = MagicMock(return_value=0)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"volume_id": "vol-1", "zone": "us-central1-a"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        apply_volume(
            tmp_path / "modules",
            state_dir,
            name="n",
            region="us-central1",
            size_gib=300,
            labels={},
        )
        init_args = run.call_args_list[0].args[0]
        assert f"-chdir={tmp_path / 'modules' / 'gcp' / 'volume'}" in init_args


class TestTofuStrategyEnv:
    """apply_volume/apply_vm must inject the correct provider credentials into tofu.

    Before this fix the four lifecycle functions called _run_tofu/_tofu_output without
    a strategy, so an Azure apply ran azurerm with GOOGLE_CLOUD_PROJECT set and no
    ARM_SUBSCRIPTION_ID — Azure was silently non-functional. The tests below assert
    the correct env for both providers and serve as a regression guard.

    NOTE: AzureStrategy.tofu_env() spreads ``os.environ`` before overriding with
    ARM_SUBSCRIPTION_ID.  If a test fixture has GOOGLE_CLOUD_PROJECT in the process
    environment the Azure env will inherit it.  We monkeypatch it away here to make
    the assertion meaningful.  In production an operator's shell normally won't have
    GOOGLE_CLOUD_PROJECT set while running an Azure workflow, but the code itself
    does not explicitly remove it — that is a known (low-risk) leak worth a follow-up.
    """

    def test_azure_apply_volume_passes_arm_subscription_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An Azure apply_volume must set ARM_SUBSCRIPTION_ID and NOT set GOOGLE_CLOUD_PROJECT."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "azure-sub-123")
        # Remove GOOGLE_CLOUD_PROJECT so the assertion that it is absent is meaningful;
        # the autouse _default_gcp_project fixture sets it, and AzureStrategy.tofu_env
        # spreads os.environ, so without this delenv the Azure env would silently carry it.
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "azure")

        captured_envs: list[dict[str, str]] = []

        def _capture_run(cmd: list[str], env: dict[str, str] | None = None, **kw: object) -> int:
            if env is not None:
                captured_envs.append(dict(env))
            return 0

        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"volume_id": "disk-x", "zone": "eastus"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", _capture_run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)

        apply_volume(
            modules,
            state_dir,
            name="vrg-azure-box",
            region="eastus",
            size_gib=64,
            labels={},
            provider="azure",
        )

        assert captured_envs, "no progress.run calls captured"
        for env in captured_envs:
            assert env.get("ARM_SUBSCRIPTION_ID") == "azure-sub-123", (
                f"ARM_SUBSCRIPTION_ID missing or wrong in tofu env: {env}"
            )
            assert "GOOGLE_CLOUD_PROJECT" not in env, (
                f"GOOGLE_CLOUD_PROJECT must not be present in Azure tofu env: {env}"
            )

    def test_azure_apply_vm_passes_arm_subscription_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An Azure apply_vm must set ARM_SUBSCRIPTION_ID and NOT set GOOGLE_CLOUD_PROJECT."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "azure-sub-456")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)

        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "azure")

        captured_envs: list[dict[str, str]] = []

        def _capture_run(cmd: list[str], env: dict[str, str] | None = None, **kw: object) -> int:
            if env is not None:
                captured_envs.append(dict(env))
            return 0

        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"host": "20.1.2.3", "ssh_user": "ubuntu"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", _capture_run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)

        apply_vm(
            modules,
            state_dir,
            name="vrg-azure-box",
            zone="1",
            instance_type="Standard_D8s_v5",
            nested=False,
            volume_id="/subscriptions/sub/resourceGroups/rg/providers/d",
            ssh_user="ubuntu",
            provision_env="VERGIL_USER=ubuntu",
            labels={},
            provider="azure",
        )

        assert captured_envs, "no progress.run calls captured"
        for env in captured_envs:
            assert env.get("ARM_SUBSCRIPTION_ID") == "azure-sub-456", (
                f"ARM_SUBSCRIPTION_ID missing or wrong in tofu env: {env}"
            )
            assert "GOOGLE_CLOUD_PROJECT" not in env, (
                f"GOOGLE_CLOUD_PROJECT must not be present in Azure tofu env: {env}"
            )

    def test_gcp_apply_volume_still_has_google_cloud_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GCP apply_volume still injects GOOGLE_CLOUD_PROJECT — regression guard."""
        monkeypatch.setenv("HOME", str(tmp_path))
        # _default_gcp_project autouse fixture already sets GOOGLE_CLOUD_PROJECT=test-project
        modules = tmp_path / "modules"
        state_dir = tofu_state_dir("k", "gcp")

        captured_envs: list[dict[str, str]] = []

        def _capture_run(cmd: list[str], env: dict[str, str] | None = None, **kw: object) -> int:
            if env is not None:
                captured_envs.append(dict(env))
            return 0

        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout=_tofu_output_json({"volume_id": "vol-1", "zone": "us-central1-a"})
            )
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.progress.run", _capture_run)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)

        apply_volume(
            modules,
            state_dir,
            name="vrg-gcp-box",
            region="us-central1",
            size_gib=300,
            labels={},
            provider="gcp",
        )

        assert captured_envs, "no progress.run calls captured"
        for env in captured_envs:
            assert env.get("GOOGLE_CLOUD_PROJECT") == "test-project", (
                f"GOOGLE_CLOUD_PROJECT must be present in GCP tofu env: {env}"
            )


class TestReadZone:
    def test_reads_persisted_zone(self, tmp_path: Path) -> None:
        (tmp_path / "zone").write_text("us-central1-a\n")
        assert read_zone(tmp_path) == "us-central1-a"

    def test_missing_zone_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="no persisted zone"):
            read_zone(tmp_path)


def _capacity_exc() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, ["tofu", "apply"], stderr="Error: the zone does not have enough resources available"
    )


class TestZoneCapacity:
    def test_detects_capacity_phrasings(self) -> None:
        assert is_zone_capacity_error(_capacity_exc()) is True
        pool = subprocess.CalledProcessError(1, [], stderr="ZONE_RESOURCE_POOL_EXHAUSTED")
        assert is_zone_capacity_error(pool) is True

    def test_other_errors_are_not_capacity(self) -> None:
        other = subprocess.CalledProcessError(1, [], stderr="Error: quota 'CPUS' exceeded")
        assert is_zone_capacity_error(other) is False

    def test_region_zones_sorted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sub = MagicMock(
            return_value=subprocess.CompletedProcess(
                [], 0, stdout="us-central1-c\nus-central1-a\nus-central1-b\n"
            )
        )
        # GcpStrategy.region_zones uses vm_provider.subprocess.run (not vm_cloud's).
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert region_zones("us-central1") == [
            "us-central1-a",
            "us-central1-b",
            "us-central1-c",
        ]


class TestZoneFallback:
    @staticmethod
    def _backend() -> MagicMock:
        backend = MagicMock()
        backend.vm_vars.return_value = {}
        backend.volume_vars.return_value = {}
        backend.spec.region = "us-central1"
        backend.spec.instance = "n2-standard-16"
        # Route capacity checks through the real GCP strategy so non-capacity
        # errors are not silently swallowed.
        from vergil_tooling.lib.vm_provider import GcpStrategy

        backend.strategy.is_zone_capacity_error.side_effect = GcpStrategy().is_zone_capacity_error
        return backend

    def test_first_zone_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        av = MagicMock(return_value={"host": "h"})
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.apply_vm", av)
        result = apply_vm_with_zone_fallback(
            tmp_path / "m",
            tmp_path / "s",
            self._backend(),
            zone="us-central1-a",
            volume_id="v1",
            fallback_zones=["us-central1-b"],
        )
        assert result == ("v1", "us-central1-a", {"host": "h"})
        av.assert_called_once()

    def test_falls_back_across_zones_until_one_lands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # zone a (initial) stocked, zone b (fallback) stocked, zone c lands.
        av = MagicMock(side_effect=[_capacity_exc(), _capacity_exc(), {"host": "h"}])
        avol = MagicMock(side_effect=[("v2", "us-central1-b"), ("v3", "us-central1-c")])
        dv = MagicMock()
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.apply_vm", av)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.apply_volume", avol)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.destroy_volume", dv)
        result = apply_vm_with_zone_fallback(
            tmp_path / "m",
            tmp_path / "s",
            self._backend(),
            zone="us-central1-a",
            volume_id="v1",
            fallback_zones=["us-central1-b", "us-central1-c"],
        )
        assert result == ("v3", "us-central1-c", {"host": "h"})
        assert dv.call_count == 2  # the empty disk is torn down before each retry
        assert avol.call_count == 2  # recreated in each fallback zone
        assert av.call_count == 3

    def test_all_zones_stocked_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=_capacity_exc())
        )
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_volume",
            MagicMock(return_value=("v2", "us-central1-b")),
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.destroy_volume", MagicMock())
        with pytest.raises(RuntimeError, match="no zone in us-central1 has capacity"):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-a",
                volume_id="v1",
                fallback_zones=["us-central1-b", "us-central1-c"],
            )

    def test_non_capacity_error_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boom = subprocess.CalledProcessError(1, [], stderr="Error: bad config")
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=boom))
        with pytest.raises(subprocess.CalledProcessError):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-a",
                volume_id="v1",
                fallback_zones=["us-central1-b"],
            )

    def test_non_capacity_error_during_fallback_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # zone a stocked -> fall back to zone b, which fails with a real (non-capacity) error.
        boom = subprocess.CalledProcessError(1, [], stderr="Error: bad config")
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=[_capacity_exc(), boom])
        )
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_volume",
            MagicMock(return_value=("v2", "us-central1-b")),
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.destroy_volume", MagicMock())
        with pytest.raises(subprocess.CalledProcessError):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-a",
                volume_id="v1",
                fallback_zones=["us-central1-b", "us-central1-c"],
            )

    def test_capacity_with_no_fallback_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reattach (no fallback zones): a capacity error is fatal, not retried.
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=_capacity_exc())
        )
        with pytest.raises(subprocess.CalledProcessError):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-a",
                volume_id="v1",
                fallback_zones=[],
            )


class TestOffPlatformTransport:
    def test_builds_iap_from_local_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A fan-out enumerator reaches a running box from purely local state:
        # resource name + the persisted zone file, no spec composition.
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "zone").write_text("us-central1-b")
        transport = off_platform_transport("vergil-lmf-cloud", state_dir)
        assert isinstance(transport, IapTransport)
        assert transport.host == "vergil-lmf-cloud"
        assert transport.zone == "us-central1-b"
        assert transport.project == "proj-env"
        assert transport.ssh_user == "ubuntu"

    def test_gcp_still_builds_iap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit provider='gcp' still returns IapTransport — GCP regression guard."""
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "zone").write_text("us-central1-c")
        transport = off_platform_transport("vrg-abc123", state_dir, provider="gcp")
        assert isinstance(transport, IapTransport)
        assert transport.host == "vrg-abc123"
        assert transport.zone == "us-central1-c"
        assert transport.project == "proj-env"

    def test_builds_ssh_transport_for_azure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """provider='azure' returns an SshTransport keyed to the persisted IP + keypair."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "host").write_text("203.0.113.5")
        (state_dir / "volume_id").write_text(
            "/subscriptions/sub-x/resourceGroups/rg-y/providers/Microsoft.Compute/disks/d"
        )
        (state_dir / "id_ed25519").write_text("PRIV")
        (state_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA key")
        # Stub nsg_refresh so we don't need a real az CLI.
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.nsg_refresh", lambda *a, **k: None)
        transport = off_platform_transport("vrg-azure-box", state_dir, provider="azure")
        assert isinstance(transport, SshTransport)
        assert transport.host == "203.0.113.5"
        assert transport.ssh_user == "ubuntu"
        assert transport.key_path == str(state_dir / "id_ed25519")

    def test_raises_when_zone_not_persisted(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="no persisted zone"):
            off_platform_transport("vergil-lmf-cloud", tmp_path / "absent")


def _off_spec(**kw: object) -> ComposedSpec:
    base = ComposedSpec(
        cpus=16,
        memory="64GiB",
        disk="50GiB",
        stale_days=3,
        packages=(),
        apt_repos=(),
        vagrant_plugins=(),
        port_forwards=(),
        dedicated=True,
        under=(),
        nested=False,
        backend="off-platform",
        provider="gcp",
        region="us-central1",
        instance="n2-standard-16",
        volume="300GiB",
    )
    return dataclasses.replace(base, **kw)


class TestOffPlatformBackend:
    def test_init_computes_name_labels_and_ssh_user(self) -> None:
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        assert b.provider_label == "gcp"
        expected_slug = state_slug("vergil-user", "o", "r")
        assert b.slug == expected_slug
        assert b.name == cloud_resource_name(expected_slug)
        assert b.state_key == expected_slug  # readable slug, not the hashed name
        assert b.labels == cloud_labels("vergil-user", "o", "r")
        assert b.ssh_user == "ubuntu"

    def test_ssh_user_override_via_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_OFF_PLATFORM_SSH_USER", "deploy")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        assert b.ssh_user == "deploy"

    def test_project_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        assert b._project() == "proj-env"

    def test_project_from_gcloud(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="proj-cli\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        assert b._project() == "proj-cli"

    def test_project_empty_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        sub = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="\n", stderr=""))
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        with pytest.raises(SystemExit):
            b._project()

    def test_state_dir_uses_state_key_and_provider(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        assert b.state_dir() == tmp_path / ".config" / "vergil" / "tofu" / b.state_key / "gcp"

    def test_transport_builds_iap_with_read_zone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        (b.state_dir() / "zone").write_text("us-central1-b")
        transport = b.transport()
        assert isinstance(transport, IapTransport)
        assert transport.host == b.name
        assert transport.zone == "us-central1-b"
        assert transport.project == "proj-env"
        assert transport.ssh_user == "ubuntu"

    def test_status_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        (b.state_dir() / "zone").write_text("us-central1-b")
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="RUNNING\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert b.status() == "Running"

    def test_status_terminated_is_stopped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        (b.state_dir() / "zone").write_text("us-central1-b")
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="TERMINATED\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert b.status() == "Stopped"

    def test_status_unknown_state_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        (b.state_dir() / "zone").write_text("us-central1-b")
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="PROVISIONING\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert b.status() == ""

    def test_status_no_creds_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        (b.state_dir() / "zone").write_text("us-central1-b")
        sub = MagicMock(side_effect=subprocess.CalledProcessError(1, "gcloud"))
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert b.status() == ""

    def test_status_delegates_to_azure_strategy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OffPlatformBackend.status delegates to AzureStrategy.status for azure provider."""
        monkeypatch.setenv("HOME", str(tmp_path))
        b = OffPlatformBackend(_off_spec(provider="azure"), "vergil-user", "o", "r")
        mock_status = MagicMock(return_value="Running")
        monkeypatch.setattr(b.strategy, "status", mock_status)
        result = b.status()
        assert result == "Running"
        mock_status.assert_called_once_with(b.name, b.state_dir())

    def test_status_no_state_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        # no zone file written -> read_zone raises -> ""
        assert b.status() == ""

    def test_volume_vars(self) -> None:
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        vars_ = b.volume_vars()
        assert vars_ == {
            "name": b.name,
            "region": "us-central1",
            "size_gib": 300,
            "labels": b.labels,
            "zone": "",
        }

    def test_volume_vars_carries_explicit_zone(self) -> None:
        b = OffPlatformBackend(_off_spec(zone="us-central1-a"), "vergil-user", "o", "r")
        assert b.volume_vars()["zone"] == "us-central1-a"

    def test_vm_vars_composition(self) -> None:
        b = OffPlatformBackend(_off_spec(nested=True), "vergil-user", "o", "r")
        vars_ = b.vm_vars(zone="us-central1-b", volume_id="vol-1")
        assert vars_["name"] == b.name
        assert vars_["zone"] == "us-central1-b"
        assert vars_["instance_type"] == "n2-standard-16"
        assert vars_["nested"] is True
        assert vars_["volume_id"] == "vol-1"
        assert vars_["ssh_user"] == "ubuntu"
        assert vars_["labels"] == b.labels
        env = vars_["provision_env"]
        assert isinstance(env, str)
        assert "VERGIL_USER=ubuntu" in env
        assert "HOME=/home/ubuntu" in env
        from vergil_tooling.lib.vm_spec import spec_fingerprint

        assert f"SPEC_FINGERPRINT={spec_fingerprint(_off_spec(nested=True))}" in env

    def test_vm_vars_omits_boot_disk_gib_when_unset(self) -> None:
        # Unset boot_disk -> no boot_disk_gib var, so the tofu module default holds.
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        vars_ = b.vm_vars(zone="us-central1-b", volume_id="vol-1")
        assert "boot_disk_gib" not in vars_

    def test_vm_vars_threads_boot_disk_gib_when_set(self) -> None:
        b = OffPlatformBackend(_off_spec(boot_disk="100GiB"), "vergil-user", "o", "r")
        vars_ = b.vm_vars(zone="us-central1-b", volume_id="vol-1")
        assert vars_["boot_disk_gib"] == 100

    def test_vm_vars_includes_ssh_public_key_for_azure_and_absent_for_gcp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Azure vm_vars includes ssh_public_key; GCP vm_vars must NOT include it."""
        monkeypatch.setenv("HOME", str(tmp_path))

        def fake_keygen(priv: Path) -> None:
            priv.write_text("PRIV")
            priv.with_suffix(".pub").write_text("ssh-ed25519 AAAA test-key")

        monkeypatch.setattr(vm_cloud, "_run_keygen", fake_keygen)

        # Azure: key must be present
        b_azure = OffPlatformBackend(_off_spec(provider="azure"), "vergil-user", "o", "r")
        state_dir = b_azure.state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        azure_vars = b_azure.vm_vars(zone="eastus", volume_id="vol-1")
        assert "ssh_public_key" in azure_vars
        assert azure_vars["ssh_public_key"] == "ssh-ed25519 AAAA test-key"

        # GCP: key must be absent
        b_gcp = OffPlatformBackend(_off_spec(provider="gcp"), "vergil-user", "o", "r")
        gcp_vars = b_gcp.vm_vars(zone="us-central1-b", volume_id="vol-1")
        assert "ssh_public_key" not in gcp_vars


def _volume_tfstate(
    *,
    name: str = "vergil-lmf-cloud-data",
    size: object = 300,
    zone: str = "us-central1-a",
    labels: dict[str, str] | None = None,
) -> str:
    """A minimal but realistic volume.tfstate carrying one google_compute_disk."""
    if labels is None:
        labels = {"vergil-identity": "vergil", "vergil-org": "lmf", "vergil-repo": "cloud"}
    return json.dumps(
        {
            "version": 4,
            "terraform_version": "1.8.0",
            "resources": [
                {
                    "mode": "managed",
                    "type": "google_compute_disk",
                    "name": "data",
                    "instances": [
                        {
                            "attributes": {
                                "name": name,
                                "size": size,
                                "zone": zone,
                                "labels": labels,
                            }
                        }
                    ],
                }
            ],
        }
    )


class TestZoneToRegion:
    def test_strips_trailing_zone_suffix(self) -> None:
        assert zone_to_region("us-central1-a") == "us-central1"

    def test_multi_token_region_preserved(self) -> None:
        assert zone_to_region("europe-west4-b") == "europe-west4"

    def test_empty_zone_is_empty(self) -> None:
        assert zone_to_region("") == ""

    def test_zone_without_suffix_is_empty(self) -> None:
        assert zone_to_region("noregion") == ""


class TestParseVolumeState:
    def test_parses_disk_attributes_and_labels(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(_volume_tfstate())
        parsed = parse_volume_state(state)
        assert parsed is not None
        assert parsed.name == "vergil-lmf-cloud-data"
        assert parsed.size_gib == 300
        assert parsed.zone == "us-central1-a"
        assert parsed.labels == {
            "vergil-identity": "vergil",
            "vergil-org": "lmf",
            "vergil-repo": "cloud",
        }

    def test_normalizes_zone_selflink_url(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        url = "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-c"
        state.write_text(_volume_tfstate(zone=url))
        parsed = parse_volume_state(state)
        assert parsed is not None
        assert parsed.zone == "us-central1-c"

    def test_string_size_coerced_to_int(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(_volume_tfstate(size="500"))
        parsed = parse_volume_state(state)
        assert parsed is not None
        assert parsed.size_gib == 500

    def test_non_numeric_size_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(_volume_tfstate(size="big"))
        parsed = parse_volume_state(state)
        assert parsed is not None
        assert parsed.size_gib is None

    def test_empty_placeholder_state_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text("{}")
        assert parse_volume_state(state) is None

    def test_malformed_json_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text("not json {")
        assert parse_volume_state(state) is None

    def test_missing_file_is_none(self, tmp_path: Path) -> None:
        assert parse_volume_state(tmp_path / "absent.tfstate") is None

    def test_no_applied_disk_resource_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(
            json.dumps(
                {
                    "version": 4,
                    "resources": [
                        {"type": "google_compute_disk", "instances": []},
                        {"type": "random_id", "instances": [{"attributes": {"hex": "abc"}}]},
                    ],
                }
            )
        )
        assert parse_volume_state(state) is None

    def test_absent_labels_yield_empty_dict(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(
            json.dumps(
                {
                    "version": 4,
                    "resources": [
                        {
                            "type": "google_compute_disk",
                            "instances": [
                                {"attributes": {"name": "d", "size": 100, "zone": "us-central1-a"}}
                            ],
                        }
                    ],
                }
            )
        )
        parsed = parse_volume_state(state)
        assert parsed is not None
        assert parsed.labels == {}

    def test_bool_size_is_none(self, tmp_path: Path) -> None:
        # JSON ``true`` round-trips to a Python bool; a bool is not a disk size.
        state = tmp_path / "volume.tfstate"
        state.write_text(_volume_tfstate(size=True))
        parsed = parse_volume_state(state)
        assert parsed is not None
        assert parsed.size_gib is None

    def test_non_scalar_size_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(_volume_tfstate(size=[1, 2]))
        parsed = parse_volume_state(state)
        assert parsed is not None
        assert parsed.size_gib is None

    def test_top_level_non_object_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text("[]")
        assert parse_volume_state(state) is None

    def test_non_object_attributes_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(
            json.dumps(
                {
                    "version": 4,
                    "resources": [
                        {
                            "type": "google_compute_disk",
                            "instances": [{"attributes": "not-an-object"}],
                        }
                    ],
                }
            )
        )
        assert parse_volume_state(state) is None

    # ------------------------------------------------------------------
    # GCP regression — default path must be byte-identical to before
    # ------------------------------------------------------------------

    def test_gcp_still_parses_google_disk(self, tmp_path: Path) -> None:
        """GCP parse_volume_state (provider='gcp') is byte-identical to pre-Task-6 behavior."""
        state = tmp_path / "volume.tfstate"
        state.write_text(_volume_tfstate())
        parsed = parse_volume_state(state, provider="gcp")
        assert parsed is not None
        assert parsed.name == "vergil-lmf-cloud-data"
        assert parsed.size_gib == 300
        assert parsed.zone == "us-central1-a"
        assert parsed.labels == {
            "vergil-identity": "vergil",
            "vergil-org": "lmf",
            "vergil-repo": "cloud",
        }

    # ------------------------------------------------------------------
    # Azure — azurerm_managed_disk with disk_size_gb / tags
    # ------------------------------------------------------------------

    def test_parses_azure_managed_disk(self, tmp_path: Path) -> None:
        """Azure parse_volume_state reads disk_size_gb and tags from azurerm_managed_disk.

        Attribute names verified against the azurerm Terraform provider schema (2026-06-25):
        https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/managed_disk
        """
        state = tmp_path / "volume.tfstate"
        state.write_text(_azure_volume_tfstate())
        parsed = parse_volume_state(state, provider="azure")
        assert parsed is not None
        assert parsed.name == "vrg-abc-data"
        assert parsed.size_gib == 300
        assert parsed.zone == "1"
        assert parsed.labels == {
            "vergil-identity": "vergil",
            "vergil-org": "lmf",
            "vergil-repo": "cloud",
        }

    def test_azure_disk_wrong_provider_returns_none(self, tmp_path: Path) -> None:
        """An azurerm_managed_disk state returns None when provider='gcp' (type mismatch)."""
        state = tmp_path / "volume.tfstate"
        state.write_text(_azure_volume_tfstate())
        assert parse_volume_state(state, provider="gcp") is None

    def test_azure_non_numeric_disk_size_is_none(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(_azure_volume_tfstate(disk_size_gb="big"))
        parsed = parse_volume_state(state, provider="azure")
        assert parsed is not None
        assert parsed.size_gib is None

    def test_azure_absent_tags_yield_empty_dict(self, tmp_path: Path) -> None:
        state = tmp_path / "volume.tfstate"
        state.write_text(_azure_volume_tfstate(tags={}))
        parsed = parse_volume_state(state, provider="azure")
        assert parsed is not None
        assert parsed.labels == {}


def _azure_volume_tfstate(
    *,
    name: str = "vrg-abc-data",
    disk_size_gb: object = 300,
    zone: str = "1",
    tags: dict[str, str] | None = None,
) -> str:
    """A minimal but realistic volume.tfstate carrying one azurerm_managed_disk.

    Azure zones are bare AZ integers ("1", "2", "3"), not GCP-style region+zone
    strings.  Azure uses ``disk_size_gb`` (not ``size``) and ``tags`` (not
    ``labels``) — verified against the azurerm provider schema (2026-06-25).
    """
    if tags is None:
        tags = {"vergil-identity": "vergil", "vergil-org": "lmf", "vergil-repo": "cloud"}
    return json.dumps(
        {
            "version": 4,
            "terraform_version": "1.8.0",
            "resources": [
                {
                    "mode": "managed",
                    "type": "azurerm_managed_disk",
                    "name": "data",
                    "instances": [
                        {
                            "attributes": {
                                "name": name,
                                "disk_size_gb": disk_size_gb,
                                "zone": zone,
                                "tags": tags,
                            }
                        }
                    ],
                }
            ],
        }
    )


def test_cloud_resource_name_is_hashed_and_deterministic() -> None:
    slug = "vergil-user--logical-minds-foundry--mq-cluster-tooling--cloud-x86"
    name = cloud_resource_name(slug)
    assert name.startswith("vrg-")
    assert len(name) == 16  # "vrg-" + 12 hex
    assert name == cloud_resource_name(slug)  # deterministic
    assert cloud_resource_name(slug + "-other") != name


def test_cloud_labels_includes_instance_when_named() -> None:
    labels = cloud_labels("vergil-user", "lmf", "mq", "cloud-x86")
    assert labels["vergil-instance"] == "cloud-x86"
    assert "vergil-instance" not in cloud_labels("vergil-user", "lmf", "mq")


class TestInstanceFallbackLadder:
    def test_requested_first_then_same_shape_siblings(self) -> None:
        assert instance_fallback_candidates("n2-standard-8") == [
            "n2-standard-8",
            "c2-standard-8",
        ]

    def test_dedups_when_requested_family_in_ladder(self) -> None:
        # c2 is in the ladder; it must appear once, still requested-first.
        result = instance_fallback_candidates("c2-standard-16")
        assert result[0] == "c2-standard-16"
        assert result.count("c2-standard-16") == 1
        assert set(result) == {
            "c2-standard-16",
            "n2-standard-16",
        }

    def test_unsupported_shape_yields_no_fallback(self) -> None:
        assert instance_fallback_candidates("n2-highmem-8") == ["n2-highmem-8"]
        assert instance_fallback_candidates("n2-standard-4") == ["n2-standard-4"]

    def test_requested_family_not_in_ladder_still_leads(self) -> None:
        # A misconfigured non-nested-virt family: original first, then full ladder.
        assert instance_fallback_candidates("e2-standard-8") == [
            "e2-standard-8",
            "n2-standard-8",
            "c2-standard-8",
        ]

    def test_ladder_change_detector(self) -> None:
        # NOT a validity proof — pins the curated values so an edit is deliberate.
        # Real nested-virt validity is verified by hand against GCP docs (#1836):
        # GCE nested virt is Intel-only, so the AMD families (n2d, c2d) are excluded.
        assert NESTED_VIRT_FAMILIES == ("n2", "c2")
        assert FALLBACK_SHAPES == frozenset({"standard-8", "standard-16"})  # noqa: SIM300 — variable == literal reads naturally for a change-detector pin


class TestVmVarsInstanceOverride:
    def test_override_swaps_machine_type_but_keeps_declared_fingerprint(self) -> None:
        spec = _off_spec(instance="n2-standard-8")
        b = OffPlatformBackend(spec, "vergil-user", "o", "r")
        declared_fp = spec_fingerprint(spec)
        would_be_landed_fp = spec_fingerprint(dataclasses.replace(spec, instance="n2d-standard-8"))

        v = b.vm_vars(zone="us-central1-f", volume_id="v1", instance_override="n2d-standard-8")

        # The tofu machine type is the fallback family...
        assert v["instance_type"] == "n2d-standard-8"
        # ...but the stamped fingerprint is the DECLARED one, never the landed family's.
        assert declared_fp in str(v["provision_env"])
        assert would_be_landed_fp not in str(v["provision_env"])
        # ...and the spec object is never mutated.
        assert b.spec.instance == "n2-standard-8"

    def test_default_uses_declared_instance(self) -> None:
        b = OffPlatformBackend(_off_spec(instance="n2-standard-16"), "vergil-user", "o", "r")
        v = b.vm_vars(zone="us-central1-b", volume_id="v1")
        assert v["instance_type"] == "n2-standard-16"
        assert spec_fingerprint(_off_spec(instance="n2-standard-16")) in str(v["provision_env"])


class TestFamilyFallback:
    @staticmethod
    def _backend() -> MagicMock:
        backend = MagicMock()
        backend.vm_vars.return_value = {}
        backend.spec.region = "us-central1"
        backend.spec.instance = "n2-standard-8"
        # Route capacity checks through the real GCP strategy so non-capacity
        # errors (e.g. "bad config") are not silently swallowed.
        from vergil_tooling.lib.vm_provider import GcpStrategy

        backend.strategy.is_zone_capacity_error.side_effect = GcpStrategy().is_zone_capacity_error
        return backend

    def test_swaps_family_in_same_zone_without_touching_volume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Requested family stocked, first fallback family lands — same zone, same disk.
        av = MagicMock(side_effect=[_capacity_exc(), {"host": "h"}])
        dv = MagicMock()
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.apply_vm", av)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.destroy_volume", dv)
        backend = self._backend()

        result = apply_vm_with_zone_fallback(
            tmp_path / "m",
            tmp_path / "s",
            backend,
            zone="us-central1-f",
            volume_id="v1",
            fallback_zones=[],
            fallback_instances=["n2d-standard-8", "c2-standard-8"],
        )

        assert result == ("v1", "us-central1-f", {"host": "h"})
        assert av.call_count == 2
        dv.assert_not_called()  # the data disk is never destroyed on this path
        backend.vm_vars.assert_any_call(
            zone="us-central1-f", volume_id="v1", instance_override="n2d-standard-8"
        )

    def test_all_families_stocked_raises_naming_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=_capacity_exc())
        )
        with pytest.raises(RuntimeError, match="no nested-virt machine family has capacity"):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-f",
                volume_id="v1",
                fallback_zones=[],
                fallback_instances=["n2d-standard-8", "c2-standard-8"],
            )

    def test_non_capacity_error_during_family_sweep_aborts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        boom = subprocess.CalledProcessError(1, [], stderr="Error: bad config")
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm",
            MagicMock(side_effect=[_capacity_exc(), boom]),
        )
        with pytest.raises(subprocess.CalledProcessError):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-f",
                volume_id="v1",
                fallback_zones=[],
                fallback_instances=["n2d-standard-8", "c2-standard-8"],
            )

    def test_capacity_with_no_fallbacks_at_all_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reattach of an unsupported shape: no families, no zones -> original error.
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.apply_vm", MagicMock(side_effect=_capacity_exc())
        )
        with pytest.raises(subprocess.CalledProcessError):
            apply_vm_with_zone_fallback(
                tmp_path / "m",
                tmp_path / "s",
                self._backend(),
                zone="us-central1-f",
                volume_id="v1",
                fallback_zones=[],
                fallback_instances=[],
            )

    def test_azure_family_sweep_drives_via_strategy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Azure backend: capacity check routes through strategy; data disk is never destroyed."""
        azure_capacity_exc = subprocess.CalledProcessError(
            1, ["tofu", "apply"], stderr="SkuNotAvailable in zone 1"
        )
        av = MagicMock(side_effect=[azure_capacity_exc, {"host": "h"}])
        dv = MagicMock()
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.apply_vm", av)
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.destroy_volume", dv)

        backend = MagicMock()
        backend.vm_vars.return_value = {}
        backend.spec.region = "eastus"
        backend.spec.instance = "Standard_D8s_v5"
        from vergil_tooling.lib.vm_provider import AzureStrategy

        backend.strategy.is_zone_capacity_error.side_effect = AzureStrategy().is_zone_capacity_error

        result = apply_vm_with_zone_fallback(
            tmp_path / "m",
            tmp_path / "s",
            backend,
            zone="1",
            volume_id="vol-azure-1",
            fallback_zones=[],
            fallback_instances=["Standard_D8s_v4"],
        )

        assert result == ("vol-azure-1", "1", {"host": "h"})
        assert av.call_count == 2
        dv.assert_not_called()  # the data disk is never destroyed on a family sweep
        backend.vm_vars.assert_any_call(
            zone="1", volume_id="vol-azure-1", instance_override="Standard_D8s_v4"
        )


class TestParseVmMachineType:
    def _state(self, machine_type: str) -> str:
        return json.dumps(
            {
                "resources": [
                    {
                        "type": "google_compute_instance",
                        "instances": [{"attributes": {"machine_type": machine_type}}],
                    }
                ]
            }
        )

    def test_returns_bare_type_from_selflink(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(self._state("projects/p/zones/us-central1-f/machineTypes/n2d-standard-8"))
        assert parse_vm_machine_type(f) == "n2d-standard-8"

    def test_returns_bare_type_when_already_bare(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(self._state("n2-standard-8"))
        assert parse_vm_machine_type(f) == "n2-standard-8"

    def test_none_when_absent_or_empty(self, tmp_path: Path) -> None:
        assert parse_vm_machine_type(tmp_path / "missing.tfstate") is None
        empty = tmp_path / "vm.tfstate"
        empty.write_text("{}")
        assert parse_vm_machine_type(empty) is None

    def test_none_when_json_is_not_an_object(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text("[]")  # valid JSON, but not a dict
        assert parse_vm_machine_type(f) is None

    def test_skips_non_dict_and_non_instance_resources(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(
            json.dumps(
                {"resources": ["not-a-dict", {"type": "google_compute_disk", "instances": []}]}
            )
        )
        assert parse_vm_machine_type(f) is None

    def test_none_when_instance_resource_has_no_usable_instance(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(
            json.dumps({"resources": [{"type": "google_compute_instance", "instances": ["x"]}]})
        )
        assert parse_vm_machine_type(f) is None

    def test_none_when_attributes_not_a_dict(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(
            json.dumps(
                {
                    "resources": [
                        {"type": "google_compute_instance", "instances": [{"attributes": "nope"}]}
                    ]
                }
            )
        )
        assert parse_vm_machine_type(f) is None

    def test_none_when_machine_type_is_empty(self, tmp_path: Path) -> None:
        f = tmp_path / "vm.tfstate"
        f.write_text(self._state(""))  # falsy machine_type -> continue -> None
        assert parse_vm_machine_type(f) is None


# ---------------------------------------------------------------------------
# Task 3: ensure_keypair / _operator_public_ip / nsg_refresh
# ---------------------------------------------------------------------------


def _ok() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


class TestRunKeygen:
    def test_invokes_ssh_keygen(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """_run_keygen calls ssh-keygen with the expected arguments."""
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.subprocess.run",
            lambda argv, **k: seen.setdefault("argv", argv) or _ok(),
        )
        priv = tmp_path / "id_ed25519"
        vm_cloud._run_keygen(priv)
        argv = seen["argv"]
        assert isinstance(argv, list)
        assert argv[0] == "ssh-keygen"
        assert "-t" in argv
        assert "ed25519" in argv
        assert str(priv) in argv


class TestFetchPublicIp:
    def test_reads_response_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_fetch_public_ip returns the decoded response body."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"  1.2.3.4  "
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.urllib.request.urlopen",
            lambda req, timeout: mock_resp,
        )
        result = vm_cloud._fetch_public_ip("https://example.com/ip")
        assert result == "1.2.3.4"


class TestReadHostAndVolumeId:
    def test_read_host_raises_when_absent(self, tmp_path: Path) -> None:
        """read_host raises RuntimeError when the host file does not exist."""
        with pytest.raises(RuntimeError, match="no persisted host"):
            read_host(tmp_path)

    def test_read_host_raises_when_empty(self, tmp_path: Path) -> None:
        """read_host raises RuntimeError when the host file exists but is empty."""
        (tmp_path / "host").write_text("   \n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="empty"):
            read_host(tmp_path)

    def test_read_host_returns_content_when_present(self, tmp_path: Path) -> None:
        """read_host returns stripped content when the file contains a valid host."""
        (tmp_path / "host").write_text("  20.1.2.3\n", encoding="utf-8")
        assert read_host(tmp_path) == "20.1.2.3"

    def test_read_volume_id_raises_when_absent(self, tmp_path: Path) -> None:
        """read_volume_id raises RuntimeError when the volume_id file does not exist."""
        with pytest.raises(RuntimeError, match="no persisted volume_id"):
            read_volume_id(tmp_path)


class TestAzureResourceGroupParse:
    def test_raises_on_short_volume_id(self) -> None:
        """_azure_resource_group_from_volume_id raises ValueError on malformed ARM IDs."""
        with pytest.raises(ValueError, match="Cannot parse resource group"):
            _azure_resource_group_from_volume_id("/subscriptions/only")

    def test_returns_resource_group_for_valid_arm_id(self) -> None:
        """Well-formed ARM ID returns the correct resource group name."""
        arm_id = "/subscriptions/sub-123/resourceGroups/my-rg/providers/Microsoft.Compute/disks/d"
        assert _azure_resource_group_from_volume_id(arm_id) == "my-rg"

    def test_raises_on_wrong_structural_tokens(self) -> None:
        """Right length but wrong token names (e.g. 'subscriptionz') raises ValueError."""
        bad_id = "/subscriptionz/sub-123/resourceGroups/my-rg/providers/Microsoft.Compute/disks/d"
        with pytest.raises(ValueError, match="ARM ID structure"):
            _azure_resource_group_from_volume_id(bad_id)

    def test_raises_on_wrong_second_structural_token(self) -> None:
        """Right length but 'resourceGroupz' at position 3 raises ValueError."""
        bad_id = "/subscriptions/sub-123/resourceGroupz/my-rg/providers/Microsoft.Compute/disks/d"
        with pytest.raises(ValueError, match="ARM ID structure"):
            _azure_resource_group_from_volume_id(bad_id)


class TestEnsureKeypair:
    def test_generates_keypair_on_first_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ensure_keypair calls _run_keygen when the key does not yet exist."""
        generated: list[Path] = []

        def fake_keygen(priv: Path) -> None:
            priv.write_text("PRIVATE_KEY")
            priv.with_suffix(".pub").write_text("ssh-ed25519 AAAA fake-key")
            generated.append(priv)

        monkeypatch.setattr(vm_cloud, "_run_keygen", fake_keygen)
        key_path, pub = ensure_keypair(tmp_path)
        assert key_path == tmp_path / "id_ed25519"
        assert pub == "ssh-ed25519 AAAA fake-key"
        assert len(generated) == 1

    def test_is_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Second call reuses the existing key without calling _run_keygen again."""
        call_count: list[int] = [0]

        def fake_keygen(priv: Path) -> None:
            call_count[0] += 1
            priv.write_text("PRIV")
            priv.with_suffix(".pub").write_text("ssh-ed25519 AAAA idempotent-key")

        monkeypatch.setattr(vm_cloud, "_run_keygen", fake_keygen)
        p1, pub1 = ensure_keypair(tmp_path)
        p2, pub2 = ensure_keypair(tmp_path)  # second call must NOT regenerate
        assert p1 == p2
        assert pub1 == pub2 == "ssh-ed25519 AAAA idempotent-key"
        assert call_count[0] == 1, "keygen must only run once"

    def test_returns_stripped_public_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Public key content is stripped of surrounding whitespace."""

        def fake_keygen(priv: Path) -> None:
            priv.write_text("PRIV")
            priv.with_suffix(".pub").write_text("  ssh-ed25519 AAAA key  \n")

        monkeypatch.setattr(vm_cloud, "_run_keygen", fake_keygen)
        _, pub = ensure_keypair(tmp_path)
        assert pub == "ssh-ed25519 AAAA key"


class TestOperatorPublicIp:
    def test_returns_ip_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_operator_public_ip returns the IP string from the echo endpoint."""
        monkeypatch.setattr(vm_cloud, "_fetch_public_ip", lambda url: "203.0.113.5")
        assert vm_cloud._operator_public_ip() == "203.0.113.5"

    def test_fail_closed_on_empty_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed: empty response raises, never produces a wildcard rule."""
        monkeypatch.setattr(vm_cloud, "_fetch_public_ip", lambda url: "")
        with pytest.raises((SystemExit, RuntimeError, ValueError)):
            vm_cloud._operator_public_ip()

    def test_fail_closed_on_non_ip_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed: non-IP response raises — no silent fallback to a wildcard."""
        monkeypatch.setattr(vm_cloud, "_fetch_public_ip", lambda url: "not-an-ip")
        # Must raise; the implementation is forbidden from returning a wildcard.
        with pytest.raises((SystemExit, RuntimeError, ValueError)):
            vm_cloud._operator_public_ip()

    def test_fail_closed_on_endpoint_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed: network error converts to SystemExit — never re-raised as URLError."""

        def fail(_url: str) -> str:
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(vm_cloud, "_fetch_public_ip", fail)
        # The implementation must catch URLError and convert it to SystemExit (sys.exit).
        # Accepting URLError here would pass even if the error slipped through uncaught,
        # so we narrow the assertion to SystemExit only.
        with pytest.raises(SystemExit):
            vm_cloud._operator_public_ip()

    def test_fail_closed_on_out_of_range_octet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail closed: '999.1.1.1' is syntactically IP-like but invalid — must abort."""
        monkeypatch.setattr(vm_cloud, "_fetch_public_ip", lambda url: "999.1.1.1")
        with pytest.raises(SystemExit):
            vm_cloud._operator_public_ip()

    def test_env_override_changes_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """VRG_PUBLIC_IP_ENDPOINT overrides the default echo URL."""
        seen_urls: list[str] = []
        monkeypatch.setenv("VRG_PUBLIC_IP_ENDPOINT", "https://custom.example.com/ip")
        monkeypatch.setattr(
            vm_cloud, "_fetch_public_ip", lambda url: seen_urls.append(url) or "1.2.3.4"
        )
        vm_cloud._operator_public_ip()
        assert seen_urls == ["https://custom.example.com/ip"]


class TestNsgRefresh:
    def test_sets_current_ip_via_az_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """nsg_refresh calls az network nsg rule update with the operator's /32."""
        monkeypatch.setattr(vm_cloud, "_operator_public_ip", lambda: "203.0.113.5")
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.subprocess.run",
            lambda argv, **k: seen.setdefault("argv", argv) or _ok(),
        )
        nsg_refresh("n-rg", "n-nsg", "ssh-operator")
        argv = seen["argv"]
        assert isinstance(argv, list)
        assert argv[0] == "az"
        assert "--source-address-prefixes" in argv
        assert "203.0.113.5/32" in argv

    def test_argv_contains_required_subcommands(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The full az network nsg rule update subcommand chain is present."""
        monkeypatch.setattr(vm_cloud, "_operator_public_ip", lambda: "10.0.0.1")
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.subprocess.run",
            lambda argv, **k: seen.setdefault("argv", argv) or _ok(),
        )
        nsg_refresh("my-rg", "my-nsg", "ssh-rule")
        argv = cast("list[str]", seen["argv"])
        assert isinstance(argv, list)
        joined = " ".join(argv)
        assert "network" in joined
        assert "nsg" in joined
        assert "rule" in joined
        assert "update" in joined
        assert "my-rg" in joined
        assert "my-nsg" in joined
        assert "ssh-rule" in joined

    def test_never_uses_wildcard_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Even if _operator_public_ip returns a real IP, no wildcard is in the argv."""
        monkeypatch.setattr(vm_cloud, "_operator_public_ip", lambda: "198.51.100.7")
        seen: dict[str, object] = {}
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.subprocess.run",
            lambda argv, **k: seen.setdefault("argv", argv) or _ok(),
        )
        nsg_refresh("rg", "nsg", "rule")
        argv = seen["argv"]
        assert isinstance(argv, list)
        assert "0.0.0.0/0" not in argv
        assert "*" not in argv

    def test_az_never_invoked_when_ip_discovery_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Wildcard-absent invariant: if IP discovery fails, az is NEVER invoked.

        Patches _fetch_public_ip to raise (simulating a network failure) then
        drives the path through _operator_public_ip as called by nsg_refresh.
        Asserts subprocess.run is not called — i.e. no `az` invocation, so no
        NSG rule (wildcard or otherwise) is ever written before the abort.
        """
        mock_subprocess_run = MagicMock()
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", mock_subprocess_run)
        monkeypatch.setattr(
            vm_cloud,
            "_fetch_public_ip",
            lambda url: (_ for _ in ()).throw(urllib.error.URLError("network down")),
        )
        with pytest.raises(SystemExit):
            nsg_refresh("rg", "nsg", "rule")
        mock_subprocess_run.assert_not_called()


class TestVmVarsSshPublicKey:
    def test_gcp_vm_vars_omits_ssh_public_key(self) -> None:
        """GCP vm_vars must NOT include ssh_public_key — GCP uses IAP, not injected keys."""
        b = OffPlatformBackend(_off_spec(provider="gcp"), "vergil-user", "o", "r")
        vars_ = b.vm_vars(zone="us-central1-b", volume_id="vol-1")
        assert "ssh_public_key" not in vars_

    def test_azure_vm_vars_has_public_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Azure vm_vars must include ssh_public_key from ensure_keypair."""
        monkeypatch.setenv("HOME", str(tmp_path))

        def fake_keygen(priv: Path) -> None:
            priv.write_text("PRIV")
            priv.with_suffix(".pub").write_text("ssh-ed25519 AAAA azure-key")

        monkeypatch.setattr(vm_cloud, "_run_keygen", fake_keygen)
        b = OffPlatformBackend(_off_spec(provider="azure"), "vergil-user", "o", "r")
        state_dir = b.state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        vars_ = b.vm_vars(
            zone="eastus-1",
            volume_id="/subscriptions/sub/resourceGroups/rg/providers/x",
        )
        assert "ssh_public_key" in vars_
        assert vars_["ssh_public_key"] == "ssh-ed25519 AAAA azure-key"


class TestAzureTransport:
    def test_azure_transport_returns_ssh_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OffPlatformBackend.transport() returns SshTransport for azure spec."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # Mock _operator_public_ip and subprocess.run for nsg_refresh
        monkeypatch.setattr(vm_cloud, "_operator_public_ip", lambda: "203.0.113.99")
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_cloud.subprocess.run",
            lambda *a, **k: _ok(),
        )

        b = OffPlatformBackend(_off_spec(provider="azure"), "vergil-user", "o", "r")
        state_dir = b.state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        # Persist the host and volume_id that apply_vm/apply_volume would write
        (state_dir / "host").write_text("20.1.2.3")
        _azure_vol_id = (
            "/subscriptions/sub-1/resourceGroups/vrg-abc-rg/providers/Microsoft.Compute/disks/d"
        )
        (state_dir / "volume_id").write_text(_azure_vol_id)
        # Write the private key so ensure_keypair finds it
        key = state_dir / "id_ed25519"
        key.write_text("PRIV")
        (state_dir / "id_ed25519.pub").write_text("ssh-ed25519 AAAA key")

        transport = b.transport()
        assert isinstance(transport, SshTransport)
        assert transport.host == "20.1.2.3"
        assert transport.ssh_user == b.ssh_user
        assert transport.key_path == str(state_dir / "id_ed25519")

    def test_gcp_transport_still_returns_iap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OffPlatformBackend.transport() still returns IapTransport for gcp spec."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(provider="gcp"), "vergil-user", "o", "r")
        state_dir = b.state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "zone").write_text("us-central1-b")

        transport = b.transport()
        assert isinstance(transport, IapTransport)
