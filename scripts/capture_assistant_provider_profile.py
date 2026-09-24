"""Pure source provider configuration capture; no provider/SDK request is made."""

from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import sys
import unicodedata
from pathlib import Path
from unittest.mock import patch
from pydantic import SecretStr
from nebula.v3 import providers as p
from nebula.v3.domain import ProviderProfile
from nebula.v3.credentials import CredentialError, CredentialVaultLockedError
from fastapi.encoders import jsonable_encoder

ROOT = Path(__file__).resolve().parents[1]
BASE = {
    "id": "fixture-provider",
    "revision": 1,
    "created_at": "2030-01-01T12:00:00Z",
    "updated_at": "2030-01-01T12:00:00Z",
    "name": "Fixture",
    "provider_type": "custom",
    "endpoint": "https://fixture.invalid",
    "metadata": {"default_model": "fixture-model"},
}
REQUEST = {"messages": [{"role": "user", "content": "Hello"}]}
SECRET = "FIXTURE-ONLY-NOT-A-REAL-SECRET"


def error(exc):
    result = {"kind": type(exc).__name__, "detail": str(exc)}
    if hasattr(exc, "errors"):
        result["issues"] = jsonable_encoder(exc.errors(include_url=False))
        result.pop("detail")
    return result


def ranges(values):
    result = []
    for value in values:
        if result and value == result[-1][1] + 1:
            result[-1][1] = value
        else:
            result.append([value, value])
    return result


def unicode_tables():
    lower = []
    cased = []
    ignorable = []
    nfkc = []
    for cp in range(0x110000):
        scalar = chr(cp)
        lowered = scalar.lower()
        if lowered != scalar:
            lower.append([cp, lowered])
        is_cased = scalar.islower() or scalar.isupper() or scalar.istitle()
        if is_cased:
            cased.append(cp)
        # Observe exactly the contextual Final_Sigma case rule. Cased-but-
        # ignorable scalars are skipped before the backwards cased test too.
        if (
            (scalar + "Σ").lower().endswith("σ")
            if is_cased
            else ("AΣ" + scalar + "A").lower()[1] == "σ"
        ):
            ignorable.append(cp)
        if cp > 127 and any(
            c in unicodedata.normalize("NFKC", scalar) for c in "/?#@:"
        ):
            nfkc.append(cp)
    return {
        "lower": lower,
        "cased": ranges(cased),
        "case_ignorable": ranges(ignorable),
        "nfkc_delimiters": nfkc,
    }


