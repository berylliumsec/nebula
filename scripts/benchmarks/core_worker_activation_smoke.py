"""One owned Core journey: activate workers, verify PDF hold, resize and restart."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time

import httpx

from nebula.v3.deployment_protocol import ReleaseManifest
from nebula.v3.process_identity import same_process


def gone(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # diagnostic-expected: each owned worker was joined and removed.
            continue
        raise AssertionError(f"Owned CPU worker {pid} survived shutdown")


def verified_runtime_environment(
    runtime_bundle: Path,
) -> tuple[dict[str, str], ReleaseManifest, str]:
    from nebula.v3.release_store import ReleaseStore

    runtime_bundle = runtime_bundle.resolve()
    manifest_digest = (runtime_bundle / "nebula-runtime.sha256").read_text().strip()
    manifest = ReleaseStore.verify(
        runtime_bundle, expected_manifest_sha256=manifest_digest
    )
    return {"NEBULA_RUNTIME_BUNDLE": str(runtime_bundle)}, manifest, manifest_digest


def run(binary: Path, output: Path, *, runtime_bundle: Path) -> None:
    runtime_environment, manifest, manifest_digest = verified_runtime_environment(
        runtime_bundle
    )
    token = secrets.token_urlsafe(32)
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_suffix(".core.log")
    receipt: dict = {
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "candidate_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "scope": "one real Linux Core worker activation and protected PDF hold journey",
        "runtime_manifest_sha256": manifest_digest,
        "runtime_version": manifest.version,
        "artifact_source": {
            "runtime_manifest_sha256": manifest_digest,
            "runtime_core_sha256": manifest.files[manifest.core_executable],
            "runtime_ui_files": {
                name: digest
                for name, digest in manifest.files.items()
                if name.startswith(manifest.ui_directory + "/")
            },
        },
    }
    repository = Path(__file__).resolve().parents[2]
    receipt["source_sha256"] = {
        name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
        for name in (
            "src/nebula/v3/api.py",
            "src/nebula/v3/execution_kernel.py",
            "src/nebula/v3/authentication.py",
            "src/nebula/v3/memory_budget.py",
            "src/nebula/v3/runtime_memory.py",
            "src/nebula/v3/cpu_workers.py",
            "src/nebula/v3/host_cpu_budget.py",
            "src/nebula/v3/report_cpu_executor.py",
            "src/nebula/v3/reporting.py",
            "scripts/nebula_core_entry.py",
        )
    }
    with socket.socket() as chosen:
        chosen.bind(("127.0.0.1", 0))
        port = chosen.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="nebula-native-cpu-") as directory:
        root = Path(directory)
        environment = dict(os.environ)
        environment.update(
            XDG_DATA_HOME=str(root / "data"),
            XDG_CONFIG_HOME=str(root / "config"),
            XDG_CACHE_HOME=str(root / "cache"),
            XDG_STATE_HOME=str(root / "state"),
            PYTHON_KEYRING_BACKEND="keyring.backends.null.Keyring",
            NEBULA_V3_KNOWLEDGE_RERANKER="off",
            **runtime_environment,
        )
        process = None
        log = log_path.open("w")
        client = httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=False,
        )

        def start():
            nonlocal process
            process = subprocess.Popen(
                [
                    str(binary),
                    "serve",
                    "--data-dir",
                    str(root / "core"),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--token",
                    token,
                ],
                env=environment,
                stdout=log,
                stderr=log,
            )
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(
                        f"Owned Core exited during startup: {process.returncode}"
                    )
                try:
                    health = client.get("/api/v1/health")
                    if health.status_code != 200 or not health.json().get("api_ready"):
                        time.sleep(0.1)
                        continue
                    # Host observations are available before the API worker.
                    # A ready host-only CPU policy route is insufficient.
                    if client.get("/api/v1/engagements").status_code != 200:
                        time.sleep(0.1)
                        continue
                    response = client.get("/api/v1/host-cpu-budget")
                    if response.status_code == 200:
                        return response.json()
                except httpx.TransportError:
                    # diagnostic-expected: only this newly launched Core's socket is polled.
                    pass
                time.sleep(0.1)
            raise AssertionError("Owned Core did not become ready")

        def stop():
            if process is None or process.poll() is not None:
                return
            database = root / "core/nebula.db"
            owned = []
            if database.exists():
                with sqlite3.connect(
                    f"file:{database}?mode=ro", uri=True
                ) as connection:
                    owned = list(
                        connection.execute(
                            "SELECT pid, process_birth FROM runtime_processes WHERE pid IS NOT NULL"
                        )
                    )
            stopped = subprocess.run(
                [str(binary), "runtime-stop", "--data-dir", str(root / "core")],
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            try:
                assert stopped.returncode == 0, stopped.stderr
                result = json.loads(stopped.stdout)
                assert result["observed_state"] == "stopped", result
                receipt.setdefault("shutdowns", []).append(result)
                process.wait(timeout=30)
                assert not [
                    (pid, birth) for pid, birth in owned if same_process(pid, birth)
                ], "Owned runtime survived coordinated stop"
            finally:
                # A failure never authorizes signalling a reused PID.
                for pid, birth in owned:
                    if same_process(pid, birth):
                        os.kill(pid, signal.SIGTERM)
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

        def request(method, path, body=None):
            headers = (
                {"Idempotency-Key": secrets.token_hex(16)}
                if method not in {"GET", "HEAD", "OPTIONS"}
                else {}
            )
            response = client.request(method, path, json=body, headers=headers)
            if (
                response.status_code == 202
                and response.headers.get("x-nebula-request-state") != "complete"
            ):
                accepted = response.json()
                command = (
                    response.headers.get("x-nebula-command-id")
                    or accepted.get("command_id")
                    or accepted.get("receipt", {}).get("command_id")
                )
                if not command:
                    raise AssertionError(
                        "Pending saved request did not identify its durable command"
                    )
                if command:
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        response = client.get(
                            f"/api/v1/runtime/requests/{command}/response"
                        )
                        state = response.headers.get("x-nebula-request-state")
                        if response.status_code == 202 and state != "complete":
                            time.sleep(0.1)
                            continue
                        if response.status_code in {429, 503} and state in {
                            None,
                            "pending",
                        }:
                            time.sleep(0.1)
                            continue
                        break
                    else:
                        raise AssertionError("Saved smoke request did not finish")
            response.raise_for_status()
            return response.json()

        def wait_state(expected):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                value = request("GET", "/api/v1/host-cpu-budget")
                if value["executor_state"] == expected:
                    return value
                assert value["apply_state"] != "failed", value
                time.sleep(0.05)
            raise AssertionError(f"Worker state never reached {expected}: {value}")

        def save(value, enabled, limit):
            return request(
                "PUT",
                "/api/v1/host-cpu-budget",
                {
                    "host_id": value["host_id"],
                    "expected_revision": value["revision"],
                    "cpu_limit": limit,
                    "enabled": enabled,
                },
            )

        try:
            initial = start()
            output.with_suffix(".initial.json").write_text(
                json.dumps(initial, indent=2) + "\n"
            )
            managed = (
                initial["activation_supported"]
                and initial["executor_scope"] == "report-pdfs"
            )
            receipt["initial"] = initial
            receipt["activation_supported"] = managed
            if managed:
                assert initial["enabled"] is False and not initial["worker_pids"]
                save(initial, True, 1)
                first = wait_state("running")
                assert first["worker_count"] == 1 and first["apply_state"] == "applied"
                assert first["worker_pids"][0] not in {os.getpid(), process.pid}
                assert len(os.sched_getaffinity(first["worker_pids"][0])) == 1
            else:
                assert (
                    initial["executor_scope"] == "external"
                    and not initial["activation_supported"]
                ), initial
                assert (
                    "memory-managed CPU runner"
                    in initial["activation_unavailable_reason"]
                )
                refused = client.put(
                    "/api/v1/host-cpu-budget",
                    json={
                        "host_id": initial["host_id"],
                        "expected_revision": initial["revision"],
                        "enabled": True,
                        "cpu_limit": 1,
                    },
                    headers={"Idempotency-Key": secrets.token_hex(16)},
                )
                assert refused.status_code == 422, refused.text
                first = request(
                    "PUT",
                    "/api/v1/host-cpu-budget",
                    {
                        "host_id": initial["host_id"],
                        "expected_revision": initial["revision"],
                        "cpu_limit": min(1, initial["cpu_capacity"]),
                    },
                )
                assert (
                    first["apply_state"] == "inactive"
                    and first["applied_cpu_limit"] is None
                )
                assert not first["worker_pids"]
                receipt["activation_limitation"] = (
                    "Supervisor has no supported PDF executor; actual affinity activation is covered by the dedicated in-process tests."
                )
            project = request(
                "POST", "/api/v1/engagements", {"name": "Synthetic native CPU smoke"}
            )
            note = request(
                "POST",
                "/api/v1/observations",
                {
                    "engagement_id": project["id"],
                    "observation_type": "note",
                    "title": "Saved native source",
                    "body": "Native PDF worker output. " * 200,
                },
            )
            report = request(
                "POST",
                "/api/v1/reports",
                {
                    "engagement_id": project["id"],
                    "title": "Native worker PDF",
                    "observation_ids": [note["id"]],
                },
            )
            render = request(
                "POST",
                f"/api/v1/reports/{report['id']}/renders",
                {"report_revision": report["revision"]},
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                rendered = request("GET", f"/api/v1/report-renders/{render['id']}")
                if rendered["status"] not in {"queued", "rendering"}:
                    break
                time.sleep(0.05)
            assert rendered["status"] == "failed", rendered
            assert (
                "PDF computation requires a memory-managed CPU runner"
                in rendered["error_detail"]
            )
            assert rendered["pdf_artifact_id"] is None
            pdf = client.get(f"/api/v1/report-renders/{render['id']}/pdf")
            assert pdf.status_code == 409, pdf.text
            jobs = request("GET", "/api/v1/memory-budgets/jobs")["items"]
            held = [job for job in jobs if job["resource_id"] == render["id"]]
            assert (
                len(held) == 1
                and held[0]["state"] == "blocked"
                and held[0]["reserved_bytes"] == 0
            )
            receipt["pdf_capability"] = "held_without_protected_memory_runner"
            receipt["held_render_id"] = render["id"]
            receipt["first_running" if managed else "first_saved_policy"] = first
            limit = min(
                2, first["maximum_worker_cpus"] if managed else first["cpu_capacity"]
            )
            if managed:
                save(first, True, limit)
                resized = wait_state("running")
                assert (
                    resized["worker_count"] == limit
                    and resized["applied_cpu_limit"] == limit
                )
            else:
                resized = request(
                    "PUT",
                    "/api/v1/host-cpu-budget",
                    {
                        "host_id": first["host_id"],
                        "expected_revision": first["revision"],
                        "cpu_limit": limit,
                    },
                )
                assert (
                    resized["cpu_limit"] == limit
                    and resized["apply_state"] == "inactive"
                )
            receipt["resized"] = resized
            stop()
            gone((first["worker_pids"] or []) + (resized["worker_pids"] or []))
            start()
            restored = (
                wait_state("running")
                if managed
                else request("GET", "/api/v1/host-cpu-budget")
            )
            assert (
                restored["host_id"] == first["host_id"]
                and restored["cpu_limit"] == limit
            )
            if managed:
                assert restored["enabled"] and restored["worker_count"] == limit
                assert not set(restored["worker_pids"]) & set(resized["worker_pids"])
            else:
                assert (
                    not restored["activation_supported"] and not restored["worker_pids"]
                )
            saved_render = request("GET", f"/api/v1/report-renders/{render['id']}")
            assert (
                saved_render["status"] == "failed"
                and saved_render["pdf_artifact_id"] is None
            )
            assert (
                client.get(f"/api/v1/report-renders/{render['id']}/pdf").status_code
                == 409
            )
            receipt["restored"] = restored
            if managed:
                save(restored, False, limit)
                disabled = wait_state("disabled")
                assert not disabled["worker_pids"] and disabled["enabled"] is False
                gone(restored["worker_pids"])
            else:
                disabled = restored
                assert disabled["apply_state"] == "inactive"
            receipt["disabled" if managed else "inactive"] = disabled
            receipt["passed"] = True
        finally:
            stop()
            receipt["core_exit_code"] = (
                process.returncode if process is not None else None
            )
            client.close()
            log.close()
            log_path.write_text(
                log_path.read_text().replace(token, "<synthetic-token>")
            )
    receipt["synthetic_data_removed"] = not root.exists()
    output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        json.dumps(
            {
                "passed": receipt["passed"],
                "output": str(output),
                "binary_sha256": receipt["binary_sha256"],
                "synthetic_data_removed": receipt["synthetic_data_removed"],
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-bundle", type=Path, required=True)
    arguments = parser.parse_args()
    run(
        arguments.binary.resolve(),
        arguments.output,
        runtime_bundle=arguments.runtime_bundle,
    )
