"""Review a public Nebula commit on Butler's configured harness profiles.

The local public-commit guard (``scripts/public_commit_guard.py``) can hand its
review to a ``command`` reviewer. On a host where Butler commits to the public
repository, that command asks Core, and Core runs the review here: one fresh,
tool-free turn (``run_harness_one_shot``) on each harness profile named by
``NEBULA_BUTLER_PUBLIC_REVIEW_HARNESS_PROFILE_IDS``, in order, until one gives a
verdict. Without that setting the profile named by
``NEBULA_BUTLER_REVIEW_HARNESS_PROFILE_ID`` reviews alone. Each profile runs on
its own account folder and default model, exactly as Butler's harnesses do.

Each request reviews on one profile, the ``attempt``-th, so that one host
effect stays inside Core's 150-second host-effect bound. The caller asks the
next attempt only when Core reports the profile unavailable: it is missing,
disabled, not cleared for sensitive data, has no default model, or its turn
fails, times out or returns something that is not a JSON object. Core returns
the verdict unchanged; the guard validates it and treats ``block`` and
``uncertain`` as final. Butler's conversation is never part of the review, and
the route is left out of the Settings assistant's catalog so Butler cannot ask
for a review itself.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import Field

from .butler_supervisor_policy import REVIEW_HARNESS_ENV
from .domain import HarnessProfile, NebulaModel
from .storage import NebulaStore, NotFoundError

PROFILES_ENV = "NEBULA_BUTLER_PUBLIC_REVIEW_HARNESS_PROFILE_IDS"
MAX_PROFILES = 4
MAX_PROMPT_CHARS = 64_000
# One reviewer turn; with opening and closing the harness it stays inside the
# 150-second host-effect bound.
REVIEW_TIMEOUT_SECONDS = 110
VERDICT_INSTRUCTION = (
    "\n\nReply with only the JSON verdict object, no prose and no code fence. "
    "Its JSON schema:\n"
)
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["allow", "block", "uncertain"]},
        "category": {
            "type": "string",
            "enum": [
                "none",
                "research_data",
                "private_artifact",
                "credential",
                "uncertain",
            ],
        },
        "reviewed_files": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "category", "reviewed_files"],
    "additionalProperties": False,
}

# (harness profile, model, prompt, timeout seconds) -> reply text of one fresh,
# tool-free turn.
PublicReviewRunner = Callable[[HarnessProfile, str, str, float], Awaitable[str]]


class PublicCommitReviewRequest(NebulaModel):
    # The guard's whole review prompt, staged text included.
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    # The guard sends its schema; Core always asks for its own fixed one.
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    # Which configured profile reviews: 0 first, then the next after an
    # unavailable answer.
    attempt: int = Field(default=0, ge=0, lt=MAX_PROFILES)


class PublicReviewUnavailable(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def configured_profile_ids(environ: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    raw = env.get(PROFILES_ENV) or env.get(REVIEW_HARNESS_ENV) or ""
    ids = list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if len(ids) > MAX_PROFILES:
        raise PublicReviewUnavailable(
            "invalid_configuration",
            f"{PROFILES_ENV} names more than {MAX_PROFILES} harness profiles",
        )
    return ids


def _parsed_verdict(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```")
    try:
        verdict = json.loads(text.strip())
    except json.JSONDecodeError:
        # diagnostic-expected: a reply that is not JSON makes this reviewer unavailable; the next profile is asked.
        return None
    return verdict if isinstance(verdict, dict) else None


async def review_public_commit(
    store: NebulaStore,
    prompt: str,
    runner: PublicReviewRunner,
    *,
    attempt: int = 0,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Review on the ``attempt``-th configured profile.

    Returns ``{"verdict", "reviewer", ...}`` when that profile answered, or
    ``{"unavailable", "next_attempt", ...}`` when it could not;
    ``next_attempt`` is ``None`` after the last configured profile.
    """

    from .harnesses import HarnessConfigurationError, HarnessTransportError

    ids = configured_profile_ids(environ)
    if not ids:
        raise PublicReviewUnavailable(
            "not_configured",
            f"set {PROFILES_ENV} (or {REVIEW_HARNESS_ENV}) to Butler's reviewer "
            "harness profiles",
        )
    if attempt >= len(ids):
        raise PublicReviewUnavailable(
            "no_such_attempt", f"only {len(ids)} reviewer profiles are configured"
        )
    profile_id = ids[attempt]
    result: dict[str, Any] = {"attempt": attempt, "profiles": len(ids)}

    def unavailable(reason: str) -> dict[str, Any]:
        return {
            **result,
            "unavailable": reason,
            "next_attempt": attempt + 1 if attempt + 1 < len(ids) else None,
        }

    try:
        profile = await store.get(HarnessProfile, profile_id)
    except NotFoundError:
        # diagnostic-expected: a deleted reviewer profile is reported to the guard, which asks the next one.
        return unavailable(f"{profile_id}: harness profile not found")
    label = f"{profile.name} ({profile_id})"
    if not profile.enabled:
        return unavailable(f"{label}: disabled")
    if not profile.privacy.local_only and not profile.privacy.permits_sensitive_data:
        return unavailable(f"{label}: not cleared for sensitive data")
    model = profile.default_model
    if not model:
        return unavailable(f"{label}: no default model")
    full_prompt = prompt + VERDICT_INSTRUCTION + json.dumps(VERDICT_SCHEMA)
    try:
        text = await runner(profile, model, full_prompt, REVIEW_TIMEOUT_SECONDS)
    except (HarnessConfigurationError, HarnessTransportError) as error:
        # diagnostic-expected: an unavailable reviewer harness is reported to the guard, which asks the next one.
        return unavailable(f"{label}: {str(error)[:200]}")
    except TimeoutError:
        # diagnostic-expected: a reviewer turn past its bound is reported to the guard, which asks the next one.
        return unavailable(f"{label}: timed out")
    verdict = _parsed_verdict(text)
    if verdict is None:
        return unavailable(f"{label}: reply was not a JSON verdict")
    return {
        **result,
        "verdict": verdict,
        "reviewer": {
            "harness_profile_id": profile.id,
            "name": profile.name,
            "kind": profile.kind.value,
            "model": model,
        },
    }
