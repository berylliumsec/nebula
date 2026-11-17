"""Linked memory vocabulary and pure rules, shared by Core and the helper.

Standard library only, so the curation helper process stays light.
"""

from __future__ import annotations

import hashlib
import os

KILL_SWITCH_ENV = "NEBULA_LINKED_MEMORY_DISABLED"
CURATOR_VERSION = "lm-1"

# Stages from the design. Only ``off`` and ``shadow`` exist in this release.
AVAILABLE_STAGES: tuple[str, ...] = ("off", "shadow")
LATER_STAGES: tuple[str, ...] = ("recall", "inject")

CARD_KINDS = (
    "decision",
    "fact",
    "outcome",
    "constraint",
    "lesson",
    "open_question",
)
# Strongest first (design: Authority and trust).
AUTHORITY_ORDER = (
    "operator_decision",
    "operator_statement",
    "runtime_observation",
    "tool_result",
    "source_inspection",
    "agent_inference",
    "external_record",
)
LINK_TYPES = (
    "supersedes",
    "updates",
    "contradicts",
    "depends_on",
    "continues",
    "followed_by",
    "similar_to",
)
CARD_STATES = (
    "active",
    "superseded",
    "contradicted",
    "retracted",
    "quarantined",
)
RUN_STATES = ("pending", "running", "paused", "done", "capped", "failed", "skipped")
OPEN_RUN_STATES = ("pending", "running", "paused")


def kill_switch_engaged() -> bool:
    """The Core-wide switch overrides every project's stage."""

    return os.environ.get(KILL_SWITCH_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def authority_rank(authority: str) -> int:
    try:
        return AUTHORITY_ORDER.index(authority)
    except ValueError:
        # diagnostic-expected: an unknown authority ranks weakest.
        return len(AUTHORITY_ORDER)


def normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def card_content_hash(kind: str, title: str, summary: str) -> str:
    return hashlib.sha256(
        "\0".join([kind, normalize_text(title), normalize_text(summary)]).encode()
    ).hexdigest()


def project_stream_id(engagement_id: str) -> str:
    """The project's oversight event stream (``butler_oversight.project_stream_id``)."""

    return "butler-project:" + hashlib.sha256(engagement_id.encode("utf-8")).hexdigest()
