import logging
import os
from importlib.resources import as_file, files

from . import constants

logger = logging.getLogger(constants.SYSTEM_LOGS_DIR + "/update_utils.log")


def resource_path(relative_path):
    """
    Get the absolute path to a packaged resource.

    The parameter `relative_path` should be the path to the resource relative to
    the root of the package. For example, for a file in the package directory
    `config/dark-stylesheet.css`, pass "config/dark-stylesheet.css".

    Package/resource errors deliberately propagate. Nebula is distributed as one
    application, so a missing resource indicates a broken build rather than an
    alternate installation layout to probe.
    """
    resource = files("nebula").joinpath(relative_path)
    with as_file(resource) as packaged_path:
        return str(packaged_path)


def is_run_as_package():
    if os.environ.get("IN_DOCKER"):
        return False
    return "site-packages" in os.path.abspath(__file__)


def return_path(path):

    return resource_path(path)
