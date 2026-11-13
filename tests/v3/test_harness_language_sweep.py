"""Keep default harness guidance independent of a particular security technique."""

import re

import pytest
from tests.v3.async_test_support import anyio_backend as anyio_backend

from nebula.v3.automation_tools import command_specs
from nebula.v3.agent_tooling import ToolMissionSupervisor
from nebula.v3.orchestration import RunBudget


FORBIDDEN_DEFAULT_GUIDANCE = re.compile(
    r"\b(?:intruder|metasploit|sqlmap|nmap|nikto|searchsploit|shellcode|"
    r"reverse shell|sql injection|password spraying|credential stuffing)\b",
    re.IGNORECASE,
)


def test_default_command_guidance_does_not_recommend_attack_tools():
    specs = command_specs()
    assert not FORBIDDEN_DEFAULT_GUIDANCE.search(specs["run_command"].description)
    assert "command" in specs["run_command"].input_schema["properties"]
    assert "process_io" in specs


@pytest.mark.anyio
async def test_generic_mission_preserves_operator_objective_without_attack_stages():
    objective = "Summarize the supplied project data."
    plan = await ToolMissionSupervisor(command_specs()).plan(
        objective, {"tool_names": ["run_command"]}, RunBudget()
    )
    assert all(objective in task.instructions for task in plan.tasks)
    assert not FORBIDDEN_DEFAULT_GUIDANCE.search(plan.model_dump_json())
