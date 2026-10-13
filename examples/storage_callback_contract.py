#!/usr/bin/env python3
"""Exercise a disposable storage callback responder over HTTP.

This probe executes the selected offer. Use a harmless fixture offer and a
disposable incident; it is not a dry run. See docs/storage-integrations.md.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


def callback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("callback URL must be an origin without credentials or path")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("callbacks require HTTPS, except HTTP on loopback")
    return value.rstrip("/")


def post(origin: str, path: str, body: dict, token: str | None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = Request(origin + path, method="POST", headers=headers,
                      data=json.dumps(body, separators=(",", ":")).encode())
    try:
        response = urlopen(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("callback response exceeds 64 KiB")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("callback response must be a JSON object")
        return response.status, value


def preview(origin: str, token: str, offer_id: str, incident_id: str, action_id: str) -> dict:
    path = f"/v1/actions/{quote(offer_id, safe='')}/preview"
    status, plan = post(origin, path, {"incident_id": incident_id, "action_id": action_id}, token)
    if status != 200:
        raise ValueError(f"preview returned HTTP {status}: {plan}")
    effects = plan.get("effects")
    if not isinstance(effects, list) or not effects or not all(isinstance(item, dict) for item in effects):
        raise ValueError("preview needs a nonempty effects array")
    digest = hashlib.sha256(json.dumps(effects, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()).hexdigest()
    if plan.get("digest") != digest:
        raise ValueError("preview digest does not match exact effects")
    expiry = datetime.fromisoformat(str(plan.get("expires_at")).replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    if expiry.tzinfo is None or expiry <= now or expiry > now + timedelta(minutes=10):
        raise ValueError("preview expiry must be in the next ten minutes")
    return plan


def run_contract(origin: str, token: str, incident_id: str, event_id: str,
                 offer_id: str | None = None, *, pause_for_drift: bool = False) -> dict:
    origin = callback_origin(origin)
    diagnosis_body = {"incident_id": incident_id, "event_id": event_id}
    unauthenticated, _ = post(origin, "/v1/diagnose", diagnosis_body, None)
    if unauthenticated not in {401, 403}:
        raise ValueError("diagnosis accepted a request without the callback bearer token")
    status, diagnosis = post(origin, "/v1/diagnose", diagnosis_body, token)
    if status != 200 or not isinstance(diagnosis.get("offers"), list):
        raise ValueError(f"authenticated diagnosis failed: HTTP {status}: {diagnosis}")
    offers = [item.get("id") for item in diagnosis["offers"] if isinstance(item, dict)]
    selected = offer_id or next((item for item in offers if isinstance(item, str)), None)
    if not isinstance(selected, str) or selected not in offers:
        raise ValueError("selected action offer was not returned by diagnosis")
    path = f"/v1/actions/{quote(selected, safe='')}"

    rejected_id = "contract-reject-" + uuid4().hex
    rejected = preview(origin, token, selected, incident_id, rejected_id)
    wrong_digest = "0" * 64 if rejected["digest"] != "0" * 64 else "f" * 64
    rejected_key = "contract-reject-" + uuid4().hex
    rejection, _ = post(origin, path + "/execute", {"incident_id": incident_id,
        "action_id": rejected_id, "digest": wrong_digest, "idempotency_key": rejected_key}, token)
    if rejection != 409:
        raise ValueError(f"changed preview digest was not rejected with HTTP 409 (got {rejection})")
    rejected_status, rejected_result = post(origin, path + "/status",
        {"action_id": rejected_id, "idempotency_key": rejected_key}, token)
    if rejected_status != 200 or rejected_result.get("status") != "unknown":
        raise ValueError("rejected plan unexpectedly has an execution receipt")

    if pause_for_drift:
        drift_id = "contract-drift-" + uuid4().hex
        drift_plan = preview(origin, token, selected, incident_id, drift_id)
        input("Change the disposable source plan now, then press Enter to test preview drift: ")
        drift_status, _ = post(origin, path + "/execute", {"incident_id": incident_id,
            "action_id": drift_id, "digest": drift_plan["digest"],
            "idempotency_key": "contract-drift-" + uuid4().hex}, token)
        if drift_status != 409:
            raise ValueError(f"source plan drift was not rejected with HTTP 409 (got {drift_status})")

    action_id = "contract-execute-" + uuid4().hex
    plan = preview(origin, token, selected, incident_id, action_id)
    key = "contract-execute-" + uuid4().hex
    body = {"incident_id": incident_id, "action_id": action_id,
            "digest": plan["digest"], "idempotency_key": key}
    execution_status, executed = post(origin, path + "/execute", body, token)
    lookup_status, looked_up = post(origin, path + "/status",
        {"action_id": action_id, "idempotency_key": key}, token)
    repeated_status, repeated = post(origin, path + "/execute", body, token)
    if (execution_status != 200 or lookup_status != 200 or repeated_status != 200
            or executed.get("status") != "complete" or not isinstance(executed.get("receipt"), dict)
            or not executed["receipt"] or looked_up != executed or repeated != executed):
        raise ValueError("execution, status lookup, and idempotent retry did not return one durable receipt")
    return {"authenticated": True, "preview_digest": plan["digest"],
            "changed_digest_rejected": True, "source_drift_checked": pause_for_drift,
            "idempotent_receipt": executed["receipt"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--offer-id")
    parser.add_argument("--pause-for-drift", action="store_true")
    parser.add_argument("--execute-disposable-offer", action="store_true", required=True,
                        help="explicitly acknowledge that this probe executes the selected fixture offer")
    args = parser.parse_args()
    token = os.environ.get("NEBULA_STORAGE_CALLBACK_TOKEN")
    if not token:
        parser.error("set NEBULA_STORAGE_CALLBACK_TOKEN")
    print(json.dumps(run_contract(args.origin, token, args.incident_id, args.event_id,
                                  args.offer_id, pause_for_drift=args.pause_for_drift), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
