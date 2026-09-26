#!/usr/bin/env python3
"""Measure what a model actually retains through Nebula's context management.

The contract tests prove that compaction, in-turn checkpoint folding and
cross-turn replay keep their structural promises. They cannot show whether the
model still *knows* a fact once it has been summarised, folded or left behind
at a turn boundary. This harness plants machine-scorable facts (codes, paths,
ports, names) in a real conversation, forces the context machinery to run with
a deliberately small window, asks for the facts back and scores the answers by
exact match. It never uses a model to judge a model.

Scenarios:

* ``s1`` conversation retention: a long chat of status notes with twelve facts
  (one later corrected) buried in filler; several compactions run before one
  tool-free probe turn asks for every fact, then a retry asks again with tools
  on (what a build can look up on demand, scored apart).
* ``s2`` long tool turn: one turn follows a chain of project files, one
  ``workspace.read`` per file (each file names the next), then lists every
  file's key. Early results are cleared or folded long before the answer.
* ``s3`` cross-turn tool recall: turn 1 reads four files and answers only
  "READY"; later turns ask for values from those outputs, first with tools off
  (pure retention), then with tools on (retention or re-fetch).

The script only talks to a Core over its HTTP API, so the same file evaluates
any build: point it at a running scratch Core with ``--base-url``/``--token``,
or let it serve one from a checkout with ``--serve-from``/``--data-dir``. Never
point it at a live Core: it creates projects, provider profiles and chats, and
switches its own projects to host execution mode with approvals off so the
file-reading turns cannot stall. It refuses a Core that already holds projects
it did not create unless ``--allow-existing-data`` is passed.

Example (scratch Core from a worktree, two repeats)::

    /path/to/.venv/bin/python scripts/context_retention_eval.py \\
        --serve-from /path/to/worktree --data-dir /tmp/eval-core --port 18715 \\
        --repeat 2 --out /tmp/eval-report

The provider profile uses ``secret_ref: env:OPEN_ROUTER_API_KEY``. When that
variable is not in the environment, the served Core is started through
``bash -ic`` so the operator's shell profile supplies it; the key itself is
never read or printed by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import secrets
import shlex
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPORT_SCHEMA = "nebula.context-retention-eval/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_SEED = 20260926
SCENARIOS = ("s1", "s2", "s3")
# Provider calls are bounded by the request's input capacity; these upper
# bounds per call size the pre-run cost estimate, not any request.
ESTIMATE_OUTPUT_TOKENS_PER_CALL = 800
TURN_TIMEOUT_SECONDS = 900
MAX_CONSECUTIVE_FAILED_TURNS = 3
# Frames after which a turn waits for the operator; the eval cannot answer them.
_BLOCKING_FRAMES = {"approval_required", "callback_required"}
_SOURCE_READ_TOOLS = {"workspace.read"}
_RECEIPT_READ_TOOLS = {"tool_output.read", "tool_output.search"}

# ---------------------------------------------------------------------------
# Deterministic scenario generation
# ---------------------------------------------------------------------------

_SUBJECTS = (
    "The review group",
    "Our platform team",
    "The operations desk",
    "The design circle",
    "A small working group",
    "The support rotation",
    "The planning committee",
    "The infrastructure guild",
    "The documentation crew",
    "Two of the newer engineers",
)
_VERBS = (
    "walked through",
    "revisited",
    "talked over",
    "skimmed",
    "compared notes on",
    "reorganized",
    "tidied up",
    "summarized",
    "read aloud",
    "spent a while on",
)
_OBJECTS = (
    "the onboarding checklist",
    "the weekly dashboard",
    "the vendor comparison",
    "the style guide",
    "the meeting cadence",
    "the shared calendar",
    "the glossary page",
    "the retrospective notes",
    "the backlog labels",
    "the office seating chart",
    "the team charter",
    "the holiday rota",
)
_ENDINGS = (
    "and nothing needs follow up.",
    "without reaching any decision.",
    "and agreed it reads fine as it is.",
    "and left a few cosmetic comments.",
    "and will look at it again someday.",
    "because it came up in passing.",
    "and found it mostly unchanged.",
    "while waiting for another meeting to start.",
    "and nobody had strong opinions.",
    "and moved on to lunch.",
)
_CODE_PREFIXES = tuple(
    "ALPHA BRAVO DELTA ECHO GAMMA KAPPA LAMBDA OMEGA SIGMA THETA ZETA VECTOR HALCYON NIMBUS QUASAR RAVEN".split()
)
_WORDS = tuple(
    "atlas harbor quartz ember cobalt juniper falcon meadow orbit saffron tundra willow basalt cinder lagoon maple nebula pylon ridge spruce".split()
)
_FIRST_NAMES = tuple(
    "Marisol Tobias Ingrid Kwame Priya Anselm Yukiko Dario Leona Rafael".split()
)
_LAST_NAMES = tuple(
    "Okafor Lindqvist Haddad Moreau Tanaka Brennan Castellano Novak Adeyemi Sorensen".split()
)
_DATABASES = ("PostgreSQL", "ClickHouse", "CockroachDB", "MariaDB", "ScyllaDB")
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_CODENAME_ADJECTIVES = ("Amber", "Silent", "Crimson", "Northern", "Velvet", "Copper")
_CODENAME_NOUNS = ("Falcon", "Lantern", "Meridian", "Orchid", "Summit", "Tributary")
_UNKNOWN_ANSWER = re.compile(
    r"\b(?:unknown|not sure|don't know|do not know|cannot recall|can't recall|"
    r"not (?:mentioned|provided|specified|stated|available)|no (?:record|information))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Probe:
    """One planted value and how an answer proves it was retained."""

    id: str
    kind: str
    question: str
    expected: str
    # Every pattern must match (case-insensitive) for a correct answer.
    patterns: tuple[str, ...]
    # A superseded value: matching it without the expected value is "stale".
    forbidden: tuple[str, ...] = ()
    # 1-based turn (s1) or chain position (s2) where the value was planted.
    planted_at: int = 0


@dataclass(frozen=True)
class TurnSpec:
    role: str
    content: str
    tools: bool = False
    # Probe ids scored against this turn's answer, in answer-line order.
    probes: tuple[str, ...] = ()
    # Regex prefix of numbered answer lines ("Q" for "Q3: ..."); None when a
    # single value answers the turn's one probe.
    line_prefix: str | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    profile: str
    turns: tuple[TurnSpec, ...]
    probes: tuple[Probe, ...]
    # Relative path -> content, written into the project folder before turn 1.
    files: dict[str, str] = field(default_factory=dict)
    # Upper-bound provider calls, for the pre-run cost estimate.
    expected_calls: int = 0

    def probe(self, probe_id: str) -> Probe:
        return next(item for item in self.probes if item.id == probe_id)

    def asked(self) -> list[Probe]:
        """Probes some turn asks; the rest are distractors that are never scored."""

        ids = {probe_id for turn in self.turns for probe_id in turn.probes}
        return [probe for probe in self.probes if probe.id in ids]


def _rng(seed: int, scenario: str) -> random.Random:
    # String seeds hash deterministically across processes and platforms.
    return random.Random(f"nebula-context-eval:{seed}:{scenario}")


def filler_sentence(rng: random.Random) -> str:
    return (
        f"{rng.choice(_SUBJECTS)} {rng.choice(_VERBS)} "
        f"{rng.choice(_OBJECTS)} {rng.choice(_ENDINGS)}"
    )


def filler_text(rng: random.Random, target_chars: int) -> str:
    """Plausible, digit-free chatter that no probe asks about."""

    sentences: list[str] = []
    size = 0
    while size < target_chars:
        sentence = filler_sentence(rng)
        sentences.append(sentence)
        size += len(sentence) + 1
    paragraphs = [
        " ".join(sentences[index : index + 6]) for index in range(0, len(sentences), 6)
    ]
    return "\n\n".join(paragraphs)


def _literal(value: str) -> str:
    """An identifier that must appear whole, not inside a longer token."""

    return rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])"


def _number(value: str) -> str:
    """A number, IP or version; thousands separators are optional."""

    digits = value.replace(",", "")
    if digits.isdigit() and len(digits) > 3:
        body = rf"{digits[:-3]}[,\s]?{digits[-3:]}"
    else:
        body = re.escape(value)
    return rf"(?<![\d.]){body}(?!\d)"


def _word(value: str) -> str:
    return rf"\b{re.escape(value)}\b"


class _Unique:
    """Draw values that never repeat within one scenario."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.seen: set[str] = set()

    def draw(self, make: Any) -> str:
        for _ in range(1_000):
            value = str(make(self.rng))
            if value not in self.seen:
                self.seen.add(value)
                return value
        raise RuntimeError("could not draw a unique scenario value")

    def code(self) -> str:
        return self.draw(
            lambda rng: f"{rng.choice(_CODE_PREFIXES)}-{rng.randint(1000, 9999)}"
        )

    def slug(self) -> str:
        return self.draw(
            lambda rng: (
                f"{rng.choice(_WORDS)}-{rng.choice(_WORDS)}-{rng.randint(10, 99)}"
            )
        )

    def port(self) -> str:
        return self.draw(lambda rng: str(rng.randint(30000, 39999)))

    def person(self) -> str:
        return self.draw(
            lambda rng: f"{rng.choice(_FIRST_NAMES)} {rng.choice(_LAST_NAMES)}"
        )


