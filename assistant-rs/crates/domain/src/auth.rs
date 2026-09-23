//! Shared paired-device data consumed by Assistant authentication. Raw bearer or
//! cookie secrets are never part of this persisted record or its debug output.
use crate::records::{MAX_RECORD_BYTES, RecordError, fill_defaults, require_canonical_fields};
use chrono::{DateTime, Duration, SecondsFormat, Utc};
use jsonschema::Validator;
use serde_json::Value;
use std::sync::LazyLock;

static SCHEMA: LazyLock<Result<Value, RecordError>> = LazyLock::new(|| {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-auth.json"))
            .map_err(|_| RecordError::Schema)?;
    Ok(fixture["device_schema"].clone())
});
static VALIDATOR: LazyLock<Result<Validator, RecordError>> = LazyLock::new(|| {
    let mut schema = SCHEMA.as_ref().map_err(|_| RecordError::Schema)?.clone();
    require_canonical_fields(&mut schema);
    jsonschema::draft202012::options()
        .build(&schema)
        .map_err(|_| RecordError::Schema)
});
#[derive(Clone)]
pub struct PairedDevice {
    payload: Value,
    last_used: DateTime<Utc>,
    idle_expires: DateTime<Utc>,
    absolute_expires: DateTime<Utc>,
}
impl std::fmt::Debug for PairedDevice {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("PairedDevice { redacted }")
    }
}
impl PairedDevice {
    pub fn decode(bytes: &[u8]) -> Result<Self, RecordError> {
        if bytes.len() > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        let mut payload: Value = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
        for field in ["id", "revision", "created_at", "updated_at", "last_used_at"] {
            if payload.get(field).is_none() {
                return Err(RecordError::Shape("paired_device_sessions"));
            }
        }
        let schema = SCHEMA.as_ref().map_err(|_| RecordError::Schema)?;
        fill_defaults(schema, schema, &mut payload);
        if !VALIDATOR
            .as_ref()
            .map_err(|_| RecordError::Schema)?
            .is_valid(&payload)
            || payload["revision"].as_i64().is_none_or(|r| r < 1)
        {
            return Err(RecordError::Shape("paired_device_sessions"));
        }
        let time = |field: &str| date(&payload[field]);
        if time("created_at")? > time("updated_at")? {
            return Err(RecordError::Invariant("device update predates creation"));
        }
        if !payload["revoked_at"].is_null() {
            time("revoked_at")?;
        }
        let last_used = time("last_used_at")?;
        let idle_expires = time("idle_expires_at")?;
        let absolute_expires = time("absolute_expires_at")?;
        if idle_expires > absolute_expires {
            return Err(RecordError::Invariant(
                "device idle expiry exceeds absolute expiry",
            ));
        }
        Ok(Self {
            payload,
            last_used,
            idle_expires,
            absolute_expires,
        })
    }
    pub fn id(&self) -> &str {
        self.payload["id"]
            .as_str()
            .expect("validated device identity")
    }
    pub fn revision(&self) -> i64 {
        self.payload["revision"]
            .as_i64()
            .expect("validated device revision")
    }
    pub fn csrf_sha256(&self) -> &str {
        self.payload["csrf_sha256"]
            .as_str()
            .expect("validated CSRF digest")
    }
    pub fn token_sha256(&self) -> &str {
        self.payload["token_sha256"]
            .as_str()
            .expect("validated token digest")
    }
    pub fn payload(&self) -> &Value {
        &self.payload
    }
    pub fn valid_at(&self, now: DateTime<Utc>) -> bool {
        self.payload["revoked_at"].is_null()
            && now < self.idle_expires
            && now < self.absolute_expires
    }
    pub fn refresh_due(&self, now: DateTime<Utc>) -> bool {
        now.signed_duration_since(self.last_used) >= Duration::seconds(300)
    }
    pub fn refreshed(&self, now: DateTime<Utc>) -> Result<Self, RecordError> {
        let expiry = now
            .checked_add_signed(Duration::days(30))
            .ok_or(RecordError::Invariant(
                "device clock exceeds supported range",
            ))?
            .min(self.absolute_expires);
        let mut payload = self.payload.clone();
        payload["revision"] = self
            .revision()
            .checked_add(1)
            .ok_or(RecordError::Invariant("device revision exhausted"))?
            .into();
        payload["last_used_at"] = stamp(now).into();
        payload["updated_at"] = stamp(now).into();
        payload["idle_expires_at"] = stamp(expiry).into();
        Self::decode(&serde_json::to_vec(&payload).map_err(|_| RecordError::Json)?)
    }
}
fn date(value: &Value) -> Result<DateTime<Utc>, RecordError> {
    value
        .as_str()
        .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.with_timezone(&Utc))
        .ok_or(RecordError::Invariant(
            "device timestamp must be timezone-aware",
        ))
}
fn stamp(time: DateTime<Utc>) -> String {
    time.to_rfc3339_opts(
        if time.timestamp_subsec_micros() == 0 {
            SecondsFormat::Secs
        } else {
            SecondsFormat::Micros
        },
        true,
    )
}
