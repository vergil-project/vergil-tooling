"""Tests for vergil_tooling.bin.st_generate_commands."""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from vergil_tooling.bin.st_generate_commands import (
    classify_command,
    generate,
    load_commands,
    main,
    parse_args,
    update_file,
)

# ---------------------------------------------------------------------------
# Minimal mapping-data.json for testing
# ---------------------------------------------------------------------------

MINIMAL_MAPPING_DATA = {
    "commands": {
        "ALTER AUTHINFO": {"qualifier": "authinfo"},
        "ALTER QMGR": {"qualifier": "qmgr"},
        "DEFINE CHANNEL": {"qualifier": "channel", "name_required": True},
        "DISPLAY AUTHINFO": {"qualifier": "authinfo"},
        "DEFINE QLOCAL": {"qualifier": "queue", "name_required": True},
        "DELETE QUEUE": {"qualifier": "queue", "name_required": True},
        "DISPLAY CHANNEL": {"qualifier": "channel", "pattern": "list", "name_default": "*"},
        "DISPLAY CMDSERV": {"qualifier": "cmdserv", "pattern": "singleton"},
        "DISPLAY QMGR": {"qualifier": "qmgr", "pattern": "singleton"},
        "DISPLAY QUEUE": {"qualifier": "queue", "pattern": "list", "name_default": "*"},
        "START CHANNEL": {"qualifier": "channel"},
        "START QMGR": {"qualifier": "qmgr"},
        "STOP CMDSERV": {"qualifier": "cmdserv"},
    },
    "qualifiers": {},
}


