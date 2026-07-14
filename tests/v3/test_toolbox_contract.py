import asyncio
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from nebula.v3.agent_tooling import BrokeredToolSpecialist, ToolMissionSupervisor
from nebula.v3.domain import RiskClass, RunBudget, ScopePolicy
from nebula.v3.orchestration import (
    MissionPlan,
    PlannedTask,
    SpecialistContext,
    SpecialistResult,
    SpecialistRole,
)
from nebula.v3.providers import (
    ModelCapabilities,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    ProviderFlavor,
    ProviderHealth,
    ProviderKind,
    ToolCall,
)
from nebula.v3.tool_interfaces import load_interface_catalog
from nebula.v3.tools import ToolExecutionResult, ToolSpec


TOOLBOX = (
    Path(__file__).parents[2] / "src/nebula/v3/tool_pack_assets/toolbox/environment"
)


def load_script(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TOOLBOX / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_catalog() -> tuple[dict[str, str], list[dict[str, object]]]:
    versions = load_script(
        "nebula_toolbox_catalog_builder", "build-tool-catalog.py"
    )._load_versions(TOOLBOX / "tool-versions.env")
    sources = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted((TOOLBOX / "interfaces").glob("*.yaml"))
    ]
    return versions, sources


def test_nmap_exact_version_switch_overrides_are_value_free():
    _, sources = source_catalog()
    nmap = next(item for item in sources if item["name"] == "nmap")

    switches = {
        "n",
        "r__upper_r",
        "sa",
        "sm",
        "ss",
        "st",
        "sw",
    }
    assert set(nmap["option_overrides"]) >= switches
    assert all(
        nmap["option_overrides"][identifier] == {"value": None}
        for identifier in switches
    )


def test_toolbox_install_smoke_tests_allow_cold_desktop_start():
    manifest = yaml.safe_load(
        (TOOLBOX / "nebula-tool-pack.yaml").read_text(encoding="utf-8")
    )

    assert manifest["tools"]
    assert all(
        smoke["timeout_seconds"] >= 120
        for tool in manifest["tools"]
        for smoke in tool["smoke_tests"]
    )
    network_tools = {
        tool["name"]: tool
        for tool in manifest["tools"]
        if tool["name"] in {"environment.run_network", "environment.run_invasive"}
    }
    assert set(network_tools) == {
        "environment.run_network",
        "environment.run_invasive",
    }
    for tool in network_tools.values():
        options = tool["smoke_tests"][0]["arguments"]["invocation"]["options"]
        port = next(option["value"] for option in options if option["id"] == "p")
        assert port == 1
        assert isinstance(port, int)

        value_schema = tool["input_schema"]["properties"]["invocation"]["properties"][
            "options"
        ]["items"]["properties"]["value"]
        assert "oneOf" not in value_schema
        assert {branch["type"] for branch in value_schema["anyOf"]} >= {
            "integer",
            "number",
        }


def _option(identifier: str, flag: str, *, value: str | None = None):
    return {
        "id": identifier,
        "flags": [flag],
        "usage": flag if value is None else f"{flag} {value}",
        "description": "Exact test interface.",
        "section": "options",
        "value": None
        if value is None
        else {
            "name": value,
            "type": "string",
            "required": True,
            "style": "separate",
        },
        "repeatable": False,
        "conflicts_with": [],
        "requires": [],
        "implies": [],
    }


