use nebula_assistant_integrations::*;
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../compatibility/python-provider-protocol.json"
    ))
    .unwrap()
}
fn config(v: &Value) -> ProviderConfig {
    let mut c = ProviderConfig::new(v["id"].as_str().unwrap(), v["base_url"].as_str().unwrap());
    c.flavor = v["flavor"].as_str().unwrap().into();
    c.default_model = v["default_model"].as_str().map(str::to_owned);
    c.enabled = v["enabled"].as_bool().unwrap_or(true);
    c.model_allowlist = v["model_allowlist"]
        .as_array()
        .map(|a| a.iter().map(|v| v.as_str().unwrap().into()).collect())
        .unwrap_or_default();
    c.tools = v["capabilities"]["tools"] == true;
    c.strict_tools = v["capabilities"]["strict_tools"] == true;
    c.structured_output = v["capabilities"]["structured_output"] == true;
    if let Some(o) = v["model_parameters"].as_object() {
        c.model_parameters = o
            .iter()
            .map(|(k, v)| {
                (
                    k.clone(),
                    v.as_array()
                        .unwrap()
                        .iter()
                        .map(|v| v.as_str().unwrap().into())
                        .collect(),
                )
            })
            .collect()
    }
    if let Some(a) = v["reasoning_mandatory_models"].as_array() {
        c.reasoning_mandatory_models = a.iter().map(|v| v.as_str().unwrap().into()).collect()
    }
    c
}
fn request(v: &Value) -> ModelRequest {
    serde_json::from_str(&v.to_string()).unwrap()
}
fn bytes(hex: &str) -> Vec<u8> {
    hex.as_bytes()
        .chunks_exact(2)
        .map(|p| u8::from_str_radix(std::str::from_utf8(p).unwrap(), 16).unwrap())
        .collect()
}
fn frame_value(s: &str) -> Value {
    serde_json::from_str(s).unwrap_or_else(|_| json!(s))
}

