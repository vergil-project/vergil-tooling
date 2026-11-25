"""vrg-tooling-install: the Mac's locked install of a ref (vergil-tooling#4827, #4841).

A line ``vX.Y`` installs the exact release its rolling tag names (epic
vergil-project/.github#1440, spec §4.6).
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from tests.vergil_tooling.tooling_install_stubs import STUB_SHA, stub_bin
from vergil_tooling.bin import vrg_tooling_install
from vergil_tooling.lib import tooling_install
from vergil_tooling.lib.tooling_install import (
    GIT_URL,
    Installed,
    ToolingInstallError,
    install_ref,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_TAG = "v2.1.10"
_UV_VARS = ("UV_PYTHON", "UV_PYTHON_PREFERENCE", "UV_NO_MANAGED_PYTHON")


@pytest.fixture
def stubs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the git and uv stubs first on PATH, and the temp root under *tmp_path*.

    Returns the stub call log. The three ``UV_*`` interpreter variables are
    set, to show the install clears them.
    """
    log = tmp_path / "stub.log"
    log.write_text("")
    monkeypatch.setenv("PATH", os.pathsep.join([str(stub_bin(tmp_path)), "/usr/bin", "/bin"]))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setenv("UV_PYTHON", "/opt/hostile/python")
    monkeypatch.setenv("UV_PYTHON_PREFERENCE", "only-system")
    monkeypatch.setenv("UV_NO_MANAGED_PYTHON", "1")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    return log


def _calls(log: Path, command: str) -> list[list[str]]:
    rows = [line.split("\t") for line in log.read_text().splitlines()]
    return [row[1:] for row in rows if row[0] == command]


def _temp_left(log: Path) -> list[Path]:
    return list((log.parent / "temp").iterdir())


