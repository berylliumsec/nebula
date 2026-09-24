#!/usr/bin/env python3
"""Passive ScopePolicy hydration/privacy oracle; isolated SQLite, no execution."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import stringprep
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unicodedata
from unittest.mock import patch

from fastapi.encoders import jsonable_encoder
from pydantic import ValidationError
import pydantic

from nebula.v3 import domain, privacy
from nebula.v3.database import Database
from nebula.v3.domain import Engagement, MissionGrant, ScopePolicy
from nebula.v3.storage import NebulaStore
from nebula.v3.web_search import web_search_enabled

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.capture_assistant_model_validation import model_metadata

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2030, 1, 1, 12, tzinfo=timezone.utc)
BASE = {
    "id": "policy",
    "created_at": "2020-01-01T00:00:00Z",
    "updated_at": "2020-01-01T00:00:00Z",
    "revision": 1,
}


def require_runtime():
    if (
        sys.implementation.name != "cpython"
        or sys.version_info[:2] != (3, 12)
        or unicodedata.unidata_version != "15.0.0"
    ):
        raise RuntimeError(
            "Scope-policy source capture requires CPython 3.12 / Unicode 15.0.0; retain the complete Unicode table comparison"
        )


@contextmanager
def frozen_clock(trace):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            value = NOW + timedelta(microseconds=len(trace))
            trace.append(value.isoformat())
            return (
                value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)
            )

    with patch("nebula.v3.domain.datetime", Frozen):
        yield


def observation(function):
    try:
        value = function()
        return {"accepted": True, "payload": jsonable_encoder(value)}
    except ValidationError as error:
        return {
            "accepted": False,
            "errors": jsonable_encoder(error.errors(include_url=False)),
            "exception_preview": str(error)[:300],
        }
    except Exception as error:
        return {
            "accepted": False,
            "error": {"kind": type(error).__name__, "detail": str(error)},
        }


def vector(name, value, model=ScopePolicy, boundary=None):
    trace = []
    with frozen_clock(trace):
        expected = observation(
            lambda: model.model_validate(value).model_dump(mode="json")
        )
    result = {
        "name": name,
        "model": model.__name__,
        "input": value,
        "raw_input": json.dumps(value, ensure_ascii=False),
        "expected": expected,
        "expected_clock_values": trace,
    }
    if boundary:
        result["strict_retained_boundary"] = boundary
    return result


def ranges(points):
    result = []
    for point in points:
        if result and result[-1][1] + 1 == point:
            result[-1][1] = point
        else:
            result.append([point, point])
    return result


def static_data():
    require_runtime()
    ucd = unicodedata.ucd_3_2_0
    mappings, combining, compositions = {}, {}, {}
    prohibited, bidi_r, bidi_l, nfkc_delimiters, nonprintable = [], [], [], [], []
    decimal_zeroes = []
    for code in range(0x110000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        char = chr(code)
        if unicodedata.decimal(char, None) == 0:
            decimal_zeroes.append(code)
        if not char.isprintable():
            nonprintable.append(code)
        mapped = (
            ""
            if stringprep.in_table_b1(char)
            else ucd.normalize("NFKD", stringprep.map_table_b2(char))
        )
        if mapped != char:
            mappings[str(code)] = mapped
        if weight := ucd.combining(char):
            combining[str(code)] = weight
        decomp = ucd.decomposition(char).split()
        if len(decomp) == 2 and not decomp[0].startswith("<"):
            pair = "".join(chr(int(value, 16)) for value in decomp)
            if ucd.normalize("NFC", pair) == char:
                compositions[pair] = char
        if any(
            table(char)
            for table in (
                stringprep.in_table_c12,
                stringprep.in_table_c22,
                stringprep.in_table_c3,
                stringprep.in_table_c4,
                stringprep.in_table_c6,
                stringprep.in_table_c7,
                stringprep.in_table_c8,
                stringprep.in_table_c9,
            )
        ):
            prohibited.append(code)
        if stringprep.in_table_d1(char):
            bidi_r.append(code)
        if stringprep.in_table_d2(char):
            bidi_l.append(code)
        if char not in "/?#@:" and any(
            c in "/?#@:" for c in unicodedata.normalize("NFKC", char)
        ):
            nfkc_delimiters.append(code)
    return {
        "python_minor": "3.12",
        "unicode_version": unicodedata.unidata_version,
        "nameprep_unicode_version": ucd.unidata_version,
        "mappings": mappings,
        "combining": combining,
        "compositions": compositions,
        "prohibited": ranges(prohibited),
        "bidi_r": ranges(bidi_r),
        "bidi_l": ranges(bidi_l),
        "nfkc_delimiters": nfkc_delimiters,
        "nonprintable": ranges(nonprintable),
        "decimal_zeroes": decimal_zeroes,
    }


def collect_scope_policy():
    require_runtime()
    assert Path(domain.__file__).resolve().is_relative_to(ROOT / "src")
    vectors = []

    def add(name, changes=None, remove=()):
        value = {**BASE, "engagement_id": "project", **(changes or {})}
        for key in remove:
            value.pop(key)
        vectors.append(vector(name, value))

    add("defaults")
    for name in ScopePolicy.model_fields:
        add("null-" + name, {name: None})
    add("missing-engagement", remove=["engagement_id"])
    for value in [None, [], "opaque", 1]:
        vectors.append(vector("root-" + type(value).__name__, value))
    add(
        "ordered-errors",
        {
            "revision": 0,
            "engagement_id": 2,
            "allowed_ports": [None, "bad"],
            "local_only": "maybe",
            "second_extra": 1,
            "first_extra": 2,
        },
    )
    add(
        "trim-and-defaults",
        {
            "engagement_id": " project ",
            "prohibited_actions": [" a ", "\u001cvalue\u001f"],
            "on_demand_tools": "no",
            "local_only": "yes",
            "max_concurrency": "2.0",
            "revision": str(10**100),
        },
    )
    for value in [0, 257, 1.5, True, " 256 ", "0__2"]:
        add("concurrency-" + repr(value), {"max_concurrency": value})
    for value in [2, 2**63 - 1, 2**63, float(-(2**63)), " y ", "ON"]:
        add("boolean-" + repr(value), {"local_only": value})
    for values in [
        [
            "192.0.2.129/24",
            "192.0.2.0/255.255.255.0",
            "2001:db8::abcd/64",
            "::ffff:192.0.2.1/128",
        ],
        ["192.0.2.1/0.0.0.255"],
        ["192.0.2.1"],
        ["fe80::1%eth0/64"],
        ["fe80::1%eth0/128"],
        ["01.2.3.4"],
        ["192.0.2.1/33"],
        ["::1/129"],
        ["192.0.2.1/255.0.255.0"],
        ["bad\nline"],
        [1],
    ]:
        add("cidr-" + str(len(vectors)), {"allowed_cidrs": values})
    add("cidr-arabic-prefix", {"allowed_cidrs": ["192.0.2.1/٢٤", "2001:db8::1/٦٤"]})
    add(
        "cidr-nag-mundari-prefix", {"allowed_cidrs": ["192.0.2.1/\U0001e4f2\U0001e4f4"]}
    )
    add("cidr-int-string-limit", {"allowed_cidrs": ["192.0.2.1/" + "0" * 4301]})
    add(
        "url-port-int-string-limit",
        {"allowed_urls": ["http://example.com:" + "0" * 4301]},
    )
    domains = [
        "EXAMPLE.COM.",
        "*.Example.COM",
        "https://BÜCHER.example/",
        "faß.de",
        "K.example",
        "a\u0345.example",
        "🌌.example",
        "\U0001e030.example",
        "مثال.إختبار",
        "aאב.example",
        "example..com",
        "https://example.com:",
        "https://example.com:443",
        "https://@example.com",
        "https://example.com?",
        "https://example.com#",
        "https://example.com/path",
        "ftp://example.com",
        "http://[::1]",
        "http://example.com:bad",
        "http://[bad]",
        "http://example.com／evil",
        "a_b.example",
        "-a.example",
        "a-.example",
        ".",
        "a" * 64 + ".com",
        "\u00ad.example",
        "\u200d.example",
        "\ue000.example",
        "\u001cexample.com\u001f",
    ]
    domains.extend(
        [
            "ftp://example.com:bad",
            "http://@example.com:bad",
            "http://[192.0.2.1]",
            "a\u0315\u0300.example",
        ]
    )
    for index, value in enumerate(domains):
        add(f"domain-{index}", {"allowed_domains": [value]})
    add(
        "domain-dedup",
        {"allowed_domains": ["EXAMPLE.COM.", "https://example.com/", "*.EXAMPLE.com"]},
    )
    urls = [
        "HTTP://BÜCHER.Example:80/a?x=1",
        "https://faß.de",
        "http://[2001:DB8::1]:80",
        "http://[fe80::1%AbC]/",
        "http://example.com:",
        "http://example.com/?",
        "http://example.com/#",
        "http://example.com/#fragment",
        "http://@example.com",
        "http://example.com:65536",
        "http://example.com:bad",
        "http://example.com:-1",
        "http://example.com:00080",
        "http://example.com:１２",
        "http://[v1.future]/",
        "http://[bad]",
        "http://[::1]garbage/path",
        "http://bad host/",
        "https://a_b.example",
        "http://example.com./a",
        "http://example.com/a\tb",
        "\u001chttp://example.com",
        "ftp://example.com",
        "http:///path",
        "http://example.com／evil",
        "http://🌌.example",
        "http://\ue000.example",
        "http://example.com/æ?x=🌌",
    ]
    for index, value in enumerate(urls):
        add(f"url-{index}", {"allowed_urls": [value]})
    add(
        "url-dedup",
        {
            "allowed_urls": [
                "HTTP://EXAMPLE.COM",
                "http://example.com/",
                "http://example.com:80/",
            ]
        },
    )
    for values in [
        [65535, "080", 0, True, 80.0, 0],
        [-1],
        [65536],
        [10**100],
        ["bad", None],
    ]:
        add("ports-" + str(len(vectors)), {"allowed_ports": values})
    add(
        "tools-normalized",
        {"always_loaded_tools": [" z ", "", "a", "a", "\u001ccontrol\u001f"]},
    )
    add("tools-name-too-long", {"always_loaded_tools": ["x" * 301]})
    add(
        "tools-list-too-long-invalid-item",
        {"always_loaded_tools": [None] + ["a"] * 500},
    )
    add(
        "window-offset",
        {
            "not_before": "2029-01-01T02:00:00+02:00",
            "not_after": "2029-01-01T00:00:00.000001Z",
        },
    )
    add(
        "window-equal",
        {"not_before": "2029-01-01T00:00:00Z", "not_after": "2029-01-01T00:00:00Z"},
    )
    add("window-naive", {"not_before": "2029-01-01T00:00:00", "not_after": "bad"})
    add(
        "entity-before-window",
        {
            "updated_at": "2019-01-01T00:00:00Z",
            "not_before": "2029-01-01T00:00:00Z",
            "not_after": "2028-01-01T00:00:00Z",
        },
    )
    grant = {
        "risk_classes": ["local_read", "passive"],
        "expires_at": "2031-01-01T00:00:00Z",
        "granted_by": " operator ",
    }
    for name, values in [
        ("default-time", [grant]),
        ("two-defaults", [grant, grant]),
        ("early-invalid-still-default", [{**grant, "risk_classes": ["bad"]}]),
        ("later-invalid-still-default", [{**grant, "granted_by": " "}]),
        ("scope-invalid-still-default", [grant]),
        ("grant-root", [None]),
        ("grant-extra", [{**grant, "z": 1, "a": 2}]),
        ("grant-aware", [{**grant, "granted_at": "2030-01-01T13:00:00+01:00"}]),
        (
            "grant-naive",
            [{**grant, "granted_at": "2030-01-01", "expires_at": "2031-01-01"}],
        ),
        ("grant-expired", [{**grant, "expires_at": "2029-01-01T00:00:00Z"}]),
        ("grant-listtype", {"opaque": True}),
        ("grant-required", [{}]),
    ]:
        add(
            name,
            {
                "grants": values,
                **(
                    {"local_only": "invalid"}
                    if name == "scope-invalid-still-default"
                    else {}
                ),
            },
        )
    for name, values in [
        (
            "grant-all-risks",
            {**grant, "risk_classes": [item.value for item in domain.RiskClass]},
        ),
        ("grant-order", {"expires_at": "bad", "granted_by": None}),
        ("grant-empty-risks", {**grant, "risk_classes": []}),
    ]:
        vectors.append(vector(name, values, MissionGrant))
    boundaries = []
    for key in BASE:
        value = {**BASE, "engagement_id": "project"}
        value.pop(key)
        # Factory-backed base identities are intentionally never invented by the Rust retained boundary.
        with patch("nebula.v3.domain.uuid4", return_value="fixed-missing-id"):
            boundaries.append(vector("missing-base-" + key, value, boundary=key))
    store_data = storage_observations()
    sources = [
        "src/nebula/v3/domain.py",
        "src/nebula/v3/privacy.py",
        "src/nebula/v3/storage.py",
        "src/nebula/v3/web_search.py",
    ]
    return {
        "source_runtime": {
            "python_minor": "3.12",
            "unicode_version": unicodedata.unidata_version,
            "pydantic": pydantic.__version__,
        },
        "source_sha256": {
            name: sha256((ROOT / name).read_bytes()).hexdigest() for name in sources
        },
        "dependency_schemas": {"scope_policies": ScopePolicy.model_json_schema()},
        "models": {
            model.__name__: model_metadata(model)
            for model in [ScopePolicy, MissionGrant]
        },
        "vectors": vectors,
        "strict_retained_boundary_observations": boundaries,
        "static_data": static_data(),
        **store_data,
    }


def storage_observations():
    cases = []
    with TemporaryDirectory(prefix="nebula-scope-policy-") as temporary:
        path = Path(temporary) / "fixture.db"
        database = Database(path)
        store = NebulaStore(database)
        payloads = {
            "allow": {
                **BASE,
                "id": "allow",
                "engagement_id": "project",
                "web_search": True,
            },
            "local": {
                **BASE,
                "id": "local",
                "engagement_id": "project",
                "local_only": "yes",
                "web_search": True,
            },
            "foreign": {
                **BASE,
                "id": "foreign",
                "engagement_id": "other",
                "local_only": True,
            },
            "malformed": {
                **BASE,
                "id": "malformed",
                "engagement_id": "project",
                "local_only": "maybe",
            },
            "legacy-grant": {
                **BASE,
                "id": "legacy-grant",
                "engagement_id": "project",
                "grants": [
                    {
                        "risk_classes": [],
                        "expires_at": "2031-01-01T00:00:00Z",
                        "granted_by": "operator",
                    }
                ],
            },
        }
        with sqlite3.connect(path) as raw:
            for identifier, payload in payloads.items():
                raw.execute(
                    "INSERT INTO entities(id,kind,engagement_id,revision,payload,created_at,updated_at) VALUES(?,?,?,1,?,'2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')",
                    (
                        identifier,
                        "scope_policies",
                        payload["engagement_id"],
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
            raw.execute(
                "INSERT INTO entities(id,kind,engagement_id,revision,payload,created_at,updated_at) VALUES('wrong-kind','chat_sessions','project',1,'{}','2020-01-01 00:00:00.000000','2020-01-01 00:00:00.000000')"
            )
            raw.row_factory = sqlite3.Row
            initial_rows = [
                dict(row) for row in raw.execute("SELECT * FROM entities ORDER BY id")
            ]
        for identifier, local in [
            (None, False),
            ("", False),
            ("missing", True),
            ("wrong-kind", True),
            ("malformed", True),
            ("foreign", False),
            ("foreign", True),
            ("local", False),
            ("local", True),
            ("allow", False),
            ("allow", True),
            ("legacy-grant", False),
        ]:
            project = Engagement.model_validate(
                {
                    **BASE,
                    "id": "project",
                    "name": "Project",
                    "scope_policy_id": identifier,
                }
            )
            trace, lookups = [], []
            get = store.get

            def observed_get(model, identity):
                lookups.append({"kind": model.entity_kind, "id": identity})
                return get(model, identity)

            with (
                frozen_clock(trace),
                patch.object(store, "get", side_effect=observed_get),
            ):
                expected = observation(
                    lambda: privacy.validate_engagement_provider_privacy(
                        store,
                        project,
                        SimpleNamespace(config=SimpleNamespace(local=local)),
                    )
                )
            case = {
                "name": f"privacy-{identifier}-{local}",
                "engagement": project.model_dump(mode="json"),
                "provider_local": local,
                "expected": expected,
                "expected_lookups": lookups,
                "expected_clock_values": trace,
            }
            if expected["accepted"] and identifier:
                with frozen_clock([]):
                    policy = get(ScopePolicy, identifier)
                    case["expected_policy"] = policy.model_dump(mode="json")
                    case["expected_web_search_enabled"] = web_search_enabled(policy)
            cases.append(case)
        with sqlite3.connect(path) as raw:
            raw.row_factory = sqlite3.Row
            final_rows = [
                dict(row) for row in raw.execute("SELECT * FROM entities ORDER BY id")
            ]
        assert initial_rows == final_rows, (
            "passive privacy lookup mutated retained state"
        )
        database.engine.dispose()
        return {
            "initial_entity_rows": initial_rows,
            "privacy_cases": cases,
            "final_entity_rows": final_rows,
            "normalization": "No model/business validation is mocked. Only trusted domain clock is fixed. Real isolated NebulaStore.get and privacy validator run; no app/lifespan/provider instance/network/tool. Raw entity rows are unchanged. Pure ScopePolicy decoding admits canonical arbitrary positive revision; SQL envelope binding remains strict. Missing factory-backed base fields are separate strict retained boundary observations.",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--static-output", type=Path)
    arguments = parser.parse_args()
    captured = collect_scope_policy()
    arguments.output.write_text(
        json.dumps(captured, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )
    if arguments.static_output:
        arguments.static_output.write_text(
            json.dumps(
                captured["static_data"],
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
