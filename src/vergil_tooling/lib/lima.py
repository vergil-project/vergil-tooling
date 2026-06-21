"""Lima VM subprocess wrappers."""

from __future__ import annotations

import json
import platform
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import progress

if TYPE_CHECKING:
    from vergil_tooling.lib.identity import Identity

_TEMPLATE_URL = (
    "https://raw.githubusercontent.com/vergil-project/vergil-vm/{tag}/templates/agent.yaml"
)

_TOOLING_INSTALL = "vergil-tooling @ git+https://github.com/vergil-project/vergil-tooling@{tag}"


def _limactl(*args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            ["limactl", *args],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def _limactl_stream(*args: str) -> None:
    """Run limactl, streaming output through the progress framework.

    Used for the long-running lifecycle verbs whose output is the only
    progress signal (issue #1454); quick query verbs stay on ``_limactl``'s
    captured model.
    """
    progress.run(("limactl", *args))


def shell_run(
    instance: str,
    *args: str,
    workdir: str = "/tmp",  # noqa: S108
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            [  # noqa: S607
                "limactl",
                "shell",
                "--workdir",
                workdir,
                instance,
                "--",
                *args,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def shell_pipe(
    instance: str,
    cmd: str,
    input_data: str,
    *,
    workdir: str = "/tmp",  # noqa: S108
) -> None:
    try:
        subprocess.run(  # noqa: S603
            [  # noqa: S607
                "limactl",
                "shell",
                "--workdir",
                workdir,
                instance,
                "--",
                "bash",
                "-c",
                cmd,
            ],
            check=True,
            input=input_data,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def vm_status(instance: str) -> str:
    """Return VM status: ``Running``, ``Stopped``, or ``""`` if not found."""
    try:
        result = _limactl("list", "--json")
    except subprocess.CalledProcessError:
        return ""
    for line in result.stdout.strip().splitlines():
        entry = json.loads(line)
        if entry.get("name") == instance:
            return str(entry.get("status", ""))
    return ""


def list_vms() -> list[dict[str, str]]:
    """Return all Lima instances as ``[{name, status, ...}]``."""
    try:
        result = _limactl("list", "--json")
    except subprocess.CalledProcessError:
        return []
    vms: list[dict[str, str]] = []
    for line in result.stdout.strip().splitlines():
        entry = json.loads(line)
        vms.append({"name": entry.get("name", ""), "status": entry.get("status", "")})
    return vms


_TAG_PATTERN = re.compile(r"^v\d+\.\d+(\.\d+)?$")


def fetch_template(tag: str) -> Path:
    """Download ``agent.yaml`` from vergil-vm at *tag*. Returns temp file path."""
    if not _TAG_PATTERN.fullmatch(tag):
        print(f"ERROR: invalid template tag '{tag}' (expected vN.N or vN.N.N)", file=sys.stderr)
        raise SystemExit(1)
    url = _TEMPLATE_URL.format(tag=tag)
    try:
        with urllib.request.urlopen(url) as resp:  # noqa: S310
            content = resp.read()
    except urllib.error.URLError as exc:
        print(f"ERROR: failed to fetch template from {url}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        suffix=".yaml", prefix="vergil-vm-", delete=False
    )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


_NESTED_VIRT_REQUIREMENT = "nested virtualization requires macOS 15+ on M3-or-later Apple silicon"
_NESTED_VIRT_MIN_MACOS_MAJOR = 15
_NESTED_VIRT_MIN_APPLE_M = 3


def _nested_virt_unsupported_reason(system: str, mac_ver: str, cpu_brand: str) -> str | None:
    """Pure-logic core of nested_virt_unsupported_reason (testable without a host)."""
    if system != "Darwin":
        return f"{_NESTED_VIRT_REQUIREMENT}; host OS is {system or 'unknown'}"
    major = mac_ver.split(".", 1)[0]
    if not major.isdigit() or int(major) < _NESTED_VIRT_MIN_MACOS_MAJOR:
        return f"{_NESTED_VIRT_REQUIREMENT}; host macOS is {mac_ver or 'unknown'}"
    match = re.search(r"\bApple M(\d+)\b", cpu_brand)
    if match is None or int(match.group(1)) < _NESTED_VIRT_MIN_APPLE_M:
        return f"{_NESTED_VIRT_REQUIREMENT}; host CPU is {cpu_brand or 'unknown'}"
    return None


def nested_virt_unsupported_reason() -> str | None:
    """Return why this host cannot do nested virtualization, or None if it can.

    First line of the three-layer defense for the per-profile ``nested`` knob
    (issue #1447): abort before any build starts. Lima's own rejection is the
    backstop; the template's in-guest /dev/kvm check is the last line.
    """
    system = platform.system()
    if system != "Darwin":
        return _nested_virt_unsupported_reason(system, "", "")
    result = subprocess.run(  # noqa: S603
        ["sysctl", "-n", "machdep.cpu.brand_string"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    # An unreadable brand string yields a reason (abort), never a silent pass.
    cpu_brand = result.stdout.strip() if result.returncode == 0 else ""
    return _nested_virt_unsupported_reason(system, platform.mac_ver()[0], cpu_brand)


def create_vm(
    instance: str,
    template: Path,
    projects_dir: str,
    *,
    cpus: int | None = None,
    memory: str | None = None,
    disk: str | None = None,
    packages: list[str] | None = None,
    apt_repos: list[dict[str, str]] | None = None,
    vagrant_plugins: list[str] | None = None,
    port_forwards: list[str] | None = None,
    fingerprint: str | None = None,
    nested: bool = False,
) -> None:
    claude_projects_path = Path.home() / ".claude" / "projects"
    claude_skills_path = Path.home() / ".claude" / "skills"
    claude_projects_path.mkdir(parents=True, exist_ok=True)
    claude_skills_path.mkdir(parents=True, exist_ok=True)
    claude_projects = str(claude_projects_path)
    claude_skills = str(claude_skills_path)

    args = [
        "create",
        f"--name={instance}",
        "--tty=false",
        f'--set=.mounts[0].location = "{projects_dir}"',
        f'--set=.mounts[0].mountPoint = "{projects_dir}"',
        f'--set=.mounts[1].location = "{claude_projects}"',
        f'--set=.mounts[1].mountPoint = "{claude_projects}"',
        "--set=.mounts[1].writable = true",
        f'--set=.mounts[2].location = "{claude_skills}"',
        f'--set=.mounts[2].mountPoint = "{claude_skills}"',
        "--set=.mounts[2].writable = false",
    ]
    if cpus is not None:
        args.append(f"--set=.cpus = {cpus}")
    if memory is not None:
        args.append(f'--set=.memory = "{memory}"')
    if disk is not None:
        args.append(f'--set=.disk = "{disk}"')
    if packages:
        args.append(f'--set=.param.EXTRA_PACKAGES = "{" ".join(packages)}"')
    if apt_repos:
        # Each repo encoded "name|key_url|uri|suite|components"; repos joined by ";".
        encoded = ";".join(
            "|".join((r["name"], r["key_url"], r["uri"], r["suite"], r["components"]))
            for r in apt_repos
        )
        args.append(f'--set=.param.APT_REPOS = "{encoded}"')
    if vagrant_plugins:
        args.append(f'--set=.param.VAGRANT_PLUGINS = "{" ".join(vagrant_plugins)}"')
    if port_forwards:
        # Each record "<port>|<host:port>"; records joined by ";" to match the
        # template's IFS=';' / IFS='|' parser (vergil-vm #170).
        args.append(f'--set=.param.PORT_FORWARDS = "{";".join(port_forwards)}"')
    if fingerprint:
        args.append(f'--set=.param.SPEC_FINGERPRINT = "{fingerprint}"')
    if nested:
        # Both halves together (vergil-vm#131): the Lima config knob exposes
        # /dev/kvm, the template param turns on the in-guest verification that
        # fails the build loudly when it didn't appear.
        args.append("--set=.nestedVirtualization = true")
        args.append('--set=.param.NESTED_VIRT = "true"')
    args.append(str(template))
    _limactl(*args)


_GUEST_LOG_POLL_SECS = 2.0
_HEARTBEAT_SECS = 30.0

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)([hms])")


def _parse_duration_secs(value: str) -> float | None:
    """Parse a Go-style duration ('30m', '1h30m', '90s') to seconds, or None."""
    parts = _DURATION_RE.findall(value)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != value:
        return None
    scale = {"h": 3600.0, "m": 60.0, "s": 1.0}
    return sum(float(n) * scale[u] for n, u in parts)


def _serial_dir(instance: str) -> Path:
    return Path.home() / ".lima" / instance


def _drain_serial_logs(serial_dir: Path, offsets: dict[Path, int]) -> None:
    """Emit complete new lines appended to the instance's serial logs.

    The serial console carries the in-guest provision output (cloud-init,
    extra-package installs, vagrant plugin builds) that ``limactl start``
    itself never prints. Partial trailing lines are held back until the
    newline arrives; *offsets* tracks per-file progress between calls.
    """
    for path in sorted(serial_dir.glob("serial*.log")):
        try:
            start = offsets.get(path, 0)
            with path.open("rb") as fh:
                fh.seek(start)
                chunk = fh.read()
        except OSError:
            continue
        cut = chunk.rfind(b"\n")
        if cut == -1:
            continue
        offsets[path] = start + cut + 1
        for raw in chunk[: cut + 1].decode("utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line:
                progress.emit(f"[guest] {line}")


def _heartbeat(elapsed: float, timeout: str, budget: float | None) -> str:
    """One elapsed-vs-budget line, so an approaching timeout cliff is visible."""
    if budget:
        return f"[elapsed] {progress.format_elapsed(elapsed)} of {timeout} timeout budget"
    return f"[elapsed] {progress.format_elapsed(elapsed)}"


def _provision_monitor(
    serial_dir: Path,
    timeout: str,
    stop: threading.Event,
    *,
    poll_secs: float = _GUEST_LOG_POLL_SECS,
    heartbeat_secs: float = _HEARTBEAT_SECS,
) -> None:
    """Tail serial logs and emit a periodic heartbeat until *stop* is set."""
    offsets: dict[Path, int] = {}
    budget = _parse_duration_secs(timeout)
    started = time.monotonic()
    next_beat = heartbeat_secs
    while not stop.wait(poll_secs):
        _drain_serial_logs(serial_dir, offsets)
        elapsed = time.monotonic() - started
        if elapsed >= next_beat:
            progress.emit(_heartbeat(elapsed, timeout, budget))
            next_beat = elapsed + heartbeat_secs
    _drain_serial_logs(serial_dir, offsets)


def start_vm(instance: str, *, timeout: str = "30m") -> None:
    """Start the VM, streaming limactl output and in-guest provision progress."""
    status = vm_status(instance)
    if status == "Running":
        return
    stop = threading.Event()
    monitor = threading.Thread(
        target=_provision_monitor,
        args=(_serial_dir(instance), timeout, stop),
        daemon=True,
    )
    monitor.start()
    try:
        _limactl_stream("start", f"--timeout={timeout}", instance)
    finally:
        stop.set()
        monitor.join()


def stop_vm(instance: str) -> None:
    status = vm_status(instance)
    if status != "Running":
        return
    # Flush the guest page cache before stopping. A non-synced shutdown can
    # truncate files written just before the stop — notably the uv cache and
    # tool receipt that `install_tooling` writes immediately before the
    # rebuild's terminal `cycle-ssh` stop — which then poisons the next
    # `vrg-vm session` update (see `_uv_tool_install`). Best-effort: a failed
    # sync must never block the stop, but surface it rather than swallow it.
    try:
        shell_run(instance, "sync")
    except subprocess.CalledProcessError:
        print(
            f"  WARNING: guest sync before stop failed for '{instance}' — stopping anyway",
            file=sys.stderr,
        )
    _limactl("stop", instance)


def delete_vm(instance: str) -> None:
    _limactl("delete", "--force", instance)


def _read_host_git_config(key: str) -> str | None:
    """Read a single value from the host's global git config."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "config", "--global", key],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _inject_host_git_identity(instance: str) -> None:
    """Copy user.name and user.email from host git config into the VM."""
    name = _read_host_git_config("user.name")
    email = _read_host_git_config("user.email")

    if name:
        print(f"  Setting git user.name: {name}")
        shell_run(instance, "git", "config", "--global", "user.name", name)
    if email:
        print(f"  Setting git user.email: {email}")
        shell_run(instance, "git", "config", "--global", "user.email", email)


def inject_credentials(instance: str, identity: Identity) -> None:
    """Inject GitHub App and Claude Code credentials into a running VM."""
    if identity.auth_type == "none":
        # Credential-less identity: skip the entire stage. No App key, no
        # app.env, no identity-mode file, no git identity, no HTTPS rewrite,
        # no Claude token. The box can only touch local files.
        print("  Skipping credential injection (credential-less identity)")
        return

    key_path = Path(identity.private_key_path).expanduser()
    if not key_path.exists():
        print(f"ERROR: private key not found: {key_path}", file=sys.stderr)
        raise SystemExit(1)

    if not identity.mode:
        print(
            f"ERROR: cannot derive identity mode for VM '{instance}' — rename the"
            " identity in identities.toml so the name contains 'user' or 'audit'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    key_content = key_path.read_text()

    print("  Injecting App private key...")
    shell_run(instance, "bash", "-c", "mkdir -p ~/.config/vergil")
    shell_pipe(
        instance,
        "cat > ~/.config/vergil/app.pem && chmod 600 ~/.config/vergil/app.pem",
        key_content,
    )

    print("  Injecting App configuration...")
    shell_pipe(
        instance,
        "cat > ~/.config/vergil/app.env && chmod 600 ~/.config/vergil/app.env",
        f"APP_ID={identity.app_id}\n",
    )

    _inject_identity_mode(instance, identity.mode)

    _inject_host_git_identity(instance)

    print("  Configuring git for HTTPS GitHub access...")
    shell_run(
        instance,
        "git",
        "config",
        "--global",
        "url.https://github.com/.insteadOf",
        "git@github.com:",
    )

    if identity.claude_token_path:
        _inject_claude_token(instance, identity.claude_token_path)


_BASHRC_MODE_LINE = (
    "[ -f ~/.config/vergil/identity-mode ]"
    ' && export VRG_IDENTITY_MODE="$(cat ~/.config/vergil/identity-mode)"'
)


def _inject_identity_mode(instance: str, mode: str) -> None:
    """Write the identity-mode file and export it from the shell profile.

    The plain-text mode file is the single source of truth: the bashrc
    line exports it as ``VRG_IDENTITY_MODE`` for interactive shells (and
    skill preflights), and ``identity_mode.current_mode()`` reads the
    file directly as a fallback for processes that never sourced bashrc.
    """
    print(f"  Injecting identity mode ({mode})...")
    shell_pipe(
        instance,
        "cat > ~/.config/vergil/identity-mode && chmod 600 ~/.config/vergil/identity-mode",
        f"{mode}\n",
    )
    export_cmd = (
        f'grep -qF "identity-mode" ~/.bashrc 2>/dev/null'
        f" || echo '{_BASHRC_MODE_LINE}' >> ~/.bashrc"
    )
    shell_run(instance, "bash", "-c", export_cmd)


_BASHRC_SOURCE_LINE = "[ -f ~/.config/vergil/claude.env ] && . ~/.config/vergil/claude.env"


def _inject_claude_token(instance: str, token_path: str) -> None:
    path = Path(token_path).expanduser()
    if not path.exists():
        print(f"ERROR: Claude token not found: {path}", file=sys.stderr)
        raise SystemExit(1)

    token = path.read_text().strip()

    print("  Injecting Claude Code token...")
    shell_pipe(
        instance,
        "cat > ~/.config/vergil/claude.env && chmod 600 ~/.config/vergil/claude.env",
        f"export CLAUDE_CODE_OAUTH_TOKEN={token}\n",
    )

    source_cmd = (
        f'grep -qF "claude.env" ~/.bashrc 2>/dev/null'
        f" || echo '{_BASHRC_SOURCE_LINE}' >> ~/.bashrc"
    )
    shell_run(instance, "bash", "-c", source_cmd)

    credentials = json.dumps({"claudeAiOauth": {"accessToken": token}})
    shell_run(instance, "bash", "-c", "mkdir -p ~/.claude")
    shell_pipe(
        instance,
        "cat > ~/.claude/.credentials.json && chmod 600 ~/.claude/.credentials.json",
        credentials + "\n",
    )

    onboarding = json.dumps({"hasCompletedOnboarding": True})
    shell_pipe(
        instance,
        "cat > ~/.claude.json",
        onboarding + "\n",
    )


_TOOLING_TAG_FILE = "~/.config/vergil/tooling-tag"


def get_tooling_version(instance: str) -> str | None:
    """Return the installed vergil-tooling version string, or None."""
    try:
        result = shell_run(
            instance,
            "bash",
            "-c",
            'export PATH="$HOME/.local/bin:$PATH" && uv tool list 2>/dev/null',
        )
        for line in result.stdout.splitlines():
            if line.startswith("vergil-tooling "):
                return line.split()[1]
    except subprocess.CalledProcessError:
        pass
    return None


def _uv_tool_install(instance: str, install_spec: str, *, reinstall: bool) -> None:
    """Run ``uv tool install`` inside the VM, self-healing a poisoned uv cache.

    A corrupt cache entry — e.g. a zero-byte wheel ``METADATA`` left behind by
    an unclean VM stop — makes ``uv tool install`` fail with "wheel is invalid".
    On the reinstall path uv removes the existing tool *before* it fails, so a
    poisoned cache leaves the VM with no tooling at all and bricks every future
    ``vrg-vm session`` until the cache is cleared by hand. So on failure, clear
    the VM's uv cache and retry the install once.

    The retry escalates to ``--force``. The same unclean stop also corrupts the
    tool *receipt*: uv removes the entry but, unable to read the receipt, cannot
    enumerate the tool's entry points, so the ``vrg-*`` executables orphan in
    ``~/.local/bin``. Clearing the cache fixes the wheel, but the retry would
    then die with "Executable already exists" — only ``--force`` replaces
    existing entry points (``--reinstall`` alone does not), so the retry must
    force to fully recover.
    """
    flag = "--reinstall " if reinstall else ""
    install_cmd = f'export PATH="$HOME/.local/bin:$PATH" && uv tool install {flag}"{install_spec}"'
    retry_cmd = (
        'export PATH="$HOME/.local/bin:$PATH" && '
        f'uv tool install --force --reinstall "{install_spec}"'
    )
    try:
        shell_run(instance, "bash", "-c", install_cmd)
        return
    except subprocess.CalledProcessError:
        print(
            "  uv tool install failed — clearing the VM uv cache and retrying once...",
            file=sys.stderr,
        )
        shell_run(
            instance,
            "bash",
            "-c",
            'export PATH="$HOME/.local/bin:$PATH" && uv cache clean',
        )
        shell_run(instance, "bash", "-c", retry_cmd)


def install_tooling(instance: str, tag: str) -> None:
    """Install vergil-tooling inside the VM and record the tag."""
    install_spec = _TOOLING_INSTALL.format(tag=tag)
    print(f"  Installing vergil-tooling ({tag})...")
    _uv_tool_install(instance, install_spec, reinstall=False)
    shell_run(instance, "bash", "-c", f"mkdir -p $(dirname {_TOOLING_TAG_FILE})")
    shell_pipe(instance, f"cat > {_TOOLING_TAG_FILE}", f"{tag}\n")


def update_tooling(instance: str, tag: str | None = None, *, fallback_tag: str = "") -> None:
    """Reinstall vergil-tooling inside the VM.

    Uses *tag* if given, otherwise reads the tag from the marker file
    written by ``install_tooling``.  Falls back to *fallback_tag* when
    no marker exists (pre-existing VMs created before marker support).

    An explicit *tag* is treated as a temporary override and is not
    persisted to the marker file.
    """
    explicit = tag is not None
    if tag is None:
        result = shell_run(instance, "bash", "-c", f"cat {_TOOLING_TAG_FILE} 2>/dev/null || true")
        tag = result.stdout.strip() or fallback_tag
    if not tag:
        print("ERROR: no tooling tag found — run 'vrg-vm create' first", file=sys.stderr)
        raise SystemExit(1)
    install_spec = _TOOLING_INSTALL.format(tag=tag)
    print(f"  Updating vergil-tooling ({tag})...")
    _uv_tool_install(instance, install_spec, reinstall=True)
    if not explicit:
        shell_run(instance, "bash", "-c", f"mkdir -p $(dirname {_TOOLING_TAG_FILE})")
        shell_pipe(instance, f"cat > {_TOOLING_TAG_FILE}", f"{tag}\n")


# Prepended to every in-VM `claude` invocation. claude itself is on the base
# PATH (/usr/bin/claude, from apt node + `npm install -g`), but exporting PATH
# explicitly — exactly as update_tooling does — keeps resolution independent of
# the interactive environment. The VM's login shell is zsh (vergil-vm `chsh -s
# /bin/zsh`), configured via ~/.zshenv / /etc/environment, so a bash login shell
# would source none of its config; a non-login `bash -c` with an explicit PATH
# avoids depending on any of that.
_PLUGIN_PATH_EXPORT = 'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"'


def update_plugins(instance: str) -> None:
    """Refresh enabled Claude Code plugins inside the VM.

    Plugins are installed VM-locally from their GitHub marketplaces (declared
    in the copied settings.json); they are deliberately not shared from the
    host (see docs/site/docs/guides/agent-vm-claude-share-set.md). This
    refreshes marketplace metadata and then advances each enabled plugin to its
    latest version, mirroring how update_tooling advances vergil-tooling.

    `claude plugin update` has no bulk form: it requires a specific plugin id
    and honours the plugin's scope (user vs project), which differ across the
    set. So enumerate the installed plugins with `claude plugin list --json`
    and update each enabled one with its own scope. Updates apply on the next
    Claude restart, which every new session triggers.

    Best-effort across the set: one plugin failing does not block the others;
    failures are collected and surfaced by raising afterwards (never swallowed).
    """
    print("  Refreshing Claude plugins...")
    shell_run(
        instance,
        "bash",
        "-c",
        f"{_PLUGIN_PATH_EXPORT} && claude plugin marketplace update",
    )
    listing = shell_run(
        instance,
        "bash",
        "-c",
        f"{_PLUGIN_PATH_EXPORT} && claude plugin list --json",
    )
    plugins = json.loads(listing.stdout)

    failures: list[str] = []
    for plugin in plugins:
        if not plugin.get("enabled"):
            continue
        pid = plugin["id"]
        scope = plugin.get("scope", "user")
        print(f"    updating {pid} ({scope})...")
        cmd = (
            f"{_PLUGIN_PATH_EXPORT} && claude plugin update "
            f"{shlex.quote(pid)} --scope {shlex.quote(scope)}"
        )
        try:
            shell_run(instance, "bash", "-c", cmd)
        except subprocess.CalledProcessError:
            failures.append(pid)

    if failures:
        joined = ", ".join(failures)
        msg = f"failed to update plugin(s): {joined}"
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)


def vm_age_days(instance: str) -> float | None:
    """Return VM age in fractional days, or None if not found."""
    try:
        result = _limactl("list", "--json")
    except subprocess.CalledProcessError:
        return None
    for line in result.stdout.strip().splitlines():
        entry = json.loads(line)
        if entry.get("name") == instance:
            vm_dir = entry.get("dir", "")
            if not vm_dir:
                return None
            dir_path = Path(vm_dir)
            if not dir_path.exists():
                return None
            st = dir_path.stat()
            created = st.st_birthtime if hasattr(st, "st_birthtime") else st.st_mtime  # type: ignore[attr-defined]
            return (time.time() - created) / 86400
    return None


_CLAUDE_CONFIG_FILES = ("CLAUDE.md", "settings.json")


def copy_claude_config(instance: str, claude_dir: Path) -> None:
    """Copy CLAUDE.md and settings.json from host into the VM."""
    if not claude_dir.is_dir():
        return
    shell_run(instance, "bash", "-c", "mkdir -p ~/.claude")
    for filename in _CLAUDE_CONFIG_FILES:
        source = claude_dir / filename
        if source.exists():
            content = source.read_text()
            shell_pipe(
                instance,
                f"cat > ~/.claude/{filename}",
                content,
            )


# The agent-VM ~/.claude share set. See
# docs/site/docs/guides/agent-vm-claude-share-set.md for the full model.
#
# projects/ -> durable, host-shared transcript store. Resume-after-rebuild
#   reads these, so a conversation survives a VM rebuild (the data lives on
#   the host). Append-only writes, which work fine through the virtiofs mount.
# skills/   -> read-only reference mount.
# sessions/ -> NOT shared; see _CLAUDE_UNLINK_DIRS below. It is a disposable
#   VM-local roster (pid->session), regenerated each run. Resume does NOT
#   depend on it, so keeping it VM-local does not break resume.
# plugins/  -> NOT shared; kept current VM-locally via update_plugins (each VM
#   installs/refreshes from the GitHub marketplaces declared in the copied
#   settings.json). Sharing the host's materialized checkout across the
#   macOS/Linux boundary would be fragile and is unnecessary.
_CLAUDE_LINK_DIRS = ("projects", "skills")

# Subdirs that must NOT be symlinked onto the host mount and must stay
# VM-local. Claude writes the sessions roster atomically (temp file in the
# VM-local tmpdir, then rename() onto the target). Renaming across filesystems
# (VM-local ext -> virtiofs mount) fails with EXDEV, so the roster write
# silently fails and no file is ever produced. The roster is also per-platform
# (pids only mean anything on the owning machine), so there is no value in
# sharing it. We instead read each VM's local roster over `limactl shell` for
# session detection. See vergil-tooling #1301 and vergil-vm #73.
_CLAUDE_UNLINK_DIRS = ("sessions",)


def link_claude_dirs(instance: str, claude_dir: Path) -> None:
    """Point selected VM ~/.claude subdirs at the path-preserved host mounts.

    The .claude/{projects,skills} mounts are path-preserved
    (mountPoint == host location), but the VM's HOME differs from the host's,
    so Claude inside the VM reads ~/.claude/... instead of the mounted host
    path. Symlink those subdirs so they are shared with the host and survive
    VM rebuilds. Idempotent; an existing non-empty real directory is left in
    place with a warning rather than clobbered.

    sessions/ is deliberately left VM-local (see _CLAUDE_UNLINK_DIRS): a
    symlink onto the virtiofs mount breaks Claude's atomic roster write with
    EXDEV. Any pre-existing sessions/ symlink (from an older build) is removed
    so Claude recreates a real local directory; removing the symlink does not
    touch the host target's contents.
    """
    if not claude_dir.is_dir():
        return
    parts = ["mkdir -p ~/.claude"]
    for sub in _CLAUDE_LINK_DIRS:
        target = shlex.quote(str(claude_dir / sub))
        link = f'"$HOME/.claude/{sub}"'
        parts.append(
            f"if [ -L {link} ] || [ ! -e {link} ]; then ln -sfn {target} {link}; "
            f'elif [ -d {link} ] && [ -z "$(ls -A {link})" ]; then '
            f"rmdir {link} && ln -s {target} {link}; "
            f'else echo "WARNING: ~/.claude/{sub} exists and is not empty;'
            f' not linking" >&2; fi'
        )
    for sub in _CLAUDE_UNLINK_DIRS:
        link = f'"$HOME/.claude/{sub}"'
        parts.append(f"if [ -L {link} ]; then rm -f {link}; fi")
    shell_run(instance, "bash", "-c", " ; ".join(parts))


def try_update_tooling(
    instance: str,
    tag: str | None = None,
    *,
    fallback_tag: str = "",
) -> bool:
    """Update vergil-tooling, returning False on failure instead of aborting.

    A soft failure is only safe when a working install remains: continuing into
    a session with *no* tooling installed would leave the user with no ``vrg-*``
    commands. So when the update fails, check what is actually installed — warn
    and continue only if a version is still present, otherwise abort loudly.
    """
    try:
        update_tooling(instance, tag, fallback_tag=fallback_tag)
        return True
    except (subprocess.CalledProcessError, SystemExit):
        if get_tooling_version(instance) is None:
            print(
                "ERROR: vergil-tooling update failed and no working tooling "
                "remains in the VM (the uv cache is likely corrupt).\n"
                "Recover by rebuilding the VM, or clear the cache manually:\n"
                "  limactl shell <instance> -- bash -c "
                "'export PATH=\"$HOME/.local/bin:$PATH\" && uv cache clean'",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        print(
            "WARNING: vergil-tooling update failed — continuing with installed version",
            file=sys.stderr,
        )
        return False


_FINGERPRINT_PATH = "/etc/vergil/vm-spec.fingerprint"


class VmUnreachableError(RuntimeError):
    """Raised when the Lima shell transport to a VM fails (e.g. SSH connection
    refused) — the VM could not be contacted at all.

    Distinct from an absent spec marker: an unreachable VM says nothing about
    whether its spec drifted, so callers must not collapse this into the
    "needs-rebuild" signal. Doing so misreports a reachability failure as spec
    drift and tells the user to rebuild a VM that may just be mid-boot or wedged.
    """


def read_fingerprint(instance: str) -> str | None:
    """Return the spec fingerprint stamped into the VM, or None if the marker is absent.

    The in-guest read is masked (``cat ... 2>/dev/null || true``) so a missing
    marker yields empty stdout from a zero exit rather than a non-zero exit. Any
    remaining failure of the shell round-trip is therefore unambiguously a
    transport failure (the VM is unreachable), which is raised as
    ``VmUnreachableError`` instead of being collapsed into None — an unreachable
    VM is not a drifted VM.
    """
    try:
        result = shell_run(instance, "bash", "-c", f"cat {_FINGERPRINT_PATH} 2>/dev/null || true")
    except subprocess.CalledProcessError as exc:
        raise VmUnreachableError(instance) from exc
    value = result.stdout.strip()
    return value or None


def vm_spec_status(instance: str, expected_fingerprint: str) -> str:
    """Compare the VM's stamped fingerprint to the freshly composed one.

    Returns 'ok' on match, 'needs-rebuild' on drift (including a missing marker on a
    box that should carry one), and 'unreachable' when the VM cannot be contacted
    over the Lima shell transport. 'unreachable' is deliberately *not* drift: the
    spec was never read, so the caller must remediate reachability, not rebuild.
    """
    try:
        actual = read_fingerprint(instance)
    except VmUnreachableError:
        return "unreachable"
    return "ok" if actual == expected_fingerprint else "needs-rebuild"


# In-VM classifier: count agent vs human login sessions by walking each logind user
# tty/pty session's process subtree for `claude`. Direct counts, no subtraction.
_OCCUPANCY_SCRIPT = r"""
set -u
has_claude() {
  local pids="$1" p comm next
  while [ -n "$pids" ]; do
    next=""
    for p in $pids; do
      comm=$(cat "/proc/$p/comm" 2>/dev/null || echo "")
      [ "$comm" = "claude" ] && return 0
      next="$next $(pgrep -P "$p" 2>/dev/null || true)"
    done
    pids="$next"
  done
  return 1
}
agents=0; humans=0
for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do
  cls=$(loginctl show-session "$s" -p Class --value 2>/dev/null || echo "")
  typ=$(loginctl show-session "$s" -p Type --value 2>/dev/null || echo "")
  [ "$cls" = "user" ] || continue
  case "$typ" in tty|pty) ;; *) continue ;; esac
  leader=$(loginctl show-session "$s" -p Leader --value 2>/dev/null || echo "")
  [ -n "$leader" ] || continue
  if has_claude "$leader"; then agents=$((agents+1)); else humans=$((humans+1)); fi
done
echo "agents=$agents humans=$humans"
"""

_OCCUPANCY_RE = re.compile(r"agents=(\d+)\s+humans=(\d+)")

# Appended to the occupancy script so one shell round-trip yields both values.
# Failure to read the marker is masked in-script: an absent fingerprint is the
# empty string, matching read_fingerprint's absent/unreadable -> None contract.
_FINGERPRINT_PROBE = f'\necho "fingerprint=$(cat {_FINGERPRINT_PATH} 2>/dev/null || true)"\n'

_FINGERPRINT_RE = re.compile(r"^fingerprint=(.*)$", re.MULTILINE)


def vm_probe(instance: str, *, fingerprint: bool = False) -> tuple[int, int, str | None]:
    """Probe a running VM in a single shell round-trip.

    Returns (agents, humans, fingerprint). Occupancy keeps vm_occupancy's
    contract — (0, 0) only on parse/exec failure. The fingerprint is read in
    the same invocation when requested and is None when not requested, absent,
    or unreadable, matching read_fingerprint.
    """
    script = _OCCUPANCY_SCRIPT + _FINGERPRINT_PROBE if fingerprint else _OCCUPANCY_SCRIPT
    try:
        result = shell_run(instance, "bash", "-c", script)
    except subprocess.CalledProcessError:
        return (0, 0, None)
    match = _OCCUPANCY_RE.search(result.stdout)
    agents, humans = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
    stamped: str | None = None
    if fingerprint:
        fp_match = _FINGERPRINT_RE.search(result.stdout)
        if fp_match:
            stamped = fp_match.group(1).strip() or None
    return (agents, humans, stamped)


def vm_occupancy(instance: str) -> tuple[int, int]:
    """Return (agents, humans) for a running VM by process-tree classification.

    Agents are login sessions whose subtree roots `claude`; humans are interactive
    user tty/pty sessions that are not agent-hosting. Returns (0, 0) on any parse/exec
    failure rather than guessing.
    """
    agents, humans, _ = vm_probe(instance)
    return (agents, humans)
