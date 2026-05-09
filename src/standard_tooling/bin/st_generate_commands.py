"""Generate MQSC command methods for all language ports.

Reads mapping-data.json and generates complete command method blocks for
Python, Ruby, Java, Go, and Rust.  Output is written between
BEGIN/END GENERATED MQSC METHODS markers in the target file.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Metadata defaults
# ---------------------------------------------------------------------------
# Commands whose MQSC qualifier matches one of these target the queue manager
# itself — no ``name`` parameter in any language.
NO_NAME_QUALIFIERS = frozenset({"QMGR", "CMDSERV", "QMSTATUS"})


@dataclass(frozen=True)
class CommandSpec:
    """Describes one MQSC command for code generation."""

    verb: str
    mqsc_qualifier: str
    qualifier: str
    pattern: str  # "singleton" | "list" | "required_name" | "optional_name" | "no_name"
    name_default: str | None  # e.g. "*" for DISPLAY QUEUE


def classify_command(
    verb: str,
    mqsc_qualifier: str,
    entry: dict[str, object],
) -> CommandSpec:
    """Derive a CommandSpec from a mapping-data.json command entry."""
    qualifier = entry.get("qualifier", mqsc_qualifier.lower())
    if not isinstance(qualifier, str):
        qualifier = mqsc_qualifier.lower()

    explicit_pattern = entry.get("pattern")
    name_required = entry.get("name_required", False)
    name_default = entry.get("name_default")

    if explicit_pattern == "singleton":
        pattern = "singleton"
    elif verb == "DISPLAY" and mqsc_qualifier not in NO_NAME_QUALIFIERS:
        # All non-singleton DISPLAY commands return a list.
        pattern = "list"
    elif name_required:
        pattern = "required_name"
    elif mqsc_qualifier in NO_NAME_QUALIFIERS:
        pattern = "no_name"
    else:
        pattern = "optional_name"

    return CommandSpec(
        verb=verb,
        mqsc_qualifier=mqsc_qualifier,
        qualifier=qualifier,
        pattern=pattern,
        name_default=str(name_default) if name_default is not None else None,
    )


def load_commands(mapping_data_path: Path) -> list[CommandSpec]:
    """Load and classify all commands from mapping-data.json."""
    data = json.loads(mapping_data_path.read_text(encoding="utf-8"))
    commands_raw = data.get("commands", {})
    if not isinstance(commands_raw, dict):
        return []

    specs: list[CommandSpec] = []
    for command_key in sorted(commands_raw.keys()):
        parts = command_key.split(" ", 1)
        if len(parts) != 2:  # noqa: PLR2004
            continue
        verb, mqsc_qualifier = parts
        entry = commands_raw[command_key]
        if not isinstance(entry, dict):
            continue
        specs.append(classify_command(verb, mqsc_qualifier, entry))
    return specs


# ---------------------------------------------------------------------------
# Python generation
# ---------------------------------------------------------------------------

MQSC_REF_URL = "https://www.ibm.com/docs/en/ibm-mq/9.4?topic=reference-mqsc-commands"
PYTHON_DOCS_BASE_URL = "https://wphillipmoore.github.io/mq-rest-admin-python"


def _python_docstring(cmd: CommandSpec, mapping_pages: frozenset[str]) -> str:
    """Build a Python docstring for a command method."""
    command_label = f"{cmd.verb} {cmd.mqsc_qualifier}"
    lines = [f'        """Execute the MQSC ``{command_label}`` command.']
    lines.append("")
    lines.append(f"        See `MQSC reference <{MQSC_REF_URL}>`__")
    lines.append("        for command details.")
    if cmd.qualifier in mapping_pages:
        lines.append(
            f"        See `{cmd.qualifier} attribute mappings"
            f" <{PYTHON_DOCS_BASE_URL}/mappings/{cmd.qualifier}.html>`__."
        )
    lines.append("")

    # Args section
    lines.append("        Args:")
    if cmd.pattern in ("list", "optional_name", "required_name"):
        name_desc = "Object name or generic pattern." if cmd.pattern == "list" else "Object name."
        lines.append(f"            name: {name_desc}")
    lines.append("            request_parameters: Request attributes as a dict. Mapped")
    lines.append("                from ``snake_case`` when mapping is enabled.")
    lines.append("            response_parameters: Response attributes to return.")
    lines.append('                Defaults to ``["all"]``.')
    if cmd.pattern == "list":
        lines.append('            where: Filter expression (e.g. ``"current_depth GT 100"``).')
        lines.append("                The keyword is mapped from ``snake_case`` when mapping")
        lines.append("                is enabled.")
    lines.append("")

    # Returns/Raises section
    if cmd.pattern == "singleton":
        lines.append("        Returns:")
        lines.append("            Parameter dict, or ``None``.")
    elif cmd.pattern == "list":
        lines.append("        Returns:")
        lines.append("            List of parameter dicts, one per matching object. Empty")
        lines.append("            list if no objects match.")
    else:
        lines.append("        Raises:")
        lines.append("            MQRESTCommandError: If the command fails.")
    lines.append("")
    lines.append('        """')
    return "\n".join(lines)


