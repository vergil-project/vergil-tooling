"""Tests for vergil_tooling.lib.config."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.config import (
    CiConfig,
    ConfigError,
    MarkdownlintConfig,
    read_config,
    vrg_install_tag,
)

if TYPE_CHECKING:
    from pathlib import Path


# -- vrg_install_tag -----------------------------------------------------------

_INSTALL_TAG_TOML = """\
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


def test_tag_from_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_INSTALL_TAG_TOML)
    with patch.dict("os.environ", {}, clear=True):
        assert vrg_install_tag(tmp_path) == "v2.0"


def test_tag_missing_file(tmp_path: Path) -> None:
    with (
        patch.dict("os.environ", {}, clear=True),
        pytest.raises(FileNotFoundError),
    ):
        vrg_install_tag(tmp_path)


def test_env_override(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_INSTALL_TAG_TOML)
    with patch.dict("os.environ", {"VRG_DOCKER_INSTALL_TAG": "v2.0"}, clear=True):
        assert vrg_install_tag(tmp_path) == "v2.0"


def test_env_override_skips_file_read(tmp_path: Path) -> None:
    with patch.dict("os.environ", {"VRG_DOCKER_INSTALL_TAG": "v2.0"}, clear=True):
        assert vrg_install_tag(tmp_path) == "v2.0"


# -- read_config (vergil.toml) --------------------------------------

_BASE_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"
"""

_VALID_TOML = _BASE_TOML + '\n[ci]\nversions = ["3.14"]\n'


def test_read_config_valid(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.project.repository_type == "library"
    assert cfg.project.versioning_scheme == "semver"
    assert cfg.project.branching_model == "library-release"
    assert cfg.project.release_model == "tagged-release"
    assert cfg.project.primary_language == "python"
    assert cfg.dependencies["vergil"] == "v2.0"


def test_read_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="vergil.toml"):
        read_config(tmp_path)


def test_read_config_invalid_toml(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[invalid\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        read_config(tmp_path)


def test_read_config_missing_project_field(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('primary-language = "python"\n', "")
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match="primary-language"):
        read_config(tmp_path)


def test_read_config_invalid_enum(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('"library"', '"banana"')
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match="repository-type.*banana"):
        read_config(tmp_path)


def test_read_config_missing_dependencies_key(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('vergil = "v2.0"', 'other = "v1.0"')
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"must contain 'vergil'"):
        read_config(tmp_path)


def test_read_config_ignores_leftover_co_authors(tmp_path: Path) -> None:
    co_authors = (
        '\n[project.co-authors]\nagent = "Co-Authored-By: x <1+x@users.noreply.github.com>"\n'
    )
    (tmp_path / "vergil.toml").write_text(_VALID_TOML + co_authors)
    cfg = read_config(tmp_path)
    assert not hasattr(cfg.project, "co_authors")


# -- [markdownlint] section ---------------------------------------------------

_ML_IGNORE_TOML = (
    _VALID_TOML
    + """
[markdownlint]
ignore = ["docs/site/docs/research"]
"""
)


def test_read_config_markdownlint_ignore(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_ML_IGNORE_TOML)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint == MarkdownlintConfig(ignore=["docs/site/docs/research"])


def test_read_config_markdownlint_multiple_ignores(tmp_path: Path) -> None:
    toml = (
        _VALID_TOML
        + '[markdownlint]\nignore = ["docs/site/docs/research", "docs/site/docs/archive"]\n'
    )
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint.ignore == [
        "docs/site/docs/research",
        "docs/site/docs/archive",
    ]


def test_read_config_no_markdownlint_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint == MarkdownlintConfig(ignore=[])


def test_read_config_markdownlint_empty_ignore(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[markdownlint]\nignore = []\n"
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint.ignore == []


def test_read_config_markdownlint_no_ignore_key(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[markdownlint]\n"
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.markdownlint.ignore == []


def test_read_config_markdownlint_ignore_not_a_list(tmp_path: Path) -> None:
    toml = _VALID_TOML + '[markdownlint]\nignore = "not-a-list"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[markdownlint\]\.ignore must be a list"):
        read_config(tmp_path)


# -- [ci] section --------------------------------------------------------------

_CI_TOML = (
    _BASE_TOML
    + """
[ci]
versions = ["3.12", "3.13", "3.14"]
integration-tests = true
"""
)


def test_read_config_ci_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_CI_TOML)
    cfg = read_config(tmp_path)
    assert cfg.ci == CiConfig(versions=["3.12", "3.13", "3.14"], integration_tests=True)


def test_read_config_ci_no_integration_tests(tmp_path: Path) -> None:
    toml = _BASE_TOML + '[ci]\nversions = ["3.14"]\n'
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.ci is not None
    assert cfg.ci.integration_tests is False


def test_read_config_ci_missing_versions(tmp_path: Path) -> None:
    toml = _BASE_TOML + "[ci]\nintegration-tests = true\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions"):
        read_config(tmp_path)


def test_read_config_ci_empty_versions(tmp_path: Path) -> None:
    toml = _BASE_TOML + "[ci]\nversions = []\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions.*at least one"):
        read_config(tmp_path)


def test_read_config_ci_versions_not_strings(tmp_path: Path) -> None:
    toml = _BASE_TOML + "[ci]\nversions = [3.12, 3.13]\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match="versions.*strings"):
        read_config(tmp_path)


def test_read_config_no_ci_section_raises(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_BASE_TOML)
    with pytest.raises(ConfigError, match=r"missing required section \[ci\]"):
        read_config(tmp_path)


# -- [publish] section --------------------------------------------------------

_PUBLISH_TOML = """\
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

[publish]
release = true
docs = true
"""


def test_publish_section_parsed(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_PUBLISH_TOML)
    cfg = read_config(tmp_path)
    assert cfg.publish is not None
    assert cfg.publish.release is True
    assert cfg.publish.docs is True


def test_publish_section_defaults_when_absent(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_INSTALL_TAG_TOML)
    cfg = read_config(tmp_path)
    assert cfg.publish is not None
    assert cfg.publish.release is False
    assert cfg.publish.docs is True


_PUBLISH_RELEASE_ONLY_TOML = """\
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

[publish]
release = true
"""


def test_publish_docs_defaults_true(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_PUBLISH_RELEASE_ONLY_TOML)
    cfg = read_config(tmp_path)
    assert cfg.publish.docs is True


def test_publish_consumer_refresh(tmp_path: Path) -> None:
    toml = _VALID_TOML + '\n[publish]\nconsumer-refresh = "uv tool install pkg@v<VERSION>"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.publish.consumer_refresh == "uv tool install pkg@v<VERSION>"


def test_publish_consumer_refresh_default_none(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.publish.consumer_refresh is None
