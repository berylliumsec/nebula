#!/bin/sh
# Copy a built Nebula web UI (ui/dist) into the app bundle's WebUI folder.
# Build it from the Core commit the phone talks to, so the UI only calls APIs
# that Core has: cd ui && npm ci && npm run build
set -eu
source_dir=${1:?usage: bundle-web-ui.sh <path to ui/dist>}
here=$(cd "$(dirname "$0")" && pwd)
test -f "$source_dir/index.html" || { echo "No index.html in $source_dir" >&2; exit 1; }
rm -rf "$here/WebUI"
mkdir -p "$here/WebUI"
cp -R "$source_dir"/. "$here/WebUI/"
echo "Bundled web UI: $(sed -n 's/.*"commit": "\([0-9a-f]*\)".*/\1/p' "$here/WebUI/web-build.json" 2>/dev/null | head -1)"
