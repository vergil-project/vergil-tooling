"""Tests for standard_tooling.lib.config."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from standard_tooling.lib.config import (
    CiConfig,
    ConfigError,
    MarkdownlintConfig,
    read_config,
    st_install_tag,
)

if TYPE_CHECKING:
    from pathlib import Path


# -- st_install_tag -----------------------------------------------------------

_INSTALL_TAG_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
standard-tooling = "v1.4"
"""


def test_tag_from_config(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_INSTALL_TAG_TOML)
    with patch.dict("os.environ", {}, clear=True):
        assert st_install_tag(tmp_path) == "v1.4"


def test_tag_missing_file(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(FileNotFoundError),
    ):
        st_install_tag(tmp_path)


def test_env_override(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_INSTALL_TAG_TOML)
    with patch.dict("os.environ", {"ST_DOCKER_INSTALL_TAG": "v2.0"}, clear=True):
        assert st_install_tag(tmp_path) == "v2.0"


def test_env_override_skips_file_read(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"ST_DOCKER_INSTALL_TAG": "v2.0"}, clear=True):
        assert st_install_tag(tmp_path) == "v2.0"


# -- read_config (standard-tooling.toml) --------------------------------------

_VALID_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[project.co-authors]
claude = "Co-Authored-By: user-claude <111+user-claude@users.noreply.github.com>"

[dependencies]
standard-tooling = "v1.4"
"""


def test_read_config_valid(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.project.repository_type == "library"
    assert cfg.project.versioning_scheme == "semver"
    assert cfg.project.branching_model == "library-release"
    assert cfg.project.release_model == "tagged-release"
    assert cfg.project.primary_language == "python"
    assert "claude" in cfg.project.co_authors
    assert "user-claude" in cfg.project.co_authors["claude"]
    assert cfg.dependencies["standard-tooling"] == "v1.4"


def test_read_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="standard-tooling.toml"):
        read_config(tmp_path)


def test_read_config_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text("[invalid\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        read_config(tmp_path)


def test_read_config_missing_project_field(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('primary-language = "python"\n', "")
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="primary-language"):
        read_config(tmp_path)


def test_read_config_invalid_enum(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('"library"', '"banana"')
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="repository-type.*banana"):
        read_config(tmp_path)


def test_read_config_malformed_co_author(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace(
        'claude = "Co-Authored-By: user-claude <111+user-claude@users.noreply.github.com>"',
        'claude = "not a valid trailer"',
    )
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="co-author.*claude"):
        read_config(tmp_path)


def test_read_config_missing_dependencies_key(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('standard-tooling = "v1.4"', 'other = "v1.0"')
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="standard-tooling"):
        read_config(tmp_path)


def test_read_config_no_co_authors(tmp_path: Path) -> None:
    lines = [
        ln
        for ln in _VALID_TOML.splitlines(keepends=True)
        if "co-authors" not in ln.lower() and "claude" not in ln
    ]
    (tmp_path / "standard-tooling.toml").write_text("".join(lines))
    cfg = read_config(tmp_path)
    assert cfg.project.co_authors == {}


# -- [markdownlint] section ---------------------------------------------------

_ML_IGNORE_TOML = (
    _VALID_TOML
    + """
[markdownlint]
ignore = ["docs/site/docs/research"]
"""
)


def test_read_config_markdownlint_ignore(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_ML_IGNORE_TOML)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint == MarkdownlintConfig(ignore=["docs/site/docs/research"])


def test_read_config_markdownlint_multiple_ignores(tmp_path: Path) -> None:
    toml = (
        _VALID_TOML
        + '[markdownlint]\nignore = ["docs/site/docs/research", "docs/site/docs/archive"]\n'
    )
    (tmp_path / "standard-tooling.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint.ignore == [
        "docs/site/docs/research",
        "docs/site/docs/archive",
    ]


def test_read_config_no_markdownlint_section(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint == MarkdownlintConfig(ignore=[])


def test_read_config_markdownlint_empty_ignore(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[markdownlint]\nignore = []\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint.ignore == []


def test_read_config_markdownlint_no_ignore_key(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[markdownlint]\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint.ignore == []


def test_read_config_markdownlint_ignore_not_a_list(tmp_path: Path) -> None:
    toml = _VALID_TOML + '[markdownlint]\nignore = "not-a-list"\n'
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[markdownlint\]\.ignore must be a list"):
        read_config(tmp_path)


# -- [ci] section --------------------------------------------------------------

_CI_TOML = (
    _VALID_TOML
    + """
[ci]
versions = ["3.12", "3.13", "3.14"]
integration-tests = true
"""
)


def test_read_config_ci_section(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_CI_TOML)
    cfg = read_config(tmp_path)
    assert cfg.ci == CiConfig(versions=["3.12", "3.13", "3.14"], integration_tests=True)


def test_read_config_ci_no_integration_tests(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nversions = [\"3.14\"]\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.ci is not None
    assert cfg.ci.integration_tests is False


def test_read_config_ci_missing_versions(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nintegration-tests = true\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions"):
        read_config(tmp_path)


def test_read_config_ci_empty_versions(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nversions = []\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions.*at least one"):
        read_config(tmp_path)


def test_read_config_ci_versions_not_strings(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[ci]\nversions = [3.12, 3.13]\n"
    (tmp_path / "standard-tooling.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions.*strings"):
        read_config(tmp_path)


def test_read_config_no_ci_section(tmp_path: Path) -> None:
    (tmp_path / "standard-tooling.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.ci is None
