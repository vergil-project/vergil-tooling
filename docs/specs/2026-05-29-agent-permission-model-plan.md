# Agent Permission Model — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement Track A of the agent permission model spec — tooling changes that restrict agent write permissions and move PR submission to a human-triggered workflow.

**Architecture:** Identity-aware CLI wrappers select allowlists and denial messages based on an environment variable (`VRG_IDENTITY_MODE`) set by the VM configuration. A new `.vergil/` scratch directory convention enables structured agent→human data handoff for PR templates. The `vrg-submit-pr` tool gains a template-reading mode, and `vrg-gh`/`vrg-git` gain identity-aware restrictions and error detection.

**Tech Stack:** Python 3.12, pytest, argparse, subprocess, `os.environ`

**Spec:** `docs/specs/2026-05-29-agent-permission-model-design.md`

---

## File Structure

**New files:**

| File | Responsibility |
|---|---|
| `src/vergil_tooling/lib/identity.py` | Identity mode detection from environment — `current_mode()`, `is_agent()`, `is_human()` |
| `src/vergil_tooling/lib/pr_template.py` | Read, write, delete `.vergil/pr-template.yml` — simple YAML-subset parser |
| `src/vergil_tooling/lib/pr_provenance.py` | Pre-merge PR provenance check — classify identities, evaluate forbidden/advisory actions |
| `src/vergil_tooling/bin/vrg_finalize_pr.py` | Consolidation of `vrg_finalize_repo.py` cleanup + merge + provenance check |
| `tests/vergil_tooling/test_identity.py` | Tests for identity module |
| `tests/vergil_tooling/test_pr_template.py` | Tests for PR template module |
| `tests/vergil_tooling/test_pr_provenance.py` | Tests for PR provenance module |
| `tests/vergil_tooling/test_vrg_finalize_pr.py` | Tests for consolidated tool (cleanup + merge + provenance) |

**Modified files:**

| File | Change |
|---|---|
| `.gitignore` | Add `.vergil/` entry |
| `src/vergil_tooling/bin/vrg_gh.py` | Identity-aware allowlists, denial messages, and `gh api` access (human full / audit GET / user denied) |
| `src/vergil_tooling/bin/vrg_submit_pr.py` | Template mode, identity gate, refactored into CLI/template paths |
| `src/vergil_tooling/bin/vrg_git.py` | Push workflow-error detection with identity-aware feedback |
| `src/vergil_tooling/bin/vrg_finalize_repo.py` | Reduced to deprecated alias that imports `vrg_finalize_pr` |
| `src/vergil_tooling/lib/release/finalize.py` | Update subprocess call from `vrg-finalize-repo` to `vrg-finalize-pr` |
| `src/vergil_tooling/lib/github.py` | Add `pr_state` helper; update merge docstring reference to `vrg-finalize-pr` |
| `pyproject.toml` | Add `vrg-finalize-pr` console script entry |
| `tests/vergil_tooling/test_vrg_gh.py` | Update allowed/denied pairs, add identity-aware tests |
| `tests/vergil_tooling/test_vrg_submit_pr.py` | Add template mode, identity gate, and confirmation tests |
| `tests/vergil_tooling/test_vrg_git.py` | Add push workflow-error detection tests |
| `tests/vergil_tooling/test_vrg_finalize_repo.py` | Reduce to deprecated-alias tests |
| `tests/vergil_tooling/test_release_finalize.py` | Update `vrg-finalize-repo` → `vrg-finalize-pr` reference |

---

### Task 1: `.vergil/` Gitignore Convention

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add `.vergil/` to `.gitignore`**

In `.gitignore`, add the entry after `.worktrees/`:

```gitignore
.worktrees/
.vergil/
```

- [ ] **Step 2: Commit**

```bash
vrg-git add .gitignore
vrg-commit --type chore --scope gitignore --message "add .vergil/ scratch directory to gitignore"
```

---

### Task 2: Identity Detection Module

**Files:**
- Create: `src/vergil_tooling/lib/identity.py`
- Create: `tests/vergil_tooling/test_identity.py`

- [ ] **Step 1: Write failing tests for `current_mode()`**

Create `tests/vergil_tooling/test_identity.py`:

```python
"""Tests for vergil_tooling.lib.identity."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.identity import IdentityMode, current_mode, is_agent, is_human


class TestCurrentMode:
    def test_user_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert current_mode() == IdentityMode.USER

    def test_audit_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert current_mode() == IdentityMode.AUDIT

    def test_human_when_no_env_and_no_app(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert current_mode() == IdentityMode.HUMAN

    def test_fallback_to_user_with_app_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.setenv("VRG_APP_ID", "12345")
        assert current_mode() == IdentityMode.USER

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "USER")
        assert current_mode() == IdentityMode.USER

    def test_whitespace_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "  audit  ")
        assert current_mode() == IdentityMode.AUDIT

    def test_invalid_mode_with_app_creds_falls_back_to_user(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "invalid")
        monkeypatch.setenv("VRG_APP_ID", "12345")
        assert current_mode() == IdentityMode.USER

    def test_invalid_mode_without_app_creds_falls_back_to_human(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "invalid")
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert current_mode() == IdentityMode.HUMAN


class TestIsAgent:
    def test_user_is_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert is_agent() is True

    def test_audit_is_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        assert is_agent() is True

    def test_human_is_not_agent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert is_agent() is False


class TestIsHuman:
    def test_human_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        assert is_human() is True

    def test_agent_is_not_human(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        assert is_human() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_identity.py -v`
Expected: ImportError — `vergil_tooling.lib.identity` does not exist yet.

- [ ] **Step 3: Implement identity module**

Create `src/vergil_tooling/lib/identity.py`:

```python
"""Agent identity detection from VM environment.

The VM provisioning sets ``VRG_IDENTITY_MODE`` alongside the GitHub App
credentials. The mode determines which allowlists and behaviors apply
in identity-aware tools (``vrg-gh``, ``vrg-submit-pr``, ``vrg-git``).

``VRG_IDENTITY_MODE`` is a Layer 1 ergonomic, not a security control.
An adversarial agent that becomes root inside the VM can set or unset
it; doing so only relaxes soft-gate allowlists. The real identity
boundary is the GitHub App credential provisioned to the VM, which no
value of this variable can change. See the design spec's Three-Layer
Security Model.
"""

from __future__ import annotations

import enum
import os


class IdentityMode(enum.Enum):
    HUMAN = "human"
    USER = "user"
    AUDIT = "audit"


_AGENT_MODES = frozenset({IdentityMode.USER, IdentityMode.AUDIT})

_ENV_VAR = "VRG_IDENTITY_MODE"


def current_mode() -> IdentityMode:
    """Detect the current identity mode from the environment."""
    raw = os.environ.get(_ENV_VAR, "").strip().lower()
    if raw:
        try:
            return IdentityMode(raw)
        except ValueError:
            pass
    if os.environ.get("VRG_APP_ID"):
        return IdentityMode.USER
    return IdentityMode.HUMAN


def is_agent() -> bool:
    """Return True if running as any agent identity."""
    return current_mode() in _AGENT_MODES


def is_human() -> bool:
    """Return True if running as the human (Chief Steward)."""
    return current_mode() == IdentityMode.HUMAN
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_identity.py -v`
Expected: All 13 tests PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/identity.py tests/vergil_tooling/test_identity.py
vrg-commit --type feat --scope identity --message "add identity detection module for VM mode awareness"
```

---

### Task 3: `vrg-gh` Allowlist Restrictions and Identity-Aware Messages

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_gh.py`
- Modify: `tests/vergil_tooling/test_vrg_gh.py`

**Architecture note:** This task modifies `vrg-gh`, the *wrapper script* layer — not the hook guard. The two layers have distinct responsibilities: (1) the hook guard (`vrg-hook-guard`) is a dumb gate that blocks raw `git`/`gh` and redirects to `vrg-git`/`vrg-gh` — it knows nothing about subcommands or identity; (2) the wrapper scripts (`vrg-gh`, `vrg-git`) parse subcommands, apply identity-aware allowlists, and redirect to custom tools like `vrg-submit-pr`. No changes to the hook guard are needed for this task.

This task converts `vrg-gh` from a static allowlist to an identity-aware system:
- User agents lose: `issue close`, `issue reopen`, `issue edit`, `pr edit`, `pr merge`
- Audit agents can only use: `pr view`, `pr diff`, `pr list`, `pr checks`, `pr comment`, `pr review`
- `pr create` denied message stops mentioning `vrg-submit-pr` for agent identities
- `pr review --approve` is allowed for audit and human identities, denied for the user identity
- Human identity retains the full current allowlist

