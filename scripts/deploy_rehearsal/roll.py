#!/usr/bin/env python3
"""Roll a staged release through the existing supervisor deployment operations.

usage: roll.py <release dir> [--parallel] [--skip-api]
stage -> migrate -> prepare_api/promote_api -> target_execution, then per
project prepare_execution -> handoff -> wait verified -> retire source. This is
the in-Core sequence the external nebula_staged_deployer drives, minus the
systemd host activation. Prints a JSON timeline.
"""

import argparse
import json
import os
from pathlib import Path
import time
import uuid

import httpx

ROOT = Path(
    os.getenv("REHEARSAL_ROOT", str(Path.home() / "nebula-scratch/deploy-continuity"))
)
BASE = os.getenv(
    "RH_BASE", f"http://127.0.0.1:{os.getenv('REHEARSAL_PORT', '18440')}/api/v1"
)
DATA = Path(os.getenv("RH_DATA", str(ROOT / "core/data")))
TOKEN = (DATA / "runtime/operator-token").read_text().strip()
http = httpx.Client(
    base_url=BASE, headers={"Authorization": "Bearer " + TOKEN}, timeout=300
)
timeline = []
started = time.monotonic()


def note(event, **values):
    entry = {"t": round(time.monotonic() - started, 2), "event": event, **values}
    timeline.append(entry)
    print(json.dumps(entry), flush=True)


def status():
    return http.get("/deployment/status").raise_for_status().json()


def operation(action, parameters):
    operation_id = f"roll-{uuid.uuid4().hex}"
    for _ in range(600):
        try:
            response = http.post(
                "/deployment/operations",
                json={
                    "operation_id": operation_id,
                    "action": action,
                    "parameters": parameters,
                },
                headers={"Idempotency-Key": operation_id},
            )
        except httpx.TransportError:
            # The host may be between generations; the same ID is idempotent.
            time.sleep(0.5)
            continue
        if response.status_code == 429 or (
            response.status_code == 503 and "Retry-After" in response.headers
        ):
            time.sleep(min(float(response.headers.get("Retry-After", "0.5")), 10))
            continue
        response.raise_for_status()
        receipt = response.json()
        if receipt.get("state") != "succeeded":
            raise RuntimeError(f"{action} {receipt}")
        return receipt.get("result") or {}
    raise RuntimeError(f"{action} stayed busy")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("release")
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--phase", choices=("all", "prepare", "roll"), default="all")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--only", default="", help="hand off only this project")
    args = parser.parse_args()
    release = Path(args.release).resolve()
    digest = (release / "nebula-runtime.sha256").read_text().strip()
    version = json.loads((release / "nebula-runtime.json").read_text())["version"]
    if args.phase in ("all", "prepare"):
        version = operation(
            "stage", {"source": str(release), "manifest_sha256": digest}
        )["version"]
        note("staged", version=version)
        operation(
            "migrate", {"version": version, "expected_revision": status()["revision"]}
        )
        note("migrated")
        if args.phase == "prepare":
            return
    if not args.skip_api:
        api = operation("prepare_api", {"version": version})
        note("api_prepared", runtime_id=api["runtime_id"])
        operation(
            "promote_api",
            {
                "runtime_id": api["runtime_id"],
                "expected_revision": status()["revision"],
            },
        )
        note("api_promoted", api_version=status()["api_version"])
    operation("target_execution", {"version": version})
    note("targeted")
    moved, pinned = [], []
    for project in status()["projects"]:
        project_id, source = project["project_id"], project.get("runtime_id")
        if not source or project.get("runtime_version") == version:
            continue
        if args.only and project_id != args.only:
            continue
        begin = time.monotonic()
        target = operation("prepare_execution", {"version": version})["runtime_id"]
        note(
            "target_prepared",
            project=project_id,
            target=target,
            seconds=round(time.monotonic() - begin, 2),
        )
        operation("handoff", {"project_id": project_id, "target_runtime_id": target})
        note("handoff_requested", project=project_id)
        last = None
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            row = next(
                item
                for item in status()["projects"]
                if item["project_id"] == project_id
            )
            shape = (
                row.get("phase"),
                row.get("runtime_id"),
                tuple(row.get("blockers") or ()),
                row.get("admission_held"),
            )
            if shape != last:
                note(
                    "phase",
                    project=project_id,
                    phase=row.get("phase"),
                    owner=row.get("runtime_id"),
                    fence=row.get("fencing_token"),
                    held=row.get("admission_held"),
                    blockers=row.get("blockers"),
                )
                last = shape
            if row.get("phase") == "verified" and row.get("runtime_id") == target:
                break
            if row.get("blockers") and not row.get("admission_held"):
                pinned.append(row)
                break
            time.sleep(0.25)
        else:
            raise RuntimeError(f"handoff of {project_id} did not verify")
        if pinned and pinned[-1]["project_id"] == project_id:
            operation("retire", {"runtime_id": target})
            note("pinned_target_retired", project=project_id)
            continue
        operation("retire", {"runtime_id": source})
        note(
            "source_retired",
            project=project_id,
            source=source,
            seconds=round(time.monotonic() - begin, 2),
        )
        moved.append(project_id)
    final = status()
    note(
        "done",
        moved=len(moved),
        pinned=len(pinned),
        resident_versions=final["resident_versions"],
        api_version=final["api_version"],
    )
    out = os.getenv("ROLL_OUT")
    if out:
        Path(out).write_text(
            json.dumps({"timeline": timeline, "final": final}, indent=2)
        )


if __name__ == "__main__":
    main()
