# Azure Off-Platform Provider — vergil-tooling Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This plan lives in vergil-vm (next to the authoritative spec) but EXECUTES in vergil-tooling.** At execution time, copy it into the vergil-tooling repo's `docs/plans/` and run it from a vergil-tooling worktree on the companion issue's feature branch.

**Goal:** Teach the `vrg-vm` off-platform backend to drive the Azure OpenTofu modules — selecting the provider, reaching the VM over a public-IP/NSG SSH transport, generating the keypair, and giving Azure its own credential, capacity/zone, read-enumerate, and lifecycle-parity paths — without regressing GCP.

**Architecture:** A small **provider-strategy** object isolates every GCP-specific seam (transport, preflight, capacity detection, zone enumeration, module path) behind one interface, with a GCP and an Azure implementation. The guest-side code (`vm_guest.py`) and dispatch (`vm_backend.py`) stay provider-neutral and unchanged.

**Tech Stack:** Python 3.12, pytest, `subprocess` over the `az` / `tofu` CLIs, the `azurerm` OpenTofu provider, `cryptography`/`ssh-keygen` for the keypair.

## Global Constraints

- **Dependency: vergil-vm #250 modules** must be released (the modules are fetched from the v-tag archive) before a real Azure `apply` works. Unit tests here mock the CLIs and need no cloud.
- **#1831 (named instances) is LANDED** — `cloud_resource_name(slug)` and `OffPlatformBackend` key `self.name`/`self.state_key` off `self.slug` (`self.instance_name` → `self.slug`). This plan is **unblocked and executable**. Still re-read `vm_cloud.py`/`vm_backend.py`/`vm_transport.py` before coding, since the file evolves (#1813/#1836/#1804 landed recently).
- **Test layout:** the suite lives under `tests/vergil_tooling/` (e.g. `tests/vergil_tooling/test_vm_cloud.py`, `test_vm_transport.py`); Azure-strategy tests go in a new `tests/vergil_tooling/test_vm_provider.py`. (Tasks 1–3 below say `tests/lib/...`; use the real `tests/vergil_tooling/...` path.)
- **Validation:** `vrg-container-run -- vrg-validate` is the only sanctioned validation command; per-test `pytest ...::name -v` invocations in the tasks are the RED/GREEN inner loop only — run `vrg-validate` before claiming a task done.
- **Git:** `vrg-git` / `vrg-commit`; feature-branch worktree; commit bodies end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **No GCP regression:** every change is provider-branched; the GCP path must behave byte-for-byte as before. The existing GCP suite imports concrete names from `vm_cloud` (`region_zones`, `is_zone_capacity_error`, `instance_fallback_candidates`, `NESTED_VIRT_FAMILIES`, `FALLBACK_SHAPES`, `preflight`, `parse_volume_state`) — **retain those module-level names as `GcpStrategy()` delegations** (or default `provider="gcp"`) so GCP tests stay green unchanged.
- **No silent non-GCP degradation:** the current `if provider != "gcp"` *skip* (in `vrg_vm._volume_live_status`) must become a real Azure branch (Task 6), never a silent no-op.

