import logging
import os
import sys

from . import constants

logger = logging.getLogger(constants.SYSTEM_LOGS_DIR + "/update_utils.log")


def resource_path(relative_path):
    """
    Get the absolute path to a resource.

    First, try to load the resource from the installed package using
    importlib.resources (available in Python 3.9+). This is the recommended
    method for accessing package data that was included via pyproject.toml.

    If that fails (for example, in development mode when the package isn’t installed),
    fall back to using a file-system–relative path based on __file__.

    The parameter `relative_path` should be the path to the resource relative to
    the root of the package. For example, for a file in the package directory
    `config/dark-stylesheet.css`, pass "config/dark-stylesheet.css".
    """
    try:
        # Try using the modern importlib.resources API (Python 3.9+)
        from importlib.resources import as_file, files

        # Assume your resources are packaged under the "nebula" package.
        resource = files("nebula").joinpath(relative_path)
        # as_file returns a context manager that gives a filesystem path.
        with as_file(resource) as resource_path:
            return str(resource_path)
    except Exception:
        # Fallback for development or if importlib.resources isn't working:
        if getattr(sys, "frozen", False):
            # If running as a bundled executable (e.g. with PyInstaller)
            base_path = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        else:
            # Use the directory of the current file as the base.
            base_path = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base_path, relative_path)


def is_run_as_package():
    if os.environ.get("IN_DOCKER"):
        return False
    return "site-packages" in os.path.abspath(__file__)


def return_path(path):

    return resource_path(path)
