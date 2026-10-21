"""Tests for vergil_tooling.lib.conan_token (issue #2902).

Every token value here is an obvious placeholder; none resembles a real
ConanCenter credential.
"""

from __future__ import annotations

import shlex
import textwrap
from pathlib import Path

import pytest

from vergil_tooling.lib import conan_token
from vergil_tooling.lib.conan_token import (
    CONAN_AUDIT_TOKEN_ENV,
    ConanTokenError,
    ResolvedToken,
    resolve_conan_audit_token,
)

# Captured at import, before the conftest autouse fixture repoints them.
_REAL_CONAN_ENV_PATH = conan_token.conan_env_path
_REAL_IDENTITIES_PATH = conan_token.identities_path

_PLACEHOLDER = "placeholder-token"  # noqa: S105


def _identities(path: Path, *stanzas: str) -> Path:
    body = "\n".join(textwrap.dedent(s) for s in stanzas)
    path.write_text(body)
    return path


def _stanza(name: str, token_path: str | None) -> str:
    line = f'conan_audit_token_path = "{token_path}"\n' if token_path is not None else ""
    return f'[identities.{name}]\nvm_instance = "{name}-vm"\n{line}'


@pytest.fixture
def absent(tmp_path: Path) -> Path:
    return tmp_path / "absent"


def test_resolved_token_repr_redacts_value() -> None:
    text = repr(ResolvedToken("do-not-print-me", "environment"))
    assert "do-not-print-me" not in text
    assert "environment" in text


# -- default locations ---------------------------------------------------------


def test_conan_env_path_is_under_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _REAL_CONAN_ENV_PATH() == tmp_path / ".config" / "vergil" / "conan.env"


def test_identities_path_is_identity_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert _REAL_IDENTITIES_PATH() == tmp_path / ".config" / "vergil" / "identities.toml"


def test_defaults_use_module_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "conan.env"
    env_file.write_text(f"export {CONAN_AUDIT_TOKEN_ENV}={_PLACEHOLDER}\n")
    monkeypatch.setattr(conan_token, "conan_env_path", lambda: env_file)
    monkeypatch.setattr(conan_token, "identities_path", lambda: tmp_path / "absent.toml")
    resolved = resolve_conan_audit_token({})
    assert resolved == ResolvedToken(_PLACEHOLDER, str(env_file))


# -- precedence ----------------------------------------------------------------


def test_absent_everywhere_returns_none(absent: Path) -> None:
    assert (
        resolve_conan_audit_token(
            {}, conan_env=absent / "conan.env", identities=absent / "identities.toml"
        )
        is None
    )


def test_environment_wins_over_files(tmp_path: Path) -> None:
    env_file = tmp_path / "conan.env"
    env_file.write_text(f"export {CONAN_AUDIT_TOKEN_ENV}=from-file\n")
    resolved = resolve_conan_audit_token(
        {CONAN_AUDIT_TOKEN_ENV: "from-env"},
        conan_env=env_file,
        identities=tmp_path / "identities.toml",
    )
    assert resolved == ResolvedToken("from-env", "environment")


def test_environment_wins_even_over_malformed_file(tmp_path: Path) -> None:
    # An explicit env token never consults the files, so a broken file cannot
    # block an operator who set the variable deliberately.
    env_file = tmp_path / "conan.env"
    env_file.write_text("garbage\n")
    resolved = resolve_conan_audit_token(
        {CONAN_AUDIT_TOKEN_ENV: "from-env"},
        conan_env=env_file,
        identities=tmp_path / "identities.toml",
    )
    assert resolved is not None
    assert resolved.source == "environment"


def test_blank_environment_value_falls_through_to_file(tmp_path: Path) -> None:
    env_file = tmp_path / "conan.env"
    env_file.write_text(f"export {CONAN_AUDIT_TOKEN_ENV}=from-file\n")
    resolved = resolve_conan_audit_token(
        {CONAN_AUDIT_TOKEN_ENV: "   "},
        conan_env=env_file,
        identities=tmp_path / "identities.toml",
    )
    assert resolved == ResolvedToken("from-file", str(env_file))


def test_conan_env_wins_over_identities(tmp_path: Path) -> None:
    env_file = tmp_path / "conan.env"
    env_file.write_text(f"export {CONAN_AUDIT_TOKEN_ENV}=from-conan-env\n")
    token_file = tmp_path / "token"
    token_file.write_text("from-identity\n")
    ids = _identities(tmp_path / "identities.toml", _stanza("vergil-user", str(token_file)))
    resolved = resolve_conan_audit_token({}, conan_env=env_file, identities=ids)
    assert resolved == ResolvedToken("from-conan-env", str(env_file))


# -- conan.env parsing ---------------------------------------------------------


def test_conan_env_round_trips_shell_quoted_value(tmp_path: Path) -> None:
    # vm_guest writes the value with shlex.quote; parsing must undo exactly that.
    value = "tok en$with;special"
    env_file = tmp_path / "conan.env"
    env_file.write_text(f"export {CONAN_AUDIT_TOKEN_ENV}={shlex.quote(value)}\n")
    resolved = resolve_conan_audit_token({}, conan_env=env_file, identities=tmp_path / "x")
    assert resolved == ResolvedToken(value, str(env_file))


