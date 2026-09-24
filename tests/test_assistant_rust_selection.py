import copy
from pathlib import Path

import pytest

from scripts.run_assistant_rust_tests import cargo_command
from scripts.test_selection import assistant_rust_target, validate

VALID = "nebula-assistant-storage/journal::committed_events_survive_reopen_and_idempotent_retries"


def plan():
    return {
        "change_digest": "current",
        "reviewed_by": "focused policy test",
        "reason": "Assistant Rust selection validation",
        "exclusions": "No product runtime tests",
        "python": [],
        "frontend": [],
        "native": [],
        "playwright": [],
        "assistant_rust": [VALID],
        "expected_tests": {
            "python": 0,
            "frontend": 0,
            "native": 0,
            "playwright": 0,
            "assistant_rust": 1,
        },
    }


def test_exact_assistant_selection_is_separate_from_native():
    receipt = validate(plan(), "current")
    assert receipt["native"] == []
    assert assistant_rust_target(VALID) == (
        "nebula-assistant-storage",
        "journal",
        "committed_events_survive_reopen_and_idempotent_retries",
    )
    assert assistant_rust_target(
        "nebula-assistant-services/context::python_service_oracle_matches_saved_context_and_cursor_transitions"
    ) == (
        "nebula-assistant-services",
        "context",
        "python_service_oracle_matches_saved_context_and_cursor_transitions",
    )
    assert assistant_rust_target(
        "nebula-assistant-transport/http::python_authentication_oracle_matches_http_status_headers_and_bodies"
    ) == (
        "nebula-assistant-transport", "http",
        "python_authentication_oracle_matches_http_status_headers_and_bodies",
    )
    assert assistant_rust_target(
        "nebula-assistant-integrations/lib::protocol::exact_case"
    ) == ("nebula-assistant-integrations", "lib", "protocol::exact_case")


@pytest.mark.parametrize(
    "selection",
    [
        "--workspace",
        "*",
        "nebula-ui/lib::test",
        "nebula-assistant-storage/../journal::test",
        "nebula-assistant-storage/journal::*",
        "nebula-assistant-storage/journal::--ignored",
        "nebula-assistant-storage/missing::test",
        "nebula-assistant-storage/journal::test --skip other",
        "nebula-assistant-integrations/../protocol::test",
        "nebula-assistant-integrations/lib::*",
    ],
)
def test_broad_missing_or_injected_selection_is_rejected(selection):
    receipt = plan()
    receipt["assistant_rust"] = [selection]
    with pytest.raises(ValueError):
        validate(receipt, "current")


@pytest.mark.parametrize(
    "selections,count",
    [([VALID, VALID], 2), ([VALID], 0), ([VALID], True), ([], 1), (None, 0)],
)
def test_duplicate_or_inaccurate_counts_are_rejected(selections, count):
    receipt = plan()
    receipt["assistant_rust"] = selections
    receipt["expected_tests"]["assistant_rust"] = count
    with pytest.raises(ValueError):
        validate(receipt, "current")


def test_legacy_receipt_does_not_start_assistant_tests():
    receipt = plan()
    del receipt["assistant_rust"]
    del receipt["expected_tests"]["assistant_rust"]
    assert validate(copy.deepcopy(receipt), "current") == receipt


def test_runner_pins_toolchain_and_exact_nonignored_target():
    command = cargo_command(VALID, "1.94.0", listing=True)
    assert command == [
        "cargo",
        "+1.94.0",
        "test",
        "--locked",
        "--manifest-path",
        "assistant-rs/Cargo.toml",
        "-p",
        "nebula-assistant-storage",
        "--test",
        "journal",
        "committed_events_survive_reopen_and_idempotent_retries",
        "--",
        "--exact",
        "--list",
    ]


def test_private_library_selection_requires_one_exact_test():
    name = "entities::conversations::tests::default_home_expansion_preserves_python_workspace_root_boundary"
    selection = f"nebula-assistant-storage/lib::{name}"
    receipt = plan()
    receipt["assistant_rust"] = [selection]
    assert validate(receipt, "current")["assistant_rust"] == [selection]
    assert assistant_rust_target(selection) == (
        "nebula-assistant-storage", "lib", name,
    )
    command = cargo_command(selection, "1.94.0", listing=False)
    assert command == [
        "cargo", "+1.94.0", "test", "--locked", "--manifest-path",
        "assistant-rs/Cargo.toml", "-p", "nebula-assistant-storage",
        "--lib", name, "--", "--exact",
    ]
    assert cargo_command(selection, "1.94.0", listing=True) == [*command, "--list"]


def test_library_target_requires_library_source_not_an_integration_namesake(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    crate = tmp_path / "assistant-rs/crates/storage"
    (crate / "tests").mkdir(parents=True)
    (crate / "tests/lib.rs").touch()
    selection = "nebula-assistant-storage/lib::module::test"
    with pytest.raises(ValueError, match="target does not exist"):
        assistant_rust_target(selection)
    (crate / "src").mkdir()
    (crate / "src/lib.rs").touch()
    assert assistant_rust_target(selection) == (
        "nebula-assistant-storage", "lib", "module::test",
    )


def test_ci_runs_only_the_validated_receipt_for_assistant():
    ci = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    section = ci.split("  assistant-rust:\n")[1].split("  desktop-macos:\n")[0]
    assert "needs: selection" in section
    assert "needs.selection.outputs.assistant_rust != '[]'" in section
    assert "python -m scripts.run_assistant_rust_tests" in section
    assert "cargo test" not in section
