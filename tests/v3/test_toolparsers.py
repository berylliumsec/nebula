import asyncio
import json
import stat

import pytest
from pydantic import ValidationError

from nebula.v3.toolparsers import (
    MAX_PARSER_INPUT_BYTES,
    ParserContainerContract,
    SandboxParserExecutor,
    ToolOutputParseError,
    parse_json,
    parse_jsonl,
    parse_nikto,
    parse_nmap_xml,
    parse_nuclei_jsonl,
    parse_sarif,
    parse_tool_output,
)


DIGEST = "a" * 64


def test_json_jsonl_sarif_and_nuclei_parsers_are_structured():
    assert parse_json('{"ok":true}') == {"ok": True}
    assert parse_jsonl('{"one":1}\n\n{"two":2}\n') == {
        "records": [{"one": 1}, {"two": 2}]
    }
    sarif = {"version": "2.1.0", "runs": []}
    assert parse_sarif(json.dumps(sarif)) == sarif
    assert parse_nuclei_jsonl('{"template-id":"tls"}\n') == {
        "findings": [{"template-id": "tls"}]
    }
    assert parse_tool_output("json/v1", "{}") == {}


@pytest.mark.parametrize(
    "call",
    [
        lambda: parse_json("[]"),
        lambda: parse_json("{"),
        lambda: parse_jsonl("[]\n"),
        lambda: parse_jsonl("{bad}\n"),
        lambda: parse_sarif('{"version":"2.1.0"}'),
        lambda: parse_tool_output("missing/v1", "{}"),
    ],
)
def test_structured_parsers_reject_malformed_output(call):
    with pytest.raises(ToolOutputParseError):
        call()


def test_nmap_xml_parser_extracts_hosts_services_and_summary():
    source = """<nmaprun scanner="nmap" args="nmap -sT">
  <host><status state="up"/><address addr="192.0.2.2" addrtype="ipv4"/>
    <hostnames><hostname name="host.example"/></hostnames>
    <ports><port protocol="tcp" portid="443"><state state="open"/>
      <service name="https" product="nginx"/></port></ports></host>
  <runstats><finished elapsed="0.3" summary="done"/></runstats>
</nmaprun>"""
    parsed = parse_nmap_xml(source)

    assert parsed["scanner"] == "nmap"
    assert parsed["hosts"][0]["addresses"] == [{"address": "192.0.2.2", "type": "ipv4"}]
    assert parsed["hosts"][0]["ports"][0] == {
        "protocol": "tcp",
        "port": 443,
        "state": "open",
        "service": {"name": "https", "product": "nginx"},
    }
    assert parsed["summary"]["elapsed"] == "0.3"


@pytest.mark.parametrize(
    "source",
    [
        "<notnmap/>",
        "<nmaprun>",
        '<nmaprun><host><ports><port portid="not-a-port" /></ports></host></nmaprun>',
        '<!DOCTYPE x [<!ENTITY y "z">]><nmaprun>&y;</nmaprun>',
    ],
)
def test_nmap_parser_rejects_non_nmap_or_active_xml(source):
    with pytest.raises(ToolOutputParseError):
        parse_nmap_xml(source)


def test_nikto_parser_supports_object_or_record_array():
    assert parse_nikto('[{"id":"1"}]') == {"vulnerabilities": [{"id": "1"}]}
    payload = {"host": "example", "vulnerabilities": []}
    assert parse_nikto(json.dumps(payload)) == {
        "vulnerabilities": payload["vulnerabilities"]
    }
    with pytest.raises(ToolOutputParseError):
        parse_nikto("null")
    with pytest.raises(ToolOutputParseError):
        parse_nikto('{"vulnerabilities":[1]}')


def test_parser_input_is_bounded(monkeypatch):
    monkeypatch.setattr("nebula.v3.toolparsers.MAX_PARSER_INPUT_BYTES", 3)
    with pytest.raises(ToolOutputParseError, match="limit"):
        parse_json('{"long":true}')
    assert MAX_PARSER_INPUT_BYTES == 100_000_000


def test_parser_container_contract_is_digest_pinned_offline_and_schema_checked():
    contract = ParserContainerContract(
        image=f"example.invalid/parsers/custom@sha256:{DIGEST}",
        executable="/parser/bin/parse",
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    assert contract.argv() == [
        "/parser/bin/parse",
        "--protocol",
        "nebula.parser/v1",
        "--input",
        "/workspace/tool-output",
        "--output",
        "-",
    ]
    assert contract.validate_result({"count": 1}) == {"count": 1}
    with pytest.raises(ToolOutputParseError, match="schema"):
        contract.validate_result({"count": "one"})
    with pytest.raises(ToolOutputParseError, match="one JSON object"):
        contract.validate_result([])
    with pytest.raises(ValidationError):
        ParserContainerContract(
            image="example.invalid/parsers/custom:latest",
            executable="../parse",
            output_schema={"type": "array"},
        )
    with pytest.raises(ValidationError, match="shell interpreters"):
        ParserContainerContract(
            image=f"example.invalid/parsers/custom@sha256:{DIGEST}",
            executable="/bin/sh",
            output_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
    with pytest.raises(ValidationError, match="additionalProperties"):
        ParserContainerContract(
            image=f"example.invalid/parsers/custom@sha256:{DIGEST}",
            executable="/parser/bin/parse",
            output_schema={"type": "object"},
        )


def test_sandbox_parser_executor_uses_private_read_only_offline_workspace(tmp_path):
    from nebula.v3.domain import utc_now
    from nebula.v3.sandbox import (
        SandboxExecutionKind,
        SandboxNetwork,
        SandboxResult,
        SandboxRunner,
        SandboxWorkspaceAccess,
    )

    class ParserRunner(SandboxRunner):
        request = None
        input_mode = None

        async def available(self):
            return True, "ready"

        async def run(self, request):
            self.request = request
            input_file = request.workspace / "tool-output"
            assert input_file.read_bytes() == b"raw scanner output"
            self.input_mode = stat.S_IMODE(input_file.stat().st_mode)
            now = utc_now()
            return SandboxResult(
                command=request.command,
                image=request.image,
                runtime="capture",
                started_at=now,
                completed_at=now,
                duration_seconds=0,
                exit_code=0,
                stdout='{ "count": 1 }',
                stderr="",
            )

    runner = ParserRunner()
    contract = ParserContainerContract(
        image=f"example.invalid/parsers/custom@sha256:{DIGEST}",
        executable="/parser/bin/parse",
        output_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    parser_root = tmp_path / "private-parsers"
    result = asyncio.run(
        SandboxParserExecutor(runner=runner, parser_root=parser_root).parse(
            contract, b"raw scanner output"
        )
    )

    assert result == {"count": 1}
    assert runner.input_mode == 0o600
    assert runner.request.execution_kind == SandboxExecutionKind.PARSER
    assert runner.request.network == SandboxNetwork.NONE
    assert runner.request.workspace_access == SandboxWorkspaceAccess.READ
    assert list(parser_root.iterdir()) == []
