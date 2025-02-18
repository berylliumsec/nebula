import json
import re
import subprocess
from typing import Type

from langchain.tools import BaseTool
from pydantic import BaseModel, Field


def extract_json_block(text: str) -> str:
    """
    If the string contains a markdown-fenced JSON block, extract just the JSON content.
    """
    match = re.search(r"```json\s*(\{.*\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    return text


def robust_json_loads(text: str) -> dict:
    """
    Attempt to load JSON from text. If it fails, try to extract a JSON block.
    If that also fails, return an error dictionary.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        extracted = extract_json_block(text)
        try:
            return json.loads(extracted)
        except json.JSONDecodeError as e:
            return {
                "error": "Failed to parse JSON",
                "original_text": text,
                "error_detail": str(e),
            }


class SubprocessInput(BaseModel):
    command: str = Field(
        ..., description="The full command string to execute (e.g. 'echo hello world')"
    )


class SubprocessTool(BaseTool):
    name: str = "subprocess_execute"
    description: str = (
        "Executes any shell command and returns the output as valid JSON with robust error checks. "
        "The output is fenced with markdown triple backticks and labeled as JSON."
    )
    args_schema: Type[BaseModel] = SubprocessInput

    def _run(self, command: str) -> str:
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            if result.returncode != 0:
                output_dict = {"error": result.stderr.strip()}
            else:
                output_dict = {"result": result.stdout.strip()}
            # Convert the output to a JSON string
            json_output = json.dumps(output_dict)
            # Return it in a markdown code fence (ensuring downstream parsers can easily extract it)
            return f"```json\n{json_output}\n```"
        except Exception as e:
            fallback = {"error": f"Exception occurred: {e}"}
            return f"```json\n{json.dumps(fallback)}\n```"

    async def _arun(self, command: str) -> str:
        raise NotImplementedError(
            "Async execution is not supported for SubprocessTool."
        )
