"""Poetry build hooks that include the current generated product index."""

from __future__ import annotations

import runpy
from pathlib import Path

from poetry.core.masonry import api


def _prepare_index() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_product_knowledge.py"
    )
    runpy.run_path(str(script))["ensure"]()


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _prepare_index()
    return api.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    _prepare_index()
    return api.build_sdist(sdist_directory, config_settings)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _prepare_index()
    return api.build_editable(wheel_directory, config_settings, metadata_directory)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return api.prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def get_requires_for_build_wheel(config_settings=None):
    return api.get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return api.get_requires_for_build_sdist(config_settings)


get_requires_for_build_editable = get_requires_for_build_wheel
prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel
