#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  printf 'usage: %s IMAGE@SHA256 PLATFORM OUTPUT_DIR TOOL_VERSIONS\n' "$0" >&2
  exit 2
fi

reference="$1"
platform="$2"
output_dir="$3"
versions_file="$4"

case "$platform" in
  linux/amd64|linux/arm64) ;;
  *) printf 'unsupported Toolbox platform: %s\n' "$platform" >&2; exit 2 ;;
esac
if [[ ! "$reference" =~ @sha256:[0-9a-f]{64}$ ]]; then
  printf 'Toolbox image must be pinned by SHA-256\n' >&2
  exit 2
fi

mkdir -p "$output_dir"
. "$versions_file"

docker pull --platform "$platform" "$reference"
user="$(docker image inspect --format '{{.Config.User}}' "$reference")"
test -n "$user" && test "$user" != 0 && test "$user" != root
test "$(docker image inspect --format '{{json .Config.Entrypoint}}' "$reference")" = null

docker run --rm --platform "$platform" --network none "$reference" \
  /opt/nebula/bin/nebula-toolbox search --query nmap > "$output_dir/search.json"
docker run --rm --platform "$platform" --network none "$reference" \
  /opt/nebula/bin/nebula-toolbox help --tool jq \
  --command-path-json '[]' > "$output_dir/help.json"
docker run --rm --platform "$platform" --network none "$reference" \
  /bin/bash --noprofile --norc -c 'cat /opt/nebula/tool-catalog.json' \
  > "$output_dir/model-catalog.json"
docker run --rm --platform "$platform" --network none "$reference" \
  /opt/nebula/bin/nebula-toolbox exec --max-risk workspace_write \
  --tool jq \
  --invocation-json '{"command_path":[],"options":[],"positionals":[{"id":"filter","value":"."}]}' \
  --cwd /workspace --timeout 30 > "$output_dir/local.json"
docker run --rm --platform "$platform" --network none "$reference" \
  /opt/nebula/bin/nebula-toolbox shell --script \
  "command -v perl >/dev/null && printf 'one\\ntwo\\n' | perl -ne 'print' | wc -l" \
  --cwd /workspace --timeout 30 > "$output_dir/shell.json"
docker run --rm --platform "$platform" --network none "$reference" \
  /opt/nebula/bin/nebula-toolbox shell --script \
  "test ! -x /usr/local/bin/nebula-egress && test ! -e /opt/nebula/bin/build-tool-catalog" \
  --cwd /workspace --timeout 30 > "$output_dir/internal-helper-refusal.json"
docker run --rm --platform "$platform" --network none "$reference" \
  /opt/nebula/venv/bin/semgrep --version | grep -Fx "$SEMGREP_VERSION"

if docker run --rm --platform "$platform" --network none "$reference" \
  /opt/nebula/bin/nebula-toolbox exec --max-risk active_scan \
  --tool sqlmap \
  --invocation-json '{"command_path":[],"options":[],"positionals":[]}' \
  --cwd /workspace --timeout 30 > "$output_dir/refusal.json"; then
  printf 'risk ceiling did not reject sqlmap\n' >&2
  exit 1
fi

python - "$output_dir" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
expected = {
    "search.json": ("search", 0),
    "help.json": ("help", 0),
    "local.json": ("exec", 0),
    "shell.json": ("shell", 0),
    "internal-helper-refusal.json": ("shell", 0),
    "refusal.json": ("error", 2),
}
for filename, (operation, exit_code) in expected.items():
    payload = json.loads((root / filename).read_text(encoding="utf-8"))
    assert payload["protocol"] == "nebula.toolbox/v1"
    assert payload["operation"] == operation
    assert payload["exit_code"] == exit_code

search = json.loads((root / "search.json").read_text(encoding="utf-8"))
assert any(
    item["name"] == "nmap" and item["version"] == "7.99"
    for item in search["matches"]
)
help_payload = json.loads((root / "help.json").read_text(encoding="utf-8"))
assert len(help_payload["matches"]) == 1
descriptor = help_payload["matches"][0]
assert descriptor["name"] == "jq" and descriptor["version"] == "1.6"
assert descriptor["synopsis"] and descriptor["examples"]
assert descriptor["selected_command"]["help_documents"]
assert descriptor["selected_command"]["options"]
catalog = json.loads((root / "model-catalog.json").read_text(encoding="utf-8"))
assert catalog["protocol"] == "nebula.toolbox.catalog/v2"
names = [item["name"] for item in catalog["tools"]]
assert len(names) == 27 and len(names) == len(set(names))
assert all(item["coverage"]["complete"] for item in catalog["tools"])
assert all(not item["coverage"]["unmapped_options"] for item in catalog["tools"])
shell = json.loads((root / "shell.json").read_text(encoding="utf-8"))
assert shell["stdout"].strip() == "2"
assert shell["metadata"]["catalogued_guidance"] is False
assert len(shell["metadata"]["script_sha256"]) == 64
PY

helper="nebula-egress-smoke-${GITHUB_RUN_ID:-local}-${RANDOM}"
cleanup() {
  docker rm -f "$helper" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker run --detach --rm --platform "$platform" --name "$helper" \
  --read-only --cap-drop ALL --cap-add NET_ADMIN \
  --security-opt no-new-privileges --network bridge --user 0:0 \
  --tmpfs /run:rw,noexec,nosuid,nodev,size=4m \
  --entrypoint /usr/local/bin/nebula-egress "$reference" \
  serve --allow tcp://127.0.0.1:1
for attempt in 1 2 3 4 5; do
  if docker logs "$helper" 2>&1 | grep -qx READY; then break; fi
  sleep 1
done
docker logs "$helper" 2>&1 | grep -qx READY
docker stop --time 0 "$helper"
trap - EXIT
