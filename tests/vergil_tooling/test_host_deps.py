"""Tests for vergil_tooling.lib.host_deps (host-dependency manifest loader)."""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import host_deps
from vergil_tooling.lib.host_deps import AuxCheck, HostPin, ManifestError

if TYPE_CHECKING:
    from pathlib import Path

_DEP = '[[dependency]]\nname = "x"\nwhy = "w"\nmethod = "custom"\n'

_PIN = (
    "[dependency.pin]\n"
    'constraint = "<=1.x"\n'
    'inducing_release = "2.0"\n'
    "deterministic = true\n"
    'reason = "breaks us"\n'
    'state = "under-evaluation"\n'
    'tracking_issue = "vergil-project/.github#155"\n'
    'pinned_date = "2026-10-15"\n'
)


def _use(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(host_deps, "_read_manifest_text", lambda: text)


def _load_error(monkeypatch: pytest.MonkeyPatch, text: str, match: str) -> None:
    _use(monkeypatch, text)
    with pytest.raises(ManifestError, match=match):
        host_deps.load_manifest()


# -- packaged manifest ---------------------------------------------------------


def test_load_manifest_parses_curated_entries() -> None:
    """The curated working set (plan Task 7, spec §4/§11): name → (method, upgrade, latest)."""
    deps = host_deps.load_manifest()
    by_name = {d.name: (d.method, d.upgrade, d.latest) for d in deps}
    assert by_name == {
        "uv": ("custom", "auto", "probe"),
        "gh": ("brew-formula", "auto", "probe"),
        "gcloud": ("custom", "auto", "probe"),
        "lima": ("brew-formula", "auto", "probe"),
        "docker": ("custom", "manual", "unmanaged"),
        "opentofu": ("brew-formula", "auto", "probe"),
        "git-cliff": ("brew-formula", "auto", "probe"),
        "vergil-tooling": ("uv-tool", "auto", "unmanaged"),
        "macos": ("custom", "manual", "probe"),
    }
    assert all(d.why for d in deps)
    assert all(d.pin is None for d in deps)  # pins live in the host-local file


@pytest.mark.parametrize(
    "excluded",
    ["shellcheck", "hadolint", "yamllint", "actionlint", "markdownlint", "az", "jq", "git"],
)
def test_container_only_and_optional_tools_are_out_of_scope(excluded: str) -> None:
    # Spec §4: linters run only inside the dev container; az is an optional
    # provider; git/jq ship with macOS. None earns a host manifest entry.
    assert excluded not in {d.name for d in host_deps.load_manifest()}


def test_custom_entries_declare_the_probes_they_need() -> None:
    for dep in host_deps.load_manifest():
        if dep.method != "custom":
            continue
        assert "installed_version" in dep.probes, dep.name
        assert ("latest_version" in dep.probes) == (dep.latest == "probe"), dep.name
        assert ("upgrade" in dep.probes) == (dep.upgrade == "auto"), dep.name


def test_no_manifest_command_escalates_privilege() -> None:
    """Spec §10: every manifest argv runs unprivileged."""
    for dep in host_deps.load_manifest():
        argvs = list(dep.probes.values())
        for check in dep.checks:
            argvs.append(check.probe)
            if check.remediation is not None:
                argvs.append(check.remediation)
        for argv in argvs:
            assert argv[0] not in {"sudo", "doas", "su", "pkexec"}, dep.name
            assert "sudo " not in " ".join(argv), dep.name


def test_manifest_read_only_from_package_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security invariant (spec §10): the manifest is never repo/CWD-overridable.

    Plant a hostile manifest in the CWD; loading must ignore it entirely and
    still read the packaged data. This test is the regression tripwire -- if a
    future change adds a repo-override path, it breaks and forces the security
    decision back into the open.
    """
    (tmp_path / "host_dependencies.toml").write_text(
        '[[dependency]]\nname="evil"\nwhy="x"\nmethod="custom"\n'
    )
    monkeypatch.chdir(tmp_path)
    names = {d.name for d in host_deps.load_manifest()}
    assert "evil" not in names  # CWD file ignored
    assert "uv" in names  # packaged manifest still used
    # And the resolved source is inside the package, never a loose path:
    ref = resources.files("vergil_tooling.data").joinpath("host_dependencies.toml")
    assert "vergil_tooling/data" in str(ref)


# -- full-entry parsing ----------------------------------------------------------


def test_full_entry_parses_probes_checks_and_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    text = (
        _DEP + 'upgrade = "manual"\nlatest = "unmanaged"\n'
        "[dependency.probes]\n"
        'installed_version = ["x", "--version"]\n'
        "[[dependency.check]]\n"
        'name = "c1"\nprobe = ["true"]\nremediation = ["fix", "it"]\n'
        "[[dependency.check]]\n"
        'name = "c2"\nprobe = ["false"]\n' + _PIN
    )
    _use(monkeypatch, text)
    (dep,) = host_deps.load_manifest()
    assert dep.upgrade == "manual"
    assert dep.latest == "unmanaged"
    assert dep.probes == {"installed_version": ["x", "--version"]}
    assert dep.checks == (
        AuxCheck("c1", ["true"], ["fix", "it"]),
        AuxCheck("c2", ["false"], None),
    )
    assert dep.pin == HostPin(
        constraint="<=1.x",
        inducing_release="2.0",
        deterministic=True,
        reason="breaks us",
        state="under-evaluation",
        tracking_issue="vergil-project/.github#155",
        pinned_date="2026-10-15",
    )


def test_defaults_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, '[[dependency]]\nname = "x"\nmethod = "brew-cask"\n')
    (dep,) = host_deps.load_manifest()
    assert (dep.why, dep.upgrade, dep.latest) == ("", "auto", "probe")
    assert dep.probes == {}
    assert dep.checks == ()
    assert dep.pin is None


def test_pin_defaults_and_toml_date_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    pin = (
        "[dependency.pin]\n"
        'constraint = "<=1.x"\ninducing_release = "2.0"\n'
        'reason = "r"\npinned_date = 2026-10-15\n'
    )
    _use(monkeypatch, _DEP + pin)
    (dep,) = host_deps.load_manifest()
    assert dep.pin is not None
    assert dep.pin.state == "active"
    assert dep.pin.deterministic is False
    assert dep.pin.tracking_issue is None
    assert dep.pin.pinned_date == "2026-10-15"


def test_empty_manifest_yields_no_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, "")
    assert host_deps.load_manifest() == ()


# -- enum validation ---------------------------------------------------------------


def test_unknown_method_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, '[[dependency]]\nname="x"\nwhy="w"\nmethod="wat"\n', "unknown method")


def test_missing_method_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, '[[dependency]]\nname="x"\n', "unknown method None")


def test_unknown_upgrade_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + 'upgrade = "sometimes"\n', "unknown upgrade 'sometimes'")


def test_unknown_latest_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + 'latest = "guess"\n', "unknown latest 'guess'")


def test_unknown_pin_state_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch, _DEP + _PIN.replace("under-evaluation", "forever"), "unknown state 'forever'"
    )


# -- pin validation -----------------------------------------------------------------


def test_pin_without_reason_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + _PIN.replace('reason = "breaks us"\n', ""), "without a reason")


def test_pin_with_empty_reason_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + _PIN.replace('"breaks us"', '""'), "without a reason")


def test_pin_non_string_reason_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + _PIN.replace('"breaks us"', "7"), "'reason' must be")


def test_pin_missing_constraint_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch,
        _DEP + _PIN.replace('constraint = "<=1.x"\n', ""),
        "missing required field 'constraint'",
    )


def test_pin_non_bool_deterministic_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch, _DEP + _PIN.replace("deterministic = true", 'deterministic = "yes"'), "boolean"
    )


def test_pin_missing_date_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + _PIN.replace('pinned_date = "2026-10-15"\n', ""), "pinned_date")


def test_pin_datetime_literal_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch,
        _DEP + _PIN.replace('"2026-10-15"', "2026-10-15T01:02:03"),
        "non-string field 'pinned_date'",
    )


def test_pin_bad_date_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + _PIN.replace("2026-10-15", "last week"), "not YYYY-MM-DD")


def test_pin_empty_tracking_issue_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch,
        _DEP + _PIN.replace('"vergil-project/.github#155"', '""'),
        "'tracking_issue' must be",
    )


def test_pin_unknown_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + _PIN + 'resaon = "typo"\n', "unknown key")


def test_pin_not_a_table_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + 'pin = "yes"\n', "pin: must be a table")


# -- field / shape validation ---------------------------------------------------------


def test_missing_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, '[[dependency]]\nmethod = "custom"\n', "missing required field 'name'")


def test_non_string_why_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP.replace('why = "w"', "why = 3"), "'why' must be")


def test_unknown_dependency_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + 'upgarde = "manual"\n', "unknown key")


def test_unknown_top_level_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, "dependencies = []\n" + _DEP, "manifest: unknown key")


def test_dependency_not_array_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, '[dependency]\nname = "x"\n', "must be an array of tables")


def test_dependency_entry_not_table_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, "dependency = [1]\n", "dependency #0: must be a table")


def test_duplicate_dependency_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + _DEP, "duplicate dependency name 'x'")


def test_invalid_toml_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, "[[dependency]\n", "not valid TOML")


def test_probes_not_table_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + 'probes = ["x"]\n', "probes must be a table")


@pytest.mark.parametrize("cmd", ['"x --version"', "[]", '["x", ""]', '["x", 1]'])
def test_probe_command_must_be_argv(monkeypatch: pytest.MonkeyPatch, cmd: str) -> None:
    _load_error(
        monkeypatch,
        _DEP + f"[dependency.probes]\ninstalled_version = {cmd}\n",
        "non-empty list of non-empty strings",
    )


def test_check_not_array_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + '[dependency.check]\nname = "c"\n', "array of tables")


def test_check_entry_not_table_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(monkeypatch, _DEP + "check = [1]\n", "each check must be a table")


def test_check_missing_probe_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch,
        _DEP + '[[dependency.check]]\nname = "c"\n',
        "missing required field 'probe'",
    )


def test_check_bad_remediation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch,
        _DEP + '[[dependency.check]]\nname = "c"\nprobe = ["true"]\nremediation = "fix"\n',
        "remediation must be",
    )


def test_check_unknown_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_error(
        monkeypatch,
        _DEP + '[[dependency.check]]\nname = "c"\nprobe = ["true"]\nfix = ["x"]\n',
        "unknown key",
    )


def test_duplicate_check_names_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    check = '[[dependency.check]]\nname = "c"\nprobe = ["true"]\n'
    _load_error(monkeypatch, _DEP + check + check, "duplicate check names")


def test_dataclasses_are_frozen() -> None:
    check = AuxCheck("c", ["true"], None)
    field = "name"
    with pytest.raises(AttributeError):
        setattr(check, field, "d")
