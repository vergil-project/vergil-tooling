"""Tests for vergil_tooling.lib.in_tree_audit (issue #4414).

The source-tree tests run the real child process. Each builds a vergil-tooling
"release" in a temporary directory: a copy of this package under ``src/``
whose CLAUDE.md template is changed, as a template change on develop would
change it, so the copy's templates differ from the ones this test process has
loaded (standing in for the installed release).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import vergil_tooling
from tests.vergil_tooling.test_repo_config import _write_compliant_repo
from vergil_tooling.lib import in_tree_audit
from vergil_tooling.lib.github_config import ConfigDiff, DiffItem
from vergil_tooling.lib.repo_config import CLAUDE_MD_TEMPLATE, audit_local_config

if TYPE_CHECKING:
    import pytest

_PACKAGE = Path(vergil_tooling.__file__).resolve().parent
_ADDED_LINE = "A line added to the template on develop, not yet released.\n"


def _source_release(root: Path) -> Path:
    """Copy this package to ``<root>/src`` with a changed CLAUDE.md template."""
    package = root / "src" / "vergil_tooling"
    shutil.copytree(_PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__"))
    template = package / "data" / CLAUDE_MD_TEMPLATE
    template.write_text(template.read_text(encoding="utf-8") + _ADDED_LINE, encoding="utf-8")
    return template


def _claude_md_fields(root: Path) -> set[str]:
    items = audit_local_config(root).items
    return {i.field for i in items if i.field.startswith("local.claude_md")}


def test_files_matching_the_in_tree_templates_pass(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # The repo's managed files follow the changed template, as vergil-tooling's
    # own files do in the PR that changes it.
    template = _source_release(tmp_path)
    _write_compliant_repo(tmp_path)
    (tmp_path / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n<!-- vergil:template:claude-md:begin -->\n"
        f"{template.read_text(encoding='utf-8')}<!-- vergil:template:claude-md:end -->\n",
        encoding="utf-8",
    )
    # The loaded (stand-in installed) templates report the false drift...
    assert _claude_md_fields(tmp_path) == {"local.claude_md"}
    # ...and the in-tree audit does not.
    assert in_tree_audit.run(tmp_path) == in_tree_audit.COMPLIANT
    assert "local: compliant" in capfd.readouterr().out


def test_files_drifting_from_the_in_tree_templates_fail(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # The repo's managed files still follow the old template: real drift from
    # the templates being released, though the loaded ones see none.
    _source_release(tmp_path)
    _write_compliant_repo(tmp_path)
    assert audit_local_config(tmp_path).is_compliant()
    assert in_tree_audit.run(tmp_path) == in_tree_audit.NON_COMPLIANT
    out = capfd.readouterr().out
    assert "local: NON-COMPLIANT (1 issues)" in out
    assert "local.claude_md:" in out


def test_this_checkout_passes_its_own_in_tree_audit(capfd: pytest.CaptureFixture[str]) -> None:
    root = Path(__file__).resolve().parents[2]
    assert in_tree_audit.run(root) == in_tree_audit.COMPLIANT, capfd.readouterr()


def test_a_root_without_a_source_tree_is_refused(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Nothing under <root>/src: the child finds this module elsewhere (here the
    # test environment's install) and refuses to run, never falling back to it.
    # The code is neither COMPLIANT nor NON_COMPLIANT.
    assert in_tree_audit.run(tmp_path) == 2
    assert f"must run vergil_tooling from {tmp_path / 'src'}" in capfd.readouterr().err


def test_main_refuses_a_package_loaded_from_elsewhere(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # In this process vergil_tooling is not under <tmp_path>/src.
    assert in_tree_audit.main([str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert f"must run vergil_tooling from {tmp_path / 'src'}" in err


def test_main_reports_compliance(capsys: pytest.CaptureFixture[str]) -> None:
    # In-process, against the root this process's package was loaded from.
    root = _PACKAGE.parents[1]
    assert in_tree_audit.main([str(root)]) == in_tree_audit.COMPLIANT
    assert "local: compliant" in capsys.readouterr().out


def test_main_reports_drift(capsys: pytest.CaptureFixture[str]) -> None:
    drift = ConfigDiff(items=[DiffItem(field="local.claude_md", expected="x", actual="y")])
    with patch("vergil_tooling.lib.repo_config.audit_local_config", return_value=drift):
        assert in_tree_audit.main([str(_PACKAGE.parents[1])]) == in_tree_audit.NON_COMPLIANT
    assert "local.claude_md: expected='x', actual='y'" in capsys.readouterr().out
