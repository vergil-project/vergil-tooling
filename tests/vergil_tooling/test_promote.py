"""Tests for vergil_tooling.lib.promote."""

from __future__ import annotations

import itertools
import subprocess as _sp
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.promote import (
    PromoteError,
    Promotion,
    _peeled_commit,
    describe,
    plan_promotion,
    promote,
    promote_confirmed,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

_RUN = "vergil_tooling.lib.promote.subprocess.run"
_READ = "vergil_tooling.lib.promote.github.read_output"
_REPO = "vergil-project/vergil-claude-plugin"
OLD_REF = "t" * 40
OLD = "a" * 40
NEW = "b" * 40
NEW_TAG_OBJ = "r" * 40


@pytest.fixture(autouse=True)
def _no_installation_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``lib/git``'s token layering off the host's real App credentials."""
    monkeypatch.setattr(
        "vergil_tooling.lib.git.github.get_installation_token", lambda *_a, **_k: None
    )


def _ok(out: str = "", returncode: int = 0, stderr: str = "") -> _sp.CompletedProcess[str]:
    return _sp.CompletedProcess(args=[], returncode=returncode, stdout=out, stderr=stderr)


def _ls_lines(tag: str, raw: str | None, peeled: str | None, patterns: list[str]) -> str:
    """ls-remote output for *tag*: annotated when *peeled* differs from *raw*.

    Like real git, the ``^{}`` line is returned only when *patterns* asks
    for it by name: ``refs/tags/<tag>`` alone does not match it.
    """
    if not raw:
        return ""
    out = f"{raw}\trefs/tags/{tag}\n" if f"refs/tags/{tag}" in patterns else ""
    if peeled and peeled != raw and f"refs/tags/{tag}^{{}}" in patterns:
        out += f"{peeled}\trefs/tags/{tag}^{{}}\n"
    return out


def _git_responses(  # noqa: PLR0913
    *,
    rolling: str | None = OLD,
    rolling_raw: str | None = OLD_REF,
    release: str | None = NEW,
    release_raw: str | None = NEW_TAG_OBJ,
    on_main: int = 0,
    origin: str = f"https://github.com/{_REPO}.git",
    push_urls: list[str] | None = None,
    local_release: str | None = None,
    fetch_rc: int = 0,
    ls_rc: int = 0,
    fail: dict[str, _sp.CalledProcessError] | None = None,
    rolling_seq: list[tuple[str | None, str | None]] | None = None,
) -> Callable[..., _sp.CompletedProcess[str]]:
    """A ``subprocess.run`` side effect answering git by argv.

    It honours ``check=True`` the way ``subprocess.run`` does. *rolling_seq*,
    when given, is consumed one ``(raw, peeled)`` per ls-remote of the rolling
    tag, so a test can make origin move. *push_urls* defaults to *origin*, and
    *local_release* (the local release tag's commit) to *release*.
    """
    seq = iter(rolling_seq) if rolling_seq is not None else None
    pushes = push_urls if push_urls is not None else [origin]

    def answer(argv: list[str]) -> _sp.CompletedProcess[str]:  # noqa: PLR0911
        sub = argv[1]
        if fail and sub in fail:
            raise fail[sub]
        if argv[1:] == ["remote", "get-url", "--push", "--all", "origin"]:
            return _ok("".join(f"{u}\n" for u in pushes))
        if argv[1:] == ["remote", "get-url", "origin"]:
            return _ok(origin + "\n")
        if sub == "fetch":
            return _ok(returncode=fetch_rc, stderr="fatal: unable to access")
        if sub == "ls-remote":
            if ls_rc:
                return _ok(returncode=ls_rc, stderr="fatal: network")
            patterns = argv[3:]
            ref = patterns[0].removeprefix("refs/tags/")
            if ref.count(".") == 2:  # release tag vX.Y.Z
                return _ok(_ls_lines(ref, release_raw or release, release, patterns))
            if seq is not None:
                raw, peeled = next(seq)
                return _ok(_ls_lines(ref, raw, peeled, patterns))
            return _ok(_ls_lines(ref, rolling_raw or rolling, rolling, patterns))
        if sub == "merge-base":
            return _ok(returncode=on_main, stderr="fatal: bad object")
        if sub == "rev-parse":
            target = argv[-1]
            if target.endswith("^{commit}"):  # peel the resolved release object
                return _ok((local_release or release or "") + "\n")
            return _ok((release_raw or release or "") + "\n")  # the local release tag
        if sub in ("tag", "push"):
            return _ok()
        msg = f"unexpected git call: {argv}"
        raise AssertionError(msg)

    def run(argv: list[str] | tuple[str, ...], **kw: Any) -> _sp.CompletedProcess[str]:
        result = answer(list(argv))
        if kw.get("check") and result.returncode:
            raise _sp.CalledProcessError(
                result.returncode, list(argv), result.stdout, result.stderr
            )
        return result

    return run


def _argvs(mock_run: MagicMock) -> list[list[str]]:
    return [list(c.args[0]) for c in mock_run.call_args_list]


def _mutations(mock_run: MagicMock) -> list[list[str]]:
    return [a for a in _argvs(mock_run) if a[1] in ("tag", "push")]


@contextmanager
def _patched(
    *, release_view: Callable[..., str] | str = "false", **kw: Any
) -> Generator[tuple[MagicMock, MagicMock]]:
    read = (
        MagicMock(side_effect=release_view)
        if callable(release_view)
        else MagicMock(return_value=release_view)
    )
    with (
        patch(_RUN, side_effect=_git_responses(**kw)) as mock_run,
        patch(_READ, read),
    ):
        yield mock_run, read


# --- plan_promotion ---------------------------------------------------------

_MAIN = "refs/remotes/origin/main"
_PUSH_ARGV = [
    "git",
    "push",
    "origin",
    f"{NEW_TAG_OBJ}:refs/tags/v2.1",
    f"--force-with-lease=refs/tags/v2.1:{OLD_REF}",
]
_LOCAL_TAG_ARGV = ["git", "tag", "-f", "v2.1", NEW_TAG_OBJ]


def test_plan_promotion_reports_repo_and_commits() -> None:
    with _patched():
        p = plan_promotion("2.1.50")
    assert p == Promotion(_REPO, "v2.1", "v2.1.50", OLD_REF, OLD, NEW)


def test_plan_promotion_peels_annotated_tags_by_asking_for_the_peeled_ref() -> None:
    with _patched() as (mock_run, _):
        p = plan_promotion("2.1.50")
    assert p.new_commit == NEW  # the commit, not the tag object NEW_TAG_OBJ
    assert p.old_commit == OLD  # the commit, not the tag object OLD_REF
    ls = [a for a in _argvs(mock_run) if a[1] == "ls-remote"]
    assert ["git", "ls-remote", "origin", "refs/tags/v2.1.50", "refs/tags/v2.1.50^{}"] in ls
    assert ["git", "ls-remote", "origin", "refs/tags/v2.1", "refs/tags/v2.1^{}"] in ls


def test_plan_promotion_equal_commits_when_already_promoted() -> None:
    with _patched(rolling=NEW, rolling_raw=NEW):
        p = plan_promotion("2.1.50")
    assert p.old_commit == p.new_commit == NEW


def test_plan_promotion_fetches_main_explicitly_and_checks_qualified_ref() -> None:
    with _patched() as (mock_run, _):
        plan_promotion("2.1.50")
    argvs = _argvs(mock_run)
    assert ["git", "fetch", "--tags", "--force", "origin", f"+refs/heads/main:{_MAIN}"] in argvs
    assert ["git", "merge-base", "--is-ancestor", NEW, _MAIN] in argvs


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{_REPO}.git",
        f"https://github.com/{_REPO}",
        f"https://github.com/{_REPO}/",
        f"https://x-access-token:secret@github.com/{_REPO}.git",
        f"https://GitHub.com/{_REPO}.git",
        f"git@github.com:{_REPO}.git",
        f"org-123@github.com:{_REPO}.git",
        f"ssh://git@github.com/{_REPO}.git",
        f"ssh://git@github.com:22/{_REPO}.git",
    ],
)
def test_plan_promotion_normalizes_origin(url: str) -> None:
    with _patched(origin=url):
        assert plan_promotion("2.1.50").origin_repo == _REPO


def test_plan_promotion_refuses_non_github_origin() -> None:
    with (
        _patched(origin="https://example.com/x/y.git"),
        pytest.raises(PromoteError, match="not a GitHub repository URL"),
    ):
        plan_promotion("2.1.50")


_SECRET_URLS = [
    "https://x-access-token:s3cret@example.com/x/y.git",
    "https://user:pa/s3cret@github.com/o/r",
    "https://user:p@s3cret@github.com/o/r",
    "https://user:s3cret@github.com/o/r/extra",
]


@pytest.mark.parametrize("url", _SECRET_URLS)
def test_plan_promotion_fetch_url_refusal_never_echoes_credentials(url: str) -> None:
    with _patched(origin=url), pytest.raises(PromoteError, match="fetch URL") as excinfo:
        plan_promotion("2.1.50")
    assert "s3cret" not in str(excinfo.value)


@pytest.mark.parametrize("url", _SECRET_URLS)
def test_plan_promotion_push_url_refusal_never_echoes_credentials(url: str) -> None:
    with _patched(push_urls=[url]), pytest.raises(PromoteError, match="push URL") as excinfo:
        plan_promotion("2.1.50")
    assert "s3cret" not in str(excinfo.value)


def test_plan_promotion_refuses_push_url_to_another_repo() -> None:
    with (
        _patched(push_urls=["git@github.com:someone/else.git"]) as (mock_run, read),
        pytest.raises(PromoteError, match="push URL does not name the repo"),
    ):
        plan_promotion("2.1.50")
    assert not any(a[1] in ("fetch", "ls-remote") for a in _argvs(mock_run))
    read.assert_not_called()


def test_plan_promotion_refuses_a_second_push_url() -> None:
    urls = [f"https://github.com/{_REPO}.git", "https://github.com/someone/else.git"]
    with (
        _patched(push_urls=urls),
        pytest.raises(PromoteError, match="push URL does not name the repo"),
    ):
        plan_promotion("2.1.50")


def test_plan_promotion_accepts_push_url_in_another_form() -> None:
    with _patched(push_urls=[f"git@github.com:{_REPO}.git"]):
        assert plan_promotion("2.1.50").origin_repo == _REPO


def test_plan_promotion_refuses_tag_not_on_main() -> None:
    with _patched(on_main=1), pytest.raises(PromoteError, match="not on origin/main"):
        plan_promotion("2.1.50")


def test_plan_promotion_merge_base_error_raises() -> None:
    with _patched(on_main=128), pytest.raises(PromoteError, match="cannot check"):
        plan_promotion("2.1.50")


def test_plan_promotion_fetch_failure_raises() -> None:
    with _patched(fetch_rc=1), pytest.raises(PromoteError, match="git fetch"):
        plan_promotion("2.1.50")


def test_plan_promotion_ls_remote_failure_raises() -> None:
    with _patched(ls_rc=2), pytest.raises(PromoteError, match="ls-remote"):
        plan_promotion("2.1.50")


def _view_raising(stderr: str) -> Callable[..., str]:
    def view(*_a: str) -> str:
        raise _sp.CalledProcessError(1, ["gh", "release", "view"], "", stderr)

    return view


def test_plan_promotion_refuses_tag_without_published_release() -> None:
    with (
        _patched(release_view=_view_raising("release not found")) as (_, read),
        pytest.raises(PromoteError, match="no published GitHub Release"),
    ):
        plan_promotion("2.1.50")
    assert read.call_count == 1  # a typed version fails fast, no 404 retry


def test_plan_promotion_unreadable_release_is_not_reported_as_missing() -> None:
    with (
        _patched(release_view=_view_raising("HTTP 500: boom")),
        pytest.raises(PromoteError, match="cannot check the GitHub Release"),
    ):
        plan_promotion("2.1.50")


def test_plan_promotion_refuses_release_still_missing_after_retry_budget() -> None:
    clock = itertools.count(0, 30)
    with (
        _patched(release_view=_view_raising("release not found")) as (_, read),
        patch("vergil_tooling.lib.retry.time.sleep"),
        patch("vergil_tooling.lib.retry.time.monotonic", side_effect=lambda: next(clock)),
        pytest.raises(PromoteError, match="no published GitHub Release"),
    ):
        plan_promotion("2.1.50", release_just_published=True)
    assert read.call_count > 1


def test_plan_promotion_retries_a_transient_release_404_when_just_published() -> None:
    answers = iter(["404", "false"])

    def view(*_a: str) -> str:
        if next(answers) == "404":
            raise _sp.CalledProcessError(1, ["gh"], "", "HTTP 404: Not Found")
        return "false"

    with (
        _patched(release_view=view) as (_, read),
        patch("vergil_tooling.lib.retry.time.sleep"),
    ):
        plan_promotion("2.1.50", release_just_published=True)
    assert read.call_count == 2
    assert read.call_args.args[:3] == ("release", "view", "v2.1.50")


def test_plan_promotion_reads_the_release_on_github_com() -> None:
    with _patched() as (_, read):
        plan_promotion("2.1.50")
    assert read.call_args.args[3:5] == ("--repo", f"github.com/{_REPO}")


def test_plan_promotion_lets_a_missing_token_through_unwrapped() -> None:
    def view(*_a: str) -> str:
        raise github.MissingGitHubTokenError(4, ["gh"], "", "gh auth login")

    with (
        _patched(release_view=view),
        pytest.raises(github.MissingGitHubTokenError),
    ):
        plan_promotion("2.1.50")


def test_plan_promotion_refuses_draft_release() -> None:
    with (
        _patched(release_view="true"),
        pytest.raises(PromoteError, match="no published GitHub Release"),
    ):
        plan_promotion("2.1.50")


def test_plan_promotion_refuses_missing_release_tag() -> None:
    with (
        _patched(release=None, release_raw=None),
        pytest.raises(PromoteError, match=r"v2\.1\.99 does not exist on origin"),
    ):
        plan_promotion("2.1.99")


def test_plan_promotion_refuses_agent_signal() -> None:
    with (
        patch("vergil_tooling.lib.promote.identity_mode.any_agent_signal", return_value=True),
        _patched() as (mock_run, read),
        pytest.raises(PromoteError, match="human maintainer action"),
    ):
        plan_promotion("2.1.50")
    mock_run.assert_not_called()
    read.assert_not_called()


def test_plan_promotion_refuses_agent_identity() -> None:
    with (
        patch("vergil_tooling.lib.promote.identity_mode.is_agent", return_value=True),
        _patched() as (mock_run, _),
        pytest.raises(PromoteError, match="human maintainer action"),
    ):
        plan_promotion("2.1.50")
    mock_run.assert_not_called()


def test_plan_promotion_new_rolling_tag_is_empty() -> None:
    with _patched(rolling=None, rolling_raw=None):
        p = plan_promotion("2.2.0")
    assert (p.old_ref, p.old_commit) == ("", "")


# --- describe ---------------------------------------------------------------


def test_describe_existing_rolling_tag() -> None:
    p = Promotion("o/r", "v2.1", "v2.1.50", "t" * 40, "a" * 40, "b" * 40)
    assert describe(p) == "origin: o/r\nv2.1 aaaaaaaaaaaa -> v2.1.50 bbbbbbbbbbbb"


def test_describe_new_rolling_tag() -> None:
    p = Promotion("o/r", "v2.2", "v2.2.0", "", "", "b" * 40)
    assert describe(p) == "origin: o/r\nv2.2 (new) -> v2.2.0 bbbbbbbbbbbb"


# --- promote_confirmed ------------------------------------------------------


def test_promote_confirmed_refuses_changed_resolution() -> None:
    confirmed = Promotion(_REPO, "v2.1", "v2.1.50", OLD_REF, OLD, NEW)
    c = "c" * 40
    with (
        _patched(rolling=c, rolling_raw=c) as (mock_run, _),
        pytest.raises(PromoteError, match="changed since it was confirmed"),
    ):
        promote_confirmed(confirmed)
    assert _mutations(mock_run) == []


def test_promote_confirmed_refuses_a_moved_release_tag() -> None:
    confirmed = Promotion(_REPO, "v2.1", "v2.1.50", OLD_REF, OLD, "e" * 40)
    with (
        _patched() as (mock_run, _),
        pytest.raises(PromoteError, match="changed since it was confirmed") as excinfo,
    ):
        promote_confirmed(confirmed)
    assert "on origin was" not in str(excinfo.value)
    assert _mutations(mock_run) == []


def test_promote_confirmed_names_a_raw_only_change() -> None:
    confirmed = Promotion(_REPO, "v2.1", "v2.1.50", OLD_REF, OLD, NEW)
    retagged = "u" * 40
    with (
        _patched(rolling=OLD, rolling_raw=retagged) as (mock_run, _),
        pytest.raises(PromoteError, match=f"was {OLD_REF}, now {retagged}"),
    ):
        promote_confirmed(confirmed)
    assert _mutations(mock_run) == []


def test_promote_confirmed_refuses_a_local_release_tag_that_differs() -> None:
    with (
        _patched(local_release="d" * 40) as (mock_run, _),
        pytest.raises(PromoteError, match="not the confirmed commit"),
    ):
        promote_confirmed(plan_promotion("2.1.50"))
    assert _mutations(mock_run) == []
    argvs = _argvs(mock_run)
    assert ["git", "rev-parse", "--verify", "--end-of-options", "refs/tags/v2.1.50"] in argvs
    peel = ["git", "rev-parse", "--verify", "--end-of-options", f"{NEW_TAG_OBJ}^{{commit}}"]
    assert peel in argvs


def test_promote_confirmed_pushes_the_object_it_checked() -> None:
    with _patched() as (mock_run, _):
        promote_confirmed(plan_promotion("2.1.50"))
    argvs = _argvs(mock_run)
    peel = ["git", "rev-parse", "--verify", "--end-of-options", f"{NEW_TAG_OBJ}^{{commit}}"]
    assert argvs.index(peel) < argvs.index(_PUSH_ARGV)
    # neither the push nor the local tag update resolves the release tag's name again
    assert all("refs/tags/v2.1.50" not in " ".join(a) for a in _mutations(mock_run))


def test_promote_confirmed_leases_on_raw_annotated_tag_object() -> None:
    with _patched(rolling=OLD, rolling_raw=OLD_REF) as (mock_run, _):
        p = plan_promotion("2.1.50")
        assert (p.old_ref, p.old_commit) == (OLD_REF, OLD)
        promote_confirmed(p)
    assert _mutations(mock_run) == [_PUSH_ARGV, _LOCAL_TAG_ARGV]


def test_promote_confirmed_lightweight_tag_leases_on_commit() -> None:
    with _patched(rolling=OLD, rolling_raw=OLD) as (mock_run, _):
        p = plan_promotion("2.1.50")
        assert p.old_ref == p.old_commit == OLD
        promote_confirmed(p)
    assert _mutations(mock_run)[0][-1] == f"--force-with-lease=refs/tags/v2.1:{OLD}"


def test_promote_confirmed_new_rolling_tag_uses_empty_lease() -> None:
    with _patched(rolling=None, rolling_raw=None) as (mock_run, _):
        promote_confirmed(plan_promotion("2.2.0"))
    assert _mutations(mock_run)[0] == [
        "git",
        "push",
        "origin",
        f"{NEW_TAG_OBJ}:refs/tags/v2.2",
        "--force-with-lease=refs/tags/v2.2:",
    ]


def test_promote_confirmed_never_plain_force() -> None:
    with _patched() as (mock_run, _):
        promote_confirmed(plan_promotion("2.1.50"))
    pushes = [a for a in _argvs(mock_run) if a[1] == "push"]
    assert pushes == [_PUSH_ARGV]
    for arg in pushes[0][2:]:
        assert arg not in ("--force", "-f")
        assert not arg.startswith("+")


# --- promote ----------------------------------------------------------------


def test_promote_runs_push_then_local_tag() -> None:
    with _patched() as (mock_run, _):
        promote("2.1.50")
    assert _mutations(mock_run) == [_PUSH_ARGV, _LOCAL_TAG_ARGV]
    argvs = _argvs(mock_run)
    assert argvs.index(_PUSH_ARGV) > argvs.index(["git", "merge-base", "--is-ancestor", NEW, _MAIN])


def test_promote_retries_the_release_read() -> None:
    with (
        _patched(release_view=_view_raising("HTTP 404: Not Found")) as (_, read),
        patch("vergil_tooling.lib.promote.retry.retry_known_resource") as m_retry,
    ):
        m_retry.return_value = "false"
        promote("2.1.50")
    assert m_retry.call_count == 2  # the plan and the re-check before the push
    read.assert_not_called()  # the patched retry never called the read


def test_promote_refuses_when_origin_moved_between_plan_and_push() -> None:
    c = "c" * 40
    with (
        _patched(rolling_seq=[(OLD_REF, OLD), (c, c)]) as (mock_run, _),
        pytest.raises(PromoteError, match="changed since it was confirmed"),
    ):
        promote("2.1.50")
    assert _mutations(mock_run) == []


def test_promote_dry_run_does_not_execute(capsys: pytest.CaptureFixture[str]) -> None:
    with _patched() as (mock_run, _):
        promote("2.1.50", dry_run=True)
    assert _mutations(mock_run) == []
    assert capsys.readouterr().out == f"origin: {_REPO}\nv2.1 {OLD[:12]} -> v2.1.50 {NEW[:12]}\n"


def test_promote_dry_run_applies_target_check() -> None:
    with _patched(on_main=1), pytest.raises(PromoteError, match="not on origin/main"):
        promote("2.1.50", dry_run=True)


def test_promote_strips_v_prefix() -> None:
    with _patched() as (mock_run, _):
        promote("v2.1.50")
    assert _mutations(mock_run)[0] == _PUSH_ARGV


def _git_err(stderr: str) -> _sp.CalledProcessError:
    err = _sp.CalledProcessError(1, ["git"])
    err.stderr = stderr
    err.stdout = ""
    return err


def test_promote_local_tag_failure_after_push_says_origin_moved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        _patched(fail={"tag": _git_err("fatal: cannot lock ref\n")}) as (mock_run, _),
        pytest.raises(PromoteError, match="moved to v2.1.50 on origin"),
    ):
        promote("2.1.50")
    assert _mutations(mock_run)[0] == _PUSH_ARGV
    assert "cannot lock ref" in capsys.readouterr().err


def test_promote_refused_push_names_the_lease(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        _patched(fail={"push": _git_err("! [rejected] v2.1 (stale info)\n")}) as (mock_run, _),
        pytest.raises(PromoteError, match=f"required origin's v2.1 to be {OLD_REF}") as excinfo,
    ):
        promote("2.1.50")
    assert "stale info" not in str(excinfo.value)  # git's stderr is echoed once, not repeated
    assert capsys.readouterr().err.count("stale info") == 1
    assert not any(a[1] == "tag" for a in _argvs(mock_run))  # local tag untouched
    # vrg-release's tracking issue records the command and git's output
    assert excinfo.value.cmd == ["git"]
    assert excinfo.value.stderr == "! [rejected] v2.1 (stale info)\n"


def test_promote_git_failure_carries_command_and_output() -> None:
    with _patched(fetch_rc=1), pytest.raises(PromoteError) as excinfo:
        promote("2.1.50")
    assert excinfo.value.cmd[:2] == ["git", "fetch"]
    assert excinfo.value.stderr == "fatal: unable to access"


def test_promote_refusal_carries_no_command() -> None:
    with _patched(on_main=1), pytest.raises(PromoteError) as excinfo:
        promote("2.1.50")
    assert getattr(excinfo.value, "cmd", "fallback") == "fallback"


def test_promote_refused_push_of_new_tag_names_absent_lease() -> None:
    with (
        _patched(rolling=None, rolling_raw=None, fail={"push": _git_err("")}),
        pytest.raises(PromoteError, match="required origin's v2.2 to be absent"),
    ):
        promote("2.2.0")


def test_promote_push_failure_no_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    with _patched(fail={"push": _git_err("")}), pytest.raises(PromoteError):
        promote("2.1.50")
    assert capsys.readouterr().err == ""


def test_promote_invalid_version_raises() -> None:
    with pytest.raises(PromoteError, match="not valid"):
        promote("invalid")


def test_promote_error_is_a_value_error() -> None:
    assert issubclass(PromoteError, ValueError)


def test_promote_skips_when_already_promoted(capsys: pytest.CaptureFixture[str]) -> None:
    with _patched(rolling=NEW, rolling_raw=NEW) as (mock_run, _):
        promote("2.1.50")
    assert _mutations(mock_run) == []
    assert "already promoted" in capsys.readouterr().out


def test_promote_already_promoted_pushes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(_RUN, side_effect=_git_responses(rolling=NEW, release=NEW)) as mock_run,
        patch(_READ, return_value="false"),
    ):
        promote("2.1.50")
    argvs = _argvs(mock_run)
    assert not any(a[:2] in (["git", "tag"], ["git", "push"]) for a in argvs)
    assert "already promoted" in capsys.readouterr().out


# --- _peeled_commit ---------------------------------------------------------


def test_peeled_commit_lightweight_tag() -> None:
    with patch(_RUN, return_value=_ok("abc123\trefs/tags/v2.1\n")):
        assert _peeled_commit("v2.1") == "abc123"


def test_peeled_commit_annotated_tag_peels_to_commit() -> None:
    out = "tagobj\trefs/tags/v2.1.0\ncommit99\trefs/tags/v2.1.0^{}\n"
    with patch(_RUN, return_value=_ok(out)):
        assert _peeled_commit("v2.1.0") == "commit99"


def test_peeled_commit_absent_tag_returns_empty() -> None:
    with patch(_RUN, return_value=_ok("")):
        assert _peeled_commit("v9.9") == ""


def test_peeled_commit_git_failure_raises() -> None:
    with (
        patch(_RUN, side_effect=_git_responses(ls_rc=1)),
        pytest.raises(PromoteError, match="ls-remote"),
    ):
        _peeled_commit("v2.1")


def test_peeled_commit_ignores_refs_that_only_end_like_the_tag() -> None:
    out = "other1\trefs/heads/refs/tags/v2.1\nabc123\trefs/tags/v2.1\n"
    with patch(_RUN, return_value=_ok(out)):
        assert _peeled_commit("v2.1") == "abc123"
