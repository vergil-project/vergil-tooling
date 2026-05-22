# Single-Account Identity: vergil-tooling Changes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement GitHub App token exchange in vergil-tooling,
remove the multi-account credential selection code, and update
git/gh wrappers so the agent authenticates via App installation
tokens from inside the identity VM.

**Architecture:** A `get_installation_token()` function in
`github.py` reads App credentials from the VM filesystem, signs
a JWT using `openssl` (no new Python dependencies), and exchanges
it for a 1-hour GitHub installation token. `vrg-gh` and `vrg-git`
use this token for GitHub operations. Co-author identity shifts
from account discovery to an environment variable set by the AI
harness.

**Tech Stack:** Python (vergil-tooling), openssl CLI (JWT signing)

**Specs:**
- `docs/specs/2026-05-20-single-account-identity-design.md` (#933)
  — Credential Strategy, Tooling Changes
- `docs/specs/2026-05-20-identity-vm-isolation-design.md` (#892)
  — Credential Provisioning

**Relationship to Plan 5:** This plan supersedes Tasks 3-4 of the
vergil-tooling VM adaptations plan (`docs/plans/in-progress/2026-05-20-p5-vergil-tooling-vm-adaptations.md`).

> **Update 2026-05-22:** `installation_id` has been removed from
> `identities.toml` and the `Identity` dataclass (#1004).
> Installation IDs are now resolved dynamically at runtime by the
> wrapper scripts via `GET /app/installations`. The embedded code
> snippets below still reference the static `installation_id` —
> they reflect the design at the time of writing. The live code
> in `github.py` will be updated to use dynamic resolution in
> Plan 5.
Plan 5 Tasks 1-2 (nerdctl runtime detection) remain
independent.

**Repository:** vergil-tooling

**Depends on:** Spec #933 (approved). VM credential provisioning
(Plan 3) defines the file layout this code reads from.

---

## Design

### Zero New Dependencies

The existing `pyproject.toml` has `dependencies = []`. This plan
preserves that by using `openssl` (available on macOS and Linux)
for RS256 JWT signing rather than adding PyJWT. The `openssl dgst
-sha256 -sign` command performs PKCS#1 v1.5 RSA signing, which is
the RS256 algorithm GitHub requires.

### Token Exchange Flow

```text
_load_app_config()
  reads ~/.config/vergil/app.env  →  APP_ID, INSTALLATION_ID
  reads ~/.config/vergil/app.pem  →  private key path
  env var overrides: VRG_APP_ID, VRG_INSTALLATION_ID, VRG_PRIVATE_KEY_PATH

_generate_jwt(app_id, key_path)
  header + payload  →  openssl dgst -sha256 -sign  →  JWT string

get_installation_token()
  _load_app_config() → _generate_jwt() →
  gh api /app/installations/{id}/access_tokens  →  installation token
  cached for 55 minutes (tokens expire after 60)
```

When App credentials are not present (host-side development),
`get_installation_token()` returns `None` and all wrappers fall
back to ambient `gh` auth — the human's credentials from
`gh auth login`. No behavior change for host users.

### Co-Author Model

Co-author identity shifts from GitHub account discovery to an
environment variable `VRG_CO_AUTHOR`. The AI harness sets this
(e.g., `VRG_CO_AUTHOR="Claude Opus 4.6 <noreply@anthropic.com>"`).
If unset, no co-author trailer is added — correct for human-only
commits. The deprecated `--agent` flag is removed.

### What Gets Deleted

| Function | File | Why |
|---|---|---|
| `_discover_accounts()` | `github.py:24` | Multi-account model retired |
| `resolve_co_author_trailer()` | `github.py:47` | Replaced by `VRG_CO_AUTHOR` env var |
| `_human_token()` | `github.py:71` | Replaced by `get_installation_token()` |
| `_get_token()` | `vrg_gh.py:46` | Account discovery removed |
| `_ESCALATED_COMMANDS` | `vrg_gh.py:40` | No credential escalation in single-identity model |

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/lib/github.py` | Modify | Add `_load_app_config`, `_generate_jwt`, `get_installation_token`; delete `_discover_accounts`, `resolve_co_author_trailer`, `_human_token`; rewrite `_gh_env` |
| `src/vergil_tooling/bin/vrg_gh.py` | Modify | Delete `_get_token`, `_ESCALATED_COMMANDS`, `_discover_accounts` import; use `get_installation_token` |
| `src/vergil_tooling/bin/vrg_git.py` | Modify | Add HTTPS token injection for remote operations |
| `src/vergil_tooling/bin/vrg_commit.py` | Modify | Replace `resolve_co_author_trailer()` with `VRG_CO_AUTHOR` env var; remove `--agent` flag |
| `tests/vergil_tooling/test_github.py` | Modify | Add token exchange tests; update credential injection tests; delete account discovery tests |
| `tests/vergil_tooling/test_vrg_gh.py` | Modify | Remove `_get_token`/`_discover_accounts` tests; update GH_TOKEN injection tests |
| `tests/vergil_tooling/test_vrg_git.py` | Modify | Add remote operation token injection tests |
| `tests/vergil_tooling/test_vrg_commit.py` | Modify | Update co-author tests for env var model |

---

### Task 1: App Token Exchange

Add three functions to `github.py`: config loading, JWT
generation, and installation token exchange. These are additive
— no existing code is deleted yet.

**Files:**
- Modify: `src/vergil_tooling/lib/github.py`
- Modify: `tests/vergil_tooling/test_github.py`

- [ ] **Step 1: Write failing tests for `_load_app_config`**

Add to `tests/vergil_tooling/test_github.py`:

```python
class TestLoadAppConfig:
    def test_returns_none_when_no_config_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        assert github._load_app_config() is None

    def test_returns_config_from_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\nINSTALLATION_ID=67890\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        result = github._load_app_config()
        assert result is not None
        app_id, installation_id, key_path = result
        assert app_id == "12345"
        assert installation_id == "67890"
        assert key_path == config_dir / "app.pem"

    def test_returns_none_when_missing_installation_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        assert github._load_app_config() is None

    def test_env_vars_override_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        key_file = tmp_path / "override.pem"
        key_file.write_text("override-key\n")
        monkeypatch.setenv("VRG_APP_ID", "99999")
        monkeypatch.setenv("VRG_INSTALLATION_ID", "88888")
        monkeypatch.setenv("VRG_PRIVATE_KEY_PATH", str(key_file))
        result = github._load_app_config()
        assert result is not None
        app_id, installation_id, key_path = result
        assert app_id == "99999"
        assert installation_id == "88888"
        assert key_path == key_file

    def test_ignores_comments_in_env_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text(
            "# GitHub App credentials\nAPP_ID=12345\nINSTALLATION_ID=67890\n"
        )
        (config_dir / "app.pem").write_text("fake-key\n")
        result = github._load_app_config()
        assert result is not None
        assert result[0] == "12345"
```

Add the `Path` import at the top of the test file:

```python
from pathlib import Path
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestLoadAppConfig -v`
Expected: FAIL — `_load_app_config` does not exist

- [ ] **Step 3: Implement `_load_app_config`**

Add to `src/vergil_tooling/lib/github.py`, after the existing
imports:

```python
from pathlib import Path

_cached_token: tuple[str, float] | None = None


def _load_app_config() -> tuple[str, str, Path] | None:
    """Read GitHub App credentials from env vars or ~/.config/vergil/.

    Returns ``(app_id, installation_id, key_path)`` or ``None``
    when App mode is not configured.
    """
    app_id = os.environ.get("VRG_APP_ID", "")
    installation_id = os.environ.get("VRG_INSTALLATION_ID", "")
    key_path_str = os.environ.get("VRG_PRIVATE_KEY_PATH", "")

    if app_id and installation_id and key_path_str:
        return app_id, installation_id, Path(key_path_str).expanduser()

    env_file = Path.home() / ".config" / "vergil" / "app.env"
    key_file = Path.home() / ".config" / "vergil" / "app.pem"
    if not env_file.exists() or not key_file.exists():
        return None

    values: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            k, v = stripped.split("=", 1)
            values[k.strip()] = v.strip()

    app_id = app_id or values.get("APP_ID", "")
    installation_id = installation_id or values.get("INSTALLATION_ID", "")

    if not app_id or not installation_id:
        return None

    return app_id, installation_id, key_file
```

- [ ] **Step 4: Run `_load_app_config` tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestLoadAppConfig -v`
Expected: PASS

- [ ] **Step 5: Write failing tests for `_generate_jwt`**

Add to `tests/vergil_tooling/test_github.py`:

```python
class TestGenerateJwt:
    def test_produces_three_part_token(self, tmp_path: Path) -> None:
        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            jwt_str = github._generate_jwt("12345", key_path)
        parts = jwt_str.split(".")
        assert len(parts) == 3

    def test_calls_openssl_with_key_path(self, tmp_path: Path) -> None:
        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            github._generate_jwt("12345", key_path)
        args = mock_run.call_args[0][0]
        assert args[0] == "openssl"
        assert str(key_path) in args

    def test_header_contains_rs256(self, tmp_path: Path) -> None:
        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            jwt_str = github._generate_jwt("12345", key_path)
        import base64
        header_b64 = jwt_str.split(".")[0]
        padding = 4 - len(header_b64) % 4
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "=" * padding))
        assert header == {"alg": "RS256", "typ": "JWT"}

    def test_payload_contains_app_id_as_iss(self, tmp_path: Path) -> None:
        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            jwt_str = github._generate_jwt("12345", key_path)
        import base64
        payload_b64 = jwt_str.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        assert payload["iss"] == 12345
        assert "iat" in payload
        assert "exp" in payload
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestGenerateJwt -v`
Expected: FAIL — `_generate_jwt` does not exist

- [ ] **Step 7: Implement `_generate_jwt`**

Add to `src/vergil_tooling/lib/github.py`, after `_load_app_config`:

```python
def _generate_jwt(app_id: str, key_path: Path) -> str:
    """Generate an RS256 JWT for GitHub App authentication.

    Uses ``openssl`` for RSA signing to avoid adding a
    cryptography dependency.
    """
    import base64 as _b64

    def b64url(data: bytes) -> str:
        return _b64.urlsafe_b64encode(data).rstrip(b"=").decode()

    now = int(time.time())
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(
        json.dumps({"iat": now - 60, "exp": now + 600, "iss": int(app_id)}).encode()
    )

    signing_input = f"{header}.{payload}"

    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(key_path),
            "-binary",
        ],
        input=signing_input.encode(),
        capture_output=True,
        check=True,
    )

    signature = b64url(result.stdout)
    return f"{header}.{payload}.{signature}"
```

- [ ] **Step 8: Run `_generate_jwt` tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestGenerateJwt -v`
Expected: PASS

- [ ] **Step 9: Write failing tests for `get_installation_token`**

Add to `tests/vergil_tooling/test_github.py`:

```python
class TestGetInstallationToken:
    def test_returns_none_when_no_app_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        github._cached_token = None
        assert github.get_installation_token() is None

    def test_exchanges_jwt_for_installation_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\nINSTALLATION_ID=67890\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        github._cached_token = None
        with (
            patch(
                "vergil_tooling.lib.github._generate_jwt",
                return_value="fake-jwt",
            ),
            patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="ghs_install_token_abc\n")
            token = github.get_installation_token()
        assert token == "ghs_install_token_abc"
        call_args = mock_run.call_args[0][0]
        assert "/app/installations/67890/access_tokens" in " ".join(call_args)

    def test_caches_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\nINSTALLATION_ID=67890\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        github._cached_token = None
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="jwt"),
            patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="ghs_token\n")
            first = github.get_installation_token()
            second = github.get_installation_token()
        assert first == second == "ghs_token"
        assert mock_run.call_count == 1

    def test_refreshes_expired_cache(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_INSTALLATION_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\nINSTALLATION_ID=67890\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        github._cached_token = ("old_token", 0.0)
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="jwt"),
            patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="new_token\n")
            token = github.get_installation_token()
        assert token == "new_token"