def _python_method(cmd: CommandSpec, mapping_pages: frozenset[str]) -> str:
    """Generate one Python method."""
    method_name = f"{cmd.verb.lower()}_{cmd.mqsc_qualifier.lower()}"

    # Signature
    sig_lines = [f"    def {method_name}(", "        self,"]
    if cmd.pattern == "required_name":
        sig_lines.append("        name: str,")
    elif cmd.pattern in ("list", "optional_name"):
        sig_lines.append("        name: str | None = None,")
    sig_lines.append("        request_parameters: Mapping[str, object] | None = None,")
    sig_lines.append("        response_parameters: Sequence[str] | None = None,")
    if cmd.pattern == "list":
        sig_lines.append("        where: str | None = None,")

    if cmd.pattern == "singleton":
        sig_lines.append("    ) -> dict[str, object] | None:")
    elif cmd.pattern == "list":
        sig_lines.append("    ) -> list[dict[str, object]]:")
    else:
        sig_lines.append("    ) -> None:")

    # Docstring
    docstring = _python_docstring(cmd, mapping_pages)

    # Body
    body_lines: list[str] = []
    if cmd.pattern == "list":
        body_lines.append("        return self._mqsc_command(")
    elif cmd.pattern == "singleton":
        body_lines.append("        objects = self._mqsc_command(")
    else:
        body_lines.append("        self._mqsc_command(")

    body_lines.append(f'            command="{cmd.verb}",')
    body_lines.append(f'            mqsc_qualifier="{cmd.mqsc_qualifier}",')

    if cmd.pattern == "list" and cmd.name_default:
        body_lines.append(f'            name=name or "{cmd.name_default}",')
    elif cmd.pattern in ("required_name", "optional_name", "list"):
        body_lines.append("            name=name,")
    else:
        body_lines.append("            name=None,")

    body_lines.append("            request_parameters=request_parameters,")
    body_lines.append("            response_parameters=response_parameters,")
    if cmd.pattern == "list":
        body_lines.append("            where=where,")
    body_lines.append("        )")

    if cmd.pattern == "singleton":
        body_lines.append("        if objects:")
        body_lines.append("            return objects[0]")
        body_lines.append("        return None")

    return "\n".join(sig_lines + [docstring] + body_lines)


def generate_python(specs: list[CommandSpec], mapping_pages: frozenset[str]) -> str:
    """Generate all Python methods."""
    methods = [_python_method(cmd, mapping_pages) for cmd in specs]
    return "\n\n".join(methods)


# ---------------------------------------------------------------------------
# Ruby generation
# ---------------------------------------------------------------------------