> **Code-completeness note.** All seven tasks are line-level (#1831 has landed, so the target functions are stable). Tasks 1–3 cover the module-path threading, `SshTransport`, and keypair/NSG refresh; Tasks 4–7 cover the provider-strategy seam, capacity resilience (zone **and** instance-family fallback), the read & enumerate surface, and lifecycle parity — each in red/green/refactor form with real `vm_cloud.py`/`vrg_vm.py` line anchors. Some Azure specifics (the VM-size grammar, the nested-virt family ladder, the power-state mapping) are marked "verify at implementation" against current Azure docs — these are genuine external facts to confirm, not deferred code.

### GCP-coupling map (orientation for Tasks 4–7)

Every provider-specific seam found in the current source, so the strategy extraction is exhaustive:

| Seam | Location | Azure form |
|---|---|---|
| Module path literal `"gcp"` | `vm_cloud.py:622,665,707,721` (`apply_*`/`destroy_*`) | `strategy.module_segment` → `azure` (Task 1 threads it; Task 4 moves it into the strategy) |
| `GOOGLE_CLOUD_PROJECT` in `_tofu_env` | `vm_cloud.py:548–558` (called by every tofu run) | `ARM_SUBSCRIPTION_ID` via `strategy.tofu_env()` (Task 4) |
| `preflight` gcloud+ADC | `vm_cloud.py:213–230` | `az` + `az account get-access-token` (Task 4) |
| `off_platform_transport` builds `IapTransport`, no `provider` arg | `vm_cloud.py:1027`; callers `vrg_vm.py:1239,2069` | grows `provider`; `SshTransport` for azure (Task 7) |
| `_cloud_status` / `_volume_live_status` shell `gcloud` | `vrg_vm.py:1623,1971` (+ `provider != "gcp"` skip at 1980) | `az vm get-instance-view` / `az disk show` (Task 6) |
| `region_zones` / `is_zone_capacity_error` | `vm_cloud.py:731,725` | `az vm list-skus` zones / Azure capacity strings (Task 5) |
| Family ladder `NESTED_VIRT_FAMILIES`/`FALLBACK_SHAPES`/`instance_fallback_candidates` (#1836) | `vm_cloud.py:782,792,795`; pinned by `test_vm_cloud.py:1588` | Azure nested-virt family ladder + Azure size grammar (Task 5) |
| `VolumeState` parses `google_compute_disk.data` | `vm_cloud.py:964,977–980` | `azurerm_managed_disk.data`, `disk_size_gb`/`tags` attrs (Task 6) |
| `vm_vars` lacks `ssh_public_key` | `vm_cloud.py:1120` | add `ssh_public_key` for azure (Task 4) |

---

### Task 1: Parameterize the module path on `spec.provider`

The four `modules_root / "gcp" / …` literals are the only thing pinning the dispatcher to GCP modules.

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py` — `apply_volume`, `apply_vm`, `destroy_vm`, `destroy_volume` (the `modules_root / "gcp" / …` lines)
- Test: `tests/lib/test_vm_cloud.py` (or the existing cloud test module)

**Interfaces:**
- Consumes: `OffPlatformBackend.provider_label` (already set from `spec.provider`).
- Produces: module paths resolved as `modules_root / provider / {"vm","volume"}`.

- [ ] **Step 1: Write the failing test**

```python
def test_module_path_uses_spec_provider(tmp_path):
    # apply_* must resolve the module dir under the spec's provider, not a hardcoded "gcp".
    from vergil_tooling.lib import vm_cloud
    calls = []
    # Stub _run_tofu / _tofu_output to capture the module_dir the function chose.
    vm_cloud._run_tofu = lambda module_dir, *a, **k: calls.append(module_dir)
    vm_cloud._tofu_output = lambda *a, **k: {"volume_id": "x", "zone": "1"}
    vm_cloud.apply_volume(  # adjust kwargs to the current signature
        tmp_path, tmp_path, name="n", region="eastus", size_gib=64, labels={}, zone="1",
        provider="azure",
    )
    assert calls[0] == tmp_path / "azure" / "volume"
```

- [ ] **Step 2: Run it — fails (hardcoded "gcp" or missing `provider` kwarg)**

Run: `pytest tests/lib/test_vm_cloud.py::test_module_path_uses_spec_provider -v`
Expected: FAIL.

- [ ] **Step 3: Thread `provider` through the four functions**

In `vm_cloud.py`, add a `provider: str` parameter (keyword) to `apply_volume`, `apply_vm`, `destroy_vm`, `destroy_volume`, and replace each `modules_root / "gcp" / "volume"` / `"vm"` with `modules_root / provider / "volume"` / `"vm"`. Update the call sites in `vrg_vm.py` to pass `backend.provider_label`.

- [ ] **Step 4: Run the test + the existing GCP cloud tests — all pass**

Run: `pytest tests/lib/test_vm_cloud.py -v`
Expected: PASS (new test green; GCP tests unaffected — they pass `provider="gcp"`).

- [ ] **Step 5: Commit**

```bash
vrg-commit --type refactor --scope off-platform \
  --message "resolve the off-platform module path from spec.provider (#<tooling-issue>)" \
  --body "Replace the four hardcoded gcp module-path literals with the spec's provider so the dispatcher can drive azure/{vm,volume}.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `SshTransport` — plain ssh over the public IP

Mirror `IapTransport`'s surface exactly so all of `vm_guest.py` is reused. Public IP + private key; the NSG refresh is Task 3.

**Files:**
- Modify: `src/vergil_tooling/lib/vm_transport.py` (add `SshTransport`)
- Test: `tests/lib/test_vm_transport.py`

**Interfaces:**
- Consumes: `host` (public IP), `ssh_user`, `key_path` (the persisted private key from Task 3).
- Produces: a `Transport` (`run`/`pipe`/`popen`/`exec_session`) the backend returns for Azure.

- [ ] **Step 1: Write the failing test**

```python
def test_ssh_transport_base_command(monkeypatch):
    from vergil_tooling.lib.vm_transport import SshTransport
    captured = {}
    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        import subprocess
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr("subprocess.run", fake_run)
    t = SshTransport(host="20.1.2.3", ssh_user="ubuntu", key_path="/k/id_ed25519")
    t.run("echo", "hi", workdir="/vergil")
    argv = captured["argv"]
    assert argv[0] == "ssh"
    assert "ubuntu@20.1.2.3" in argv
    assert "/k/id_ed25519" in argv                    # -i <key>
    assert any("cd /vergil && echo hi" in a for a in argv)
    assert "StrictHostKeyChecking=accept-new" in " ".join(argv)
```

- [ ] **Step 2: Run it — fails (SshTransport undefined)**

Run: `pytest tests/lib/test_vm_transport.py::test_ssh_transport_base_command -v`
Expected: FAIL with ImportError.

- [ ] **Step 3: Implement `SshTransport`** (append to `vm_transport.py`)

```python
class SshTransport:
    """Transport over plain ``ssh`` to a public-IP host (Azure off-platform).

    The vm module exposes a routable public IP (``host``) and the box is reached as
    ``ssh -i <key> <ssh_user>@<host>``. The NSG that fronts port 22 is locked to the
    operator's current /32, refreshed at session start (see vm_cloud.nsg_refresh).
    Same run/pipe/popen/exec_session surface as the other transports.
    """

    def __init__(self, host: str, ssh_user: str, key_path: str) -> None:
        self.host = host
        self.ssh_user = ssh_user
        self.key_path = key_path

    def _base(self) -> list[str]:
        return [
            "ssh",
            "-i", self.key_path,
            # accept-new: trust the key on first contact (the box is freshly created and
            # its host key is unknown), but still detect a changed key thereafter.
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=~/.config/vergil/known_hosts",
            f"{self.ssh_user}@{self.host}",
        ]

    def run(
        self, *args: str, workdir: str = _DEFAULT_WORKDIR, quiet: bool = False
    ) -> subprocess.CompletedProcess[str]:
        remote = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"
        try:
            return subprocess.run(  # noqa: S603
                [*self._base(), remote],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr and not quiet:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def pipe(self, cmd: str, input_data: str, *, workdir: str = _DEFAULT_WORKDIR) -> None:
        remote = f"cd {shlex.quote(workdir)} && {cmd}"
        try:
            subprocess.run(  # noqa: S603
                [*self._base(), remote],
                check=True,
                input=input_data,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise

    def popen(self, *args: str, workdir: str = _DEFAULT_WORKDIR) -> subprocess.Popen[str]:
        remote = f"cd {shlex.quote(workdir)} && {shlex.join(args)}"
        return subprocess.Popen(  # noqa: S603
            [*self._base(), remote],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def exec_session(self, workdir: str, inner: str) -> NoReturn:
        remote = f"cd {workdir} && {inner}"
        cmd = [*self._base(), "-t", remote]
        os.execvp(cmd[0], cmd)  # noqa: S606, S607
```

- [ ] **Step 4: Run the test — passes**

Run: `pytest tests/lib/test_vm_transport.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-commit --type feat --scope off-platform \
  --message "add SshTransport for the azure public-IP backend (#<tooling-issue>)" \
  --body "Plain-ssh transport mirroring IapTransport's surface, so all guest-side credential/provisioning code is reused unchanged. NSG refresh lands separately.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Keypair generation/persistence + NSG source-rule refresh

The `host` is public; the box must be reachable only from the operator's current IP, and the SSH keypair must exist before the VM is created (its public half is passed to the module as `ssh_public_key`).

**Files:**
- Modify: `src/vergil_tooling/lib/vm_cloud.py` (keypair helper; `vm_vars` to include `ssh_public_key`; an `nsg_refresh` helper)
- Test: `tests/lib/test_vm_cloud.py`

**Interfaces:**
- Produces:
  - `ensure_keypair(state_dir: Path) -> tuple[Path, str]` → `(private_key_path, public_key_openssh)`, generated once per instance state dir, reused thereafter.
  - `vm_vars(...)` gains `"ssh_public_key": <public_key_openssh>` (Azure) / `""` (GCP).
  - `nsg_refresh(resource_group: str, nsg_name: str, rule: str) -> None` → sets the rule's source to the operator's current public IP via `az network nsg rule update`.

- [ ] **Step 1: Write the failing tests**

```python
def test_ensure_keypair_is_idempotent(tmp_path, monkeypatch):
    from vergil_tooling.lib import vm_cloud
    monkeypatch.setattr(vm_cloud, "_run_keygen", lambda priv: (priv.write_text("PRIV"),
                                                               (priv.with_suffix(".pub")).write_text("ssh-ed25519 AAAA")))
    p1, pub1 = vm_cloud.ensure_keypair(tmp_path)
    p2, pub2 = vm_cloud.ensure_keypair(tmp_path)   # second call must NOT regenerate
    assert p1 == p2 and pub1 == pub2 == "ssh-ed25519 AAAA"

def test_nsg_refresh_sets_current_ip(monkeypatch):
    from vergil_tooling.lib import vm_cloud
    monkeypatch.setattr(vm_cloud, "_operator_public_ip", lambda: "203.0.113.5")
    seen = {}
    monkeypatch.setattr("subprocess.run", lambda argv, **k: seen.setdefault("argv", argv) or _ok())
    vm_cloud.nsg_refresh("n-rg", "n-nsg", "ssh-operator")
    argv = seen["argv"]
    assert "az" == argv[0] and "203.0.113.5/32" in " ".join(argv)
    assert "--source-address-prefixes" in argv
```

- [ ] **Step 2: Run — both fail**

Run: `pytest tests/lib/test_vm_cloud.py -k "keypair or nsg_refresh" -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

- `ensure_keypair(state_dir)`: if `state_dir/id_ed25519` exists, read `.pub` and return; else `ssh-keygen -t ed25519 -N "" -f <state_dir>/id_ed25519` (via a `_run_keygen` seam) and return the path + the `.pub` contents stripped. ed25519 (short public key, modern default).
- `_operator_public_ip()`: query a stable echo endpoint (e.g. `curl -fsS https://api.ipify.org`) with a short timeout; raise a clear error on failure (no silent fallback to a wide-open rule).
- `nsg_refresh(resource_group, nsg_name, rule)`: `az network nsg rule update -g <rg> --nsg-name <nsg> -n <rule> --source-address-prefixes <ip>/32`.
- In `vm_vars`, add `"ssh_public_key"`: the Azure path passes `ensure_keypair(...)`'s public key; the GCP path passes `""`. (This is where the provider strategy from Task 4 chooses.)

- [ ] **Step 4: Wire `nsg_refresh` into the Azure transport acquisition**

In the backend's `transport()` for Azure: derive `resource_group` from the persisted volume_id (same parse as the module), call `nsg_refresh(...)` before returning the `SshTransport`. (GCP's `transport()` is unchanged — IAP needs no refresh.)

- [ ] **Step 5: Run the tests + GCP regression**

Run: `pytest tests/lib/test_vm_cloud.py -v`
Expected: PASS; GCP `vm_vars` now emits `ssh_public_key=""` (assert this in the existing GCP test).

- [ ] **Step 6: Commit**

```bash
vrg-commit --type feat --scope off-platform \
  --message "azure keypair + session-time NSG source refresh (#<tooling-issue>)" \
  --body "Generate/persist an ed25519 keypair per instance, pass the public key to the module as ssh_public_key, and rewrite the NSG inbound-22 source to the operator's current /32 before each session (the roaming fix replacing IAP).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Seam shape (decide in the Task 4 brainstorm before coding)

A hybrid: a `Provider` strategy object owns provider-specific *values and helpers* (module
segment, tofu-env vars, zone enumeration, capacity regex, family ladder, status mapping,
transport construction, volume-state disk type), and `OffPlatformBackend` (via
`self.strategy = strategy_for(spec.provider)`) plus the free functions consume it. The
existing module-level names in `vm_cloud` stay as thin `GcpStrategy()` delegations so the
GCP suite imports keep working. Confirm this seam against current source first.

---

### Task 4: Provider-strategy seam + Azure preflight/credentials

**Files:**
- Create `src/vergil_tooling/lib/vm_provider.py` — `Provider` protocol; `GcpStrategy`, `AzureStrategy`; `strategy_for(provider: str) -> Provider`.
- Modify `src/vergil_tooling/lib/vm_cloud.py` — `preflight` (split into `GcpStrategy.preflight`/`AzureStrategy.preflight`, with the shared OpenTofu-version check staying in `vm_cloud.preflight(provider)`); `_resolve_project`→`GcpStrategy.tofu_env`; `_tofu_env()`→`_tofu_env(strategy)`; `_run_tofu`/`_tofu_output`/`apply_volume`/`apply_vm`/`destroy_vm`/`destroy_volume` to derive the provider module segment via the strategy (subsuming Task 1's interim `provider` kwarg); `OffPlatformBackend.__init__`/`vm_vars` (add `ssh_public_key` for Azure); `off_platform_transport(name, state_dir, provider)`.
- Modify `src/vergil_tooling/bin/vrg_vm.py` — `vm_cloud.preflight()` call sites (lines 892, 2224) pass `target.spec.provider`.

**Interfaces:**
- Consumes: `spec.provider` ("gcp"|"azure"); `os.environ["AZURE_SUBSCRIPTION_ID"]`; `az account show --query id -o tsv`.
- Produces:
  - `strategy_for(provider: str) -> Provider`
  - `Provider` protocol (verify exact members at implementation; proposed):
    ```python
    class Provider(Protocol):
        name: str                      # "gcp" | "azure"
        module_segment: str            # "gcp" | "azure" (path under modules_root)
        def preflight(self) -> None: ...
        def tofu_env(self) -> dict[str, str]: ...           # GOOGLE_CLOUD_PROJECT vs ARM_SUBSCRIPTION_ID
        def region_zones(self, region: str) -> list[str]: ...
        def is_zone_capacity_error(self, exc: subprocess.CalledProcessError) -> bool: ...
        def instance_fallback_candidates(self, requested: str) -> list[str]: ...
        def transport(self, name: str, state_dir: Path, ssh_user: str) -> Transport: ...
        def status(self, name: str, state_dir: Path) -> str: ...
        def volume_disk_type(self) -> str: ...   # "google_compute_disk" vs "azurerm_managed_disk"
    ```
  - `AzureStrategy.tofu_env()` resolves subscription: `os.environ.get("AZURE_SUBSCRIPTION_ID")` else `subprocess.run(["az","account","show","--query","id","-o","tsv"], check=True)` stripped; abort `SystemExit(1)` if empty (mirror `_resolve_project` at `vm_cloud.py:538`), inject `{"ARM_SUBSCRIPTION_ID": sub, "TF_IN_AUTOMATION": "1", "TF_PLUGIN_CACHE_DIR": ...}`.
  - `OffPlatformBackend.vm_vars` returns the existing keys plus `"ssh_public_key"` **only when provider is azure** (the GCP module rejects unknown vars; the Azure module variable name is exactly `ssh_public_key`).

#### RED
- **Test file:** `tests/vergil_tooling/test_vm_provider.py` (new), plus additions to `tests/vergil_tooling/test_vm_cloud.py`.
- **Tests + assertions:**
  1. `test_vm_provider.py::TestStrategyFor::test_returns_gcp_strategy` — `strategy_for("gcp").name == "gcp"` and `.module_segment == "gcp"`.
  2. `test_vm_provider.py::TestStrategyFor::test_returns_azure_strategy` — `strategy_for("azure").module_segment == "azure"`.
  3. `test_vm_provider.py::TestStrategyFor::test_unknown_provider_aborts` — `with pytest.raises(SystemExit): strategy_for("aws")` (fail-closed; no silent default to gcp).
  4. `test_vm_provider.py::TestAzureTofuEnv::test_subscription_from_env` — `monkeypatch.setenv("AZURE_SUBSCRIPTION_ID","sub-123")`; assert `AzureStrategy().tofu_env()["ARM_SUBSCRIPTION_ID"] == "sub-123"` and `"GOOGLE_CLOUD_PROJECT" not in env`. Mirrors `TestResolveProject::test_uses_env_when_set` (`test_vm_cloud.py:55`).
  5. `test_vm_provider.py::TestAzureTofuEnv::test_subscription_from_az_cli` — `monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)`; mock `vm_provider.subprocess.run` → `CompletedProcess([],0,stdout="sub-cli\n")`; assert `ARM_SUBSCRIPTION_ID == "sub-cli"` and argv == `["az","account","show","--query","id","-o","tsv"]`. Mirrors `test_falls_back_to_gcloud_config` (`test_vm_cloud.py:59`).
  6. `test_vm_provider.py::TestAzureTofuEnv::test_empty_subscription_aborts` — empty stdout → `pytest.raises(SystemExit)` (mirror `test_vm_cloud.py:65`).
  7. `test_vm_provider.py::TestAzurePreflight::test_missing_az_aborts` — `@patch("vergil_tooling.lib.vm_provider.shutil.which", return_value=None)`; `pytest.raises(SystemExit)`; stderr contains "az". Mirror `test_missing_gcloud_aborts` (`test_vm_cloud.py:354`).
  8. `test_vm_provider.py::TestAzurePreflight::test_missing_token_aborts` — `which` returns a path, `az account get-access-token` raises `CalledProcessError`; assert `SystemExit` and stderr mentions `az login`. Mirror `test_missing_adc_aborts` (`test_vm_cloud.py:367`).
  9. `test_vm_provider.py::TestAzurePreflight::test_all_present_passes` — both succeed; no raise.
  10. `test_vm_cloud.py::TestRunTofu::test_azure_apply_volume_uses_azure_module_dir` — with provider="azure", assert `apply_volume(...)` init/apply argv contains `-chdir={modules / 'azure' / 'volume'}` (not `gcp`). Reuse the `_setup_volume` pattern (`test_vm_cloud.py:711`), parameterizing the provider.
  11. `test_vm_cloud.py::TestOffPlatformBackend::test_vm_vars_includes_ssh_public_key_for_azure` — `_off_spec(provider="azure")` (extend `_off_spec` at `test_vm_cloud.py:1200`); assert `"ssh_public_key" in b.vm_vars(...)` and that for `provider="gcp"` the key is **absent**.
- **Command:** `vrg-container-run -- python -m pytest tests/vergil_tooling/test_vm_provider.py -v` and the `TestRunTofu` case above.
- **Expected failure:** `ModuleNotFoundError: vergil_tooling.lib.vm_provider` (1–9); `AssertionError` on the `gcp` literal still in argv (10); `assert`/`KeyError` on missing `ssh_public_key` (11).

#### GREEN
- Create `vm_provider.py`:
  ```python
  class GcpStrategy:
      name = "gcp"
      module_segment = "gcp"
      def preflight(self) -> None:
          ...  # gcloud present + ADC, moved verbatim from vm_cloud.preflight lines 213-230
      def tofu_env(self) -> dict[str, str]:
          return {**os.environ, "TF_IN_AUTOMATION": "1",
                  "TF_PLUGIN_CACHE_DIR": str(_plugin_cache_dir()),
                  "GOOGLE_CLOUD_PROJECT": _resolve_project()}

  class AzureStrategy:
      name = "azure"
      module_segment = "azure"
      def preflight(self) -> None:
          if shutil.which("az") is None:
              print("ERROR: az CLI not found — install the Azure CLI", file=sys.stderr); raise SystemExit(1)
          try:
              subprocess.run(["az","account","get-access-token"], check=True, capture_output=True, text=True)
          except (subprocess.CalledProcessError, FileNotFoundError):
              print("ERROR: not logged in to Azure — run: az login", file=sys.stderr); raise SystemExit(1) from None
      def tofu_env(self) -> dict[str, str]:
          return {**os.environ, "TF_IN_AUTOMATION":"1",
                  "TF_PLUGIN_CACHE_DIR": str(_plugin_cache_dir()),
                  "ARM_SUBSCRIPTION_ID": self._subscription()}
      def _subscription(self) -> str:
          sub = os.environ.get("AZURE_SUBSCRIPTION_ID")
          if not sub:
              sub = subprocess.run(["az","account","show","--query","id","-o","tsv"],
                                   check=True, capture_output=True, text=True).stdout.strip()
          if not sub:
              print("ERROR: no Azure subscription — set AZURE_SUBSCRIPTION_ID or run: az account set", file=sys.stderr)
              raise SystemExit(1)
          return sub

  def strategy_for(provider: str) -> Provider:
      if provider == "gcp": return GcpStrategy()
      if provider == "azure": return AzureStrategy()
      print(f"ERROR: unknown off-platform provider '{provider}'", file=sys.stderr); raise SystemExit(1)
  ```
- In `vm_cloud.py`: `preflight()` → `preflight(provider: str)` keeping the OpenTofu-version block (lines 193–211) shared, then `strategy_for(provider).preflight()`. `_tofu_env()` → `_tofu_env(strategy)` returning `strategy.tofu_env()`; thread `strategy` (or `provider`) through `_run_tofu`/`_tofu_output`/`apply_volume`/`apply_vm`/`destroy_vm`/`destroy_volume`, replacing the `"gcp"` path literals with `strategy.module_segment`. `OffPlatformBackend.__init__` sets `self.strategy = strategy_for(spec.provider)`. `vm_vars` adds `ssh_public_key` when `self.strategy.name == "azure"` (sourced from `ensure_keypair`, Task 3).
- Update `vrg_vm.py`: `vm_cloud.preflight(target.spec.provider)` (lines 892, 2224); pass provider/strategy into the `apply_*`/`destroy_*` callers (`_cs_tofu_volume` 776, `_cs_tofu_vm` 784, `_destroy_recorded` 1288).

#### REFACTOR
- Collapse `_resolve_project`/`_tofu_env` into `GcpStrategy`; keep a thin `vm_cloud._resolve_project` only if other call sites still need it (they become strategy methods in Tasks 5/6/7, so it should end up GCP-private).
- Make `OffPlatformBackend.transport`/`status`/`region_zones`/`_project` delegate to `self.strategy` so the class body carries no provider literal.
- Keep the OpenTofu-version preflight in `vm_cloud` (provider-neutral); only the CLI/credential half moves into the strategy — do not duplicate the version check.

---

### Task 5: Azure capacity resilience — zone AND instance-family fallback

**Files:**
- Modify `src/vergil_tooling/lib/vm_provider.py` — `GcpStrategy.region_zones`/`is_zone_capacity_error`/`instance_fallback_candidates` (move existing GCP impls in); add `AzureStrategy` equivalents; add Azure `NESTED_VIRT_FAMILIES`/`FALLBACK_SHAPES` analogs as `AzureStrategy` class constants.
- Modify `src/vergil_tooling/lib/vm_cloud.py` — `region_zones`/`is_zone_capacity_error`/`instance_fallback_candidates` become strategy delegations; `apply_vm_with_zone_fallback` calls `backend.strategy.is_zone_capacity_error` (`vm_cloud.py:845,864,879`); GCP-flavored remediation string (`vm_cloud.py:894`) made provider-neutral.
- Modify `src/vergil_tooling/bin/vrg_vm.py` — `_candidate_zones` (line 747) and `_cs_tofu_volume` (line 773) call `backend.strategy.region_zones` / `backend.strategy.instance_fallback_candidates`.

**Interfaces:**
- `AzureStrategy.region_zones(region) -> list[str]` — Azure AZs as bare integers `["1","2","3"]`, or `[]` for a zoneless region.
- `AzureStrategy.is_zone_capacity_error(exc) -> bool` — matches `SkuNotAvailable`, `ZonalAllocationFailed`, `OverconstrainedAllocationRequest` (case-insensitive).
- `AzureStrategy.instance_fallback_candidates(requested) -> list[str]` — requested-first ladder across nested-virt-capable Azure families at the same size.

**PROPOSED — verify at implementation (Azure nested-virt family ladder):** Azure nested virtualization requires Dv3+/Ev3+ era or later (Dv2/Av2 do NOT support it). A defensible same-vCPU ladder:
- `AZURE_NESTED_VIRT_FAMILIES = ("Dsv5", "Dsv4", "Fsv2")` — proposed; the premium-storage `s` variants match the module's premium managed-disk attach. (Add `Esv5` only for memory sizes.)
- `AZURE_FALLBACK_SHAPES = frozenset({"8", "16"})` — Azure encodes size as `Standard_D{n}s_v5`, so the "shape" token is the vCPU integer, NOT GCP's `standard-8`. **The Azure type grammar differs structurally — GCP's `partition("-")` parser (`vm_cloud.py:806`) cannot be reused; the Azure variant needs its own regex/reassembly.**
- **Re-verify** Azure nested-virt support + exact size strings against current Azure docs at implementation (analogous to the GCP `# verify` note at `vm_cloud.py:782–786`).

