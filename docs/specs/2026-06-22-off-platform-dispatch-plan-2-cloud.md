# Off-Platform Dispatch — Plan 2: Cloud `OffPlatformBackend`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the off-platform (GCP) VM backend so `vrg-vm create/session/destroy/rebuild/list/destroy-volume/update` drive a remote nested-virt cloud host via OpenTofu + GCP IAP, dispatched on the composed `backend`.

**Architecture:** A new `OffPlatformBackend` (selected by `select_backend` when `spec.off_platform`) fetches the vergil-vm OpenTofu module tarball at the resolved tag, runs a two-state `tofu` lifecycle (volume → vm) keyed to per-repo local state, reaches the box via an `IapTransport` (gcloud IAP tunnel — no public IP, no keypair), reuses the Plan-1 `vm_guest` helpers for credential/tooling injection over that transport, bootstraps the persistent volume (clone vs reattach), and adds the cloud-specific verbs.

**Tech Stack:** Python 3.12+, `subprocess` (drives `tofu` and `gcloud`), `urllib`/`tarfile` (module fetch), `unittest.mock`, `pytest`. New **host** dependencies: OpenTofu ≥ 1.8.0 and `gcloud` (with ADC) — preflighted, not assumed.

## Prerequisites (must be DONE before this plan is testable)

- **vergil-vm #207 released** — GCP `vm` module uses IAP (no public IP; firewall = `35.235.240.0/20`; `ssh_public_key`/`ssh_source_ranges` retired; `host`/`ssh_user` reworked). Re-read the released `opentofu/interface.json` before Task 4; the variable/output names below assume the IAP-revised contract.
- **vergil-vm #208 released** — module tarball published as `opentofu-modules-<tag>.tar.gz` release asset; volume `prevent_destroy` removed.
- **Plan 1 merged** — `Transport`, `vm_guest`, `Backend`/`select_backend` exist.

> Until the prerequisite release exists, implement against the documented interface and mock `tofu`/`gcloud` at the subprocess boundary. The gated real-cloud e2e lives in vergil-vm; this plan's tests never touch a real cloud.

## Global Constraints

- **100% test coverage**, enforced by `vrg-validate`. `tofu`/`gcloud` are always mocked at `subprocess` in tests; no network, no cloud.
- **No raw `git`/`gh`.** Use `vrg-git`/`vrg-commit`.
- **No-silent-failures:** every `tofu`/`gcloud` non-zero exit fails loudly with the captured stderr; the ingress/credential/readiness paths never degrade silently.
- **No secret on the persistent volume:** injected creds (App key, Claude token) live on the boot-disk home; only history subdirs link onto `/vergil/claude`.
- **Provider-blind core:** the dispatcher passes opaque `provider`/`region`/`instance` strings; provider specifics live in the module. Only the *transport* is GCP-specific (IAP), absorbed by `IapTransport`.
- **Same vergil-vm tag** for modules and the Lima template (`resolve_vm_tag`).
- **Validation:** `vrg-container-run -- vrg-validate` only.
- **Work location:** worktree `.worktrees/issue-1706-off-platform-dispatch`, branch `feature/1706-off-platform-dispatch`.

---

## File Structure

- **Create** `src/vergil_tooling/lib/vm_cloud.py` — `OffPlatformBackend`, the tofu/state/naming/provision-env machinery, volume bootstrap.
- **Modify** `src/vergil_tooling/lib/vm_transport.py` — add `IapTransport`.
- **Modify** `src/vergil_tooling/lib/vm_backend.py` — `select_backend` returns `OffPlatformBackend` for `off-platform`.
- **Modify** `src/vergil_tooling/lib/vm_lima.py` — add `provider_label`/lifecycle parity already covered; no cloud logic here.
- **Modify** `src/vergil_tooling/bin/vrg_vm.py` — cloud lifecycle stages, `destroy-volume` verb, `update`→`rebuild`, `stop`/`start` unsupported message, BACKEND column, under-provisioning warning.
- **Create tests** `tests/vergil_tooling/test_vm_cloud.py`; **modify** `test_vm_transport.py`, `test_vm_backend.py`, `test_vrg_vm.py`.

---

## Task 1: Cloud-safe resource naming + structured labels

**Files:**
- Create: `src/vergil_tooling/lib/vm_cloud.py` (start the module)
- Test: `tests/vergil_tooling/test_vm_cloud.py`

