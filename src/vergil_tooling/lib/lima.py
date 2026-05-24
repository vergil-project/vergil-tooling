"""Lima VM subprocess wrappers."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
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


def create_vm(instance: str, template: Path, projects_dir: str) -> None:
    _limactl(
        "create",
        f"--name={instance}",
        "--tty=false",
        f'--set=.mounts[0].location = "{projects_dir}"',
        str(template),
    )


def start_vm(instance: str) -> None:
    status = vm_status(instance)
    if status == "Running":
        return
    _limactl("start", instance)


def stop_vm(instance: str) -> None:
    status = vm_status(instance)
    if status != "Running":
        return
    _limactl("stop", instance)


def delete_vm(instance: str) -> None:
    _limactl("delete", "--force", instance)


def inject_credentials(instance: str, identity: Identity) -> None:
    """Inject GitHub App and Claude Code credentials into a running VM."""
    key_path = Path(identity.private_key_path).expanduser()
    if not key_path.exists():
        print(f"ERROR: private key not found: {key_path}", file=sys.stderr)
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


def install_tooling(instance: str, tag: str) -> None:
    """Install vergil-tooling inside the VM."""
    install_spec = _TOOLING_INSTALL.format(tag=tag)
    print(f"  Installing vergil-tooling ({tag})...")
    shell_run(
        instance,
        "bash",
        "-c",
        f'export PATH="$HOME/.local/bin:$PATH" && uv tool install "{install_spec}"',
    )