#### RED
- **Tests** (mirroring `TestZoneCapacity` `test_vm_cloud.py:1030` / `TestInstanceFallbackLadder` `test_vm_cloud.py:1559`):
  1. `test_vm_provider.py::TestAzureZoneCapacity::test_detects_azure_capacity_strings` — three `CalledProcessError(1,[],stderr="...SkuNotAvailable...")` etc. → `True`; a `quota exceeded` string → `False`.
  2. `test_vm_provider.py::TestAzureZones::test_enumerates_bare_integer_zones` — mock `vm_provider.subprocess.run` → the `az vm list-skus` zones output; assert `region_zones("eastus") == ["1","2","3"]` and argv is the `az vm list-skus --location eastus ...` form.
  3. `test_vm_provider.py::TestAzureZones::test_zoneless_region_returns_empty` — no zones → `[]`.
  4. `test_vm_provider.py::TestAzureLadder::test_requested_first_then_same_size_siblings` — `instance_fallback_candidates("Standard_D8s_v5")[0] == "Standard_D8s_v5"`; same-vCPU siblings follow, deduped.
  5. `test_vm_provider.py::TestAzureLadder::test_unsupported_size_yields_no_fallback` — size not in `AZURE_FALLBACK_SHAPES` → `[requested]`.
  6. `test_vm_provider.py::TestAzureLadder::test_ladder_change_detector` — pin `AzureStrategy.NESTED_VIRT_FAMILIES`/`FALLBACK_SHAPES` (mirroring `test_vm_cloud.py:1588`); comment that nested-virt validity is hand-verified.
  7. `test_vm_cloud.py::TestFamilyFallback::test_azure_family_sweep_drives_via_strategy` — extend `TestFamilyFallback` (`test_vm_cloud.py:1620`): `.strategy.is_zone_capacity_error` True for the Azure error, `apply_vm` side-effects `[<azure capacity exc>, {"host":"h"}]`; assert family swap with `instance_override="Standard_D8s_v4"` and that the data disk (`destroy_volume`) is never touched.
