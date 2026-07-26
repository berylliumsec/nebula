#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
  printf 'usage: %s PUBLIC_ROOT CHANNELS\n' "$0" >&2
  exit 2
fi

public=$(cd "$1" && pwd)
channels=$(cd "$(dirname "$2")" && pwd)/$(basename "$2")

for channel in stable prerelease; do
  count=$(jq -r --arg channel "$channel" '.channels[$channel] | length' "$channels")
  if [ "$count" -eq 0 ]; then
    continue
  fi
  current_version=$(jq -r --arg channel "$channel" '.channels[$channel][0].version' "$channels")
  previous=
  if [ "$count" -gt 1 ]; then
    previous=$(jq -r --arg channel "$channel" '.channels[$channel][1].asset' "$channels")
  fi
  for image in ubuntu:24.04 debian:12-slim kalilinux/kali-rolling:latest; do
    docker run --rm \
      -e CHANNEL="$channel" \
      -e CURRENT_VERSION="$current_version" \
      -e PREVIOUS="$previous" \
      -v "$public:/repo:ro" \
      "$image" sh -euxc '
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates gnupg xauth xvfb
        gpg --dearmor </repo/nebula-archive-keyring.asc >/usr/share/keyrings/nebula.gpg
        printf "deb [arch=amd64 signed-by=/usr/share/keyrings/nebula.gpg] file:/repo %s main\n" "$CHANNEL" \
          >/etc/apt/sources.list.d/nebula.list
        apt-get update
        if [ -n "$PREVIOUS" ]; then
          DEBIAN_FRONTEND=noninteractive apt-get install -y "/repo/pool/$CHANNEL/main/n/nebula/$PREVIOUS"
        fi
        DEBIAN_FRONTEND=noninteractive apt-get install -y nebula
        expected_version="$CURRENT_VERSION"
        case "$expected_version" in
          *-*) expected_version="${expected_version%%-*}~${expected_version#*-}" ;;
        esac
        test "$(dpkg-query -W -f="\${Version}" nebula)" = "$expected_version"
        xvfb-run -a nebula --self-test
        nebula-core doctor --data-dir /tmp/nebula-doctor --json
        DEBIAN_FRONTEND=noninteractive apt-get remove -y nebula
        test ! -e /usr/bin/nebula
        test ! -e /usr/bin/nebula-ui
        test ! -e /usr/bin/nebula-core
      '
  done
done
