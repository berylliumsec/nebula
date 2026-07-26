#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
  printf 'usage: %s CHANNELS DOWNLOADS PUBLIC_ROOT GPG_KEY_ID\n' "$0" >&2
  exit 2
fi

channels=$1
downloads=$2
public=$3
gpg_key=$4

for command in apt-ftparchive dpkg-deb dpkg-scanpackages gpg jq python3; do
  command -v "$command" >/dev/null
done
test -f "$channels"
test -d "$downloads"
test -n "${APT_SIGNING_PASSPHRASE_FILE:-}"
test -f "$APT_SIGNING_PASSPHRASE_FILE"
script_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
python3 "$script_dir/channel_policy.py" "$channels"

install -d -m 0755 "$public/pool" "$public/dists"

for channel in stable prerelease; do
  pool="$public/pool/$channel/main/n/nebula"
  binary="$public/dists/$channel/main/binary-amd64"
  install -d -m 0755 "$pool" "$binary"

  tab=$(printf '\t')
  jq -r --arg channel "$channel" \
    '.channels[$channel][] | [.asset, .version] | @tsv' "$channels" |
  while IFS="$tab" read -r asset version; do
    test -n "$asset"
    test -n "$version"
    deb="$downloads/$asset"
    test "$(dpkg-deb -f "$deb" Package)" = nebula
    test "$(dpkg-deb -f "$deb" Architecture)" = amd64
    case "$version" in
      *-*) expected_deb_version="${version%%-*}~${version#*-}" ;;
      *) expected_deb_version=$version ;;
    esac
    test "$(dpkg-deb -f "$deb" Version)" = "$expected_deb_version"
    install -m 0644 "$deb" "$pool/$asset"
  done

  (
    cd "$public"
    # Upstream release assets use `linux-x86_64.deb`, not Debian's
    # `_amd64.deb` filename convention. The --arch filter selects by filename
    # and would silently omit them; package metadata is validated above.
    dpkg-scanpackages "pool/$channel" /dev/null
  ) >"$binary/Packages"
  expected_count=$(jq -r --arg channel "$channel" \
    '.channels[$channel] | length' "$channels")
  actual_count=$(awk '/^Package: / { count++ } END { print count+0 }' \
    "$binary/Packages")
  if [ "$actual_count" -ne "$expected_count" ]; then
    printf '%s package index contains %s entries; expected %s\n' \
      "$channel" "$actual_count" "$expected_count" >&2
    exit 1
  fi
  gzip -n -9 -c "$binary/Packages" >"$binary/Packages.gz"

  release="$public/dists/$channel/Release"
  apt-ftparchive \
    -o APT::FTPArchive::Release::Origin=Nebula \
    -o APT::FTPArchive::Release::Label=Nebula \
    -o APT::FTPArchive::Release::Suite="$channel" \
    -o APT::FTPArchive::Release::Codename="$channel" \
    -o APT::FTPArchive::Release::Architectures=amd64 \
    -o APT::FTPArchive::Release::Components=main \
    release "$public/dists/$channel" >"$release"

  gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$APT_SIGNING_PASSPHRASE_FILE" \
    --local-user "$gpg_key" --armor --detach-sign \
    --output "$public/dists/$channel/Release.gpg" "$release"
  gpg --batch --yes --pinentry-mode loopback \
    --passphrase-file "$APT_SIGNING_PASSPHRASE_FILE" \
    --local-user "$gpg_key" --armor --clearsign \
    --output "$public/dists/$channel/InRelease" "$release"
done

gpg --batch --yes --local-user "$gpg_key" --armor --export \
  --output "$public/nebula-archive-keyring.asc"

# Repository metadata and packages are public artifacts. Set deterministic
# modes so APT's unprivileged downloader can read them even if the runner has
# inherited a restrictive umask from signing-key setup.
chmod -R u=rwX,go=rX "$public"