- **Command:** `vrg-container-run -- python -m pytest tests/vergil_tooling/test_vm_provider.py::TestAzureLadder tests/vergil_tooling/test_vm_provider.py::TestAzureZones tests/vergil_tooling/test_vm_provider.py::TestAzureZoneCapacity -v`
- **Expected failure:** `AttributeError`/`ModuleNotFoundError` on the new strategy methods; ladder assertions fail (no Azure parser).

#### GREEN
- `AzureStrategy`:
  ```python
  _AZURE_CAPACITY_RE = re.compile(
      r"SkuNotAvailable|ZonalAllocationFailed|OverconstrainedAllocationRequest", re.IGNORECASE)
  _AZURE_SIZE_RE = re.compile(r"^Standard_([A-Za-z]+)(\d+)([a-z]*)_(v\d+)$")  # verify grammar at impl

  NESTED_VIRT_FAMILIES = ("Dsv5", "Dsv4", "Fsv2")   # PROPOSED — verify
  FALLBACK_SHAPES = frozenset({"8", "16"})           # vCPU counts — verify

  def is_zone_capacity_error(self, exc):
      return bool(_AZURE_CAPACITY_RE.search(f"{exc.stderr or ''}{exc.stdout or ''}"))
  def region_zones(self, region):
      out = subprocess.run(["az","vm","list-skus","--location",region,
                            "--resource-type","virtualMachines","--query",
                            "[0].locationInfo[0].zones","-o","tsv"],
                           check=True, capture_output=True, text=True).stdout
      return sorted(out.split())
  def instance_fallback_candidates(self, requested):
      m = _AZURE_SIZE_RE.match(requested)
      if not m: return [requested]
      _fam, n, _s, _ver = m.groups()
      if n not in self.FALLBACK_SHAPES: return [requested]
      cands = [requested]
      for fam in self.NESTED_VIRT_FAMILIES:           # reassemble Dsv5 -> Standard_D{n}s_v5 — verify
          c = self._size_for(fam, n)
          if c not in cands: cands.append(c)
      return cands
  ```
  (The size-string reassembly `Dsv5` → `Standard_D8s_v5` is the fiddly part; the tests pin the expected strings so GREEN is concrete. Confirm the canonical Azure size grammar before finalizing.)
