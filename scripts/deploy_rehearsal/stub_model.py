"""Fake OpenAI-compatible model for deploy rehearsals.

Logs every streamed request with the [rh:...] markers of its latest user
message (STUB_LOG), so a rehearsal can count provider requests per message.

Streams plausible replies with realistic timing so staging chats exercise the
real provider path without any external API. Stdlib only.

Env:
  STUB_PORT            listen port (default 18190, loopback only)
  STUB_FIRST_TOKEN_MS  delay before the first streamed token (default 800)
  STUB_CHUNKS          streamed content chunks per reply (default 20)
  STUB_CHUNK_MS        delay between chunks (default 100)
"""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("STUB_PORT", "18190"))
FIRST_TOKEN_MS = int(os.getenv("STUB_FIRST_TOKEN_MS", "800"))
CHUNKS = int(os.getenv("STUB_CHUNKS", "20"))
CHUNK_MS = int(os.getenv("STUB_CHUNK_MS", "100"))
MODEL = "staging-model"

WORDS = (
    "the service exposes a login form and a status endpoint; next I will review "
    "response headers, check the certificate chain, compare banner versions and "
    "summarize which findings need confirmation before the report"
).split()

stats = {"requests": 0, "streams": 0, "tool_calls": 0, "active": 0}
LOG = os.getenv("STUB_LOG")

MARK = re.compile(r"\[rh:[^\]]+\]")
lock = threading.Lock()


def completion(message: dict, finish: str) -> dict:
    return {
        "id": f"chatcmpl-stub-{random.randrange(1 << 30)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
    }


def tool_call(name: str, arguments: dict) -> dict:
    return completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{random.randrange(1 << 30)}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        },
        "tool_calls",
    )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):  # quiet
        pass

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL,
                            "object": "model",
                            "created": 1,
                            "owned_by": "staging",
                        }
                    ],
                },
            )
        elif self.path == "/stats":
            with lock:
                self._json(200, dict(stats))
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": "not found"})
            return
        body = json.loads(
            self.rfile.read(int(self.headers.get("Content-Length", "0")) or 0) or b"{}"
        )
        with lock:
            stats["requests"] += 1
            stats["active"] += 1
        try:
            self._respond(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with lock:
                stats["active"] -= 1

    def _respond(self, body: dict) -> None:
        tools = [
            t.get("function", {})
            for t in body.get("tools") or []
            if isinstance(t, dict)
        ]
        names = {t.get("name") for t in tools}
        probe = next(
            (t for t in tools if t.get("name") == "nebula_capability_probe"), None
        )
        if probe:
            nonce = (
                ((probe.get("parameters") or {}).get("properties") or {}).get("nonce")
                or {}
            ).get("enum", [""])[0]
            self._json(200, tool_call("nebula_capability_probe", {"nonce": nonce}))
            return
        if "finish_response" in names:
            with lock:
                stats["tool_calls"] += 1
            self._json(200, tool_call("finish_response", {}))
            return
        text = " ".join(
            m.get("content", "")
            for m in body.get("messages", [])
            if isinstance(m.get("content"), str)
        )
        if "Name this conversation" in text:
            self._json(
                200,
                completion(
                    {"role": "assistant", "content": "Staging Load Chat"}, "stop"
                ),
            )
            return
        words = [random.choice(WORDS) for _ in range(CHUNKS * 3)]
        if not body.get("stream"):
            time.sleep((FIRST_TOKEN_MS + CHUNKS * CHUNK_MS) / 1000)
            self._json(
                200,
                completion(
                    {
                        "role": "assistant",
                        "content": " ".join(words).capitalize() + ".",
                    },
                    "stop",
                ),
            )
            return
        with lock:
            stats["streams"] += 1
        users = [
            m.get("content")
            for m in body.get("messages", [])
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        last = users[-1] if users else ""
        marks = MARK.findall(last)
        if LOG:
            with lock, open(LOG, "a") as out:
                out.write(
                    json.dumps({"t": time.time(), "marks": marks, "users": len(users)})
                    + "\n"
                )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        def chunk(delta: dict, finish=None, usage=None) -> None:
            payload = {
                "id": "chatcmpl-stub-stream",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": MODEL,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            if usage:
                payload["usage"] = usage
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        time.sleep(FIRST_TOKEN_MS / 1000)
        chunk({"role": "assistant", "content": ""})
        for i in range(CHUNKS):
            piece = " ".join(words[i * 3 : (i + 1) * 3])
            chunk({"content": (piece.capitalize() if i == 0 else " " + piece)})
            time.sleep(CHUNK_MS / 1000)
        chunk({"content": "."}, None)
        if LOG:
            with lock, open(LOG, "a") as out:
                out.write(json.dumps({"t": time.time(), "done": marks}) + "\n")
        chunk(
            {},
            "stop",
            {
                "prompt_tokens": 120,
                "completion_tokens": CHUNKS * 3,
                "total_tokens": 120 + CHUNKS * 3,
            },
        )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"stub model on http://127.0.0.1:{PORT}/v1", flush=True)
    server.serve_forever()
