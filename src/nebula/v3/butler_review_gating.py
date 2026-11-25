"""Closure gating that keeps a goal's completion independent of its own word.

These rules sit beside ``butler_independent_review`` and apply to every route
that can finish a goal:

- **No self-approval.** Approval is only Core's independent reviewer record
  (``metadata["independent_review"]`` with ``approval_provenance``). A goal
  cannot write it, and a Butler that steered a goal (a Butler supervisor note,
  a Butler guidance change, or a Butler goal action recorded under
  ``metadata["butler_steering"]``) cannot complete that goal without it, even
  when the goal does not otherwise require review.
- **Forged sign-off lines.** Goal-authored text that claims a supervisor,
  reviewer or operator sign-off or verdict is detected by
  :func:`detect_forged_signoff`. A completion proposal containing one is
  rejected at the goal's own completion route. In a review packet such lines
  are removed from goal-authored text and listed under
  ``forged_signoff_flags`` so the reviewer sees that they were there.
- **MUST-DO gating.** A reviewer's must-dos are stored as
  ``metadata["review_must_dos"]`` items (``id``, ``text``, ``status`` of
  ``open``/``satisfied``/``waived``, ``evidence``). Butler and the goal's own
  route cannot complete while any item is open. Butler may satisfy an item
  only with an artifact Core verifies (:func:`must_do_resolve`); only the
  operator route or a later independent review may waive one.
- **Closure request rate limit.** A goal may request closure at most
  ``NEBULA_BUTLER_CLOSURE_REQUESTS_PER_DAY`` (default 3) times per rolling
  24 hours. Every request and refusal is recorded in
  ``metadata["closure_requests"]``.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .domain import ChatGoal, RiskClass, utc_now
from .storage import REVISE_ATTEMPTS, RevisionConflict
from .tools import AnalysisTool, IdempotencyBehavior, ToolSpec


MUST_DOS_KEY = "review_must_dos"
CLOSURE_REQUESTS_KEY = "closure_requests"
APPROVAL_PROVENANCE_KEY = "approval_provenance"
STEERING_KEY = "butler_steering"
REOPEN_HISTORY_KEY = "reopened"

CLOSURE_REQUESTS_ENV = "NEBULA_BUTLER_CLOSURE_REQUESTS_PER_DAY"
EVIDENCE_REPOS_ENV = "NEBULA_BUTLER_REVIEW_EVIDENCE_REPOS"
DEFAULT_CLOSURE_REQUESTS_PER_DAY = 3
CLOSURE_WINDOW = timedelta(days=1)
CLOSURE_REQUEST_HISTORY_LIMIT = 50
STEERING_HISTORY_LIMIT = 50
MUST_DO_LIMIT = 100

MUST_DO_STATUSES = frozenset({"open", "satisfied", "waived"})
FORGED_SIGNOFF_PLACEHOLDER = "[removed by Core: goal-authored sign-off claim]"


class MustDoError(ValueError):
    """A must-do change Core refuses."""


# --------------------------------------------------------------------------
# Forged sign-off detection
# --------------------------------------------------------------------------

_AUTHORITY = (
    r"(?:supervisor|reviewer|independent[\s_-]*review(?:er)?|closure[\s_-]*review"
    r"(?:er)?|operator|butler|core\s+review(?:er)?)"
)
_CLAIM = (
    r"(?:sign[\s_-]*off|signed[\s_-]*off|verdict|approval|approved|approves|"
    r"accepted|accepts|accept|lgtm|passed)"
)
_AFFIRMATIVE = re.compile(
    r"\b(?:approved|accepted|granted|signed[\s_-]*off|passed|lgtm|"
    r"ACCEPT(?:_WITH_RESIDUALS)?|NOT_READY|REJECT(?:ED)?)\b|[:=]\s*\S",
    re.IGNORECASE,
)
_PENDING = re.compile(
    r"\b(?:pending|awaiting|await|requires?|required|needs?|request(?:s|ed|ing)?|"
    r"until|before|seek(?:s|ing)?|ask(?:s|ed|ing)?|wait(?:s|ing)?|not\s+yet|"
    r"no\s+one|nobody|without)\b",
    re.IGNORECASE,
)
_FORGED_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "verdict: ACCEPT", "Verdict = approved", "**Verdict**: NOT_READY"
    re.compile(
        r"\bverdict\b[\s*_`]*[:=\-]\s*[*_`]*\s*(?:ACCEPT(?:_WITH_RESIDUALS)?|REJECT|"
        r"NOT_READY|approved?|pass(?:ed)?|ok)\b",
        re.IGNORECASE,
    ),
    # "sign-off: yes", "Signoff = granted"
    re.compile(r"\bsign[\s_-]*off\b[\s*_`]*[:=]", re.IGNORECASE),
    # "signed off by the supervisor", "approved by the independent reviewer"
    re.compile(
        r"\b(?:signed[\s_-]*off|approved|accepted|verified)\s+by\s+(?:the\s+|an?\s+)?"
        + _AUTHORITY,
        re.IGNORECASE,
    ),
    # Core's own review note header copied into goal text.
    re.compile(r"INDEPENDENT\s+CLOSURE\s+REVIEW", re.IGNORECASE),
)
_AUTHORITY_CLAIM = re.compile(
    r"\b" + _AUTHORITY + r"\b[^\n]{0,48}?\b" + _CLAIM + r"\b", re.IGNORECASE
)


def detect_forged_signoff(text: str | None) -> list[str]:
    """Lines of goal-authored ``text`` that claim a sign-off or verdict.

    Goal-authored text can only make claims; a supervisor or reviewer
    approval is real only as Core's reviewer record. A line is flagged when
    it states a verdict or sign-off as a value ("verdict: ACCEPT",
    "sign-off: yes"), attributes approval to an authority ("approved by the
    supervisor"), copies Core's review header, or pairs an authority with an
    approval word unless it only says the approval is still pending
    ("awaiting supervisor sign-off"). Returns the flagged lines, stripped and
    clipped to 300 characters, in order.
    """

    if not text:
        return []
    flagged: list[str] = []
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line:
            continue
        hit = any(pattern.search(line) for pattern in _FORGED_PATTERNS)
        if not hit and _AUTHORITY_CLAIM.search(line):
            hit = not _PENDING.search(line) or bool(_AFFIRMATIVE.search(line))
        if hit:
            flagged.append(line[:300])
    return flagged


def strip_forged_signoff(text: str) -> tuple[str, list[str]]:
    """``text`` with flagged lines replaced by a placeholder, and the lines."""

    flagged: list[str] = []
    kept: list[str] = []
    for raw in text.splitlines(keepends=True):
        found = detect_forged_signoff(raw)
        if found:
            flagged += found
            kept.append(
                FORGED_SIGNOFF_PLACEHOLDER + ("\n" if raw.endswith("\n") else "")
            )
        else:
            kept.append(raw)
    return "".join(kept), flagged


def sanitize_goal_text(value: Any) -> tuple[Any, list[str]]:
    """Strip forged sign-off lines from every string in a JSON-like value."""

    if isinstance(value, str):
        return strip_forged_signoff(value)
    if isinstance(value, list):
        items: list[Any] = []
        flags: list[str] = []
        for item in value:
            cleaned, found = sanitize_goal_text(item)
            items.append(cleaned)
            flags += found
        return items, flags
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        flags = []
        for key, item in value.items():
            cleaned, found = sanitize_goal_text(item)
            out[key] = cleaned
            flags += found
        return out, flags
    return value, []


# --------------------------------------------------------------------------
# Butler steering
# --------------------------------------------------------------------------


def butler_steered(goal: ChatGoal) -> list[str]:
    """How a Butler steered this goal; empty when it never did."""

    reasons: list[str] = []
    if any(note.author == "butler" for note in goal.supervisor_notes):
        reasons.append("Butler added supervisor notes")
    if any(change.author == "butler" for change in goal.guidance_changes):
        reasons.append("Butler changed its guidance")
    steering = goal.metadata.get(STEERING_KEY)
    if isinstance(steering, list) and steering:
        actions = sorted(
            {str(item.get("action")) for item in steering if isinstance(item, dict)}
        )
        reasons.append("Butler took goal actions: " + ", ".join(actions))
    return reasons


def with_butler_steering(
    metadata: Mapping[str, Any], action: str, *, now: datetime | None = None
) -> dict[str, Any]:
    entry = {"action": action, "at": (now or utc_now()).isoformat()}
    history = [*(metadata.get(STEERING_KEY) or []), entry][-STEERING_HISTORY_LIMIT:]
    return {**metadata, STEERING_KEY: history}


async def record_butler_steering(
    store: Any, get_goal: Callable[[str], Any], session_id: str, action: str
) -> ChatGoal:
    """Mark that Butler steered the goal, on its newest revision."""

    for attempt in range(1, REVISE_ATTEMPTS + 1):
        goal = await get_goal(session_id)
        try:
            return await store.update(
                ChatGoal,
                goal.id,
                {"metadata": with_butler_steering(goal.metadata, action)},
                expected_revision=goal.revision,
            )
        except RevisionConflict:
            # diagnostic-expected: a usage charge moved the revision; the
            # steering mark is applied again on the newest goal.
            if attempt == REVISE_ATTEMPTS:
                raise
    raise RuntimeError("goal kept changing; the Butler steering mark was not saved")


# --------------------------------------------------------------------------
# MUST-DO items
# --------------------------------------------------------------------------


def must_dos(goal_or_metadata: ChatGoal | Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = (
        goal_or_metadata.metadata
        if isinstance(goal_or_metadata, ChatGoal)
        else goal_or_metadata
    )
    return [item for item in metadata.get(MUST_DOS_KEY) or [] if isinstance(item, dict)]


def open_must_dos(
    goal_or_metadata: ChatGoal | Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [item for item in must_dos(goal_or_metadata) if item.get("status") == "open"]


def with_review_must_dos(
    metadata: Mapping[str, Any],
    texts: list[str],
    *,
    review_id: str,
    status: str = "open",
    waiver: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Add a review's must-dos to the goal's list."""

    if status not in MUST_DO_STATUSES:
        raise MustDoError(f"unknown must-do status {status!r}")
    stamp = (now or utc_now()).isoformat()
    items = must_dos(metadata)
    for index, text in enumerate(texts, start=1):
        item: dict[str, Any] = {
            "id": f"md-{review_id[:8]}-{index}",
            "text": str(text)[:2_000],
            "status": status,
            "evidence": None,
            "source_review_id": review_id,
            "created_at": stamp,
        }
        if waiver is not None:
            item["waiver"] = waiver
            item["resolved_at"] = stamp
        items.append(item)
    return {**metadata, MUST_DOS_KEY: items[-MUST_DO_LIMIT:]}


def _waiver_actor_allowed(waived_by: str) -> bool:
    return waived_by == "operator" or waived_by.startswith("independent_review:")


def waive_must_do(
    metadata: Mapping[str, Any],
    item_id: str,
    *,
    waived_by: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Waive one open must-do. Only the operator or a later review may."""

    if not _waiver_actor_allowed(waived_by):
        raise PermissionError(
            "only the operator or a later independent review may waive a must-do"
        )
    if not reason or not reason.strip():
        raise MustDoError("a waiver needs a reason")
    return _resolve_item(
        metadata,
        item_id,
        status="waived",
        fields={"waiver": {"by": waived_by, "reason": reason.strip()[:2_000]}},
        now=now,
    )


def waive_open_must_dos(
    metadata: Mapping[str, Any],
    *,
    review_id: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """A later approving review waives every item still open."""

    for item in open_must_dos(metadata):
        metadata = waive_must_do(
            metadata,
            str(item["id"]),
            waived_by=f"independent_review:{review_id}",
            reason=reason,
            now=now,
        )
    return dict(metadata)


def _resolve_item(
    metadata: Mapping[str, Any],
    item_id: str,
    *,
    status: str,
    fields: dict[str, Any],
    now: datetime | None,
) -> dict[str, Any]:
    items = must_dos(metadata)
    for index, item in enumerate(items):
        if item.get("id") != item_id:
            continue
        if item.get("status") != "open":
            raise MustDoError(f"must-do {item_id} is already {item.get('status')}")
        items[index] = {
            **item,
            **fields,
            "status": status,
            "resolved_at": (now or utc_now()).isoformat(),
        }
        return {**metadata, MUST_DOS_KEY: items}
    raise MustDoError(f"no must-do {item_id} on this goal")


_COMMIT = re.compile(r"[0-9a-fA-F]{7,64}")
_REPO_PATH = re.compile(r"[A-Za-z0-9._/@+-][A-Za-z0-9._/@+ -]{0,999}")


def evidence_repos(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    env = os.environ if environ is None else environ
    return tuple(
        item.strip()
        for item in (env.get(EVIDENCE_REPOS_ENV) or "").split(",")
        if item.strip()
    )


def _under(path: str, roots: tuple[str, ...]) -> bool:
    resolved = os.path.realpath(path)
    for root in roots:
        real = os.path.realpath(root)
        if resolved == real or resolved.startswith(real.rstrip("/") + "/"):
            return True
    return False


def verify_artifact(
    artifact: Mapping[str, Any],
    *,
    evidence_roots: tuple[str, ...],
    repos: tuple[str, ...],
    git: str = "git",
) -> dict[str, Any]:
    """Verify an artifact Butler cites for a must-do; returns the evidence.

    ``{"kind": "commit", "repo": <configured repo>, "commit": <sha>,
    "path": <optional committed path>}`` is confirmed with ``git cat-file -e``.
    ``{"kind": "file", "path": <path>}`` must be an existing file under
    ``NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS``.
    """

    kind = artifact.get("kind")
    if kind == "file":
        path = artifact.get("path")
        if not isinstance(path, str) or not path:
            raise MustDoError("a file artifact needs a path")
        if not evidence_roots:
            raise MustDoError("set NEBULA_BUTLER_REVIEW_EVIDENCE_ROOTS")
        if not _under(path, evidence_roots):
            raise MustDoError(f"{path} is outside the evidence roots")
        if not Path(os.path.realpath(path)).is_file():
            raise MustDoError(f"{path} does not exist")
        return {
            "kind": "file",
            "path": os.path.realpath(path),
            "verified_by": "file_exists",
        }
    if kind == "commit":
        repo = artifact.get("repo")
        commit = artifact.get("commit")
        path = artifact.get("path")
        if not isinstance(repo, str) or not any(
            os.path.realpath(repo) == os.path.realpath(item) for item in repos
        ):
            raise MustDoError(
                f"the repository is not configured in {EVIDENCE_REPOS_ENV}"
            )
        if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
            raise MustDoError("a commit artifact needs a hexadecimal commit id")
        if path is not None and (
            not isinstance(path, str)
            or not _REPO_PATH.fullmatch(path)
            or path.startswith("/")
            or ".." in path.split("/")
        ):
            raise MustDoError("a committed path must be relative to the repository")
        target = f"{commit}^{{commit}}" if path is None else f"{commit}:{path}"
        try:
            result = subprocess.run(
                [git, "-C", os.path.realpath(repo), "cat-file", "-e", target],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MustDoError(f"git could not check the artifact: {exc}") from exc
        if result.returncode != 0:
            raise MustDoError(f"git cat-file -e {target} failed in the repository")
        return {
            "kind": "commit",
            "repo": os.path.realpath(repo),
            "commit": commit,
            "path": path,
            "verified_by": "git_cat_file",
        }
    raise MustDoError("artifact kind must be 'commit' or 'file'")


def must_do_resolve(
    goal: ChatGoal,
    item_id: str,
    *,
    artifact: Mapping[str, Any],
    evidence_roots: tuple[str, ...],
    repos: tuple[str, ...],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Satisfy one open must-do with a verified artifact; returns new metadata.

    This is Butler's only must-do route: it takes an artifact, never a
    waiver, and Core verifies the artifact before the item changes.
    """

    if not any(item.get("id") == item_id for item in open_must_dos(goal)):
        raise MustDoError(f"no open must-do {item_id} on this goal")
    evidence = verify_artifact(artifact, evidence_roots=evidence_roots, repos=repos)
    return _resolve_item(
        goal.metadata,
        item_id,
        status="satisfied",
        fields={"evidence": evidence},
        now=now,
    )


# --------------------------------------------------------------------------
# Closure request rate limit
# --------------------------------------------------------------------------


def closure_request_limit(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = (env.get(CLOSURE_REQUESTS_ENV) or "").strip()
    try:
        value = int(raw)
    except ValueError:  # diagnostic-expected: unset or malformed env uses the default
        return DEFAULT_CLOSURE_REQUESTS_PER_DAY
    return value if value >= 1 else DEFAULT_CLOSURE_REQUESTS_PER_DAY


def closure_requests_in_window(goal: ChatGoal, now: datetime) -> int:
    """Counted closure requests in the window; rate-limited refusals do not count."""

    since = now - CLOSURE_WINDOW
    count = 0
    for entry in goal.metadata.get(CLOSURE_REQUESTS_KEY) or []:
        if not isinstance(entry, dict) or entry.get("outcome") == "rate_limited":
            continue
        try:
            at = datetime.fromisoformat(str(entry.get("at")))
        except ValueError:  # diagnostic-expected: an unreadable stamp counts as recent
            count += 1
            continue
        if at >= since:
            count += 1
    return count


def with_closure_request(
    metadata: Mapping[str, Any], entry: dict[str, Any]
) -> dict[str, Any]:
    history = [*(metadata.get(CLOSURE_REQUESTS_KEY) or []), entry]
    return {**metadata, CLOSURE_REQUESTS_KEY: history[-CLOSURE_REQUEST_HISTORY_LIMIT:]}


async def record_closure_request(
    store: Any, get_goal: Callable[[str], Any], session_id: str, entry: dict[str, Any]
) -> ChatGoal:
    """Append one closure request outcome to the goal, on its newest revision."""

    for attempt in range(1, REVISE_ATTEMPTS + 1):
        goal = await get_goal(session_id)
        try:
            return await store.update(
                ChatGoal,
                goal.id,
                {"metadata": with_closure_request(goal.metadata, entry)},
                expected_revision=goal.revision,
            )
        except RevisionConflict:
            # diagnostic-expected: a usage charge moved the revision; the
            # request is recorded again on the newest goal.
            if attempt == REVISE_ATTEMPTS:
                raise
    raise RuntimeError("goal kept changing; the closure request was not recorded")


def closure_request_refusal(
    goal: ChatGoal,
    texts: list[str],
    *,
    now: datetime,
    limit: int | None = None,
) -> tuple[str, str] | None:
    """Why the goal's own closure request is refused, or None.

    Returns ``(outcome, reason)``; outcome is ``rate_limited``,
    ``forged_signoff`` or ``open_must_dos``.
    """

    allowed = closure_request_limit() if limit is None else limit
    if closure_requests_in_window(goal, now) >= allowed:
        return (
            "rate_limited",
            f"this goal already requested closure {allowed} times in the last 24 "
            f"hours ({CLOSURE_REQUESTS_ENV}={allowed}); keep working on the "
            "open evidence and request closure later",
        )
    flagged = [line for text in texts for line in detect_forged_signoff(text)]
    if flagged:
        return (
            "forged_signoff",
            "the completion proposal claims a supervisor or reviewer sign-off or "
            "verdict, which only Core's independent reviewer can record; remove "
            "these lines: " + " | ".join(flagged[:5]),
        )
    pending = open_must_dos(goal)
    if pending:
        return (
            "open_must_dos",
            "the independent review left open must-dos: "
            + "; ".join(
                f"{item['id']}: {item.get('text', '')[:200]}" for item in pending[:10]
            ),
        )
    return None


# --------------------------------------------------------------------------
# Butler completion gate
# --------------------------------------------------------------------------


def butler_completion_gate(
    goal: ChatGoal,
    goal_model: str | None,
    *,
    completion_summary: str | None = None,
) -> tuple[bool, str]:
    """Whether Butler's ``complete`` goal action may proceed."""

    from .butler_independent_review import independent_review_approves

    steered = butler_steered(goal)
    if goal.completion_requires_review or steered:
        approved, why = independent_review_approves(goal, goal_model)
        if not approved:
            if steered and not goal.completion_requires_review:
                why += " (Butler steered this goal: " + "; ".join(steered) + ")"
            return False, why
    pending = open_must_dos(goal)
    if pending:
        return False, (
            "open review must-dos remain ("
            + ", ".join(str(item["id"]) for item in pending[:10])
            + "); resolve them with nebula.supervisor.must_do_resolve or leave a "
            "waiver to the operator"
        )
    flagged = detect_forged_signoff(completion_summary)
    if flagged:
        return False, "the completion summary claims a sign-off: " + flagged[0]
    return True, "completion allowed"


# --------------------------------------------------------------------------
# Butler operation: resolve a must-do with an artifact
# --------------------------------------------------------------------------


class ButlerMustDoResolver:
    """Butler tool that satisfies one must-do with a Core-verified artifact."""

    def __init__(
        self,
        store: Any,
        work: Any,
        project_id: str,
        *,
        policy_loader: Callable[[], Any] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.work = work
        self.project_id = project_id
        self._policy_loader = policy_loader
        self._environ = environ

    def _policy(self) -> Any:
        if self._policy_loader is not None:
            return self._policy_loader()
        from .butler_supervisor_policy import load_policy

        return load_policy()

    async def resolve(self, arguments: dict[str, Any]) -> dict[str, Any]:
        policy = self._policy()
        policy.require_enabled()
        session_id = arguments.get("session_id")
        item_id = arguments.get("item_id")
        artifact = arguments.get("artifact")
        if not isinstance(session_id, str) or not isinstance(item_id, str):
            raise ValueError("session_id and item_id are required")
        if not isinstance(artifact, dict):
            raise ValueError("an artifact is required; Butler cannot waive a must-do")
        if "waive" in arguments or "waiver" in arguments:
            raise PermissionError("Butler cannot waive a must-do")
        repos = evidence_repos(self._environ) or tuple(policy.forensics_repos)
        for attempt in range(1, REVISE_ATTEMPTS + 1):
            goal = await self.work.goals.get(session_id)
            if goal.engagement_id != self.project_id:
                raise ValueError("the goal belongs to a different project")
            metadata = must_do_resolve(
                goal,
                item_id,
                artifact=artifact,
                evidence_roots=tuple(policy.evidence_roots),
                repos=repos,
            )
            try:
                updated = await self.store.update(
                    ChatGoal,
                    goal.id,
                    {"metadata": metadata},
                    expected_revision=goal.revision,
                )
            except RevisionConflict:
                # diagnostic-expected: a usage charge moved the revision; the
                # artifact is verified again on the newest goal.
                if attempt == REVISE_ATTEMPTS:
                    raise
                continue
            item = next(i for i in must_dos(updated) if i.get("id") == item_id)
            return {
                "goal_id": updated.id,
                "item": item,
                "open_must_dos": [i["id"] for i in open_must_dos(updated)],
            }
        raise RuntimeError("goal kept changing; the must-do was not resolved")

    def actions(self) -> list[AnalysisTool]:
        return [
            AnalysisTool(
                ToolSpec(
                    name="nebula.supervisor.must_do_resolve",
                    description=(
                        "Mark one open MUST-DO from an independent review satisfied "
                        "by citing an artifact Core verifies: a commit (and "
                        "optional committed path) in a repository configured in "
                        "NEBULA_BUTLER_REVIEW_EVIDENCE_REPOS, confirmed with git "
                        "cat-file -e, or an existing file under the review "
                        "evidence roots. Butler cannot waive a must-do; only the "
                        "operator or a later independent review can."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                            },
                            "item_id": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": 200,
                            },
                            "artifact": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": ["commit", "file"],
                                    },
                                    "repo": {"type": "string", "maxLength": 1_000},
                                    "commit": {"type": "string", "maxLength": 64},
                                    "path": {"type": "string", "maxLength": 1_000},
                                },
                                "required": ["kind"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["session_id", "item_id", "artifact"],
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object", "additionalProperties": True},
                    risk_class=RiskClass.WORKSPACE_WRITE,
                    idempotency=IdempotencyBehavior.NON_IDEMPOTENT,
                ),
                self.resolve,
            )
        ]
