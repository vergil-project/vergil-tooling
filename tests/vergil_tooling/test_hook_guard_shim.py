"""Tests for the hook-guard shim's jq fallback (both copies).

The shim execs ``vrg-hook-guard`` when it is installed; these tests run it
with a PATH that holds only the tools the fallback needs, so the jq path is
exercised. Both the scaffold template and this repo's own copy must agree
with ``vrg-hook-guard``: fail closed on input they cannot check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SHIMS = [
    _REPO / "src" / "vergil_tooling" / "data" / "hook_guard_shim.sh",
    _REPO / ".claude" / "hooks" / "guard.sh",
]
_FALLBACK_TOOLS = ("cat", "jq", "awk", "basename")


def _bin_dir(tmp_path: Path, tools: tuple[str, ...]) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in tools:
        found = shutil.which(tool)
        assert found is not None, f"{tool} is required to test the shim fallback"
        (bin_dir / tool).symlink_to(found)
    return bin_dir


def _run_shim(
    shim: Path, raw: str, tmp_path: Path, tools: tuple[str, ...] = _FALLBACK_TOOLS
) -> subprocess.CompletedProcess[str]:
    bash = shutil.which("bash")
    assert bash is not None
    return subprocess.run(  # noqa: S603 - fixed interpreter and argv
        [bash, str(shim)],
        input=raw,
        env={"PATH": str(_bin_dir(tmp_path, tools))},
        capture_output=True,
        text=True,
        check=False,
    )


def _payload(tool_input: object, tool_name: str = "Bash") -> str:
    return json.dumps({"cwd": "/repo", "tool_name": tool_name, "tool_input": tool_input})


@pytest.mark.parametrize("shim", _SHIMS, ids=lambda p: p.name)
class TestShimFallback:
    @pytest.mark.parametrize(
        ("raw", "problem"),
        [
            ("not json", "not valid JSON"),
            ('{"tool_input": {"command": "git sta', "not valid JSON"),
            ("", "empty"),
            ("  \n ", "empty"),
            ('["git"]', "not valid JSON"),
            (_payload("git status"), "tool_input.command"),
            (_payload({}), "tool_input.command"),
            (_payload({"command": None}), "tool_input.command"),
            (json.dumps({"tool_name": "Bash"}), "tool_input.command"),
        ],
    )
    def test_malformed_input_fails_closed(
        self, shim: Path, tmp_path: Path, raw: str, problem: str
    ) -> None:
        proc = _run_shim(shim, raw, tmp_path)
        assert proc.returncode == 2
        assert proc.stdout == ""
        assert proc.stderr.startswith("guard.sh: blocked: ")
        assert problem in proc.stderr

    def test_missing_jq_fails_closed(self, shim: Path, tmp_path: Path) -> None:
        proc = _run_shim(shim, _payload({"command": "ls"}), tmp_path, ("cat", "awk", "basename"))
        assert proc.returncode == 2
        assert "jq" in proc.stderr

    @pytest.mark.parametrize("command", ["git status", "gh pr list", "/usr/bin/git log"])
    def test_raw_git_gh_denied(self, shim: Path, tmp_path: Path, command: str) -> None:
        proc = _run_shim(shim, _payload({"command": command}), tmp_path)
        assert proc.returncode == 0
        assert proc.stderr == ""
        decision = json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny"

    @pytest.mark.parametrize("command", ["ls -la", "vrg-git status", "", "echo git"])
    def test_other_commands_allowed(self, shim: Path, tmp_path: Path, command: str) -> None:
        proc = _run_shim(shim, _payload({"command": command}), tmp_path)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    def test_non_bash_tool_passes_through(self, shim: Path, tmp_path: Path) -> None:
        proc = _run_shim(shim, _payload({"file_path": "/x"}, tool_name="Read"), tmp_path)
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


def test_shim_copies_differ_only_in_deny_reason() -> None:
    template, local = (p.read_text().splitlines() for p in _SHIMS)
    differing = [(a, b) for a, b in zip(template, local, strict=True) if a != b]
    assert len(differing) == 1
    assert all("permissionDecisionReason" in line for line in differing[0])
