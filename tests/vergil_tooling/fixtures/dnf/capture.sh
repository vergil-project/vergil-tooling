#!/bin/bash
# Capture real dnf output for a mismatched repomd.xml / repomd.xml.asc pair,
# against a local mirror of the live vergil rpm repodata served over HTTP.
# Run as root in a pkg-rhel image; see README.md.
set -u
BASE=https://vergil-project.github.io/packages
ARCH=$(uname -m)
REL=$(rpm -E %rhel)
SRV=/srv/repo
LIVE="$BASE/rpm/el$REL/$ARCH"
R="$SRV/rpm/el$REL/$ARCH"
mkdir -p "$R/repodata"
curl -fsSL "$BASE/keys/vergil.asc" -o /etc/pki/rpm-gpg/RPM-GPG-KEY-vergil-test
curl -fsSL "$LIVE/repodata/repomd.xml" -o "$R/repodata/repomd.xml"
curl -fsSL "$LIVE/repodata/repomd.xml.asc" -o "$R/repodata/repomd.xml.asc"
grep -o 'href="[^"]*"' "$R/repodata/repomd.xml" | sed 's/href="//;s/"$//' | while read -r f; do
  mkdir -p "$R/$(dirname "$f")"
  curl -fsSL "$LIVE/$f" -o "$R/$f"
done
cp -a "$R/repodata" /srv/repodata.orig
(cd "$SRV" && python3 -m http.server 8080 --bind 127.0.0.1 >/dev/null 2>&1 &)
sleep 1
# shellcheck disable=SC2016 # dnf expands $releasever and $basearch itself
printf '%s\n' '[vergil]' 'name=vergil packages (capture)' \
  'baseurl=http://127.0.0.1:8080/rpm/el$releasever/$basearch' 'enabled=1' 'gpgcheck=1' \
  'repo_gpgcheck=1' 'gpgkey=file:///etc/pki/rpm-gpg/RPM-GPG-KEY-vergil-test' \
  >/etc/yum.repos.d/vergil.repo
run() {
  echo "=== CASE $1: $2"
  shift 2
  echo "--- argv: $*"
  "$@" >/tmp/out 2>/tmp/err
  echo "--- exit $?"
  echo "--- stdout"
  cat /tmp/out
  echo "--- stderr"
  cat /tmp/err
  echo "=== END"
}
reset() {
  rm -rf "$R/repodata"
  cp -a /srv/repodata.orig "$R/repodata"
}
verify() {
  gpg --batch -q --homedir /tmp/g --import /etc/pki/rpm-gpg/RPM-GPG-KEY-vergil-test 2>/dev/null
  local got
  got=$(gpg --batch --homedir /tmp/g --verify "$R/repodata/repomd.xml.asc" "$R/repodata/repomd.xml" 2>&1)
  echo "--- gpg --verify: $(grep -oE "(Good|BAD) signature" <<<"$got")"
}
mkdir -p /tmp/g
chmod 700 /tmp/g
cat /etc/redhat-release
rpm -q dnf librepo gnupg2
reset
verify
run decline-import "untouched repo, fresh cache, no -y (key import declined)" \
  dnf makecache --refresh --repo vergil
run control-first "untouched repo, key not yet imported" dnf -y makecache --refresh --repo vergil
run control-again "untouched repo, key imported" dnf -y makecache --refresh --repo vergil
run control-plain "untouched repo, no -y (key already imported)" \
  dnf makecache --refresh --repo vergil
# The race: an edge serves repomd.xml from one publish next to the .asc of another.
reset
printf '<!-- another publish -->\n' >>"$R/repodata/repomd.xml"
verify
run mismatch-makecache "repomd.xml from another publish than its .asc" \
  dnf -y makecache --refresh --repo vergil
run mismatch-makecache-plain "same mismatch, no -y (key already imported)" \
  dnf makecache --refresh --repo vergil
run mismatch-install "same mismatch, scoped install" \
  dnf install -y --refresh --repo vergil vergil-archive-keyring
reset
run recovered "pair consistent again after the mismatch" dnf makecache --refresh --repo vergil
# Negative controls: failures that are not the signature race.
reset
rm "$R/repodata/repomd.xml.asc"
run missing-asc "repomd.xml.asc absent (404)" dnf -y makecache --refresh --repo vergil
reset
rm "$R/repodata/repomd.xml"
run missing-repomd "repomd.xml absent (404)" dnf -y makecache --refresh --repo vergil
reset
sed -i 's#8080#8081#' /etc/yum.repos.d/vergil.repo
run unreachable "nothing listening on the baseurl port" dnf -y makecache --refresh --repo vergil
sed -i 's#8081#8080#' /etc/yum.repos.d/vergil.repo
run unknown-repo "no such repo id" dnf -y makecache --refresh --repo nosuchrepo
