from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import vm_cloud
from vergil_tooling.lib.vm_cloud import (
    FALLBACK_SHAPES,
    NESTED_VIRT_FAMILIES,
    OffPlatformBackend,
    apply_vm,
    apply_vm_with_zone_fallback,
    apply_volume,
    await_readiness,
    bootstrap_volume,
    cloud_labels,
    cloud_resource_name,
    destroy_vm,
    destroy_volume,
    fetch_modules,
    instance_fallback_candidates,
    is_zone_capacity_error,
    link_cloud_claude_dirs,
    off_platform_transport,
    parse_vm_machine_type,
    parse_volume_state,
    preflight,
    provision_params,
    read_zone,
    region_zones,
    render_provision_env,
    tofu_state_dir,
    zone_to_region,
)
from vergil_tooling.lib.vm_spec import ComposedSpec, spec_fingerprint, state_slug
from vergil_tooling.lib.vm_transport import IapTransport

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
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
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
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
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
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
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
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        assert b.status() == ""

    def test_status_no_creds_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        b = OffPlatformBackend(_off_spec(), "vergil-user", "o", "r")
        (b.state_dir() / "zone").write_text("us-central1-b")
        sub = MagicMock(side_effect=subprocess.CalledProcessError(1, "gcloud"))
        monkeypatch.setattr("vergil_tooling.lib.vm_cloud.subprocess.run", sub)
        assert b.status() == ""

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