@pytest.fixture()
def mapping_data_file(tmp_path: Path) -> Path:
    path = tmp_path / "mapping-data.json"
    path.write_text(json.dumps(MINIMAL_MAPPING_DATA), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_minimal(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    args = parse_args(["--language", "python", "--mapping-data", str(p)])
    assert args.language == "python"
    assert args.mapping_data == p
    assert args.target is None
    assert args.check is False


def test_parse_args_all_options(tmp_path: Path) -> None:
    m = tmp_path / "m.json"
    t = tmp_path / "out.go"
    pages = tmp_path / "pages"
    args = parse_args(
        [
            "--language",
            "go",
            "--mapping-data",
            str(m),
            "--target",
            str(t),
            "--mapping-pages-dir",
            str(pages),
            "--check",
        ]
    )
    assert args.language == "go"
    assert args.target == t
    assert args.mapping_pages_dir == pages
    assert args.check is True


def test_parse_args_invalid_language(tmp_path: Path) -> None:
    p = tmp_path / "m.json"
    with pytest.raises(SystemExit):
        parse_args(["--language", "fortran", "--mapping-data", str(p)])


# ---------------------------------------------------------------------------
# classify_command
# ---------------------------------------------------------------------------


def test_classify_singleton() -> None:
    spec = classify_command("DISPLAY", "QMGR", {"qualifier": "qmgr", "pattern": "singleton"})
    assert spec.pattern == "singleton"
    assert spec.qualifier == "qmgr"


def test_classify_list_with_default() -> None:
    spec = classify_command(
        "DISPLAY", "QUEUE", {"qualifier": "queue", "pattern": "list", "name_default": "*"}
    )
    assert spec.pattern == "list"
    assert spec.name_default == "*"


def test_classify_display_list_without_explicit_pattern() -> None:
    """All non-singleton DISPLAY commands are list pattern."""
    spec = classify_command("DISPLAY", "AUTHINFO", {"qualifier": "authinfo"})
    assert spec.pattern == "list"
    assert spec.name_default is None


def test_classify_required_name() -> None:
    spec = classify_command("DEFINE", "QLOCAL", {"qualifier": "queue", "name_required": True})
    assert spec.pattern == "required_name"


def test_classify_no_name() -> None:
    spec = classify_command("ALTER", "QMGR", {"qualifier": "qmgr"})
    assert spec.pattern == "no_name"


def test_classify_no_name_cmdserv() -> None:
    spec = classify_command("STOP", "CMDSERV", {"qualifier": "cmdserv"})
    assert spec.pattern == "no_name"


def test_classify_optional_name_default() -> None:
    spec = classify_command("ALTER", "AUTHINFO", {"qualifier": "authinfo"})
    assert spec.pattern == "optional_name"


def test_classify_fallback_qualifier() -> None:
    """Falls back to mqsc_qualifier.lower() if qualifier is missing or invalid."""
    spec = classify_command("ALTER", "AUTHINFO", {})
    assert spec.qualifier == "authinfo"

    spec2 = classify_command("ALTER", "AUTHINFO", {"qualifier": 42})
    assert spec2.qualifier == "authinfo"


# ---------------------------------------------------------------------------
# load_commands
# ---------------------------------------------------------------------------


def test_load_commands(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    assert len(specs) == len(MINIMAL_MAPPING_DATA["commands"])
    names = [f"{s.verb} {s.mqsc_qualifier}" for s in specs]
    assert names == sorted(names)  # alphabetical order


def test_load_commands_empty(tmp_path: Path) -> None:
    path = tmp_path / "empty.json"
    path.write_text('{"commands": {}}', encoding="utf-8")
    assert load_commands(path) == []


def test_load_commands_no_commands_key(tmp_path: Path) -> None:
    path = tmp_path / "no.json"
    path.write_text("{}", encoding="utf-8")
    assert load_commands(path) == []


def test_load_commands_non_dict(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text('{"commands": "invalid"}', encoding="utf-8")
    assert load_commands(path) == []


def test_load_commands_skips_invalid_entries(tmp_path: Path) -> None:
    path = tmp_path / "partial.json"
    data = {
        "commands": {
            "ALTER AUTHINFO": {"qualifier": "authinfo"},
            "BADENTRY": {"qualifier": "bad"},  # no space, skipped
            "ALTER CHANNEL": "not_a_dict",  # skipped
        }
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    specs = load_commands(path)
    assert len(specs) == 1
    assert specs[0].verb == "ALTER"


# ---------------------------------------------------------------------------
# Python generation
# ---------------------------------------------------------------------------


def test_python_singleton(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("python", specs)
    assert "def display_qmgr(" in output
    assert "-> dict[str, object] | None:" in output
    assert "objects = self._mqsc_command(" in output
    assert "return objects[0]" in output
    assert "name=None," in output


def test_python_list_with_default(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("python", specs)
    assert "def display_queue(" in output
    assert "-> list[dict[str, object]]:" in output
    assert 'name=name or "*",' in output
    assert "where: str | None = None," in output


def test_python_list_without_default(mapping_data_file: Path) -> None:
    """DISPLAY commands without name_default still pass name directly."""
    data = {"commands": {"DISPLAY AUTHINFO": {"qualifier": "authinfo"}}}
    path = mapping_data_file.parent / "auth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    specs = load_commands(path)
    output = generate("python", specs)
    assert "def display_authinfo(" in output
    assert "name=name," in output
    assert "where=where," in output


def test_python_required_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("python", specs)
    assert "def define_qlocal(" in output
    assert "name: str," in output
    assert "-> None:" in output


def test_python_optional_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("python", specs)
    assert "def alter_authinfo(" in output
    assert "name: str | None = None," in output
    assert "-> None:" in output


def test_python_no_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("python", specs)
    assert "def alter_qmgr(" in output
    # Should not have name parameter
    assert "alter_qmgr(\n        self,\n        request_parameters:" in output


def test_python_mapping_pages(mapping_data_file: Path, tmp_path: Path) -> None:
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "queue.md").write_text("# Queue")
    (pages_dir / "index.md").write_text("# Index")
    mapping_pages = frozenset(
        p.stem for p in pages_dir.iterdir() if p.suffix == ".md" and p.stem != "index"
    )
    specs = load_commands(mapping_data_file)
    output = generate("python", specs, mapping_pages=mapping_pages)
    assert "queue attribute mappings" in output
    # Index should not appear
    assert "index attribute mappings" not in output


# ---------------------------------------------------------------------------
# Ruby generation
# ---------------------------------------------------------------------------


def test_ruby_singleton(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("ruby", specs)
    assert "def display_qmgr(request_parameters: nil, response_parameters: nil)" in output
    assert "objects.empty? ? nil : objects[0]" in output


def test_ruby_list_with_default(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("ruby", specs)
    assert "def display_queue(name: nil," in output
    assert "name: name || '*'" in output
    assert "where: where" in output


def test_ruby_list_without_default(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("ruby", specs)
    assert "def display_authinfo(name, request_parameters:" in output
    assert "where: where" in output


def test_ruby_required_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("ruby", specs)
    assert "def define_qlocal(name, request_parameters:" in output
    assert "nil\n        end" in output


def test_ruby_optional_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("ruby", specs)
    assert "def alter_authinfo(name: nil," in output


def test_ruby_no_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("ruby", specs)
    assert "def alter_qmgr(request_parameters: nil, response_parameters: nil)" in output
    assert "name: nil, request_parameters:" in output


# ---------------------------------------------------------------------------
# Java generation
# ---------------------------------------------------------------------------


def test_java_singleton(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("java", specs)
    assert "public @Nullable Map<String, Object> displayQmgr(" in output
    assert "objects.isEmpty() ? null : objects.get(0);" in output


def test_java_list_with_default(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("java", specs)
    assert "public List<Map<String, Object>> displayQueue(" in output
    assert 'name != null ? name : "*"' in output


def test_java_required_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("java", specs)
    assert "public void defineQlocal(" in output
    assert 'Objects.requireNonNull(name, "name")' in output


def test_java_optional_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("java", specs)
    assert "public void alterAuthinfo(" in output
    # No requireNonNull for optional
    lines_between = output[output.index("alterAuthinfo(") : output.index("alterQmgr(")]
    assert "requireNonNull" not in lines_between


def test_java_no_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("java", specs)
    assert "public void alterQmgr(" in output
    assert "null, requestParameters, responseParameters, null);" in output


# ---------------------------------------------------------------------------
# Go generation
# ---------------------------------------------------------------------------


def test_go_singleton(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("go", specs)
    assert "func (session *Session) DisplayQmgr(ctx context.Context," in output
    assert "(map[string]any, error)" in output
    assert "return objects[0], nil" in output


def test_go_list_with_default(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("go", specs)
    assert "func (session *Session) DisplayQueue(ctx context.Context, name string," in output
    assert "([]map[string]any, error)" in output
    assert 'displayName = "*"' in output


def test_java_list_without_default(mapping_data_file: Path) -> None:
    """DISPLAY commands without name_default pass name directly in Java."""
    data = {"commands": {"DISPLAY AUTHINFO": {"qualifier": "authinfo"}}}
    path = mapping_data_file.parent / "auth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    specs = load_commands(path)
    output = generate("java", specs)
    assert "displayAuthinfo(" in output
    assert "name, requestParameters, responseParameters, where" in output


def test_rust_list_without_default(mapping_data_file: Path) -> None:
    """DISPLAY commands without name_default pass name directly in Rust."""
    data = {"commands": {"DISPLAY AUTHINFO": {"qualifier": "authinfo"}}}
    path = mapping_data_file.parent / "auth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    specs = load_commands(path)
    output = generate("rust", specs)
    assert "pub fn display_authinfo(" in output
    assert "            name," in output


def test_go_list_without_default(mapping_data_file: Path) -> None:
    """DISPLAY commands without name_default use displayList helper."""
    data = {"commands": {"DISPLAY AUTHINFO": {"qualifier": "authinfo"}}}
    path = mapping_data_file.parent / "auth.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    specs = load_commands(path)
    output = generate("go", specs)
    assert "displayList(ctx," in output


def test_go_required_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("go", specs)
    assert "func (session *Session) DefineQlocal(ctx context.Context, name string," in output
    assert "error {" in output
    assert "voidCommand(ctx," in output


def test_go_optional_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("go", specs)
    assert "func (session *Session) AlterAuthinfo(ctx context.Context, name string," in output
    assert "voidCommandOptionalName(ctx," in output


def test_go_no_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("go", specs)
    assert "func (session *Session) AlterQmgr(ctx context.Context, opts ...CommandOption)" in output
    assert "voidCommand(ctx," in output


# ---------------------------------------------------------------------------
# Rust generation
# ---------------------------------------------------------------------------


def test_rust_singleton(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("rust", specs)
    assert "pub fn display_qmgr(" in output
    assert "Result<Option<HashMap<String, Value>>>" in output
    assert "Ok(objects.into_iter().next())" in output


def test_rust_list_with_default(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("rust", specs)
    assert "pub fn display_queue(" in output
    assert "Result<Vec<HashMap<String, Value>>>" in output
    assert 'Some(name.unwrap_or("*"))' in output


def test_rust_required_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("rust", specs)
    assert "pub fn define_qlocal(" in output
    assert "name: &str," in output
    assert "Result<()>" in output
    assert "Some(name)," in output


def test_rust_optional_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("rust", specs)
    assert "pub fn alter_authinfo(" in output
    assert "name: Option<&str>," in output


def test_rust_no_name(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    output = generate("rust", specs)
    assert "pub fn alter_qmgr(" in output
    # Should not have name parameter
    section = output[output.index("pub fn alter_qmgr(") : output.index("pub fn define_channel(")]
    assert "name:" not in section
    assert "None," in section  # passes None for name


# ---------------------------------------------------------------------------
# generate() dispatch
# ---------------------------------------------------------------------------


def test_generate_invalid_language(mapping_data_file: Path) -> None:
    specs = load_commands(mapping_data_file)
    with pytest.raises(ValueError, match="Unsupported language"):
        generate("fortran", specs)


# ---------------------------------------------------------------------------
# update_file
# ---------------------------------------------------------------------------


def test_update_file_python(tmp_path: Path, mapping_data_file: Path) -> None:
    target = tmp_path / "commands.py"
    target.write_text(
        textwrap.dedent("""\
            class Mixin:
                # BEGIN GENERATED MQSC METHODS
                # old content
                # END GENERATED MQSC METHODS
                pass
        """).replace("            ", "    "),
        encoding="utf-8",
    )
    specs = load_commands(mapping_data_file)
    generated = generate("python", specs)
    changed = update_file(target, "python", generated)
    assert changed is True
    content = target.read_text()
    assert "# BEGIN GENERATED MQSC METHODS" in content
    assert "# END GENERATED MQSC METHODS" in content
    assert "def display_qmgr(" in content
    assert "# old content" not in content


def test_update_file_idempotent(tmp_path: Path, mapping_data_file: Path) -> None:
    target = tmp_path / "commands.py"
    target.write_text(
        "    # BEGIN GENERATED MQSC METHODS\n    # old\n    # END GENERATED MQSC METHODS\n",
        encoding="utf-8",
    )
    specs = load_commands(mapping_data_file)
    generated = generate("python", specs)
    update_file(target, "python", generated)
    changed = update_file(target, "python", generated)
    assert changed is False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_main_stdout(mapping_data_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["--language", "python", "--mapping-data", str(mapping_data_file)])
    assert result == 0
    captured = capsys.readouterr()
    assert "def display_qmgr(" in captured.out


def test_main_target_update(
    mapping_data_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "out.py"
    target.write_text(
        "    # BEGIN GENERATED MQSC METHODS\n    # END GENERATED MQSC METHODS\n",
        encoding="utf-8",
    )
    result = main(
        [
            "--language",
            "python",
            "--mapping-data",
            str(mapping_data_file),
            "--target",
            str(target),
        ]
    )
    assert result == 0
    assert "def display_qmgr(" in target.read_text()
    captured = capsys.readouterr()
    assert "updated" in captured.out


def test_main_check_pass(
    mapping_data_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "out.py"
    target.write_text(
        "    # BEGIN GENERATED MQSC METHODS\n    # END GENERATED MQSC METHODS\n",
        encoding="utf-8",
    )
    # First write
    main(
        [
            "--language",
            "python",
            "--mapping-data",
            str(mapping_data_file),
            "--target",
            str(target),
        ]
    )
    # Then check
    result = main(
        [
            "--language",
            "python",
            "--mapping-data",
            str(mapping_data_file),
            "--target",
            str(target),
            "--check",
        ]
    )
    assert result == 0
    captured = capsys.readouterr()
    assert "up to date" in captured.out


def test_main_check_fail(
    mapping_data_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "out.py"
    target.write_text(
        "    # BEGIN GENERATED MQSC METHODS\n    # stale\n    # END GENERATED MQSC METHODS\n",
        encoding="utf-8",
    )
    result = main(
        [
            "--language",
            "python",
            "--mapping-data",
            str(mapping_data_file),
            "--target",
            str(target),
            "--check",
        ]
    )
    assert result == 1


def test_main_missing_mapping_data(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["--language", "python", "--mapping-data", "/nonexistent/mapping.json"])
    assert result == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_main_all_languages(mapping_data_file: Path) -> None:
    for lang in ("python", "ruby", "java", "go", "rust"):
        result = main(["--language", lang, "--mapping-data", str(mapping_data_file)])
        assert result == 0, f"Failed for {lang}"


def test_main_mapping_pages(mapping_data_file: Path, tmp_path: Path) -> None:
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "queue.md").write_text("# Queue")
    result = main(
        [
            "--language",
            "python",
            "--mapping-data",
            str(mapping_data_file),
            "--mapping-pages-dir",
            str(pages_dir),
        ]
    )
    assert result == 0


# ---------------------------------------------------------------------------
# Method count consistency
# ---------------------------------------------------------------------------


def test_all_languages_same_method_count(mapping_data_file: Path) -> None:
    """All languages should generate the same number of methods."""
    specs = load_commands(mapping_data_file)
    expected = len(specs)
    for lang in ("python", "ruby", "java", "go", "rust"):
        output = generate(lang, specs)
        # Count methods by language-specific markers
        if lang == "python":
            count = output.count("    def ")
        elif lang == "ruby":
            count = output.count("        def ")
        elif lang == "java":
            count = output.count("  public ")
        elif lang == "go":
            count = output.count("func (session *Session) ")
        elif lang == "rust":
            count = output.count("    pub fn ")
        else:
            continue
        assert count == expected, f"{lang}: expected {expected} methods, got {count}"