def write_test_catalog(path: Path) -> None:
    versions, sources = source_catalog()
    resolved = []
    for source in sources:
        positionals = source["positionals"]
        options = [_option("version", "--version")]
        if source["name"] == "nmap":
            options = [
                _option("st", "-sT"),
                _option("n", "-n"),
                _option("pn", "-Pn"),
                _option("p", "-p", value="PORTS"),
            ]
        item = {
            "protocol": "nebula.toolbox.interface/v2",
            "name": source["name"],
            "version": versions[source["version_key"]],
            "executable": source["executable"],
            "aliases": [],
            "category": source["category"],
            "risk_class": source["risk_class"],
            "description": source["description"],
            "homepage": source["homepage"],
            "synopsis": source["synopsis"],
            "examples": source["examples"],
            "notes": source["notes"],
            "commands": [
                {
                    "path": [],
                    "synopsis": source["synopsis"],
                    "positionals": positionals,
                    "options": options,
                    "help_documents": [
                        {
                            "command_path": [],
                            "argv": source["documentation"][0]["argv"],
                            "exit_code": 0,
                            "sha256": "a" * 64,
                            "text": "--version",
                        }
                    ],
                }
            ],
            "coverage": {
                "help_documents": 1,
                "documented_options": len(options),
                "structured_options": len(options),
                "unmapped_options": [],
                "complete": True,
            },
        }
        if package_key := source.get("package_version_key"):
            item["package_version"] = versions[package_key]
        resolved.append(item)
    payload = {
        "protocol": "nebula.toolbox.catalog/v2",
        "interface_protocol": "nebula.toolbox.interface/v2",
        "toolbox_version": versions["TOOLBOX_VERSION"],
        "tools": resolved,
        "inventory": [
            {
                "name": "custom-tool",
                "path": "/usr/local/bin/custom-tool",
                "catalogued": False,
                "interface": None,
                "aliases": [],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_toolbox_catalog_has_pinned_unique_absolute_executables(tmp_path):
    toolbox = load_script("nebula_toolbox_contract", "nebula-toolbox.py")
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    toolbox.CATALOG_PATH = catalog_path

    index = toolbox._load_catalog()

    assert len(index) == 27
    assert set(index) >= {"ncat", "nmap", "nuclei", "semgrep", "sqlmap"}
    assert index["nmap"]["version"] == "7.99"
    assert all(Path(item["executable"]).is_absolute() for item in index.values())


def test_publication_build_uses_native_architecture_runners():
    workflow = (
        Path(__file__).parents[2] / ".github/workflows/toolbox-publication.yml"
    ).read_text(encoding="utf-8")

    prepare = workflow.split("  prepare:\n", 1)[1].split("\n  build:\n", 1)[0]
    build = workflow.split("\n  build:\n", 1)[1].split("\n  catalog:\n", 1)[0]

    assert "runs-on: ubuntu-22.04" in prepare
    assert "matrix.runner" not in prepare
    assert "runs-on: ${{ matrix.runner }}" in build
    assert "runner: ubuntu-22.04-arm" in build
    assert "docker/setup-qemu-action" not in build


def test_publication_compares_architecture_independent_catalog_contract():
    workflow = (
        Path(__file__).parents[2] / ".github/workflows/toolbox-publication.yml"
    ).read_text(encoding="utf-8")
    catalog = workflow.split("\n  catalog:\n", 1)[1].split("\n  deploy:\n", 1)[0]

    assert "scripts.compare_toolbox_interface_catalogs" in catalog
    assert "cmp release-input/toolbox-amd64.model-catalog.json" not in catalog


def test_toolbox_sources_are_one_reviewed_v2_interface_per_first_class_tool():
    versions, tools = source_catalog()

    names = [tool["name"] for tool in tools]
    assert len(names) == 27
    assert len(names) == len(set(names))
    assert all(
        tool["protocol"] == "nebula.toolbox.interface-source/v2" for tool in tools
    )
    assert all(tool["version_key"] in versions for tool in tools)
    assert all(tool["version_probe"][0] == tool["executable"] for tool in tools)
    assert all(tool["documentation"] for tool in tools)
    assert all(
        tool["synopsis"] and tool["examples"] and tool["notes"] for tool in tools
    )


def test_catalog_builder_canonicalizes_repeated_help_enum_values():
    builder = load_script("nebula_toolbox_enum_builder", "build-tool-catalog.py")
    option = _option("method", "--method", value="METHOD")
    option["usage"] = "--method <HEAD|POST|HEAD|POST>"

    builder._enrich_option_semantics([option])

    assert option["value"]["enum"] == ["HEAD", "POST"]


def test_catalog_builder_deduplicates_inventory_aliases(tmp_path, monkeypatch):
    builder = load_script("nebula_toolbox_inventory_builder", "build-tool-catalog.py")
    binary = tmp_path / "binary"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "alias").symlink_to(binary)
    (second / "alias").symlink_to(binary)
    monkeypatch.setattr(builder, "TRUSTED_PATHS", (str(first), str(second)))

    inventory = builder._inventory([])

    assert inventory == [
        {
            "name": "alias",
            "path": str(first / "alias"),
            "catalogued": False,
            "interface": None,
            "aliases": [],
        }
    ]


def test_catalog_builder_uses_reviewed_synopsis_for_empty_usage_header():
    builder = load_script("nebula_toolbox_synopsis_builder", "build-tool-catalog.py")

    synopsis = builder._exact_synopsis(
        [{"text": "Usage:\n\nOptions:\n  -h  help"}],
        "socat [options] address address",
    )

    assert synopsis == "socat [options] address address"


def test_catalog_builder_linearizes_branched_inferred_positionals():
    builder = load_script("nebula_toolbox_positional_builder", "build-tool-catalog.py")

    positionals = builder._inferred_positionals(
        "tool [<optional>] <branch-required> <items>... <suffix>"
    )

    assert [item["required"] for item in positionals] == [False, False, False, False]
    assert all(not item["repeatable"] for item in positionals)


def test_catalog_builder_rebinds_choice_conflicts_after_id_disambiguation():
    builder = load_script("nebula_toolbox_conflict_builder", "build-tool-catalog.py")
    lower = _option("r__r", "-r")
    lower["usage"] = "-r"
    upper = _option("r__upper_r", "-R")
    upper["usage"] = "-n/-R"
    n_option = _option("n", "-n")
    n_option["usage"] = "-n/-R"
    options = [lower, upper, n_option]

    builder._rebind_choice_conflicts(options)

    assert lower["conflicts_with"] == []
    assert upper["conflicts_with"] == ["n"]
    assert n_option["conflicts_with"] == ["r__upper_r"]


def test_core_loader_rejects_incomplete_interface_coverage(tmp_path):
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    payload = json.loads(catalog_path.read_text())
    payload["tools"][0]["coverage"]["unmapped_options"] = ["--unknown"]

    with pytest.raises(ValueError, match="incomplete"):
        load_interface_catalog(json.dumps(payload).encode())


def test_core_loader_rejects_ambiguous_or_mutable_interfaces(tmp_path):
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    original = json.loads(catalog_path.read_text())

    payload = json.loads(json.dumps(original))
    payload["tools"][0]["version"] = "latest"
    with pytest.raises(ValueError, match="immutable exact version"):
        load_interface_catalog(json.dumps(payload).encode())

    payload = json.loads(json.dumps(original))
    payload["tools"][0]["aliases"] = ["shared-alias"]
    payload["tools"][1]["aliases"] = ["shared-alias"]
    with pytest.raises(ValueError, match="incomplete Toolbox interface"):
        load_interface_catalog(json.dumps(payload).encode())

    payload = json.loads(json.dumps(original))
    jq = next(item for item in payload["tools"] if item["name"] == "jq")
    jq["commands"][0]["positionals"][1]["id"] = "filter"
    with pytest.raises(ValueError, match="ambiguous positionals"):
        load_interface_catalog(json.dumps(payload).encode())

    payload = json.loads(json.dumps(original))
    option = payload["tools"][0]["commands"][0]["options"][0]
    option["requires"] = ["does-not-exist"]
    with pytest.raises(ValueError, match="dangling option relation"):
        load_interface_catalog(json.dumps(payload).encode())

    payload = json.loads(json.dumps(original))
    option = payload["tools"][0]["commands"][0]["options"][0]
    option["value"] = {
        "name": "VALUE",
        "type": "untrusted-type",
        "required": True,
        "style": "separate",
    }
    with pytest.raises(ValueError, match="value is invalid"):
        load_interface_catalog(json.dumps(payload).encode())

    payload = json.loads(json.dumps(original))
    payload["tools"][0]["coverage"]["help_documents"] = 99
    with pytest.raises(ValueError, match="coverage counts"):
        load_interface_catalog(json.dumps(payload).encode())


def test_toolbox_mission_groups_structured_and_shell_capabilities():
    names = (
        "environment.search",
        "environment.help",
        "environment.run_local",
        "environment.run_network",
        "environment.run_invasive",
        "environment.shell_local",
        "environment.shell_network",
    )
    specs = {
        name: ToolSpec(
            name=name,
            description=name,
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            risk_class=(
                RiskClass.EXPLOITATION
                if name.endswith("invasive")
                else RiskClass.ACTIVE_SCAN
                if name.endswith(("network", "shell_network"))
                else RiskClass.LOCAL_READ
            ),
        )
        for name in names
    }

    plan = asyncio.run(
        ToolMissionSupervisor(specs).plan(
            "Inspect the lab",
            {"tool_names": list(names), "scope_summary": "lab only"},
            RunBudget(max_tool_calls=50, max_concurrency=2),
        )
    )

    assert [task.allowed_tools for task in plan.tasks] == [
        frozenset({"environment.search"}),
        frozenset({"environment.help"}),
        frozenset(
            {
                "environment.run_local",
                "environment.run_network",
                "environment.run_invasive",
                "environment.shell_local",
                "environment.shell_network",
            }
        ),
    ]


def test_toolbox_mission_summary_is_structured_markdown_with_commands():
    task = PlannedTask(
        role=SpecialistRole.NETWORK_SERVICE,
        title="Inspect the service",
        instructions="Inspect one in-scope endpoint.",
    )
    result = SpecialistResult(
        summary="The endpoint exposed one HTTP service.",
        reproducible_steps=["nmap -sV 'example target'"],
        evidence_ids=["evidence-1"],
    )

    summary = asyncio.run(
        ToolMissionSupervisor({}).synthesize(
            "Review the lab",
            MissionPlan(summary="Review", rationale="Bounded", tasks=[task]),
            {task.id: result},
        )
    )

    assert summary.startswith("## Summary\n\nReview the lab")
    assert "### Inspect the service" in summary
    assert "```bash\nnmap -sV 'example target'\n```" in summary
    assert "**Evidence:** evidence-1" in summary


def test_toolbox_wrapper_compiles_structured_invocation(tmp_path, capsys):
    toolbox = load_script("nebula_toolbox_structured", "nebula-toolbox.py")
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    toolbox.CATALOG_PATH = catalog_path
    toolbox.WORKSPACE = tmp_path

    exit_code = toolbox.main(
        [
            "exec",
            "--max-risk",
            "workspace_write",
            "--tool",
            "jq",
            "--invocation-json",
            json.dumps(
                {
                    "command_path": [],
                    "options": [],
                    "positionals": [{"id": "filter", "value": "."}],
                }
            ),
            "--cwd",
            str(tmp_path),
            "--timeout",
            "30",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code in {0, 127}
    assert output["command"][-1] == "."
    assert output["metadata"]["catalogued_guidance"] is True


def test_toolbox_wrapper_keeps_large_pathological_output_valid_json():
    toolbox = load_script("nebula_toolbox_bounded_json", "nebula-toolbox.py")
    output = toolbox._envelope(
        "exec",
        tool="nmap",
        stdout="\x00" * toolbox.MAX_OUTPUT_BYTES,
        stderr="warning\n" * 100_000,
        metadata={
            "catalog_digest": "a" * 64,
            "catalogued_guidance": True,
            "script_sha256": None,
        },
    )

    serialized = toolbox._serialized_output(output)
    decoded = json.loads(serialized)

    assert len(serialized.encode("utf-8")) <= toolbox.MAX_ENVELOPE_BYTES
    assert decoded["protocol"] == "nebula.toolbox/v1"
    assert decoded["tool"] == "nmap"
    assert "output truncated to preserve JSON envelope" in decoded["stderr"]


def test_toolbox_wrapper_rejects_invented_structured_option(tmp_path, capsys):
    toolbox = load_script("nebula_toolbox_unknown_option", "nebula-toolbox.py")
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    toolbox.CATALOG_PATH = catalog_path
    toolbox.WORKSPACE = tmp_path

    exit_code = toolbox.main(
        [
            "exec",
            "--max-risk",
            "workspace_write",
            "--tool",
            "jq",
            "--invocation-json",
            '{"command_path":[],"options":[{"id":"invented","value":null}],"positionals":[{"id":"filter","value":"."}]}',
            "--cwd",
            str(tmp_path),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert "unknown option" in output["stderr"]


def test_specialist_injects_selected_exact_interface_before_execution(tmp_path):
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    catalog = load_interface_catalog(catalog_path.read_bytes())

    class QueueProvider(ModelProvider):
        def __init__(self):
            super().__init__(
                ProviderConfig(
                    id="queue",
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    flavor=ProviderFlavor.VLLM,
                    base_url="http://127.0.0.1:8000/v1",
                    local=True,
                    enabled=True,
                    capabilities=ModelCapabilities(tools=True, strict_tools=True),
                )
            )
            self.requests: list[ModelRequest] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.requests.append(request)
            if len(self.requests) == 1:
                call = ToolCall(
                    id="select",
                    name="nebula.select_environment_command",
                    arguments={
                        "mode": "structured",
                        "tool": "jq",
                        "command_path": [],
                        "rationale": "Use the exact jq interface.",
                    },
                )
                return ModelResponse(
                    provider_id="queue", model="test", tool_calls=[call]
                )
            if len(self.requests) == 2:
                call = ToolCall(
                    id="execute",
                    name="environment.run_local",
                    arguments={
                        "tool": "jq",
                        "invocation": {
                            "command_path": [],
                            "options": [],
                            "positionals": [{"id": "filter", "value": "."}],
                        },
                        "cwd": "/tmp/provider-selected-path",
                    },
                )
                return ModelResponse(
                    provider_id="queue", model="test", tool_calls=[call]
                )
            return ModelResponse(provider_id="queue", model="test", text="jq completed")

        async def health(self) -> ProviderHealth:
            return ProviderHealth(provider_id="queue", healthy=True)

    class Broker:
        async def execute(self, invocation, scope):
            del scope
            assert invocation.tool_name == "environment.run_local"
            assert invocation.arguments["cwd"] == "."
            return ToolExecutionResult(
                output={"ok": True},
                exit_code=0,
                execution={"command": ["/usr/bin/jq", "."]},
            )

    spec = ToolSpec(
        name="environment.run_local",
        description="Run a local structured command.",
        input_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "invocation": {"type": "object"},
                "cwd": {"type": "string"},
            },
            "required": ["tool", "invocation", "cwd"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.WORKSPACE_WRITE,
        path_arguments=["cwd"],
    )
    provider = QueueProvider()
    specialist = BrokeredToolSpecialist(
        provider,
        role=SpecialistRole.NETWORK_SERVICE,
        broker=Broker(),
        scope=ScopePolicy(engagement_id="engagement"),
        workspace=tmp_path,
        specs={spec.name: spec},
        interface_catalog=catalog,
    )
    task = PlannedTask(
        role=SpecialistRole.NETWORK_SERVICE,
        title="Transform JSON",
        instructions="Pretty-print the assigned JSON.",
        allowed_tools=frozenset({spec.name}),
    )

    result = asyncio.run(
        specialist.run(
            SpecialistContext(
                engagement_id="engagement",
                run_id="run",
                task=task,
                objective="Transform evidence",
                prior_results={},
                allowed_tools=frozenset({spec.name}),
            )
        )
    )

    assert result.tool_calls == 1
    assert len(provider.requests) == 3
    assert "do not invent option IDs" in (provider.requests[1].instructions or "")
    assert '"name": "jq"' in (provider.requests[1].instructions or "")
    assert [tool.name for tool in provider.requests[1].tools] == [
        "environment.run_local"
    ]
    assert provider.requests[1].tools[0].input_schema["properties"]["cwd"] == {
        "type": "string",
        "const": ".",
        "description": "Engagement workspace root; supplied by Nebula Core.",
    }
    assert spec.input_schema["properties"]["cwd"] == {"type": "string"}


def test_specialist_corrects_single_catalogued_help_path(tmp_path):
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    catalog = load_interface_catalog(catalog_path.read_bytes())

    class HelpProvider(ModelProvider):
        def __init__(self):
            super().__init__(
                ProviderConfig(
                    id="help",
                    kind=ProviderKind.OPENAI_COMPATIBLE,
                    flavor=ProviderFlavor.VLLM,
                    base_url="http://127.0.0.1:8000/v1",
                    local=True,
                    enabled=True,
                    capabilities=ModelCapabilities(tools=True, strict_tools=True),
                )
            )
            self.calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    provider_id="help",
                    model="test",
                    tool_calls=[
                        ToolCall(
                            id="help-jq",
                            name="environment.help",
                            arguments={"tool": "jq", "command_path": ["jq"]},
                        )
                    ],
                )
            return ModelResponse(provider_id="help", model="test", text="jq help read")

        async def health(self) -> ProviderHealth:
            return ProviderHealth(provider_id="help", healthy=True)

    class Broker:
        async def execute(self, invocation, scope):
            del scope
            assert invocation.arguments == {"tool": "jq", "command_path": []}
            return ToolExecutionResult(
                output={"catalogued_guidance": True},
                exit_code=0,
                execution={"command": ["nebula-toolbox", "help"]},
            )

    spec = ToolSpec(
        name="environment.help",
        description="Read exact-version help.",
        input_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string"},
                "command_path": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["tool", "command_path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object", "additionalProperties": True},
        risk_class=RiskClass.LOCAL_READ,
    )
    specialist = BrokeredToolSpecialist(
        HelpProvider(),
        role=SpecialistRole.NETWORK_SERVICE,
        broker=Broker(),
        scope=ScopePolicy(engagement_id="engagement"),
        workspace=tmp_path,
        specs={spec.name: spec},
        interface_catalog=catalog,
    )
    task = PlannedTask(
        role=SpecialistRole.NETWORK_SERVICE,
        title="Read jq help",
        instructions="Inspect jq's exact interface.",
        allowed_tools=frozenset({spec.name}),
    )

    result = asyncio.run(
        specialist.run(
            SpecialistContext(
                engagement_id="engagement",
                run_id="run",
                task=task,
                objective="Read jq help",
                prior_results={},
                allowed_tools=frozenset({spec.name}),
            )
        )
    )

    assert result.tool_calls == 1


def test_toolbox_shell_supports_full_pipeline_and_records_script_hash(tmp_path, capsys):
    toolbox = load_script("nebula_toolbox_shell", "nebula-toolbox.py")
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    toolbox.CATALOG_PATH = catalog_path
    toolbox.WORKSPACE = tmp_path

    exit_code = toolbox.main(
        [
            "shell",
            "--script",
            "printf 'one\\ntwo\\n' | wc -l",
            "--cwd",
            str(tmp_path),
            "--timeout",
            "30",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["stdout"].strip() == "2"
    assert output["metadata"]["catalogued_guidance"] is False
    assert len(output["metadata"]["script_sha256"]) == 64


def test_toolbox_wrapper_rejects_workspace_escape(tmp_path, capsys):
    toolbox = load_script("nebula_toolbox_workspace", "nebula-toolbox.py")
    catalog_path = tmp_path / "tool-catalog.json"
    write_test_catalog(catalog_path)
    toolbox.CATALOG_PATH = catalog_path
    toolbox.WORKSPACE = tmp_path

    exit_code = toolbox.main(
        [
            "shell",
            "--script",
            "true",
            "--cwd",
            str(tmp_path.parent),
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert "inside /workspace" in output["stderr"]


@pytest.mark.parametrize(
    ("value", "address", "port"),
    [
        ("tcp://192.0.2.10:443", "192.0.2.10", 443),
        ("tcp://[2001:db8::10]:8443", "2001:db8::10", 8443),
    ],
)
def test_egress_helper_accepts_only_literal_tcp_rules(value, address, port):
    egress = load_script("nebula_egress_valid", "nebula-egress.py")

    parsed_address, parsed_port = egress._rule(value)

    assert str(parsed_address) == address
    assert parsed_port == port


@pytest.mark.parametrize(
    "value",
    [
        "udp://192.0.2.10:53",
        "tcp://example.test:443",
        "https://192.0.2.10:443",
        "tcp://192.0.2.10:0",
    ],
)
def test_egress_helper_rejects_unpinned_or_non_tcp_rules(value):
    egress = load_script("nebula_egress_invalid", "nebula-egress.py")

    with pytest.raises((ValueError, TypeError)):
        egress._rule(value)
