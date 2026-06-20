"""Tests for vergil_tooling.lib.config."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.config import (
    DEFAULT_VALIDATION_COMMAND,
    CiConfig,
    ConfigError,
    ContainerConfig,
    MarkdownlintConfig,
    ValidationConfig,
    VmStanza,
    _warn_unrecognized_keys,
    container_env_prefixes,
    parse_vm_stanza,
    read_config,
    validation_container_command,
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


def test_read_config_missing_required_project_field(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('release-model = "tagged-release"\n', "")
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match="release-model"):
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


# -- optional primary-language -------------------------------------------------


def test_config_without_primary_language(tmp_path: Path) -> None:
    """Repos with no toolchain can omit primary-language."""
    toml = (
        "[project]\n"
        'repository-type = "infrastructure"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        "\n[dependencies]\n"
        'vergil = "v2.0.60"\n'
        '\n[ci]\nversions = ["3.12"]\n'
    )
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.project.primary_language is None


def test_config_warns_on_shell_language(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    toml = _VALID_TOML.replace('primary-language = "python"', 'primary-language = "shell"')
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.project.primary_language is None
    assert "unrecognized primary-language 'shell'" in capsys.readouterr().err


def test_config_warns_on_none_language(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    toml = _VALID_TOML.replace('primary-language = "python"', 'primary-language = "none"')
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.project.primary_language is None
    assert "unrecognized primary-language 'none'" in capsys.readouterr().err


def test_config_warns_on_claude_plugin_language(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML.replace('primary-language = "python"', 'primary-language = "claude-plugin"')
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.project.primary_language is None
    assert "unrecognized primary-language 'claude-plugin'" in capsys.readouterr().err


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


# -- unrecognized-key warnings ------------------------------------------------

_EXTRA_PROJECT_KEY_TOML = (
    "[project]\n"
    'repository-type = "library"\n'
    'versioning-scheme = "semver"\n'
    'branching-model = "library-release"\n'
    'release-model = "tagged-release"\n'
    'primary-language = "python"\n'
    'version-file = "custom/VERSION"\n'
    "\n[dependencies]\n"
    'vergil = "v2.0"\n'
    '\n[ci]\nversions = ["3.14"]\n'
)


def test_warns_unrecognized_project_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text(_EXTRA_PROJECT_KEY_TOML)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'version-file' in [project]" in err


def test_warns_unrecognized_top_level_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML + '\n[custom]\nfoo = "bar"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized section [custom]" in err


def test_warns_unrecognized_dependency_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML.replace('vergil = "v2.0"', 'vergil = "v2.0"\nother-tool = "v1.0"')
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'other-tool' in [dependencies]" in err


def test_warns_unrecognized_ci_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    toml = _BASE_TOML + '\n[ci]\nversions = ["3.14"]\nfoo = true\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'foo' in [ci]" in err


def test_warns_unrecognized_publish_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    toml = _VALID_TOML + "\n[publish]\nrelease = true\nfoo = true\n"
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'foo' in [publish]" in err


def test_skips_non_dict_known_section(capsys: pytest.CaptureFixture[str]) -> None:
    raw: dict[str, object] = {"ci": "not-a-dict"}
    _warn_unrecognized_keys(raw)
    err = capsys.readouterr().err
    assert err == ""


def test_no_warnings_for_valid_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert err == ""


# -- messages include the config path (issue #1411) ---------------------------


def test_primary_language_warning_includes_config_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML.replace('primary-language = "python"', 'primary-language = "none"')
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    err = capsys.readouterr().err
    assert f"warning: {tmp_path / 'vergil.toml'}: unrecognized primary-language 'none'" in err
    assert cfg.project.primary_language is None


def test_unrecognized_key_warning_includes_config_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_EXTRA_PROJECT_KEY_TOML)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert f"{tmp_path / 'vergil.toml'}: unrecognized key 'version-file' in [project]" in err


def test_unrecognized_section_warning_includes_config_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML + '\n[custom]\nfoo = "bar"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert f"{tmp_path / 'vergil.toml'}: unrecognized section [custom]" in err


def test_config_error_includes_config_path(tmp_path: Path) -> None:
    toml = _VALID_TOML.replace('release-model = "tagged-release"\n', "")
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError) as excinfo:
        read_config(tmp_path)
    assert str(tmp_path / "vergil.toml") in str(excinfo.value)


def test_invalid_toml_error_includes_config_path(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[invalid\n")
    with pytest.raises(ConfigError) as excinfo:
        read_config(tmp_path)
    assert str(tmp_path / "vergil.toml") in str(excinfo.value)


def test_vm_key_warning_includes_config_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML + "\n[vm]\nbogus = 1\n")
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert f"{tmp_path / 'vergil.toml'}: unrecognized key 'bogus' in [vm]" in err


def test_vm_role_warning_includes_config_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML + "\n[vm.vergil-user]\nbogus = 1\n")
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert f"{tmp_path / 'vergil.toml'}: unrecognized key 'bogus' in [vm.vergil-user]" in err


# -- [container] section ------------------------------------------------------

_CONTAINER_TOML = (
    _VALID_TOML
    + """
