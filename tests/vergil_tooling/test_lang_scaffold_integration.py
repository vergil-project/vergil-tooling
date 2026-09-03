"""Gated container integration test for the cpp born-green scaffold.

Epic vergil-project/.github#342, Task 2 (§2g). The unit tests in
``test_lang_scaffold.py`` prove ``scaffold_language`` with the resolve/verify
mocked; this test runs the **real** lock-resolve and ``vrg-validate`` inside the
dev container against a freshly-scaffolded cpp repo and asserts it is born green
with a resolved ``conan.lock`` and no Conan generator output at the source root.

It needs the cpp toolchain (Conan) plus a container runtime and ConanCenter
network access, so it is gated exactly like the other binary-gated integration
checks (cf. the ``markdownlint`` skipif in ``test_validate_common``): skipped
unless ``conan`` is on ``PATH``. That binary is absent in the Python dev
container that measures coverage and on a plain host, so the test is skipped
there — and being skipped never drops coverage, because every line of
``lang_scaffold`` is already covered by the mocked unit tests. This only adds
real end-to-end confidence where a cpp environment exists.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import lang_scaffold
from vergil_tooling.lib.repo_init import RepoInitContext

if TYPE_CHECKING:
    from pathlib import Path

_HAS_CPP_TOOLCHAIN = shutil.which("conan") is not None

_VERGIL_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "gitflow"
release-model = "none"
primary-language = "cpp"

[ci]
versions = ["clang-20"]
integration-tests = false
"""


@pytest.mark.skipif(
    not _HAS_CPP_TOOLCHAIN,
    reason="conan (the cpp toolchain) is not on PATH; the cpp born-green "
    "integration test needs a cpp environment to resolve locks and run vrg-validate",
)
def test_cpp_scaffold_is_born_green(tmp_path: Path) -> None:
    # The repo asserts cpp so vrg-container-run selects the cpp toolchain image.
    (tmp_path / "vergil.toml").write_text(_VERGIL_TOML)

    ctx = RepoInitContext(org="acme", name="mq-protocol-gateway")
    ctx.primary_language = "cpp"
    ctx.work_dir = tmp_path

    # Real resolve + real vrg-validate. scaffold_language raises ScaffoldError on
    # any non-zero, so returning normally *is* the exit-0 born-green assertion.
    lang_scaffold.scaffold_language(ctx)

    # The skeleton was stamped and the lock resolved.
    assert (tmp_path / "CMakeLists.txt").is_file()
    assert (tmp_path / "conanfile.txt").is_file()
    assert (tmp_path / "src" / "mq_protocol_gateway.cpp").is_file()
    assert (tmp_path / "conan.lock").is_file()

    # No Conan generator output leaked to the source root — it lives under
    # build/ (#2912). Only the build dir may hold the toolchain file.
    assert not (tmp_path / "conan_toolchain.cmake").exists()
    assert not list(tmp_path.glob("*-config.cmake"))
    assert not list(tmp_path.glob("Find*.cmake"))
