"""Compatibility import for the execution-owned Core factory.

Replaceable public workers use ``api_worker``. Legacy launchers and integrations
retain their import and monkeypatch identity during the coordinated migration.
"""

import sys

from . import execution_kernel

sys.modules[__name__] = execution_kernel