def _s1_facts(unique: _Unique) -> list[dict[str, Any]]:
    rng = unique.rng
    ticket = unique.code()
    manifest = unique.draw(
        lambda r: (
            f"/srv/{r.choice(_WORDS)}/{r.choice(_WORDS)}/manifest-{r.randint(2, 9)}.yaml"
        )
    )
    old_port, new_port = unique.port(), unique.port()
    bastion = unique.draw(
        lambda r: f"10.{r.randint(16, 250)}.{r.randint(1, 250)}.{r.randint(2, 250)}"
    )
    owner = unique.person()
    database, rejected = rng.sample(_DATABASES, 2)
    weekday = rng.choice(_WEEKDAYS)
    amount = f"{rng.randrange(24, 90) * 500:,}"
    version = unique.draw(
        lambda r: f"{r.randint(2, 9)}.{r.randint(10, 29)}.{r.randint(1, 19)}"
    )
    codename = f"{rng.choice(_CODENAME_ADJECTIVES)} {rng.choice(_CODENAME_NOUNS)}"
    digest = unique.draw(
        lambda r: "".join(r.choice("0123456789abcdef") for _ in range(12))
    )
    runbook = unique.draw(
        lambda r: (
            f"{r.choice(_WORDS)}.example.internal/runbooks/{r.choice(_WORDS)}-{r.choice(_WORDS)}"
        )
    )
    first, last = owner.split(" ")
    adjective, noun = codename.split(" ")
    return [
        {
            "id": "ticket",
            "kind": "identifier",
            "statement": f"The tracking ticket for the storage migration is {ticket}.",
            "question": "What is the tracking ticket for the storage migration?",
            "expected": ticket,
            "patterns": (_literal(ticket),),
        },
        {
            "id": "manifest_path",
            "kind": "path",
            "statement": f"The deployment manifest lives at {manifest}.",
            "question": "What is the full path of the deployment manifest?",
            "expected": manifest,
            "patterns": (_literal(manifest),),
        },
        {
            "id": "exporter_port",
            "kind": "corrected_port",
            "statement": f"The metrics exporter listens on port {old_port}.",
            "correction": (
                "Correction to an earlier note: the metrics exporter no longer "
                f"listens on port {old_port}; it moved to port {new_port}."
            ),
            "question": "Which port does the metrics exporter listen on now?",
            "expected": new_port,
            "patterns": (_number(new_port),),
            "forbidden": (_number(old_port),),
        },
        {
            "id": "bastion_ip",
            "kind": "address",
            "statement": f"The bastion host address is {bastion}.",
            "question": "What is the bastion host address?",
            "expected": bastion,
            "patterns": (_number(bastion),),
        },
        {
            "id": "ingest_owner",
            "kind": "person",
            "statement": f"The on-call owner for the ingest pipeline is {owner}.",
            "question": "Who is the on-call owner for the ingest pipeline?",
            "expected": owner,
            "patterns": (_word(first), _word(last)),
        },
        {
            "id": "audit_database",
            "kind": "decision",
            "statement": (
                f"Decision: we will use {database} for the audit log store "
                f"instead of {rejected}."
            ),
            "question": "Which database did we decide to use for the audit log store?",
            "expected": database,
            "patterns": (_word(database),),
        },
        {
            "id": "reindex_weekday",
            "kind": "constraint",
            "statement": f"Constraint: the reindex job must never run on a {weekday}.",
            "question": "On which weekday must the reindex job never run?",
            "expected": weekday,
            "patterns": (_word(weekday),),
        },
        {
            "id": "cloud_budget",
            "kind": "number",
            "statement": f"The cloud spending cap for this quarter is {amount} US dollars.",
            "question": "What is the cloud spending cap for this quarter, in US dollars?",
            "expected": amount,
            "patterns": (_number(amount),),
        },
        {
            "id": "parser_version",
            "kind": "version",
            "statement": f"Pin the log parser library to version {version}.",
            "question": "Which version must the log parser library be pinned to?",
            "expected": version,
            "patterns": (_number(version),),
        },
        {
            "id": "release_codename",
            "kind": "name",
            "statement": f"The internal codename for the next release is {codename}.",
            "question": "What is the internal codename for the next release?",
            "expected": codename,
            "patterns": (_word(adjective), _word(noun)),
        },
        {
            "id": "build_digest",
            "kind": "hex",
            "statement": f"The approved build digest begins with {digest}.",
            "question": "What does the approved build digest begin with?",
            "expected": digest,
            "patterns": (_literal(digest),),
        },
        {
            "id": "runbook_url",
            "kind": "url",
            "statement": f"The incident runbook is at https://{runbook}.",
            "question": "What is the URL of the incident runbook?",
            "expected": f"https://{runbook}",
            "patterns": (_literal(runbook),),
        },
    ]


def build_s1(seed: int, *, nonce: str = "", turn_chars: int = 4_200) -> Scenario:
    """Twelve facts over thirteen notes, two filler notes, then one probe."""

    rng = _rng(seed, "s1")
    facts = _s1_facts(_Unique(rng))
    corrected = next(item for item in facts if "correction" in item)
    # Facts land on turns 1-7 and 9-13; the correction lands on turn 8, so
    # both the superseded and the current port are old by probe time.
    statements: list[tuple[str, str | None]] = [
        (item["statement"], item["id"]) for item in facts[:7]
    ]
    statements.append((corrected["correction"], None))
    statements.extend((item["statement"], item["id"]) for item in facts[7:])
    statements.extend([("", None), ("", None)])
    planted: dict[str, int] = {}
    turns: list[TurnSpec] = []
    intro = (
        "I am going to send you a series of project status notes. A few of them "
        "contain details I will ask about later. Reply to each note with only "
        "the word Noted. until I ask you questions."
    )
    for index, (statement, fact_id) in enumerate(statements, start=1):
        before = filler_text(rng, int(turn_chars * 0.45))
        after = filler_text(rng, int(turn_chars * 0.55))
        parts = []
        if index == 1:
            if nonce:
                parts.append(f"Evaluation run {nonce}.")
            parts.append(intro)
        parts.append(f"Status note:\n\n{before}")
        if statement:
            parts.append(statement)
        parts.append(after)
        parts.append("Reply with only the word Noted.")
        turns.append(
            TurnSpec(
                role="plant" if statement else "filler", content="\n\n".join(parts)
            )
        )
        if fact_id is not None:
            planted[fact_id] = index
    probes = tuple(
        Probe(
            id=f"s1.{item['id']}",
            kind=item["kind"],
            question=item["question"],
            expected=item["expected"],
            patterns=tuple(item["patterns"]),
            forbidden=tuple(item.get("forbidden", ())),
            planted_at=planted[item["id"]],
        )
        for item in facts
    )
    questions = "\n".join(
        f"Q{index}: {probe.question}" for index, probe in enumerate(probes, start=1)
    )
    turns.append(
        TurnSpec(
            role="probe",
            content=(
                "Now answer these questions from our conversation. Put each answer "
                "on its own line as `Q<number>: <answer>`, keep answers short, and "
                "write `Q<number>: unknown` when you do not know.\n\n" + questions
            ),
            probes=tuple(probe.id for probe in probes),
            line_prefix="Q",
        )
    )
    # The same questions with tools on: what a build can recover on demand
    # (e.g. by searching the archived conversation). Scored apart, so the
    # tool-free recall above stays comparable across builds.
    turns.append(
        TurnSpec(
            role="probe_retry",
            content=(
                "Answer the same questions again, in the same `Q<number>: <answer>` "
                "format. For any answer you are not sure of, first use any tool "
                "you have that can look up earlier parts of this conversation."
            ),
            tools=True,
            probes=tuple(probe.id for probe in probes),
            line_prefix="Q",
        )
    )
    return Scenario(
        name="s1",
        title="Conversation retention across compactions",
        profile="s1",
        turns=tuple(turns),
        probes=probes,
        # One call per turn plus a compaction (and a possible repair) about
        # every third turn, and a few lookups in the retry.
        expected_calls=len(turns) + 2 * math.ceil(len(turns) / 3) + 6,
    )


def _file_body(rng: random.Random, header: str, value_line: str, footer: str) -> str:
    before = [filler_sentence(rng) for _ in range(rng.randint(5, 7))]
    after = [filler_sentence(rng) for _ in range(rng.randint(5, 7))]
    return "\n".join([header, *before, value_line, *after, footer]) + "\n"


def build_s2(seed: int, *, nonce: str = "", files: int = 32) -> Scenario:
    """One tool turn that must follow ``files`` chained reads, then recite keys."""

    if files < 3:
        raise ValueError("s2 needs at least three chained files")
    rng = _rng(seed, "s2")
    unique = _Unique(rng)
    paths = [f"chain/{unique.slug()}.txt" for _ in range(files)]
    keys = [unique.code() for _ in range(files)]
    contents: dict[str, str] = {}
    for index, (path, key) in enumerate(zip(paths, keys), start=1):
        following = f"NEXT: {paths[index]}" if index < files else "NEXT: END"
        contents[path] = _file_body(
            rng, f"This is file {index} of the chain.", f"KEY: {key}", following
        )
    probes = tuple(
        Probe(
            id=f"s2.file_{index:02d}",
            kind="chain_key",
            question=f"KEY of file {index}",
            expected=key,
            patterns=(_literal(key),),
            planted_at=index,
        )
        for index, key in enumerate(keys, start=1)
    )
    prompt = (
        (f"Evaluation run {nonce}.\n\n" if nonce else "")
        + f"Follow a chain of project files, starting with {paths[0]}. Read the "
        "files with workspace.read, one file per call: each file names the next "
        "file on its NEXT line, so you only learn the next path after reading the "
        "current one. Keep going until a file says NEXT: END. Every file states its "
        "number at the top and has a KEY line. When you reach the end, reply with "
        "one line per file in chain order, formatted as `<file number>: <KEY value>`."
    )
    return Scenario(
        name="s2",
        title="Long tool turn (in-turn clearing and checkpoint folding)",
        profile="tools",
        turns=(
            TurnSpec(
                role="probe",
                content=prompt,
                tools=True,
                probes=tuple(probe.id for probe in probes),
                line_prefix=r"(?:file\s*#?)?",
            ),
        ),
        probes=probes,
        files=contents,
        # A model that loses early results re-reads the chain, often in full.
        expected_calls=3 * files + 4,
    )


