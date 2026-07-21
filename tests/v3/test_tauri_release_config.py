import json

import pytest

from scripts.tauri_release_config import ReleaseConfigError, updater_config


def test_updater_config_contains_only_public_release_settings():
    config = updater_config(
        "public-key",
        "https://berylliumsec.github.io/nebula/updates/prerelease/latest.json",
    )

    assert config == {
        "plugins": {
            "updater": {
                "pubkey": "public-key",
                "endpoints": [
                    "https://berylliumsec.github.io/nebula/updates/prerelease/latest.json"
                ],
            }
        }
    }
    assert "private" not in json.dumps(config).lower()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://updates.example.test/latest.json",
        "https://user:secret@updates.example.test/latest.json",
        "https://updates.example.test/latest.json?channel=stable",
        "",
    ],
)
def test_updater_config_rejects_unsafe_endpoints(endpoint):
    with pytest.raises(ReleaseConfigError):
        updater_config("public-key", endpoint)


def test_updater_config_requires_a_public_key():
    with pytest.raises(ReleaseConfigError):
        updater_config("", "https://updates.example.test/latest.json")
