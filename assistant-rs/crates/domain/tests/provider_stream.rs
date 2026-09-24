use nebula_assistant_domain::provider_stream::{
    Draft, Error, MAX_FRAME_BYTES, MAX_SEQUENCE, digest_parts,
};
use serde::Serialize;
use serde_json::{Value, json};

#[test]
fn provider_sse_bytes_and_terminal_snapshots_match_python_journeys() {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-execution.json")).unwrap();
    let mut sequenced = 0;
    let mut snapshots = 0;
    for case in fixture["cases"].as_array().unwrap() {
        let Some(raw) = case["expected_raw_sse"].as_str() else {
            continue;
        };
        let mut joined = String::new();
        for frame in raw.split("\n\n").filter(|f| !f.is_empty()) {
            let event = frame
                .lines()
                .find_map(|l| l.strip_prefix("event: "))
                .unwrap();
            let data = frame
                .lines()
                .find_map(|l| l.strip_prefix("data: "))
                .unwrap();
            let parsed: Value = serde_json::from_str(data).unwrap();
            let encoded = if let Some(sequence) = parsed["sequence"].as_u64() {
                let suffix = format!(",\"sequence\":{sequence}}}");
                let original = format!("{}}}", data.strip_suffix(&suffix).unwrap());
                sequenced += 1;
                Draft::from_raw(event, &original)
                    .unwrap()
                    .encode(sequence)
                    .unwrap()
            } else {
                snapshots += 1;
                Draft::from_raw(event, data)
                    .unwrap()
                    .terminal_snapshot()
                    .unwrap()
            };
            assert_eq!(
                encoded.sse_bytes(),
                format!("{frame}\n\n").as_bytes(),
                "{}",
                case["name"]
            );
            assert_eq!(encoded.json_bytes(), data.as_bytes());
            joined.push_str(std::str::from_utf8(encoded.sse_bytes()).unwrap());
        }
        assert_eq!(joined.trim_end_matches('\n'), raw.trim_end_matches('\n'));
    }
    assert!(sequenced >= 20 && snapshots >= 3);
    // Build a source-shaped payload independently of the raw fixture parser.
    #[derive(Serialize)]
    struct Started<'a> {
        r#type: &'a str,
        turn_id: &'a str,
        provider_id: &'a str,
        model: &'a str,
        session_id: &'a str,
    }
    let event = Draft::from_payload(
        "started",
        &Started {
            r#type: "started",
            turn_id: "turn",
            provider_id: "provider",
            model: "fixture",
            session_id: "session",
        },
    )
    .unwrap()
    .encode(1)
    .unwrap();
    assert_eq!(
        std::str::from_utf8(event.sse_bytes()).unwrap(),
        "id: 1\nevent: started\ndata: {\"type\":\"started\",\"turn_id\":\"turn\",\"provider_id\":\"provider\",\"model\":\"fixture\",\"session_id\":\"session\",\"sequence\":1}\n\n"
    );
}

#[test]
fn provider_sse_rejects_ambiguous_fields_and_control_line_injection() {
    for name in [
        "",
        "delta\ninjected",
        "done\r",
        "é",
        "0event",
        "A",
        "a-b",
        "a\0b",
    ] {
        assert!(matches!(Draft::from_raw(name, "{}"), Err(Error::EventType)));
    }
    for raw in [
        "[]",
        "null",
        "{}",
        "{\"type\":7}",
        "{\"type\":\"done\"}",
        "{\"type\":\"delta\",\"type\":\"delta\"}",
        "{\"type\":\"delta\",\"sequence\":null}",
        "{\"type\":\"delta\",\"id\":\"injected\"}",
        "{\"type\":\"delta\",\"x\":0,\"x\":1}",
        "{\"type\":\"delta\"} {}",
    ] {
        assert!(Draft::from_raw("delta", raw).is_err(), "{raw}");
    }
    assert!(matches!(
        Draft::from_raw("delta", "{\n\"type\":\"delta\"}")
            .unwrap()
            .encode(1),
        Err(Error::Payload)
    ));
    for sequence in [0, MAX_SEQUENCE + 1, u64::MAX] {
        assert!(matches!(
            Draft::from_payload("delta", &json!({"type":"delta"}))
                .unwrap()
                .encode(sequence),
            Err(Error::Sequence)
        ));
    }
    assert!(matches!(
        Draft::from_payload("delta", &json!({"type":"delta"}))
            .unwrap()
            .terminal_snapshot(),
        Err(Error::Snapshot)
    ));
    let event = Draft::from_raw(
        "done",
        " {\"type\":\"done\",\"content\":\"line\\n\\rnext\"} \t",
    )
    .unwrap()
    .terminal_snapshot()
    .unwrap();
    assert!(event.sequence().is_none());
    assert!(event.sse_bytes().starts_with(b"event: done\n"));
    assert_eq!(event.sse_bytes().iter().filter(|&&b| b == b'\n').count(), 3);
}

