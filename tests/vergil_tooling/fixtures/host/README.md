# Host-probe output fixtures

These files are probe outputs that the `vrg-host` parsers are tested against
(epic vergil-project/.github#167, plan Task 7). The tests in
`test_host_methods.py` read them through `_FIXTURES`.

## Provenance: constructed, not captured

**None of these files was captured from a live macOS host.** Task 7 was
implemented on a Linux VM that has no Homebrew, gcloud, Lima, Docker Desktop
or macOS. Each file was written by hand from the tool's documented or
well-known output format. A file stays on the re-capture list below until
someone replaces it with real output from a Mac and moves it to the
captured list.

| Fixture | Command it stands in for |
| --- | --- |
| `brew-list-versions-gh.txt` | `brew list --formula --versions gh` |
| `brew-list-versions-multi-keg.txt` | `brew list --formula --versions python@3.12` with old kegs left over |
| `brew-info-formula-gh.json` | `brew info --json=v2 --formula gh` (trimmed to the keys parsed) |
| `brew-info-formula-revision.json` | `brew info --json=v2 --formula python@3.12` (revision 1) |
| `brew-info-cask-docker.json` | `brew info --json=v2 --cask docker` (trimmed) |
| `uv-tool-list.txt` | `uv tool list` |
| `uv-tool-list-outdated.txt` | `uv tool list --outdated` |
| `uv-version.txt` | `uv --version` |
| `gh-api-uv-latest.txt` | `gh api repos/astral-sh/uv/releases/latest --jq .tag_name` |
| `gh-version.txt` | `gh --version` |
| `gcloud-version-core.txt` | `gcloud version --format=value(core)` |
| `gcloud-components-list-core-latest.txt` | `gcloud components list --filter=id=core --format=value(latest_version_string)` |
| `docker-version.txt` | `docker --version` |
| `sw-vers-product-version.txt` | `sw_vers -productVersion` |
| `softwareupdate-list-macos-available.txt` | `softwareupdate --list 2>&1` with a macOS update pending |
| `softwareupdate-list-none.txt` | `softwareupdate --list 2>&1` with nothing pending |
| `softwareupdate-list-non-macos-only.txt` | `softwareupdate --list 2>&1` with only a non-OS update pending |

## Captured from a live host

None yet.

## Re-capturing on a Mac

Run the command in the table and replace the file with its output exactly,
keeping the file name. For the `brew info` JSON you can trim keys the parser
does not read, but keep `versions`, `revision` and `version` exactly as
printed. Then move the row to "Captured from a live host" and note the date
and tool version. If the real output breaks a parser, fix the parser. Never
edit the fixture to make a test pass.