def _ruby_method(cmd: CommandSpec) -> str:
    """Generate one Ruby method."""
    method_name = f"{cmd.verb.lower()}_{cmd.mqsc_qualifier.lower()}"
    command_label = f"{cmd.verb} {cmd.mqsc_qualifier}"

    lines: list[str] = []

    # YARD doc
    lines.append(f"        # Execute the MQSC +{command_label}+ command.")
    lines.append("        #")

    if cmd.pattern == "required_name" or (cmd.pattern == "list" and not cmd.name_default):
        lines.append("        # @param name [String] the object name")
    elif cmd.pattern in ("list", "optional_name"):
        if cmd.name_default:
            lines.append(
                f"        # @param name [String, nil] object name or pattern,"
                f' defaults to +"{cmd.name_default}"+'
            )
        else:
            lines.append("        # @param name [String, nil] object name")

    lines.append(
        "        # @param request_parameters [Hash{String => Object}, nil] request attributes"
    )
    lines.append(
        "        # @param response_parameters [Array<String>, nil] response attributes to return"
    )
    if cmd.pattern == "list":
        lines.append("        # @param where [String, nil] filter expression")

    if cmd.pattern == "singleton":
        lines.append(
            "        # @return [Hash{String => Object}, nil] parameter hash, or nil if empty"
        )
    elif cmd.pattern == "list":
        lines.append("        # @return [Array<Hash{String => Object}>] parameter hashes")
    else:
        lines.append("        # @return [nil]")

    lines.append("        # @raise [CommandError] if the MQSC command fails")
    lines.append("        # @raise [MappingError] if attribute mapping fails in strict mode")

    # Signature
    if cmd.pattern == "required_name":
        lines.append(
            f"        def {method_name}(name, request_parameters: nil, response_parameters: nil)"
        )
    elif cmd.pattern in ("no_name", "singleton"):
        lines.append(
            f"        def {method_name}(request_parameters: nil, response_parameters: nil)"
        )
    elif cmd.pattern == "list" and cmd.name_default:
        lines.append(
            f"        def {method_name}(name: nil, request_parameters: nil,"
            f" response_parameters: nil, where: nil)"
        )
    elif cmd.pattern == "list":
        lines.append(
            f"        def {method_name}(name, request_parameters: nil,"
            f" response_parameters: nil, where: nil)"
        )
    else:  # optional_name
        lines.append(
            f"        def {method_name}(name: nil, request_parameters: nil,"
            f" response_parameters: nil)"
        )

    # Body
    if cmd.pattern == "list" and cmd.name_default:
        name_expr = f"name || '{cmd.name_default}'"
    elif cmd.pattern in ("required_name", "optional_name", "list"):
        name_expr = "name"
    else:
        name_expr = "nil"

    if cmd.pattern == "list":
        lines.append("          mqsc_command(")
        lines.append(f"            command: '{cmd.verb}', mqsc_qualifier: '{cmd.mqsc_qualifier}',")
        lines.append(f"            name: {name_expr}, request_parameters: request_parameters,")
        lines.append("            response_parameters: response_parameters, where: where")
        lines.append("          )")
    elif cmd.pattern == "singleton":
        lines.append("          objects = mqsc_command(")
        lines.append(f"            command: '{cmd.verb}', mqsc_qualifier: '{cmd.mqsc_qualifier}',")
        lines.append(f"            name: {name_expr}, request_parameters: request_parameters,")
        lines.append("            response_parameters: response_parameters")
        lines.append("          )")
        lines.append("          objects.empty? ? nil : objects[0]")
    else:
        lines.append("          mqsc_command(")
        lines.append(f"            command: '{cmd.verb}', mqsc_qualifier: '{cmd.mqsc_qualifier}',")
        lines.append(f"            name: {name_expr}, request_parameters: request_parameters,")
        lines.append("            response_parameters: response_parameters")
        lines.append("          )")
        lines.append("          nil")

    lines.append("        end")
    return "\n".join(lines)


def generate_ruby(specs: list[CommandSpec]) -> str:
    """Generate all Ruby methods."""
    methods = [_ruby_method(cmd) for cmd in specs]
    return "\n\n".join(methods)


# ---------------------------------------------------------------------------
# Java generation
# ---------------------------------------------------------------------------