The approach: keep `_ALLOWED` as the human-level allowlist (maximum permissions). Add a `_denied_pairs()` function that returns identity-specific denied pairs. For audit mode, use a separate restricted allowlist. The denied-pairs check runs before the allowlist check, so agent-specific denials take precedence.

- [ ] **Step 1: Write failing tests for agent-specific denials**

Add these tests to `tests/vergil_tooling/test_vrg_gh.py`. The new tests patch `VRG_IDENTITY_MODE` to test identity-aware behavior.

At the top of the file, add the import:

```python
from unittest.mock import patch
```

(Already imported — verify it's there.)

Add a new test class after the existing tests:

```python
# -- identity-aware restrictions -----------------------------------------------


class TestAgentDenials:
    """Commands blocked for the user agent identity."""

    @pytest.fixture(autouse=True)
    def _agent_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("issue", "close"),
            ("issue", "reopen"),
            ("issue", "edit"),
            ("pr", "edit"),
            ("pr", "merge"),
        ],
    )
    def test_agent_denied_pair(
        self, top: str, sub: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([top, sub]) != 0
        err = capsys.readouterr().err
        assert "denied" in err.lower()

    def test_issue_close_says_race_director(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["issue", "close", "42"])
        err = capsys.readouterr().err
        assert "human maintainer" in err.lower()

    def test_pr_merge_denied_unconditionally_for_agent(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["pr", "merge", "42"]) != 0
        err = capsys.readouterr().err
        assert "denied" in err.lower()

    def test_pr_create_no_vrg_submit_pr_mention(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["pr", "create"])
        err = capsys.readouterr().err
        assert "vrg-submit-pr" not in err


class TestHumanAllowlist:
    """Human identity retains full allowlist."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("issue", "close"),
            ("issue", "reopen"),
            ("issue", "edit"),
            ("pr", "edit"),
        ],
    )
    def test_human_allowed_pair(self, top: str, sub: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main([top, sub])
        assert rc == 0

    def test_pr_merge_allowed_for_human_with_context(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["pr", "merge", "42"])
        assert rc == 0

    def test_pr_create_mentions_vrg_submit_pr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["pr", "create"])
        err = capsys.readouterr().err
        assert "vrg-submit-pr" in err


class TestAuditAllowlist:
    """Audit identity can only do PR read/review operations."""

    @pytest.fixture(autouse=True)
    def _audit_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("pr", "view"),
            ("pr", "diff"),
            ("pr", "list"),
            ("pr", "checks"),
            ("pr", "comment"),
        ],
    )
    def test_audit_pr_read_allowed(self, top: str, sub: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main([top, sub])
        assert rc == 0

    def test_audit_pr_review_allowed(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["pr", "review"])
        assert rc == 0

    def test_audit_pr_review_approve_allowed(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["pr", "review", "--approve"])
        assert rc == 0

    @pytest.mark.parametrize(
        ("top", "sub"),
        [
            ("issue", "view"),
            ("issue", "list"),
            ("issue", "create"),
            ("run", "list"),
            ("repo", "view"),
            ("label", "list"),
        ],
    )
    def test_audit_non_pr_denied(
        self, top: str, sub: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([top, sub]) != 0


class TestApiAccess:
    """`gh api` is identity-aware: human full, audit GET-only, user denied."""

    def test_user_api_denied(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        rc = main(["api", "repos/o/r/pulls/1/reviews"])
        assert rc == 1
        assert "denied for the user identity" in capsys.readouterr().err

    def test_audit_api_get_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["api", "repos/o/r/pulls/1/reviews"])
        assert rc == 0

    @pytest.mark.parametrize(
        "extra",
        [
            ["-X", "POST"],
            ["--method", "DELETE"],
            ["-f", "title=x"],
            ["--field", "body=y"],
        ],
    )
    def test_audit_api_write_denied(
        self, extra: list[str], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        rc = main(["api", "repos/o/r/issues/1/comments", *extra])
        assert rc == 1
        assert "read-only GET" in capsys.readouterr().err

    def test_human_api_allowed(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_gh.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            rc = main(["api", "repos/o/r/pulls/1/merge", "-X", "PUT"])
        assert rc == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py::TestAgentDenials -v`
Expected: FAIL — `vrg_gh` does not read identity mode yet.

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py::TestApiAccess -v`
Expected: FAIL — `gh api` is not yet identity-aware.

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py::TestAuditAllowlist -v`
Expected: FAIL — no audit allowlist.

- [ ] **Step 3: Implement identity-aware `vrg-gh`**

Replace the module-level constants and update `main()` in `src/vergil_tooling/bin/vrg_gh.py`:

```python
"""Safe gh wrapper for AI agent sessions.

Enforces identity-aware subcommand allowlists and flag deny lists.
Injects GitHub App installation tokens when available.
"""

from __future__ import annotations

import os
import subprocess
import sys

from vergil_tooling.lib import github, identity, retry

_ALLOWED: dict[str, set[str]] = {
    "issue": {"view", "create", "close", "reopen", "edit", "list", "comment"},
    "pr": {"view", "checks", "list", "diff", "comment", "edit", "review", "merge"},
    "run": {"list", "view", "watch"},
    "repo": {"view", "list"},
    "label": {"list", "create"},
}

_ALLOWED_AUDIT: dict[str, set[str]] = {
    "pr": {"view", "diff", "list", "checks", "comment", "review"},
}

_DENIED_ALWAYS: dict[str, dict[str, str]] = {
    "pr": {
        "close": "gh pr close is denied by vrg-gh.",
    },
    "repo": {
        "edit": "gh repo edit is denied by vrg-gh.",
        "create": "gh repo create is denied by vrg-gh.",
        "delete": "gh repo delete is denied by vrg-gh.",
    },
}

_DENIED_AGENT: dict[str, dict[str, str]] = {
    "pr": {
        "create": "PR creation requires a human maintainer.",
        "edit": "PR edit requires a human maintainer.",
        "merge": "PR merge requires a human maintainer.",
    },
    "issue": {
        "close": "Issue close requires a human maintainer.",
        "reopen": "Issue reopen requires a human maintainer.",
        "edit": "Issue edit requires a human maintainer.",
    },
}

_DENIED_HUMAN: dict[str, dict[str, str]] = {
    "pr": {
        "create": "Use vrg-submit-pr instead of gh pr create.",
    },
}

_DENIED_TOP: dict[str, str] = {
    "auth": "gh auth is denied by vrg-gh.",
}

# `gh api` is identity-aware (handled in main, not in _DENIED_TOP):
#   human -> full; audit -> read-only GET; user/other agent -> denied.
# These flags flip gh's default verb from GET to POST.
_API_WRITE_FLAGS: set[str] = {"-f", "-F", "--field", "--raw-field", "--input"}


def _get_allowed() -> dict[str, set[str]]:
    if identity.current_mode() == identity.IdentityMode.AUDIT:
        return _ALLOWED_AUDIT
    return _ALLOWED


def _get_denied_pairs() -> dict[str, dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for source in (_DENIED_ALWAYS,):
        for top, subs in source.items():
            merged.setdefault(top, {}).update(subs)
    if identity.is_agent():
        for top, subs in _DENIED_AGENT.items():
            merged.setdefault(top, {}).update(subs)
    else:
        for top, subs in _DENIED_HUMAN.items():
            merged.setdefault(top, {}).update(subs)
    return merged


def _api_is_get(argv: list[str]) -> bool:
    """Return True if a ``gh api`` invocation is a read-only GET.

    gh defaults to GET, flips to POST when fields are present, and
    honors an explicit ``-X``/``--method``.
    """
    method: str | None = None
    has_fields = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-X", "--method"):
            if i + 1 < len(argv):
                method = argv[i + 1].upper()
            i += 2
            continue
        if arg.startswith("--method="):
            method = arg.split("=", 1)[1].upper()
        elif arg in _API_WRITE_FLAGS or arg.startswith(("--field=", "--raw-field=")):
            has_fields = True
        i += 1
    if method is not None:
        return method == "GET"
    return not has_fields


def _exec_gh(argv: list[str]) -> int:
    """Inject the installation token and execute ``gh`` with retry."""
    token = github.get_installation_token()
    env: dict[str, str] | None = None
    if token is not None:
        env = {**os.environ, "GH_TOKEN": token}
    try:
        result = retry.run_with_retry(
            ["gh", *argv],  # noqa: S607
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            sys.stdout.write(exc.stdout)
        if exc.stderr:
            sys.stderr.write(exc.stderr)
        return exc.returncode
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return 0


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

    # Identity-aware `gh api`: the broad escape hatch is gated per identity.
    if top == "api":
        mode = identity.current_mode()
        if mode == identity.IdentityMode.USER:
            print(
                "vrg-gh: gh api is denied for the user identity "
                "(broad write-capable escape hatch).",
                file=sys.stderr,
            )
            return 1
        if mode == identity.IdentityMode.AUDIT and not _api_is_get(argv):
            print(
                "vrg-gh: gh api is restricted to read-only GET calls "
                "for the audit identity.",
                file=sys.stderr,
            )
            return 1
        # human (full) or audit GET: execute directly, bypassing the
        # subcommand-pair allowlist (api has no fixed sub-actions).
        return _exec_gh(argv)

    allowed = _get_allowed()

    if top not in allowed:
        print(
            f"vrg-gh: {top} is not recognized. Allowed: {', '.join(sorted(allowed))}",
            file=sys.stderr,
        )
        return 1

    if len(argv) < 2:  # noqa: PLR2004
        print(
            f"vrg-gh: {top} requires a subcommand. Allowed: {', '.join(sorted(allowed[top]))}",
            file=sys.stderr,
        )
        return 1

    sub = argv[1]

    denied_pairs = _get_denied_pairs()
    if top in denied_pairs and sub in denied_pairs[top]:
        msg = denied_pairs[top][sub]
        print(f"vrg-gh: {top} {sub} is denied. {msg}", file=sys.stderr)
        return 1

    if sub not in allowed[top]:
        print(
            f"vrg-gh: {top} {sub} is not recognized. Allowed: {', '.join(sorted(allowed[top]))}",
            file=sys.stderr,
        )
        return 1

    mode = identity.current_mode()

    if top == "pr" and sub == "review" and "--approve" in argv:
        if mode not in (identity.IdentityMode.AUDIT, identity.IdentityMode.HUMAN):
            print(
                "vrg-gh: pr review --approve is denied. "
                "PR approval is restricted to the audit identity or a human maintainer.",
                file=sys.stderr,
            )
            return 1

    if top == "pr" and sub == "merge" and not identity.is_agent():
        if len(argv) < 3:  # noqa: PLR2004
            print(
                "vrg-gh: pr merge is denied. pr merge requires a PR number or URL.",
                file=sys.stderr,
            )
            return 1

    return _exec_gh(argv)
```

- [ ] **Step 4: Update existing tests for compatibility**

The existing `_ALLOWED_PAIRS` and `_DENIED_PAIRS` parametrized tests run without an identity mode set, so they default to human mode. Seven existing tests need updates:

**4a.** Remove `("pr", "merge")` from `_ALLOWED_PAIRS` — it's tested separately in `TestHumanAllowlist`:

Replace:
```python
_ALLOWED_PAIRS: list[tuple[str, str]] = [
    ("issue", "view"),
    ("issue", "create"),
    ("issue", "close"),
    ("issue", "reopen"),
    ("issue", "edit"),
    ("issue", "list"),
    ("issue", "comment"),
    ("pr", "view"),
    ("pr", "checks"),
    ("pr", "list"),
    ("pr", "diff"),
    ("pr", "comment"),
    ("pr", "edit"),
    ("run", "list"),
    ("run", "view"),
    ("run", "watch"),
    ("repo", "view"),
    ("repo", "list"),
    ("label", "list"),
    ("label", "create"),
]
```

With (removed `pr merge`):
```python
_ALLOWED_PAIRS: list[tuple[str, str]] = [
    ("issue", "view"),
    ("issue", "create"),
    ("issue", "close"),
    ("issue", "reopen"),
    ("issue", "edit"),
    ("issue", "list"),
    ("issue", "comment"),
    ("pr", "view"),
    ("pr", "checks"),
    ("pr", "list"),
    ("pr", "diff"),
    ("pr", "comment"),
    ("pr", "edit"),
    ("run", "list"),
    ("run", "view"),
    ("run", "watch"),
    ("repo", "view"),
    ("repo", "list"),
    ("label", "list"),
    ("label", "create"),
]
```

**4b.** Remove `test_pr_merge_allowed_with_valid_context` — replaced by `TestHumanAllowlist.test_pr_merge_allowed_for_human_with_context`.

**4c.** Remove `test_pr_merge_denied_without_args` — replaced by the human merge context check in the updated `main()`.

**4d.** Remove `test_pr_create_denied_suggests_vrg_submit_pr` — replaced by `TestHumanAllowlist.test_pr_create_mentions_vrg_submit_pr`.

**4e.** Update `test_pr_review_approve_denied` to set the user identity (without it, human mode allows approve):

Replace:
```python
def test_pr_review_approve_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["pr", "review", "--approve"]) != 0
    err = capsys.readouterr().err
    assert "approve" in err.lower()
```

With:
```python
def test_pr_review_approve_denied_for_user(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    assert main(["pr", "review", "--approve"]) != 0
    err = capsys.readouterr().err
    assert "approve" in err.lower()
```

**4f.** `test_pr_review_no_flags_allowed` and `test_pr_review_comment_allowed` — no changes needed (human mode, no --approve).

**4g.** Remove `test_api_denied` — `gh api` is no longer denied unconditionally. With no identity mode set the test defaulted to human, where `api` is now allowed. The identity-aware behavior is covered by `TestApiAccess` (user denied, audit GET-only, human full).

- [ ] **Step 5: Run all vrg-gh tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_gh.py -v`
Expected: All tests PASS.

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_gh.py tests/vergil_tooling/test_vrg_gh.py
vrg-commit --type feat --scope vrg-gh --message "add identity-aware allowlists and agent-specific denials"
```

---

### Task 4: PR Template Library

**Files:**
- Create: `src/vergil_tooling/lib/pr_template.py`
- Create: `tests/vergil_tooling/test_pr_template.py`

This module handles reading, writing, and deleting `.vergil/pr-template.yml` files. It includes a minimal YAML-subset parser (no PyYAML dependency) that handles flat `key: value` pairs and `key: |` multi-line blocks.

- [ ] **Step 1: Write failing tests**

Create `tests/vergil_tooling/test_pr_template.py`:

```python
"""Tests for vergil_tooling.lib.pr_template."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.pr_template import (
    TemplateError,
    delete_template,
    read_template,
    write_template,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestParseAndRead:
    def test_reads_simple_fields(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\n"
            "title: 'fix: bug'\n"
            "summary: Fix the bug\n"
            "linkage: Ref\n"
            "notes: Tested on macOS\n"
        )
        result = read_template(tmp_path)
        assert result["issue"] == "42"
        assert result["title"] == "fix: bug"
        assert result["summary"] == "Fix the bug"
        assert result["linkage"] == "Ref"
        assert result["notes"] == "Tested on macOS"

    def test_reads_double_quoted_values(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            'issue: 42\n'
            'title: "feat(perms): restrict agent writes"\n'
            'summary: Restrict writes\n'
        )
        result = read_template(tmp_path)
        assert result["title"] == "feat(perms): restrict agent writes"

    def test_reads_multiline_block(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\n"
            "title: fix\n"
            "summary: |\n"
            "  Line one\n"
            "  Line two\n"
            "linkage: Ref\n"
        )
        result = read_template(tmp_path)
        assert result["summary"] == "Line one\nLine two"

    def test_skips_comment_lines(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "# Generated by agent\n"
            "issue: 42\n"
            "title: fix\n"
            "summary: Fix\n"
        )
        result = read_template(tmp_path)
        assert result["issue"] == "42"

    def test_raises_when_file_missing(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_template(tmp_path)

    def test_raises_when_required_field_missing(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text("issue: 42\ntitle: fix\n")
        with pytest.raises(TemplateError, match="summary"):
            read_template(tmp_path)


class TestWriteTemplate:
    def test_creates_vergil_dir_and_file(self, tmp_path: Path) -> None:
        path = write_template(
            tmp_path, issue="42", title="fix: bug", summary="Fix the bug"
        )
        assert path.exists()
        content = path.read_text()
        assert "issue:" in content
        assert "42" in content
        assert "fix: bug" in content

    def test_roundtrip(self, tmp_path: Path) -> None:
        write_template(
            tmp_path,
            issue="99",
            title="feat(auth): add token rotation",
            summary="Adds token rotation\nfor the GitHub App.",
            linkage="Ref",
            notes="Tested in staging VM.",
        )
        result = read_template(tmp_path)
        assert result["issue"] == "99"
        assert result["title"] == "feat(auth): add token rotation"
        assert "token rotation" in result["summary"]
        assert result["linkage"] == "Ref"
        assert result["notes"] == "Tested in staging VM."

    def test_warns_on_overwrite(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text("old content")
        write_template(tmp_path, issue="42", title="fix", summary="Fix")
        err = capsys.readouterr().err
        assert "overwriting" in err.lower()

    def test_default_linkage(self, tmp_path: Path) -> None:
        write_template(tmp_path, issue="42", title="fix", summary="Fix")
        result = read_template(tmp_path)
        assert result["linkage"] == "Ref"


class TestDeleteTemplate:
    def test_deletes_existing_file(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        f = vergil / "pr-template.yml"
        f.write_text("content")
        delete_template(tmp_path)
        assert not f.exists()

    def test_no_error_when_missing(self, tmp_path: Path) -> None:
        delete_template(tmp_path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_pr_template.py -v`
Expected: ImportError — `vergil_tooling.lib.pr_template` does not exist yet.

- [ ] **Step 3: Implement PR template library**

Create `src/vergil_tooling/lib/pr_template.py`:

```python
"""Read, write, and delete ``.vergil/pr-template.yml`` files.

Uses a minimal YAML-subset parser — no PyYAML dependency. Handles
flat ``key: value`` pairs, quoted values, and ``key: |`` multi-line
blocks. This is sufficient for the PR template format.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TEMPLATE_DIR = ".vergil"
_TEMPLATE_FILE = "pr-template.yml"
_REQUIRED_FIELDS = ("issue", "title", "summary")


class TemplateError(Exception):
    """Raised when a template file is malformed or missing required fields."""


def _parse(text: str) -> dict[str, str]:
    """Parse the pr-template.yml format."""
    result: dict[str, str] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "|":
            block_lines: list[str] = []
            i += 1
            while i < len(lines):
                if lines[i] and not lines[i][0].isspace():
                    break
                block_lines.append(lines[i][2:] if len(lines[i]) > 2 else lines[i].strip())
                i += 1
            result[key] = "\n".join(block_lines).strip()
        else:
            if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                value = value[1:-1]
            result[key] = value
            i += 1
    return result


def _template_path(worktree_root: Path) -> Path:
    return worktree_root / _TEMPLATE_DIR / _TEMPLATE_FILE


def read_template(worktree_root: Path) -> dict[str, str]:
    """Read and validate ``.vergil/pr-template.yml``.

    Raises ``FileNotFoundError`` if the file does not exist.
    Raises ``TemplateError`` if required fields are missing.
    """
    path = _template_path(worktree_root)
    if not path.exists():
        msg = f"No PR template found at {path}"
        raise FileNotFoundError(msg)
    fields = _parse(path.read_text())
    for field in _REQUIRED_FIELDS:
        if field not in fields:
            msg = f"PR template is missing required field: {field}"
            raise TemplateError(msg)
    return fields


def write_template(
    worktree_root: Path,
    *,
    issue: str,
    title: str,
    summary: str,
    linkage: str = "Ref",
    notes: str = "",
) -> Path:
    """Write ``.vergil/pr-template.yml``, warning if it already exists."""
    path = _template_path(worktree_root)
    if path.exists():
        print(
            f"WARNING: Overwriting existing PR template at {path}. "
            "A leftover template indicates a previous cycle was not completed.",
            file=sys.stderr,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by agent — review and edit before running vrg-submit-pr",
    ]
    for key, value in [
        ("issue", issue),
        ("title", title),
        ("summary", summary),
        ("linkage", linkage),
        ("notes", notes),
    ]:
        if not value:
            continue
        if "\n" in value:
            lines.append(f"{key}: |")
            for vline in value.splitlines():
                lines.append(f"  {vline}")
        elif ":" in value or value.startswith(("'", '"')):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")
    path.write_text("\n".join(lines) + "\n")
    return path


def delete_template(worktree_root: Path) -> None:
    """Delete the template file if it exists."""
    path = _template_path(worktree_root)
    path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_pr_template.py -v`
Expected: All 11 tests PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_template.py tests/vergil_tooling/test_pr_template.py
vrg-commit --type feat --scope pr-template --message "add PR template library for .vergil/ scratch convention"
```

---

### Task 5: `vrg-submit-pr` Redesign — Template Mode and Identity Gate

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_submit_pr.py`
- Modify: `tests/vergil_tooling/test_vrg_submit_pr.py`

The tool gains three major changes:
1. **Identity gate:** Aborts immediately for any agent identity.
2. **Template mode:** When no `--issue`/`--summary`/`--title` CLI args are provided, reads `.vergil/pr-template.yml` instead. Template mode prompts for confirmation before creating the PR.
3. **Ensure-pushed:** Before creating the PR, the tool pushes the branch to the remote using the human's host credentials. Because `vrg-submit-pr` runs in the human's context (the superset of any agent's rights, per the spec's Foundational Assumption), this push succeeds even when the branch touches `.github/workflows/` — which the agent's own push would have been rejected for. This is how workflow-touching changes reach the remote without any agent holding `workflows: write`. The agent never pushed (its push of a workflow-touching branch would fail), so template mode must push, not assume the branch is already on the remote.

CLI argument mode (existing behavior) is preserved for direct human use and also pushes.

- [ ] **Step 1: Write failing tests for identity gate**

Add to `tests/vergil_tooling/test_vrg_submit_pr.py`:

```python
class TestIdentityGate:
    """Agent identities are blocked from PR submission."""

    def test_user_mode_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result != 0
        assert "human maintainer" in capsys.readouterr().err.lower()

    def test_audit_mode_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result != 0

    def test_human_mode_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(
                ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--dry-run"]
            )
        assert result == 0
```

- [ ] **Step 2: Write failing tests for template mode**

Add to `tests/vergil_tooling/test_vrg_submit_pr.py`:

```python
class TestTemplateMode:
    """Template mode reads .vergil/pr-template.yml when no CLI args given."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_template_dry_run(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\n"
            "title: 'fix: bug'\n"
            "summary: Fix the bug\n"
            "linkage: Ref\n"
        )
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(["--dry-run"])
        assert result == 0
        out = capsys.readouterr().out
        assert "fix: bug" in out
        assert "#42" in out

    def test_template_creates_pr_on_confirm(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\ntitle: 'fix: bug'\nsummary: Fix the bug\n"
        )
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
            patch("builtins.input", return_value="y"),
        ):
            result = main([])
        assert result == 0
        mock_pr.assert_called_once()
        assert not (vergil / "pr-template.yml").exists()

    def test_template_aborts_on_decline(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\ntitle: fix\nsummary: Fix\n"
        )
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("builtins.input", return_value="n"),
        ):
            result = main([])
        assert result == 1
        assert (vergil / "pr-template.yml").exists()

    def test_template_ensures_pushed(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\ntitle: fix\nsummary: Fix\n"
        )
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run") as mock_git_run,
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
            patch("builtins.input", return_value="y"),
        ):
            main([])
        # Ensure-pushed: the branch is pushed with the human's credentials
        # before the PR is created.
        push_calls = [
            c for c in mock_git_run.call_args_list if c.args and c.args[0] == "push"
        ]
        assert push_calls, "expected vrg-submit-pr to push the branch"
        mock_pr.assert_called_once()

    def test_no_template_and_no_args_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path):
            result = main([])
        assert result != 0
        assert "pr-template.yml" in capsys.readouterr().err

    def test_partial_cli_args_errors(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = main(["--issue", "42"])
        assert result != 0
        assert "required" in capsys.readouterr().err.lower()

    def test_template_base_override(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\ntitle: fix\nsummary: Fix\n"
        )
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(["--base", "main", "--dry-run"])
        assert result == 0
        out = capsys.readouterr().out
        assert "main" in out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py::TestIdentityGate -v`
Expected: FAIL — no identity gate in `vrg_submit_pr`.

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py::TestTemplateMode -v`
Expected: FAIL — no template mode.

- [ ] **Step 4: Implement the redesigned `vrg-submit-pr`**

Replace `src/vergil_tooling/bin/vrg_submit_pr.py`:

```python
"""PR submission wrapper that constructs standards-compliant PR bodies.

Supports two modes:
- **Template mode** (no CLI args): reads ``.vergil/pr-template.yml``,
  shows a summary, prompts for confirmation, pushes the branch, and
  creates the PR.
- **CLI argument mode** (args provided): existing direct invocation
  for human emergency use.

Both modes ensure the branch is pushed using the human's host
credentials before creating the PR. Because the human is the superset
of any agent's rights, this carries workflow-touching pushes that the
agent's own credentials would be rejected for.

Agent identities are blocked — PR submission is a Chief Steward
(human) operation.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import git, github, identity, pr_template

ALLOWED_LINKAGES = ("Ref",)
_ISSUE_PLAIN_RE = re.compile(r"^[1-9]\d*$")
_ISSUE_CROSS_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9]\d*$")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Create a standards-compliant pull request.")
    parser.add_argument(
        "--issue", default=None, help="Issue reference: number or owner/repo#number"
    )
    parser.add_argument("--summary", default=None, help="One-line PR summary")
    parser.add_argument(
        "--linkage", default="Ref", choices=ALLOWED_LINKAGES, help="Issue linkage keyword"
    )
    parser.add_argument("--notes", default="", help="Additional notes")
    parser.add_argument("--title", default=None, help="PR title")
    parser.add_argument("--dry-run", action="store_true", help="Print without executing")
    parser.add_argument("--base", default=None, help="Override auto-detected target branch")
    return parser.parse_args(argv)


def _resolve_issue_ref(issue: str) -> str:
    """Validate and normalize the issue reference."""
    if _ISSUE_PLAIN_RE.match(issue):
        return f"#{issue}"
    if _ISSUE_CROSS_RE.match(issue):
        return issue
    msg = f"--issue must be a number (42) or cross-repo ref (owner/repo#42), got '{issue}'."
    raise SystemExit(msg)


def _build_pr_body(
    *, summary: str, linkage: str, issue_ref: str, notes: str
) -> str:
    notes_section = notes or "-"
    return (
        f"# Pull Request\n\n"
        f"## Summary\n\n- {summary}\n\n"
        f"## Issue Linkage\n\n- {linkage} {issue_ref}\n\n"
        f"## Notes\n\n- {notes_section}"
    )


def _target_branch(branch: str, base_override: str | None) -> str:
    if base_override:
        return base_override
    return "main" if branch.startswith("release/") else "develop"


def _create_pr(*, target_branch: str, title: str, pr_body: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(pr_body)
        tmp_path = f.name
    try:
        pr_url = github.create_pr(base=target_branch, title=title, body_file=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return pr_url


def _run_cli_mode(args: argparse.Namespace) -> int:
    assert args.issue is not None
    assert args.summary is not None
    assert args.title is not None

    issue_ref = _resolve_issue_ref(args.issue)
    branch = git.current_branch()
    target = _target_branch(branch, args.base)
    pr_body = _build_pr_body(
        summary=args.summary,
        linkage=args.linkage,
        issue_ref=issue_ref,
        notes=args.notes,
    )

    if args.dry_run:
        print(f"=== PR Title ===\n{args.title}\n")
        print(f"=== Target Branch ===\n{target}\n")
        print(f"=== PR Body ===\n{pr_body}")
        return 0

    print(f"Pushing branch '{branch}' to origin...")
    git.run("push", "-u", "origin", branch)

    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=args.title, pr_body=pr_body)
    print(f"PR created: {pr_url}")
    print(f"Done. PR URL: {pr_url}")
    return 0


def _run_template_mode(args: argparse.Namespace) -> int:
    root = Path(git.repo_root())

    try:
        fields = pr_template.read_template(root)
    except FileNotFoundError:
        print(
            "vrg-submit-pr: No .vergil/pr-template.yml found and no CLI arguments provided.\n"
            "  Either provide --issue, --summary, and --title, or ensure the agent\n"
            "  has written a PR template file.",
            file=sys.stderr,
        )
        return 1

    issue_ref = _resolve_issue_ref(fields["issue"])
    branch = git.current_branch()
    target = _target_branch(branch, args.base)
    title = fields["title"]
    linkage = fields.get("linkage", "Ref")
    notes = fields.get("notes", "")
    pr_body = _build_pr_body(
        summary=fields["summary"],
        linkage=linkage,
        issue_ref=issue_ref,
        notes=notes,
    )

    print(f"=== PR from template ===")
    print(f"Title:  {title}")
    print(f"Base:   {target}")
    print(f"Branch: {branch}")
    print(f"Issue:  {issue_ref}")
    print()
    print(f"=== Body Preview ===\n{pr_body}")

    if args.dry_run:
        return 0

    try:
        answer = input("\nSubmit this PR? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if answer != "y":
        print("Aborted.")
        return 1

    # Ensure-pushed: push with the human's host credentials before creating
    # the PR. The human is the superset of any agent's rights, so this push
    # succeeds even for branches that touch .github/workflows/ — which the
    # agent's own push would have been rejected for.
    print(f"Ensuring branch '{branch}' is pushed to origin...")
    git.run("push", "-u", "origin", branch)

    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=title, pr_body=pr_body)
    pr_template.delete_template(root)
    print(f"PR created: {pr_url}")
    print(f"Done. PR URL: {pr_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if identity.is_agent():
        print(
            "vrg-submit-pr: PR submission requires a human maintainer. "
            "Agents cannot submit PRs.",
            file=sys.stderr,
        )
        return 1

    cli_fields = [args.issue, args.summary, args.title]
    has_any = any(f is not None for f in cli_fields)
    has_all = all(f is not None for f in cli_fields)

    if has_any and not has_all:
        missing = []
        if args.issue is None:
            missing.append("--issue")
        if args.summary is None:
            missing.append("--summary")
        if args.title is None:
            missing.append("--title")
        print(
            f"vrg-submit-pr: The following required arguments are missing: "
            f"{', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    if has_all:
        return _run_cli_mode(args)
    return _run_template_mode(args)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Update existing tests for compatibility**

The existing tests pass `--issue`, `--summary`, `--title` as CLI args, so they use CLI mode. Three existing tests need updates:

**5a.** Remove `test_parse_args_title_is_required` — title is no longer `required` in argparse. The equivalent validation is in `TestTemplateMode.test_partial_cli_args_errors` which tests that partial CLI args return an error from `main()`.

Replace:
```python
def test_parse_args_title_is_required() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--issue", "42", "--summary", "Fix bug"])
```

With nothing — delete the test entirely.

**5b.** Rename `test_parse_args_required` — the args are no longer `required` by argparse (they default to `None`). The test still validates that values parse correctly when provided:

Replace:
```python
def test_parse_args_required() -> None:
    args = parse_args(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert args.issue == "42"
    assert args.summary == "Fix bug"
    assert args.title == "fix: bug"
    assert args.linkage == "Ref"
    assert args.dry_run is False
```

With:
```python
def test_parse_args_cli_fields() -> None:
    args = parse_args(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert args.issue == "42"
    assert args.summary == "Fix bug"
    assert args.title == "fix: bug"
    assert args.linkage == "Ref"
    assert args.dry_run is False
```

**5c.** All existing `test_main_*` tests default to human identity (no env var), so the identity gate does not fire. They should continue to pass without changes.

- [ ] **Step 6: Run all vrg-submit-pr tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_submit_pr.py tests/vergil_tooling/test_vrg_submit_pr.py
vrg-commit --type feat --scope vrg-submit-pr --message "add template mode and identity gate for human-triggered PR submission"
```

---

### Task 6: `vrg-git` Push Workflow Error Detection

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_git.py`
- Modify: `tests/vergil_tooling/test_vrg_git.py`

When `vrg-git push` fails because the GitHub App token lacks `workflows` permission, the wrapper detects the specific error and provides identity-aware guidance instead of raw stderr. The agent is told to stop and escalate — not to work around the failure.

The GitHub error pattern:
```
refusing to allow a GitHub App to create or update workflow `.github/workflows/...` without `workflows` permission
```

- [ ] **Step 1: Write failing tests**

Add to `tests/vergil_tooling/test_vrg_git.py`:

```python
class TestPushWorkflowErrorDetection:
    """vrg-git push detects workflow permission errors and provides guidance."""

    _WORKFLOW_ERR = (
        "refusing to allow a GitHub App to create or update workflow "
        "`.github/workflows/ci.yml` without `workflows` permission"
    )

    def test_workflow_error_detected_and_guidance_printed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=1,
                stdout="",
                stderr=self._WORKFLOW_ERR,
            )
            rc = main(["push", "origin", "feature/x"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "workflow" in err.lower()
        assert "escalate" in err.lower() or "human maintainer" in err.lower()

    def test_workflow_error_shows_original_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=1,
                stdout="",
                stderr=self._WORKFLOW_ERR,
            )
            main(["push", "origin", "feature/x"])
        err = capsys.readouterr().err
        assert "refusing to allow" in err

    def test_non_workflow_push_error_passes_through(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=1,
                stdout="",
                stderr="fatal: remote rejected\n",
            )
            rc = main(["push", "origin", "feature/x"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "fatal: remote rejected" in err
        assert "escalate" not in err.lower()

    def test_successful_push_unchanged(self) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=0,
                stdout="Everything up-to-date\n",
                stderr="",
            )
            rc = main(["push", "origin", "feature/x"])
        assert rc == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py::TestPushWorkflowErrorDetection -v`
Expected: FAIL — push does not capture output currently.

- [ ] **Step 3: Implement push error detection**

In `src/vergil_tooling/bin/vrg_git.py`, add the workflow error detection. Add the import at the top:

```python
from vergil_tooling.lib import identity
```

Add the error detection pattern and guidance function before `main()`:

```python
_WORKFLOW_PERMISSION_RE = re.compile(
    r"refusing to allow.*workflow.*without.*workflows.*permission",
    re.IGNORECASE,
)


def _print_workflow_push_guidance() -> None:
    mode = identity.current_mode()
    print(
        f"\nvrg-git: Push rejected — workflow file changes require elevated permissions.\n"
        f"  Your identity ({mode.value}) is not permitted to push workflow file changes.\n"
        f"  Stop and escalate to a human maintainer. Do not attempt to work around\n"
        f"  this failure (e.g., by removing workflow files from the commit).",
        file=sys.stderr,
    )
```

Add `import re` to the imports.

Then modify the push handling in `main()`. Replace the section that runs remote subcommands (the `if subcmd in _REMOTE_SUBCOMMANDS:` block and the final `subprocess.run`) with:

```python
    env = None
    if subcmd in _REMOTE_SUBCOMMANDS:
        token = github.get_installation_token()
        if token is not None:
            env = _git_auth_env(token)

    if subcmd == "push":
        result = subprocess.run(  # noqa: S603, S607
            ["git", *argv],
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            sys.stdout.write(result.stdout)
        if result.returncode != 0 and _WORKFLOW_PERMISSION_RE.search(result.stderr or ""):
            if result.stderr:
                sys.stderr.write(result.stderr)
            _print_workflow_push_guidance()
            return result.returncode
        if result.stderr:
            sys.stderr.write(result.stderr)
        return result.returncode

    result = subprocess.run(["git", *argv], check=False, env=env)  # noqa: S603, S607
    return result.returncode
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_git.py -v`
Expected: All tests PASS, including the new `TestPushWorkflowErrorDetection` tests and all existing tests.

Note: the existing `test_push_normal_allowed` test uses `mock_run.return_value.returncode = 0` which returns a `MagicMock` — it doesn't set `stdout`/`stderr`. The new push path accesses `result.stdout` and `result.stderr`, so the mock needs to return a proper `CompletedProcess`. Update that test:

```python
def test_push_normal_allowed() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
        rc = main(["push", "origin", "feature/foo"])
    assert rc == 0
```

Similarly update the `TestRemoteTokenInjection` tests for `push` to return `CompletedProcess` objects.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_git.py tests/vergil_tooling/test_vrg_git.py
vrg-commit --type feat --scope vrg-git --message "detect workflow permission errors on push with identity-aware guidance"
```

---

### Task 7: `vrg-finalize-pr` (merge consolidation + pre-merge provenance check)

This is not a rename — it is a consolidation. `vrg-finalize-repo` was
post-merge cleanup only (branch deletion, prune, validation, CD
check); the human merged the PR by hand on the web first. We collapse
both into one human tool, `vrg-finalize-pr`, and add a pre-merge
provenance check — the host-side "human chokepoint" verification from
the design spec that closes gaps GitHub's coarse permission model
cannot enforce server-side.

The tool has two modes, keyed on whether a PR argument is given:

- **`vrg-finalize-pr <PR>`** (human feature-PR path): run the pre-merge
  provenance check, then merge the PR (or confirm it is already
  merged), then run the existing cleanup. This replaces the manual web
  merge + `vrg-finalize-repo`.
- **`vrg-finalize-pr`** (no PR — release path, backward compatible):
  cleanup only, exactly as `vrg-finalize-repo` did today. The release
  workflow already merges programmatically, so it must not merge again;
  it calls this no-argument form for cleanup.

**Provenance check.** Fetch the PR's action history with read-only
`gh api` GET calls (running in the human context, where the API is
available) and verify no agent identity performed an action its role
forbids. Identities are recognized by account naming convention
(`*-vergil-user`, `*-vergil-audit`); everything else is human and
never forbidden. The audit identity must never have created, edited,
closed, reopened, or merged the PR; an audit *approval* is permitted
but surfaced as advisory. A fetch failure must abort the merge — never
silently pass (no silent failures).

**Files:**
- Create: `src/vergil_tooling/lib/pr_provenance.py`
- Create: `tests/vergil_tooling/test_pr_provenance.py`
- Create: `src/vergil_tooling/bin/vrg_finalize_pr.py`
- Modify: `src/vergil_tooling/bin/vrg_finalize_repo.py` (becomes deprecated alias)
- Create: `tests/vergil_tooling/test_vrg_finalize_pr.py`
- Modify: `tests/vergil_tooling/test_vrg_finalize_repo.py` (reduce to alias test)
- Modify: `src/vergil_tooling/lib/github.py` (add `pr_state` helper; update merge docstring)
- Modify: `src/vergil_tooling/lib/release/finalize.py` (update subprocess call)
- Modify: `tests/vergil_tooling/test_release_finalize.py:94` (update match string)
- Modify: `pyproject.toml:25` (add entry point)

- [ ] **Step 1: Write failing tests for `pr_provenance`**

Create `tests/vergil_tooling/test_pr_provenance.py`. These tests cover
the pure classification logic (`classify_login`, `evaluate`) and the
API-collection path (`check_pr`, with `github` mocked).

```python
"""Tests for vergil_tooling.lib.pr_provenance."""

from __future__ import annotations

import json
from unittest.mock import patch

from vergil_tooling.lib import pr_provenance
from vergil_tooling.lib.pr_provenance import Action, Role


def test_classify_login_user() -> None:
    assert pr_provenance.classify_login("alice-vergil-user") is Role.USER


def test_classify_login_audit() -> None:
    assert pr_provenance.classify_login("alice-vergil-audit") is Role.AUDIT


def test_classify_login_human() -> None:
    assert pr_provenance.classify_login("alice") is Role.HUMAN


def test_evaluate_human_actions_ignored() -> None:
    actions = [Action("alice", Role.HUMAN, "created"), Action("alice", Role.HUMAN, "merged")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert not result.violations
    assert not result.advisories


def test_evaluate_audit_approval_is_advisory() -> None:
    actions = [Action("a-vergil-audit", Role.AUDIT, "approved")]
    result = pr_provenance.evaluate(actions)
    assert result.ok
    assert len(result.advisories) == 1
    assert not result.violations


def test_evaluate_audit_close_is_violation() -> None:
    actions = [Action("a-vergil-audit", Role.AUDIT, "closed")]
    result = pr_provenance.evaluate(actions)
    assert not result.ok
    assert len(result.violations) == 1


def test_evaluate_user_approval_is_violation() -> None:
    actions = [Action("a-vergil-user", Role.USER, "approved")]
    result = pr_provenance.evaluate(actions)
    assert not result.ok
    assert len(result.violations) == 1


def test_check_pr_flags_audit_close() -> None:
    reviews = json.dumps([])
    timeline = json.dumps([{"event": "closed", "actor": {"login": "a-vergil-audit"}}])

    def fake_read_output(*args: str, **_: object) -> str:
        if args[0] == "api" and args[1].endswith("/reviews"):
            return reviews
        if args[0] == "api" and args[1].endswith("/timeline"):
            return timeline
        if args[:2] == ("pr", "view") and "number" in args:
            return "42"
        if args[:2] == ("pr", "view") and "author" in args:
            return "alice"  # human author
        raise AssertionError(f"unexpected call: {args}")

    with (
        patch("vergil_tooling.lib.pr_provenance.github.current_repo", return_value="o/r"),
        patch("vergil_tooling.lib.pr_provenance.github.read_output", side_effect=fake_read_output),
    ):
        result = pr_provenance.check_pr("42")
    assert not result.ok
    assert result.violations[0].action == "closed"
```

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_pr_provenance.py -v`
Expected: FAIL (module does not exist yet).

- [ ] **Step 2: Implement `pr_provenance`**

Create `src/vergil_tooling/lib/pr_provenance.py`:

```python
"""Pre-merge provenance verification for vrg-finalize-pr.

Fetches a PR's action history (reviews and timeline events) from
GitHub and verifies that no agent identity performed an action its
role forbids on the PR being merged. Agent identities are recognized
by the account naming convention (``*-vergil-user`` and
``*-vergil-audit``); everything else is treated as human and is never
forbidden — the human holds every right.

This is the host-side "human chokepoint" verification described in
docs/specs/2026-05-29-agent-permission-model-design.md: it closes the
gap the coarse GitHub permission model cannot enforce server-side
(notably the audit identity's ``pull_requests: write`` scope, which
GitHub cannot narrow to "review but never author/close/merge").

Read-only ``gh api`` GET calls back this check. They run in the human
context where the API is available; the same endpoints are reachable
from the audit context under the identity-aware API allowance. A fetch
failure propagates (``github.read_output`` raises on nonzero exit) so a
broken check aborts the merge rather than silently passing.
"""

from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field

from vergil_tooling.lib import github


class Role(enum.Enum):
    HUMAN = "human"
    USER = "user"
    AUDIT = "audit"


def classify_login(login: str) -> Role:
    """Map a GitHub login to an identity role by naming convention."""
    if login.endswith("-vergil-audit"):
        return Role.AUDIT
    if login.endswith("-vergil-user"):
        return Role.USER
    return Role.HUMAN


# Actions an agent role must never perform on a PR it is merging.
_FORBIDDEN: dict[Role, frozenset[str]] = {
    Role.USER: frozenset({"created", "edited", "closed", "reopened", "merged", "approved"}),
    Role.AUDIT: frozenset({"created", "edited", "closed", "reopened", "merged"}),
}

# Actions permitted but advisory — surfaced to the human, not blocked.
_ADVISORY: dict[Role, frozenset[str]] = {
    Role.AUDIT: frozenset({"approved"}),
}


@dataclass(frozen=True)
class Action:
    """A single agent-attributed action on the PR."""

    login: str
    role: Role
    action: str


@dataclass
class ProvenanceResult:
    violations: list[Action] = field(default_factory=list)
    advisories: list[Action] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


_EVENT_MAP = {
    "closed": "closed",
    "reopened": "reopened",
    "merged": "merged",
    "renamed": "edited",
}


def _collect_actions(pr: str) -> list[Action]:
    """Collect agent-attributed actions on *pr* from GitHub."""
    repo = github.current_repo()
    number = github.read_output("pr", "view", pr, "--json", "number", "--jq", ".number").strip()
    actions: list[Action] = []

    author = github.read_output(
        "pr", "view", pr, "--json", "author", "--jq", ".author.login"
    ).strip()
    if author:
        actions.append(Action(author, classify_login(author), "created"))

    reviews_raw = github.read_output("api", f"repos/{repo}/pulls/{number}/reviews")
    for review in json.loads(reviews_raw or "[]"):
        if review.get("state") == "APPROVED":
            login = (review.get("user") or {}).get("login", "")
            if login:
                actions.append(Action(login, classify_login(login), "approved"))

    timeline_raw = github.read_output("api", f"repos/{repo}/issues/{number}/timeline")
    for event in json.loads(timeline_raw or "[]"):
        mapped = _EVENT_MAP.get(event.get("event", ""))
        if mapped is None:
            continue
        login = (event.get("actor") or {}).get("login", "")
        if login:
            actions.append(Action(login, classify_login(login), mapped))

    return actions


def evaluate(actions: list[Action]) -> ProvenanceResult:
    """Partition *actions* into violations and advisories. Pure."""
    result = ProvenanceResult()
    for act in actions:
        if act.role is Role.HUMAN:
            continue
        if act.action in _ADVISORY.get(act.role, frozenset()):
            result.advisories.append(act)
        elif act.action in _FORBIDDEN.get(act.role, frozenset()):
            result.violations.append(act)
    return result


def check_pr(pr: str) -> ProvenanceResult:
    """Verify PR provenance by fetching and evaluating its action history."""
    return evaluate(_collect_actions(pr))
```

- [ ] **Step 3: Run `pr_provenance` tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_pr_provenance.py -v`
Expected: All tests PASS.

- [ ] **Step 4: Add `pr_state` helper and update merge docstring in `github.py`**

In `src/vergil_tooling/lib/github.py`, add a small helper near `merge`:

```python
def pr_state(pr: str) -> str:
    """Return the PR state: ``OPEN``, ``CLOSED``, or ``MERGED``."""
    return read_output("pr", "view", pr, "--json", "state", "--jq", ".state")
```

And update the `merge` docstring (was `vrg-finalize-repo`):

```python
    Does not pass ``--delete-branch`` — branch cleanup is handled by
    ``vrg-finalize-pr`` after the merge completes.
```

- [ ] **Step 5: Create `vrg_finalize_pr.py` from `vrg_finalize_repo.py`**

Copy `src/vergil_tooling/bin/vrg_finalize_repo.py` to `src/vergil_tooling/bin/vrg_finalize_pr.py`. Then apply the changes below.

Update the module docstring (first line): `"""Finalize a pull request: provenance check, merge, and cleanup."""`

Update the error message at line 151: change `vrg-finalize-repo` to `vrg-finalize-pr`:

```python
        print(
            f"ERROR: vrg-finalize-pr must be run from the main worktree at {main_root},\n"
            "  not from a secondary worktree. The script removes worktrees during cleanup\n"
            "  and cannot safely do so when the calling shell's CWD is inside one.",
            file=sys.stderr,
        )
```

Add the new imports at the top alongside the existing ones:

```python
from vergil_tooling.lib import config, git, github, pr_provenance
```

Extend `parse_args` with the PR argument and merge controls:

```python
    parser.add_argument(
        "pr",
        nargs="?",
        default=None,
        help="PR number or URL to merge and finalize. Omit for cleanup-only (release path).",
    )
    parser.add_argument(
        "--strategy",
        default="squash",
        choices=["merge", "squash", "rebase"],
        help="Merge strategy for the PR (feature PRs default to squash)",
    )
    parser.add_argument(
        "--allow-provenance-violation",
        action="store_true",
        help="Proceed despite provenance violations (conscious human override)",
    )
```

Add the provenance-check-and-merge helper:

```python
def _finalize_specific_pr(args: argparse.Namespace) -> int:
    """Run the pre-merge provenance check, then merge (or confirm merged).

    Returns 0 to continue to cleanup, nonzero to abort.
    """
    print(f"Checking provenance for PR {args.pr}...")
    result = pr_provenance.check_pr(args.pr)

    for adv in result.advisories:
        print(
            f"  ADVISORY: {adv.login} ({adv.role.value}) performed '{adv.action}' "
            "— permitted but advisory.",
            file=sys.stderr,
        )

    if result.violations:
        print(f"ERROR: PR {args.pr} has provenance violations:", file=sys.stderr)
        for v in result.violations:
            print(
                f"  {v.login} ({v.role.value}) performed forbidden action '{v.action}'.",
                file=sys.stderr,
            )
        if not args.allow_provenance_violation:
            print(
                "\n  Aborting merge. Re-run with --allow-provenance-violation to\n"
                "  override consciously — you hold every right, but the violation\n"
                "  is in front of you.",
                file=sys.stderr,
            )
            return 1
        print(
            "  Overriding provenance violations per --allow-provenance-violation.",
            file=sys.stderr,
        )

    if github.pr_state(args.pr) == "MERGED":
        print(f"PR {args.pr} already merged.")
    elif args.dry_run:
        print(f"  [dry-run] merge PR {args.pr} (--{args.strategy})")
    else:
        print(f"Merging PR {args.pr} (--{args.strategy})...")
        github.merge(args.pr, strategy=args.strategy)

    return 0
```

Wire it into `main`, immediately after the main-worktree guard and before the cleanup flow (i.e., right after `root = git.repo_root()`):

```python
    if args.pr is not None:
        rc = _finalize_specific_pr(args)
        if rc != 0:
            return rc
```

The existing cleanup flow (switch branch, pull, delete merged branches, prune, validation, CD check) runs unchanged afterward in both modes.

- [ ] **Step 6: Write failing tests for `vrg_finalize_pr` merge + provenance integration**

Create `tests/vergil_tooling/test_vrg_finalize_pr.py` by copying
`tests/vergil_tooling/test_vrg_finalize_repo.py` and applying these
replacements throughout:

| Old | New |
|---|---|
| `vergil_tooling.bin.vrg_finalize_repo` | `vergil_tooling.bin.vrg_finalize_pr` |
| `_MOD = "vergil_tooling.bin.vrg_finalize_repo"` | `_MOD = "vergil_tooling.bin.vrg_finalize_pr"` |
| `from vergil_tooling.bin.vrg_finalize_repo import` | `from vergil_tooling.bin.vrg_finalize_pr import` |
| `"""Tests for vergil_tooling.bin.vrg_finalize_repo."""` | `"""Tests for vergil_tooling.bin.vrg_finalize_pr."""` |

The copied tests exercise the no-PR cleanup-only path (backward
compatibility — they pass no positional argument, so `args.pr` is
`None` and the merge path is skipped). Then add the merge + provenance
tests below.

```python
from vergil_tooling.lib.pr_provenance import Action, ProvenanceResult, Role


def _clean() -> ProvenanceResult:
    return ProvenanceResult()


def _with_violation() -> ProvenanceResult:
    return ProvenanceResult(violations=[Action("a-vergil-audit", Role.AUDIT, "closed")])


def _with_advisory() -> ProvenanceResult:
    return ProvenanceResult(advisories=[Action("a-vergil-audit", Role.AUDIT, "approved")])


def test_pr_arg_runs_provenance_then_merges() -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()) as mock_check,
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value="/tmp/repo"),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
        result = main(["42"])
    assert result == 0
    mock_check.assert_called_once_with("42")
    mock_merge.assert_called_once()


def test_provenance_violation_aborts_without_merge(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_with_violation()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["42"])
    assert result == 1
    mock_merge.assert_not_called()
    err = capsys.readouterr().err
    assert "provenance violation" in err.lower()
    assert "closed" in err


def test_provenance_violation_override_merges() -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_with_violation()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value="/tmp/repo"),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
        result = main(["42", "--allow-provenance-violation"])
    assert result == 0
    mock_merge.assert_called_once()


def test_advisory_surfaced_and_merge_proceeds(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_with_advisory()),
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value="/tmp/repo"),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
        result = main(["42"])
    assert result == 0
    mock_merge.assert_called_once()
    assert "advisory" in capsys.readouterr().err.lower()


def test_already_merged_skips_merge() -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr", return_value=_clean()),
        patch(f"{_MOD}.github.pr_state", return_value="MERGED"),
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value="/tmp/repo"),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
        result = main(["42"])
    assert result == 0
    mock_merge.assert_not_called()


def test_no_pr_arg_is_cleanup_only() -> None:
    with (
        patch(f"{_MOD}.pr_provenance.check_pr") as mock_check,
        patch(f"{_MOD}.github.merge") as mock_merge,
        patch(f"{_MOD}.git.current_branch", return_value="develop"),
        patch(f"{_MOD}.git.merged_branches", return_value=[]),
        patch(f"{_MOD}.git.run"),
        patch(f"{_MOD}.git.working_tree_status", return_value=""),
        patch(f"{_MOD}.git.repo_root", return_value="/tmp/repo"),
        patch(f"{_MOD}.config.read_config", side_effect=FileNotFoundError),
        patch(f"{_MOD}.subprocess.run") as mock_sub,
    ):
        mock_sub.return_value.returncode = 0
        result = main([])
    assert result == 0
    mock_check.assert_not_called()
    mock_merge.assert_not_called()
```

Note: the copied tests assume the autouse `is_main_worktree` fixture
from the original file is preserved. If the original file lacks one,
add `patch(f"{_MOD}.git.is_main_worktree", return_value=True)` to each
new test (or as an autouse fixture).

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v`
Expected: FAIL (merge/provenance wiring not yet present, or module missing).

- [ ] **Step 7: Run `vrg_finalize_pr` tests**

After Step 5's implementation is in place, run:
`vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v`
Expected: All tests PASS.

- [ ] **Step 8: Replace `vrg_finalize_repo.py` with deprecated alias**

Replace the contents of `src/vergil_tooling/bin/vrg_finalize_repo.py` with:

```python
"""Deprecated alias for vrg-finalize-pr.

This module exists for backward compatibility during the 2.0→2.1
transition. Use ``vrg-finalize-pr`` instead.
"""

from __future__ import annotations

import sys

from vergil_tooling.bin.vrg_finalize_pr import main as _main


def main(argv: list[str] | None = None) -> int:
    print(
        "WARNING: vrg-finalize-repo is deprecated. Use vrg-finalize-pr instead.",
        file=sys.stderr,
    )
    return _main(argv)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 9: Reduce `test_vrg_finalize_repo.py` to a deprecated-alias test**

Replace `tests/vergil_tooling/test_vrg_finalize_repo.py` with:

```python
"""Tests for the deprecated vrg-finalize-repo alias."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_finalize_repo import main

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _main_worktree() -> Iterator[None]:
    with patch("vergil_tooling.bin.vrg_finalize_pr.git.is_main_worktree", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _clean_working_tree() -> Iterator[None]:
    with patch("vergil_tooling.bin.vrg_finalize_pr.git.working_tree_status", return_value=""):
        yield


def test_deprecated_alias_prints_warning(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_finalize_pr.git.repo_root", return_value="/tmp/repo"),
        patch("vergil_tooling.bin.vrg_finalize_pr.config.read_config") as mock_config,
        patch("vergil_tooling.bin.vrg_finalize_pr.git.current_branch", return_value="develop"),
        patch("vergil_tooling.bin.vrg_finalize_pr.git.merged_branches", return_value=[]),
        patch("vergil_tooling.bin.vrg_finalize_pr.git.run"),
        patch("vergil_tooling.bin.vrg_finalize_pr.subprocess.run") as mock_sub,
    ):
        mock_config.side_effect = FileNotFoundError
        mock_sub.return_value.returncode = 0
        result = main(["--dry-run"])
    assert result == 0
    err = capsys.readouterr().err
    assert "deprecated" in err.lower()
    assert "vrg-finalize-pr" in err
```

- [ ] **Step 10: Update `release/finalize.py`**

In `src/vergil_tooling/lib/release/finalize.py`, update all references
(the release path still calls the no-argument cleanup-only form):

Line 1: `"""Phase 5: Close tracking issue and run vrg-finalize-pr."""`

Line 21: `print("Running vrg-finalize-pr...")`

Line 23: `("vrg-finalize-pr",),  # noqa: S607`

Line 33: `command="vrg-finalize-pr",`

Line 34: `message="vrg-finalize-pr failed.",`

- [ ] **Step 11: Update `test_release_finalize.py`**

In `tests/vergil_tooling/test_release_finalize.py` at line 94, change the match string:

```python
        pytest.raises(ReleaseError, match="vrg-finalize-pr"),
```

- [ ] **Step 12: Add `vrg-finalize-pr` entry point to `pyproject.toml`**

In `pyproject.toml`, add the new entry point after the existing `vrg-finalize-repo` line:

```toml
vrg-finalize-pr = "vergil_tooling.bin.vrg_finalize_pr:main"
vrg-finalize-repo = "vergil_tooling.bin.vrg_finalize_repo:main"
```

- [ ] **Step 13: Run all affected tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_pr_provenance.py tests/vergil_tooling/test_vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_repo.py tests/vergil_tooling/test_release_finalize.py -v`
Expected: All tests PASS.

- [ ] **Step 14: Commit**

```bash
vrg-git add \
  src/vergil_tooling/lib/pr_provenance.py \
  src/vergil_tooling/bin/vrg_finalize_pr.py \
  src/vergil_tooling/bin/vrg_finalize_repo.py \
  src/vergil_tooling/lib/release/finalize.py \
  src/vergil_tooling/lib/github.py \
  tests/vergil_tooling/test_pr_provenance.py \
  tests/vergil_tooling/test_vrg_finalize_pr.py \
  tests/vergil_tooling/test_vrg_finalize_repo.py \
  tests/vergil_tooling/test_release_finalize.py \
  pyproject.toml
vrg-commit --type feat --scope finalize --message "consolidate merge + cleanup into vrg-finalize-pr with pre-merge provenance check"
```

---

### Task 8: Full Validation

- [ ] **Step 1: Run full validation**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: All checks pass (lint, typecheck, tests, audit).

- [ ] **Step 2: Fix any issues**

If validation fails, fix the issues and re-run until clean.

- [ ] **Step 3: Commit any fixes**

If there were fixes, commit them:

```bash
vrg-commit --type fix --scope validation --message "fix validation issues from permission model implementation"
```
