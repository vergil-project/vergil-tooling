"""The draft relay ref: pushed with plumbing to a local bare remote, read back over a fake REST."""

from __future__ import annotations

import base64
import hashlib
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib import git, github
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.draft_ref import (
    delete_draft_api,
    delete_scope_draft_api,
    draft_ref,
    fetch_draft,
    fetch_scope_draft,
    push_draft,
    push_scope_draft,
    reply_command,
    require_remote_is,
    scope_command,
    scope_draft_ref,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603
        ("git", *args),  # noqa: S607
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


class Clone:
    """A clone wired to a local bare origin, with a commit on ``develop``."""

    def __init__(self, path: Path, remote: Path) -> None:
        self.path = path
        self.remote = remote
        self.head = _git("rev-parse", "HEAD", cwd=path)
        self.status = _git("status", "--porcelain", "--ignored", cwd=path)
        self.index = _git("ls-files", "--stage", cwd=path)

    def head_unchanged(self) -> bool:
        return _git("rev-parse", "HEAD", cwd=self.path) == self.head

    def status_clean(self) -> bool:
        return (
            _git("status", "--porcelain", "--ignored", cwd=self.path) == self.status
            and _git("ls-files", "--stage", cwd=self.path) == self.index
        )

    def remote_file(self, ref: str, path: str) -> str:
        return subprocess.run(  # noqa: S603
            ("git", "show", f"{ref}:{path}"),  # noqa: S607
            cwd=self.remote,
            check=True,
            text=True,
            capture_output=True,
        ).stdout

    def remote_tree(self, ref: str) -> list[str]:
        return _git("ls-tree", "-r", "--name-only", ref, cwd=self.remote).splitlines()


@pytest.fixture
def git_repo_with_remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Clone:
    # No global or system config (an insteadOf rewrite, say) reaches these tests.
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    _git("init", str(clone), cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=clone)
    _git("config", "user.email", "test@example.com", cwd=clone)
    _git("remote", "add", "origin", str(remote), cwd=clone)
    (clone / "README.md").write_text("hi\n")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "init", cwd=clone)
    _git("branch", "-M", "develop", cwd=clone)
    _git("push", "origin", "develop", cwd=clone)
    (clone / "staged.txt").write_text("staged\n")
    _git("add", "staged.txt", cwd=clone)
    (clone / "untracked.txt").write_text("untracked\n")
    monkeypatch.chdir(clone)
    return Clone(clone, remote)


def test_draft_ref_and_reply_command() -> None:
    assert draft_ref(7, 41) == "refs/vergil/ruling-draft/7/41"
    assert reply_command("o/r", 7, 41) == "vrg-ruling-reply --issue 'o/r#7' --request 41"


# --- push_draft ----------------------------------------------------------------


def test_push_draft_is_plumbing_only(git_repo_with_remote: Clone) -> None:
    push_draft(7, 41, "fix R1.1\n")
    assert git_repo_with_remote.head_unchanged()
    assert git_repo_with_remote.status_clean()
    assert git_repo_with_remote.remote_file(draft_ref(7, 41), "reply.md") == "fix R1.1\n"
    assert git_repo_with_remote.remote_tree(draft_ref(7, 41)) == ["reply.md"]
    # No branch was created or moved on the remote.
    remote = git_repo_with_remote.remote
    heads = _git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=remote)
    assert heads == "refs/heads/develop"


def test_push_draft_force_overwrites(git_repo_with_remote: Clone) -> None:
    push_draft(7, 41, "R1.1: fix\n")
    push_draft(7, 41, "R1.1: file\n")
    assert git_repo_with_remote.remote_file(draft_ref(7, 41), "reply.md") == "R1.1: file\n"


def test_push_draft_keeps_stdout_clean(
    git_repo_with_remote: Clone, capsys: pytest.CaptureFixture[str]
) -> None:
    del git_repo_with_remote
    push_draft(7, 41, "R1.1: fix\n")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "bad", ["R1.1: caf\u00e9\n", "a\u202eb\n", "a\u200bb\n", "a\tb\n", "\x1b[31mred\n"]
)
def test_push_draft_refuses_non_plain_text(git_repo_with_remote: Clone, bad: str) -> None:
    with pytest.raises(RulingError, match="not plain text"):
        push_draft(7, 41, bad)
    assert not _git("ls-remote", "origin", draft_ref(7, 41), cwd=git_repo_with_remote.path)