def build_s3(seed: int, *, nonce: str = "") -> Scenario:
    """Tool outputs from turn 1, asked about in later turns."""

    rng = _rng(seed, "s3")
    unique = _Unique(rng)
    labels = [
        ("service port", "port", unique.port(), _number),
        ("escrow token", "identifier", unique.code(), _literal),
        (
            "archive path",
            "path",
            unique.draw(
                lambda r: (
                    f"/var/lib/{r.choice(_WORDS)}/{r.choice(_WORDS)}-{r.randint(10, 99)}.tar"
                )
            ),
            _literal,
        ),
        ("maintainer", "person", unique.person(), None),
    ]
    paths = [f"records/{unique.slug()}.txt" for _ in labels]
    contents: dict[str, str] = {}
    probes: list[Probe] = []
    for index, ((label, kind, value, pattern), path) in enumerate(
        zip(labels, paths), start=1
    ):
        contents[path] = _file_body(
            rng, f"Record file {index}.", f"{label}: {value}", "End of record."
        )
        patterns = (
            tuple(_word(part) for part in value.split(" "))
            if pattern is None
            else (pattern(value),)
        )
        probes.append(
            Probe(
                id=f"s3.{label.replace(' ', '_')}",
                kind=kind,
                question=f"What is the {label} in {path}?",
                expected=value,
                patterns=patterns,
                planted_at=1,
            )
        )
    load = (
        (f"Evaluation run {nonce}.\n\n" if nonce else "")
        + "Read these four files with workspace.read: "
        + ", ".join(paths)
        + ". Do not repeat, quote or summarize anything from them; reply with only "
        "the word READY once you have read all four."
    )
    no_tools = (
        "Without using any tools, answer from what you saw earlier in this "
        "conversation. Put each answer on its own line as `Q<number>: <answer>`, "
        "or `Q<number>: unknown` if you cannot tell.\n\n"
        f"Q1: {probes[1].question}\nQ2: {probes[3].question}"
    )
    with_tools = (
        f"{probes[2].question} Answer from what you already saw in this "
        "conversation if you can; use a tool only if you must. Reply with only the "
        "value."
    )
    return Scenario(
        name="s3",
        title="Cross-turn tool-output recall",
        profile="tools",
        turns=(
            TurnSpec(role="load", content=load, tools=True),
            TurnSpec(
                role="probe_no_tools",
                content=no_tools,
                probes=(probes[1].id, probes[3].id),
                line_prefix="Q",
            ),
            TurnSpec(
                role="probe_tools",
                content=with_tools,
                tools=True,
                probes=(probes[2].id,),
            ),
        ),
        probes=tuple(probes),
        files=contents,
        expected_calls=12,
    )


def build_scenario(
    name: str,
    seed: int,
    *,
    nonce: str = "",
    s1_turn_chars: int = 4_200,
    s2_files: int = 32,
) -> Scenario:
    if name == "s1":
        return build_s1(seed, nonce=nonce, turn_chars=s1_turn_chars)
    if name == "s2":
        return build_s2(seed, nonce=nonce, files=s2_files)
    if name == "s3":
        return build_s3(seed, nonce=nonce)
    raise ValueError(f"unknown scenario {name!r}")


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def answer_lines(text: str, prefix: str = "Q") -> dict[int, str]:
    """Numbered answers ("Q3: ...", "**3.** ...", "| 3 | ... |") by number.

    A line without a number continues the previous answer. The first answer
    for a number wins, so a later recap cannot overwrite it.
    """

    numbered = re.compile(
        rf"^\s*(?:[-*]\s+)?(?:\*\*|__)?\s*{prefix}\s*(\d{{1,3}})\s*(?:\*\*|__)?"
        r"\s*[:.)\]–—-]\s*(?:\*\*|__)?\s*(.*)$",
        re.IGNORECASE,
    )
    table = re.compile(
        rf"^\s*\|\s*(?:{prefix})?\s*(\d{{1,3}})\s*\|\s*(.*?)\s*\|?\s*$", re.IGNORECASE
    )
    found: dict[int, str] = {}
    current: int | None = None
    for line in text.splitlines():
        match = numbered.match(line) or table.match(line)
        if match:
            number = int(match.group(1))
            current = number if number not in found else None
            if current is not None:
                found[current] = match.group(2).strip()
        elif current is not None and line.strip():
            found[current] = f"{found[current]} {line.strip()}".strip()
        else:
            current = None
    return found


def classify(probe: Probe, text: str) -> str:
    """``correct``, ``stale`` (superseded value only), ``unknown``, ``wrong`` or ``missing``."""

    if not text.strip():
        return "missing"
    if all(re.search(pattern, text, re.IGNORECASE) for pattern in probe.patterns):
        return "correct"
    if probe.forbidden and any(
        re.search(pattern, text, re.IGNORECASE) for pattern in probe.forbidden
    ):
        return "stale"
    if _UNKNOWN_ANSWER.search(text):
        return "unknown"
    return "wrong"


def unanswered(probes: Iterable[Probe], outcome: str) -> list[dict[str, Any]]:
    """Misses for probes that got no reply: ``not_asked`` or ``turn_failed``."""

    return [
        {
            "probe": probe.id,
            "kind": probe.kind,
            "planted_at": probe.planted_at,
            "expected": probe.expected,
            "outcome": outcome,
            "correct": False,
            "mentioned": False,
            "answer": "",
        }
        for probe in probes
    ]


def score_turn(scenario: Scenario, turn: TurnSpec, answer: str) -> list[dict[str, Any]]:
    """Score every probe a turn asks, strictly by answer line and loosely anywhere."""

    lines = answer_lines(answer, turn.line_prefix) if turn.line_prefix else {}
    results = []
    for number, probe_id in enumerate(turn.probes, start=1):
        probe = scenario.probe(probe_id)
        text = lines.get(number, "") if turn.line_prefix else answer
        outcome = classify(probe, text)
        results.append(
            {
                "probe": probe.id,
                "kind": probe.kind,
                "planted_at": probe.planted_at,
                "expected": probe.expected,
                "outcome": outcome,
                "correct": outcome == "correct",
                # Retained somewhere in the reply even if misplaced or unnumbered.
                "mentioned": all(
                    re.search(pattern, answer, re.IGNORECASE)
                    for pattern in probe.patterns
                ),
                "answer": text[:300],
            }
        )
    return results


def _source_keys(
    name: str, arguments: dict[str, Any], files: Iterable[str]
) -> list[str]:
    """Which planted files a tool call reads."""

    if name in _SOURCE_READ_TOOLS:
        path = str(arguments.get("path") or "").strip().lstrip("./")
        return [path] if path else []
    text = json.dumps(arguments, sort_keys=True)
    return [path for path in files if path in text]


