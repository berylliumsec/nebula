import ast
import asyncio
import logging
import sys
import traceback
from io import StringIO

from IPython.core.interactiveshell import InteractiveShell
from langchain.tools import BaseTool

# Set up a basic logger.
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class InteractiveScriptDetector(ast.NodeVisitor):
    def __init__(self):
        self.interactive_detected = False

    def visit_Call(self, node):
        # Check if the call is to a known interactive function.
        if isinstance(node.func, ast.Name) and node.func.id in {"input", "raw_input"}:
            self.interactive_detected = True
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "input",
            "raw_input",
        }:
            self.interactive_detected = True
        self.generic_visit(node)

    def visit_Expr(self, node):
        # Check for interactive shell commands.
        if isinstance(node.value, ast.Call):
            self.visit_Call(node.value)
        self.generic_visit(node)


def detect_interactive_script(script: str) -> bool:
    """
    Detect if the script contains interactive input calls.
    Returns True if interactive input is detected, False otherwise.
    """
    try:
        tree = ast.parse(script)
        detector = InteractiveScriptDetector()
        detector.visit(tree)
        return detector.interactive_detected
    except SyntaxError as e:
        logger.error(f"Syntax error while parsing the script: {e}")
        return False


def execute_python_script(script: str) -> str:
    """
    Execute a Python script in an IPython environment.
    Returns the output or error messages.
    """
    logger.info("Original script:\n" + script)

    if detect_interactive_script(script):
        error_message = (
            "Interactive scripts are not supported. Please use the terminal instead."
        )
        logger.error(error_message)
        return error_message

    output = ""
    error_occurred = False

    try:
        # Initialize the IPython shell instance.
        ipython = InteractiveShell.instance()

        # Redirect stdout and stderr.
        stdout_backup = sys.stdout
        stderr_backup = sys.stderr
        sys.stdout = StringIO()
        sys.stderr = StringIO()

        try:
            result = ipython.run_cell(script)
            output = sys.stdout.getvalue()
            error_output = sys.stderr.getvalue()

            if result.error_in_exec:
                error_occurred = True
                error_message = f"Error: {str(result.error_in_exec)}"
                logger.error(error_message)
                output += error_output
            else:
                logger.info("Execution result: " + output)
        finally:
            # Restore stdout and stderr.
            sys.stdout = stdout_backup
            sys.stderr = stderr_backup
    except Exception as e:
        logger.exception("An error occurred while executing the script:")
        error_message = f"An exception occurred: {str(e)}\n{traceback.format_exc()}"
        output += error_message
        error_occurred = True

    if error_occurred:
        logger.info("Execution completed with errors.")
    else:
        logger.info("Execution completed successfully.")

    return output


class PythonScriptTool(BaseTool):
    name = "python_script_tool"
    description = (
        "Executes a Python script using an IPython environment and returns its output. "
        "Note: Interactive scripts (using input/raw_input) are not supported."
    )

    def _run(self, tool_input: str) -> str:
        """Synchronous execution of the Python script."""
        return execute_python_script(tool_input)

    async def _arun(self, tool_input: str) -> str:
        """Asynchronous execution of the Python script."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, execute_python_script, tool_input)


# Example usage:
# from langchain.agents import initialize_agent, AgentType
# tool = PythonScriptTool()
# agent = initialize_agent([tool], llm, agent=AgentType.ZERO_SHOT_REACT_DESCRIPTION)
# result = agent.run("print('Hello, world!')")