def _java_method_name(cmd: CommandSpec) -> str:
    """Convert verb_qualifier to camelCase."""
    parts = f"{cmd.verb.lower()}_{cmd.mqsc_qualifier.lower()}".split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _java_method(cmd: CommandSpec) -> str:
    """Generate one Java method."""
    method_name = _java_method_name(cmd)
    command_label = f"{cmd.verb} {cmd.mqsc_qualifier}"

    lines: list[str] = []
    article = "an" if cmd.verb[0] in "AEIOU" else "a"
    lines.append(f"  /** Executes {article} {command_label} MQSC command. */")

    if cmd.pattern == "singleton":
        lines.append(f"  public @Nullable Map<String, Object> {method_name}(")
        lines.append(
            "      @Nullable Map<String, Object> requestParameters,"
            " @Nullable List<String> responseParameters) {"
        )
        lines.append("    List<Map<String, Object>> objects =")
        lines.append(
            f'        mqscCommand("{cmd.verb}", "{cmd.mqsc_qualifier}",'
            f" null, requestParameters, responseParameters, null);"
        )
        lines.append("    return objects.isEmpty() ? null : objects.get(0);")
    elif cmd.pattern == "list":
        lines.append(f"  public List<Map<String, Object>> {method_name}(")
        lines.append("      @Nullable String name,")
        lines.append("      @Nullable Map<String, Object> requestParameters,")
        lines.append("      @Nullable List<String> responseParameters,")
        lines.append("      @Nullable String where) {")
        if cmd.name_default:
            lines.append(
                f'    return mqscCommand("{cmd.verb}", "{cmd.mqsc_qualifier}",'
                f' name != null ? name : "{cmd.name_default}",'
                f" requestParameters, responseParameters, where);"
            )
        else:
            lines.append(
                f'    return mqscCommand("{cmd.verb}", "{cmd.mqsc_qualifier}",'
                f" name, requestParameters, responseParameters, where);"
            )
    elif cmd.pattern == "required_name":
        lines.append(f"  public void {method_name}(")
        lines.append("      @Nullable String name,")
        lines.append("      @Nullable Map<String, Object> requestParameters,")
        lines.append("      @Nullable List<String> responseParameters) {")
        lines.append('    Objects.requireNonNull(name, "name");')
        lines.append(
            f'    mqscCommand("{cmd.verb}", "{cmd.mqsc_qualifier}",'
            f" name, requestParameters, responseParameters, null);"
        )
    elif cmd.pattern == "no_name":
        lines.append(f"  public void {method_name}(")
        lines.append(
            "      @Nullable Map<String, Object> requestParameters,"
            " @Nullable List<String> responseParameters) {"
        )
        lines.append(
            f'    mqscCommand("{cmd.verb}", "{cmd.mqsc_qualifier}",'
            f" null, requestParameters, responseParameters, null);"
        )
    else:  # optional_name
        lines.append(f"  public void {method_name}(")
        lines.append("      @Nullable String name,")
        lines.append("      @Nullable Map<String, Object> requestParameters,")
        lines.append("      @Nullable List<String> responseParameters) {")
        lines.append(
            f'    mqscCommand("{cmd.verb}", "{cmd.mqsc_qualifier}",'
            f" name, requestParameters, responseParameters, null);"
        )

    lines.append("  }")
    return "\n".join(lines)


def generate_java(specs: list[CommandSpec]) -> str:
    """Generate all Java methods."""
    methods = [_java_method(cmd) for cmd in specs]
    return "\n\n".join(methods)


# ---------------------------------------------------------------------------
# Go generation
# ---------------------------------------------------------------------------


def _go_method_name(cmd: CommandSpec) -> str:
    """Convert verb_qualifier to PascalCase."""
    parts = f"{cmd.verb.lower()}_{cmd.mqsc_qualifier.lower()}".split("_")
    return "".join(p.capitalize() for p in parts)