def tool_call_stats(
    calls: list[dict[str, Any]],
    files: Iterable[str] = (),
    already_read: Iterable[str] = (),
    earlier_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Count re-reads of planted files and re-fetches through tool receipts.

    ``calls`` are ``{"name", "arguments"}`` in order. A source read is repeated
    when the same file was read earlier in ``calls`` or in ``already_read``
    (earlier turns); reading at another starting line counts as new. A receipt
    re-fetch is cross-turn when it names a tool call or artifact id an earlier
    turn produced (``earlier_ids``).
    """

    files = list(files)
    seen = set(already_read)
    previous = set(earlier_ids)
    by_name: dict[str, int] = {}
    repeated = 0
    source_reads = 0
    cross_turn = 0
    for call in calls:
        name = str(call.get("name") or "")
        arguments = call.get("arguments") or {}
        by_name[name] = by_name.get(name, 0) + 1
        if name in _RECEIPT_READ_TOOLS and isinstance(arguments, dict):
            named = {arguments.get("tool_call_id"), arguments.get("artifact_id")}
            cross_turn += bool(named & previous)
        start = arguments.get("starting_line") if isinstance(arguments, dict) else None
        for key in _source_keys(
            name, arguments if isinstance(arguments, dict) else {}, files
        ):
            source_reads += 1
            marker = f"{key}#{start or 1}"
            if marker in seen:
                repeated += 1
            seen.add(marker)
    return {
        "tool_calls": len(calls),
        "by_name": dict(sorted(by_name.items())),
        "source_reads": source_reads,
        "repeated_source_reads": repeated,
        "receipt_refetches": sum(by_name.get(name, 0) for name in _RECEIPT_READ_TOOLS),
        "cross_turn_refetches": cross_turn,
        "read_markers": sorted(seen),
    }


def produced_ids(completed: dict[str, Any]) -> set[str]:
    """Tool call and artifact ids a ``tool_completed`` frame hands the model."""

    ids = {completed.get("tool_call_id"), completed.get("result_artifact_id")}
    for artifact in completed.get("artifacts") or []:
        if isinstance(artifact, dict):
            ids |= {artifact.get("artifact_id"), artifact.get("id")}
        elif isinstance(artifact, str):
            ids.add(artifact)
    return {item for item in ids if isinstance(item, str) and item}


# ---------------------------------------------------------------------------
# Usage, cost and aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pricing:
    """USD per million tokens."""

    input: float
    output: float
    cached_input: float
    # Routes with explicit cache breakpoints (Anthropic) bill writes apart.
    cache_write: float | None = None
    source: str = "unknown"

    def cost(self, usage: dict[str, Any] | None) -> float:
        # Core's input_tokens is the whole prompt; reads and writes are parts of it.
        usage = usage or {}
        total_input = int(usage.get("input_tokens") or 0)
        cached = min(int(usage.get("cached_input_tokens") or 0), total_input)
        written = min(
            int(usage.get("cache_creation_input_tokens") or 0), total_input - cached
        )
        output = int(usage.get("output_tokens") or 0)
        write_rate = self.input if self.cache_write is None else self.cache_write
        return (
            (total_input - cached - written) * self.input
            + cached * self.cached_input
            + written * write_rate
            + output * self.output
        ) / 1_000_000


def pricing_from_descriptor(descriptor: dict[str, Any] | None) -> Pricing | None:
    """OpenRouter-style per-token decimal strings -> USD per million tokens."""

    prices = (descriptor or {}).get("pricing") or {}
    try:
        prompt = float(prices["prompt"]) * 1_000_000
        completion = float(prices["completion"]) * 1_000_000
    except (KeyError, TypeError, ValueError):
        return None
    try:
        cached = float(prices.get("input_cache_read", prices["prompt"])) * 1_000_000
    except (TypeError, ValueError):
        cached = prompt
    try:
        write = (
            float(prices["input_cache_write"]) * 1_000_000
            if "input_cache_write" in prices
            else None
        )
    except (TypeError, ValueError):
        write = None
    return Pricing(prompt, completion, cached, write, source="core model catalog")


USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
)


def add_usage(total: dict[str, int], usage: dict[str, Any] | None) -> dict[str, int]:
    for key in USAGE_KEYS:
        total[key] = total.get(key, 0) + int((usage or {}).get(key) or 0)
    return total


def estimate_cost(
    scenarios: list[Scenario], capacities: dict[str, int], pricing: Pricing, repeat: int
) -> float:
    """An upper bound: every call fills its input capacity, none hits a cache."""

    total = 0.0
    for scenario in scenarios:
        calls = scenario.expected_calls
        usage = {
            "input_tokens": calls * capacities[scenario.profile],
            "output_tokens": calls * ESTIMATE_OUTPUT_TOKENS_PER_CALL,
        }
        total += pricing.cost(usage)
    return total * repeat


def aggregate(values: Iterable[float | int | None]) -> dict[str, float] | None:
    numbers = [float(value) for value in values if value is not None]
    if not numbers:
        return None
    return {
        "mean": round(statistics.fmean(numbers), 4),
        "min": round(min(numbers), 4),
        "max": round(max(numbers), 4),
        "n": len(numbers),
    }


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def run_metrics(
    scenario: Scenario,
    turns: list[dict[str, Any]],
    pricing: Pricing,
    snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Scenario metrics from recorded turns (see ``Evaluation.run_scenario``).

    ``snapshots`` are every context snapshot the store holds for the session,
    when the store is readable. They are the better source: the API shows only
    the latest snapshot after each turn, and a refused turn reports no usage
    for the compaction attempts that failed it.
    """

    results = [
        item
        for turn in turns
        if turn.get("role") != "probe_retry"
        for item in turn.get("scores", [])
    ]
    answered = {item["probe"] for item in results}
    # A probe whose turn never ran (an earlier failure aborted the scenario) is
    # a miss, not an omission from the denominator.
    results += unanswered(
        (probe for probe in scenario.asked() if probe.id not in answered), "not_asked"
    )
    chat_usage: dict[str, int] = {}
    compaction_usage: dict[str, int] = {}
    for turn in turns:
        add_usage(chat_usage, turn.get("usage"))
    if snapshots is None:
        seen: dict[str, dict[str, Any]] = {}
        for turn in turns:
            add_usage(compaction_usage, turn.get("context_usage"))
            snapshot = (turn.get("context") or {}).get("snapshot")
            if snapshot and snapshot.get("id"):
                seen[snapshot["id"]] = snapshot
        observed = list(seen.values())
        # Versions count every snapshot the session created, seen or not.
        compactions = max((item.get("version") or 0 for item in observed), default=0)
    else:
        observed = snapshots
        for snapshot in snapshots:
            add_usage(compaction_usage, snapshot.get("usage"))
        compactions = len(snapshots)
    qualities = sorted(
        {str(item["quality"]) for item in observed if item.get("quality")}
    )
    # A turn compacted when it saw a newer snapshot than the turn before, or
    # when Core refused a send (every refusal so far is a failed compaction).
    # Its time before the turn started is the compaction's latency.
    latencies = []
    version = 0
    for turn in turns:
        seen_version = ((turn.get("context") or {}).get("snapshot") or {}).get(
            "version"
        ) or version
        if (seen_version > version or turn.get("refused_attempts")) and turn.get(
            "prepare_s"
        ) is not None:
            latencies.append(float(turn["prepare_s"]))
        version = max(version, seen_version)
    estimate_ratios = [
        _ratio(request["reported_input_tokens"], request["estimated_total"])
        for turn in turns
        if (request := (turn.get("context") or {}).get("last_provider_request"))
        and request.get("reported_input_tokens")
        and request.get("estimated_total")
    ]
    tool_turns = [turn["tool_stats"] for turn in turns if turn.get("tool_stats")]
    by_name: dict[str, int] = {}
    for stats in tool_turns:
        for name, count in (stats.get("by_name") or {}).items():
            by_name[name] = by_name.get(name, 0) + count
    retry = [
        item
        for turn in turns
        if turn.get("role") == "probe_retry"
        for item in turn.get("scores", [])
    ]
    checkpoints = [item for turn in turns for item in turn.get("checkpoints") or []]
    correct = sum(1 for item in results if item["correct"])
    early = [item for item in results if item["planted_at"] <= _early_cutoff(scenario)]
    metrics: dict[str, Any] = {
        "recall": _ratio(correct, len(results)),
        "recall_loose": _ratio(
            sum(1 for item in results if item["mentioned"]), len(results)
        ),
        "recall_early": _ratio(sum(1 for item in early if item["correct"]), len(early)),
        "probes": len(results),
        "stale_answers": sum(1 for item in results if item["outcome"] == "stale"),
        "turns": len(turns),
        "failed_turns": sum(1 for turn in turns if not turn.get("ok")),
        "skipped_turns": sum(1 for turn in turns if turn.get("skipped")),
        # Sends Core refused before a turn started (the operator would resend).
        "refused_attempts": sum(turn.get("refused_attempts") or 0 for turn in turns),
        "compactions": compactions,
        "failed_compactions": sum(
            1 for item in observed if item.get("status") == "failed"
        ),
        "snapshot_qualities": qualities,
        "snapshot_dropped_items": sum(
            item.get("dropped_items") or 0 for item in observed
        ),
        "segments": sum(item.get("segment_count") or 0 for item in observed),
        "reused_segments": sum(item.get("reused_segments") or 0 for item in observed),
        "compaction_latency_mean": (
            round(statistics.fmean(latencies), 1) if latencies else None
        ),
        "compaction_latency_max": round(max(latencies), 1) if latencies else None,
        "compacted_through": max(
            (
                (turn.get("context") or {}).get("compacted_through") or 0
                for turn in turns
            ),
            default=0,
        ),
        "tool_calls": sum(item["tool_calls"] for item in tool_turns),
        "repeated_source_reads": sum(
            item["repeated_source_reads"] for item in tool_turns
        ),
        "receipt_refetches": sum(item["receipt_refetches"] for item in tool_turns),
        "cross_turn_refetches": sum(
            item.get("cross_turn_refetches", 0) for item in tool_turns
        ),
        "tool_calls_by_name": dict(sorted(by_name.items())),
        # On-demand lookups newer builds offer (retrieval, working notes).
        "conversation_searches": by_name.get("conversation.search", 0),
        "notes_writes": by_name.get("notes.write", 0),
        # Size of the working notes Core holds after the last turn, if any.
        "working_notes_chars": next(
            (
                notes["chars"]
                for turn in reversed(turns)
                if (notes := (turn.get("context") or {}).get("working_notes"))
            ),
            None,
        ),
        "input_tokens": chat_usage.get("input_tokens", 0),
        "output_tokens": chat_usage.get("output_tokens", 0),
        "cached_input_tokens": chat_usage.get("cached_input_tokens", 0),
        "cache_hit_ratio": _ratio(
            chat_usage.get("cached_input_tokens", 0), chat_usage.get("input_tokens", 0)
        ),
        "cache_creation_input_tokens": chat_usage.get("cache_creation_input_tokens", 0),
        "cache_write_ratio": _ratio(
            chat_usage.get("cache_creation_input_tokens", 0),
            chat_usage.get("input_tokens", 0),
        ),
        "compaction_input_tokens": compaction_usage.get("input_tokens", 0),
        "compaction_output_tokens": compaction_usage.get("output_tokens", 0),
        # Provider-reported input over Core's estimate for the last request of
        # each turn: how far the estimator is off for this model.
        "estimate_ratio": (
            round(statistics.fmean(ratio for ratio in estimate_ratios if ratio), 4)
            if any(estimate_ratios)
            else None
        ),
        # The per-model factor Core learned from reported usage (newer builds).
        "estimate_calibration": next(
            (
                calibration
                for turn in reversed(turns)
                if (
                    calibration := (turn.get("context") or {}).get(
                        "estimate_calibration"
                    )
                )
            ),
            None,
        ),
        "cost_usd": round(pricing.cost(chat_usage) + pricing.cost(compaction_usage), 6),
        "wall_seconds": round(sum(turn.get("elapsed_s") or 0 for turn in turns), 1),
    }
    if any("checkpoints" in turn for turn in turns):
        metrics["checkpoints"] = len(checkpoints)
        metrics["checkpoint_omitted_steps"] = max(
            (item.get("omitted_steps") or 0 for item in checkpoints), default=0
        )
    if any(turn.get("role") == "probe_retry" for turn in turns):
        metrics["recall_retry"] = _ratio(
            sum(1 for item in retry if item["correct"]), len(retry)
        )
    if scenario.name == "s3":
        by_role = {turn["role"]: turn for turn in turns}
        load_answer = (by_role.get("load") or {}).get("answer") or ""
        metrics["load_turn_leaked_values"] = sum(
            1
            for probe in scenario.probes
            if all(re.search(p, load_answer, re.IGNORECASE) for p in probe.patterns)
        )
        for role in ("probe_no_tools", "probe_tools"):
            scores = (by_role.get(role) or {}).get("scores", [])
            metrics[f"recall_{role.removeprefix('probe_')}"] = _ratio(
                sum(1 for item in scores if item["correct"]), len(scores)
            )
    return {"metrics": metrics, "probe_results": results}


def _early_cutoff(scenario: Scenario) -> int:
    """Probes planted in the first third of the scenario count as early."""

    last = max((probe.planted_at for probe in scenario.probes), default=0)
    return max(1, math.ceil(last / 3))


SUMMARY_METRICS = (
    "recall",
    "recall_loose",
    "recall_early",
    "recall_no_tools",
    "recall_tools",
    "recall_retry",
    "stale_answers",
    "failed_turns",
    "refused_attempts",
    "compactions",
    "failed_compactions",
    "snapshot_dropped_items",
    "segments",
    "reused_segments",
    "compaction_latency_mean",
    "compaction_latency_max",
    "checkpoints",
    "checkpoint_omitted_steps",
    "tool_calls",
    "repeated_source_reads",
    "receipt_refetches",
    "cross_turn_refetches",
    "conversation_searches",
    "notes_writes",
    "working_notes_chars",
    "input_tokens",
    "cached_input_tokens",
    "cache_hit_ratio",
    "cache_creation_input_tokens",
    "cache_write_ratio",
    "compaction_input_tokens",
    "estimate_ratio",
    "estimate_calibration",
    "cost_usd",
    "wall_seconds",
)


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per scenario: mean/min/max of each metric and per-probe hit counts."""

    summary: dict[str, Any] = {}
    for name in sorted({run["scenario"] for run in runs}):
        selected = [run for run in runs if run["scenario"] == name]
        metrics = {
            key: aggregate(run["metrics"].get(key) for run in selected)
            for key in SUMMARY_METRICS
        }
        qualities = sorted(
            {
                quality
                for run in selected
                for quality in run["metrics"].get("snapshot_qualities", [])
            }
        )
        probes: dict[str, dict[str, Any]] = {}
        for run in selected:
            for item in run["probe_results"]:
                entry = probes.setdefault(
                    item["probe"],
                    {
                        "kind": item["kind"],
                        "planted_at": item["planted_at"],
                        "correct": 0,
                        "runs": 0,
                        "outcomes": {},
                    },
                )
                entry["runs"] += 1
                entry["correct"] += int(item["correct"])
                entry["outcomes"][item["outcome"]] = (
                    entry["outcomes"].get(item["outcome"], 0) + 1
                )
        summary[name] = {
            "runs": len(selected),
            "metrics": {key: value for key, value in metrics.items() if value},
            "snapshot_qualities": qualities,
            "probes": probes,
        }
    return summary


def _fmt(
    stat: dict[str, float] | None, *, percent: bool = False, digits: int = 0
) -> str:
    if not stat:
        return "–"

    def one(value: float) -> str:
        if percent:
            return f"{value * 100:.0f}%"
        return f"{value:,.{0 if value == int(value) else digits}f}"

    if stat["min"] == stat["max"]:
        return one(stat["mean"])
    return f"{one(stat['mean'])} (min {one(stat['min'])})"


def _error_detail(error: str | None) -> str:
    """Core's ``detail`` from an error body, or the start of the text."""

    match = re.search(r'"detail"\s*:\s*"([^"]*)"', error or "")
    return (match.group(1) if match else (error or ""))[:200]


def render_markdown(report: dict[str, Any]) -> str:
    core = report.get("core", {})
    lines = [
        f"# Context retention eval — {report.get('label') or core.get('commit') or 'core'}",
        "",
        f"- Core: {core.get('url', '?')} version {core.get('version', '?')}, "
        f"commit {core.get('commit', '?')}"
        + (f", checkout {core['checkout']}" if core.get("checkout") else ""),
        f"- Harness: `context_retention_eval.py` sha256 "
        f"{str(report.get('eval_script_sha256', '?'))[:12]}",
        f"- Model: `{report['model']}`; seed {report['seed']}; repeat {report['repeat']}; "
        f"temperature {report.get('temperature')}",
        "- Profiles (configured window / output → Core's compaction trigger): "
        + "; ".join(
            f"{name} {item['context_window']:,} / {item['max_output_tokens']:,} → "
            f"{(item.get('resolved') or {}).get('target_input_tokens') or 0:,}"
            for name, item in sorted(report.get("profiles", {}).items())
        ),
        f"- Cost: estimated ≤ ${report.get('estimated_cost_usd', 0):.4f}, actual "
        f"${report.get('total_cost_usd', 0):.4f} (pricing: {report.get('pricing', {}).get('source', '?')}; "
        "excludes profile verification and conversation naming calls)",
        "",
        "Values are mean (min) across repeats; recall is exact-match per probe.",
        "",
        "| Scenario | Recall | Early recall | Loose | Failed turns / refused sends "
        "| Compactions | Snapshot quality | Turn checkpoints / omitted steps "
        "| Repeated reads / receipt re-fetches (of earlier turns) "
        "| Conversation searches / notes writes "
        "| Input tokens | Cache read (write) "
        "| Est. ratio (calibration) | Cost | Wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, item in report.get("summary", {}).items():
        metrics = item["metrics"]
        recall = _fmt(metrics.get("recall"), percent=True)
        if metrics.get("recall_retry"):
            recall += (
                f" (re-asked with tools {_fmt(metrics['recall_retry'], percent=True)})"
            )
        if name == "s3":
            recall += (
                f" (no tools {_fmt(metrics.get('recall_no_tools'), percent=True)},"
                f" tools {_fmt(metrics.get('recall_tools'), percent=True)})"
            )
        failed = metrics.get("failed_compactions") or {}
        compactions = _fmt(metrics.get("compactions"), digits=1)
        if failed.get("max"):
            compactions += f", {failed['mean']:.1f} failed"
        if metrics.get("compaction_latency_max"):
            compactions += (
                f"; {_fmt(metrics.get('compaction_latency_mean'), digits=0)} s each "
                f"(max {metrics['compaction_latency_max']['max']:.0f} s)"
            )
        quality = ", ".join(item.get("snapshot_qualities") or []) or "–"
        dropped = (metrics.get("snapshot_dropped_items") or {}).get("max")
        segments = (metrics.get("segments") or {}).get("max")
        if dropped:
            quality += f"; {_fmt(metrics['snapshot_dropped_items'], digits=1)} dropped"
        if segments:
            quality += (
                f"; reused {_fmt(metrics.get('reused_segments'), digits=1)} of "
                f"{_fmt(metrics['segments'], digits=1)} segments"
            )
        lines.append(
            "| "
            + " | ".join(
                [
                    name,
                    recall,
                    _fmt(metrics.get("recall_early"), percent=True),
                    _fmt(metrics.get("recall_loose"), percent=True),
                    f"{_fmt(metrics.get('failed_turns'), digits=1)} / "
                    f"{_fmt(metrics.get('refused_attempts'), digits=1)}",
                    compactions,
                    quality,
                    f"{_fmt(metrics.get('checkpoints'), digits=1)} / "
                    f"{_fmt(metrics.get('checkpoint_omitted_steps'), digits=1)}",
                    f"{_fmt(metrics.get('repeated_source_reads'), digits=1)} / "
                    f"{_fmt(metrics.get('receipt_refetches'), digits=1)} "
                    f"({_fmt(metrics.get('cross_turn_refetches'), digits=1)})",
                    f"{_fmt(metrics.get('conversation_searches'), digits=1)} / "
                    f"{_fmt(metrics.get('notes_writes'), digits=1)}",
                    _fmt(metrics.get("input_tokens")),
                    _fmt(metrics.get("cache_hit_ratio"), percent=True)
                    + (
                        f" ({_fmt(metrics.get('cache_write_ratio'), percent=True)})"
                        if (metrics.get("cache_write_ratio") or {}).get("max")
                        else ""
                    ),
                    _fmt(metrics.get("estimate_ratio"), digits=2)
                    + (
                        f" ({_fmt(metrics['estimate_calibration'], digits=2)})"
                        if metrics.get("estimate_calibration")
                        else ""
                    ),
                    "$" + _fmt(metrics.get("cost_usd"), digits=4),
                    _fmt(metrics.get("wall_seconds")),
                ]
            )
            + " |"
        )
    failures = []
    for run in report.get("runs", []):
        prefix = f"- {run['scenario']} repeat {run['repeat']}"
        skipped = 0
        for turn in run.get("turns", []):
            if turn.get("skipped"):
                skipped += 1
            elif not turn.get("ok"):
                failures.append(
                    f"{prefix}: {turn['role']} turn {turn['index']} failed: "
                    f"{_error_detail(turn.get('error'))}"
                )
            elif turn.get("refused_attempts"):
                failures.append(
                    f"{prefix}: {turn['role']} turn {turn['index']} succeeded after "
                    f"{turn['refused_attempts']} refused send(s): "
                    f"{_error_detail((turn.get('refusals') or [''])[0])}"
                )
        if skipped:
            failures.append(f"{prefix}: {skipped} later turn(s) skipped")
    if failures:
        lines += ["", "## Failed and refused turns", "", *failures]
    for name, item in report.get("summary", {}).items():
        lines += [
            "",
            f"## {name} probes",
            "",
            "| Probe | Kind | Planted at | Correct | Outcomes |",
            "|---|---|---|---|---|",
        ]
        for probe_id, entry in item["probes"].items():
            outcomes = ", ".join(
                f"{key} {value}" for key, value in sorted(entry["outcomes"].items())
            )
            lines.append(
                f"| {probe_id} | {entry['kind']} | {entry['planted_at']} | "
                f"{entry['correct']}/{entry['runs']} | {outcomes} |"
            )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Core over HTTP
# ---------------------------------------------------------------------------


class EvalError(RuntimeError):
    pass


PROJECT_PREFIX = "Retention eval "


def foreign_projects(projects: list[dict[str, Any]]) -> list[str]:
    """Names of projects neither this eval nor Core's bootstrap created.

    A fresh Core seeds its own Scratch Project (``created_by: system:...``);
    anything else means an operator's Core, which the eval must not write to.
    """

    return [
        str(project.get("name") or "(unnamed)")
        for project in projects
        if not str(project.get("name") or "").startswith(PROJECT_PREFIX)
        and not str((project.get("metadata") or {}).get("created_by") or "").startswith(
            "system:"
        )
    ]


class CoreClient:
    def __init__(self, base_url: str, token: str) -> None:
        import httpx

        self._httpx = httpx
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(120.0, read=300.0),
        )

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, path, **kwargs)
        if response.status_code >= 400:
            raise EvalError(
                f"{method} {path}: HTTP {response.status_code} {response.text[:600]}"
            )
        return response.json() if response.content else None

    def stream_turn(self, body: dict[str, Any]) -> dict[str, Any]:
        """Send one streamed turn; return its frames' outcome, never raise for a turn failure."""

        started = time.monotonic()
        outcome: dict[str, Any] = {
            "ok": False,
            "tool_calls": [],
            "produced_ids": set(),
            "error": None,
        }
        deadline = started + TURN_TIMEOUT_SECONDS
        try:
            with self.http.stream(
                "POST", "/api/v1/chat/completions", json=body
            ) as response:
                # Core answers only after prepare_async, which runs any
                # conversation compaction the turn needs.
                outcome["prepare_s"] = round(time.monotonic() - started, 2)
                if response.status_code >= 400:
                    outcome["error"] = (
                        f"HTTP {response.status_code}: {response.read().decode()[:600]}"
                    )
                    return self._finish(outcome, started)
                event = None
                for line in response.iter_lines():
                    if line.startswith("event: "):
                        event = line.removeprefix("event: ").strip()
                        continue
                    if not line.startswith("data: "):
                        if time.monotonic() > deadline:
                            outcome["error"] = f"turn exceeded {TURN_TIMEOUT_SECONDS}s"
                            break
                        continue
                    payload = json.loads(line.removeprefix("data: "))
                    outcome["turn_id"] = outcome.get("turn_id") or payload.get(
                        "turn_id"
                    )
                    outcome["session_id"] = outcome.get("session_id") or payload.get(
                        "session_id"
                    )
                    if event == "tool_started":
                        outcome["tool_calls"].append(
                            {
                                "name": payload.get("capability"),
                                "arguments": payload.get("arguments") or {},
                                "step": payload.get("step"),
                            }
                        )
                    elif event == "tool_completed":
                        outcome["produced_ids"] |= produced_ids(payload)
                    elif event == "done":
                        outcome.update(
                            ok=True,
                            answer=(payload.get("message") or {}).get("content") or "",
                            usage=payload.get("usage") or {},
                            context_usage=payload.get("context_usage") or {},
                            finish_reason=payload.get("finish_reason"),
                        )
                    elif event in {"error", "cancelled"}:
                        outcome["error"] = str(
                            payload.get("operator_detail")
                            or payload.get("detail")
                            or payload.get("message")
                            or event
                        )
                    elif event in _BLOCKING_FRAMES:
                        outcome["error"] = f"turn stopped: it waited on {event}"
                        break
                    if time.monotonic() > deadline:
                        outcome["error"] = f"turn exceeded {TURN_TIMEOUT_SECONDS}s"
                        break
        except self._httpx.HTTPError as exc:
            outcome["error"] = f"stream failed: {type(exc).__name__}: {exc}"
        if not outcome["ok"] and outcome.get("turn_id") and outcome.get("session_id"):
            self._stop_if_pending(outcome["session_id"], outcome["turn_id"])
        return self._finish(outcome, started)

    def _stop_if_pending(self, session_id: str, turn_id: str) -> None:
        """Stop an abandoned turn that still blocks the conversation.

        Only a turn Core still holds open is stopped, as an operator would; a
        turn that already failed keeps its recorded outcome.
        """

        try:
            pending = self.call(
                "GET",
                f"/api/v1/chat/sessions/{session_id}/pending-turn",
                params={"view": "status"},
            )
            if pending and pending.get("id") == turn_id:
                self.call("POST", f"/api/v1/chat/turns/{turn_id}/cancel")
        except EvalError:
            pass

    @staticmethod
    def _finish(outcome: dict[str, Any], started: float) -> dict[str, Any]:
        outcome["elapsed_s"] = round(time.monotonic() - started, 2)
        return outcome


