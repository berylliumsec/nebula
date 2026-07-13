import asyncio

import pytest

from nebula.v3.domain import RiskClass, utc_now
from nebula.v3.sandbox import SandboxExecutionKind, SandboxResult, SandboxRunner
from nebula.v3.tools import (
    InvalidToolArguments,
    SandboxCommandTool,
    ToolInvocation,
    ToolSpec,
)


class CaptureRunner(SandboxRunner):
    def __init__(self):
        self.request = None

    async def available(self):
        return True, "ready"

    async def run(self, request):
        self.request = request
        now = utc_now()
        return SandboxResult(
            command=request.command,
            image=request.image,
            runtime="capture",
            started_at=now,
            completed_at=now,
            duration_seconds=0,
            exit_code=0,
            stdout="{}",
            stderr="",
        )


def network_tool(*, port_argument="ports"):
    properties = {"target": {"type": "string"}}
    required = ["target"]
    if port_argument:
        properties[port_argument] = {
            "type": "array",
            "items": {"type": "integer"},
        }
        required.append(port_argument)
    spec = ToolSpec(
        name="sample.network",
        description="network",
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": False},
        risk_class=RiskClass.ACTIVE_SCAN,
        network_access=True,
        target_argument="target",
        port_argument=port_argument,
    )
    return SandboxCommandTool(
        spec,
        image="example.invalid/tool@sha256:" + "a" * 64,
        command_builder=lambda arguments: ["/tool", arguments["target"]],
        output_parser=lambda stdout, stderr, exit_code: {},
        network_name="legacy-must-not-authorize",
    )


def test_network_tool_emits_certified_egress_for_every_resolved_ip(tmp_path):
    plugin = network_tool()
    runner = CaptureRunner()
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-1",
        tool_name=plugin.spec.name,
        arguments={"target": "scanner.example", "ports": [443, 80, 443]},
        workspace=tmp_path,
        target="scanner.example",
        resolved_ips=["192.0.2.1", "2001:db8::1"],
    )

    asyncio.run(plugin.execute(invocation, runner))

    assert runner.request.execution_kind == SandboxExecutionKind.NETWORK_TOOL
    assert runner.request.network_name is None
    assert [(rule.address, rule.ports) for rule in runner.request.egress_rules] == [
        ("192.0.2.1", [80, 443]),
        ("2001:db8::1", [80, 443]),
    ]
    assert runner.request.pinned_hosts == {"scanner.example": "192.0.2.1"}


def test_url_target_derives_https_port_and_empty_resolution_fails_closed(tmp_path):
    plugin = network_tool(port_argument=None)
    invocation = ToolInvocation(
        engagement_id="eng-1",
        run_id="run-1",
        tool_name=plugin.spec.name,
        arguments={"target": "https://scanner.example/path"},
        workspace=tmp_path,
        target="https://scanner.example/path",
        resolved_ips=["192.0.2.1"],
    )
    runner = CaptureRunner()
    asyncio.run(plugin.execute(invocation, runner))
    assert runner.request.egress_rules[0].ports == [443]

    with pytest.raises(InvalidToolArguments, match="resolved"):
        asyncio.run(
            plugin.execute(
                invocation.model_copy(update={"resolved_ips": []}), CaptureRunner()
            )
        )