def collect_provider_profiles(root: Path = ROOT):
    if sys.version_info[:2] != (3, 12) or unicodedata.unidata_version != "15.0.0":
        raise RuntimeError(
            "Provider configuration oracle requires CPython 3.12 / Unicode 15"
        )
    vectors = []

    def case(
        name,
        changes=None,
        *,
        request=None,
        mode="complete",
        environment=None,
        managed=None,
        raw_profile_input=None,
    ):
        raw = BASE | (changes or {})
        if changes is not None and "metadata" in changes:
            raw["metadata"] = changes["metadata"]
        if raw_profile_input is None:
            raw_profile_input = json.dumps(
                raw, ensure_ascii=False, separators=(",", ":")
            )
        raw = json.loads(raw_profile_input)
        profile = ProviderProfile.model_validate(raw)
        trace = []
        env = environment or {}

        def getenv(name, default=None):
            trace.append({"kind": "environment", "name": name})
            return env.get(name, default)

        def resolver(reference):
            trace.append({"kind": "managed", "reference": reference})
            if managed == "locked":
                raise CredentialVaultLockedError("fixture vault is locked")
            if managed == "missing":
                raise CredentialError("fixture credential is missing")
            return SecretStr(SECRET if managed is None else managed)

        result = {}
        with (
            patch.object(p.os, "getenv", getenv),
            patch.object(p, "record_caught_exception", lambda *a, **k: None),
        ):
            try:
                try:
                    provider = p.provider_from_profile(
                        profile, None if managed == "no-resolver" else resolver
                    )
                except CredentialVaultLockedError as exc:
                    raise p.ProviderCredentialLockedError(str(exc)) from exc
                except CredentialError as exc:
                    raise p.ProviderError(str(exc)) from exc
                result["config"] = provider.config.model_dump(mode="json")
                result["managed_reference"] = provider.config.credential_ref
                result["adapter"] = provider.config.kind.value
                actual = p.ModelRequest.model_validate(REQUEST | (request or {}))
                model = provider.require(actual)
                result["required_model"] = model
                # Configuration-only headers/tuning. Never instantiate a HTTP client.
                if provider.config.kind == p.ProviderKind.OPENAI_COMPATIBLE:
                    headers = provider._headers()
                    timeout = (
                        p._native_request_timeout(provider.config)
                        if mode == "complete"
                        else None
                    )
                    policy = p.retry_policy(provider.config)
                    allowed = provider.openrouter_allowed_providers
                    # Publish an owned wire configuration only after every source
                    # step succeeds. The trace retains failure-phase ordering.
                    result["headers"] = headers
                    if timeout is not None:
                        result["completion_timeout"] = timeout
                    result["retry"] = {
                        "attempts": policy.attempts,
                        "backoff_seconds": policy.backoff_seconds,
                    }
                    result["allowed_providers"] = allowed
                result["accepted"] = True
            except Exception as exc:
                result["accepted"] = False
                result["error"] = error(exc)
        vectors.append(
            {
                "name": name,
                "profile": profile.model_dump(mode="json"),
                "profile_input_raw": raw_profile_input,
                "metadata_raw": json.dumps(
                    profile.metadata, ensure_ascii=False, separators=(",", ":")
                ),
                "request": REQUEST | (request or {}),
                "mode": mode,
                "environment": env,
                "managed": managed,
                "trace": trace,
                "expected": result,
            }
        )

    for flavor, entry in p.PROVIDER_CATALOG.items():
        endpoint = (
            None
            if entry.default_base_url
            else (
                "https://fixture.invalid"
                if not entry.local
                else "http://127.0.0.1:8999"
            )
        )
        environment = (
            {entry.suggested_key_env: SECRET} if entry.suggested_key_env else {}
        )
        case(
            "catalog-" + flavor.value,
            {"provider_type": flavor.value, "endpoint": endpoint},
            environment=environment,
        )
    for flavor in [
        "openai-compatible",
        "openai_compatible",
        "openai-responses",
        "openai_responses",
        "unknown",
    ]:
        case("type-" + flavor, {"provider_type": flavor})
    for name, endpoint, local in [
        ("private-unmarked", "http://127.0.0.1:9000", False),
        ("private-local", "http://127.0.0.1:9000", True),
        ("cloud-local", "https://fixture.invalid", True),
        ("public-http", "http://fixture.invalid", False),
        ("cloud-https", "https://fixture.invalid/path///", False),
        ("username", "https://alice@fixture.invalid", False),
        ("empty-username", "https://@fixture.invalid", False),
        ("query", "https://fixture.invalid?q=1", False),
        ("empty-query", "https://fixture.invalid?", False),
        ("fragment", "https://fixture.invalid#s", False),
        ("missing-host", "https:///x", False),
        ("wrong-scheme", "ftp://fixture.invalid", False),
        ("upper-scheme", "HTTPS://fixture.invalid", False),
        ("bracket-v4", "https://[127.0.0.1]", True),
        ("ipv6", "http://[::1]:99/v1", True),
        ("private-v4", "http://192.0.0.9", True),
        ("shared-v4", "http://100.64.0.1", True),
        ("reserved-v4", "http://240.0.0.1", True),
        ("multicast-v4", "http://224.0.0.1", True),
        ("v6-document", "http://[2001:db8::1]", True),
        ("mapped-public", "http://[::ffff:8.8.8.8]", True),
        ("mapped-private", "http://[::ffff:127.0.0.1]", True),
        ("dns-dot", "http://LOCALHOST.", True),
        ("local-name", "http://host.local", True),
        ("bad-port", "https://fixture.invalid:not-a-port", False),
        ("empty-fragment", "https://fixture.invalid#", False),
        ("embedded-tab", "https://fix\tture.invalid", False),
        ("nfkc", "https://fixture.invalid\uff0fx", False),
        ("bracket-prefix", "http://prefix[::1]", True),
        ("bracket-suffix", "http://[::1]suffix", True),
        ("ipvfuture-invalid", "https://[v-no]", True),
        ("ipvfuture-valid", "https://[v1.host]", True),
        ("nfkc-quote", "https://fixture'host\uff0fx", False),
    ]:
        case("endpoint-" + name, {"endpoint": endpoint, "is_local": local})
    case("missing-explicit-endpoint", {"endpoint": None})
    case(
        "catalog-local-forced",
        {"provider_type": "ollama", "endpoint": None, "is_local": False},
    )
    case("privacy-cloud", {"is_local": True, "privacy": {"local_only": True}})
    case("default-first-model", {"model_allowlist": ["one", "two"], "metadata": {}})
    for name, value in [
        ("null", None),
        ("empty", ""),
        ("false", False),
        ("zero", 0),
        ("true", True),
        ("number", 4),
        ("object", {"x": 1}),
        ("list", ["x"]),
    ]:
        case("default-" + name, {"metadata": {"default_model": value}})
    case(
        "default-invalid-before-endpoint-after",
        {
            "endpoint": "https://user@fixture.invalid",
            "metadata": {"default_model": True},
        },
    )
    case(
        "default-invalid-with-endpoint-field",
        {"endpoint": "ftp://fixture.invalid", "metadata": {"default_model": True}},
    )
    case("disabled-before-missing-model", {"enabled": False, "metadata": {}})
    case(
        "allowlist-denial",
        {"model_allowlist": ["one"], "metadata": {}},
        request={"model": "two"},
    )
    case("model-request-overrides-default", request={"model": "other"})
    case("empty-request-model-fallback", request={"model": ""})
    case("required-structured", request={"response_schema": {"type": "object"}})
    case("empty-schema-no-capability", request={"response_schema": {}})
    case(
        "required-tools",
        request={
            "tools": [
                {
                    "name": "fixture",
                    "description": "Inert fixture",
                    "input_schema": {"type": "object"},
                    "strict": True,
                }
            ]
        },
    )
    verified = {
        "fixture-model": {
            "model": "fixture-model",
            "status": "verified",
            "checked_at": "2030-01-01T12:00:00Z",
        }
    }
    strict_tool = {
        "name": "fixture",
        "description": "Inert fixture",
        "input_schema": {"type": "object"},
        "strict": True,
    }
    case(
        "capability-verified-tools",
        {"capability_verifications": verified},
        request={"tools": [strict_tool]},
    )
    case(
        "capability-complete",
        {
            "capability_verifications": verified,
            "capabilities": {
                "streaming": True,
                "strict_structured_output": True,
                "vision": True,
                "documents": True,
                "audio": True,
                "embeddings": True,
                "reasoning_controls": True,
                "parallel_tool_calls": True,
            },
            "metadata": {
                "default_model": "fixture-model",
                "options": {"context_window": 10**100, "max_output_tokens": 2000},
            },
        },
        request={"tools": [strict_tool], "response_schema": {"type": "object"}},
    )
    case(
        "capability-unverified-flag",
        {"capabilities": {"tool_calling": True}},
        request={"tools": [strict_tool]},
    )
    case(
        "capability-tool-result",
        request={
            "tool_results": [
                {"call_id": "call", "name": "fixture", "output": {"retained": True}}
            ]
        },
    )
    case("capability-streaming-not-required", mode="stream")
    for ref in [
        "env:FIXTURE_KEY",
        "vault:" + "a" * 32,
        "session:" + "b" * 32,
        "systemd:fixture-key",
    ]:
        case(
            "credential-" + ref.split(":")[0],
            {"secret_ref": ref},
            environment={"FIXTURE_KEY": SECRET},
        )
    case("credential-env-missing", {"secret_ref": "env:FIXTURE_KEY"})
    case(
        "credential-env-empty",
        {"secret_ref": "env:FIXTURE_KEY"},
        environment={"FIXTURE_KEY": ""},
    )
    case("credential-env-disabled", {"secret_ref": "env:FIXTURE_KEY", "enabled": False})
    case(
        "credential-env-invalid-endpoint",
        {"secret_ref": "env:FIXTURE_KEY", "endpoint": "ftp://fixture.invalid"},
    )
    for outcome in ["locked", "missing", "no-resolver", ""]:
        case(
            "credential-managed-" + (outcome or "empty"),
            {"secret_ref": "vault:" + "a" * 32},
            managed=outcome,
        )
    case(
        "managed-empty-invalid-endpoint",
        {"secret_ref": "vault:" + "a" * 32, "endpoint": "http://fixture.invalid"},
        managed="",
    )
    case(
        "managed-error-before-endpoint",
        {"secret_ref": "vault:" + "a" * 32, "endpoint": "ftp://fixture.invalid"},
        managed="locked",
    )
    case(
        "unknown-type-before-managed",
        {"provider_type": "unknown", "secret_ref": "vault:" + "a" * 32},
        managed="locked",
    )
    for name, options in [
        (
            "ordinary",
            {
                "retry_attempts": 2,
                "retry_backoff_seconds": 1.25,
                "request_timeout_seconds": 42,
            },
        ),
        (
            "clamped",
            {
                "retry_attempts": 100,
                "retry_backoff_seconds": 100,
                "request_timeout_seconds": 9999,
            },
        ),
        (
            "zero",
            {
                "retry_attempts": 0,
                "retry_backoff_seconds": 0,
                "request_timeout_seconds": 0,
            },
        ),
        (
            "negative",
            {
                "retry_attempts": -1,
                "retry_backoff_seconds": -1,
                "request_timeout_seconds": -1,
            },
        ),
        (
            "bool",
            {
                "retry_attempts": True,
                "retry_backoff_seconds": False,
                "request_timeout_seconds": True,
            },
        ),
        (
            "fraction",
            {
                "retry_attempts": 2.9,
                "retry_backoff_seconds": 0.125,
                "request_timeout_seconds": 3.125,
            },
        ),
        (
            "invalid",
            {
                "retry_attempts": {},
                "retry_backoff_seconds": [],
                "request_timeout_seconds": "no",
            },
        ),
        (
            "null",
            {
                "retry_attempts": None,
                "retry_backoff_seconds": None,
                "request_timeout_seconds": None,
            },
        ),
        (
            "strings",
            {
                "retry_attempts": " 2.9 ",
                "retry_backoff_seconds": "1_0.5",
                "request_timeout_seconds": " 1_200 ",
            },
        ),
        (
            "unicode",
            {
                "retry_attempts": "٢",
                "retry_backoff_seconds": "１.２５",
                "request_timeout_seconds": " ١_٢٠ ",
            },
        ),
        (
            "infinity",
            {
                "retry_attempts": "inf",
                "retry_backoff_seconds": "inf",
                "request_timeout_seconds": "inf",
            },
        ),
        (
            "nan",
            {
                "retry_attempts": "nan",
                "retry_backoff_seconds": "nan",
                "request_timeout_seconds": "nan",
            },
        ),
        ("huge-integer", {"retry_attempts": 10**400}),
        (
            "provider-list",
            {
                "openrouter_providers": [
                    " One ",
                    "one",
                    "TWO",
                    False,
                    "\x1cTHREE\x1f",
                    "",
                ]
            },
        ),
        (
            "provider-list-unicode",
            {
                "openrouter_providers": [
                    "ΟΣ",
                    "ΟΣΑ",
                    "AΣ\u0345A",
                    "AΣ\u0345!",
                    "\u1c89",
                    "İ",
                ]
            },
        ),
    ]:
        case(
            "tuning-" + name,
            {"metadata": {"default_model": "fixture-model", "options": options}},
        )
    case(
        "tuning-environment",
        environment={
            "NEBULA_PROVIDER_RETRY_ATTEMPTS": "4",
            "NEBULA_PROVIDER_RETRY_BACKOFF_SECONDS": "2",
            "NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS": "32",
        },
    )
    case(
        "tuning-stream",
        mode="stream",
        environment={"NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS": "bad"},
    )
    for name, options in [
        ("custom", {"api_key_header": "X-Token", "api_key_scheme": "token:"}),
        ("null", {"api_key_header": None, "api_key_scheme": None}),
        (
            "opaque",
            {
                "api_key_header": "X-Fixture",
                "api_key_scheme": [{"z": 1, "a": 2}, None, False],
            },
        ),
    ]:
        case(
            "header-" + name,
            {
                "secret_ref": "env:FIXTURE_KEY",
                "metadata": {"default_model": "fixture-model", "options": options},
            },
            environment={"FIXTURE_KEY": SECRET},
        )
    duplicate_base = BASE | {"secret_ref": "env:FIXTURE_KEY"}
    raw_duplicate = (
        json.dumps(duplicate_base, ensure_ascii=False, separators=(",", ":"))[:-1]
        + r',"metadata":{" options ":{"retry_attempts":1},"options":{"api_key_scheme":[{"z":1,"a":2}],"api_key_header":"X-Fixture"}," options ":{"retry_attempts":7},"default_model":"fixture-model","\u001cno-strip\u001f":{" z ":1,"z":2}}}'
    )
    case(
        "metadata-raw-duplicates-then-trim",
        raw_profile_input=raw_duplicate,
        environment={"FIXTURE_KEY": SECRET},
    )
    for name, parameters in [
        ("list", ["reasoning", False, None, {"z": 1, "a": 2}, [1, 2]]),
        ("string", "abc"),
        ("object", {"z": True, "a": False}),
        ("false", False),
        ("number", 4),
    ]:
        case(
            "descriptors-" + name,
            {
                "metadata": {
                    "default_model": "fixture-model",
                    "model_descriptors": [
                        None,
                        {"id": 1},
                        {
                            "id": "fixture-model",
                            "supported_parameters": parameters,
                            "reasoning_mandatory": True,
                        },
                    ],
                }
            },
        )
    return {
        "format": "nebula.assistant-provider-profile/v1",
        "source_sha256": {
            f: hashlib.sha256((root / f).read_bytes()).hexdigest()
            for f in [
                "src/nebula/v3/providers.py",
                "src/nebula/v3/domain.py",
                "src/nebula/v3/api.py",
            ]
        },
        "catalog": [v.model_dump(mode="json") for v in p.PROVIDER_CATALOG.values()],
        "locality": {
            str(version): {
                name: [str(n) for n in getattr(constants, name, [])]
                for name in [
                    "_private_networks",
                    "_private_networks_exceptions",
                    "_reserved_networks",
                ]
            }
            for version, constants in [
                (4, ipaddress._IPv4Constants),
                (6, ipaddress._IPv6Constants),
            ]
        },
        "unicode_tables": unicode_tables(),
        "decimal_zeroes": [
            i
            for i in range(0x110000)
            if unicodedata.category(chr(i)) == "Nd" and unicodedata.decimal(chr(i)) == 0
        ],
        "python_version": __import__("sys").version.split()[0],
        "unicode_version": unicodedata.unidata_version,
        "vectors": vectors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    args.output.write_text(
        json.dumps(collect_provider_profiles(args.root), ensure_ascii=False, indent=2)
        + "\n"
    )
