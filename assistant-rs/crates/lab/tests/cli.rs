use serde_json::Value;
use std::process::Command;

fn binary() -> Command {
    Command::new(env!("CARGO_BIN_EXE_nebula-assistant-lab"))
}

#[test]
fn fixture_replay_is_durable_and_does_not_overwrite_existing_output() {
    let temp = tempfile::tempdir().unwrap();
    let output = temp.path().join("fixture");
    let result = binary()
        .args([
            "fixture",
            "--turns",
            "2",
            "--events-per-turn",
            "3",
            "--directory",
        ])
        .arg(&output)
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let receipt: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(receipt["events"], 6);
    assert_eq!(receipt["production_compatible"], false);
    let replay = binary()
        .args(["replay", "--database"])
        .arg(output.join("assistant-lab.db"))
        .args(["--turn", "fixture-turn-00001", "--after", "1"])
        .output()
        .unwrap();
    assert!(
        replay.status.success(),
        "{}",
        String::from_utf8_lossy(&replay.stderr)
    );
    let events: Vec<Value> = serde_json::from_slice(&replay.stdout).unwrap();
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["sequence"], 2);
    assert!(
        !binary()
            .args(["fixture", "--directory"])
            .arg(&output)
            .output()
            .unwrap()
            .status
            .success()
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&std::fs::read(output.join("receipt.json")).unwrap())
            .unwrap(),
        receipt
    );
}

#[test]
fn measurement_reports_only_verified_journal_work() {
    let temp = tempfile::tempdir().unwrap();
    let result = binary()
        .args([
            "measure",
            "--streams",
            "3",
            "--events-per-stream",
            "4",
            "--directory",
        ])
        .arg(temp.path().join("measure"))
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    let receipt: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(receipt["committed_events"], 12);
    assert_eq!(receipt["replayed_events"], 12);
    assert_eq!(receipt["assistant_performance_gates_verified"], false);
    assert_eq!(receipt["python_baseline_measured"], false);
    assert!(receipt["events_per_second"].as_f64().unwrap() > 0.0);
}

#[test]
fn laboratory_cannot_be_started_as_core_or_with_unbounded_work() {
    assert!(!binary().arg("serve").output().unwrap().status.success());
    let temp = tempfile::tempdir().unwrap();
    let output = temp.path().join("invalid");
    for count in ["0", "201"] {
        assert!(
            !binary()
                .args(["measure", "--streams", count, "--directory"])
                .arg(&output)
                .output()
                .unwrap()
                .status
                .success()
        );
    }
    assert!(!output.exists());
}