def test_conan_env_accepts_bare_assignment_comments_and_blanks(tmp_path: Path) -> None:
    env_file = tmp_path / "conan.env"
    env_file.write_text(f"# written by vrg-vm\n\n{CONAN_AUDIT_TOKEN_ENV}={_PLACEHOLDER}\n")
    resolved = resolve_conan_audit_token({}, conan_env=env_file, identities=tmp_path / "x")
    assert resolved == ResolvedToken(_PLACEHOLDER, str(env_file))


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("", "no CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER assignment"),
        ("# only a comment\n", "no CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER assignment"),
        ("export OTHER_VAR=x\n", "line 1 is not"),
        ("export\n", "line 1 is not"),
        ("echo hi there\n", "line 1 is not"),
        (f"export {CONAN_AUDIT_TOKEN_ENV}='unterminated\n", "line 1 is not valid shell"),
        (f"export {CONAN_AUDIT_TOKEN_ENV}=\n", "empty"),
        (f"export {CONAN_AUDIT_TOKEN_ENV}=''\n", "empty"),
        (
            f"export {CONAN_AUDIT_TOKEN_ENV}=a\nexport {CONAN_AUDIT_TOKEN_ENV}=b\n",
            "more than once",
        ),
    ],
)
def test_malformed_conan_env_fails_loudly(tmp_path: Path, content: str, reason: str) -> None:
    env_file = tmp_path / "conan.env"
    env_file.write_text(content)
    with pytest.raises(ConanTokenError) as exc:
        resolve_conan_audit_token({}, conan_env=env_file, identities=tmp_path / "x")
    message = str(exc.value)
    assert str(env_file) in message
    assert reason in message


def test_malformed_conan_env_error_never_echoes_content(tmp_path: Path) -> None:
    secret = "do-not-print-me"  # noqa: S105
    env_file = tmp_path / "conan.env"
    env_file.write_text(f"export {CONAN_AUDIT_TOKEN_ENV}={secret} extra\n")
    with pytest.raises(ConanTokenError) as exc:
        resolve_conan_audit_token({}, conan_env=env_file, identities=tmp_path / "x")
    assert secret not in str(exc.value)


def test_unreadable_conan_env_fails_loudly(tmp_path: Path) -> None:
    # A directory where the file should be: present but unreadable as a file.
    env_file = tmp_path / "conan.env"
    env_file.mkdir()
    with pytest.raises(ConanTokenError, match="cannot read"):
        resolve_conan_audit_token({}, conan_env=env_file, identities=tmp_path / "x")


# -- identities.toml conan_audit_token_path --------------------------------------


def test_identity_token_file_is_read(tmp_path: Path, absent: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(f"{_PLACEHOLDER}\n")
    ids = _identities(tmp_path / "identities.toml", _stanza("vergil-user", str(token_file)))
    resolved = resolve_conan_audit_token({}, conan_env=absent, identities=ids)
    assert resolved == ResolvedToken(_PLACEHOLDER, str(token_file))


def test_identity_token_path_expands_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, absent: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "token").write_text(f"{_PLACEHOLDER}\n")
    ids = _identities(tmp_path / "identities.toml", _stanza("vergil-user", "~/token"))
    resolved = resolve_conan_audit_token({}, conan_env=absent, identities=ids)
    assert resolved == ResolvedToken(_PLACEHOLDER, str(tmp_path / "token"))


def test_identities_sharing_one_token_file_resolve(tmp_path: Path, absent: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(f"{_PLACEHOLDER}\n")
    ids = _identities(
        tmp_path / "identities.toml",
        _stanza("vergil-user", str(token_file)),
        _stanza("vergil-audit", str(token_file)),
        _stanza("sandbox", None),
    )
    resolved = resolve_conan_audit_token({}, conan_env=absent, identities=ids)
    assert resolved == ResolvedToken(_PLACEHOLDER, str(token_file))


def test_identities_without_token_path_returns_none(tmp_path: Path, absent: Path) -> None:
    ids = _identities(tmp_path / "identities.toml", _stanza("vergil-user", None))
    assert resolve_conan_audit_token({}, conan_env=absent, identities=ids) is None


def test_identities_with_conflicting_token_paths_fail_loudly(tmp_path: Path, absent: Path) -> None:
    ids = _identities(
        tmp_path / "identities.toml",
        _stanza("vergil-user", str(tmp_path / "a")),
        _stanza("vergil-audit", str(tmp_path / "b")),
    )
    with pytest.raises(ConanTokenError, match="different conan_audit_token_path"):
        resolve_conan_audit_token({}, conan_env=absent, identities=ids)


def test_missing_identity_token_file_fails_loudly(tmp_path: Path, absent: Path) -> None:
    missing = tmp_path / "missing-token"
    ids = _identities(tmp_path / "identities.toml", _stanza("vergil-user", str(missing)))
    with pytest.raises(ConanTokenError) as exc:
        resolve_conan_audit_token({}, conan_env=absent, identities=ids)
    assert "cannot read" in str(exc.value)
    assert str(missing) in str(exc.value)


def test_empty_identity_token_file_fails_loudly(tmp_path: Path, absent: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("  \n")
    ids = _identities(tmp_path / "identities.toml", _stanza("vergil-user", str(token_file)))
    with pytest.raises(ConanTokenError, match="is empty"):
        resolve_conan_audit_token({}, conan_env=absent, identities=ids)