```

- [ ] **Step 10: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::TestGetInstallationToken -v`
Expected: FAIL — `get_installation_token` does not exist

- [ ] **Step 11: Implement `get_installation_token`**

Add to `src/vergil_tooling/lib/github.py`, after `_generate_jwt`:

```python
def get_installation_token() -> str | None:
    """Return a GitHub App installation token, or ``None`` if not in App mode.

    Reads App credentials from ``~/.config/vergil/`` (or env var
    overrides), generates a JWT, and exchanges it for a 1-hour
    installation token via the GitHub API. Tokens are cached for
    55 minutes.
    """
    global _cached_token  # noqa: PLW0603
    if _cached_token is not None:
        token, expiry = _cached_token
        if time.time() < expiry:
            return token

    config = _load_app_config()
    if config is None:
        return None

    app_id, installation_id, key_path = config
    jwt_token = _generate_jwt(app_id, key_path)

    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gh",
            "api",
            f"/app/installations/{installation_id}/access_tokens",
            "-X",
            "POST",
            "--jq",
            ".token",
        ],
        env={**os.environ, "GH_TOKEN": jwt_token},
        capture_output=True,
        text=True,
        check=True,
    )

    token = result.stdout.strip()
    _cached_token = (token, time.time() + 3300)
    return token
```

