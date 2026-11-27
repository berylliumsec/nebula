#!/usr/bin/env python3
"""Review a public Nebula commit on Butler's configured harnesses, through local Core.

This is a ``command`` reviewer for ``scripts/public_commit_guard.py``. Name it in
the untracked hook configuration on the Core host, for example::

    {"reviewers": [{"kind": "command", "label": "Nebula Butler",
                    "argv": ["/usr/bin/python3", "-I",
                             "/path/to/scripts/simulation/nebula_public_review.py"]}]}

It reads the guard's ``{"prompt", "schema"}`` JSON on stdin and asks Core's
``POST /api/v1/harnesses/public-commit-review`` once per configured reviewer
profile (``NEBULA_BUTLER_PUBLIC_REVIEW_HARNESS_PROFILE_IDS`` in Core's service
environment), moving on only when Core reports a profile unavailable. The first
verdict is printed to stdout for the guard to validate. Any other outcome exits
non-zero with a one-line reason on stderr, which the guard treats as
"reviewer unavailable" and so fails closed when no other reviewer is configured.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nebula_deploy_gate import service_token  # noqa: E402

DEFAULT_ORIGIN = "http://127.0.0.1:8000"
DEFAULT_TOKEN_FILE = Path("/home/simulation/.config/nebula/service.env")
LOCAL_ORIGINS = {"http://127.0.0.1:8000", "http://127.0.0.1:18000"}
ROUTE = "/api/v1/harnesses/public-commit-review"
REQUEST_TIMEOUT_SECONDS = 30
# One accepted review stays inside Core's 150-second host-effect bound.
ATTEMPT_DEADLINE_SECONDS = 170
MAX_ATTEMPTS = 4


class ReviewFailed(Exception):
    pass


def _call(origin: str, token: str, method: str, path: str, body: bytes | None = None,
          key: str | None = None) -> tuple[int, dict[str, str], Any]:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if key is not None:
        headers["Idempotency-Key"] = key
    request = Request(origin + path, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, dict(response.headers), json.load(response)
    except HTTPError as error:
        try:
            detail = json.load(error).get("detail")
        except (ValueError, AttributeError):
            # diagnostic-expected: a non-JSON error body is reported by its status alone.
            detail = None
        if isinstance(detail, dict):
            detail = detail.get("detail") or detail.get("code")
        raise ReviewFailed(f"Core answered HTTP {error.code}" + (f": {detail}" if detail else "")) from None
    except (URLError, TimeoutError, ConnectionError, ValueError) as error:
        raise ReviewFailed(f"Core is not reachable at {origin} ({error})") from None


def review_attempt(origin: str, token: str, prompt: str, attempt: int) -> dict[str, Any]:
    body = json.dumps({"prompt": prompt, "attempt": attempt}).encode("utf-8")
    deadline = time.monotonic() + ATTEMPT_DEADLINE_SECONDS
    status, headers, payload = _call(origin, token, "POST", ROUTE, body, key=f"public-review-{uuid.uuid4()}")
    while status == 202:
        command_id = headers.get("X-Nebula-Command-Id") or headers.get("x-nebula-command-id")
        if not command_id or time.monotonic() > deadline:
            raise ReviewFailed("Core accepted the review but did not finish it in time")
        status, headers, payload = _call(
            origin, token, "GET", f"/api/v1/runtime/requests/{command_id}/response"
        )
    if status != 200 or not isinstance(payload, dict):
        raise ReviewFailed(f"Core answered HTTP {status} without a review result")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, choices=sorted(LOCAL_ORIGINS))
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read())
        prompt = request.get("prompt") if isinstance(request, dict) else None
        if not isinstance(prompt, str) or not prompt:
            raise ReviewFailed("the guard sent no review prompt")
        try:
            token = service_token(args.token_file)
        except (OSError, ValueError) as error:
            raise ReviewFailed(f"the Core service token is unavailable ({error})") from None
        reasons: list[str] = []
        attempt: int | None = 0
        while attempt is not None and attempt < MAX_ATTEMPTS:
            result = review_attempt(args.origin, token, prompt, attempt)
            if isinstance(result.get("verdict"), dict):
                reviewer = result.get("reviewer") or {}
                print(
                    f"Reviewed on {reviewer.get('name', 'a Butler harness')} ({reviewer.get('model', '?')}).",
                    file=sys.stderr,
                )
                print(json.dumps(result["verdict"]))
                return 0
            reasons.append(str(result.get("unavailable") or "no verdict"))
            next_attempt = result.get("next_attempt")
            attempt = next_attempt if isinstance(next_attempt, int) and next_attempt > attempt else None
        raise ReviewFailed("no Butler reviewer harness was available: " + "; ".join(reasons))
    except (ReviewFailed, json.JSONDecodeError) as error:
        print(f"Butler review unavailable: {error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
