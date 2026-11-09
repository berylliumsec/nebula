"""Owned LAN stress of real GUI-activated PDF workers, resizing and draining."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx


def distribution(values):
    ordered = sorted(values)

    def percentile(ratio):
        return (
            round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * ratio))], 3)
            if ordered
            else None
        )

    return {
        "count": len(ordered),
        "p50": percentile(0.5),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": max(ordered) if ordered else None,
    }


async def exercise(origin, paragraphs):
    failures, latencies, samples = [], [], []
    phase = ["initial-limit"]
    started = time.monotonic()
    stop = asyncio.Event()
    async with httpx.AsyncClient(
        base_url=origin,
        headers={"Authorization": "Bearer cpu-budget-test-token"},
        timeout=30,
        trust_env=False,
        limits=httpx.Limits(max_connections=100),
    ) as client:

        async def request(method, path, body=None):
            response = await client.request(method, path, json=body)
            response.raise_for_status()
            return response.json()

        async def control(body):
            return await request("POST", "/__cpu_fixture/state", body)

        async def status():
            return await request("GET", "/api/v1/host-cpu-budget")

        async def save(enabled, cpus):
            current = await status()
            return await request(
                "PUT",
                "/api/v1/host-cpu-budget",
                {
                    "host_id": current["host_id"],
                    "expected_revision": current["revision"],
                    "cpu_limit": cpus,
                    "enabled": enabled,
                },
            )

        async def wait_state(expected):
            async with asyncio.timeout(300):
                while (value := await status())["executor_state"] != expected:
                    assert value["apply_state"] != "failed", value
                    await asyncio.sleep(0.05)
                return value

        await control({"reset": True})
        build = await control({"seed_reports": 50, "paragraphs": paragraphs})
        limit = min(3, build["policy"]["maximum_worker_cpus"])
        await save(True, limit)
        initial = await wait_state("running")

        async def probe():
            while not stop.is_set():
                at = time.monotonic()
                current_phase = phase[0]
                try:
                    response = await client.get(
                        "/api/v1/engagements/cpu-project/assistant-defaults"
                    )
                    response.raise_for_status()
                    latencies.append(
                        {
                            "at_s": at - started,
                            "ms": (time.monotonic() - at) * 1000,
                            "phase": current_phase,
                        }
                    )
                except httpx.HTTPError as exc:
                    # diagnostic-expected: ordinary probes are recorded without retry.
                    failures.append(
                        {
                            "at_s": at - started,
                            "kind": type(exc).__name__,
                            "phase": current_phase,
                        }
                    )
                await asyncio.sleep(0.03)

        async def observe():
            while not stop.is_set():
                facts = await control({})
                samples.append(
                    {
                        "at_s": time.monotonic() - started,
                        "phase": phase[0],
                        "policy": facts["policy"],
                        "pool": facts["pool_stats"],
                    }
                )
                await asyncio.sleep(0.2)

        async with asyncio.TaskGroup() as group:
            group.create_task(probe())
            group.create_task(probe())
            group.create_task(observe())
            try:
                load_start = time.monotonic()
                renders = await asyncio.gather(
                    *(
                        request(
                            "POST",
                            f"/api/v1/reports/load-{index}/renders",
                            {"report_revision": 1},
                        )
                        for index in range(50)
                    )
                )
                before_resize = await control({})
                phase[0] = "resize-drain"
                await save(True, 1)
                resized = await wait_state("running")
                assert (
                    resized["worker_count"] == 1 and resized["applied_cpu_limit"] == 1
                )
                results = await asyncio.gather(
                    *(
                        request("GET", f"/api/v1/report-renders/{render['id']}")
                        for render in renders
                    )
                )
                assert all(row["status"] in {"completed", "failed"} for row in results)
                completed = [row for row in results if row["status"] == "completed"]
                rejected = [row for row in results if row["status"] == "failed"]
                assert completed
                assert all(
                    "queue is full" in str(row["error_detail"]) for row in rejected
                ), rejected
                for render in completed:
                    pdf = await client.get(f"/api/v1/report-renders/{render['id']}/pdf")
                    pdf.raise_for_status()
                    assert pdf.content.startswith(b"%PDF-")
                load_wall = time.monotonic() - load_start
                phase[0] = "disable-drain"
                report = await request(
                    "POST",
                    "/api/v1/reports",
                    {
                        "engagement_id": "cpu-project",
                        "title": "PDF during disable",
                        "observation_ids": ["load-note"],
                    },
                )
                final_render = await request(
                    "POST",
                    f"/api/v1/reports/{report['id']}/renders",
                    {"report_revision": report["revision"]},
                )
                async with asyncio.timeout(20):
                    while (await status())["active_jobs"] < 1:
                        await asyncio.sleep(0.01)
                await save(False, 1)
                disabled = await wait_state("disabled")
                assert disabled["worker_count"] == 0 and not disabled["worker_pids"]
                final = await request(
                    "GET", f"/api/v1/report-renders/{final_render['id']}"
                )
                assert final["status"] == "completed", final
            finally:
                stop.set()
        return {
            "origin": origin,
            "paragraphs_per_report": paragraphs,
            "reports_requested": 50,
            "completed_first_attempt": len(completed),
            "explicit_queue_rejections": len(rejected),
            "completed_during_disable": 1,
            "load_wall_s": load_wall,
            "initial_running": initial,
            "before_resize": before_resize,
            "resized": resized,
            "disabled": disabled,
            "ordinary_reads_ms": distribution([row["ms"] for row in latencies]),
            "ordinary_failures": failures,
            "samples": samples,
            "raw_reads": latencies,
            "bundle_sha256": build["bundle_sha256"],
            "passed": True,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--paragraphs", type=int, default=2500)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    with socket.socket() as chosen:
        chosen.bind((args.host, 0))
        port = chosen.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="nebula-cpu-activation-load-") as directory:
        environment = dict(
            os.environ,
            NEBULA_MODEL_TEST_HOST=args.host,
            NEBULA_MODEL_TEST_PORT=str(port),
            TMPDIR=directory,
            XDG_DATA_HOME=directory + "/data",
            XDG_CONFIG_HOME=directory + "/config",
            XDG_CACHE_HOME=directory + "/cache",
        )
        log_path = args.output.with_suffix(".core.log")
        with log_path.open("w") as log:
            core = subprocess.Popen(
                [
                    sys.executable,
                    str(repository / "tests/v3/host_cpu_budget_fixture.py"),
                ],
                cwd=repository,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                origin = f"http://{args.host}:{port}"
                deadline = time.monotonic() + 60
                with httpx.Client(timeout=2, trust_env=False) as client:
                    while time.monotonic() < deadline:
                        assert core.poll() is None, "Owned Core exited before readiness"
                        try:
                            if (
                                client.get(
                                    origin + "/api/v1/health",
                                    headers={
                                        "Authorization": "Bearer cpu-budget-test-token"
                                    },
                                ).status_code
                                == 200
                            ):
                                break
                        except httpx.TransportError:
                            # diagnostic-expected: poll only the newly owned listener.
                            pass
                        time.sleep(0.1)
                    else:
                        raise AssertionError("Owned Core did not start")
                result = asyncio.run(exercise(origin, args.paragraphs))
            finally:
                if core.poll() is None:
                    core.send_signal(signal.SIGTERM)
                    try:
                        core.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        # diagnostic-expected: force only the driver's owned failed fixture.
                        core.kill()
                        core.wait()
        result["core_exit_code"] = core.returncode
        result["fixture_data_removed"] = not any(
            Path(directory).glob("nebula-host-cpu-ui-*")
        )
        assert not result["ordinary_failures"]
    result["synthetic_data_removed"] = not Path(directory).exists()
    result["source_sha256"] = {
        str(path.relative_to(repository)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [
            repository / name
            for name in (
                "src/nebula/v3/api.py",
                "src/nebula/v3/execution_kernel.py",
                "src/nebula/v3/authentication.py",
                "src/nebula/v3/cpu_workers.py",
                "src/nebula/v3/host_cpu_budget.py",
                "src/nebula/v3/report_cpu_executor.py",
                "src/nebula/v3/reporting.py",
                "tests/v3/host_cpu_budget_fixture.py",
            )
        ]
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "passed",
                    "reports_requested",
                    "completed_first_attempt",
                    "explicit_queue_rejections",
                    "load_wall_s",
                    "ordinary_reads_ms",
                    "core_exit_code",
                    "synthetic_data_removed",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