def test_push_draft_failure_is_a_ruling_error(git_repo_with_remote: Clone, tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.git")
    _git("remote", "set-url", "origin", missing, cwd=git_repo_with_remote.path)
    with pytest.raises(RulingError, match="pushing the draft to refs/vergil/ruling-draft/7/41"):
        push_draft(7, 41, "R1.1: fix\n")


# --- require_remote_is -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r.git",
        "https://github.com/o/r",
        "https://x-access-token:secret@github.com/o/r.git",
        "git@github.com:o/r.git",
        "ssh://git@github.com/o/r.git",
        "ssh://git@github.com:22/O/R",
        "git://github.com/o/r.git",
        # #4088: the forms the shared github_remote_repo parser adds.
        "git+ssh://git@github.com/o/r.git",
        "ssh+git://git@github.com/o/r.git",
        "github.com:o/r.git",
        "git@github.com:/o/r.git",
    ],
)
def test_require_remote_is_accepts_the_issue_repo(git_repo_with_remote: Clone, url: str) -> None:
    _git("remote", "set-url", "origin", url, cwd=git_repo_with_remote.path)
    require_remote_is("o/r")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/other.git",
        "https://github.com/x/r.git",
        "git@github.com:o/r-fork.git",
        "https://example.com/o/r.git",
        "https://github.com.evil.example/o/r.git",
        "https://x-access-token:secret@github.com/o/rr.git",
        "https://github.com/o/r/extra",
        "ftp://github.com/o/r.git",
        # #4088: a user part with '#', '?' or '\' puts the host a URL parser
        # sees off github.com (or leaves it ambiguous); the old regex read
        # these as github.com/o/r, the shared parser refuses them.
        "https://evil.example#secret@github.com/o/r.git",
        "https://evil.example?secret@github.com/o/r.git",
        "https://evil.example\\secret@github.com/o/r.git",
    ],
)
def test_require_remote_is_refuses_another_repo(git_repo_with_remote: Clone, url: str) -> None:
    _git("remote", "set-url", "origin", url, cwd=git_repo_with_remote.path)
    with pytest.raises(RulingError, match="run vrg-ruling draft in a clone of o/r") as exc:
        require_remote_is("o/r")
    assert "secret" not in str(exc.value)


def test_require_remote_is_checks_every_push_url(git_repo_with_remote: Clone) -> None:
    clone = git_repo_with_remote.path
    _git("remote", "set-url", "origin", "https://github.com/o/r.git", cwd=clone)
    _git("remote", "set-url", "--push", "origin", "https://github.com/o/r.git", cwd=clone)
    other = "https://github.com/o/other.git"
    _git("remote", "set-url", "--add", "--push", "origin", other, cwd=clone)
    with pytest.raises(RulingError, match="pushes to o/other"):
        require_remote_is("o/r")


def test_require_remote_is_uses_the_push_url(git_repo_with_remote: Clone) -> None:
    clone = git_repo_with_remote.path
    _git("remote", "set-url", "origin", "https://github.com/o/r.git", cwd=clone)
    _git("remote", "set-url", "--push", "origin", "https://github.com/o/other.git", cwd=clone)
    with pytest.raises(RulingError, match="pushes to o/other"):
        require_remote_is("o/r")


def test_require_remote_is_refuses_no_push_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git, "read_output", lambda *_args: "")
    with pytest.raises(RulingError, match="has no push URL"):
        require_remote_is("o/r")


def test_require_remote_is_refuses_without_origin(git_repo_with_remote: Clone) -> None:
    _git("remote", "remove", "origin", cwd=git_repo_with_remote.path)
    with pytest.raises(RulingError, match="cannot read the push URL"):
        require_remote_is("o/r")


# --- fetch_draft -----------------------------------------------------------------


def _not_found() -> github.GitHubAPIError:
    body = '{"message":"Not Found"}'
    return github.GitHubAPIError(1, ("gh", "api"), body, "gh: Not Found (HTTP 404)\n")


def _sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