class ScratchCore:
    """A Core served from a checkout for the length of the eval."""

    def __init__(
        self,
        checkout: Path,
        data_dir: Path,
        port: int,
        python: str,
        secret_env: str | None,
    ) -> None:
        self.checkout = checkout.resolve()
        self.data_dir = data_dir.resolve()
        self.port = port
        self.python = python
        self.secret_env = secret_env
        self.process: subprocess.Popen[bytes] | None = None
        self.log_path = self.data_dir.parent / f"{self.data_dir.name}.core.log"

    def start(self) -> tuple[str, str]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(24)
        env = {
            **os.environ,
            "NEBULA_V3_API_TOKEN": token,
            "PYTHONPATH": str(self.checkout / "src"),
        }
        command = [
            self.python,
            "-c",
            "from nebula.v3.cli import main; main()",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--data-dir",
            str(self.data_dir),
        ]
        if self.secret_env and self.secret_env not in os.environ:
            # The key lives in the operator's shell profile; an interactive
            # shell loads it without this process ever reading it.
            command = ["bash", "-ic", "exec " + shlex.join(command)]
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                command,
                cwd=self.checkout,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        base_url = f"http://127.0.0.1:{self.port}"
        client = CoreClient(base_url, token)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise EvalError(
                    f"scratch Core exited with {self.process.returncode}; see {self.log_path}"
                )
            try:
                client.call("GET", "/api/v1/health")
                return base_url, token
            except (EvalError, client._httpx.HTTPError):
                time.sleep(1)
        self.stop()
        raise EvalError(f"scratch Core did not become healthy; see {self.log_path}")

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=10)

    def commit(self) -> str | None:
        try:
            sha = subprocess.check_output(
                ["git", "-C", str(self.checkout), "rev-parse", "HEAD"], text=True
            ).strip()
            dirty = subprocess.check_output(
                ["git", "-C", str(self.checkout), "status", "--porcelain"], text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            return None
        return sha + ("+dirty" if dirty else "")


class DatabaseProbe:
    """Optional read-only look at a scratch Core's SQLite store.

    The API exposes only a session's latest snapshot and nothing about in-turn
    checkpoints; the store has both. Any schema drift disables the probe
    rather than failing the eval.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "nebula.db"

    def _query(
        self, sql: str, parameters: tuple[Any, ...]
    ) -> list[tuple[Any, ...]] | None:
        if not self.path.exists():
            return None
        try:
            connection = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True, timeout=10
            )
            try:
                return connection.execute(sql, parameters).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return None

    def snapshots(self, session_id: str) -> list[dict[str, Any]] | None:
        rows = self._query(
            "select payload from entities where kind = 'context_snapshots' "
            "and json_extract(payload, '$.owner_id') = ?",
            (session_id,),
        )
        if rows is None:
            return None
        found = []
        for (payload,) in rows:
            item = json.loads(payload)
            found.append(
                {
                    key: item.get(key)
                    for key in (
                        "id",
                        "version",
                        "status",
                        "compacted_through",
                        "quality",
                        "dropped_items",
                        "segment_count",
                        "reused_segments",
                        "usage",
                        "error",
                    )
                    if key in item
                }
            )
        return sorted(found, key=lambda item: item.get("version") or 0)

    def turn(self, turn_id: str) -> dict[str, Any] | None:
        """A turn's recorded status and usage; a failed stream reports neither."""

        rows = self._query(
            "select payload from entities where kind = 'chat_turns' and id = ?",
            (turn_id,),
        )
        if not rows:
            return None
        item = json.loads(rows[0][0])
        return {
            "status": item.get("status"),
            "usage": item.get("usage") or {},
            "error": (item.get("error") or "")[:300] or None,
        }

    def checkpoints(self, turn_id: str) -> list[dict[str, Any]] | None:
        rows = self._query(
            "select through_step, token_estimate, summary from chat_turn_checkpoints "
            "where turn_id = ? order by through_step",
            (turn_id,),
        )
        if rows is None:
            return None
        found = []
        for through_step, token_estimate, summary in rows:
            data = json.loads(summary) if isinstance(summary, str) else summary or {}
            found.append(
                {
                    "through_step": through_step,
                    "token_estimate": token_estimate,
                    "step_count": data.get("step_count"),
                    "omitted_steps": data.get("omitted_steps"),
                }
            )
        return found


@dataclass(frozen=True)
class ProfileSpec:
    context_window: int
    max_output_tokens: int


class Evaluation:
    def __init__(self, client: CoreClient, args: argparse.Namespace) -> None:
        self.client = client
        self.args = args
        self.workdir = Path(args.workdir).resolve()
        self.database = DatabaseProbe(Path(args.data_dir)) if args.data_dir else None
        self.profiles: dict[str, dict[str, Any]] = {}
        self.pricing: Pricing | None = None
        self.spent = 0.0

    # -- setup ------------------------------------------------------------

    def create_profile(self, name: str, spec: ProfileSpec) -> dict[str, Any]:
        model = self.args.model
        profile = self.client.call(
            "POST",
            "/api/v1/providers",
            json={
                "name": f"{PROJECT_PREFIX}{name} ({spec.context_window}/{spec.max_output_tokens})",
                "provider_type": self.args.provider_type,
                "secret_ref": self.args.secret_ref,
                "model_allowlist": [model],
                "privacy": {"permits_sensitive_data": True},
                "metadata": {
                    "default_model": model,
                    "options": {
                        "context_window": spec.context_window,
                        "max_output_tokens": spec.max_output_tokens,
                    },
                },
            },
        )
        # The catalog refresh stores model limits and prices; verification
        # enables tools and checks route limits.
        self.client.call("POST", f"/api/v1/providers/{profile['id']}/health")
        profile = self.client.call("GET", f"/api/v1/providers/{profile['id']}")
        verified = self.client.call(
            "POST",
            f"/api/v1/providers/{profile['id']}/capabilities/verify",
            json={"model": model, "expected_revision": profile["revision"]},
        )
        status = (verified.get("verification") or {}).get("status")
        if status != "verified":
            raise EvalError(f"provider verification for {model} returned {status!r}")
        profile = self.client.call("GET", f"/api/v1/providers/{profile['id']}")
        descriptor = next(
            (
                item
                for item in profile.get("metadata", {}).get("model_descriptors", [])
                if isinstance(item, dict) and item.get("id") == model
            ),
            None,
        )
        if self.pricing is None:
            self.pricing = pricing_from_descriptor(descriptor)
        return profile

    def create_project(
        self, title: str, files: dict[str, str], *, tools: bool
    ) -> tuple[str, Path | None]:
        """A project; with tools, a scratch folder in host execution mode.

        Tool turns need the command runtime, and host mode is the one every
        machine has; approvals are off so no turn waits on an operator.
        """

        folder: Path | None = None
        payload: dict[str, Any] = {"name": title}
        if tools or files:
            folder = self.workdir / re.sub(r"[^A-Za-z0-9_.-]+", "-", title)
            folder.mkdir(parents=True, exist_ok=True)
            for relative, content in files.items():
                path = folder / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            payload["workspace_path"] = str(folder)
        project = self.client.call("POST", "/api/v1/engagements", json=payload)
        if tools:
            policy = self.client.call(
                "GET", f"/api/v1/engagements/{project['id']}/automation-policy"
            )
            self.client.call(
                "PUT",
                f"/api/v1/engagements/{project['id']}/automation-policy",
                json={
                    "execution_mode": "host",
                    "host_access_acknowledged": True,
                    "approval_policy": "never",
                    "expected_revision": policy.get("revision"),
                },
            )
        return project["id"], folder

    # -- one scenario run ----------------------------------------------------

    def run_scenario(self, scenario: Scenario, repeat: int) -> dict[str, Any]:
        assert self.pricing is not None
        profile = self.profiles[scenario.profile]
        engagement_id, folder = self.create_project(
            f"{PROJECT_PREFIX}{scenario.name} r{repeat} {self.args.nonce}",
            scenario.files,
            tools=any(turn.tools for turn in scenario.turns),
        )
        session_id: str | None = None
        turns: list[dict[str, Any]] = []
        already_read: list[str] = []
        earlier_ids: set[str] = set()
        consecutive_failures = 0
        for index, spec in enumerate(scenario.turns, start=1):
            if consecutive_failures >= MAX_CONSECUTIVE_FAILED_TURNS and not spec.probes:
                # The conversation is stuck; still ask the probes so the run
                # measures what survived instead of ending unscored.
                turns.append(
                    {
                        "index": index,
                        "role": spec.role,
                        "ok": False,
                        "skipped": True,
                        "error": "skipped after repeated failed turns",
                        "elapsed_s": 0,
                    }
                )
                continue
            body: dict[str, Any] = {
                "provider_id": profile["id"],
                "engagement_id": engagement_id,
                "model": self.args.model,
                "messages": [{"role": "user", "content": spec.content}],
                "include_knowledge": False,
                "tools_enabled": spec.tools,
                "allow_cloud_tool_results": True,
                "stream": True,
            }
            if session_id:
                body["session_id"] = session_id
            if self.args.temperature is not None:
                body["temperature"] = self.args.temperature
            if self.args.reasoning_effort:
                body["reasoning_effort"] = self.args.reasoning_effort
            refusals: list[str] = []
            elapsed = 0.0
            prepare = 0.0
            while True:
                outcome = self.client.stream_turn(body)
                elapsed += outcome["elapsed_s"]
                prepare += outcome.get("prepare_s") or outcome["elapsed_s"]
                # Core refused the send before starting a turn (for example a
                # failed compaction) and stored nothing, so resending is what an
                # operator would do and cannot duplicate the message.
                if outcome["ok"] or outcome.get("turn_id"):
                    break
                refusals.append(str(outcome.get("error"))[:300])
                if len(refusals) > self.args.turn_retries:
                    break
            session_id = session_id or outcome.get("session_id")
            record: dict[str, Any] = {
                "index": index,
                "role": spec.role,
                "tools": spec.tools,
                "ok": outcome["ok"],
                "error": outcome.get("error"),
                "refused_attempts": len(refusals),
                "refusals": refusals,
                "turn_id": outcome.get("turn_id"),
                "elapsed_s": round(elapsed, 2),
                # Time before the turn started, refused attempts included.
                "prepare_s": round(prepare, 2),
                "usage": outcome.get("usage") or {},
                "context_usage": outcome.get("context_usage") or {},
                "answer": (outcome.get("answer") or "")[:4_000],
                "tool_calls": [
                    {"name": call["name"], "arguments": call["arguments"]}
                    for call in outcome["tool_calls"]
                ],
            }
            if spec.tools or outcome["tool_calls"]:
                stats = tool_call_stats(
                    outcome["tool_calls"], scenario.files, already_read, earlier_ids
                )
                already_read = stats.pop("read_markers")
                record["tool_stats"] = stats
            earlier_ids |= outcome["produced_ids"]
            if spec.probes:
                record["scores"] = (
                    score_turn(scenario, spec, outcome.get("answer") or "")
                    if outcome["ok"]
                    else unanswered(map(scenario.probe, spec.probes), "turn_failed")
                )
            if session_id:
                record["context"] = self._context(session_id)
            if self.database is not None and record["turn_id"]:
                record["checkpoints"] = self.database.checkpoints(record["turn_id"])
                stored = (
                    None if outcome["ok"] else self.database.turn(record["turn_id"])
                )
                if stored is not None:
                    # A failed turn's provider calls still cost; only the
                    # store has their usage.
                    record["turn_status"] = stored["status"]
                    record["usage"] = stored["usage"]
                    record["usage_source"] = "store"
            turns.append(record)
            self.spent += self.pricing.cost(record["usage"]) + self.pricing.cost(
                record["context_usage"]
            )
            self._progress(scenario, repeat, record)
            consecutive_failures = 0 if outcome["ok"] else consecutive_failures + 1
            if consecutive_failures == MAX_CONSECUTIVE_FAILED_TURNS:
                print(
                    f"  {scenario.name}: {consecutive_failures} turns failed in a row; "
                    "skipping to the probe turns",
                    flush=True,
                )
            if self.spent > self.args.max_cost_usd:
                raise EvalError(
                    f"spend ${self.spent:.4f} passed --max-cost-usd {self.args.max_cost_usd}"
                )
        snapshots = (
            self.database.snapshots(session_id)
            if self.database is not None and session_id
            else None
        )
        return {
            "scenario": scenario.name,
            "repeat": repeat,
            "engagement_id": engagement_id,
            "session_id": session_id,
            "workspace": str(folder) if folder else None,
            **run_metrics(scenario, turns, self.pricing, snapshots),
            "snapshots": snapshots,
            "turns": turns,
        }

    def _context(self, session_id: str) -> dict[str, Any]:
        try:
            status = self.client.call(
                "GET", f"/api/v1/chat/sessions/{session_id}/context"
            )
        except EvalError as exc:
            return {"error": str(exc)[:300]}
        snapshot = status.get("snapshot") or None
        context: dict[str, Any] = {
            "limits": {
                key: status.get(key)
                for key in (
                    "context_window",
                    "max_output_tokens",
                    "target_input_tokens",
                    "compacted_input_target",
                    "capacity_source",
                )
            }
        }
        context |= {
            key: status.get(key)
            for key in (
                "status",
                "estimated_input_tokens",
                "compacted_through",
                "last_provider_request",
                # Fields later builds add; absent keys stay absent.
                "quality",
                "estimate_calibration",
                "reserved_input_tokens",
            )
            if key in status
        }
        notes = status.get("working_notes")
        if isinstance(notes, dict):
            context["working_notes"] = {
                "revision": notes.get("revision"),
                "chars": len(str(notes.get("content") or "")),
            }
        if snapshot:
            context["snapshot"] = {
                key: snapshot.get(key)
                for key in (
                    "id",
                    "version",
                    "status",
                    "compacted_through",
                    "quality",
                    "dropped_items",
                    "segment_count",
                    "reused_segments",
                    "usage",
                    "error",
                )
                if key in snapshot
            }
        return context

    @staticmethod
    def _progress(scenario: Scenario, repeat: int, record: dict[str, Any]) -> None:
        usage = record["usage"]
        context = record.get("context") or {}
        snapshot = context.get("snapshot") or {}
        correct = [item["correct"] for item in record.get("scores", [])]
        parts = [
            f"  {scenario.name} r{repeat} turn {record['index']:>2} {record['role']:<14}",
            "ok " if record["ok"] else "ERR",
            f"{record['elapsed_s']:6.1f}s",
            f"in {usage.get('input_tokens', 0):>7,} cached {usage.get('cached_input_tokens', 0):>7,}",
        ]
        if record["tool_calls"]:
            parts.append(f"tools {len(record['tool_calls'])}")
        if snapshot:
            parts.append(
                f"snapshot v{snapshot.get('version')} {snapshot.get('status')}"
                + (f" {snapshot['quality']}" if snapshot.get("quality") else "")
            )
        if correct:
            parts.append(f"recall {sum(correct)}/{len(correct)}")
        if record.get("error"):
            parts.append(str(record["error"])[:160])
        print(" ".join(parts), flush=True)

    # -- whole evaluation ------------------------------------------------------

    def run(self) -> dict[str, Any]:
        args = self.args
        health = self.client.call("GET", "/api/v1/health")
        others = foreign_projects(self.client.call("GET", "/api/v1/engagements"))
        if others and not args.allow_existing_data:
            raise EvalError(
                f"this Core already has {len(others)} project(s) the eval did not "
                f"create (e.g. {others[0]!r}); use a fresh scratch Core or pass "
                "--allow-existing-data"
            )
        specs = {
            "s1": ProfileSpec(args.s1_window, args.s1_output),
            "tools": ProfileSpec(args.tools_window, args.tools_output),
        }

        def scenarios_for(repeat: int) -> list[Scenario]:
            # Each repeat opens with its own nonce, so a provider cache cannot
            # serve one repeat's conversation from another's.
            return [
                build_scenario(
                    name,
                    args.seed,
                    nonce=f"{args.nonce}-{repeat}",
                    s1_turn_chars=args.s1_turn_chars,
                    s2_files=args.s2_files,
                )
                for name in args.scenarios
            ]

        scenarios = scenarios_for(1)
        needed = {scenario.profile for scenario in scenarios}
        for name in sorted(needed):
            self.profiles[name] = self.create_profile(name, specs[name])
        if args.price_input is not None and args.price_output is not None:
            self.pricing = Pricing(
                args.price_input,
                args.price_output,
                args.price_cached_input
                if args.price_cached_input is not None
                else args.price_input,
                args.price_cache_write,
                source="command line",
            )
        if self.pricing is None:
            raise EvalError(
                "Core's catalog has no price for this model; pass --price-input and "
                "--price-output (USD per million tokens)"
            )
        capacities = {
            name: spec.context_window - spec.max_output_tokens
            for name, spec in specs.items()
        }
        estimate = estimate_cost(scenarios, capacities, self.pricing, args.repeat)
        print(
            f"Estimated cost upper bound: ${estimate:.4f} for {len(scenarios)} "
            f"scenario(s) x {args.repeat} repeat(s) at ${self.pricing.input}/M input, "
            f"${self.pricing.output}/M output ({self.pricing.source})",
            flush=True,
        )
        if estimate > args.max_cost_usd:
            raise EvalError(
                f"estimated ${estimate:.4f} exceeds --max-cost-usd {args.max_cost_usd}"
            )
        runs = []
        for repeat in range(1, args.repeat + 1):
            for scenario in scenarios_for(repeat):
                print(f"{scenario.name} repeat {repeat}: {scenario.title}", flush=True)
                runs.append(self.run_scenario(scenario, repeat))
        profile_of = {scenario.name: scenario.profile for scenario in scenarios}
        profiles = {}
        for name in sorted(needed):
            # The limits Core actually resolved, as its context endpoint reports.
            resolved: dict[str, Any] = next(
                (
                    turn["context"]["limits"]
                    for run in runs
                    if profile_of[run["scenario"]] == name
                    for turn in run["turns"]
                    if (turn.get("context") or {}).get("limits")
                ),
                {},
            )
            profiles[name] = {
                **asdict(specs[name]),
                "provider_profile_id": self.profiles[name]["id"],
                "resolved": resolved,
            }
        report = {
            "schema": REPORT_SCHEMA,
            # Compare reports only when the harness itself is identical.
            "eval_script_sha256": args.eval_script_sha256,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "label": args.label,
            "core": {
                "url": self.client.base_url,
                "version": health.get("version"),
                "commit": args.core_commit or health.get("commit"),
                "checkout": args.serve_from,
            },
            "model": args.model,
            "seed": args.seed,
            "nonce": args.nonce,
            "repeat": args.repeat,
            "temperature": args.temperature,
            "reasoning_effort": args.reasoning_effort,
            "scenarios": [scenario.name for scenario in scenarios],
            "profiles": profiles,
            "pricing": asdict(self.pricing),
            "estimated_cost_usd": round(estimate, 6),
            "total_cost_usd": round(sum(run["metrics"]["cost_usd"] for run in runs), 6),
            "summary": summarize(runs),
            "runs": runs,
        }
        return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    target = parser.add_argument_group("Core")
    target.add_argument(
        "--base-url", help="A running scratch Core, e.g. http://127.0.0.1:18715"
    )
    target.add_argument(
        "--token", help="Its bearer token (default: $NEBULA_V3_API_TOKEN)"
    )
    target.add_argument("--serve-from", help="Serve a scratch Core from this checkout")
    target.add_argument(
        "--data-dir", help="Scratch Core data directory (also enables store probes)"
    )
    target.add_argument("--port", type=int, default=18715)
    target.add_argument("--core-python", default=sys.executable)
    target.add_argument(
        "--allow-existing-data",
        action="store_true",
        help="Run on a Core that holds projects this eval did not create",
    )
    target.add_argument(
        "--core-commit", help="Commit label when not serving from a checkout"
    )
    model = parser.add_argument_group("Model")
    model.add_argument("--model", default=DEFAULT_MODEL)
    model.add_argument("--provider-type", default="openrouter")
    model.add_argument("--secret-ref", default="env:OPEN_ROUTER_API_KEY")
    model.add_argument("--temperature", type=float, default=0.0)
    model.add_argument(
        "--reasoning-effort",
        choices=["none", "minimal", "low", "medium", "high", "xhigh"],
        help="Chat turns' reasoning level (default: the model's own)",
    )
    model.add_argument("--price-input", type=float, help="USD per million input tokens")
    model.add_argument(
        "--price-output", type=float, help="USD per million output tokens"
    )
    model.add_argument(
        "--price-cached-input", type=float, help="USD per million cached input tokens"
    )
    model.add_argument(
        "--price-cache-write",
        type=float,
        help="USD per million prompt-cache write tokens (default: the input price)",
    )
    run = parser.add_argument_group("Run")
    run.add_argument("--scenarios", default=",".join(SCENARIOS))
    run.add_argument("--seed", type=int, default=DEFAULT_SEED)
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--max-cost-usd", type=float, default=1.0)
    run.add_argument(
        "--turn-retries",
        type=int,
        default=2,
        help="Resends of a message Core refused before starting its turn",
    )
    run.add_argument("--s1-window", type=int, default=12_000)
    run.add_argument("--s1-output", type=int, default=1_024)
    run.add_argument("--s1-turn-chars", type=int, default=4_200)
    run.add_argument("--tools-window", type=int, default=16_000)
    run.add_argument("--tools-output", type=int, default=2_048)
    run.add_argument("--s2-files", type=int, default=32)
    run.add_argument(
        "--workdir", help="Where project folders are written (default: beside --out)"
    )
    run.add_argument("--label", help="Report title, e.g. the commit under test")
    run.add_argument(
        "--out",
        required=True,
        help="Report path prefix; writes <out>.json and <out>.md",
    )
    args = parser.parse_args(argv)
    args.scenarios = [
        item.strip() for item in args.scenarios.split(",") if item.strip()
    ]
    unknown = sorted(set(args.scenarios) - set(SCENARIOS))
    if unknown:
        parser.error(f"unknown scenarios: {', '.join(unknown)}")
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    if bool(args.serve_from) == bool(args.base_url):
        parser.error("pass exactly one of --serve-from or --base-url")
    if args.serve_from and not args.data_dir:
        parser.error("--serve-from needs --data-dir")
    args.nonce = secrets.token_hex(3)
    # Hashed at start: the file may change on disk while a long run is going.
    args.eval_script_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if not args.workdir:
        args.workdir = f"{args.out}-workspaces"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    core: ScratchCore | None = None
    try:
        if args.serve_from:
            secret_env = (
                args.secret_ref.removeprefix("env:")
                if args.secret_ref.startswith("env:")
                else None
            )
            core = ScratchCore(
                Path(args.serve_from),
                Path(args.data_dir),
                args.port,
                args.core_python,
                secret_env,
            )
            base_url, token = core.start()
            args.core_commit = args.core_commit or core.commit()
            print(
                f"Scratch Core {args.core_commit} at {base_url} (log {core.log_path})"
            )
        else:
            base_url = args.base_url
            given = args.token or os.environ.get("NEBULA_V3_API_TOKEN")
            if not given:
                raise EvalError("--base-url needs --token or $NEBULA_V3_API_TOKEN")
            token = given
        report = Evaluation(CoreClient(base_url, token), args).run()
    except EvalError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if core is not None:
            core.stop()
    json_path, markdown_path = Path(f"{args.out}.json"), Path(f"{args.out}.md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2) + "\n")
    markdown = render_markdown(report)
    markdown_path.write_text(markdown)
    print()
    print(markdown)
    print(f"Wrote {json_path} and {markdown_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