def _go_method(cmd: CommandSpec) -> str:
    """Generate one Go method."""
    method_name = _go_method_name(cmd)
    command_label = f"{cmd.verb} {cmd.mqsc_qualifier}"

    lines: list[str] = []

    if cmd.pattern == "singleton":
        lines.append(f"// {method_name} executes the {command_label} command.")
        lines.append(
            f"func (session *Session) {method_name}(ctx context.Context,"
            f" opts ...CommandOption) (map[string]any, error) {{"
        )
        lines.append("	config := buildCommandConfig(opts)")
        lines.append(
            f'	objects, err := session.mqscCommand(ctx, "{cmd.verb}",'
            f' "{cmd.mqsc_qualifier}", nil,'
            f" config.requestParameters, config.responseParameters, nil, true)"
        )
        lines.append("	if err != nil {")
        lines.append("		return nil, err")
        lines.append("	}")
        lines.append("	if len(objects) == 0 {")
        lines.append("		return nil, nil")
        lines.append("	}")
        lines.append("	return objects[0], nil")
        lines.append("}")
    elif cmd.pattern == "list" and cmd.name_default:
        lines.append(
            f"// {method_name} executes the {command_label} command."
            f' Name defaults to "{cmd.name_default}" if empty.'
        )
        lines.append(
            f"func (session *Session) {method_name}(ctx context.Context,"
            f" name string, opts ...CommandOption) ([]map[string]any, error) {{"
        )
        lines.append("	config := buildCommandConfig(opts)")
        lines.append("	displayName := name")
        lines.append('	if displayName == "" {')
        lines.append(f'		displayName = "{cmd.name_default}"')
        lines.append("	}")
        lines.append(
            f'	return session.mqscCommand(ctx, "{cmd.verb}",'
            f' "{cmd.mqsc_qualifier}", &displayName,'
            f" config.requestParameters, config.responseParameters,"
            f" config.where, true)"
        )
        lines.append("}")
    elif cmd.pattern == "list":
        lines.append(f"// {method_name} executes the {command_label} command.")
        lines.append(
            f"func (session *Session) {method_name}(ctx context.Context,"
            f" name string, opts ...CommandOption) ([]map[string]any, error) {{"
        )
        lines.append(f'	return session.displayList(ctx, "{cmd.mqsc_qualifier}", name, opts)')
        lines.append("}")
    elif cmd.pattern == "required_name":
        lines.append(f"// {method_name} executes the {command_label} command. Name is required.")
        lines.append(
            f"func (session *Session) {method_name}(ctx context.Context,"
            f" name string, opts ...CommandOption) error {{"
        )
        lines.append(
            f'	return session.voidCommand(ctx, "{cmd.verb}",'
            f' "{cmd.mqsc_qualifier}", &name, opts)'
        )
        lines.append("}")
    elif cmd.pattern == "no_name":
        lines.append(f"// {method_name} executes the {command_label} command.")
        lines.append(
            f"func (session *Session) {method_name}(ctx context.Context,"
            f" opts ...CommandOption) error {{"
        )
        lines.append(
            f'	return session.voidCommand(ctx, "{cmd.verb}", "{cmd.mqsc_qualifier}", nil, opts)'
        )
        lines.append("}")
    else:  # optional_name
        lines.append(f"// {method_name} executes the {command_label} command.")
        lines.append(
            f"func (session *Session) {method_name}(ctx context.Context,"
            f" name string, opts ...CommandOption) error {{"
        )
        lines.append(
            f'	return session.voidCommandOptionalName(ctx, "{cmd.verb}",'
            f' "{cmd.mqsc_qualifier}", name, opts)'
        )
        lines.append("}")

    return "\n".join(lines)


def generate_go(specs: list[CommandSpec]) -> str:
    """Generate all Go methods."""
    methods = [_go_method(cmd) for cmd in specs]
    return "\n\n".join(methods)


# ---------------------------------------------------------------------------
# Rust generation
# ---------------------------------------------------------------------------


