#!/usr/bin/env python3
"""Reference source callback server. Replace DemoBackend with your own guarded backend.

The HTTP listener is loopback only. Put an HTTPS reverse proxy in front of it
for a remote Nebula Core, and keep the bearer token in an environment variable.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


def digest(effects: list[dict]) -> str:
    raw = json.dumps(effects, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


class DemoBackend:
    """A harmless example effect; implement effects/execute for a real system."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def diagnose(self, incident_id: str) -> dict:
        return {"diagnostics": "Example responder is connected. No live device diagnosis is configured.",
                "offers": [{"id": "write-demo-receipt", "title": "Write an example receipt"}]}

    def effects(self, offer_id: str, incident_id: str) -> list[dict]:
        if offer_id != "write-demo-receipt":
            raise KeyError("unknown action offer")
        return [{"operation": "write_example_receipt", "path": str(self.root / "example-receipt.json"),
                 "incident_id": incident_id}]

    def execute(self, offer_id: str, effects: list[dict], key: str) -> dict:
        path = self.root / "example-receipt.json"
        receipt = {"action": offer_id, "idempotency_key": key, "effects": effects}
        path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
        return receipt


class ActionResponder:
    def __init__(self, database: Path, backend: DemoBackend, *, now=None):
        self.database = database
        self.backend = backend
        self.now = now or (lambda: datetime.now(timezone.utc))
        database.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS previews (action_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL, offer_id TEXT NOT NULL, digest TEXT NOT NULL, effects TEXT NOT NULL, expires_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS executions (idempotency_key TEXT PRIMARY KEY, action_id TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT)")

    def _db(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def diagnose(self, body: dict) -> dict:
        return self.backend.diagnose(str(body["incident_id"]))

    def preview(self, offer_id: str, body: dict) -> dict:
        action_id, incident_id = str(body["action_id"]), str(body["incident_id"])
        effects = self.backend.effects(offer_id, incident_id)
        if not effects or len(effects) > 50:
            raise ValueError("preview must have 1-50 exact effects")
        expiry = self.now() + timedelta(minutes=5)
        plan_digest = digest(effects)
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO previews VALUES (?, ?, ?, ?, ?, ?)",
                       (action_id, incident_id, offer_id, plan_digest, json.dumps(effects), expiry.isoformat()))
        return {"effects": effects, "digest": plan_digest, "expires_at": expiry.isoformat()}

    def execute(self, offer_id: str, body: dict) -> dict:
        action_id, key, supplied = str(body["action_id"]), str(body["idempotency_key"]), str(body["digest"])
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM executions WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                if prior["action_id"] != action_id:
                    raise ValueError("idempotency key belongs to another action")
                return {"status": prior["status"] if prior["status"] != "executing" else "unknown",
                        "receipt": json.loads(prior["receipt"]) if prior["receipt"] else None}
            preview = db.execute("SELECT * FROM previews WHERE action_id=?", (action_id,)).fetchone()
            if not preview or preview["offer_id"] != offer_id or preview["incident_id"] != str(body["incident_id"]):
                raise ValueError("preview is missing or belongs to another action")
            if self.now() >= datetime.fromisoformat(preview["expires_at"]):
                raise ValueError("preview expired")
            current_effects = self.backend.effects(offer_id, preview["incident_id"])
            if supplied != preview["digest"] or digest(current_effects) != supplied:
                raise ValueError("plan drifted; preview again")
            db.execute("INSERT INTO executions VALUES (?, ?, 'executing', NULL)", (key, action_id))
        # The executing marker commits before the effect. A crash leaves an
        # uncertain result for status lookup and never repeats an unknown effect.
        # An exception may happen after a source effect. Keep the marker as
        # unknown so neither Nebula nor a retry executes it a second time.
        receipt = self.backend.execute(offer_id, current_effects, key)
        with self._db() as db:
            db.execute("UPDATE executions SET status='complete', receipt=? WHERE idempotency_key=?", (json.dumps(receipt), key))
        return {"status": "complete", "receipt": receipt}

    def status(self, body: dict) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM executions WHERE idempotency_key=? AND action_id=?",
                             (str(body["idempotency_key"]), str(body["action_id"]))).fetchone()
        if not row or row["status"] == "executing":
            return {"status": "unknown"}
        return {"status": row["status"], "receipt": json.loads(row["receipt"]) if row["receipt"] else None}


def handler_for(responder: ActionResponder, token: str):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                return self._reply(401, {"error": "bearer token required"})
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 65536:
                return self._reply(413, {"error": "request must fit 64 KiB"})
            try:
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                path = urlsplit(self.path).path
                if path == "/v1/diagnose":
                    result = responder.diagnose(body)
                else:
                    parts = path.strip("/").split("/")
                    if len(parts) != 4 or parts[:2] != ["v1", "actions"]:
                        return self._reply(404, {"error": "unknown callback"})
                    offer_id, operation = unquote(parts[2]), parts[3]
                    if operation == "preview": result = responder.preview(offer_id, body)
                    elif operation == "execute": result = responder.execute(offer_id, body)
                    elif operation == "status": result = responder.status(body)
                    else: return self._reply(404, {"error": "unknown callback"})
                return self._reply(200, result)
            except (ValueError, KeyError) as exc:
                return self._reply(409, {"error": str(exc)})

        def _reply(self, code: int, value: dict):
            payload = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    token = os.environ.get("NEBULA_STORAGE_CALLBACK_TOKEN")
    if not token:
        parser.error("set NEBULA_STORAGE_CALLBACK_TOKEN")
    responder = ActionResponder(args.state_dir / "actions.db", DemoBackend(args.state_dir))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(responder, token))
    server.serve_forever()


if __name__ == "__main__":
    main()
