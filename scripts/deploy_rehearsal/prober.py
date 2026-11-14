#!/usr/bin/env python3
"""Probe /health every 100 ms until a stop file appears; summarize outcomes."""

import json
import os
import sys
import time
from pathlib import Path
import httpx

base = os.getenv(
    "RH_BASE", f"http://127.0.0.1:{os.getenv('REHEARSAL_PORT', '18440')}/api/v1"
)
root = Path(
    os.getenv("REHEARSAL_ROOT", str(Path.home() / "nebula-scratch/deploy-continuity"))
)
token = (root / "core/data/runtime/operator-token").read_text().strip()
stop, out = Path(sys.argv[1]), Path(sys.argv[2])
results = []
with httpx.Client(timeout=60, headers={"Authorization": "Bearer " + token}) as client:
    while not stop.exists():
        began = time.monotonic()
        try:
            response = client.get(base + "/health", headers={"Connection": "close"})
            outcome = str(response.status_code) + (
                ":ready"
                if response.status_code == 200 and response.json().get("api_ready")
                else ""
            )
        except httpx.HTTPError as error:
            outcome = type(error).__name__
        results.append(
            {
                "t": time.time(),
                "outcome": outcome,
                "ms": round((time.monotonic() - began) * 1000),
            }
        )
        time.sleep(max(0, 0.1 - (time.monotonic() - began)))
counts = {}
for item in results:
    counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
slowest = sorted(results, key=lambda item: -item["ms"])[:5]
out.write_text(
    json.dumps(
        {"probes": len(results), "outcomes": counts, "slowest": slowest}, indent=2
    )
)
print(
    json.dumps(
        {
            "probes": len(results),
            "outcomes": counts,
            "max_ms": slowest[0]["ms"] if slowest else None,
        }
    )
)