- [ ] **Step 12: Run all token exchange tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py -k "LoadAppConfig or GenerateJwt or GetInstallationToken" -v`
Expected: PASS

- [ ] **Step 13: Update autouse fixture to clear token cache**

In `tests/vergil_tooling/test_github.py`, update the
`_no_credential_injection` fixture:

```python
@pytest.fixture(autouse=True)
def _no_credential_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)
    github._human_token.cache_clear()
    github._cached_token = None
```

- [ ] **Step 14: Run full github.py test suite**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py -v`
Expected: PASS (existing tests unaffected)

- [ ] **Step 15: Commit**

```bash
vrg-commit --type feat --scope github \
  --message "GitHub App installation token exchange" \
  --body "Add _load_app_config, _generate_jwt, and get_installation_token. Uses openssl for RS256 JWT signing — no new dependencies."
```

---

### Task 2: Simplify vrg-gh

Remove credential selection code from `vrg-gh`. Replace
`_get_token()` with `get_installation_token()` from `github.py`.
The wrapper becomes pure workflow enforcement.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_gh.py`
- Modify: `tests/vergil_tooling/test_vrg_gh.py`

- [ ] **Step 1: Write tests for simplified behavior**

Replace the credential-related tests in
`tests/vergil_tooling/test_vrg_gh.py`. Remove everything below
the `# -- credential selection` comments. Replace with:

```python
# -- token injection --------------------------------------------------------


def test_injects_app_token_when_available() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value="ghs_app_token",
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs["env"]["GH_TOKEN"] == "ghs_app_token"  # noqa: S105


def test_no_env_injection_when_no_app_token() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert "env" not in kwargs or kwargs.get("env") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py -k "app_token" -v`
Expected: FAIL

- [ ] **Step 3: Update existing tests to remove `_get_token` mocking**

In `tests/vergil_tooling/test_vrg_gh.py`, update the allowed-pair
test to use the new token injection:

```python
@pytest.mark.parametrize(("top", "sub"), _ALLOWED_PAIRS)
def test_allowed_pair_passes(top: str, sub: str) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        rc = main([top, sub])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[0] == "gh"
    assert args[1] == top
    assert args[2] == sub
```

Update the `pr review` tests similarly (replace `_get_token`
mocking with `github.get_installation_token` mocking).

Update the `pr merge` allowed test:

```python
def test_pr_merge_allowed_with_valid_context() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        rc = main(["pr", "merge", "42"])
    assert rc == 0
```

Update the subprocess passthrough tests:

```python
def test_subprocess_uses_shell_false() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        main(["issue", "list"])
    _, kwargs = mock_run.call_args
    assert kwargs.get("shell") is not True


def test_returns_subprocess_exit_code() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_gh.github.get_installation_token",
            return_value=None,
        ),
        patch("vergil_tooling.bin.vrg_gh.subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 128
        rc = main(["issue", "list"])
    assert rc == 128
```

Delete these tests entirely (they test the removed credential
selection code):

- `test_discover_accounts`
- `test_discover_accounts_deduplicates`
- `test_discover_accounts_ignores_other_accounts`
- `test_discover_accounts_missing_vergil`
- `test_default_uses_human_token_workaround`
- `test_issue_close_escalates_to_human`
- `test_pr_merge_release_branch_escalates`
- `test_gh_token_injected_into_env`

- [ ] **Step 4: Rewrite `vrg_gh.py`**

Replace the full contents of `src/vergil_tooling/bin/vrg_gh.py`:

