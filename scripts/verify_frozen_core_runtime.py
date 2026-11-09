"""Verify an actual frozen Core in an owned, disposable data directory.

This exercises the packaged Core and private role IPC; it makes no installed
desktop/GUI claim and never contacts an operational provider.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import httpx

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.package_audit import (
    inspect_pyinstaller_binary,
    inspect_pyinstaller_directory,
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def require(response: httpx.Response, codes=(200,)):
    if response.status_code not in codes:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: {response.status_code} {response.text[:1000]}"
        )
    return response.json()


def operation(client: httpx.Client, action: str, parameters: dict) -> dict:
    receipt = require(
        client.post(
            "/deployment/operations",
            json={
                "operation_id": str(uuid4()),
                "action": action,
                "parameters": parameters,
            },
        )
    )
    deadline = time.monotonic() + 90
    while receipt["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        receipt = require(
            client.get("/deployment/operations/" + receipt["operation_id"])
        )
    if receipt["state"] != "succeeded":
        raise RuntimeError(f"{action} failed: {receipt}")
    return receipt.get("result") or {}


def run(
    core: Path,
    bundle: Path,
    output: Path,
    candidate: Path | None = None,
    *,
    diagnostic_declared_budget: bool = False,
) -> dict:
    core, bundle = core.resolve(strict=True), bundle.resolve(strict=True)
    audit = inspect_pyinstaller_binary(core)
    manifest = json.loads((bundle / "nebula-runtime.json").read_text())
    runtime_executable = bundle / manifest["core_executable"]
    runtime_audit = inspect_pyinstaller_directory(
        runtime_executable.parent, runtime_executable.name
    )
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    data = output / "data"
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TZ"}
    }
    environment.update(
        HOME=str(output / "home"),
        NEBULA_V3_DATA_DIR=str(data),
        NEBULA_RUNTIME_BUNDLE=str(bundle),
        PYINSTALLER_RESET_ENVIRONMENT="1",
    )
    if diagnostic_declared_budget:
        # Explicit diagnosis only: the clean SQLite startup result remains a
        # separate gate. Do not mistake these unrelated settings for its fix.
        environment.update(
            NEBULA_V3_DB_ADMIN_RESERVE="2",
            NEBULA_V3_DB_OTHER_CONNECTIONS="1",
            NEBULA_V3_DB_PEAK_PROCESSES="4",
            NEBULA_V3_DB_POOL_SIZE="2",
        )
    Path(environment["HOME"]).mkdir()
    # Public model weights are reusable data, never operator credentials.
    cache = Path.home() / ".cache/chroma/onnx_models/all-MiniLM-L6-v2"
    if cache.is_dir():
        destination = (
            Path(environment["HOME"]) / ".cache/chroma/onnx_models/all-MiniLM-L6-v2"
        )
        shutil.copytree(cache, destination)
    client = None
    stopped = False
    checks = []

    def connect():
        result = subprocess.run(
            [str(core), "desktop-connect", "--data-dir", str(data)],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            (output / "connector.stderr").write_text(result.stderr)
            raise RuntimeError(
                "Frozen connector failed; see connector.stderr and data/runtime/supervisor.log"
            )
        value = json.loads(result.stdout)
        if value.get("protocol") != "nebula-desktop-runtime-v1":
            raise AssertionError("Frozen Core returned an incompatible connection")
        return value

    try:
        first = connect()
        origin = f"http://127.0.0.1:{first['port']}/api/v1"
        client = httpx.Client(
            base_url=origin,
            headers={"Authorization": "Bearer " + first["ipc_token"]},
            timeout=90,
            trust_env=False,
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            health = require(client.get("/health"))
            if health.get("api_ready"):
                break
            time.sleep(0.1)
        else:
            raise TimeoutError("Packaged API admission never became ready")
        if httpx.get(origin + "/health", trust_env=False).status_code != 401:
            raise AssertionError("Packaged Core did not require normal authentication")
        checks.append("authenticated stable listener and API role ready")
        identity = require(client.get("/runtime/identity"))
        baseline = require(client.get("/deployment/status"))["api_version"]
        project = require(
            client.post(
                "/engagements",
                headers={"Idempotency-Key": "frozen-project"},
                json={"name": "Owned frozen runtime fixture"},
            ),
            (200, 201),
        )
        if connect() != first or require(client.get("/runtime/identity")) != identity:
            raise AssertionError("A new connector changed the runtime or principal")
        checks.append(
            "connector exit/reconnect preserves endpoint, credential and principal"
        )
        worker = operation(client, "prepare_execution", {"version": baseline})
        if worker.get("role") != "execution":
            raise AssertionError("Packaged execution role did not authenticate")
        operation(client, "retire", {"runtime_id": worker["runtime_id"]})
        checks.append("packaged execution-role startup, private IPC and retirement")
        candidate_version = None
        if candidate is not None:
            candidate = candidate.resolve(strict=True)
            candidate_manifest = json.loads(
                (candidate / "nebula-runtime.json").read_text()
            )
            if digest(candidate / candidate_manifest["core_executable"]) == digest(
                runtime_executable
            ):
                raise ValueError(
                    "Code-update acceptance requires a second frozen build"
                )
            accepted = operation(
                client,
                "stage",
                {
                    "source": str(candidate),
                    "manifest_sha256": digest(candidate / "nebula-runtime.json"),
                },
            )
            candidate_version = accepted["version"]
            if candidate_version == baseline:
                raise ValueError(
                    "Update acceptance requires a distinct built runtime artifact"
                )
            migration_revision = require(client.get("/deployment/status"))["revision"]
            operation(
                client,
                "migrate",
                {"version": candidate_version, "expected_revision": migration_revision},
            )
            candidate_worker = operation(
                client, "prepare_api", {"version": candidate_version}
            )
            revision = require(client.get("/deployment/status"))["revision"]
            operation(
                client,
                "promote_api",
                {
                    "runtime_id": candidate_worker["runtime_id"],
                    "expected_revision": revision,
                },
            )
            if (
                require(client.get("/deployment/status"))["api_version"]
                != candidate_version
            ):
                raise AssertionError("Packaged candidate was not promoted")
            if require(client.get("/runtime/identity")) != identity:
                raise AssertionError("Promotion changed runtime identity")
            # A durable global mutation under candidate must survive code rollback.
            created = require(
                client.post(
                    "/engagements",
                    headers={"Idempotency-Key": "candidate-project"},
                    json={"name": "Created under frozen candidate"},
                ),
                (200, 201),
            )
            revision = require(client.get("/deployment/status"))["revision"]
            operation(
                client,
                "rollback_code",
                {"version": baseline, "expected_revision": revision},
            )
            projects = require(client.get("/engagements"))
            if created["id"] not in {item["id"] for item in projects}:
                raise AssertionError("Code rollback lost candidate-era SQL")
            checks.append(
                "candidate promotion and retained code rollback preserve SQL and identity"
            )
        projects = require(client.get("/engagements"))
        if project["id"] not in {item["id"] for item in projects}:
            raise AssertionError("Original saved project disappeared")
        result = subprocess.run(
            [str(core), "runtime-stop", "--data-dir", str(data)],
            cwd=output,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            (output / "shutdown.stderr").write_text(result.stderr)
            raise RuntimeError(
                "Packaged shutdown was not verified; see shutdown.stderr"
            )
        stop = json.loads(result.stdout)
        if stop.get("observed_state") != "stopped":
            raise AssertionError("Shutdown did not report observed process exit")
        stopped = True
        checks.append("explicit shutdown observes service exit after durable success")
        receipt = {
            "schema_version": 1,
            "result": "passed",
            "surface": "frozen Core; no installed GUI",
            "startup_configuration": "diagnostic_declared_postgres_budget_for_sqlite"
            if diagnostic_declared_budget
            else "clean_sqlite",
            "core_sha256": digest(core),
            "archive_audit": audit,
            "runtime_archive_audit": runtime_audit,
            "runtime_executable_sha256": digest(runtime_executable),
            "manifest_sha256": digest(bundle / "nebula-runtime.json"),
            "baseline_version": baseline,
            "candidate_version": candidate_version,
            "identity": identity,
            "checks": checks,
        }
        log = data / "runtime/supervisor.log"
        if log.is_file():
            shutil.copy2(log, output / "supervisor.log")
        for directory in (data, Path(environment["HOME"])):
            for current, _children, _files in os.walk(directory):
                Path(current).chmod(0o700)
            shutil.rmtree(directory)
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt
    finally:
        if client is not None:
            client.close()
        if not stopped and data.exists():
            cleanup = subprocess.run(
                [str(core), "runtime-stop", "--data-dir", str(data)],
                cwd=output,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
            )
            (output / "cleanup.stderr").write_text(cleanup.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--diagnostic-declared-db-budget", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.core.resolve(),
                args.bundle.resolve(),
                args.output.resolve(),
                args.candidate.resolve() if args.candidate else None,
                diagnostic_declared_budget=args.diagnostic_declared_db_budget,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