def test_installs_the_tag_commit_with_its_lock(stubs: Path) -> None:
    assert install_ref(_TAG) == Installed(ref=_TAG, sha=STUB_SHA)

    git = _calls(stubs, "git")
    assert git[0][:2] == ["init", "-q"]
    src = git[0][2]
    assert git[1] == ["-C", src, "fetch", "-q", "--depth", "1", GIT_URL, f"refs/tags/{_TAG}"]
    assert git[2] == ["-C", src, "checkout", "-q", "FETCH_HEAD"]
    assert git[3] == ["-C", src, "rev-parse", "HEAD"]
    assert not any("ls-remote" in call for call in git)

    export, install = _calls(stubs, "uv")
    assert export[0] == "export"
    for flag in ("--no-config", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes"):
        assert flag in export
    constraints = export[export.index("-o") + 1]
    assert install[:2] == ["tool", "install"]
    for flag in ("--force", "--no-config", "--managed-python"):
        assert flag in install
    assert install[install.index("--python") + 1] == "3.15"
    assert install[install.index("-c") + 1] == constraints
    # The code installs from the same commit the lock was read from.
    assert install[-1] == f"vergil-tooling @ git+{GIT_URL}@{STUB_SHA}"

    uv_env = _calls(stubs, "uv-env")
    for seen in uv_env:
        env = dict(item.split("=", 1) for item in seen)
        for name in _UV_VARS:
            assert env[name] == "<unset>"
    # The export runs in the checkout, where the tag's uv.lock is.
    assert dict(item.split("=", 1) for item in uv_env[0])["PWD"] == src
    assert _temp_left(stubs) == []


def _no_listing(url: str, pattern: str) -> str:
    msg = f"unexpected ls-remote: {url} {pattern}"
    raise AssertionError(msg)


def _no_refs(url: str, patterns: tuple[str, ...]) -> str:
    msg = f"unexpected ls-remote: {url} {patterns}"
    raise AssertionError(msg)


def _refs(listing: str, calls: list[tuple[str, ...]] | None = None) -> Callable[..., str]:
    """An *ls_refs* returning *listing*, recording each call's patterns in *calls*."""

    def ls_refs(url: str, patterns: tuple[str, ...]) -> str:
        assert url == GIT_URL
        if calls is not None:
            calls.append(patterns)
        return listing

    return ls_refs


_BRANCH_SHA = "b" * 40


@pytest.mark.parametrize(
    "ref",
    [
        "-x",
        "--upload-pack=touch /tmp/pwned",
        "",
        "https://example.com/other.git",
        "git@github.com:other/repo",
        "develop:refs/heads/x",
        "+develop",
        "^develop",
        "dev elop",
        "dev\nelop",
        "a..b",
        "a@{1}",
        "a//b",
        "/develop",
        "develop/",
        "develop.",
        "develop.lock",
        ".hidden",
        "feature/.x",
        "@",
        "HEAD~1",
        "v2.*",
        "refs/pull/1/head",
        "refs/remotes/origin/develop",
    ],
)
def test_refuses_what_is_not_a_ref_name(stubs: Path, ref: str) -> None:
    with pytest.raises(ToolingInstallError, match=r"not a git ref name|'-'|empty|neither a branch"):
        install_ref(ref, ls_remote=_no_listing, ls_refs=_no_refs)
    assert _calls(stubs, "git") == []


def test_a_leading_dash_is_refused_as_an_option(stubs: Path) -> None:
    with pytest.raises(ToolingInstallError, match="starts with '-'; a ref is never an option"):
        install_ref("-develop", ls_refs=_no_refs)
    assert _calls(stubs, "git") == []


def test_a_branch_installs_its_commit_with_its_lock(stubs: Path) -> None:
    calls: list[tuple[str, ...]] = []
    listing = f"{STUB_SHA}\trefs/heads/develop\n"
    assert install_ref("develop", ls_refs=_refs(listing, calls)) == Installed(
        ref="develop", sha=STUB_SHA
    )
    # One lookup, among the branches and tags only.
    assert calls == [("refs/heads/develop", "refs/tags/develop", "refs/tags/develop^{}")]
    git = _calls(stubs, "git")
    src = git[0][2]
    # The commit the branch named is fetched, by its SHA.
    assert git[1] == ["-C", src, "fetch", "-q", "--depth", "1", GIT_URL, STUB_SHA]
    export, install = _calls(stubs, "uv")
    assert export[0] == "export"
    assert install[install.index("-c") + 1] == export[export.index("-o") + 1]
    assert install[-1] == f"vergil-tooling @ git+{GIT_URL}@{STUB_SHA}"
    assert _temp_left(stubs) == []


def test_a_full_branch_name_is_looked_up_as_itself(stubs: Path) -> None:
    calls: list[tuple[str, ...]] = []
    listing = f"{STUB_SHA}\trefs/heads/develop\n"
    assert install_ref("refs/heads/develop", ls_refs=_refs(listing, calls)).sha == STUB_SHA
    assert calls == [("refs/heads/develop",)]


def _like_ls_remote(refs: dict[str, str]) -> Callable[..., str]:
    """An *ls_refs* listing only the *refs* that one of its patterns names exactly.

    Real ``git ls-remote`` lists an annotated tag's peeled ``<tag>^{}`` line only
    when a pattern asks for it, so a lookup that forgets it gets the tag object.
    """

    def ls_refs(_url: str, patterns: tuple[str, ...]) -> str:
        return "".join(f"{sha}\t{name}\n" for name, sha in refs.items() if name in patterns)

    return ls_refs


_ANNOTATED = {"refs/tags/rc-test": "d" * 40, "refs/tags/rc-test^{}": STUB_SHA}


@pytest.mark.parametrize("ref", ["rc-test", "refs/tags/rc-test"])
def test_an_annotated_non_release_tag_installs_its_peeled_commit(stubs: Path, ref: str) -> None:
    assert install_ref(ref, ls_refs=_like_ls_remote(_ANNOTATED)) == Installed(ref=ref, sha=STUB_SHA)
    assert _calls(stubs, "git")[1][-1] == STUB_SHA


def test_a_full_tag_name_asks_for_its_peeled_line(stubs: Path) -> None:
    calls: list[tuple[str, ...]] = []
    listing = f"{STUB_SHA}\trefs/tags/rc-test\n"
    assert install_ref("refs/tags/rc-test", ls_refs=_refs(listing, calls)).sha == STUB_SHA
    assert calls == [("refs/tags/rc-test", "refs/tags/rc-test^{}")]


def test_a_lightweight_non_release_tag_installs_its_commit(stubs: Path) -> None:
    refs = {"refs/tags/rc-test": STUB_SHA}
    assert install_ref("rc-test", ls_refs=_like_ls_remote(refs)).sha == STUB_SHA


def test_a_commit_sha_installs_itself_with_no_lookup(stubs: Path) -> None:
    assert install_ref(STUB_SHA, ls_refs=_no_refs) == Installed(ref=STUB_SHA, sha=STUB_SHA)
    git = _calls(stubs, "git")
    assert git[1][-1] == STUB_SHA
    assert not any("ls-remote" in call for call in git)


def test_a_ref_that_does_not_resolve_installs_nothing(stubs: Path) -> None:
    listing = f"{STUB_SHA}\trefs/heads/develop-old\n"
    with pytest.raises(ToolingInstallError, match=f"no branch or tag nope at {GIT_URL}"):
        install_ref("nope", ls_refs=_refs(listing))
    assert _calls(stubs, "git") == []


def test_a_short_sha_does_not_resolve(stubs: Path) -> None:
    with pytest.raises(ToolingInstallError, match="full 40-character SHA"):
        install_ref("0123456", ls_refs=_refs(""))
    assert _calls(stubs, "git") == []


def test_a_name_that_is_both_branch_and_tag_is_refused(stubs: Path) -> None:
    listing = f"{STUB_SHA}\trefs/heads/x\n{_BRANCH_SHA}\trefs/tags/x\n"
    with pytest.raises(ToolingInstallError, match="both a branch and a tag.*refs/heads/x or"):
        install_ref("x", ls_refs=_refs(listing))
    assert _calls(stubs, "git") == []


def test_a_lookup_skips_malformed_and_non_ref_lines(stubs: Path) -> None:
    listing = (
        f"warning: noise\n{_BRANCH_SHA}\tHEAD\n{_BRANCH_SHA} refs/heads/develop extra\n"
        f"{STUB_SHA}\trefs/heads/develop\n"
    )
    assert install_ref("develop", ls_refs=_refs(listing)).sha == STUB_SHA


def test_a_listed_non_sha_is_refused(stubs: Path) -> None:
    with pytest.raises(ToolingInstallError, match="not a commit SHA"):
        install_ref("develop", ls_refs=_refs("zzz\trefs/heads/develop\n"))
    assert _calls(stubs, "git") == []


def test_a_failed_lookup_is_loud(stubs: Path) -> None:
    def failing(_url: str, _patterns: tuple[str, ...]) -> str:
        msg = "network down"
        raise ToolingInstallError(msg)

    with pytest.raises(ToolingInstallError, match=f"could not list the refs of {GIT_URL}.*down"):
        install_ref("develop", ls_refs=failing)
    assert _calls(stubs, "git") == []


def test_a_branch_checked_out_off_its_commit_is_refused(stubs: Path) -> None:
    listing = f"{_BRANCH_SHA}\trefs/heads/develop\n"
    with pytest.raises(ToolingInstallError, match=rf"not the commit {_BRANCH_SHA} the branch"):
        install_ref("develop", ls_refs=_refs(listing))
    assert [c for c in _calls(stubs, "uv") if c[0] == "tool"] == []
    assert _temp_left(stubs) == []


def test_a_branch_lookup_uses_git_ls_refs_by_default(
    stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_LS_REMOTE", f"{STUB_SHA}\trefs/heads/develop\n")
    assert install_ref("develop").sha == STUB_SHA
    (lookup,) = [c for c in _calls(stubs, "git") if c[0] == "ls-remote"]
    assert lookup == [
        *("ls-remote", GIT_URL),
        *("refs/heads/develop", "refs/tags/develop", "refs/tags/develop^{}"),
    ]


def test_git_ls_refs_failure_names_url_and_patterns(
    stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_LS_REMOTE_RC", "2")
    with pytest.raises(ToolingInstallError, match=f"git ls-remote {GIT_URL} refs/heads/x failed"):
        tooling_install.git_ls_refs(GIT_URL, ("refs/heads/x",))


def test_git_ls_refs_missing_git_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ToolingInstallError, match="could not run"):
        tooling_install.git_ls_refs(GIT_URL, ("refs/heads/x",))


def _line_listing(line_sha: str) -> Callable[[str, str], str]:
    """A listing where v2.1 names v2.1.10 (on *line_sha*), while v2.1.11 is newer."""

    def ls_remote(_url: str, pattern: str) -> str:
        assert pattern == "refs/tags/v2.1*"
        return (
            f"{line_sha}\trefs/tags/v2.1.10\n{'e' * 40}\trefs/tags/v2.1.11\n"
            f"{line_sha}\trefs/tags/v2.1\n"
        )

    return ls_remote


def test_install_ref_line_installs_and_reports_exact_tag(stubs: Path) -> None:
    assert install_ref("v2.1", ls_remote=_line_listing(STUB_SHA)) == Installed(
        ref=_TAG, sha=STUB_SHA
    )
    git = _calls(stubs, "git")
    assert git[1][-1] == f"refs/tags/{_TAG}"
    (install,) = [c for c in _calls(stubs, "uv") if c[0] == "tool"]
    assert install[-1] == f"vergil-tooling @ git+{GIT_URL}@{STUB_SHA}"


def test_install_ref_line_uses_git_ls_remote_by_default(
    stubs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tooling_install, "git_ls_remote", _line_listing(STUB_SHA))
    assert install_ref("v2.1").ref == _TAG


def test_install_ref_line_that_does_not_resolve_installs_nothing(stubs: Path) -> None:
    with pytest.raises(ToolingInstallError, match="the line tag v2.2 does not exist"):
        install_ref("v2.2", ls_remote=lambda _u, _p: f"{STUB_SHA}\trefs/tags/v2.2.0\n")
    assert _calls(stubs, "git") == []


def test_install_ref_line_refuses_a_checkout_off_the_line_commit(stubs: Path) -> None:
    with pytest.raises(ToolingInstallError, match=r"not the commit a{40} the line's tag names"):
        install_ref("v2.1", ls_remote=_line_listing("a" * 40))
    assert [c for c in _calls(stubs, "uv") if c[0] == "tool"] == []
    assert _temp_left(stubs) == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"STUB_FETCH_RC": "3"}, f"fetching tag {_TAG} from {GIT_URL} failed .exit 3."),
        ({"STUB_REV_PARSE": "not-a-sha"}, "not a commit SHA"),
        ({"STUB_EXPORT_RC": "4"}, f"exporting the uv.lock of {_TAG} .*failed .exit 4.: error"),
        ({"STUB_EXPORT": ""}, "is empty"),
        ({"STUB_EXPORT": "rich==15.0.0\n-e ./local\n"}, "non-registry package: '-e ./local'"),
        ({"STUB_EXPORT": "vergil-tooling==2.1.10\n"}, "non-registry package"),
        ({"STUB_EXPORT": "foo @ git+https://x/y\ngit+https://x/y\n"}, "non-registry package"),
    ],
)
def test_a_failed_step_stops_before_the_install(
    stubs: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ToolingInstallError, match=message):
        install_ref(_TAG)
    assert [c for c in _calls(stubs, "uv") if c[0] == "tool"] == []
    assert _temp_left(stubs) == []


