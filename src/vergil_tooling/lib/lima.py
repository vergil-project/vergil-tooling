"""Lima VM subprocess wrappers."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING

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
    fingerprint: str | None = None,
) -> None:
    claude_projects_path = Path.home() / ".claude" / "projects"
    claude_skills_path = Path.home() / ".claude" / "skills"
    claude_sessions_path = Path.home() / ".claude" / "sessions"
    claude_projects_path.mkdir(parents=True, exist_ok=True)
    claude_skills_path.mkdir(parents=True, exist_ok=True)
    claude_sessions_path.mkdir(parents=True, exist_ok=True)
    claude_projects = str(claude_projects_path)
    claude_skills = str(claude_skills_path)
    claude_sessions = str(claude_sessions_path)

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
        f'--set=.mounts[3].location = "{claude_sessions}"',
        f'--set=.mounts[3].mountPoint = "{claude_sessions}"',
        "--set=.mounts[3].writable = true",
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
    if fingerprint:
        args.append(f'--set=.param.SPEC_FINGERPRINT = "{fingerprint}"')
    args.append(str(template))
    _limactl(*args)


def start_vm(instance: str, *, timeout: str = "30m") -> None:
    status = vm_status(instance)
    if status == "Running":
        return
    _limactl("start", f"--timeout={timeout}", instance)


def stop_vm(instance: str) -> None:
    status = vm_status(instance)
    if status != "Running":
        return
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


def install_tooling(instance: str, tag: str) -> None:
    """Install vergil-tooling inside the VM and record the tag."""
    install_spec = _TOOLING_INSTALL.format(tag=tag)
    print(f"  Installing vergil-tooling ({tag})...")
    shell_run(
        instance,
        "bash",
        "-c",
        f'export PATH="$HOME/.local/bin:$PATH" && uv tool install "{install_spec}"',
    )
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
    shell_run(
        instance,
        "bash",
        "-c",
        f'export PATH="$HOME/.local/bin:$PATH" && uv tool install --reinstall "{install_spec}"',
    )
    if not explicit:
        shell_run(instance, "bash", "-c", f"mkdir -p $(dirname {_TOOLING_TAG_FILE})")
        shell_pipe(instance, f"cat > {_TOOLING_TAG_FILE}", f"{tag}\n")


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


# Subdirs symlinked to the path-preserved host mounts so they are shared with
# the host and survive VM rebuilds. projects/ holds the conversation
# transcripts (append writes, which work fine through the virtiofs mount);
# skills/ is a read-only reference mount.
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
    """Update vergil-tooling, returning False on failure instead of aborting."""
    try:
        update_tooling(instance, tag, fallback_tag=fallback_tag)
        return True
    except (subprocess.CalledProcessError, SystemExit):
        print(
            "WARNING: vergil-tooling update failed — continuing with installed version",
            file=sys.stderr,
        )
        return False


_FINGERPRINT_PATH = "/etc/vergil/vm-spec.fingerprint"


def read_fingerprint(instance: str) -> str | None:
    """Return the spec fingerprint stamped into the VM, or None if absent/unreadable."""
    try:
        result = shell_run(instance, "cat", _FINGERPRINT_PATH)
    except subprocess.CalledProcessError:
        return None
    value = result.stdout.strip()
    return value or None


def vm_spec_status(instance: str, expected_fingerprint: str) -> str:
    """Compare the VM's stamped fingerprint to the freshly composed one.

    Returns 'ok' on match, 'needs-rebuild' on drift (including a missing marker on a
    box that should carry one).
    """
    actual = read_fingerprint(instance)
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
