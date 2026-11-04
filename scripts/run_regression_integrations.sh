#!/usr/bin/env bash
# Explicitly selected integration checks for the manual hosted workflow.
# Intended boundary: a fresh disposable GitHub-hosted ubuntu-22.04 x64 VM,
# with Python 3.12, Poetry 2.1.3, and `poetry install --with dev` already done.
# Run from the repository root. No Core, UI bundle, operator data, or live
# provider credentials are used. Kali needs public Docker Hub/Kali APT/DNS
# egress, substantial free disk, and up to a one-hour image build.
set -euo pipefail
: "${RUNNER_TEMP:?Use a disposable hosted runner with RUNNER_TEMP set}"
test "${GITHUB_ACTIONS:-}" = true
test "${RUNNER_ENVIRONMENT:-}" = github-hosted
test -f packaging/notebook/runtime-lock.json
test "$(id -u)" != 0

# All installs below belong only to the disposable integration job.
sudo rm -rf /usr/local/lib/android /usr/share/dotnet /opt/ghc
sudo apt-get update
sudo apt-get install -y ripgrep podman uidmap slirp4netns fuse-overlayfs
poetry run python -m pip install --require-hashes -r packaging/notebook/requirements.lock

# Reuse the repository's existing rootless/cgroup recipe from optional release
# diagnostics. Do not substitute the hosted VM's rootful Docker daemon.
podman info --format '{{.Host.Security.Rootless}}' | rg -Fx true
runner_uid="$(id -u)"
user_service="/sys/fs/cgroup/user.slice/user-${runner_uid}.slice/user@${runner_uid}.service"
for control_group in \
  /sys/fs/cgroup/user.slice \
  "/sys/fs/cgroup/user.slice/user-${runner_uid}.slice" \
  "$user_service" \
  "$user_service/user.slice"
do
  test -f "$control_group/cgroup.controllers"
  rg -qw cpu "$control_group/cgroup.controllers"
  echo +cpu | sudo tee "$control_group/cgroup.subtree_control" >/dev/null
done
rg -qw cpu "$user_service/user.slice/cgroup.subtree_control"

# Kali's actual image build consumes exactly these two staged assets. It
# installs its own Python/Jupyter packages; no portable Python/runtime manifest
# or repository UI resource directory needs to be staged for this test.
export NEBULA_NOTEBOOK_RUNTIME_DIR="$RUNNER_TEMP/nebula-kali-notebook-runtime"
poetry run python - <<'PY'
import json
import os
import shutil
import tempfile
from pathlib import Path

from scripts.stage_notebook_runtime import LOCK, ROOT, download, extract

lock = json.loads(LOCK.read_text(encoding="utf-8"))
if lock["target"] != "x86_64-unknown-linux-gnu":
    raise RuntimeError("This recipe is for the hosted Linux x64 runner")
destination = Path(os.environ["NEBULA_NOTEBOOK_RUNTIME_DIR"])
destination.mkdir(parents=True, exist_ok=True)
cache = Path(os.environ["RUNNER_TEMP"]) / "nebula-notebook-downloads"
archive = download(lock["uv_url"], lock["uv_sha256"], cache)
with tempfile.TemporaryDirectory(
    prefix="nebula-uv-", dir=os.environ["RUNNER_TEMP"]
) as temporary:
    extracted = Path(temporary)
    extract(archive, extracted)
    candidates = [item for item in extracted.rglob("uv") if item.is_file()]
    if len(candidates) != 1:
        raise RuntimeError("Pinned uv archive must contain one uv executable")
    shutil.copy2(candidates[0], destination / "uv")
(destination / "uv").chmod(0o755)
shutil.copy2(ROOT / "packaging/notebook/worker.py", destination / "worker.py")
PY
"$NEBULA_NOTEBOOK_RUNTIME_DIR/uv" --version

# The calibration test deliberately refuses reranker downloads itself. Prepare
# both pinned files first using the real SHA-256-verifying code, then prepare
# the public Chroma embedding model in its normal disposable-user cache.
export NEBULA_TEST_RERANKER_DIR="$RUNNER_TEMP/nebula-reranker"
poetry run python - <<'PY'
import os
from pathlib import Path

from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
from nebula.v3.knowledge_rerank import CrossEncoderReranker

reranker = CrossEncoderReranker(Path(os.environ["NEBULA_TEST_RERANKER_DIR"]))
reranker.prepare()
if not reranker.ready:
    raise RuntimeError("Pinned reranker did not become ready")
embedding = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
vectors = embedding(["synthetic CI calibration preparation"])
if len(vectors) != 1 or len(vectors[0]) != 384:
    raise RuntimeError("Public Chroma embedding model did not produce a vector")
print(reranker.status.model_dump_json())
PY

export NEBULA_TEST_CONTAINER_RUNTIME=/usr/bin/podman
export NEBULA_TEST_RUNTIME_IMAGE=localhost/nebula-operator-runtime:integration
export NEBULA_TEST_KALI_TERMINAL=1
podman build --file tests/v3/integration/Containerfile.operator-runtime \
  --tag "$NEBULA_TEST_RUNTIME_IMAGE" .

# Exactly nine feasible former skips, including the three real runtime nodes.
targets=(
  tests/v3/test_workspace.py::test_workspace_text_search_ripgrep_preserves_python_results
  tests/v3/test_workspace.py::test_workspace_text_search_pins_files_and_falls_back_on_rg_failure
  tests/v3/test_workspace.py::test_workspace_text_search_limit_keeps_original_file_count
  tests/v3/test_workspace.py::test_workspace_text_search_uses_python_for_small_files
  tests/v3/test_notebooks.py::test_jupyter_worker_streams_and_reuses_a_kernel
  tests/v3/test_knowledge_rerank.py::test_the_pinned_model_meets_its_calibration
  tests/v3/test_sandbox_integration.py::test_real_rootless_runtime_streams_raw_code_and_persists_only_workspace
  tests/v3/test_sandbox_integration.py::test_real_rootless_runtime_opens_and_removes_container_terminal
  tests/v3/test_human_terminal_integration.py::test_real_kali_terminal_is_root_writable_networked_and_ephemeral
)
poetry run pytest --collect-only -q "${targets[@]}"
poetry run pytest -vv -ra "${targets[@]}" \
  --junitxml="$RUNNER_TEMP/former-skip-tests.xml"
poetry run python - <<'PY'
import json
import os
import platform
import subprocess
from pathlib import Path
from xml.etree import ElementTree

root = ElementTree.parse(Path(os.environ["RUNNER_TEMP"]) / "former-skip-tests.xml")
cases = root.findall(".//testcase")
if len(cases) != 9:
    raise RuntimeError(f"Expected exactly 9 test cases, found {len(cases)}")
if any(case.find(status) is not None for case in cases for status in ["skipped", "failure", "error"]):
    raise RuntimeError("All nine cases must execute and pass without skips")
receipt = {
    "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    "python": platform.python_version(),
    "executed": len(cases),
    "passed": len(cases),
    "skipped": 0,
    "rootless_runtime": "/usr/bin/podman",
    "fixtures": "synthetic temporary workspaces and public pinned CPU models",
    "live_qwen": "requires a separately configured live inference server",
}
(Path(os.environ["RUNNER_TEMP"]) / "regression-integrations-receipt.json").write_text(
    json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
)
print("9 executed, 9 passed, 0 skipped")
PY
