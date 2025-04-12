import subprocess

from langchain.tools import tool
from pydantic import BaseModel, Field


class TerminalCommandInput(BaseModel):
    command: str = Field(
        description="The terminal command to execute, e.g. 'ls -la' or 'pwd'"
    )


@tool("terminal", args_schema=TerminalCommandInput, return_direct=True)
def run_terminal_command(command: str) -> str:
    """
    Executes a terminal command on the host machine.

    Example:
        run_terminal_command "ls -la"
    """
    try:
        # Using shell=True to enable execution of full terminal commands.
        output = subprocess.check_output(
            command, shell=True, stderr=subprocess.STDOUT, text=True
        )
        return output
    except subprocess.CalledProcessError as e:
        return f"Error executing command: {e.output}"