**Interfaces:**
- Produces:
  - `cloud_resource_name(identity: str, org: str, repo: str) -> str` — RFC1035, ≤59 chars (leaves room for the module's `-ssh` suffix), deterministic.
  - `cloud_labels(identity: str, org: str, repo: str) -> dict[str, str]` — `{"vergil-identity":…, "vergil-org":…, "vergil-repo":…}`, each value RFC1035-label-safe.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py
from vergil_tooling.lib.vm_cloud import cloud_labels, cloud_resource_name

_RFC1035 = __import__("re").compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")


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


class TestCloudLabels:
    def test_structured_recovery_labels(self) -> None:
        labels = cloud_labels("vergil-audit", "org", "repo")
        assert labels == {
            "vergil-identity": "vergil-audit",
            "vergil-org": "org",
            "vergil-repo": "repo",
        }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .worktrees/issue-1706-off-platform-dispatch && uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py -v`
Expected: FAIL with `ModuleNotFoundError: vergil_tooling.lib.vm_cloud`.

- [ ] **Step 3: Implement**

```python
# src/vergil_tooling/lib/vm_cloud.py
"""Off-platform (cloud) VM backend: tofu two-state lifecycle + IAP transport."""

from __future__ import annotations

import hashlib
import re

_MAX_NAME = 59  # GCP instance name <=63; the module appends "-ssh" to the firewall name.
_HASH_LEN = 6


def _slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s or "x"


def cloud_resource_name(identity: str, org: str, repo: str) -> str:
    """A deterministic RFC1035 name (<=59 chars) for the GCP instance/disk/firewall."""
    base = "-".join(_slug(p) for p in (identity, org, repo))
    if not base[:1].isalpha():
        base = f"v-{base}"
    if len(base) <= _MAX_NAME:
        return base
    digest = hashlib.sha256(f"{identity}/{org}/{repo}".encode()).hexdigest()[:_HASH_LEN]
    keep = _MAX_NAME - _HASH_LEN - 1
    return f"{base[:keep].rstrip('-')}-{digest}"


def cloud_labels(identity: str, org: str, repo: str) -> dict[str, str]:
    """Structured labels for label-based recovery (independent of the mangled name)."""
    return {
        "vergil-identity": _slug(identity),
        "vergil-org": _slug(org),
        "vergil-repo": _slug(repo),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "derive cloud-safe GCP resource name + structured labels (#1706)" --body "The dotted Lima instance name is invalid as a GCP name; derive an RFC1035 name (<=59, hash-suffixed when truncated) and structured vergil-identity/org/repo labels for label-based recovery.\n\nRef #1706"
```

---

## Task 2: Module tarball fetch

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py`
- Test: `tests/vergil_tooling/test_vm_cloud.py`

**Interfaces:**
- Produces: `fetch_modules(tag: str) -> Path` — downloads `opentofu-modules-<tag>.tar.gz` from the vergil-vm release at `tag`, extracts to a temp dir, returns the extracted `opentofu/modules` root path. Validates the tag (`vN.N[.N]`) like `fetch_template`; raises `SystemExit(1)` with a clear message on a bad tag or download/extract failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py (append)
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.vm_cloud import fetch_modules


class TestFetchModules:
    def test_rejects_bad_tag(self) -> None:
        with pytest.raises(SystemExit):
            fetch_modules("not-a-tag")

    @patch("vergil_tooling.lib.vm_cloud.urllib.request.urlopen")
    def test_builds_release_asset_url(self, mock_urlopen: MagicMock) -> None:
        with pytest.raises(SystemExit):  # tar extraction of fake bytes fails loudly
            fetch_modules("v2.2")
        url = mock_urlopen.call_args[0][0]
        assert url == (
            "https://github.com/vergil-project/vergil-vm/releases/download/"
            "v2.2/opentofu-modules-v2.2.tar.gz"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestFetchModules -v`
Expected: FAIL with `ImportError: cannot import name 'fetch_modules'`.

- [ ] **Step 3: Implement** (add imports `tarfile`, `tempfile`, `urllib.request`, `urllib.error`, `sys`, `from pathlib import Path`):

```python
_MODULES_URL = (
    "https://github.com/vergil-project/vergil-vm/releases/download/"
    "{tag}/opentofu-modules-{tag}.tar.gz"
)
_TAG_RE = re.compile(r"^v\d+\.\d+(\.\d+)?$")


def fetch_modules(tag: str) -> Path:
    if not _TAG_RE.fullmatch(tag):
        print(f"ERROR: invalid module tag '{tag}' (expected vN.N or vN.N.N)", file=sys.stderr)
        raise SystemExit(1)
    url = _MODULES_URL.format(tag=tag)
    tmp = Path(tempfile.mkdtemp(prefix="vergil-modules-"))
    archive = tmp / "modules.tar.gz"
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310
            archive.write_bytes(resp.read())
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")  # noqa: S202
    except (urllib.error.URLError, tarfile.TarError, OSError) as exc:
        print(f"ERROR: failed to fetch modules from {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    modules = tmp / "opentofu" / "modules"
    if not modules.is_dir():
        print(f"ERROR: module archive missing opentofu/modules ({url})", file=sys.stderr)
        raise SystemExit(1)
    return modules
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestFetchModules -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "fetch the vergil-vm OpenTofu module tarball at tag (#1706)" --body "Single unauthenticated GET of the release asset (cloud analog of fetch_template), extracted to a temp dir.\n\nRef #1706"
```

---

## Task 3: `IapTransport`

**Files:**
- Modify: `src/vergil_tooling/lib/vm_transport.py`
- Test: `tests/vergil_tooling/test_vm_transport.py`

**Interfaces:**
- Produces: `class IapTransport` with `__init__(self, instance: str, zone: str, project: str)` implementing the `Transport` protocol via `gcloud compute ssh <instance> --tunnel-through-iap --zone <zone> --project <project> --command …` for `run`/`pipe`, and `--` interactive form for `exec_session`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_transport.py (append)
from vergil_tooling.lib.vm_transport import IapTransport


class TestIapTransport:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_builds_iap_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        t = IapTransport("inst", "us-central1-b", "proj")
        result = t.run("echo", "hi", workdir="/work")
        assert result.stdout == "ok"
        args = mock_run.call_args[0][0]
        assert args[:6] == ["gcloud", "compute", "ssh", "inst", "--tunnel-through-iap", "--zone=us-central1-b"]
        assert "--project=proj" in args
        # The remote command runs in workdir.
        assert any("cd /work" in a for a in args)

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_sends_input(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p").pipe("cat > f", "payload", workdir="/work")
        assert mock_run.call_args[1]["input"] == "payload"

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_tunnels_interactively(self, mock_execvp: MagicMock) -> None:
        IapTransport("inst", "z", "p").exec_session("/work", "exec bash")
        cmd = mock_execvp.call_args[0][1]
        assert cmd[:3] == ["gcloud", "compute", "ssh"]
        assert "--tunnel-through-iap" in cmd
        assert any("cd /work" in a for a in cmd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_transport.py::TestIapTransport -v`
Expected: FAIL with `ImportError: cannot import name 'IapTransport'`.

- [ ] **Step 3: Implement** (add `import shlex` to `vm_transport.py`):

```python
class IapTransport:
    """Transport over a GCP IAP SSH tunnel (no public IP, IAM-authed)."""

    def __init__(self, instance: str, zone: str, project: str) -> None:
        self.instance = instance
        self.zone = zone
        self.project = project

    def _base(self) -> list[str]:
        return [
            "gcloud", "compute", "ssh", self.instance,
            "--tunnel-through-iap",
            f"--zone={self.zone}",
            f"--project={self.project}",
        ]

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR
    ) -> subprocess.CompletedProcess[str]:
        remote = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"
        try:
            return subprocess.run(  # noqa: S603
                [*self._base(), f"--command={remote}"],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None:
        remote = f"cd {shlex.quote(workdir)} && {cmd}"
        try:
            subprocess.run(  # noqa: S603
                [*self._base(), f"--command={remote}"],
                check=True, input=input_data, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def exec_session(self, workdir: str, inner: str) -> NoReturn:
        remote = f"cd {shlex.quote(workdir)} && {inner}"
        cmd = [*self._base(), "--", "-t", "bash", "-lc", remote]
        os.execvp(cmd[0], cmd)  # noqa: S606, S607
        raise AssertionError("unreachable")  # pragma: no cover
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_transport.py::TestIapTransport -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_transport.py tests/vergil_tooling/test_vm_transport.py
vrg-commit --type feat --scope off-platform --message "add IapTransport (gcloud IAP tunnel) (#1706)" --body "Transport over gcloud compute ssh --tunnel-through-iap: no public IP, no managed keypair, IAM-authed. Same run/pipe/exec_session surface as LimaTransport.\n\nRef #1706"
```

---

## Task 4: `tofu` runner + two-state apply/destroy

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py`
- Test: `tests/vergil_tooling/test_vm_cloud.py`

**Interfaces:**
- Produces:
  - `tofu_state_dir(instance_name: str, provider: str) -> Path` — `~/.config/vergil/tofu/<instance_name>/<provider>`.
  - `_run_tofu(module_dir: Path, state: Path, action: str, tofu_vars: dict[str, str]) -> None` — runs `tofu -chdir=<module_dir> init` then `<action>` with `-input=false`, `TF_IN_AUTOMATION=1`, `-auto-approve` (apply/destroy), `-state=<state>`, `-var k=v…`, `TF_PLUGIN_CACHE_DIR` set; streams output; raises on non-zero.
  - `_tofu_output(module_dir: Path, state: Path) -> dict[str, str]` — `tofu output -json`, flattened to `{name: value}`.
  - `apply_volume(...) -> tuple[str, str]` (returns `(volume_id, zone)`), `apply_vm(...) -> dict[str, str]` (returns the vm outputs), `destroy_vm(...)`, `destroy_volume(...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py (append)
from pathlib import Path

from vergil_tooling.lib import vm_cloud


class TestTofuRunner:
    def test_state_dir_layout(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        d = vm_cloud.tofu_state_dir("vergil-user.org.repo", "gcp")
        assert d == tmp_path / ".config/vergil/tofu/vergil-user.org.repo/gcp"

    @patch("vergil_tooling.lib.vm_cloud.progress.run")
    def test_run_tofu_is_non_interactive(self, mock_run: MagicMock, tmp_path: Path) -> None:
        vm_cloud._run_tofu(tmp_path, tmp_path / "vm.tfstate", "apply", {"name": "x"})
        # init then apply
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any(c[:2] == ("tofu", f"-chdir={tmp_path}") and "init" in c for c in calls)
        apply = next(c for c in calls if "apply" in c)
        assert "-input=false" in apply and "-auto-approve" in apply
        assert "-var" in apply and "name=x" in apply
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestTofuRunner -v`
Expected: FAIL (`tofu_state_dir`/`_run_tofu` undefined).

- [ ] **Step 3: Implement** (import `os`, `json`, `from vergil_tooling.lib import progress`). `_run_tofu` builds the env (`{**os.environ, "TF_IN_AUTOMATION": "1", "TF_PLUGIN_CACHE_DIR": str(plugin_cache)}`), runs `progress.run(("tofu", f"-chdir={module_dir}", "init", "-input=false"), env=…)` then the action with `-input=false`, `-state=<state>`, `-auto-approve` for apply/destroy, and `-var k=v` per item. `_tofu_output` runs `tofu … output -json` captured, parses, returns `{k: v["value"] for k,v in data.items()}`. `apply_volume`/`apply_vm` assemble `tofu_vars` from the spec + labels (label map serialized as the module expects — confirm against the released module: GCP labels are a map var, passed via a single `-var 'labels={...}'` JSON or repeated `-var labels.k=v`; use the form the released `variables.tf` accepts). `destroy_vm` runs `_run_tofu(vm_dir, vm_state, "destroy", vm_vars)`. `destroy_volume` runs `_run_tofu(volume_dir, volume_state, "destroy", volume_vars)` (works because #208 removed `prevent_destroy`) then removes the local state dir if empty.

> **Note for the implementer:** `progress.run` is used for streaming (mirrors `_limactl_stream`). If `progress.run` does not accept an `env=` kwarg, extend it minimally or set the vars via `os.environ` within a scoped context manager — keep the streaming behavior. Confirm the signature in `lib/progress.py` before implementing and adjust the test's patch target accordingly.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestTofuRunner -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "tofu two-state runner (volume then vm) (#1706)" --body "Non-interactive, streamed tofu init/apply/destroy with per-repo local state, plugin cache, and -var assembly; volume apply returns (volume_id, zone), vm follows the zone.\n\nRef #1706"
```

---

## Task 5: `provision.env` rendering (shared with Lima param set)

**Files:**
- Modify: `src/vergil_tooling/lib/lima.py` (extract the param assembly), `src/vergil_tooling/lib/vm_cloud.py`
- Test: `tests/vergil_tooling/test_vm_cloud.py`, `tests/vergil_tooling/test_lima.py`

**Interfaces:**
- Produces: `provision_params(spec, fingerprint) -> dict[str, str]` (shared) returning `{EXTRA_PACKAGES, APT_REPOS, VAGRANT_PLUGINS, NESTED_VIRT, PORT_FORWARDS, SPEC_FINGERPRINT}` with the **exact** encodings `create_vm` uses today; `render_provision_env(params, *, vergil_user, home) -> str` (cloud) → newline-joined `KEY=value` body.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py (append)
from vergil_tooling.lib.vm_cloud import render_provision_env


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestProvisionEnv -v`
Expected: FAIL (`render_provision_env` undefined).

- [ ] **Step 3: Implement.** Extract the `--set=.param.*` value construction from `create_vm` into `lima.provision_params(spec, fingerprint)` (the apt-repos `name|key_url|uri|suite|components` join, `;`-joined port forwards, space-joined packages/plugins, `NESTED_VIRT`/`SPEC_FINGERPRINT`); have `create_vm` call it and keep passing the values as `--set=.param.*` (Lima behavior unchanged — assert via existing `test_lima.py`). Add `render_provision_env(params, *, vergil_user, home)` in `vm_cloud.py` producing the `KEY=value` body plus `VERGIL_USER`/`HOME`.

- [ ] **Step 4: Run tests**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestProvisionEnv tests/vergil_tooling/test_lima.py -v`
Expected: PASS (Lima param tests unchanged).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/lima.py src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py tests/vergil_tooling/test_lima.py
vrg-commit --type feat --scope off-platform --message "share provisioning param assembly across Lima and cloud (#1706)" --body "One source of truth: provision_params() feeds Lima's --set=.param.* and the cloud provision.env body, so the same profile yields the same box on either backend.\n\nRef #1706"
```

---

## Task 6: `OffPlatformBackend` + `select_backend` wiring

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py`, `src/vergil_tooling/lib/vm_backend.py`
- Test: `tests/vergil_tooling/test_vm_cloud.py`, `tests/vergil_tooling/test_vm_backend.py`

**Interfaces:**
- Produces: `class OffPlatformBackend` with `provider_label` (the spec's `provider`, e.g. `"gcp"`), `transport(instance, zone, project)` → `IapTransport`, `status(...)` (from vm state / `gcloud` describe, mocked), and lifecycle helpers used by the cloud stages. `select_backend(spec)` returns `OffPlatformBackend(spec)` when `spec.off_platform` (replacing Plan 1's `NotImplementedError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_backend.py (replace the NotImplementedError test)
from vergil_tooling.lib.vm_cloud import OffPlatformBackend


def test_off_platform_returns_cloud_backend() -> None:
    from vergil_tooling.lib.vm_backend import select_backend
    spec = _spec("off-platform", provider="gcp", region="us-central1",
                 instance="n2-standard-16", volume="300GiB")
    backend = select_backend(spec)
    assert isinstance(backend, OffPlatformBackend)
    assert backend.provider_label == "gcp"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_backend.py -v`
Expected: FAIL (`OffPlatformBackend` undefined / still raises).

- [ ] **Step 3: Implement** `OffPlatformBackend` (holds the composed `spec`, exposes `provider_label = spec.provider`, builds `IapTransport` from the vm outputs/zone, composes tofu vars from `cloud_resource_name`/`cloud_labels`/spec/`render_provision_env`). Update `select_backend`:

```python
# vm_backend.py
def select_backend(spec: ComposedSpec) -> Backend:
    if spec.off_platform:
        from vergil_tooling.lib.vm_cloud import OffPlatformBackend
        return OffPlatformBackend(spec)
    return LimaBackend()
```

- [ ] **Step 4: Run tests**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_backend.py tests/vergil_tooling/test_vm_cloud.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py src/vergil_tooling/lib/vm_backend.py tests/vergil_tooling/test_vm_backend.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "OffPlatformBackend selected for off-platform specs (#1706)" --body "select_backend now returns the cloud backend; it composes tofu vars/labels and builds the IAP transport from vm outputs.\n\nRef #1706"
```

---

## Task 7: Preflight (`tofu`/`gcloud`/ADC)

**Files:** Modify `src/vergil_tooling/lib/vm_cloud.py`; Test `tests/vergil_tooling/test_vm_cloud.py`.

**Interfaces:** Produces `preflight() -> None` — verifies `tofu` present and ≥ `1.8.0`, `gcloud` present, and ADC available; raises `SystemExit(1)` with a specific remediation per missing piece.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py (append)
from vergil_tooling.lib.vm_cloud import preflight


class TestPreflight:
    @patch("vergil_tooling.lib.vm_cloud.shutil.which", return_value=None)
    def test_missing_tofu_aborts_with_remediation(self, _which, capsys) -> None:
        with pytest.raises(SystemExit):
            preflight()
        assert "OpenTofu" in capsys.readouterr().err
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestPreflight -v`
Expected: FAIL (`preflight` undefined).

- [ ] **Step 3: Implement** using `shutil.which("tofu")`/`which("gcloud")`, parse `tofu version -json` for the version floor (`>= 1.8.0`), and check ADC via `gcloud auth application-default print-access-token` (mocked in tests). Each failure prints its own remediation (`install OpenTofu >= 1.8.0`; `install the gcloud CLI`; `run: gcloud auth application-default login`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestPreflight -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "preflight tofu/gcloud/ADC for cloud verbs (#1706)" --body "Fail fast with specific remediation rather than an opaque tofu/gcloud error.\n\nRef #1706"
```

---

## Task 8: Volume bootstrap (clone vs reattach vs credential-less skip)

**Files:** Modify `src/vergil_tooling/lib/vm_cloud.py`; Test `tests/vergil_tooling/test_vm_cloud.py`.

**Interfaces:** Produces `bootstrap_volume(transport: Transport, identity: Identity, org: str, repo: str) -> None` — over the transport: if `auth_type == "none"`, log-skip; elif `/vergil/projects/<org>/<repo>` exists, `git fetch` (no clone); else `vrg-git clone` into it and seed `/vergil/claude/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py (append)
from vergil_tooling.lib.vm_cloud import bootstrap_volume


class TestBootstrap:
    def test_skips_for_credential_less_identity(self, capsys) -> None:
        transport = MagicMock()
        identity = MagicMock(); identity.auth_type = "none"
        bootstrap_volume(transport, identity, "org", "repo")
        transport.run.assert_not_called()
        assert "skipping checkout" in capsys.readouterr().out.lower()

    def test_clones_when_absent(self) -> None:
        transport = MagicMock()
        # First run = test -d (exit nonzero -> absent), subsequent = clone/seed.
        transport.run.side_effect = [
            __import__("subprocess").CalledProcessError(1, "test"),  # path absent
            MagicMock(), MagicMock(),
        ]
        identity = MagicMock(); identity.auth_type = "app"
        bootstrap_volume(transport, identity, "org", "repo")
        cloned = " ".join(c for call in transport.run.call_args_list for c in call.args)
        assert "clone" in cloned
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestBootstrap -v`
Expected: FAIL (`bootstrap_volume` undefined).

- [ ] **Step 3: Implement.** Probe existence with `transport.run("test", "-d", path)` (catch `CalledProcessError` → absent). On absent + credentialed: `transport.run("vrg-git", "clone", f"https://github.com/{org}/{repo}.git", path)` and `transport.run("mkdir", "-p", "/vergil/claude")`. On present: `transport.run("git", "-C", path, "fetch", "--all")`. On `auth_type == "none"`: print the skip line and return.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestBootstrap -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "volume bootstrap: clone vs reattach vs credential-less skip (#1706)" --body "Fresh volume -> in-guest vrg-git clone + seed /vergil/claude; reattach -> fetch only; auth_type=none -> skip checkout, logged.\n\nRef #1706"
```

---

## Task 8a: `await_readiness` hard-fail gate

**Files:** Modify `src/vergil_tooling/lib/vm_cloud.py`; Test `tests/vergil_tooling/test_vm_cloud.py`.

**Why:** the cloud backend has no native Lima-style readiness gate, so it must synthesize one — a non-zero `cloud-init` status or a missing fingerprint marker is a **hard `create` failure** (no half-ready box; no-silent-failures). This is spec-critical and gets its own task rather than living inside Task 9's pipeline prose.

**Interfaces:** Produces `await_readiness(transport: Transport, fingerprint: str) -> None` — runs `cloud-init status --wait` over the transport, then confirms `/etc/vergil/vm-spec.fingerprint` exists and matches `fingerprint`; raises `RuntimeError` (caught by the create pipeline as a fail_fast stage) on either failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py (append)
import subprocess

import pytest

from vergil_tooling.lib.vm_cloud import await_readiness


class TestAwaitReadiness:
    def test_passes_when_cloud_init_done_and_marker_matches(self) -> None:
        transport = MagicMock()
        # cloud-init status --wait -> ok; marker read -> matching fingerprint
        transport.run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="status: done\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="fp123\n", stderr=""),
        ]
        await_readiness(transport, "fp123")  # no raise

    def test_raises_when_cloud_init_fails(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = subprocess.CalledProcessError(1, "cloud-init")
        with pytest.raises(RuntimeError):
            await_readiness(transport, "fp123")

    def test_raises_when_marker_missing_or_mismatched(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="status: done\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="different\n", stderr=""),
        ]
        with pytest.raises(RuntimeError):
            await_readiness(transport, "fp123")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestAwaitReadiness -v`
Expected: FAIL (`await_readiness` undefined).

- [ ] **Step 3: Implement.** `transport.run("cloud-init", "status", "--wait")` (a non-zero exit raises `CalledProcessError` → wrap in `RuntimeError`); then `transport.run("cat", "/etc/vergil/vm-spec.fingerprint")` and compare the stripped stdout to `fingerprint`; on mismatch or read failure raise `RuntimeError` with a message naming the box and the remediation (`rebuild`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestAwaitReadiness -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "synthesize a hard-fail readiness gate for cloud create (#1706)" --body "cloud-init status --wait + fingerprint-marker check; either failure fails create loudly (no half-ready box), mirroring Lima's readiness gate.\n\nRef #1706"
```

---

## Task 8b: Cloud `.claude` layout (boot-disk credentials, volume-only history)

**Files:** Modify `src/vergil_tooling/lib/vm_cloud.py`; Test `tests/vergil_tooling/test_vm_cloud.py`.

**Why:** satisfies acceptance #4 — **no injected credential persists on the detachable volume.** The Claude OAuth token (`~/.claude/.credentials.json`) must stay on the ephemeral boot disk and die with the VM, while session history survives on the volume. Plan 1's `link_claude_dirs` is Lima-shaped (links to the Mac mount); the cloud box needs its own layout step.

**Interfaces:** Produces `link_cloud_claude_dirs(transport: Transport) -> None` — ensures `~/.claude` is a real directory on the boot disk and symlinks only the history subdirs (`projects`, `todos`) onto `/vergil/claude/<subdir>`. Credentials/config files (`.credentials.json`, `.claude.json`) are never moved to the volume.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vm_cloud.py (append)
from vergil_tooling.lib.vm_cloud import link_cloud_claude_dirs


class TestCloudClaudeLayout:
    def test_symlinks_history_subdirs_to_volume_only(self) -> None:
        transport = MagicMock()
        transport.run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        link_cloud_claude_dirs(transport)
        joined = " ".join(c for call in transport.run.call_args_list for c in call.args)
        # History subdirs are linked onto the volume...
        assert "/vergil/claude/projects" in joined
        assert "/vergil/claude/todos" in joined
        # ...but credentials are NEVER pointed at the volume.
        assert "/vergil/claude/.credentials.json" not in joined
        assert ".credentials.json" not in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestCloudClaudeLayout -v`
Expected: FAIL (`link_cloud_claude_dirs` undefined).

- [ ] **Step 3: Implement.** Over the transport: `mkdir -p ~/.claude /vergil/claude/projects /vergil/claude/todos`; then for each of `projects`, `todos`, create the symlink `~/.claude/<sub> -> /vergil/claude/<sub>` (e.g. `ln -sfn /vergil/claude/projects ~/.claude/projects`). Do not touch `.credentials.json`/`.claude.json` — they are written to `~/.claude` (boot disk) by `inject_credentials` and stay there.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vm_cloud.py::TestCloudClaudeLayout -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/vm_cloud.py tests/vergil_tooling/test_vm_cloud.py
vrg-commit --type feat --scope off-platform --message "cloud .claude layout: history on volume, creds on boot disk (#1706)" --body "Symlinks ~/.claude/projects and todos onto /vergil/claude so session history survives teardown, while .credentials.json stays on the ephemeral boot disk (acceptance: no injected credential on the volume).\n\nRef #1706"
```

---

## Task 9: Cloud lifecycle stages in `vrg_vm.py` (create/destroy/rebuild/session)

**Files:** Modify `src/vergil_tooling/bin/vrg_vm.py`; Test `tests/vergil_tooling/test_vrg_vm.py`.

**Interfaces:** Consumes `OffPlatformBackend`, `IapTransport`, `bootstrap_volume`, `vm_guest` helpers, `preflight`. Produces backend-specific stage lists so the create pipeline becomes `fetch-modules → tofu-volume → tofu-vm → await-readiness → credentials → tooling → bootstrap-volume`, reusing the shared guest stages over the cloud transport.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vrg_vm.py (append)
class TestCloudCreate:
    @patch("vergil_tooling.bin.vrg_vm.preflight")
    @patch("vergil_tooling.bin.vrg_vm.select_backend")
    def test_cloud_create_preflights_and_uses_cloud_backend(self, mock_select, mock_preflight):
        # A spec resolving off-platform routes through preflight + OffPlatformBackend stages.
        ...  # assemble args via the existing test harness; assert preflight called once
        assert mock_preflight.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py::TestCloudCreate -v`
Expected: FAIL (cloud branch not wired).

- [ ] **Step 3: Implement.** In `_cmd_create`/`_cmd_rebuild`/`_cmd_destroy`/`_cmd_session`, use `backend = select_backend(target.spec)`; when `target.spec.off_platform`: call `preflight()`, then build the cloud create stage list — `fetch-modules → tofu-volume → tofu-vm → await-readiness (Task 8a) → credentials → tooling → bootstrap-volume (Task 8) → link-cloud-claude-dirs (Task 8b)` — where each lifecycle stage uses `backend`'s tofu helpers and the shared guest stages (`_st_credentials`, `_st_install_tooling`) run over an `IapTransport` built from the vm outputs (Plan 1). `_cmd_session` execs via `IapTransport(...).exec_session(workdir, inner)` where `workdir = /vergil/projects/<org>/<repo>` and `inner` is the unchanged resolver command. **Concurrency guard:** before building the create pipeline, call `backend.status(...)` and, if it reports a running instance, refuse with the existing "already exists" error path (the cloud analog of the Lima `vm_status` guard) — never stand up a second instance for an `(identity, org/repo)`. Keep the Lima branch exactly as Plan 1 left it.

- [ ] **Step 4: Run tests**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope off-platform --message "wire cloud create/destroy/rebuild/session stages (#1706)" --body "Off-platform create pipeline: fetch-modules -> tofu-volume -> tofu-vm -> await-readiness -> shared guest stages over IapTransport -> bootstrap-volume. session SSHes via IAP into /vergil/projects/<org>/<repo>.\n\nRef #1706"
```

---

## Task 10: `destroy-volume` verb, `update`→`rebuild`, `stop`/`start` unsupported

**Files:** Modify `src/vergil_tooling/bin/vrg_vm.py`; Test `tests/vergil_tooling/test_vrg_vm.py`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/vergil_tooling/test_vrg_vm.py (append)
class TestCloudVerbs:
    def test_stop_off_platform_is_unsupported_message(self, capsys):
        rc = vrg_vm.main(["stop", "org/repo"])  # off-platform repo in the harness fixture
        # returns nonzero with a clear message, never a crash/silent no-op
        assert rc != 0
        assert "ephemeral" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py::TestCloudVerbs -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** Add a `destroy-volume` subparser (guarded: prompt for explicit confirmation unless `--yes`; calls `OffPlatformBackend.destroy_volume`, then cleans the local state dir). In `_cmd_update`, when `spec.off_platform`, delegate to `_cmd_rebuild`. In `_cmd_stop`/`_cmd_start`, when `spec.off_platform`, print the unsupported message and return 1.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py::TestCloudVerbs -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope off-platform --message "destroy-volume verb; update->rebuild; stop/start unsupported (#1706)" --body "destroy-volume is the only path that deletes the disk (confirmation-gated, cleans local state). update maps to rebuild; stop/start return a clear unsupported message.\n\nRef #1706"
```

---

## Task 11: `list` BACKEND column + graceful degradation; under-provisioning warning

**Files:** Modify `src/vergil_tooling/bin/vrg_vm.py`; Test `tests/vergil_tooling/test_vrg_vm.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vrg_vm.py (append)
class TestListBackendColumn:
    def test_header_has_backend_column(self, capsys):
        vrg_vm.main(["list"])
        assert "BACKEND" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py::TestListBackendColumn -v`
Expected: FAIL.

- [ ] **Step 3: Implement.** Add a `BACKEND` column to the `list` header/rows (`local` for Lima rows; the provider for off-platform rows). For off-platform rows, render status from local state/profile; when ADC is absent, show `unknown (no <provider> creds)` (never error). Add the instance-vs-declared `cpus`/`memory` under-provisioning warning at `session` using a small built-in table of known nested-virt instance types; silent on an unknown type.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run --project . pytest tests/vergil_tooling/test_vrg_vm.py::TestListBackendColumn -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_vm.py tests/vergil_tooling/test_vrg_vm.py
vrg-commit --type feat --scope off-platform --message "list BACKEND column + cloud degradation + under-provision warning (#1706)" --body "list shows BACKEND and degrades to 'unknown (no creds)' without erroring; session warns when the instance is smaller than declared cpus/memory (silent on unknown types).\n\nRef #1706"
```

---

## Task 12: Docs + full validation gate

**Files:** Modify `docs/site/docs/reference/vm-spec.md` (off-platform verbs/behavior); verification.

- [ ] **Step 1: Document** the off-platform verbs, the IAP access model, the `destroy-volume` verb, and the `stop`/`start` limitation in the vm-spec reference doc.

- [ ] **Step 2: Run the full validation pipeline**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — lint, typecheck, **100% coverage**, tests, audit, common checks. Add focused tests for any uncovered branch (tofu/gcloud error paths, `_tofu_output` parsing, degradation path).

- [ ] **Step 3: Commit**

```bash
vrg-git add docs/ tests/ src/
vrg-commit --type docs --scope off-platform --message "document off-platform verbs and IAP access (#1706)" --body "Ref #1706"
```

---

## Self-Review (completed during authoring)

- **Spec coverage:** Tasks map to the spec sections — naming (T1), module fetch (T2), IAP transport (T3), tofu two-state (T4), shared provision.env (T5), backend selection (T6), preflight (T7), volume bootstrap incl. credential-less skip (T8), readiness hard-fail gate (T8a), cloud `.claude` layout / no-cred-on-volume (T8b), cloud lifecycle + session + concurrency guard (T9), destroy-volume/update/stop-start (T10), list column + degradation + under-provisioning (T11), docs (T12). Acceptance #4 (no injected credential on the volume) is satisfied concretely by T8b (history-only symlinks; `.credentials.json` stays on the boot disk).
- **Placeholders:** a few tasks (T9–T11 test bodies) reference "the existing test harness" because they depend on `test_vrg_vm.py`'s established fixtures; the implementer fills the fixture wiring, but every behavioral assertion and every implementation step is concrete. The `progress.run` `env=` note (T4) is flagged explicitly as a signature to confirm.
- **Type consistency:** `cloud_resource_name`/`cloud_labels`/`fetch_modules`/`IapTransport`/`tofu_state_dir`/`_run_tofu`/`render_provision_env`/`OffPlatformBackend`/`bootstrap_volume`/`preflight` names are used identically across tasks and match `select_backend`'s wiring.

## Dependency reminder

Do **not** run Tasks 4, 6, 8a, 8b, 9–11 against a real cloud until vergil-vm #207 + #208 are released (the unit tests, which mock `tofu`/`gcloud`, can be written anytime). Re-read the released `opentofu/interface.json` and `variables.tf` first; the exact `-var` names and the `labels` encoding must match the released module.