def _rust_method(cmd: CommandSpec) -> str:
    """Generate one Rust method."""
    method_name = f"{cmd.verb.lower()}_{cmd.mqsc_qualifier.lower()}"
    command_label = f"{cmd.verb} {cmd.mqsc_qualifier}"

    lines: list[str] = []
    lines.append(f"    /// Execute the MQSC `{command_label}` command.")

    if cmd.pattern == "singleton":
        lines.append("    pub fn " + method_name + "(")
        lines.append("        &mut self,")
        lines.append("        request_parameters: Option<&HashMap<String, Value>>,")
        lines.append("        response_parameters: Option<&[&str]>,")
        lines.append("    ) -> Result<Option<HashMap<String, Value>>> {")
        lines.append("        let objects = self.mqsc_command(")
        lines.append(f'            "{cmd.verb}",')
        lines.append(f'            "{cmd.mqsc_qualifier}",')
        lines.append("            None,")
        lines.append("            request_parameters,")
        lines.append("            response_parameters,")
        lines.append("            None,")
        lines.append("        )?;")
        lines.append("        Ok(objects.into_iter().next())")
        lines.append("    }")
    elif cmd.pattern == "list":
        lines.append("    pub fn " + method_name + "(")
        lines.append("        &mut self,")
        lines.append("        name: Option<&str>,")
        lines.append("        request_parameters: Option<&HashMap<String, Value>>,")
        lines.append("        response_parameters: Option<&[&str]>,")
        lines.append("        where_clause: Option<&str>,")
        lines.append("    ) -> Result<Vec<HashMap<String, Value>>> {")
        if cmd.name_default:
            lines.append("        self.mqsc_command(")
            lines.append(f'            "{cmd.verb}",')
            lines.append(f'            "{cmd.mqsc_qualifier}",')
            lines.append(f'            Some(name.unwrap_or("{cmd.name_default}")),')
            lines.append("            request_parameters,")
            lines.append("            response_parameters,")
            lines.append("            where_clause,")
            lines.append("        )")
        else:
            lines.append("        self.mqsc_command(")
            lines.append(f'            "{cmd.verb}",')
            lines.append(f'            "{cmd.mqsc_qualifier}",')
            lines.append("            name,")
            lines.append("            request_parameters,")
            lines.append("            response_parameters,")
            lines.append("            where_clause,")
            lines.append("        )")
        lines.append("    }")
    elif cmd.pattern == "required_name":
        lines.append("    pub fn " + method_name + "(")
        lines.append("        &mut self,")
        lines.append("        name: &str,")
        lines.append("        request_parameters: Option<&HashMap<String, Value>>,")
        lines.append("        response_parameters: Option<&[&str]>,")
        lines.append("    ) -> Result<()> {")
        lines.append("        self.mqsc_command(")
        lines.append(f'            "{cmd.verb}",')
        lines.append(f'            "{cmd.mqsc_qualifier}",')
        lines.append("            Some(name),")
        lines.append("            request_parameters,")
        lines.append("            response_parameters,")
        lines.append("            None,")
        lines.append("        )?;")
        lines.append("        Ok(())")
        lines.append("    }")
    elif cmd.pattern == "no_name":
        lines.append("    pub fn " + method_name + "(")
        lines.append("        &mut self,")
        lines.append("        request_parameters: Option<&HashMap<String, Value>>,")
        lines.append("        response_parameters: Option<&[&str]>,")
        lines.append("    ) -> Result<()> {")
        lines.append("        self.mqsc_command(")
        lines.append(f'            "{cmd.verb}",')
        lines.append(f'            "{cmd.mqsc_qualifier}",')
        lines.append("            None,")
        lines.append("            request_parameters,")
        lines.append("            response_parameters,")
        lines.append("            None,")
        lines.append("        )?;")
        lines.append("        Ok(())")
        lines.append("    }")
    else:  # optional_name
        lines.append("    pub fn " + method_name + "(")
        lines.append("        &mut self,")
        lines.append("        name: Option<&str>,")
        lines.append("        request_parameters: Option<&HashMap<String, Value>>,")
        lines.append("        response_parameters: Option<&[&str]>,")
        lines.append("    ) -> Result<()> {")
        lines.append("        self.mqsc_command(")
        lines.append(f'            "{cmd.verb}",')
        lines.append(f'            "{cmd.mqsc_qualifier}",')
        lines.append("            name,")
        lines.append("            request_parameters,")
        lines.append("            response_parameters,")
        lines.append("            None,")
        lines.append("        )?;")
        lines.append("        Ok(())")
        lines.append("    }")

    return "\n".join(lines)


def generate_rust(specs: list[CommandSpec]) -> str:
    """Generate all Rust methods."""
    methods = [_rust_method(cmd) for cmd in specs]
    return "\n\n".join(methods)


# ---------------------------------------------------------------------------
# Language dispatch
# ---------------------------------------------------------------------------

LANGUAGES = ("python", "ruby", "java", "go", "rust")

GENERATORS: dict[str, object] = {
    "python": generate_python,
    "ruby": generate_ruby,
    "java": generate_java,
    "go": generate_go,
    "rust": generate_rust,
}


