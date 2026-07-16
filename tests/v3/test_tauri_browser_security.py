import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_remote_browser_webviews_receive_no_nebula_capability():
    capability = json.loads(
        (ROOT / "ui/src-tauri/capabilities/default.json").read_text(encoding="utf-8")
    )

    assert capability["webviews"] == ["main"]
    assert "windows" not in capability
    assert "remote" not in capability
    assert capability["permissions"] == ["core:default"]


def test_browser_webviews_are_owned_by_the_native_manager():
    source = (ROOT / "ui/src-tauri/src/browser.rs").read_text(encoding="utf-8")

    assert 'format!("browser-{tab_id}")' in source
    assert 'matches!(url.scheme(), "http" | "https")' in source
    assert "MAX_TABS_PER_PROJECT: usize = 16" in source
    assert "MAX_DOWNLOAD_BYTES: u64 = 1024 * 1024 * 1024" in source
