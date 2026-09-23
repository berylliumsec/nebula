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


def test_ci_runs_only_the_validated_receipt_for_assistant():
    ci = (Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text()
    section = ci.split("  assistant-rust:\n")[1].split("  desktop-macos:\n")[0]
    assert "needs: selection" in section
    assert "needs.selection.outputs.assistant_rust != '[]'" in section
    assert "python -m scripts.run_assistant_rust_tests" in section
    assert "cargo test" not in section
