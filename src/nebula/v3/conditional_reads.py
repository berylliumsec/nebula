"""Conditional GETs for the chat workspace's polled reads.

Browsers poll a conversation's state, queue, goal, published results and
activity every few seconds, and nearly every answer is the same as the last.
A poll that sends the ``ETag`` it last saw in ``If-None-Match`` gets ``304 Not
Modified`` with no body when nothing changed, so an idle tab stops moving and
re-parsing the same JSON. Responses stay ``no-store``: the browser never caches
them itself, and a client that does not send the header sees no difference.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import Request, Response
from fastapi.encoders import jsonable_encoder

POLL_CACHE_CONTROL = "no-store"


def revision_etag(prefix: str, *parts: object) -> str:
    """A strong validator for a representation identified by its revisions."""

    return '"' + "-".join([prefix, *(str(part) for part in parts)]) + '"'


def content_etag(prefix: str, content: Any) -> str:
    """A strong validator derived from the exact JSON a client would receive."""

    encoded = json.dumps(
        jsonable_encoder(content),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return f'"{prefix}-{hashlib.sha256(encoded).hexdigest()[:32]}"'


def etag_matches(request: Request, etag: str) -> bool:
    supplied = request.headers.get("if-none-match")
    if not supplied:
        return False
    candidates = {item.strip() for item in supplied.split(",")}
    # A weak comparison is enough for a read that is never range-requested.
    return "*" in candidates or any(
        item.removeprefix("W/") == etag for item in candidates
    )


def not_modified(etag: str) -> Response:
    return Response(
        status_code=304,
        headers={"ETag": etag, "Cache-Control": POLL_CACHE_CONTROL},
    )


def conditional(request: Request, response: Response, etag: str) -> Response | None:
    """Answer 304 when the client already holds ``etag``; else tag the reply."""

    if etag_matches(request, etag):
        return not_modified(etag)
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = POLL_CACHE_CONTROL
    return None
