import ast
import sys
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from io import StringIO

from IPython.core.interactiveshell import InteractiveShell

from . import constants
from .log_config import setup_logging

logger = setup_logging(log_file=constants.SYSTEM_LOGS_DIR + "/jupyter.log")


class InteractiveScriptDetector(ast.NodeVisitor):
    def __init__(self):
        self.interactive_detected = False

    def visit_Call(self, node):
        # Check if the call is to a known interactive function
        if isinstance(node.func, ast.Name) and node.func.id in {"input", "raw_input"}:
            self.interactive_detected = True
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {
            "input",
            "raw_input",
        }:
            self.interactive_detected = True
        self.generic_visit(node)

    def visit_Expr(self, node):
        # Check for interactive shell commands and other forms of user input
        if isinstance(node.value, ast.Call):
            self.visit_Call(node.value)
        self.generic_visit(node)


def detect_interactive_script(script: str) -> bool:
    """
    Detect if the script contains interactive input calls or other forms of user interaction.
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
    Execute a full Python script in an IPython environment and handle errors.
    Returns the output or error messages.
    """
    # Log the original script
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
        # Initialize IPython environment if not already available
        ipython = InteractiveShell.instance()

        # Redirect stdout and stderr to capture output
        stdout_backup = sys.stdout
        stderr_backup = sys.stderr
        sys.stdout = StringIO()
        sys.stderr = StringIO()

        try:
            # Execute the script in the IPython environment
            result = ipython.run_cell(script)

            # Capture stdout and stderr
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
            # Restore stdout and stderr
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


def execute_script_in_thread(script: str) -> Future:
    """
    Execute a Python script in a separate thread to avoid blocking the main UI.
    Returns a Future object that can be used to retrieve the result.
    """
    with ThreadPoolExecutor() as executor:
        future = executor.submit(execute_python_script, script)
    return future


# Example usage:
# selected_text = cursor.selectedText()
# future = execute_script_in_thread(selected_text)
# output = future.result()  # This will block until the script execution is complete