def test_a_failed_install_raises(stubs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUB_INSTALL_RC", "5")
    with pytest.raises(ToolingInstallError, match=rf"uv tool install of {_TAG} .*failed .exit 5."):
        install_ref(_TAG)
    assert _temp_left(stubs) == []


def test_a_missing_tool_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ToolingInstallError, match="git could not run"):
        install_ref(_TAG)


def test_an_install_that_cannot_run_raises(stubs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = subprocess.run

    def run(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401
        if argv[:2] == ("uv", "tool"):
            raise FileNotFoundError(2, "No such file or directory", "uv")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(tooling_install.subprocess, "run", run)
    with pytest.raises(ToolingInstallError, match=rf"uv tool install of {_TAG} .*could not run"):
        install_ref(_TAG)
    assert _temp_left(stubs) == []


def test_an_unreadable_export_raises(stubs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_read = Path.read_text

    def read_text(self: Path, *args: Any, **kwargs: Any) -> str:  # noqa: ANN401
        if self.name == "constraints.txt":
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(ToolingInstallError, match="could not be read: .*Permission denied"):
        install_ref(_TAG)
    assert [c for c in _calls(stubs, "uv") if c[0] == "tool"] == []


def test_main_reports_the_install(stubs: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_tooling_install.main([_TAG]) == 0
    out = capsys.readouterr().out
    assert f"vrg-tooling-install: installed {_TAG} (commit {STUB_SHA})" in out


def test_main_reports_a_failure(
    stubs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STUB_FETCH_RC", "3")
    assert vrg_tooling_install.main([_TAG]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"vrg-tooling-install: fetching tag {_TAG}")
    assert _calls(stubs, "uv") == []


def test_main_needs_a_ref(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_tooling_install.main([])
    assert exc.value.code == 2
    assert "required: ref" in capsys.readouterr().err


def test_main_never_reads_a_dash_argument_as_a_ref(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_tooling_install.main(["--upload-pack=x"])
    assert exc.value.code == 2
    assert "required: ref" in capsys.readouterr().err


def test_main_refuses_a_dash_ref_after_double_dash(
    stubs: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert vrg_tooling_install.main(["--", "-x"]) == 1
    assert "starts with '-'" in capsys.readouterr().err
    assert _calls(stubs, "git") == []


def test_vrg_tooling_install_help_names_every_ref_kind(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_tooling_install.main(["--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "a release line, e.g. v2.1" in out
    assert "an exact release tag, e.g. v2.1.330" in out
    assert "full commit SHA" in out
    assert "never a URL" in out


def test_main_reports_a_branch_with_its_commit(
    stubs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STUB_LS_REMOTE", f"{STUB_SHA}\trefs/heads/develop\n")
    assert vrg_tooling_install.main(["develop"]) == 0
    out = capsys.readouterr().out
    assert f"vrg-tooling-install: installed develop (commit {STUB_SHA})" in out


def test_main_reports_the_exact_tag_a_line_installs(
    stubs: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tooling_install, "git_ls_remote", _line_listing(STUB_SHA))
    assert vrg_tooling_install.main(["v2.1"]) == 0
    out = capsys.readouterr().out
    assert f"vrg-tooling-install: installed {_TAG} (commit {STUB_SHA})" in out