- `apply_vm_with_zone_fallback`: replace module-level `is_zone_capacity_error(exc)` (`vm_cloud.py:845,864,879`) with `backend.strategy.is_zone_capacity_error(exc)`; replace the `(e.g. n2d-*)` remediation (`vm_cloud.py:894`) with a provider-neutral message.
- `vrg_vm.py`: `_candidate_zones` → `backend.strategy.region_zones(...)`; `_cs_tofu_volume` → `backend.strategy.instance_fallback_candidates(...)`.

#### REFACTOR
- Move GCP's `NESTED_VIRT_FAMILIES`/`FALLBACK_SHAPES`/`instance_fallback_candidates`/`region_zones`/`is_zone_capacity_error` into `GcpStrategy`, and **keep thin module-level delegations in `vm_cloud`** because the existing GCP tests import them by name (`test_vm_cloud.py:13–37`). This is the cheapest no-regression path.
- Factor the shared `f"{stderr}{stdout}"` capacity-blob idiom into one helper used by both strategies.

---

### Task 6: Read & enumerate surface (status, zones, volume-state parsing)

**Files:**
- Modify `src/vergil_tooling/lib/vm_provider.py` — `AzureStrategy.status(name, state_dir)`; `AzureStrategy.volume_disk_type() -> "azurerm_managed_disk"`.
- Modify `src/vergil_tooling/lib/vm_cloud.py` — `OffPlatformBackend.status` (line 1081) delegates to `self.strategy.status`; `parse_volume_state` (line 948) and `destroy_volume`'s disk-presence probe (line 720) match `strategy.volume_disk_type()` instead of the hard-coded `"google_compute_disk"` (line 964).
- Modify `src/vergil_tooling/bin/vrg_vm.py` — `_cloud_status` (line 1623) and `_volume_live_status` (line 1971) gain a real Azure branch (replacing the `gcloud`-only body and the `provider != "gcp"` degradation at line 1980).

