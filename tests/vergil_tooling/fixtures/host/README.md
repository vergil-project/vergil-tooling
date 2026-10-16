# Host-probe output fixtures

These files are probe outputs that the `vrg-host` parsers are tested against
(epic vergil-project/.github#167). `test_host_methods.py` reads them through
`_FIXTURES`, and `test_vrg_host.py` replays the captured ones through stub host
tools to drive `vrg-host status` end to end.

## Captured from a live host

Captured on 2026-10-16 (#3261) on the maintainer's macOS host: Darwin 25.5.0,
macOS 26.5.2, Apple silicon, Homebrew 7.0.9. Each file is the exact stdout of
its command. Nothing was edited, and the `brew info` JSON is untrimmed.

| Fixture | Command | Tool version / notes |
| --- | --- | --- |
| `brew-list-versions-gh.txt` | `brew list --formula --versions gh` | gh 2.102.0 |
| `brew-list-versions-multi-keg-python314.txt` | `brew list --formula --versions python@3.14` | Two kegs, **not sorted** (`3.14.6 3.14.4_1`) |
| `brew-list-versions-uv.txt` | `brew list --formula --versions uv` | uv 0.11.8 |
| `brew-info-formula-gh.json` | `brew info --json=v2 --formula gh` | Stable 2.102.0, revision 0 |
| `brew-info-formula-python314.json` | `brew info --json=v2 --formula python@3.14` | Stable 3.14.8, revision 2; installed 3.14.4_1 and 3.14.6 |
| `brew-info-formula-uv.json` | `brew info --json=v2 --formula uv` | Homebrew stable 0.13.0 (upstream was 0.13.1) |
| `brew-info-cask-docker-desktop.json` | `brew info --json=v2 --cask docker-desktop` | `version` `4.94.0,241994`, installed `4.71.0,225177`, `auto_updates: true` |
| `docker-desktop-info-plist-version.txt` | `defaults read /Applications/Docker.app/Contents/Info.plist CFBundleShortVersionString` | Docker Desktop 4.71.0 |
| `docker-version.txt` | `docker --version` | Docker CLI 29.4.1 (not the Desktop version) |
| `uv-tool-list.txt` | `uv tool list` | vergil-tooling v2.1.241 |
| `uv-tool-list-outdated.txt` | `uv tool list --outdated` | Empty, exit 0: the git-installed vergil-tooling is never reported |
| `uv-version.txt` | `uv --version` | `(Homebrew …)`: uv is Homebrew-installed |
| `gh-version.txt` | `gh --version` | gh 2.102.0 |
| `gcloud-version-core.txt` | `gcloud version --format=value(core)` | core 2026.09.25 |
| `gcloud-components-list-core-latest.txt` | `gcloud components list --filter=id=core --format=value(latest_version_string)` | stdout only; gcloud also prints an SDK-version notice on stderr |
| `sw-vers-product-version.txt` | `sw_vers -productVersion` | macOS 26.5.2 |
| `softwareupdate-list-minor-and-major-available.txt` | `softwareupdate --list` | Two macOS updates (26.7.1 and 27.0.1) plus non-OS items (Safari, Pro Video Formats) |
| `vrg-host-status-before-3261.txt` | `vrg-host status`, run from a worktree build | Reference only: the real end-to-end report (exit 1, overall WARN) **before** #3261 changed the uv and Docker entries |

The host-status reference shows the old `uv` (custom, `uv self update`, latest
from GitHub releases) and `docker` (CLI version, no latest feed) entries.
`test_vrg_host.py` checks that the rows #3261 did not change (gh, gcloud,
vergil-tooling) still render exactly as the real host printed them. The macos
row there is the old single-latest output (`26.5.2 -> 27.0.1 behind (drift)`);
since #3269 the same capture renders every pending level
(`behind (drift): minor 26.7.1, major 27.0.1`), and the test asserts both.

`gh api repos/astral-sh/uv/releases/latest --jq .tag_name` was also captured
(it printed `0.13.1`). It is not kept, because no probe uses it once uv is
managed as a Homebrew formula.

## Recorded on a live host (#3300)

Recorded on 2026-10-16 (#3300) by a read-only probe of the same macOS host, for
the git, bash, Xcode Command Line Tools and Claude Code entries. The issue
records the values these commands printed, not whole output files, so each file
holds the recorded value in the command's output format. Where the issue
quotes a line verbatim, the file is that line exactly; for a `brew list` value
the file is the line Homebrew prints for it (`<name> <version>`).

| Fixture | Command | Tool version / notes |
| --- | --- | --- |
| `brew-list-versions-git.txt` | `brew list --versions git` | git 2.54.0 (`/opt/homebrew/bin/git`, ahead of `/usr/bin/git`) |
| `brew-list-versions-bash.txt` | `brew list --versions bash` | bash 5.3.9 (`/opt/homebrew/bin/bash`, ahead of `/bin/bash` 3.2) |
| `brew-list-versions-cask-claude-code.txt` | `brew list --cask --versions claude-code` | Claude Code 2.1.280 |
| `claude-version.txt` | `claude --version` | Verbatim: `2.1.280 (Claude Code)` |
| `pkgutil-pkg-info-clt-version-line.txt` | `pkgutil --pkg-info=com.apple.pkg.CLTools_Executables` | Verbatim, the `version:` line only (`26.6.0.0.1781586589`); the other lines were not recorded |

## Constructed, not captured

These have no real counterpart on the capture host, so they were written by
hand from the documented output format.

| Fixture | Command it stands in for | Why it is constructed |
| --- | --- | --- |
| `softwareupdate-list-none.txt` | `softwareupdate --list 2>&1` with nothing pending | The host had updates pending |
| `softwareupdate-list-non-macos-only.txt` | `softwareupdate --list 2>&1` with only a non-OS update pending | The host had macOS updates pending |
| `softwareupdate-list-patch-only.txt` | `softwareupdate --list 2>&1` offering only a patch update (26.5.3 on 26.5.2) | The host's pending macOS updates were a minor and a major (#3269) |
| `uv-tool-list-outdated-listed.txt` | `uv tool list --outdated` listing an outdated tool | The real output was empty, so the `[latest: …]` line format is still unverified |
| `softwareupdate-list-clt-available.txt` | `softwareupdate --list 2>&1` offering a Command Line Tools update (26.7) and a macOS update | The host had no CLT update pending. The title follows the newer `Command Line Tools for Xcode <version>` form; `softwareupdate-list-non-macos-only.txt` has the older title without a version |
| `pkgutil-pkg-info-clt.txt` | `pkgutil --pkg-info=com.apple.pkg.CLTools_Executables`, full output | Only the `version:` line was recorded (#3300); the other lines and the install time are written from pkgutil's documented format |
| `brew-info-cask-claude-code.json` | `brew info --json=v2 --cask claude-code` | Not run on the host. Built from the Homebrew API record (<https://formulae.brew.sh/api/cask/claude-code.json>, fetched 2026-10-16: `version` 2.1.292, `auto_updates` null) wrapped in the `brew info` shape, with the host's installed 2.1.280, and trimmed to the keys the parsers read |

Tests also build a few synthetic edge cases inline (for example an unreadable
keg name, or a revision-only rebuild). They are labelled where they appear.

## Re-capturing on a Mac

Run the command in the table and replace the file with its output exactly,
keeping the file name. You can trim keys from the `brew info` JSON that the
parser does not read, but keep `versions`, `revision`, `version`, `installed`
and `auto_updates` exactly as printed. Then update the table with the date and
tool version. If the real output breaks a parser, fix the parser. Never edit a
fixture to make a test pass.
