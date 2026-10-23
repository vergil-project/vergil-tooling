"""Tests for the canonical report-only ``dependabot.yml`` renderer (epic .github#420)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib import dependabot
from vergil_tooling.lib.config import ConfigError

# A ``[package]`` overlay that fails to load because no ``uv.lock`` sits beside
# the ``vergil.toml`` it is appended to (the #3451 repro).
PACKAGE_PYTHON_NO_LOCK = """
[package]
builder = "python"
vendor = "vergil"
summary = "test package"
smoke = "true"
"""

_PYTHON_CANONICAL = """\
# Managed by vergil-tooling (epic vergil-project/.github#420). Do not edit.
# Report-only: Dependabot alerts are on; Dependabot never opens PRs here.
# open-pull-requests-limit: 0 on every entry is enforced by vrg-validate
# and the nightly repo-config audit.
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 0
  - package-ecosystem: "uv"
    directory: "/"
    schedule:
      interval: "weekly"
    open-pull-requests-limit: 0
"""

_TOML_TEMPLATE = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
{lang_line}
[ci]
versions = ["3.12"]

[dependencies]
vergil = "v2.0.7"
"""


def test_dependabot_path() -> None:
    assert Path(".github/dependabot.yml") == dependabot.DEPENDABOT_PATH


def test_language_ecosystems_mapping() -> None:
    assert dependabot.LANGUAGE_ECOSYSTEMS == {
        "python": "uv",
        "go": "gomod",
        "rust": "cargo",
        "java": "maven",
        "typescript": "npm",
    }


# --- render ---------------------------------------------------------------


def test_render_python_exact() -> None:
    assert dependabot.render("python") == _PYTHON_CANONICAL


@pytest.mark.parametrize("lang", [None, "cpp", "shell", ""])
def test_render_no_language_actions_only(lang: str | None) -> None:
    text = dependabot.render(lang)
    assert text.count("package-ecosystem:") == 1
    assert 'package-ecosystem: "github-actions"' in text
    assert dependabot.limit_violations(text) == []


@pytest.mark.parametrize(("lang", "ecosystem"), sorted(dependabot.LANGUAGE_ECOSYSTEMS.items()))
def test_render_every_mapped_language(lang: str, ecosystem: str) -> None:
    text = dependabot.render(lang)
    assert dependabot.limit_violations(text) == []
    assert f'package-ecosystem: "{ecosystem}"' in text
    assert text.count("\n    open-pull-requests-limit: 0\n") == 2


def test_render_is_stable() -> None:
    assert dependabot.render("go") == dependabot.render("go")


# --- check ----------------------------------------------------------------


def test_check_missing() -> None:
    assert dependabot.check(None, "python") == ["missing .github/dependabot.yml"]


def test_check_differs() -> None:
    text = dependabot.render("python") + "# a local addition\n"
    assert dependabot.check(text, "python") == ["differs from the canonical render"]


def test_check_differs_wrong_language() -> None:
    assert dependabot.check(dependabot.render("go"), "python") == [
        "differs from the canonical render"
    ]


def test_check_canonical() -> None:
    assert dependabot.check(dependabot.render("python"), "python") == []


# --- limit_violations -----------------------------------------------------


def test_dependabot_guard_missing_limit_fails() -> None:
    text = """\
version: 2
updates:
  - package-ecosystem: "pip"
    directory: "/"
    schedule:
      interval: "weekly"
"""
    violations = dependabot.limit_violations(text)
    assert len(violations) == 1
    assert "pip" in violations[0]
    assert "missing open-pull-requests-limit" in violations[0]


def test_limit_nonzero_fails() -> None:
    text = """\
version: 2
updates:
  - package-ecosystem: "github-actions"
    directory: "/"
    open-pull-requests-limit: 0
  - package-ecosystem: "npm"
    directory: "/"
    open-pull-requests-limit: 5
"""
    violations = dependabot.limit_violations(text)
    assert len(violations) == 1
    assert "npm" in violations[0]
    assert "5" in violations[0]


def test_limit_boolean_false_is_not_zero() -> None:
    """``false`` is not the integer 0 — it must not slip through as compliant."""
    text = "version: 2\nupdates:\n  - package-ecosystem: npm\n    open-pull-requests-limit: false\n"
    assert len(dependabot.limit_violations(text)) == 1


def test_limit_string_zero_is_not_zero() -> None:
    text = 'version: 2\nupdates:\n  - package-ecosystem: npm\n    open-pull-requests-limit: "0"\n'
    assert len(dependabot.limit_violations(text)) == 1


def test_limit_entry_without_ecosystem_named_unknown() -> None:
    text = "version: 2\nupdates:\n  - directory: /\n"
    assert dependabot.limit_violations(text) == [
        "updates entry <unknown ecosystem> is missing open-pull-requests-limit "
        "(Dependabot defaults to 5); it must be 0"
    ]


def test_limit_unparseable() -> None:
    violations = dependabot.limit_violations("updates: [\n")
    assert len(violations) == 1
    assert violations[0].startswith("unparseable: ")


@pytest.mark.parametrize(
    "text",
    [
        "- just a list\n",
        "version: 2\nupdates: not-a-list\n",
        "version: 2\nupdates:\n  - not-a-mapping\n",
    ],
)
def test_limit_malformed_structure(text: str) -> None:
    violations = dependabot.limit_violations(text)
    assert len(violations) == 1
    assert violations[0].startswith("unparseable: ")


def test_limit_no_updates_key_is_compliant() -> None:
    """A file with no ``updates`` can open no PRs."""
    assert dependabot.limit_violations("version: 2\n") == []


def test_limit_empty_file_is_compliant() -> None:
    assert dependabot.limit_violations("") == []


# --- resolve_language -----------------------------------------------------


def _write_toml(root: Path, lang: str | None) -> None:
    lang_line = f'primary-language = "{lang}"\n' if lang else ""
    (root / "vergil.toml").write_text(_TOML_TEMPLATE.format(lang_line=lang_line), encoding="utf-8")


def test_resolve_language_declared(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    assert dependabot.resolve_language(tmp_path) == "python"


def test_resolve_language_unmapped_passthrough(tmp_path: Path) -> None:
    """An unmapped language is returned as-is; ``render`` maps it to actions-only."""
    _write_toml(tmp_path, "cpp")
    assert dependabot.resolve_language(tmp_path) == "cpp"


def test_resolve_language_undeclared(tmp_path: Path) -> None:
    _write_toml(tmp_path, None)
    assert dependabot.resolve_language(tmp_path) is None


def test_resolve_language_config_error_propagates(tmp_path: Path) -> None:
    """A vergil.toml that fails to load is loud, never an actions-only ``None`` (#3451)."""
    (tmp_path / "vergil.toml").write_text("not = [valid toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid TOML"):
        dependabot.resolve_language(tmp_path)


def test_resolve_language_package_overlay_error_propagates(tmp_path: Path) -> None:
    """The #3451 repro: ``builder = "python"`` with no ``uv.lock`` must not drop ``uv``."""
    _write_toml(tmp_path, "python")
    with (tmp_path / "vergil.toml").open("a", encoding="utf-8") as f:
        f.write(PACKAGE_PYTHON_NO_LOCK)
    with pytest.raises(ConfigError, match="requires a uv.lock"):
        dependabot.resolve_language(tmp_path)


def test_resolve_language_no_config(tmp_path: Path) -> None:
    assert dependabot.resolve_language(tmp_path) is None
