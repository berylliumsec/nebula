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


def test_live_page_context_is_bounded_and_excludes_secrets():
    source = (ROOT / "ui/src-tauri/src/browser.rs").read_text(encoding="utf-8")

    script = source.split('const BROWSER_CONTEXT_SCRIPT: &str = r#"', 1)[1].split(
        '"#;', 1
    )[0]
    assert "MAX_CAPTURE_TEXT_CHARS: usize = 16_000" in source
    assert "MAX_CAPTURE_SELECTION_CHARS: usize = 4_000" in source
    assert "MAX_CAPTURE_RAW_BYTES: usize = 2 * 1024 * 1024" in source
    assert "raw.len() > MAX_CAPTURE_RAW_BYTES" in source
    assert "browser_capture_context" in source
    assert "document.body?.innerText" in script
    assert "document.forms" in script
    assert "document.links" in script
    assert ".value" not in script
    assert "document.cookie" not in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