```python
"""Safe gh wrapper for AI agent sessions.

Enforces a two-level subcommand allowlist and flag deny lists.
Injects GitHub App installation tokens when available.
"""

from __future__ import annotations

import os
import subprocess
import sys

from vergil_tooling.lib import github

_ALLOWED: dict[str, set[str]] = {
    "issue": {"view", "create", "close", "edit", "list", "comment"},
    "pr": {"view", "checks", "list", "diff", "comment", "edit", "review", "merge"},
    "run": {"list", "view", "watch"},
    "repo": {"view"},
    "label": {"list", "create"},
}

_DENIED_PAIRS: dict[str, dict[str, str]] = {
    "pr": {
        "create": "Use vrg-submit-pr instead of gh pr create.",
        "close": "gh pr close is denied by vrg-gh.",
    },
    "repo": {
        "edit": "gh repo edit is denied by vrg-gh.",
        "create": "gh repo create is denied by vrg-gh.",
        "delete": "gh repo delete is denied by vrg-gh.",
    },
}

_DENIED_TOP: dict[str, str] = {
    "api": "gh api is denied by vrg-gh.",
    "auth": "gh auth is denied by vrg-gh.",
}


def _validate_merge_context(argv: list[str]) -> str | None:
    if len(argv) < 3:  # noqa: PLR2004
        return "pr merge requires a PR number or URL."
    return None


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        print("usage: vrg-gh <subcommand> <action> [args...]", file=sys.stderr)
        return 2

    top = argv[0]

    if top in _DENIED_TOP:
        msg = _DENIED_TOP[top]
        print(f"vrg-gh: {top} is denied. {msg}", file=sys.stderr)
        return 1

    if top not in _ALLOWED:
        print(
            f"vrg-gh: {top} is not recognized. Allowed: {', '.join(sorted(_ALLOWED))}",
            file=sys.stderr,
        )
        return 1

    if len(argv) < 2:  # noqa: PLR2004
        print(
            f"vrg-gh: {top} requires a subcommand. Allowed: {', '.join(sorted(_ALLOWED[top]))}",
            file=sys.stderr,
        )
        return 1

    sub = argv[1]

    if top in _DENIED_PAIRS and sub in _DENIED_PAIRS[top]:
        msg = _DENIED_PAIRS[top][sub]
        print(f"vrg-gh: {top} {sub} is denied. {msg}", file=sys.stderr)
        return 1

    if sub not in _ALLOWED[top]:
        print(
            f"vrg-gh: {top} {sub} is not recognized. Allowed: {', '.join(sorted(_ALLOWED[top]))}",
            file=sys.stderr,
        )
        return 1

    if top == "pr" and sub == "review" and "--approve" in argv:
        print(
            "vrg-gh: pr review --approve is denied. Agents cannot approve PRs.",
            file=sys.stderr,
        )
        return 1

    if top == "pr" and sub == "merge":
        err = _validate_merge_context(argv)
        if err:
            print(f"vrg-gh: pr merge is denied. {err}", file=sys.stderr)
            return 1

    token = github.get_installation_token()
    env: dict[str, str] | None = None
    if token is not None:
        env = {**os.environ, "GH_TOKEN": token}
    result = subprocess.run(  # noqa: S603
        ["gh", *argv],  # noqa: S607
        env=env,
        check=False,
    )
    return result.returncode
```

- [ ] **Step 5: Run full vrg-gh test suite**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
vrg-commit --type refactor --scope gh \
  --message "remove credential selection from vrg-gh" \
  --body "Replace _get_token/account discovery with github.get_installation_token. Wrapper is now pure workflow enforcement with optional App token injection."
```

---

### Task 3: Co-Author Resolution in vrg-commit

Replace `resolve_co_author_trailer()` (which depends on account
discovery) with the `VRG_CO_AUTHOR` environment variable. Remove
the deprecated `--agent` flag.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_commit.py`
- Modify: `tests/vergil_tooling/test_vrg_commit.py`

- [ ] **Step 1: Write tests for env-var co-author**

Replace the co-author tests in
`tests/vergil_tooling/test_vrg_commit.py`. First, update the
`_commit_environment` fixture to remove the
`resolve_co_author_trailer` mock and add `VRG_CO_AUTHOR`:

```python
@contextlib.contextmanager
def _commit_environment(
    tmp_path: Path,
    *,
    branch: str = "feature/42-test",
    is_main_worktree: bool = False,
    branching_model: str = "library-release",
    has_staged: bool = True,
    write_config: bool = True,
    co_author: str = "test-agent <test-agent@test.com>",
) -> Iterator[None]:
    if write_config:
        (tmp_path / "vergil.toml").write_text(
            _TEST_TOML_TEMPLATE.format(branching_model=branching_model)
        )

    env_patches = {}
    if co_author:
        env_patches["VRG_CO_AUTHOR"] = co_author

    with (
        patch("vergil_tooling.bin.vrg_commit.git.current_branch", return_value=branch),
        patch("vergil_tooling.bin.vrg_commit.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_commit.git.is_main_worktree",
            return_value=is_main_worktree,
        ),
        patch(
            "vergil_tooling.bin.vrg_commit.git.has_staged_changes",
            return_value=has_staged,
        ),
        patch("vergil_tooling.bin.vrg_commit.git.run"),
        patch.dict(os.environ, env_patches, clear=False),
    ):
        yield
```