def generate(
    language: str,
    specs: list[CommandSpec],
    *,
    mapping_pages: frozenset[str] | None = None,
) -> str:
    """Generate command methods for the given language."""
    if language == "python":
        return generate_python(specs, mapping_pages or frozenset())
    if language == "ruby":
        return generate_ruby(specs)
    if language == "java":
        return generate_java(specs)
    if language == "go":
        return generate_go(specs)
    if language == "rust":
        return generate_rust(specs)
    msg = f"Unsupported language: {language}"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Marker-based file update
# ---------------------------------------------------------------------------

MARKERS: dict[str, tuple[str, str]] = {
    "python": ("    # BEGIN GENERATED MQSC METHODS", "    # END GENERATED MQSC METHODS"),
    "ruby": ("        # BEGIN GENERATED MQSC METHODS", "        # END GENERATED MQSC METHODS"),
    "java": ("  // BEGIN GENERATED MQSC METHODS", "  // END GENERATED MQSC METHODS"),
    "go": ("// BEGIN GENERATED MQSC METHODS", "// END GENERATED MQSC METHODS"),
    "rust": ("    // BEGIN GENERATED MQSC METHODS", "    // END GENERATED MQSC METHODS"),
}


def update_file(target_path: Path, language: str, generated: str) -> bool:
    """Replace content between markers in target file. Returns True if changed."""
    begin_marker, end_marker = MARKERS[language]
    source = target_path.read_text(encoding="utf-8")

    begin_idx = source.index(begin_marker)
    end_idx = source.index(end_marker)

    new_source = (
        source[:begin_idx]
        + begin_marker
        + "\n"
        + generated
        + "\n\n"
        + end_marker
        + source[end_idx + len(end_marker) :]
    )

    changed = new_source != source
    if changed:
        target_path.write_text(new_source, encoding="utf-8")
    return changed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Generate MQSC command methods from mapping-data.json."
    )
    parser.add_argument(
        "--language",
        required=True,
        choices=LANGUAGES,
        help="Target language for code generation.",
    )
    parser.add_argument(
        "--mapping-data",
        required=True,
        type=Path,
        help="Path to mapping-data.json.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Target source file to update (between markers). If omitted, prints to stdout.",
    )
    parser.add_argument(
        "--mapping-pages-dir",
        type=Path,
        default=None,
        help="Directory containing Sphinx mapping .md files (Python only).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that generated output matches the target file (exit 1 if different).",
    )
    return parser.parse_args(argv)


def _resolve_mapping_pages(mapping_pages_dir: Path | None) -> frozenset[str]:
    """Discover available mapping pages from a directory."""
    if mapping_pages_dir is None or not mapping_pages_dir.is_dir():
        return frozenset()
    return frozenset(
        p.stem for p in mapping_pages_dir.iterdir() if p.suffix == ".md" and p.stem != "index"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.mapping_data.is_file():
        print(f"Error: mapping-data.json not found: {args.mapping_data}", file=sys.stderr)
        return 1

    specs = load_commands(args.mapping_data)
    mapping_pages = _resolve_mapping_pages(args.mapping_pages_dir)
    generated = generate(args.language, specs, mapping_pages=mapping_pages)

    if args.target is not None:
        if args.check:
            return _check_target(args.target, args.language, generated)
        changed = update_file(args.target, args.language, generated)
        status = "updated" if changed else "unchanged"
        print(f"{args.target}: {status} ({len(specs)} methods)")
    else:
        print(generated)

    return 0


def _check_target(target: Path, language: str, generated: str) -> int:
    """Verify that the target file matches the generated output."""
    begin_marker, end_marker = MARKERS[language]
    source = target.read_text(encoding="utf-8")

    begin_idx = source.index(begin_marker)
    end_idx = source.index(end_marker)

    expected = (
        source[:begin_idx]
        + begin_marker
        + "\n"
        + generated
        + "\n\n"
        + end_marker
        + source[end_idx + len(end_marker) :]
    )

    if source == expected:
        print(f"{target}: up to date")
        return 0

    print(f"{target}: out of date (run st-generate-commands to update)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
