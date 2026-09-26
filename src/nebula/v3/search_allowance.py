"""A turn's allowance of ``conversation.search`` and ``knowledge.search`` calls.

Both searches ask the model to stop once searching stops finding anything, and
models still searched twenty to a hundred times in one turn, re-confirming
facts they already had, each search another provider round trip over a
growing history. Core enforces an allowance per tool: once a turn has run
``TURN_SEARCH_BUDGET`` searches of one kind, or its last
``TURN_EMPTY_SEARCH_LIMIT`` found nothing, the searches of any later routing
response are answered by Core instead of run. The answer is an ordinary
result, not an error: nothing failed, and the turn goes on.

The count is read from the turn's own ledger, the searches that ran, so a turn
resumed after a restart keeps it. The searches one routing response asks for
together are checked against the earlier responses only, so a response's batch
runs whole or not at all and no half-checked set reaches the answer.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from .conversation_search import CONVERSATION_SEARCH_TOOL_NAME
from .knowledge_search import KNOWLEDGE_SEARCH_TOOL_NAME

# What each allowance-bound search reads, as the answer names it.
SEARCH_ALLOWANCE_TOOLS: dict[str, str] = {
    CONVERSATION_SEARCH_TOOL_NAME: "the earlier conversation",
    KNOWLEDGE_SEARCH_TOOL_NAME: "the project's knowledge",
}
# Searches of one kind a turn runs before a later response's are answered. A
# turn looks up the details its answer needs, a batch of them in one response;
# beyond a second round it is re-reading what it found.
TURN_SEARCH_BUDGET = 8
# Searches of one kind in a row that found nothing: what is looked for is most
# likely not there, and rewording again rarely changes that.
TURN_EMPTY_SEARCH_LIMIT = 3


def _found_nothing(entry: Mapping[str, Any]) -> bool:
    result = entry.get("provider_result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (
            ValueError
        ):  # diagnostic-expected: an unreadable receipt is not an empty search
            return False
    return isinstance(result, dict) and result.get("result_count") == 0


def turn_search_allowance_spent(
    history: Iterable[Mapping[str, Any]],
    tool_name: str,
    query: str,
    *,
    response_group: str | None = None,
) -> dict[str, Any] | None:
    """The result a further ``tool_name`` search gets instead of running, if any.

    ``history`` is the turn's ledger; searches of ``response_group``, the
    routing response asking for this one, do not count.
    """

    subject = SEARCH_ALLOWANCE_TOOLS.get(tool_name)
    if subject is None:
        return None
    searches = [
        entry
        for entry in history
        if entry.get("name") == tool_name
        and entry.get("budget_class") == "artifact_query"
        and not (response_group and entry.get("response_group") == response_group)
    ]
    empty = 0
    for entry in reversed(searches):
        if not _found_nothing(entry):
            break
        empty += 1
    # Results cleared from the model's context are still retained; reading
    # one again costs nothing a search would find anew.
    reread = (
        " A result of this turn's earlier searches that was cleared from your "
        "context can still be read with tool_output.search by its tool_call_id."
    )
    if empty >= TURN_EMPTY_SEARCH_LIMIT:
        detail = (
            f"This search did not run. The last {empty} searches of {subject} "
            "found nothing, so what you are looking for is most likely not "
            "there, and further searches this turn get this notice. Answer the "
            "operator's request as they asked, from what you already have; "
            "call a detail missing only if you have it from nowhere." + reread
        )
    elif len(searches) >= TURN_SEARCH_BUDGET:
        detail = (
            f"This search did not run: this turn has already run {len(searches)} "
            f"searches of {subject}, past its allowance of {TURN_SEARCH_BUDGET}, "
            "and further searches get this notice. Answer the operator's "
            "request as they asked, from what you already have; call a detail "
            "missing only if you have it from nowhere." + reread
        )
    else:
        return None
    # No empty result list: nothing was searched, so nothing was not found.
    return {
        "tool": tool_name,
        "query": query,
        "search_budget_spent": True,
        "searches_this_turn": len(searches),
        "detail": detail,
    }


__all__ = [
    "SEARCH_ALLOWANCE_TOOLS",
    "TURN_EMPTY_SEARCH_LIMIT",
    "TURN_SEARCH_BUDGET",
    "turn_search_allowance_spent",
]
