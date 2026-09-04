#!/bin/sh
set -eu

mkdir -p -m 0700 /run/nebula
/usr/local/lib/nebula-public-ip-update >/dev/null 2>&1 &
/usr/sbin/cron
exec "$@"
