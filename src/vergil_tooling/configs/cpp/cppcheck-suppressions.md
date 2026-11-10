# cppcheck suppressions

[`cppcheck-suppressions.txt`](cppcheck-suppressions.txt) is the packaged
cppcheck suppressions list for Vergil C++ repos (epic
vergil-project/.github#207, T5). The C++ lint step passes it to cppcheck as
`--suppressions-list={configs}/cpp/cppcheck-suppressions.txt`.

## The file holds no comments

The file holds one suppression ID per line and nothing else: no comment lines
and no blank lines. cppcheck 2.13.0, the version Ubuntu 24.04 (noble) ships,
rejects a comment line with `Failed to add suppression. No id.`, which fails
C++ lint for every consumer (vergil-project/vergil-tooling#4138). Later
cppcheck releases, such as 2.17.1, accept comments, but the file must work with
both. The reasons for each entry live here instead, and a test in
`tests/vergil_tooling/test_languages.py` keeps the file free of comments.

## What the suppressions are for

The list was written when the lint step ran cppcheck with `--enable=all`,
which turns on categories (`information`, `missingInclude`) that report
environmental noise rather than defects. These entries silence exactly that
noise, so the gate flags real findings. They are not a standing
defect-suppression list: a suppression for a single finding belongs inline,
through `--inline-suppr`. The lint step now passes a curated `--enable` set
instead (vergil-project/vergil-tooling#2585); the entries stay as they were.

| Suppression | Why |
|---|---|
| `missingIncludeSystem` | System headers cppcheck cannot resolve: the dependencies live in the Conan cache, not on cppcheck's include path. |
| `unmatchedSuppression` | `--enable=all` implies checks whose config is absent in a bare invocation. |
| `checkersReport` | `--enable=all` implies checks whose config is absent in a bare invocation. |
| `missingInclude` | cppcheck cannot see across the CMake and Conan boundary for every translation unit. |
