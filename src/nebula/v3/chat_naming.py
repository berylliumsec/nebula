"""Shared conservative naming policy: greetings do not settle a chat's title."""

import re
from .domain import ChatSession

_GREETING = re.compile(
    r"\s*(?:hi|hello|hey|greetings|good (?:morning|afternoon|evening)|thanks|thank you)[!.\s]*",
    re.I,
)


def substantive_prompt(prompts: list[str]) -> str:
    return next(
        (text for text in prompts if text.strip() and not _GREETING.fullmatch(text)), ""
    )


def should_name(session: ChatSession) -> bool:
    state = session.metadata.get("initial_title_state")
    if state == "operator":
        return False
    # Existing greeting titles can improve on the next substantive exchange.
    return state != "generated" or bool(_GREETING.fullmatch(session.title))
