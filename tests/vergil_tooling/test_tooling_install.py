from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.vergil_tooling.tooling_install_stubs import Run, run_lines, stub_bin, write_stub
from vergil_tooling.lib.tooling_install import (
    GIT_URL,
    ResolvedRef,
    ToolingInstallError,
    check_lines,
    git_ls_remote,
    install_lines,
    resolve,
    unresolved,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

SHA = "c" * 40
TAG_OBJECT = "d" * 40
SPEC = f"vergil-tooling @ git+{GIT_URL}@"
resolved_exact = ResolvedRef(ref="v2.1.10", kind="exact", tag="v2.1.10", sha=SHA)
resolved_line = ResolvedRef(ref="v2.1", kind="line", tag="v2.1.10", sha=SHA)
resolved_dev = ResolvedRef(ref="develop", kind="dev", tag=None, sha=None)


def fake(tags: dict[str, str], calls: list[tuple[str, str]] | None = None) -> Callable[..., str]:
    """An ``ls_remote`` listing *tags* (lightweight), recording its calls."""

    def ls_remote(url: str, pattern: str) -> str:
        if calls is not None:
            calls.append((url, pattern))
        return "".join(f"{sha}\trefs/tags/{name}\n" for name, sha in tags.items())

    return ls_remote


def _script(resolved: ResolvedRef, **kwargs: str | None) -> str:
    opts: dict[str, str | None] = {"python_dir_env": None, "tool_dir": None, "bin_dir": None}
    opts.update(kwargs)
    return "\n".join(install_lines(resolved, **opts))  # type: ignore[arg-type]


def _install_call(run: Run) -> list[str]:
    (install,) = [c for c in run.of("uv") if c[:2] == ["tool", "install"]]
    return install


def _env_of(run: Run, index: int) -> dict[str, str]:
    return dict(item.split("=", 1) for item in run.of("uv-env")[index])


# -- resolve ----------------------------------------------------------------


def test_resolve_line_picks_newest_patch_and_its_sha() -> None:
    r = resolve("v2.1", ls_remote=fake({"v2.1.9": "a" * 40, "v2.1.10": "b" * 40}))
    assert (r.tag, r.sha) == ("v2.1.10", "b" * 40)
    assert r.kind == "line"


def test_resolve_line_ignores_other_lines_and_suffixed_tags() -> None:
    tags = {"v2.1.3": "a" * 40, "v2.10.0": "b" * 40, "v2.1.4-rc1": "e" * 40}
    r = resolve("v2.1", ls_remote=fake(tags))
    assert (r.tag, r.sha) == ("v2.1.3", "a" * 40)


def test_resolve_uses_the_peeled_commit_of_an_annotated_tag() -> None:
    listing = f"{TAG_OBJECT}\trefs/tags/v2.1.10\n{SHA}\trefs/tags/v2.1.10^{{}}\n"
    r = resolve("v2.1.10", ls_remote=lambda _url, _pattern: listing)
    assert (r.kind, r.tag, r.sha) == ("exact", "v2.1.10", SHA)


def test_resolve_skips_non_tag_and_malformed_lines() -> None:
    listing = f"warning: noise\n{'a' * 40}\trefs/heads/v2.1.11\n{SHA}\trefs/tags/v2.1.10\n"
    r = resolve("v2.1", ls_remote=lambda _url, _pattern: listing)
    assert (r.tag, r.sha) == ("v2.1.10", SHA)


def test_resolve_makes_one_ls_remote_call_on_the_git_url() -> None:
    calls: list[tuple[str, str]] = []
    resolve("v2.1", ls_remote=fake({"v2.1.0": SHA}, calls))
    assert calls == [(GIT_URL, "refs/tags/v2.1*")]


def test_resolve_line_ls_remote_failure_is_loud() -> None:
    def broken(url: str, pattern: str) -> str:
        msg = "network down"
        raise ToolingInstallError(msg)

    with pytest.raises(ToolingInstallError) as excinfo:
        resolve("v2.1", ls_remote=broken)
    assert GIT_URL in str(excinfo.value)
    assert "v2.1" in str(excinfo.value)
    assert "network down" in str(excinfo.value)


def test_resolve_line_with_no_tag_on_the_line_is_loud() -> None:
    with pytest.raises(ToolingInstallError, match=r"no release tag for v2\.2 at https://"):
        resolve("v2.2", ls_remote=fake({"v2.1.9": SHA}))


def test_resolve_exact_missing_tag_is_loud() -> None:
    with pytest.raises(ToolingInstallError, match=r"v2\.1\.11"):
        resolve("v2.1.11", ls_remote=fake({"v2.1.10": SHA, "v2.1.110": SHA}))


def test_resolve_rejects_a_non_sha() -> None:
    with pytest.raises(ToolingInstallError, match="not a commit SHA"):
        resolve("v2.1.10", ls_remote=fake({"v2.1.10": "nonsense"}))


def test_resolve_dev_ref_has_no_sha() -> None:
    calls: list[tuple[str, str]] = []
    r = resolve("develop", ls_remote=fake({}, calls))
    assert r == ResolvedRef(ref="develop", kind="dev", tag=None, sha=None)
    assert calls == []


def test_unresolved_exact_needs_no_network() -> None:
    assert unresolved("v2.1.10") == ResolvedRef(
        ref="v2.1.10", kind="exact", tag="v2.1.10", sha=None
    )


@pytest.mark.parametrize("ref", ["v2.1", "develop"])
def test_unresolved_refuses_a_line_or_dev_ref(ref: str) -> None:
    with pytest.raises(ToolingInstallError, match="not an exact release"):
        unresolved(ref)


# -- git_ls_remote ----------------------------------------------------------


def test_git_ls_remote_returns_the_listing() -> None:
    done = subprocess.CompletedProcess((), 0, stdout="listing", stderr="")
    with patch("vergil_tooling.lib.tooling_install.subprocess.run", return_value=done) as run:
        assert git_ls_remote(GIT_URL, "refs/tags/v2.1*") == "listing"
    assert run.call_args.args[0] == ("git", "ls-remote", "--tags", GIT_URL, "refs/tags/v2.1*")


def test_git_ls_remote_failure_names_url_and_pattern() -> None:
    done = subprocess.CompletedProcess((), 128, stdout="", stderr="fatal: unreachable\n")
    with (
        patch("vergil_tooling.lib.tooling_install.subprocess.run", return_value=done),
        pytest.raises(ToolingInstallError, match=r"refs/tags/v2\.1\*.*exit 128.*unreachable"),
    ):
        git_ls_remote(GIT_URL, "refs/tags/v2.1*")


def test_git_ls_remote_missing_git_is_loud() -> None:
    with (
        patch(
            "vergil_tooling.lib.tooling_install.subprocess.run",
            side_effect=FileNotFoundError("git"),
        ),
        pytest.raises(ToolingInstallError, match="could not run"),
    ):
        git_ls_remote(GIT_URL, "refs/tags/v2.1*")


# -- install_lines: rendering ------------------------------------------------


def test_install_lines_use_sha_never_tag() -> None:
    lines = _script(resolved_exact)
    after = lines.split("uv tool install", 1)[1]
    assert "@$vt_sha" in after
    assert f"vt_sha={SHA}" in lines
    assert "@v2.1.10" not in after


def test_install_lines_clear_uv_python_vars_and_ignore_config() -> None:
    lines = _script(resolved_line)
    assert "unset UV_PYTHON UV_PYTHON_PREFERENCE UV_NO_MANAGED_PYTHON" in lines
    for flag in ("--no-config", "--python 3.15", "--managed-python"):
        assert flag in lines.split("uv tool install", 1)[1]
    assert "uv export --no-config" in lines


def test_constraints_exclude_the_project() -> None:
    lines = _script(resolved_line)
    export = lines.split("uv export", 1)[1].split("\n", 1)[0]
    for flag in ("--no-emit-project", "--no-dev", "--no-hashes", "--frozen"):
        assert flag in export


def test_install_lines_replace_existing_install() -> None:
    assert "uv tool install --force " in _script(resolved_exact)
    assert "uv tool install --force --reinstall " in _script(resolved_dev)


def test_dev_ref_installs_unconstrained() -> None:
    lines = _script(resolved_dev)
    assert "-c " not in lines
    assert "uv export" not in lines
    assert "git " not in lines
    assert f"'{SPEC}develop'" in lines


def test_install_lines_resolve_unresolved_exact_once_in_shell() -> None:
    lines = _script(unresolved("v2.1.10"))
    assert lines.count("git ls-remote") == 1
    assert "vt_sha=c" not in lines


def test_resolved_install_runs_no_ls_remote() -> None:
    assert "ls-remote" not in _script(resolved_line)


def test_install_lines_refuse_bad_inputs() -> None:
    with pytest.raises(ToolingInstallError, match="not a shell variable name"):
        _script(resolved_line, python_dir_env="A B")
    with pytest.raises(ToolingInstallError, match="neither an absolute path"):
        _script(resolved_line, bin_dir="relative/bin")
    with pytest.raises(ToolingInstallError, match="not a commit SHA"):
        _script(ResolvedRef(ref="v2.1", kind="line", tag="v2.1.1", sha="abc"))
    with pytest.raises(ToolingInstallError, match="has no tag"):
        _script(ResolvedRef(ref="v2.1", kind="line", tag=None, sha=SHA))


# -- install_lines: under bash -------------------------------------------------


def test_released_install_runs_the_locked_sequence(tmp_path: Path) -> None:
    run = run_lines(_script(resolved_line), tmp_path)
    assert run.returncode == 0, run.stderr
    git = run.of("git")
    assert git[0][:2] == ["init", "-q"]
    assert git[1][2:] == ["fetch", "-q", "--depth", "1", GIT_URL, SHA]
    assert git[2][2:] == ["checkout", "-q", "FETCH_HEAD"]
    export = run.of("uv")[0]
    assert export[0] == "export"
    assert _env_of(run, 0)["PWD"].endswith("/src")
    install = _install_call(run)
    constraints = install[install.index("-c") + 1]
    assert install[-1] == f"{SPEC}{SHA}"
    assert constraints.endswith("/constraints.txt")
    assert f"installed v2.1.10 (commit {SHA})" in run.stderr
    # The temp directory is gone.
    assert not list(tmp_path.glob("tmp.*"))


def test_install_runs_with_the_uv_python_vars_cleared(tmp_path: Path) -> None:
    run = run_lines(_script(resolved_line), tmp_path)
    assert run.returncode == 0, run.stderr
    for index in (0, 1):
        env = _env_of(run, index)
        assert env["UV_PYTHON"] == "<unset>"
        assert env["UV_PYTHON_PREFERENCE"] == "<unset>"
        assert env["UV_NO_MANAGED_PYTHON"] == "<unset>"


def test_unresolved_exact_resolves_in_shell_and_uses_the_peeled_sha(tmp_path: Path) -> None:
    listing = (
        f"{TAG_OBJECT}\trefs/tags/v2.1.10\n{SHA}\trefs/tags/v2.1.10^{{}}\n"
        f"{'e' * 40}\trefs/tags/v2.1.100\n"
    )
    run = run_lines(_script(unresolved("v2.1.10")), tmp_path, env={"STUB_LS_REMOTE": listing})
    assert run.returncode == 0, run.stderr
    ls_remotes = [c for c in run.of("git") if c[0] == "ls-remote"]
    assert ls_remotes == [["ls-remote", "--tags", GIT_URL, "refs/tags/v2.1.10*"]]
    fetch = next(c for c in run.of("git") if "fetch" in c)
    assert fetch[-1] == SHA
    assert _install_call(run)[-1] == f"{SPEC}{SHA}"


def test_unresolved_exact_lightweight_tag(tmp_path: Path) -> None:
    run = run_lines(
        _script(unresolved("v2.1.10")),
        tmp_path,
        env={"STUB_LS_REMOTE": f"{SHA}\trefs/tags/v2.1.10\n"},
    )
    assert run.returncode == 0, run.stderr
    assert _install_call(run)[-1] == f"{SPEC}{SHA}"


def test_unresolved_exact_missing_tag_fails_before_installing(tmp_path: Path) -> None:
    run = run_lines(
        _script(unresolved("v2.1.10")),
        tmp_path,
        env={"STUB_LS_REMOTE": f"{SHA}\trefs/tags/v2.1.100\n"},
    )
    assert run.returncode == 1
    assert f"no release tag v2.1.10 at {GIT_URL}" in run.stderr
    assert run.of("uv") == []


def test_unresolved_exact_ls_remote_failure_is_loud(tmp_path: Path) -> None:
    run = run_lines(_script(unresolved("v2.1.10")), tmp_path, env={"STUB_LS_REMOTE_RC": "128"})
    assert run.returncode == 1
    assert f"listing the tags of {GIT_URL} for v2.1.10 failed" in run.stderr
    assert run.of("uv") == []


@pytest.mark.parametrize("value", ["set", "unset"])
def test_install_lines_python_dir_env_branches_at_run_time(tmp_path: Path, value: str) -> None:
    env = {"VERGIL_CORE_PYTHON_DIR": "/opt/managed"} if value == "set" else {}
    run = run_lines(
        _script(resolved_line, python_dir_env="VERGIL_CORE_PYTHON_DIR"), tmp_path, env=env
    )
    assert run.returncode == 0, run.stderr
    uv_env = _env_of(run, 1)
    if value == "set":
        assert uv_env["UV_PYTHON_INSTALL_DIR"] == "/opt/managed"
        assert uv_env["UV_PYTHON_DOWNLOADS"] == "never"
        assert "uv may download" not in run.stderr
    else:
        assert uv_env["UV_PYTHON_INSTALL_DIR"] == "<unset>"
        assert uv_env["UV_PYTHON_DOWNLOADS"] == "<unset>"
        assert "vergil-tooling: no VERGIL_CORE_PYTHON_DIR; uv may download 3.15" in run.stderr


def test_tool_and_bin_dirs_are_exported(tmp_path: Path) -> None:
    run = run_lines(
        _script(resolved_dev, tool_dir="/opt/vergil/tools", bin_dir="$HOME/.local/bin"), tmp_path
    )
    assert run.returncode == 0, run.stderr
    env = _env_of(run, 0)
    assert env["UV_TOOL_DIR"] == "/opt/vergil/tools"
    assert env["UV_TOOL_BIN_DIR"] == f"{tmp_path}/home/.local/bin"


def test_dev_install_under_bash(tmp_path: Path) -> None:
    run = run_lines(_script(resolved_dev), tmp_path)
    assert run.returncode == 0, run.stderr
    assert run.of("git") == []
    assert _install_call(run)[-1] == f"{SPEC}develop"
    assert "installed dev ref develop (unconstrained)" in run.stderr


def test_hostile_dev_ref_is_never_run(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    ref = f'x";touch {marker};"$(touch {marker})`touch {marker}`'
    run = run_lines(
        _script(ResolvedRef(ref=ref, kind="dev", tag=None, sha=None)),
        tmp_path,
        env={"STUB_INSTALL_RC": "1"},
    )
    assert run.returncode == 1
    assert not marker.exists()
    assert ref in run.stderr


def test_hostile_uv_toml_changes_nothing(tmp_path: Path) -> None:
    work = tmp_path / "consumer"
    work.mkdir()
    (work / "uv.toml").write_text('python-preference = "only-system"\nno-managed-python = true\n')
    (work / "pyproject.toml").write_text('[tool.uv]\npython-downloads = "never"\n')
    run = run_lines(_script(resolved_line), tmp_path, cwd=work)
    assert run.returncode == 0, run.stderr
    export, install = run.of("uv")
    assert "--no-config" in export
    assert "--no-config" in install
    # The export runs in the fetched commit, never the consumer's tree.
    assert _env_of(run, 0)["PWD"] != str(work)
    assert install[install.index("--python") + 1] == "3.15"
    assert "--managed-python" in install


def test_failed_install_exits_nonzero_with_named_error(tmp_path: Path) -> None:
    script = _script(resolved_line) + "\necho after"
    run = run_lines(script, tmp_path, env={"STUB_INSTALL_RC": "2"})
    assert run.returncode == 1
    assert f"uv tool install of v2.1.10 (commit {SHA}) failed" in run.stderr
    assert "after" not in run.stdout
    assert not list(tmp_path.glob("tmp.*"))


def test_failed_lock_export_exits_nonzero_with_named_error(tmp_path: Path) -> None:
    run = run_lines(_script(resolved_line), tmp_path, env={"STUB_EXPORT_RC": "2"})
    assert run.returncode == 1
    assert f"exporting the uv.lock of commit {SHA} (v2.1.10) failed" in run.stderr
    assert [c for c in run.of("uv") if c[0] == "tool"] == []


def test_failed_commit_fetch_exits_nonzero_with_named_error(tmp_path: Path) -> None:
    run = run_lines(_script(resolved_line), tmp_path, env={"STUB_FETCH_RC": "128"})
    assert run.returncode == 1
    assert f"fetching commit {SHA} (v2.1.10) from {GIT_URL} failed" in run.stderr
    assert run.of("uv") == []


def test_empty_lock_export_fails(tmp_path: Path) -> None:
    run = run_lines(_script(resolved_line), tmp_path, env={"STUB_EXPORT": ""})
    assert run.returncode == 1
    assert "is empty" in run.stderr
    assert [c for c in run.of("uv") if c[0] == "tool"] == []


@pytest.mark.parametrize(
    "line",
    ["-e .", "vergil-tooling==2.1.10", f"vergil-tooling @ git+{GIT_URL}@x", "./local", "git+x"],
)
def test_constraints_naming_the_project_fail(tmp_path: Path, line: str) -> None:
    export = f"# via vergil-tooling\nrich==15.0.0\n    # via vergil-tooling\n{line}\n"
    run = run_lines(_script(resolved_line), tmp_path, env={"STUB_EXPORT": export})
    assert run.returncode == 1
    assert "names a non-registry package" in run.stderr
    assert [c for c in run.of("uv") if c[0] == "tool"] == []


def test_constraints_comments_naming_the_project_pass(tmp_path: Path) -> None:
    export = "rich==15.0.0\n    # via vergil-tooling\nvergil-toolingx==1.0\n"
    run = run_lines(_script(resolved_line), tmp_path, env={"STUB_EXPORT": export})
    assert run.returncode == 0, run.stderr


# -- check_lines ---------------------------------------------------------------


def test_check_lines_pass_when_vrg_whoami_is_in_bin_dir(tmp_path: Path) -> None:
    bin_dir = stub_bin(tmp_path)
    run = run_lines("\n".join(check_lines(str(bin_dir))), tmp_path, path=[bin_dir])
    assert run.returncode == 0, run.stderr


def test_check_lines_fail_on_shadowing_usr_bin_shim(tmp_path: Path) -> None:
    usr_bin = tmp_path / "usr" / "bin"
    write_stub(usr_bin, "vrg-whoami", "#!/bin/bash\nexit 0\n")
    tool_bin = stub_bin(tmp_path)
    run = run_lines("\n".join(check_lines(str(tool_bin))), tmp_path, path=[usr_bin, tool_bin])
    assert run.returncode == 1
    assert f"vrg-whoami resolves to {usr_bin}/vrg-whoami, not {tool_bin}/vrg-whoami" in run.stderr


def test_check_lines_fail_when_vrg_whoami_is_missing(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    run = run_lines("\n".join(check_lines("$HOME/.local/bin")), tmp_path, path=[empty])
    assert run.returncode == 1
    assert f"resolves to nothing, not {tmp_path}/home/.local/bin/vrg-whoami" in run.stderr


def test_check_lines_fail_when_vrg_whoami_does_not_run(tmp_path: Path) -> None:
    bin_dir = stub_bin(tmp_path)
    run = run_lines(
        "\n".join(check_lines(str(bin_dir))),
        tmp_path,
        path=[bin_dir],
        env={"STUB_WHOAMI_RC": "1"},
    )
    assert run.returncode == 1
    assert f"{bin_dir}/vrg-whoami --mode failed" in run.stderr