#[test]
fn provider_payloads_and_completion_fields_match_source_vectors() {
    let f = fixture();
    assert_eq!(f["payloads"].as_array().unwrap().len(), 29);
    for case in f["payloads"].as_array().unwrap() {
        let c = config(&case["config"]);
        let r = request(&case["request"]);
        let actual = openai_payload(&c, &r, false);
        if case["expected"]["accepted"] == true {
            assert_eq!(
                actual.unwrap(),
                case["expected"]["payload"],
                "{}",
                case["name"]
            );
        } else {
            assert!(actual.is_err(), "{}", case["name"]);
        }
    }
    let c = config(&f["config"]);
    let r = request(&f["request"]);
    assert_eq!(f["completions"].as_array().unwrap().len(), 28);
    for case in f["completions"].as_array().unwrap() {
        let response = decode_completion(
            &c,
            &r,
            case["body"].as_str().unwrap().as_bytes(),
            ProtocolLimits::default(),
        );
        if case["expected"]["accepted"] == true {
            assert_eq!(
                serde_json::to_value(response.unwrap()).unwrap(),
                case["expected"]["response"],
                "{}",
                case["name"]
            )
        } else {
            assert!(response.is_err())
        }
    }
}
#[test]
fn incremental_sse_framing_matches_source_across_byte_boundaries() {
    let f = fixture();
    assert_eq!(f["framing"].as_array().unwrap().len(), 36);
    for case in f["framing"].as_array().unwrap() {
        let mut parser = SseDecoder::new(256 * 1024, 1024).unwrap();
        let mut frames = vec![];
        for chunk in case["chunks_hex"].as_array().unwrap() {
            frames.extend(parser.push(&bytes(chunk.as_str().unwrap())).unwrap());
        }
        frames.extend(parser.finish().unwrap());
        let actual: Vec<_> = frames.iter().map(|s| frame_value(s)).collect();
        let expected: Vec<_> = case["frames"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| frame_value(s.as_str().unwrap()))
            .collect();
        assert_eq!(actual, expected, "{}", case["name"]);
        assert_eq!(parser.retained_bytes(), 0);
    }
}
#[test]
fn provider_stream_vectors_preserve_channels_terminal_evidence_and_error_classes() {
    let f = fixture();
    assert_eq!(f["streams"].as_array().unwrap().len(), 36);
    for case in f["streams"].as_array().unwrap() {
        let mut decoder = SseDecoder::new(256 * 1024, 1024).unwrap();
        let mut frames = vec![];
        for chunk in case["chunks_hex"].as_array().unwrap() {
            frames.extend(decoder.push(&bytes(chunk.as_str().unwrap())).unwrap());
        }
        frames.extend(decoder.finish().unwrap());
        let mut acc = OpenAiAccumulator::new(
            config(&f["config"]),
            request(&f["request"]),
            ProtocolLimits::default(),
        )
        .unwrap();
        let mut events = vec![ModelStreamEvent::started()];
        let mut failed = false;
        for frame in frames {
            match acc.push(&frame) {
                Ok(next) => events.extend(next),
                Err(error) => {
                    events.push(ModelStreamEvent::failure(error));
                    failed = true;
                    break;
                }
            }
        }
        if !failed {
            match acc.finish() {
                Ok(next) => events.extend(next),
                Err(error) => events.push(ModelStreamEvent::failure(error)),
            }
        }
        let mut actual = serde_json::to_value(events).unwrap();
        let mut expected = case["events"].clone();
        for items in [&mut actual, &mut expected] {
            for event in items.as_array_mut().unwrap() {
                if event["type"] == "error" {
                    assert!(event["error"].is_string());
                    event["error"] = Value::Null;
                }
            }
        }
        assert_eq!(
            actual, expected,
            "{} (raw vendor diagnostic wording is an explicit remaining parity gap)",
            case["name"]
        );
    }
}
#[test]
fn reasoning_and_protocol_bounds_preserve_content_without_leaking_debug() {
    let f = fixture();
    for case in f["reasoning"].as_array().unwrap() {
        let model = case["model"].as_str().unwrap();
        let text = case["text"].as_str().unwrap();
        let mut splitter = ReplySplitter::new(model, 1024);
        let mut pieces = vec![];
        for c in text.chars() {
            pieces.extend(splitter.push(&c.to_string()).unwrap());
        }
        pieces.extend(splitter.finish());
        let actual: Vec<_> = pieces
            .into_iter()
            .map(|(r, t)| json!([if r { "reasoning" } else { "text" }, t]))
            .collect();
        assert_eq!(actual, case["pieces"].as_array().unwrap().to_vec());
    }
    let mut decoder = SseDecoder::new(32, 4).unwrap();
    decoder.push(b"data: do-not-print").unwrap();
    assert!(!format!("{decoder:?}").contains("do-not-print"));
    assert!(decoder.push(&[b'x'; 33]).is_err());
    let mut terminal = SseDecoder::new(32, 4).unwrap();
    let terminal_tail = format!("data: [DONE]\n\ndata: {}", "x".repeat(4096));
    assert_eq!(
        terminal.push_reply(terminal_tail.as_bytes()).unwrap(),
        vec!["[DONE]"]
    );
    assert_eq!(terminal.retained_bytes(), 0);
    // Repeated unmatched control openings must remain a linear scan.
    assert!(!control_markup(&"<tool_call>".repeat(4096)));
    let mut splitter = ReplySplitter::new("deepseek-r1", 32);
    splitter.push("do-not-print").unwrap();
    assert!(!format!("{splitter:?}").contains("do-not-print"));
    assert!(splitter.push(&"x".repeat(33)).is_err());
    assert!(!control_markup(
        "DSML is a format, and <tool_call> is a quoted tag."
    ));
    assert!(control_markup("<|DSML|tool_calls>"));
    assert!(control_markup(
        "<tool_call> { \"name\": \"fixture.echo\" }</tool_call>"
    ));
    assert!(!control_markup("<tool_call>{\"example\":true}</tool_call>"));
    let c = config(&f["config"]);
    let mut r = request(&f["request"]);
    r.tool_results.push(ModelToolResult {
        call_id: "inert".into(),
        name: "fixture.echo".into(),
        arguments: Default::default(),
        output: json!({}),
        is_error: false,
        response_group: None,
        response_text: None,
        reasoning_state: None,
        provider_metadata: None,
        attachments: vec![],
    });
    assert_eq!(
        openai_payload(&c, &r, false).unwrap_err().kind,
        ErrorKind::Configuration
    );
    let r = request(&f["request"]);
    let mut acc = OpenAiAccumulator::new(
        c,
        r,
        ProtocolLimits {
            response_bytes: 32,
            ..ProtocolLimits::default()
        },
    )
    .unwrap();
    assert!(
        acc.push(&json!({"choices":[{"delta":{"content":"x".repeat(33)}}]}).to_string())
            .is_err()
    );
}