Add `import os` to the test file's imports.

Then add new tests:

```python
def test_co_author_from_env_var(tmp_path: Path) -> None:
    commit_file_content = ""

    def capture_run(*args: str) -> None:
        nonlocal commit_file_content
        if args[0] == "commit" and args[1] == "--file":
            commit_file_content = Path(args[2]).read_text(encoding="utf-8")

    with (
        _commit_environment(
            tmp_path,
            co_author="Claude Opus 4.6 <noreply@anthropic.com>",
        ),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 0
    assert (
        "Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
        in commit_file_content
    )


def test_no_co_author_when_env_var_unset(tmp_path: Path) -> None:
    commit_file_content = ""

    def capture_run(*args: str) -> None:
        nonlocal commit_file_content
        if args[0] == "commit" and args[1] == "--file":
            commit_file_content = Path(args[2]).read_text(encoding="utf-8")

    with (
        _commit_environment(tmp_path, co_author=""),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 0
    assert "Co-Authored-By" not in commit_file_content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_commit.py -k "co_author" -v`
Expected: FAIL

- [ ] **Step 3: Rewrite co-author resolution in vrg-commit**

In `src/vergil_tooling/bin/vrg_commit.py`:

Remove the `github` import from the top. Change:

```python
from vergil_tooling.lib import config, git, github
```

to:

```python
from vergil_tooling.lib import config, git
```

Remove the `--agent` argument from `parse_args`:

```python
    parser.add_argument(
        "--agent",
        required=False,
        default=None,
        help="Deprecated. Co-author identity is now auto-discovered.",
    )
```

Delete those four lines entirely.

Replace the co-author resolution block in `main()`. Change:

```python
    if args.agent is not None:
        print(
            "WARNING: --agent is deprecated and will be removed in a future release. "
            "Co-author identity is now auto-discovered from gh auth status.",
            file=sys.stderr,
        )

    try:
        identity = github.resolve_co_author_trailer()
    except (SystemExit, github.GitHubAPIError) as exc:
        print(f"ERROR: failed to resolve co-author identity: {exc}", file=sys.stderr)
        return 1
```

to:

```python
    co_author = os.environ.get("VRG_CO_AUTHOR")
```

Add `import os` to the imports at the top of the file.

Replace the commit-message formatting block. Change:

```python
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(f"{subject}\n")
        if args.body:
            f.write(f"\n{args.body}\n")
        f.write(f"\n{identity}\n")
        tmp_path = f.name
```

to:

```python
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(f"{subject}\n")
        if args.body:
            f.write(f"\n{args.body}\n")
        if co_author:
            f.write(f"\nCo-Authored-By: {co_author}\n")
        tmp_path = f.name
```

- [ ] **Step 4: Delete obsolete test code**

In `tests/vergil_tooling/test_vrg_commit.py`, delete:

- `test_main_auto_discovery` (tests resolve_co_author_trailer)
- `test_main_agent_flag_prints_deprecation_warning` (--agent removed)
- `test_main_resolve_failure_returns_1` (no longer fails on co-author)

Update all remaining tests that pass `--agent` in their argv to
remove that flag. For example, change:

```python
["--type", "feat", "--scope", "core", "--message", "test", "--agent", "agent"]
```

to:

```python
["--type", "feat", "--scope", "core", "--message", "test"]
```

Update the `_commit_environment` fixture call sites that
previously mocked `resolve_co_author_trailer` — those mocks are
no longer needed since the fixture now uses `co_author` parameter.

- [ ] **Step 5: Run full vrg-commit test suite**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_commit.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
vrg-commit --type refactor --scope commit \
  --message "replace account-based co-author with VRG_CO_AUTHOR env var" \
  --body "Co-author identity now comes from the AI harness via env var. Remove --agent flag and resolve_co_author_trailer dependency."
```

---

### Task 4: Clean Up github.py

Delete the old credential functions that are no longer called by
any consumer. Rewrite `_gh_env()` to use
`get_installation_token()`.

**Files:**
- Modify: `src/vergil_tooling/lib/github.py`
- Modify: `tests/vergil_tooling/test_github.py`

- [ ] **Step 1: Write tests for new `_gh_env` behavior**

Add to `tests/vergil_tooling/test_github.py`:

```python
class TestGhEnvNew:
    def test_returns_env_with_app_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("vergil_tooling.lib.github._gh_env", _real_gh_env)
        with patch(
            "vergil_tooling.lib.github.get_installation_token",
            return_value="ghs_app_token",
        ):
            env = github._gh_env()
        assert env is not None
        assert env["GH_TOKEN"] == "ghs_app_token"  # noqa: S105

    def test_returns_none_when_no_app_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("vergil_tooling.lib.github._gh_env", _real_gh_env)
        with patch(
            "vergil_tooling.lib.github.get_installation_token",
            return_value=None,
        ):
            assert github._gh_env() is None