class FakeGh:
    """The git database endpoints of ``o/r``, served from a dict."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[str] = []

    def ref(self, name: str, *, blob_text: str | None = None, blob: bytes | None = None) -> None:
        raw = blob if blob is not None else (blob_text or "").encode()
        blob_sha = _sha(raw)
        commit_sha, tree_sha = f"c{blob_sha[1:]}", f"d{blob_sha[1:]}"
        self.responses[f"repos/o/r/git/ref/{name}"] = {
            "ref": f"refs/{name}",
            "object": {"type": "commit", "sha": commit_sha},
        }
        self.responses[f"repos/o/r/git/commits/{commit_sha}"] = {"tree": {"sha": tree_sha}}
        self.responses[f"repos/o/r/git/trees/{tree_sha}"] = {
            "truncated": False,
            "tree": [{"path": "reply.md", "type": "blob", "mode": "100644", "sha": blob_sha}],
        }
        encoded = base64.b64encode(raw).decode()
        self.responses[f"repos/o/r/git/blobs/{blob_sha}"] = {
            "encoding": "base64",
            # GitHub wraps the base64 content at 60 characters.
            "content": "\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60)),
            "sha": blob_sha,
        }

    def read_json(self, *args: str) -> Any:
        assert args[0] == "api"
        assert len(args) == 2
        self.calls.append(args[1])
        if args[1] not in self.responses:
            raise _not_found()
        return self.responses[args[1]]


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr(github, "read_json", fake.read_json)
    return fake


def test_fetch_draft_via_rest(fake_gh: FakeGh) -> None:
    fake_gh.ref("vergil/ruling-draft/7/41", blob_text="fix R1.1\n")
    assert fetch_draft("o/r", 7, 41) == "fix R1.1\n"
    assert fake_gh.calls[0] == "repos/o/r/git/ref/vergil/ruling-draft/7/41"
    assert fetch_draft("o/r", 7, 99) is None


def test_fetch_draft_long_content(fake_gh: FakeGh) -> None:
    text = "".join(f"R1.{i}: fix it the plain way\n" for i in range(40))
    fake_gh.ref("vergil/ruling-draft/7/41", blob_text=text)
    assert fetch_draft("o/r", 7, 41) == text


def test_fetch_draft_other_errors_propagate(
    fake_gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    del fake_gh

    def fail(*_args: str) -> Any:
        raise github.GitHubAPIError(1, ("gh",), None, "gh: Server Error (HTTP 500)\n")

    monkeypatch.setattr(github, "read_json", fail)
    with pytest.raises(github.GitHubAPIError):
        fetch_draft("o/r", 7, 41)


def test_fetch_draft_refuses_invalid_utf8(fake_gh: FakeGh) -> None:
    fake_gh.ref("vergil/ruling-draft/7/41", blob=b"R1.1: \xff\n")
    with pytest.raises(RulingError, match="not UTF-8"):
        fetch_draft("o/r", 7, 41)


def test_fetch_draft_keeps_non_ascii_for_the_caller_to_refuse(fake_gh: FakeGh) -> None:
    # Decoding never converts: what was pushed is what comes back.
    fake_gh.ref("vergil/ruling-draft/7/41", blob_text="caf\u00e9\n")
    assert fetch_draft("o/r", 7, 41) == "caf\u00e9\n"


def _first(fake: FakeGh, prefix: str) -> dict[str, Any]:
    return next(v for k, v in fake.responses.items() if k.startswith(prefix))


@pytest.mark.parametrize(
    ("corrupt", "match"),
    [
        (lambda f: _first(f, "repos/o/r/git/ref/").update(ref="refs/vergil/x"), "different ref"),
        (lambda f: _first(f, "repos/o/r/git/ref/")["object"].update(type="tag"), "not point at"),
        (lambda f: _first(f, "repos/o/r/git/ref/")["object"].pop("sha"), "has no 'sha'"),
        (lambda f: _first(f, "repos/o/r/git/ref/").update(object=[]), "not a JSON object"),
        (lambda f: _first(f, "repos/o/r/git/trees/")["tree"].append({}), "exactly one file"),
        (lambda f: _first(f, "repos/o/r/git/trees/").update(truncated=True), "exactly one file"),
        (lambda f: _first(f, "repos/o/r/git/trees/")["tree"][0].update(path="x"), "single reply"),
        (lambda f: _first(f, "repos/o/r/git/trees/")["tree"][0].update(mode="100755"), "single"),
        (lambda f: _first(f, "repos/o/r/git/blobs/").update(encoding="utf-8"), "not base64"),
        (lambda f: _first(f, "repos/o/r/git/blobs/").update(content="!!!"), "does not decode"),
        (
            lambda f: _first(f, "repos/o/r/git/blobs/").update(
                content=base64.b64encode(b"R1.1: file\n").decode()
            ),
            "does not match its SHA",
        ),
    ],
)
def test_fetch_draft_refuses_a_malformed_draft(fake_gh: FakeGh, corrupt: Any, match: str) -> None:
    fake_gh.ref("vergil/ruling-draft/7/41", blob_text="R1.1: fix\n")
    corrupt(fake_gh)
    with pytest.raises(RulingError, match=match):
        fetch_draft("o/r", 7, 41)


def test_fetch_reads_what_push_wrote(
    git_repo_with_remote: Clone, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REST reader understands the commit the plumbing writer builds."""
    push_draft(7, 41, "R1.1: fix\nR1.S1: defer\n")
    remote = git_repo_with_remote.remote

    def read_json(*args: str) -> Any:
        path = args[1].removeprefix("repos/o/r/git/")
        kind, _, rest = path.partition("/")
        if kind == "ref":
            sha = _git("rev-parse", f"refs/{rest}", cwd=remote)
            return {"ref": f"refs/{rest}", "object": {"type": "commit", "sha": sha}}
        if kind == "commits":
            return {"tree": {"sha": _git("rev-parse", f"{rest}^{{tree}}", cwd=remote)}}
        if kind == "trees":
            mode, kind_, sha_name = _git("ls-tree", rest, cwd=remote).split(" ", 2)
            sha, name = sha_name.split("\t")
            entry = {"path": name, "type": kind_, "mode": mode, "sha": sha}
            return {"truncated": False, "tree": [entry]}
        raw = subprocess.run(  # noqa: S603
            ("git", "cat-file", "blob", rest),  # noqa: S607
            cwd=remote,
            check=True,
            capture_output=True,
        ).stdout
        return {"encoding": "base64", "content": base64.b64encode(raw).decode()}

    monkeypatch.setattr(github, "read_json", read_json)
    assert fetch_draft("o/r", 7, 41) == "R1.1: fix\nR1.S1: defer\n"


