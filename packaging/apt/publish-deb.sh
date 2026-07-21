#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  printf 'usage: %s DEB REPOSITORY_ROOT GPG_KEY_ID\n' "$0" >&2
  exit 2
fi

deb=$1
repository=$2
gpg_key=$3

command -v apt-ftparchive >/dev/null
command -v dpkg-scanpackages >/dev/null
command -v gpg >/dev/null
test -f "$deb"
test -d "$repository"

package=$(basename "$deb")
pool="$repository/pool/main/n/nebula"
install -d -m 0755 "$pool"
install -m 0644 "$deb" "$pool/$package"

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT HUP INT TERM
binary="$stage/dists/stable/main/binary-amd64"
install -d -m 0755 "$binary"

(
  cd "$repository"
  dpkg-scanpackages --arch amd64 pool /dev/null
) > "$binary/Packages"
gzip -n -9 -c "$binary/Packages" > "$binary/Packages.gz"

release="$stage/dists/stable/Release"
apt-ftparchive \
  -o APT::FTPArchive::Release::Origin=Nebula \
  -o APT::FTPArchive::Release::Label=Nebula \
  -o APT::FTPArchive::Release::Suite=stable \
  -o APT::FTPArchive::Release::Codename=stable \
  -o APT::FTPArchive::Release::Architectures=amd64 \
  -o APT::FTPArchive::Release::Components=main \
  release "$stage/dists/stable" > "$release"

gpg --batch --yes --local-user "$gpg_key" --armor --detach-sign \
  --output "$stage/dists/stable/Release.gpg" "$release"
gpg --batch --yes --local-user "$gpg_key" --armor --clearsign \
  --output "$stage/dists/stable/InRelease" "$release"

rm -rf "$repository/dists/stable"
install -d -m 0755 "$repository/dists"
mv "$stage/dists/stable" "$repository/dists/stable"
gpg --batch --yes --local-user "$gpg_key" --armor --export \
  --output "$repository/nebula-archive-keyring.asc"
