"""One owned CLI/frozen Core journey: enable, PDF, resize, restart, disable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import time

import httpx


def gone(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # diagnostic-expected: each owned worker was joined and removed.
            continue
        raise AssertionError(f"Owned CPU worker {pid} survived shutdown")


def run(binary: Path, output: Path) -> None:
    token = secrets.token_urlsafe(32)
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_suffix(".core.log")
    receipt: dict = {
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "candidate_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "scope": "one real Linux Core PDF-worker activation journey",
    }
    repository = Path(__file__).resolve().parents[2]
    receipt["source_sha256"] = {
        name: hashlib.sha256((repository / name).read_bytes()).hexdigest()
        for name in (
            "src/nebula/v3/api.py",
            "src/nebula/v3/execution_kernel.py",
            "src/nebula/v3/authentication.py",
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
                    response = client.get("/api/v1/host-cpu-budget")
                    if response.status_code == 200:
                        return response.json()
                except httpx.TransportError:
                    # diagnostic-expected: only this newly launched Core's socket is polled.
                    pass
                time.sleep(0.1)
            raise AssertionError("Owned Core did not become ready")

        def stop():
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    # diagnostic-expected: kill only this smoke's owned Core on failure.
                    process.kill()
                    process.wait(timeout=10)
                    raise AssertionError("Owned Core did not shut down gracefully")

        def request(method, path, body=None):
            response = client.request(method, path, json=body)
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
            assert (
                initial["executor_scope"] == "report-pdfs"
                and initial["activation_supported"]
            )
            assert initial["enabled"] is False and not initial["worker_pids"]
            save(initial, True, 1)
            first = wait_state("running")
            assert first["worker_count"] == 1 and first["apply_state"] == "applied"
            assert first["worker_pids"][0] not in {os.getpid(), process.pid}
            assert len(os.sched_getaffinity(first["worker_pids"][0])) == 1
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
            assert rendered["status"] == "completed", rendered
            pdf = client.get(f"/api/v1/report-renders/{render['id']}/pdf")
            pdf.raise_for_status()
            assert pdf.content.startswith(b"%PDF-")
            receipt["pdf_sha256"] = hashlib.sha256(pdf.content).hexdigest()
            receipt["first_running"] = first
            limit = min(2, first["maximum_worker_cpus"])
            save(first, True, limit)
            resized = wait_state("running")
            assert (
                resized["worker_count"] == limit
                and resized["applied_cpu_limit"] == limit
            )
            receipt["resized"] = resized
            stop()
            gone(first["worker_pids"] + resized["worker_pids"])
            start()
            restored = wait_state("running")
            assert restored["host_id"] == first["host_id"] and restored["enabled"]
            assert restored["worker_count"] == limit
            assert not set(restored["worker_pids"]) & set(resized["worker_pids"])
            saved_pdf = client.get(f"/api/v1/report-renders/{render['id']}/pdf")
            saved_pdf.raise_for_status()
            assert (
                hashlib.sha256(saved_pdf.content).hexdigest() == receipt["pdf_sha256"]
            )
            receipt["restored"] = restored
            save(restored, False, limit)
            disabled = wait_state("disabled")
            assert not disabled["worker_pids"] and disabled["enabled"] is False
            gone(restored["worker_pids"])
            receipt["disabled"] = disabled
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
    arguments = parser.parse_args()
    run(arguments.binary.resolve(), arguments.output)