# --- delete_draft_api ------------------------------------------------------------


def test_delete_draft_api_deletes_the_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(github, "delete_if_exists", lambda endpoint: calls.append(endpoint) or True)
    delete_draft_api("o/r", 7, 41)
    assert calls == ["repos/o/r/git/refs/vergil/ruling-draft/7/41"]


def test_delete_draft_api_absent_ref_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(_endpoint: str) -> bool:
        raise github.GitHubAPIError(
            1,
            ("gh",),
            'HTTP/2.0 422 Unprocessable Entity\n\n{"message":"Reference does not exist"}',
            "gh: Reference does not exist (HTTP 422)\n",
        )

    monkeypatch.setattr(github, "delete_if_exists", gone)
    delete_draft_api("o/r", 7, 41)
    monkeypatch.setattr(github, "delete_if_exists", lambda _e: False)
    delete_draft_api("o/r", 7, 41)


def test_delete_draft_api_other_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(_endpoint: str) -> bool:
        raise github.GitHubAPIError(1, ("gh",), None, "gh: Validation Failed (HTTP 422)\n")

    monkeypatch.setattr(github, "delete_if_exists", fail)
    with pytest.raises(github.GitHubAPIError):
        delete_draft_api("o/r", 7, 41)


# --- the scope change draft --------------------------------------------------------


def test_scope_draft_ref_and_command() -> None:
    assert scope_draft_ref(7) == "refs/vergil/scope-draft/7"
    assert scope_command("o/r", 7) == "vrg-scope-change --issue 'o/r#7'"


def test_push_scope_draft_is_plumbing_only(git_repo_with_remote: Clone) -> None:
    push_scope_draft(7, "Also cover the docs.\n")
    assert git_repo_with_remote.head_unchanged()
    assert git_repo_with_remote.status_clean()
    ref = scope_draft_ref(7)
    assert git_repo_with_remote.remote_file(ref, "scope.md") == "Also cover the docs.\n"
    assert git_repo_with_remote.remote_tree(ref) == ["scope.md"]


def test_push_scope_draft_refuses_non_plain_text(git_repo_with_remote: Clone) -> None:
    with pytest.raises(RulingError, match="the draft scope change is not plain text"):
        push_scope_draft(7, "caf\u00e9\n")
    assert not _git("ls-remote", "origin", scope_draft_ref(7), cwd=git_repo_with_remote.path)


def test_require_remote_is_names_the_command(git_repo_with_remote: Clone) -> None:
    url = "https://github.com/o/other.git"
    _git("remote", "set-url", "origin", url, cwd=git_repo_with_remote.path)
    with pytest.raises(RulingError, match="run vrg-ruling draft-scope in a clone of o/r"):
        require_remote_is("o/r", command="vrg-ruling draft-scope")


def test_fetch_scope_draft_via_rest(fake_gh: FakeGh) -> None:
    fake_gh.ref("vergil/scope-draft/7", blob_text="Add x.\n")
    _first(fake_gh, "repos/o/r/git/trees/")["tree"][0].update(path="scope.md")
    assert fetch_scope_draft("o/r", 7) == "Add x.\n"
    assert fake_gh.calls[0] == "repos/o/r/git/ref/vergil/scope-draft/7"
    assert fetch_scope_draft("o/r", 8) is None


def test_fetch_scope_draft_refuses_a_reply_file(fake_gh: FakeGh) -> None:
    # A ref holding reply.md is not a scope change draft.
    fake_gh.ref("vergil/scope-draft/7", blob_text="Add x.\n")
    with pytest.raises(RulingError, match="does not hold a single scope.md file"):
        fetch_scope_draft("o/r", 7)


def test_delete_scope_draft_api_deletes_the_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(github, "delete_if_exists", lambda endpoint: calls.append(endpoint) or True)
    delete_scope_draft_api("o/r", 7)
    assert calls == ["repos/o/r/git/refs/vergil/scope-draft/7"]