#[test]
fn provider_sse_bounds_encoded_bytes_and_preserves_numeric_and_unicode_identity() {
    let raw = "{\"type\":\"delta\",\"delta\":\"DO-NOT-LOG-🌌\\n\\u001c\",\"opaque\":{\"z\":1.00,\"a\":999999999999999999999999999999999999}}";
    let draft = Draft::from_raw("delta", raw).unwrap();
    assert!(!format!("{draft:?}").contains("DO-NOT-LOG"));
    let event = draft.encode(MAX_SEQUENCE).unwrap();
    assert!(!format!("{event:?}").contains("DO-NOT-LOG"));
    assert!(
        std::str::from_utf8(event.json_bytes())
            .unwrap()
            .contains("\"z\":1.00,\"a\":999999999999999999999999999999999999")
    );
    assert!(event.retained_bytes() > event.sse_bytes().len() + event.json_bytes().len());
    let changed = Draft::from_raw("delta", &raw.replace("1.00", "1.0"))
        .unwrap()
        .encode(MAX_SEQUENCE)
        .unwrap();
    assert_ne!(event.content_sha256(), changed.content_sha256());
    assert_ne!(
        event.content_sha256(),
        Draft::from_raw("delta", raw)
            .unwrap()
            .encode(1)
            .unwrap()
            .content_sha256()
    );
    assert_eq!(
        event.content_sha256(),
        Draft::from_raw("delta", raw)
            .unwrap()
            .encode(MAX_SEQUENCE)
            .unwrap()
            .content_sha256()
    );
    assert!(matches!(
        Draft::from_payload(
            "delta",
            &json!({"type":"delta","delta":"x".repeat(MAX_FRAME_BYTES)})
        ),
        Err(Error::Capacity)
    ));
    let overhead = "{\"type\":\"delta\",\"delta\":\"\"}".len();
    let raw = format!(
        "{{\"type\":\"delta\",\"delta\":\"{}\"}}",
        "x".repeat(MAX_FRAME_BYTES - overhead)
    );
    assert_eq!(raw.len(), MAX_FRAME_BYTES);
    assert!(matches!(
        Draft::from_raw("delta", &raw).unwrap().encode(1),
        Err(Error::Capacity)
    ));
    let overhead = "{\"type\":\"done\",\"content\":\"\"}".len();
    let raw = format!(
        "{{\"type\":\"done\",\"content\":\"{}\"}}",
        "x".repeat(MAX_FRAME_BYTES - overhead)
    );
    assert!(matches!(
        Draft::from_raw("done", &raw).unwrap().terminal_snapshot(),
        Err(Error::Capacity)
    ));
    let fields = (0..128)
        .map(|n| format!("\"x{n}\":0"))
        .collect::<Vec<_>>()
        .join(",");
    assert!(matches!(
        Draft::from_raw("delta", &format!("{{\"type\":\"delta\",{fields}}}")),
        Err(Error::Capacity)
    ));
    assert!(matches!(
        Draft::from_raw(
            "delta",
            &format!("{{\"type\":\"delta\",\"{}\":0}}", "x".repeat(257))
        ),
        Err(Error::Capacity)
    ));
    assert_ne!(digest_parts(&[b"ab", b"c"]), digest_parts(&[b"a", b"bc"]));
    assert_ne!(digest_parts(&[b"a"]), digest_parts(&[b"a", b""]));
}