```

- [ ] **Step 2: Delete old functions from github.py**

In `src/vergil_tooling/lib/github.py`, delete:

- `_discover_accounts()` (lines 24-44)
- `resolve_co_author_trailer()` (lines 47-68)
- `_human_token()` (lines 71-81, including the `@functools.lru_cache` decorator)

Remove `functools` from the imports (it was only used by
`_human_token`).

- [ ] **Step 3: Rewrite `_gh_env()`**

Replace the existing `_gh_env()`:

```python
def _gh_env() -> dict[str, str] | None:
    """Return env dict with the human account's GH_TOKEN, or None on failure."""
    try:
        token = _human_token()
    except (subprocess.CalledProcessError, SystemExit):
        return None
    return {**os.environ, "GH_TOKEN": token}
```

with:

```python
def _gh_env() -> dict[str, str] | None:
    """Return env dict with App installation token, or ``None`` for ambient auth."""
    token = get_installation_token()
    if token is None:
        return None
    return {**os.environ, "GH_TOKEN": token}
```

- [ ] **Step 4: Delete old tests**

In `tests/vergil_tooling/test_github.py`, delete:

- `TestDiscoverAccounts` class (all tests)
- `TestHumanToken` class (all tests)
- `TestGhEnv` class (replaced by `TestGhEnvNew`)
- `TestResolveCoAuthorTrailer` class (all tests)
- `_AUTH_TWO_ACCOUNTS`, `_AUTH_MANY_ACCOUNTS`, `_AUTH_NO_VERGIL`
  string constants

Update the `_no_credential_injection` autouse fixture — remove the
`_human_token.cache_clear()` call (function no longer exists):

```python
@pytest.fixture(autouse=True)
def _no_credential_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)
    github._cached_token = None
```

- [ ] **Step 5: Run full github.py test suite**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
vrg-commit --type refactor --scope github \
  --message "delete multi-account credential functions" \
  --body "Remove _discover_accounts, resolve_co_author_trailer, _human_token. Rewrite _gh_env to use get_installation_token."
```

---

### Task 5: HTTPS Token Injection in vrg-git

Add token injection for git operations that contact GitHub
(`push`, `pull`, `fetch`, `ls-remote`). Uses the
`http.extraHeader` git config mechanism via environment variables
— no temp files, no credential helpers.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_git.py`
- Modify: `tests/vergil_tooling/test_vrg_git.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/vergil_tooling/test_vrg_git.py`:

```python
from unittest.mock import patch


class TestRemoteTokenInjection:
    @pytest.mark.parametrize("subcmd", ["push", "pull", "fetch", "ls-remote"])
    def test_injects_token_for_remote_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value="ghs_token_123",
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            from vergil_tooling.bin.vrg_git import main

            rc = main([subcmd, "origin", "main"])
        assert rc == 0
        _, kwargs = mock_run.call_args
        env = kwargs["env"]
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
        assert "Authorization: Basic" in env["GIT_CONFIG_VALUE_0"]

    @pytest.mark.parametrize("subcmd", ["status", "log", "diff", "add", "branch"])
    def test_no_injection_for_local_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value="ghs_token_123",
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            from vergil_tooling.bin.vrg_git import main

            main([subcmd])
        _, kwargs = mock_run.call_args
        assert "env" not in kwargs or kwargs.get("env") is None

    def test_no_injection_when_no_app_token(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            from vergil_tooling.bin.vrg_git import main

            main(["push", "origin", "main"])
        _, kwargs = mock_run.call_args
        assert "env" not in kwargs or kwargs.get("env") is None

    def test_token_encodes_as_basic_auth(self) -> None:
        import base64

        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value="ghs_test_token",
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            from vergil_tooling.bin.vrg_git import main

            main(["push", "origin", "main"])
        _, kwargs = mock_run.call_args
        header_value = kwargs["env"]["GIT_CONFIG_VALUE_0"]
        expected = base64.b64encode(b"x-access-token:ghs_test_token").decode()
        assert expected in header_value
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py::TestRemoteTokenInjection -v`
Expected: FAIL

- [ ] **Step 3: Implement token injection**

In `src/vergil_tooling/bin/vrg_git.py`, add the import and
constant after the existing imports:

```python
import base64
import os

from vergil_tooling.lib import github

_REMOTE_SUBCOMMANDS: set[str] = {"push", "pull", "fetch", "ls-remote"}
```

Add a helper function after `_check_denied_flags`:

```python
def _git_auth_env(token: str) -> dict[str, str]:
    """Return env dict that authenticates HTTPS git to GitHub."""
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }
```

In the `main()` function, modify the final `subprocess.run` calls
for simple commands to inject auth when needed. At the end of the
simple-command path (the last `subprocess.run` call in `main`),
change:

```python
    result = subprocess.run(["git", *argv], check=False)  # noqa: S603, S607
    return result.returncode
