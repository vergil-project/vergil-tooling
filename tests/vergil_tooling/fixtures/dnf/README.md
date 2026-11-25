# dnf metadata-refresh fixtures

`test_package_repo_setup.py` checks `repo_setup.dnf_refresh`, and the
signature-failure match it retries on, against these files (#4663). Each pair
is the exact stdout and stderr of one dnf run. Nothing was edited, and every
run exited 1.

## How they were captured

Captured on 2026-11-25 in `ghcr.io/vergil-project/prod-pkg-rhel:9` (RHEL 9.9,
dnf 4.14.0-37.el9, librepo 1.19.0) and `ghcr.io/vergil-project/prod-pkg-rhel:10`
(RHEL 10.3, dnf 4.20.0-28.el10, librepo 1.19.0), on arm64. The live vergil rpm
repository's `repodata/` for each release was mirrored into the container and
served over HTTP on `127.0.0.1:8080`, behind a `[vergil]` repo with
`gpgcheck=1`, `repo_gpgcheck=1` and the published `keys/vergil.asc` as its
`gpgkey`. With the pair unchanged (`gpg --verify` printed `Good signature`),
`dnf -y makecache --refresh --repo vergil` exited 0 on both releases. Each case
then changed one thing:

| Fixture (`el9-` / `el10-`) | What was changed | Command |
| --- | --- | --- |
| `mismatch-makecache` | A line appended to `repomd.xml`, keeping its `.asc` (`gpg --verify` printed `BAD signature`): the publish cache race | `dnf -y makecache --refresh --repo vergil` |
| `decline-import` | Nothing; a fresh dnf cache and no `-y`, so dnf declined the key import with no terminal to answer | `dnf makecache --refresh --repo vergil` |
| `missing-asc` | `repomd.xml.asc` removed (404) | `dnf -y makecache --refresh --repo vergil` |
| `missing-repomd` | `repomd.xml` removed (404) | `dnf -y makecache --refresh --repo vergil` |
| `unreachable` | The repo's `baseurl` pointed at a port nothing listens on | `dnf -y makecache --refresh --repo vergil` |
| `unknown-repo` | Nothing; a repo id that does not exist | `dnf -y makecache --refresh --repo nosuchrepo` |

What the captures show:

- The race reads `repomd.xml GPG signature verification error: Bad GPG
  signature` on RHEL 9 and `... Bad PGP signature` on RHEL 10.
- On RHEL 9 a declined key import prints the race's exact error, so
  `dnf_refresh` always passes `-y`. RHEL 10 says `Signing key not found`.
- A scoped `dnf install -y --refresh --repo vergil` printed the same error as
  `makecache` for the race.
- Once the pair matched again, the next refresh succeeded.

## Re-capturing

Run `capture.sh` in each `pkg-rhel` image (`nerdctl run --rm -v "$PWD:/cap:ro"
<image> bash /cap/capture.sh`, from this directory); it prints every case, the
controls included. Replace each file with the
real output, keeping its name. Then update the versions above. If the real
output breaks the match, fix the match. Never edit a fixture to make a test
pass.
