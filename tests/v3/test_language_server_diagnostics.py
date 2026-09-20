"""Language-server diagnostics must name a registered feature or they raise."""

import re
from pathlib import Path

from nebula.v3 import language_server
from nebula.v3.diagnostics import FEATURE_FILES, _normalize_feature


def test_language_server_diagnostics_use_registered_features():
    source = Path(language_server.__file__).read_text(encoding="utf-8")
    features = set(re.findall(r'record_caught_exception\(\s*"([a-z_-]+)"', source))

    assert features
    for feature in features:
        assert _normalize_feature(feature) in FEATURE_FILES