[container]
env-prefixes = ["MQ_"]
"""
)


def test_read_config_container_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_CONTAINER_TOML)
    cfg = read_config(tmp_path)
    assert cfg.container == ContainerConfig(env_prefixes=["MQ_"])


def test_read_config_no_container_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.container == ContainerConfig(env_prefixes=[])


def test_read_config_container_empty_prefixes(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[container]\nenv-prefixes = []\n"
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.container.env_prefixes == []


def test_read_config_container_multiple_prefixes(tmp_path: Path) -> None:
    toml = _VALID_TOML + '[container]\nenv-prefixes = ["MQ_", "KAFKA_"]\n'
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.container.env_prefixes == ["MQ_", "KAFKA_"]


def test_read_config_container_missing_env_prefixes(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[container]\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[container\].*env-prefixes"):
        read_config(tmp_path)


def test_read_config_container_prefixes_not_list(tmp_path: Path) -> None:
    toml = _VALID_TOML + '[container]\nenv-prefixes = "MQ_"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[container\]\.env-prefixes must be a list"):
        read_config(tmp_path)


def test_read_config_container_prefixes_not_strings(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[container]\nenv-prefixes = [1, 2]\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[container\]\.env-prefixes must be a list of strings"):
        read_config(tmp_path)


def test_warns_unrecognized_container_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALID_TOML + '[container]\nenv-prefixes = ["MQ_"]\nfoo = true\n'
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    err = capsys.readouterr().err
    assert "unrecognized key 'foo' in [container]" in err


# -- container_env_prefixes convenience function ------------------------------


def test_container_env_prefixes_with_config(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_CONTAINER_TOML)
    assert container_env_prefixes(tmp_path) == ["MQ_"]


def test_container_env_prefixes_no_file(tmp_path: Path) -> None:
    assert container_env_prefixes(tmp_path) == []


def test_container_env_prefixes_no_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    assert container_env_prefixes(tmp_path) == []


# -- [validation] section -----------------------------------------------------

_VALIDATION_TOML = _VALID_TOML + '[validation]\ncontainer-command = "uv run vrg-validate"\n'


def test_read_config_validation_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALIDATION_TOML)
    cfg = read_config(tmp_path)
    assert cfg.validation == ValidationConfig(container_command="uv run vrg-validate")


def test_read_config_no_validation_section_defaults(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.validation == ValidationConfig(container_command=DEFAULT_VALIDATION_COMMAND)


def test_read_config_validation_missing_container_command(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[validation]\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[validation\].*container-command"):
        read_config(tmp_path)


def test_read_config_validation_command_not_string(tmp_path: Path) -> None:
    toml = _VALID_TOML + "[validation]\ncontainer-command = 42\n"
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[validation\]\.container-command must be a non-empty"):
        read_config(tmp_path)


def test_read_config_validation_command_empty(tmp_path: Path) -> None:
    toml = _VALID_TOML + '[validation]\ncontainer-command = "   "\n'
    (tmp_path / "vergil.toml").write_text(toml)
    with pytest.raises(ConfigError, match=r"\[validation\]\.container-command must be a non-empty"):
        read_config(tmp_path)


def test_warns_unrecognized_validation_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    toml = _VALIDATION_TOML + "foo = true\n"
    (tmp_path / "vergil.toml").write_text(toml)
    read_config(tmp_path)
    assert "unrecognized key 'foo' in [validation]" in capsys.readouterr().err


# -- validation_container_command convenience function ------------------------


def test_validation_container_command_with_override(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALIDATION_TOML)
    assert validation_container_command(tmp_path) == "uv run vrg-validate"


def test_validation_container_command_no_file(tmp_path: Path) -> None:
    assert validation_container_command(tmp_path) == DEFAULT_VALIDATION_COMMAND


def test_validation_container_command_no_section(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    assert validation_container_command(tmp_path) == DEFAULT_VALIDATION_COMMAND


# -- [vm] cascade (issue #99) -------------------------------------------------


class TestParseVmStanza:
    def test_absent_vm_section_returns_none(self) -> None:
        assert parse_vm_stanza({}) is None

    def test_flat_vm_packages_and_footprint(self) -> None:
        raw = {
            "vm": {
                "packages": ["qemu-system-x86", "libvirt-clients"],
            }
        }
        stanza = parse_vm_stanza(raw)
        assert isinstance(stanza, VmStanza)
        assert stanza.packages == ["qemu-system-x86", "libvirt-clients"]
        assert stanza.apt_repos == []
        assert stanza.vagrant_plugins == []
        assert stanza.port_forwards == []
        assert stanza.cpus is None
        assert stanza.roles == {}

    def test_apt_repos_and_vagrant_plugins_parsed(self) -> None:
        repo = {
            "name": "hashicorp",
            "key_url": "https://apt.releases.hashicorp.com/gpg",
            "uri": "https://apt.releases.hashicorp.com",
            "suite": "noble",
            "components": "main",
        }
        raw = {
            "vm": {
                "apt_repos": [repo],
                "vagrant_plugins": ["vagrant-libvirt"],
                "port_forwards": ["3000|10.50.0.2:3000"],
                "vergil-user": {
                    "apt_repos": [repo],
                    "vagrant_plugins": ["vagrant-libvirt"],
                    "port_forwards": ["8080|10.50.0.2:8080"],
                    "packages": ["vagrant"],
                },
            }
        }
        stanza = parse_vm_stanza(raw)
        assert stanza is not None
        assert stanza.apt_repos == [repo]
        assert stanza.vagrant_plugins == ["vagrant-libvirt"]
        assert stanza.port_forwards == ["3000|10.50.0.2:3000"]
        overlay = stanza.roles["vergil-user"]
        assert overlay.apt_repos == [repo]
        assert overlay.vagrant_plugins == ["vagrant-libvirt"]
        assert overlay.port_forwards == ["8080|10.50.0.2:8080"]
        assert overlay.packages == ["vagrant"]

    def test_role_overlay_parsed(self) -> None:
        raw = {
            "vm": {
                "packages": ["qemu-system-x86"],
                "vergil-user": {"cpus": 12, "memory": "64GiB", "stale_days": 7},
            }
        }
        stanza = parse_vm_stanza(raw)
        assert stanza is not None
        assert stanza.packages == ["qemu-system-x86"]
        assert "vergil-user" in stanza.roles
        overlay = stanza.roles["vergil-user"]
        assert overlay.cpus == 12
        assert overlay.memory == "64GiB"
        assert overlay.stale_days == 7
        assert overlay.packages == []

    def test_nested_parsed_at_vm_and_role_tiers(self) -> None:
        raw = {"vm": {"nested": True, "vergil-user": {"nested": False}}}
        stanza = parse_vm_stanza(raw)
        assert stanza is not None
        assert stanza.nested is True
        assert stanza.roles["vergil-user"].nested is False

    def test_nested_absent_is_none(self) -> None:
        stanza = parse_vm_stanza({"vm": {"packages": []}})
        assert stanza is not None
        assert stanza.nested is None

    def test_nested_not_flagged_unrecognized(self, capsys: pytest.CaptureFixture[str]) -> None:
        parse_vm_stanza({"vm": {"nested": True, "vergil-user": {"nested": True}}})
        assert "unrecognized" not in capsys.readouterr().err

    def test_port_forwards_not_flagged_unrecognized(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parse_vm_stanza(
            {
                "vm": {
                    "port_forwards": ["3000|10.50.0.2:3000"],
                    "vergil-user": {"port_forwards": ["8080|10.50.0.2:8080"]},
                }
            }
        )
        assert "unrecognized" not in capsys.readouterr().err

    def test_vm_section_not_flagged_unrecognized(self, capsys: pytest.CaptureFixture[str]) -> None:
        _warn_unrecognized_keys({"vm": {"packages": [], "vergil-user": {"cpus": 4}}})
        err = capsys.readouterr().err
        assert "unrecognized section [vm]" not in err
        assert "unrecognized key" not in err

    def test_unrecognized_scalar_key_in_vm_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        parse_vm_stanza({"vm": {"bogus": 1, "packages": []}})
        assert "unrecognized key 'bogus' in [vm]" in capsys.readouterr().err

    def test_unrecognized_key_in_role_warns(self, capsys: pytest.CaptureFixture[str]) -> None:
        parse_vm_stanza({"vm": {"vergil-user": {"bogus": 1, "cpus": 4}}})
        assert "unrecognized key 'bogus' in [vm.vergil-user]" in capsys.readouterr().err

    def test_read_config_surfaces_vm_stanza(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_VALID_TOML + '\n[vm]\npackages = ["x"]\n')
        cfg = read_config(tmp_path)
        assert cfg.vm is not None
        assert cfg.vm.packages == ["x"]

    def test_read_config_no_vm_stanza_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text(_VALID_TOML)
        assert read_config(tmp_path).vm is None

    def test_shared_from_parsed_to_org_repo(self) -> None:
        stanza = parse_vm_stanza({"vm": {"shared_from": "lmf/mq-resiliency-lab"}})
        assert stanza is not None
        assert stanza.shared_from == ("lmf", "mq-resiliency-lab")
        assert stanza.packages == []
        assert stanza.roles == {}

    def test_shared_from_absent_is_none(self) -> None:
        stanza = parse_vm_stanza({"vm": {"packages": []}})
        assert stanza is not None
        assert stanza.shared_from is None

    def test_shared_from_not_flagged_unrecognized(self, capsys: pytest.CaptureFixture[str]) -> None:
        parse_vm_stanza({"vm": {"shared_from": "lmf/mq"}})
        assert "unrecognized" not in capsys.readouterr().err

    def test_shared_from_bare_repo_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from must be 'org/repo'"):
            parse_vm_stanza({"vm": {"shared_from": "mq-resiliency-lab"}})

    def test_shared_from_empty_side_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from must be 'org/repo'"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/"}})

    def test_shared_from_extra_slash_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from must be 'org/repo'"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq/extra"}})

    def test_shared_from_whitespace_rejected(self) -> None:
        with pytest.raises(ConfigError, match="whitespace"):
            parse_vm_stanza({"vm": {"shared_from": "lmf / mq"}})

    def test_shared_from_non_string_rejected(self) -> None:
        with pytest.raises(ConfigError, match="must be a string"):
            parse_vm_stanza({"vm": {"shared_from": 123}})

    def test_shared_from_with_footprint_key_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq", "cpus": 8}})

    def test_shared_from_with_packages_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq", "packages": ["x"]}})

    def test_shared_from_with_role_overlay_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq", "vergil-user": {"cpus": 8}}})

    def test_shared_from_inside_role_rejected(self) -> None:
        with pytest.raises(ConfigError, match="shared_from is not allowed in a role"):
            parse_vm_stanza({"vm": {"vergil-user": {"shared_from": "lmf/mq"}}})

    # -- off-platform (cloud) keys (vergil-vm #199 / #1706) -------------------

    def test_off_platform_keys_parsed_at_vm_tier(self) -> None:
        raw = {
            "vm": {
                "backend": "off-platform",
                "provider": "gcp",
                "region": "us-central1",
                "instance": "n2-standard-16",
                "volume": "300GiB",
            }
        }
        stanza = parse_vm_stanza(raw)
        assert stanza is not None
        assert stanza.backend == "off-platform"
        assert stanza.provider == "gcp"
        assert stanza.region == "us-central1"
        assert stanza.instance == "n2-standard-16"
        assert stanza.volume == "300GiB"

    def test_off_platform_keys_parsed_at_role_tier(self) -> None:
        raw = {
            "vm": {
                "backend": "off-platform",
                "vergil-user": {"instance": "n2-standard-16", "volume": "300GiB"},
            }
        }
        stanza = parse_vm_stanza(raw)
        assert stanza is not None
        assert stanza.backend == "off-platform"
        overlay = stanza.roles["vergil-user"]
        assert overlay.instance == "n2-standard-16"
        assert overlay.volume == "300GiB"
        assert overlay.backend is None  # only the [vm] tier declared it

    def test_off_platform_keys_absent_are_none(self) -> None:
        stanza = parse_vm_stanza({"vm": {"packages": []}})
        assert stanza is not None
        assert stanza.backend is None
        assert stanza.provider is None
        assert stanza.region is None
        assert stanza.instance is None
        assert stanza.volume is None

    def test_off_platform_keys_not_flagged_unrecognized(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        parse_vm_stanza(
            {
                "vm": {
                    "backend": "off-platform",
                    "provider": "gcp",
                    "vergil-user": {"region": "us-central1"},
                }
            }
        )
        assert "unrecognized" not in capsys.readouterr().err

    def test_backend_non_string_rejected(self) -> None:
        with pytest.raises(ConfigError, match="'backend' must be a string"):
            parse_vm_stanza({"vm": {"backend": 1}})

    def test_provider_non_string_in_role_rejected(self) -> None:
        with pytest.raises(ConfigError, match="'provider' must be a string"):
            parse_vm_stanza({"vm": {"vergil-user": {"provider": ["gcp"]}}})

    def test_shared_from_with_backend_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cannot be combined"):
            parse_vm_stanza({"vm": {"shared_from": "lmf/mq", "backend": "off-platform"}})


# -- [project] ghas key -------------------------------------------------------

_GHAS_TOML_TEMPLATE = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "python"
{ghas_line}

[dependencies]
vergil = "v2.1"

[ci]
versions = ["3.14"]
"""


def test_project_ghas_absent_is_none(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_GHAS_TOML_TEMPLATE.format(ghas_line=""))
    cfg = read_config(tmp_path)
    assert cfg.project.ghas is None


def test_project_ghas_true(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_GHAS_TOML_TEMPLATE.format(ghas_line="ghas = true"))
    cfg = read_config(tmp_path)
    assert cfg.project.ghas is True


def test_project_ghas_false(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_GHAS_TOML_TEMPLATE.format(ghas_line="ghas = false"))
    cfg = read_config(tmp_path)
    assert cfg.project.ghas is False


def test_project_ghas_non_bool_raises(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_GHAS_TOML_TEMPLATE.format(ghas_line='ghas = "yes"'))
    with pytest.raises(ConfigError, match=r"\[project\].ghas must be a boolean"):
        read_config(tmp_path)


def test_project_ghas_is_recognized_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "vergil.toml").write_text(_GHAS_TOML_TEMPLATE.format(ghas_line="ghas = true"))
    read_config(tmp_path)
    assert "unrecognized key 'ghas'" not in capsys.readouterr().err
