"""Butler's own project: created on first use, before routing reaches it.

Butler's home sessions live in ``BUTLER_HOME_PROJECT`` so they run on their own
execution worker instead of queueing behind the project they are about. Routing
refuses a project with no row, and only the host or API may create projects, so
the API creates the home project when a Butler home session is first reserved.
See docs/design/butler-home-worker.md.
"""

from __future__ import annotations

import json
import os

from .database import DeploymentHostRow, RuntimeReleaseRow
from .deployment_protocol import (
    BUTLER_HOME_FEATURE,
    BUTLER_HOME_PROJECT,
    ReleaseManifest,
)
from .domain import Engagement, EngagementStatus
from .storage import ConflictError, NebulaStore, NotFoundError

BUTLER_HOME_FLAG = "NEBULA_BUTLER_HOME_WORKER"
RESERVATION_PATH = "/api/v1/chat/control-assistant/session"


def butler_home_enabled() -> bool:
    """Butler's home worker stays off until the operator turns it on."""

    return os.environ.get(BUTLER_HOME_FLAG, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_home_reservation(method: str, path: str, body: bytes) -> bool:
    """Whether a request reserves a Butler session in Butler's home project."""

    if method != "POST" or path != RESERVATION_PATH:
        return False
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        # diagnostic-expected: a malformed body is not a home reservation; normal validation answers it.
        return False
    return isinstance(payload, dict) and payload.get("engagement_id") == (
        BUTLER_HOME_PROJECT
    )


async def target_release_hosts_butler_home(store: NebulaStore) -> bool:
    """Whether the execution target can run Butler's home worker.

    Without a staged target the host admits with its own release; admission
    then holds the home project on a release without ``butler.home/v1`` and
    says so, so only an explicit target that lacks it is refused here.
    """

    async with store.database.session() as session:
        host = await session.get(DeploymentHostRow, "host")
        if host is None or not host.target_version:
            return True
        release = await session.get(RuntimeReleaseRow, host.target_version)
    return release is not None and (
        BUTLER_HOME_FEATURE in ReleaseManifest.model_validate(release.manifest).features
    )


async def ensure_butler_home(store: NebulaStore) -> Engagement:
    """Return Butler's home project, creating it once.

    Its id is fixed, so two creators race on the same primary keys: the loser
    rolls back and reads the winner's row. The project gets the default scope
    (no targets, no bypass) and an unacknowledged host policy, so Butler runs
    no commands there until an operator grants them.
    """

    from .setup import create_engagement_with_default_scope

    try:
        return await store.get(Engagement, BUTLER_HOME_PROJECT)
    except NotFoundError:
        # diagnostic-expected: the first home reservation creates the project below.
        pass
    try:
        return await create_engagement_with_default_scope(
            store,
            Engagement(
                id=BUTLER_HOME_PROJECT,
                name="Butler",
                description="Butler's own project. Core manages it.",
                status=EngagementStatus.ACTIVE,
                metadata={"system": "butler"},
            ),
        )
    except ConflictError:
        # diagnostic-expected: a concurrent first reservation created it; read that row.
        return await store.get(Engagement, BUTLER_HOME_PROJECT)
