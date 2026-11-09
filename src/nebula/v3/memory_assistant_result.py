"""Bounded structured memory observations, preserving policy and unknown states."""

import json

MEMORY_SETTINGS_LIMIT = 128_000
_JOB_PAGE_LIMIT = 24_000
_PREVIEW_COUNT = 32


def _capability(value):
    return {
        key: item[:200] if key == "detail" and isinstance(item, str) else item
        for key, item in value.items()
    }


def memory_assistant_result(path, value):
    """Project presentation only; the HTTP response and saved objects are intact."""
    if not isinstance(value, dict):
        return value
    if path == "/api/v1/memory-budgets":
        result = dict(value)
        profiles = value.get("profiles", [])
        result["profiles"] = [
            {
                **profile,
                "name": profile["name"][:128],
                "capabilities": _capability(profile["capabilities"])
                if profile.get("capabilities")
                else None,
            }
            for profile in profiles[:_PREVIEW_COUNT]
        ]
        result["profile_display_limit"] = _PREVIEW_COUNT
        result["profiles_at_limit"] = len(profiles) > _PREVIEW_COUNT
        result["profiles_summary_only"] = True
        capabilities = value.get("capabilities", [])
        result["capabilities"] = [
            _capability(item) for item in capabilities[:_PREVIEW_COUNT]
        ]
        result["capabilities_at_limit"] = len(capabilities) > _PREVIEW_COUNT
        result["capabilities_summary_only"] = True
        # Policy, revisions, effective capacity, application state and admin
        # ceilings remain complete, including every permitted profile override.
        return result
    if path != "/api/v1/memory-budgets/jobs" or not isinstance(
        value.get("items"), list
    ):
        return value
    items, size = [], 0
    for job in value["items"]:
        item = {
            key: job.get(key)
            for key in (
                "id",
                "project_id",
                "revision",
                "resource_kind",
                "resource_id",
                "state",
                "reserved_bytes",
                "pending_limits",
                "application_state",
                "outcome",
            )
        }
        snapshot = job["snapshot"]
        item["snapshot"] = {
            key: snapshot.get(key)
            for key in (
                "policy_revision",
                "environment",
                "profile_id",
                "source",
                "limits",
            )
        }
        evidence = job.get("evidence")
        item["evidence"] = (
            {
                key: evidence.get(key)
                for key in (
                    "applied",
                    "current_bytes",
                    "peak_bytes",
                    "observed_at",
                    "protected_process_tree",
                    "application_uncertain",
                    "termination_reason",
                    "oom_kill_count",
                    "oom_victim_confirmed",
                    "group_empty",
                    "auxiliary_groups_empty",
                )
            }
            if evidence
            else None
        )
        detail = job.get("detail")
        item["detail"] = detail[:200] if isinstance(detail, str) else None
        item["detail_truncated"] = isinstance(detail, str) and len(detail) > 200
        length = len(json.dumps(item))
        if size + length > _JOB_PAGE_LIMIT:
            break
        items.append(item)
        size += length + 2
    return {
        **value,
        "items": items,
        "display_limit": len(items),
        "at_limit": bool(value.get("at_limit")) or len(items) < len(value["items"]),
        "summary_only": True,
    }