```

to:

```python
    env = None
    if subcmd in _REMOTE_SUBCOMMANDS:
        token = github.get_installation_token()
        if token is not None:
            env = _git_auth_env(token)
    result = subprocess.run(["git", *argv], check=False, env=env)  # noqa: S603, S607
    return result.returncode
```

Apply the same change to the compound-command path (the
`subprocess.run` inside the `_ALLOWED_COMPOUND` block):

```python
        env = None
        if subcmd in _REMOTE_SUBCOMMANDS:
            token = github.get_installation_token()
            if token is not None:
                env = _git_auth_env(token)
        result = subprocess.run(["git", *argv], check=False, env=env)  # noqa: S603, S607
        return result.returncode
```

- [ ] **Step 4: Run remote token injection tests**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py::TestRemoteTokenInjection -v`
Expected: PASS

- [ ] **Step 5: Run full vrg-git test suite**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -v`
Expected: PASS (existing tests unaffected — they don't mock
`github.get_installation_token` so it returns `None` and no env
is injected)

- [ ] **Step 6: Commit**

```bash
vrg-commit --type feat --scope git \
  --message "HTTPS token injection for remote git operations" \
  --body "push/pull/fetch/ls-remote inject App installation token via http.extraHeader when available. Local operations are unaffected."
```

---

### Task 6: Full Validation

- [ ] **Step 1: Run full validation**

```bash
cd /Users/pmoore/dev/projects/vergil-project/vergil-tooling/.worktrees/issue-933-single-account-identity && vrg-docker-run -- uv run vrg-validate
```

- [ ] **Step 2: Fix any lint, typecheck, or test failures**

Common issues to watch for:
- Unused imports (`functools` in github.py, `github` in
  vrg_commit.py)
- Type annotations on new functions
- `noqa` comments on `subprocess.run` and list literals
- Test fixtures referencing deleted functions

- [ ] **Step 3: Commit any fixes**

```bash
vrg-commit --type fix --scope tooling \
  --message "fix validation issues from identity refactor" \
  --body "Address lint, typecheck, or test failures from the single-account identity changes"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** Token exchange (Credential Strategy
  section), wrapper simplification (Tooling Changes section),
  co-author resolution (How commits and PRs work), HTTPS git auth
  (Git transport open question) — all covered.
- [x] **Placeholder scan:** No TBD, TODO, or "implement later."
  All steps contain exact code.
- [x] **Type consistency:** Function names (`_load_app_config`,
  `_generate_jwt`, `get_installation_token`, `_git_auth_env`),
  variable names (`_cached_token`, `co_author`), and env var
  names (`VRG_CO_AUTHOR`, `VRG_APP_ID`, etc.) are consistent
  across all tasks.
- [x] **Task ordering:** Each task is independently committable.
  Task 2 removes vrg-gh's import of `_discover_accounts`. Task 3
  removes vrg-commit's call to `resolve_co_author_trailer`. Task 4
  deletes the functions (now safe — no remaining callers). No
  broken intermediate states.
- [x] **Scope boundaries:** This plan does NOT include nerdctl
  runtime detection (Plan 5 Tasks 1-2), VM provisioning (Plan 3),
  egress filtering (Plan 4), or doc/spec updates beyond code
  changes. CLAUDE.md/AGENTS.md updates are a separate concern.
- [x] **Zero new dependencies:** JWT signing uses `openssl` CLI,
  not PyJWT. `pyproject.toml` `dependencies` stays `[]`.
- [x] **Backward compatibility:** On the host (no App config),
  `get_installation_token()` returns `None` and all wrappers fall
  through to ambient `gh` auth. No behavior change for host
  development.