**Interfaces:**
- Produces status strings normalized to the existing vocabulary — `"Running"` / `"Stopped"` / `""` (matching `OffPlatformBackend.status` `vm_cloud.py:1105–1109`).

**PROPOSED — verify at implementation (Azure power-state mapping):** `az vm get-instance-view --name <n> --resource-group <rg> --query "instanceView.statuses[?starts_with(code,'PowerState/')].code" -o tsv`:
- `PowerState/running` → `"Running"`; `PowerState/stopped`, `PowerState/deallocated` → `"Stopped"`; transitional (`starting`/`deallocating`) → `""`.
- Azure needs a resource group (GCP's zone addressing does not) — recover it from the RG-qualified `volume_id` ARM ID. `read_zone` returns Azure AZ integers (or empty); `status` must not assume a GCP zone string.

#### RED
- **Tests:**
  1. `test_vm_provider.py::TestAzureStatus::test_running` — mock `vm_provider.subprocess.run` → `stdout="PowerState/running\n"`; assert `AzureStrategy().status("vrg-x", state_dir) == "Running"` and argv begins `["az","vm","get-instance-view",...]`. Mirror `test_status_running` (`test_vm_cloud.py:1281`).
  2. `test_vm_provider.py::TestAzureStatus::test_deallocated_is_stopped` — `PowerState/deallocated` → `"Stopped"`.
  3. `test_vm_provider.py::TestAzureStatus::test_transitional_is_empty` — `PowerState/starting` → `""`.
  4. `test_vm_provider.py::TestAzureStatus::test_no_creds_is_empty` — `CalledProcessError` → `""` (mirror `test_vm_cloud.py:1318`).
  5. `test_vm_cloud.py::TestOffPlatformBackend::test_status_delegates_to_azure_strategy` — `_off_spec(provider="azure")`; patch the strategy's `status`, assert delegation.
  6. `test_vm_cloud.py::TestParseVolumeState::test_parses_azure_managed_disk` — new `_azure_volume_tfstate(...)` helper (mirror `_volume_tfstate` `test_vm_cloud.py:1371`) with `"type":"azurerm_managed_disk"`, ARM-ID `name`, `disk_size_gb`, `zone:"1"`, `tags` (Azure uses `tags`/`disk_size_gb`, not `labels`/`size` — **verify attr names**); assert `parse_volume_state(state, provider="azure")` populates size/labels.
  7. `test_vm_cloud.py::TestParseVolumeState::test_gcp_still_parses_google_disk` — regression under `provider="gcp"`.
- **Command:** `vrg-container-run -- python -m pytest "tests/vergil_tooling/test_vm_cloud.py::TestParseVolumeState" tests/vergil_tooling/test_vm_provider.py::TestAzureStatus -v`
- **Expected failure:** `parse_volume_state` returns `None` for `azurerm_managed_disk` (line 964 hard-matches `google_compute_disk`); `AzureStrategy.status` missing.

#### GREEN
- `parse_volume_state(state_file, provider="gcp")`: replace `resource.get("type") != "google_compute_disk"` (line 964) with `!= strategy_for(provider).volume_disk_type()`; read size from `attrs.get("disk_size_gb", attrs.get("size"))` and labels from `attrs.get("tags", attrs.get("labels"))`, gated on provider so GCP is byte-identical. (Cleaner: a `strategy.parse_disk_attrs(attrs) -> VolumeState`; weigh against the existing `TestParseVolumeState` cases.)
- `destroy_volume` disk-presence probe (line 720) passes provider through.
- `OffPlatformBackend.status` delegates to `self.strategy.status(self.name, self.state_dir())`.
- `AzureStrategy.status`: `read_zone` first (return `""` on `RuntimeError`, mirroring `vm_cloud.py:1083`), then the `az vm get-instance-view` argv, mapping power-state; any `CalledProcessError`/`FileNotFoundError` → `""`.
- `vrg_vm.py`: `_cloud_status` (1623) branches on the in-scope `provider` and calls the strategy for Azure; `_volume_live_status` (1971) adds `elif provider == "azure":` → `az disk show --ids <arm-id> --query "diskState" -o tsv` (verify `Unattached`/`Attached` values), replacing the blanket `provider != "gcp"` degradation — **no silent non-GCP degradation**.

#### REFACTOR
- Move the `gcloud compute instances describe` body out of `vrg_vm._cloud_status` into `GcpStrategy.status` so `_cloud_status` becomes `strategy_for(provider).status(...)` for both providers — killing the duplication with `OffPlatformBackend.status`.
- Make `_volume_live_status` a strategy method too, so the provider literal lives in exactly one place.

---

### Task 7: Lifecycle parity — in-place update, orphan rollback, host-key prune

**Files:**
- Modify `src/vergil_tooling/lib/vm_cloud.py` — `off_platform_transport(name, state_dir, provider)` (line 1027) builds the provider's transport via `strategy.transport`; `apply_vm` rollback (line 683) confirmed to cover the Azure ephemeral set; add `prune_known_hosts(host)` helper invoked on destroy/rebuild.
- Modify `src/vergil_tooling/bin/vrg_vm.py` — `_cmd_update_off_platform` (line 1076) and `update --all` (line 1239) pass `vm.provider` to `off_platform_transport`; the rebuild/destroy path (`_destroy_recorded` line 1272 / rebuild stages line 581) prunes the known_hosts entry for Azure boxes.

**Interfaces:**
- `off_platform_transport(name, state_dir, provider) -> Transport` (Iap for gcp, Ssh for azure); `prune_known_hosts(host: str) -> None`.

**Confirmations from source (verify only — no new behavior):**
- **In-place update uses the transport, not a rebuild:** `_update_over_transport` (`vrg_vm.py:1043`) is transport-generic; `_cmd_update_off_platform` (1076) calls `backend.transport()` and `update --all` calls `off_platform_transport` (1239). Azure inherits this unchanged **once both factories return `SshTransport`** (#1815/#1812). The only change is the transport factory.
- **Orphan rollback:** `apply_vm`'s `tofu destroy` rollback (`vm_cloud.py:683–691`) is state-driven, so it covers the Azure ephemeral set (public IP + NIC + VM) automatically **provided those resources are in the vm tfstate, not the volume tfstate** — verify the Azure module split. The session-time NSG *source* rule from `nsg_refresh` (Task 3) is NOT in tofu state, so the rollback won't clear it — track as a known residual for nsg_refresh teardown.

#### RED
- **Tests:**
  1. `test_vm_cloud.py::TestOffPlatformTransport::test_builds_ssh_transport_for_azure` — extend `TestOffPlatformTransport` (`test_vm_cloud.py:1178`): `off_platform_transport("vrg-x", state_dir, provider="azure")` returns an `SshTransport` (host = public IP from state, right ssh_user/key). Mirror `test_builds_iap_from_local_state` (`test_vm_cloud.py:1179`).
  2. `test_vm_cloud.py::TestOffPlatformTransport::test_gcp_still_builds_iap` — regression: `provider="gcp"` still returns `IapTransport` (existing test updated for the new `provider` arg — a **signature change**, so the call sites `vrg_vm.py:1239,2069` update in GREEN).
  3. `test_vm_provider.py::TestPruneKnownHosts::test_removes_managed_entry` — `prune_known_hosts("203.0.113.5")` runs `ssh-keygen -R 203.0.113.5 -f <known_hosts>` (assert argv targets the vergil-managed known_hosts path); mock `subprocess.run`.
  4. `test_vm_provider.py::TestPruneKnownHosts::test_missing_known_hosts_is_noop` — absent file → no raise, no `ssh-keygen` call.
  5. `test_vrg_vm.py::<rebuild class>::test_rebuild_prunes_known_hosts_for_azure` — patch `vm_cloud.prune_known_hosts`; drive the Azure destroy/rebuild path; assert it was called with the box's IP. Match the existing `_destroy_recorded`/rebuild mocking style.
  6. `test_vm_cloud.py::TestRunTofu::test_apply_vm_rollback_covers_azure_vm_state` — Azure provider; `apply_vm` raises a capacity error; assert the rollback `tofu destroy` runs against the **azure** vm.tfstate (argv contains `-chdir={modules/'azure'/'vm'}`). Mirror `test_apply_vm_rolls_back_on_failure` (`test_vm_cloud.py:806`).
- **Command:** `vrg-container-run -- python -m pytest "tests/vergil_tooling/test_vm_cloud.py::TestOffPlatformTransport" tests/vergil_tooling/test_vm_provider.py::TestPruneKnownHosts -v`
- **Expected failure:** `off_platform_transport` takes no `provider` arg (`TypeError`); `prune_known_hosts` missing; rebuild path makes no prune call.

#### GREEN
- `off_platform_transport(name, state_dir, provider)`: `return strategy_for(provider).transport(name, state_dir, _effective_ssh_user())`. `GcpStrategy.transport` builds `IapTransport(name, read_zone(state_dir), _resolve_project(), ssh_user)` (lines 1036–1037). `AzureStrategy.transport` reads the box's public IP from local state (the vm tfstate output or a persisted `host` file — verify what create writes) and returns `SshTransport(ip, ssh_user, key_path)` (Task 2/3 class + key).
- `OffPlatformBackend.transport` (line 1077) delegates to `self.strategy.transport`.
- Update the two `off_platform_transport(vm.name, vm.state_dir)` call sites (`vrg_vm.py:1239,2069`) to pass `vm.provider`.
- Add `prune_known_hosts(host)` (in `vm_provider`): if the managed known_hosts file exists, `subprocess.run(["ssh-keygen","-R",host,"-f",str(known_hosts_path)], check=False)`; no-op when absent. Wire into the destroy/rebuild path for Azure boxes after `destroy_vm`.

#### REFACTOR
- Make `prune_known_hosts` a strategy method (`GcpStrategy.prune_known_hosts` = no-op — IAP tunnels pin no host key by IP; `AzureStrategy.prune_known_hosts` = the `ssh-keygen -R`) so the destroy path calls `strategy.prune_known_hosts(host)` with no provider `if`.
- Comment in `apply_vm` (line 683) that the rollback is state-driven hence provider-neutral; add the residual-NSG-rule caveat pointing at the `nsg_refresh` teardown (Task 3) so the gap is tracked, not silently dropped.
- Verify the `update --all` fail-deferred loop (`vrg_vm.py:1231–1255`) catches `SshTransport`'s failure modes the same way it catches IAP's (`CalledProcessError` on connect failure) — confirm `SshTransport.run` raises `CalledProcessError` like `IapTransport`.

---

## Self-Review (vergil-tooling plan)

**Spec coverage:** module-path parameterization → T1/T4; `SshTransport` + NSG refresh + fail-closed operator-IP + host-key policy → T2/T3/T7; keypair + `ssh_public_key` → T3/T4; provider-strategy seam + Azure preflight/credentials (`ARM_SUBSCRIPTION_ID`) → T4; capacity resilience — zone **and** family fallback, the three GCP-coupled pieces → T5; read & enumerate surface (status/zones/volume-state) + no-silent-degradation → T6; lifecycle parity (#1815 in-place update, #1807 orphan rollback, host-key prune) → T7. ✓

**Placeholder scan:** No TBD/TODO. Items marked "verify at implementation" (Azure size grammar, family ladder, power-state mapping, disk-state values) are genuine external facts to confirm against Azure docs — not deferred code; each has a pinned test so GREEN is concrete. ✓

**Type consistency:** the `Provider` protocol members (`module_segment`/`preflight`/`tofu_env`/`region_zones`/`is_zone_capacity_error`/`instance_fallback_candidates`/`transport`/`status`/`volume_disk_type`/`prune_known_hosts`) are named identically across T4–T7; `ensure_keypair`/`nsg_refresh`/`_operator_public_ip`/`SshTransport` (T2/T3) referenced with consistent signatures; `ssh_public_key` matches the vergil-vm interface variable. ✓

**No-regression:** GCP-importing tests (`test_vm_cloud.py:13–37`) preserved via module-level `GcpStrategy()` delegations; `preflight(provider)` signature change handled by defaulting `provider="gcp"` or threading `"gcp"` at the existing call sites. ✓

**Cross-repo ordering:** land + tag the vergil-vm modules plan first (modules fetched from the v-tag archive); #1831 is already landed, so no further blocker.
