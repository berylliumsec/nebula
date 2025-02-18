import os
import sys

from .log_config import setup_logging

HOME_DIR = os.path.join(os.environ["HOME"], ".local", "share", "nebula")
SYSTEM_LOGS_DIR = os.path.join(HOME_DIR, "logs")
logger = setup_logging(log_file=SYSTEM_LOGS_DIR + "/constants.log")


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        if getattr(sys, "frozen", False):
            base_path = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        else:
            base_path = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_path, relative_path)
    except Exception as e:
        logger.debug(f"An Error occured: {e}")


def is_run_as_package():
    if os.environ.get("IN_DOCKER"):
        return False
    return "site-packages" in os.path.abspath(__file__)


def return_path(path):
    try:
        if is_run_as_package():
            return path
        else:
            return resource_path(path)

    except Exception as e:
        logger.debug(f"An Error occured: {e}")


INITIAL_HELP = os.path.join(
    os.environ["HOME"], ".local", "share", "nebula", "initial_help"
)

CUSTOM_PROMPT_PATTERN = (
    r"(nebula(?: (?:\$\s|%~%#|~\$)|)|[\w-]+@[\w-]+:[\w/~]+[#$]>|\$\s|>\s|#\s|"
    r"\d{2}:\d{2}:\d{2} [\w.-]+@[\w.-]+ \w+ [±\|][\w_ ]+ [✗✔︎]?[\|→]\s)"
)

NEBULA_DIR = os.path.join(os.environ["HOME"], ".local", "share", "nebula")
