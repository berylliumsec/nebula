use chrono::{DateTime, Utc};
use nebula_assistant_domain::auth::PairedDevice;
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str::<Value>(include_str!("../../../compatibility/python-auth.json")).unwrap()
        ["device"]
        .clone()
}
fn decode(value: &Value) -> Result<PairedDevice, impl std::fmt::Debug> {
    PairedDevice::decode(&serde_json::to_vec(value).unwrap())
}
fn time(value: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(value)
        .unwrap()
        .with_timezone(&Utc)
}

#[test]
fn device_identity_expiry_defaults_and_refresh_preserve_credentials() {
    let original = fixture();
    let device = decode(&original).unwrap();
    let now = time("2030-01-01T00:00:00Z");
    assert!(device.valid_at(now));
    assert!(!device.valid_at(time(original["idle_expires_at"].as_str().unwrap())));
    assert!(!device.refresh_due(now));
    assert!(device.refresh_due(time("2030-01-01T00:05:00Z")));

    let mut bounded = original.clone();
    bounded["absolute_expires_at"] = "2030-01-03T00:00:00Z".into();
    let refreshed = decode(&bounded).unwrap().refreshed(now).unwrap();
    assert_eq!(refreshed.revision(), 2);
    assert_eq!(
        refreshed.payload()["idle_expires_at"],
        bounded["absolute_expires_at"]
    );
    for field in [
        "id",
        "token_sha256",
        "csrf_sha256",
        "metadata",
        "capabilities",
        "created_at",
    ] {
        assert_eq!(refreshed.payload()[field], bounded[field], "{field}");
    }

    let mut legacy = original.clone();
    for key in ["metadata", "capabilities", "revoked_at"] {
        legacy.as_object_mut().unwrap().remove(key);
    }
    let defaulted = decode(&legacy).unwrap();
    assert_eq!(defaulted.payload()["metadata"], json!({}));
    assert!(defaulted.payload()["revoked_at"].is_null());
    for key in ["id", "revision", "created_at", "updated_at", "last_used_at"] {
        let mut missing = original.clone();
        missing.as_object_mut().unwrap().remove(key);
        assert!(decode(&missing).is_err(), "missing {key}");
    }
    for (key, invalid) in [
        ("token_sha256", json!("invalid")),
        ("csrf_sha256", json!("invalid")),
        ("revision", json!(0)),
        ("last_used_at", json!("2030-01-01T00:00:00")),
        ("idle_expires_at", json!("2031-01-01T00:00:00Z")),
        ("revoked_at", json!("not a timestamp")),
    ] {
        let mut bad = original.clone();
        bad[key] = invalid;
        assert!(decode(&bad).is_err(), "invalid {key}");
    }
    let mut revoked = original.clone();
    revoked["revoked_at"] = "2029-12-31T19:00:00-05:00".into();
    assert!(!decode(&revoked).unwrap().valid_at(now));
    let mut exhausted = original.clone();
    exhausted["revision"] = i64::MAX.into();
    assert!(decode(&exhausted).unwrap().refreshed(now).is_err());
    assert_eq!(format!("{device:?}"), "PairedDevice { redacted }");
}
