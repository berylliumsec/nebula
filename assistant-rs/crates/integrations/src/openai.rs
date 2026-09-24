use crate::contracts::{bounded_json, json_len, pyspace, truthy};
use crate::reasoning::template_opened;
use crate::*;
use num_bigint::BigInt;
use num_traits::{FromPrimitive, Zero};
use serde_json::{Map, Number, Value, json};
use std::str::FromStr;

fn config_error(detail: &'static str) -> ProviderError {
    ProviderError::new(ErrorKind::Configuration, detail)
}
fn reason_model(model: &str) -> bool {
    let s = model.rsplit('/').next().unwrap_or(model);
    if let Some(rest) = s.strip_prefix("gpt-5") {
        return rest.is_empty() || rest.starts_with(['-', '.']);
    }
    if let Some(rest) = s.strip_prefix('o') {
        let n = rest.bytes().take_while(u8::is_ascii_digit).count();
        return n > 0 && (rest.len() == n || rest[n..].starts_with(['-', '.']));
    }
    false
}
fn family(model: &str, prefix: &str) -> Option<(u64, u64)> {
    let s = model.to_lowercase();
    let rest = s.split_once(prefix)?.1;
    let n = rest.bytes().take_while(u8::is_ascii_digit).count();
    if n == 0 {
        return None;
    }
    let major = rest[..n].parse().ok()?;
    let minor = rest[n..]
        .strip_prefix('.')
        .map(|s| {
            let n = s.bytes().take_while(u8::is_ascii_digit).count();
            s[..n].parse().unwrap_or(0)
        })
        .unwrap_or(0);
    Some((major, minor))
}
fn host(config: &ProviderConfig, domains: &[&str]) -> bool {
    let Ok(url) = reqwest::Url::parse(&config.base_url) else {
        return false;
    };
    let Some(host) = url.host_str() else {
        return false;
    };
    domains
        .iter()
        .any(|d| host == *d || host.ends_with(&format!(".{d}")))
}
fn strict_schema(v: &Value) -> bool {
    match v {
        Value::Array(a) => a.iter().all(strict_schema),
        Value::Object(o) => {
            if o.contains_key("default") || o.contains_key("uniqueItems") {
                return false;
            }
            let props = o.get("properties").and_then(Value::as_object);
            if v["type"] == "object"
                || v["type"]
                    .as_array()
                    .is_some_and(|a| a.contains(&json!("object")))
                || props.is_some()
            {
                let required = v["required"].as_array();
                if v["additionalProperties"] != false || required.is_none() {
                    return false;
                }
                if props.is_some_and(|p| {
                    p.keys()
                        .any(|k| !required.expect("checked").contains(&json!(k)))
                }) {
                    return false;
                }
            }
            o.iter().all(|(k, v)| {
                if ["enum", "const", "examples", "required"].contains(&k.as_str()) {
                    true
                } else if ["properties", "$defs", "definitions", "patternProperties"]
                    .contains(&k.as_str())
                {
                    v.as_object().is_none_or(|o| o.values().all(strict_schema))
                } else {
                    strict_schema(v)
                }
            })
        }
        _ => true,
    }
}
fn grammar(v: &Value) -> Value {
    match v {
        Value::Object(o) => Value::Object(
            o.iter()
                .filter(|(k, _)| *k != "uniqueItems")
                .map(|(k, v)| (k.clone(), grammar(v)))
                .collect(),
        ),
        Value::Array(a) => Value::Array(a.iter().map(grammar).collect()),
        _ => v.clone(),
    }
}
fn wire_name(name: &str) -> String {
    name.replace('.', "_")
}
fn decode_name(request: &ModelRequest, name: &str) -> String {
    request
        .tools
        .iter()
        .find(|t| wire_name(&t.name) == name)
        .map_or_else(|| name.to_owned(), |t| t.name.clone())
}
fn openai_message(message: &ModelMessage) -> Result<Value> {
    if message.content.is_string() {
        return Ok(json!({"role":message.role,"content":message.content}));
    }
    let Some(parts) = message.content.as_array() else {
        return Err(config_error(
            "Normalized message content must be text or parts",
        ));
    };
    let mut content = vec![];
    for part in parts {
        match part["type"].as_str(){Some("text")=>content.push(json!({"type":"text","text":part["text"].as_str().unwrap_or("")})),Some("image")=>content.push(json!({"type":"image_url","image_url":{"url":format!("data:{};base64,{}",part["media_type"].as_str().unwrap_or("None"),part["data"].as_str().unwrap_or("None"))}})),_=>{}}
    }
    Ok(json!({"role":message.role,"content":content}))
}
/// Pure request rendering. Retained tool results need the later replay adapter;
/// rejecting them prevents dropping signed reasoning or changing call grouping.
pub fn openai_payload(
    config: &ProviderConfig,
    request: &ModelRequest,
    stream: bool,
) -> Result<Value> {
    let model = config.require(request)?;
    bounded_json(request, 16 * 1024 * 1024)?;
    if !request.tool_results.is_empty() {
        return Err(config_error(
            "Retained tool-result replay is not implemented by this Assistant adapter",
        ));
    }
    let openrouter = config.flavor == "openrouter";
    let reasoning = reason_model(model);
    let supported = config.model_parameters.get(model);
    let has = |s: &str| supported.is_some_and(|a| a.iter().any(|v| v == s));
    let json_object = request.response_schema.as_ref().is_some_and(truthy)
        && (if openrouter {
            supported.is_some_and(|p| !p.is_empty()) && !has("structured_outputs")
        } else {
            config.flavor == "deepseek" || host(config, &["deepseek.com", "z.ai", "bigmodel.cn"])
        });
    let strict = ["openai", "azure_openai", "microsoft_foundry"].contains(&config.flavor.as_str())
        || openrouter && model.to_lowercase().starts_with("openai/");
    let mut messages = request
        .messages
        .iter()
        .map(openai_message)
        .collect::<Result<Vec<_>>>()?;
    let mut instructions = request.instructions.clone().unwrap_or_default();
    if json_object {
        return Err(config_error(
            "JSON-object instruction rendering requires the structured-output adapter",
        ));
    }
    if !instructions.is_empty() {
        messages.insert(
            0,
            json!({"role":"system","content":std::mem::take(&mut instructions)}),
        );
    }
    let mut payload = json!({"model":model,"messages":messages});
    if let Some(tokens) = request.max_output_tokens {
        payload[if !openrouter && reasoning {
            "max_completion_tokens"
        } else {
            "max_tokens"
        }] = json!(tokens)
    }
    if !reasoning && let Some(t) = request.temperature {
        payload["temperature"] = json!(t)
    }
    if let Some(effort) = request.reasoning_effort.as_deref()
        && !openrouter
    {
        if config.flavor == "deepseek" || host(config, &["deepseek.com"]) {
            payload["thinking"] = json!({"type":if effort=="none"{"disabled"}else{"enabled"}});
            if effort != "none" {
                payload["reasoning_effort"] = json!(match effort {
                    "minimal" | "low" => "low",
                    "xhigh" => "max",
                    _ => "high",
                })
            }
        } else if host(config, &["z.ai", "bigmodel.cn"]) {
            if family(model, "glm-").is_some_and(|v| v >= (4, 5)) {
                payload["thinking"] = if effort == "none" {
                    json!({"type":"disabled"})
                } else {
                    json!({"type":"enabled","clear_thinking":false})
                };
                if family(model, "glm-").is_some_and(|v| v >= (5, 2))
                    && ["high", "xhigh"].contains(&effort)
                {
                    payload["reasoning_effort"] =
                        json!(if effort == "high" { "high" } else { "max" });
                }
            }
        } else if ["vllm", "sglang", "ollama"].contains(&config.flavor.as_str()) {
            let switch = if let Some(v) = family(model, "deepseek-v") {
                (v >= (3, 1)).then_some("thinking")
            } else if family(model, "glm-").is_some_and(|v| v >= (4, 5))
                || family(model, "qwen").is_some_and(|v| v >= (3, 0))
            {
                Some("enable_thinking")
            } else {
                None
            };
            if let Some(switch) = switch {
                if config.flavor == "ollama" {
                    payload["reasoning_effort"] = json!(match effort {
                        "minimal" => "low",
                        "xhigh" => "max",
                        _ => effort,
                    })
                } else {
                    payload["chat_template_kwargs"] = json!({switch:effort!="none"})
                }
            }
        } else if reasoning {
            payload["reasoning_effort"] = json!(effort)
        }
    }
    if !request.tools.is_empty() {
        if openrouter {
            payload["provider"] = json!({"require_parameters":true})
        } else {
            payload["parallel_tool_calls"] = json!(request.parallel_tool_calls)
        }
        let mut names = std::collections::BTreeSet::new();
        let mut tools = vec![];
        for tool in &request.tools {
            let name = wire_name(&tool.name);
            if !names.insert(name.clone()) {
                return Err(config_error(
                    "Provider tool names collide after normalization",
                ));
            }
            let schema = if config.flavor == "vllm" {
                grammar(&tool.input_schema)
            } else {
                tool.input_schema.clone()
            };
            let mut function =
                json!({"name":name,"description":tool.description,"parameters":schema});
            if strict {
                function["strict"] = json!(tool.strict && strict_schema(&tool.input_schema));
            }
            tools.push(json!({"type":"function","function":function}));
        }
        payload["tools"] = json!(tools);
        if ["required", "none"].contains(&request.tool_choice.as_str()) {
            payload["tool_choice"] = json!(request.tool_choice)
        }
    }
    if let Some(schema) = request.response_schema.as_ref().filter(|v| truthy(v)) {
        let mut value = json!({"name":"nebula_response","schema":if config.flavor=="vllm"{grammar(schema)}else{schema.clone()}});
        if strict {
            value["strict"] = json!(strict_schema(schema));
        }
        payload["response_format"] = json!({"type":"json_schema","json_schema":value});
    }
    if openrouter {
        if let Some(id) = request
            .metadata
            .get("chat_session_id")
            .filter(|s| !s.is_empty())
        {
            payload["session_id"] = json!(id)
        }
        if !config.openrouter_allowed_providers.is_empty() {
            if payload.get("provider").is_none() {
                payload["provider"] = json!({})
            }
            payload["provider"]["only"] = json!(config.openrouter_allowed_providers)
        }
        let mut r = json!({"exclude":false});
        if supported.is_none_or(|s| s.is_empty()) || has("reasoning") {
            if let Some(e) = request.reasoning_effort.as_deref()
                && (e != "none" || !config.reasoning_mandatory_models.iter().any(|m| m == model))
            {
                r["effort"] = json!(e)
            }
            if let Some(n) = request.reasoning_max_tokens {
                r["max_tokens"] = json!(n)
            }
        }
        payload["reasoning"] = r;
        payload["plugins"] = json!([{"id":"context-compression","enabled":false}]);
        if !request.tools.is_empty() {
            let p = payload.as_object_mut().expect("object");
            for k in ["reasoning", "temperature"] {
                if !has(k) {
                    p.remove(k);
                }
            }
            if supported.is_some_and(|s| !s.is_empty()) {
                for k in ["tool_choice", "max_tokens"] {
                    if !has(k) {
                        p.remove(k);
                    }
                }
                if !(has("response_format") && has("structured_outputs")) {
                    p.remove("response_format");
                }
            }
        }
    }
    if model.to_lowercase().contains("deepseek-v4") {
        for message in payload["messages"].as_array_mut().expect("messages") {
            if message["role"] == "assistant" {
                message[if openrouter {
                    "reasoning_details"
                } else {
                    "reasoning_content"
                }] = if openrouter { json!([]) } else { json!("") }
            }
        }
    }
    if stream {
        payload["stream"] = json!(true);
        payload["stream_options"] = json!({"include_usage":true});
    }
    bounded_json(&payload, 16 * 1024 * 1024)?;
    Ok(payload)
}
fn first_choice(data: &Value) -> &Value {
    data.get("choices")
        .and_then(Value::as_array)
        .and_then(|a| a.first())
        .filter(|v| v.is_object())
        .unwrap_or(&Value::Null)
}
fn text_parts(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Array(a) => a
            .iter()
            .filter_map(|p| {
                if p.is_string() {
                    p.as_str().filter(|s| !s.is_empty())
                } else if p["type"].is_null() || p["type"] == "text" || p["type"] == "output_text" {
                    p["text"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .or_else(|| p["content"].as_str().filter(|s| !s.is_empty()))
                } else {
                    None
                }
            })
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}
fn content(message: &Value) -> String {
    let text = text_parts(&message["content"]);
    if text.is_empty() {
        message["refusal"].as_str().unwrap_or("").into()
    } else {
        text
    }
}
fn reasoning(message: &Value) -> String {
    for k in ["reasoning_content", "reasoning"] {
        if let Some(s) = message[k].as_str().filter(|s| !s.is_empty()) {
            return s.into();
        }
    }
    let Some(details) = message["reasoning_details"].as_array() else {
        return String::new();
    };
    details
        .iter()
        .filter_map(|v| {
            if let Some(s) = v.as_str() {
                return (!s.is_empty()).then_some(s);
            }
            if v["type"].as_str().unwrap_or("").contains("encrypted") {
                return None;
            }
            ["text", "summary", "content"]
                .iter()
                .find_map(|k| v[*k].as_str().filter(|s| !s.is_empty()))
        })
        .collect::<Vec<_>>()
        .join("\n")
}
fn count(value: &Value) -> BigInt {
    let Some(n) = value.as_number() else {
        return BigInt::zero();
    };
    let s = n.to_string();
    let n = if !s.contains(['.', 'e', 'E']) {
        BigInt::from_str(&s).ok()
    } else {
        n.as_f64().and_then(BigInt::from_f64)
    };
    n.filter(|n| n > &BigInt::zero()).unwrap_or_default()
}
fn number(n: BigInt) -> Number {
    Number::from_str(&n.to_string()).expect("integer JSON")
}
fn usage(value: &Value) -> ModelUsage {
    let input = count(&value["prompt_tokens"]);
    let output = count(&value["completion_tokens"]);
    let total = count(&value["total_tokens"]);
    ModelUsage {
        input_tokens: number(input.clone()),
        output_tokens: number(output.clone()),
        total_tokens: number(if total.is_zero() {
            input + output
        } else {
            total
        }),
    }
}
pub(crate) fn finish_failure(choice: &Value) -> Option<ProviderError> {
    let raw = choice["finish_reason"].as_str()?;
    let r = raw.to_lowercase();
    let kind = if ["error", "insufficient_system_resource", "network_error"].contains(&r.as_str())
        || r.split(|c: char| !c.is_ascii_alphanumeric())
            .any(|s| s == "error")
    {
        ErrorKind::Overloaded
    } else if r == "model_context_window_exceeded" {
        ErrorKind::ContextLength
    } else if ["content_filter", "sensitive"].contains(&r.as_str()) {
        ErrorKind::Refusal
    } else {
        return None;
    };
    Some(ProviderError::new(
        kind,
        match kind {
            ErrorKind::Refusal => "provider blocked the response: content filter stopped the reply",
            ErrorKind::ContextLength => "provider stopped the reply at the model's context window",
            _ => "provider ended the reply with an error",
        },
    ))
}
const CONTEXT_CODES: &[&str] = &[
    "context_length_exceeded",
    "context_window_exceeded",
    "max_tokens_exceeded",
    "prompt_too_long",
    "exceed_context_size_error",
    "request_too_large",
];
struct ErrorFields {
    code: String,
    typ: String,
    detail: String,
}
impl ErrorFields {
    fn read(data: &Value) -> Self {
        let error = &data["error"];
        let code = error
            .get("code")
            .filter(|v| truthy(v))
            .or_else(|| data.get("code"))
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
            })
            .unwrap_or_default()
            .to_lowercase();
        let typ = error["type"].as_str().unwrap_or("").to_lowercase();
        let mut detail = error
            .get("message")
            .filter(|v| truthy(v))
            .or_else(|| data.get("message"))
            .filter(|v| !v.is_null())
            .map(|v| {
                v.as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| v.to_string())
            })
            .or_else(|| {
                error
                    .as_str()
                    .filter(|s| !s.trim_matches(pyspace).is_empty())
                    .map(str::to_owned)
            })
            .unwrap_or_default();
        if let Some(raw) = error["metadata"]["raw"]
            .as_str()
            .filter(|s| !s.trim_matches(pyspace).is_empty())
        {
            let parsed = serde_json::from_str::<Value>(raw).ok();
            let upstream = parsed
                .as_ref()
                .and_then(|v| v["error"]["message"].as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or(raw);
            // Diagnostic strings are inspected for source classification but never exposed.
            let mut chars = 0;
            for word in upstream.split(pyspace).filter(|s| !s.is_empty()) {
                if chars >= 400 {
                    break;
                }
                detail.push(' ');
                chars += 1;
                for ch in word.chars().take(400 - chars) {
                    detail.push(ch);
                    chars += 1;
                }
            }
        }
        Self {
            code,
            typ,
            detail: detail.to_lowercase(),
        }
    }
    fn context(&self) -> bool {
        CONTEXT_CODES.contains(&self.code.as_str())
            || [
                "context length",
                "context window",
                "maximum context",
                "prompt is too long",
                "prompt too long",
                "too many tokens",
                "input token count",
                "input is too long",
                "maximum prompt length",
                "context size",
                "exceeded model token limit",
                "maximum allowed input length",
                "request exceeds the maximum size",
            ]
            .iter()
            .any(|s| self.detail.contains(s))
    }
    fn tool(&self) -> bool {
        self.code == "tool_use_failed"
            || [
                "undeclared or disallowed function",
                "not in request.tools",
                "tool call validation failed",
            ]
            .iter()
            .any(|s| self.detail.contains(s))
    }
    fn quota(&self) -> bool {
        [
            "insufficient_quota",
            "billing_not_active",
            "billing_hard_limit_reached",
        ]
        .iter()
        .any(|s| *s == self.code || *s == self.typ)
            || self.detail.contains("insufficient_quota")
    }
    fn transient(&self) -> bool {
        [
            "server_error",
            "internal_error",
            "internal_server_error",
            "service_unavailable",
            "overloaded",
            "overloaded_error",
            "timeout",
            "upstream_error",
        ]
        .iter()
        .any(|s| *s == self.code || *s == self.typ)
            || ["provider returned error", "provider disconnected"]
                .iter()
                .any(|s| self.detail.contains(s))
    }
}
pub(crate) fn error_frame(data: &Value) -> Option<ProviderError> {
    data.get("error").filter(|v| truthy(v))?;
    let fields = ErrorFields::read(data);
    let numeric = !fields.code.is_empty() && fields.code.bytes().all(|b| b.is_ascii_digit());
    let status = numeric.then(|| fields.code.parse::<u16>().ok()).flatten();
    let kind = if fields.context() {
        ErrorKind::ContextLength
    } else if fields.tool() {
        ErrorKind::ToolCall
    } else if status == Some(429) && fields.quota() {
        ErrorKind::Quota
    } else if status.is_some_and(retryable_status) || (!numeric && fields.transient()) {
        ErrorKind::Overloaded
    } else {
        ErrorKind::Provider
    };
    Some(ProviderError {
        kind,
        detail: "provider reported an error while streaming".into(),
        status_code: status,
        retry_after: None,
    })
}
pub(crate) fn http_error_kind(status: u16, data: &Value) -> ErrorKind {
    let fields = ErrorFields::read(data);
    if (matches!(status, 400 | 413 | 422) || status >= 500)
        && (fields.context() || CONTEXT_CODES.contains(&fields.typ.as_str()))
    {
        ErrorKind::ContextLength
    } else if matches!(status, 400 | 422) && fields.tool() {
        ErrorKind::ToolCall
    } else if status == 429 && fields.quota() {
        ErrorKind::Quota
    } else if retryable_status(status) {
        ErrorKind::Overloaded
    } else {
        ErrorKind::Provider
    }
}
pub(crate) fn retryable_status(status: u16) -> bool {
    [408, 425, 429, 500, 502, 503, 504, 529].contains(&status)
}
pub(crate) fn frame_data(encoded: &str) -> Result<Option<Value>> {
    let encoded = encoded.trim_matches(pyspace);
    match serde_json::from_str::<Value>(encoded) {
        Ok(v) if v.is_object() => Ok(Some(v)),
        Ok(_) => Ok(None),
        Err(_) if encoded.starts_with(['{', '[']) => Err(ProviderError::response(
            "provider sent a malformed stream chunk",
        )),
        Err(_) => Ok(None),
    }
}
pub(crate) fn frame_output(data: &Value) -> bool {
    if !data.is_object() {
        return true;
    }
    match data["choices"].as_array() {
        Some(choices) => choices
            .iter()
            .any(|c| !c.is_object() || truthy(&c["finish_reason"]) || delta_output(&c["delta"])),
        None => truthy(&data["choices"]),
    }
}
pub(crate) fn delta_output(v: &Value) -> bool {
    truthy(&v["tool_calls"])
        || truthy(&v["function_call"])
        || !content(v).is_empty()
        || !reasoning(v).is_empty()
}
fn valid_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    (2..=128).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b"_.-".contains(b))
}
fn argument_error(text: &str, error: serde_json::Error) -> ProviderError {
    let message = error.to_string();
    let reason =
        if error.is_eof() && message.contains("value") || message.starts_with("expected value") {
            "Expecting value"
        } else if message.contains("key must be a string") {
            "Expecting property name enclosed in double quotes"
        } else if message.contains("expected `,`") {
            "Expecting ',' delimiter"
        } else if message.contains("expected `:`") {
            "Expecting ':' delimiter"
        } else if message.contains("trailing characters") {
            "Extra data"
        } else {
            "Invalid JSON"
        };
    let byte = if error.is_eof() {
        text.len()
    } else {
        text.split_inclusive('\n')
            .take(error.line().saturating_sub(1))
            .map(str::len)
            .sum::<usize>()
            + error.column().saturating_sub(1)
    }
    .min(text.len());
    let mut boundary = byte;
    while !text.is_char_boundary(boundary) {
        boundary -= 1;
    }
    let prefix = &text[..boundary];
    let line = prefix.bytes().filter(|b| *b == b'\n').count() + 1;
    let col = prefix.rsplit('\n').next().unwrap_or("").chars().count() + 1;
    ProviderError::new(
        ErrorKind::ToolCall,
        format!(
            "arguments were not valid JSON: {reason}: line {line} column {col} (char {})",
            prefix.chars().count()
        ),
    )
}
fn arguments(value: &Value) -> Result<Value> {
    if value.is_object() {
        return Ok(value.clone());
    }
    if !truthy(value) {
        return Ok(json!({}));
    }
    let Some(s) = value.as_str() else {
        return Err(ProviderError::new(
            ErrorKind::ToolCall,
            "arguments were not a JSON object",
        ));
    };
    let mut values = serde_json::Deserializer::from_str(s).into_iter::<Value>();
    let mut parsed = values
        .next()
        .transpose()
        .map_err(|e| argument_error(s, e))?
        .ok_or_else(|| {
            ProviderError::new(ErrorKind::ToolCall, "arguments were not a JSON object")
        })?;
    for next in values {
        if next.map_err(|e| argument_error(s, e))? != parsed {
            return Err(ProviderError::new(
                ErrorKind::ToolCall,
                "arguments were not valid JSON: Extra data",
            ));
        }
    }
    if let Some(s) = parsed.as_str()
        && let Ok(v) = serde_json::from_str(s)
    {
        parsed = v;
    }
    if !parsed.is_object() {
        return Err(ProviderError::new(
            ErrorKind::ToolCall,
            "arguments were not a JSON object",
        ));
    }
    Ok(parsed)
}
fn tool_call(request: &ModelRequest, item: &Value) -> InertToolCall {
    let name = decode_name(request, item["function"]["name"].as_str().unwrap_or(""));
    let id = item["id"]
        .as_str()
        .filter(|s| !s.is_empty() && s.chars().count() <= 500)
        .map(str::to_owned)
        .unwrap_or_else(|| format!("call_{}", uuid::Uuid::new_v4().simple()));
    let args = arguments(&item["function"]["arguments"]);
    let invalid = if !valid_name(&name) {
        Some("provider returned an invalid tool name".into())
    } else {
        args.as_ref().err().map(|e| e.detail.clone())
    };
    InertToolCall {
        id,
        name: if valid_name(&name) {
            name
        } else {
            "invalid_tool_call".into()
        },
        arguments: args.unwrap_or_else(|_| json!({})),
        invalid_reason: invalid,
        provider_metadata: item
            .get("extra_content")
            .filter(|v| v.is_object())
            .and_then(|v| {
                let value = json!({"extra_content":v});
                bounded_json(&value, 32 * 1024).ok().map(|_| value)
            }),
    }
}
fn calls(request: &ModelRequest, message: &Value, limit: usize) -> Result<Vec<InertToolCall>> {
    let mut out = vec![];
    if let Some(items) = message["tool_calls"].as_array() {
        for item in items.iter().filter(|v| v.is_object()) {
            if out.len() == limit {
                return Err(ProviderError::capacity());
            }
            out.push(tool_call(request, item));
        }
    }
    if out.is_empty()
        && message["function_call"].is_object()
        && truthy(&message["function_call"]["name"])
    {
        out.push(tool_call(
            request,
            &json!({"function":message["function_call"]}),
        ));
    }
    Ok(out)
}
fn replay_state(
    config: &ProviderConfig,
    model: &str,
    message: &Value,
    calls: &[InertToolCall],
) -> Option<Value> {
    if calls.is_empty() {
        return None;
    }
    let mut state = Map::new();
    state.insert("provider_id".into(), json!(config.id));
    state.insert("model".into(), json!(model));
    for key in ["reasoning_content", "reasoning", "reasoning_details"] {
        if let Some(value) = message.get(key).filter(|v| truthy(v)) {
            state.insert(key.into(), value.clone());
        }
    }
    let value = Value::Object(state);
    bounded_json(&value, 256 * 1024).ok().map(|_| value)
}
fn response_string(value: &Value) -> Result<Option<String>> {
    match value {
        Value::Null => Ok(None),
        Value::String(s) => Ok(Some(s.clone())),
        _ => Err(ProviderError::new(
            ErrorKind::Provider,
            "Provider response fields failed validation",
        )),
    }
}
pub fn decode_completion(
    config: &ProviderConfig,
    request: &ModelRequest,
    bytes: &[u8],
    limits: ProtocolLimits,
) -> Result<ModelResponse> {
    if bytes.len() > limits.response_bytes {
        return Err(ProviderError::capacity());
    }
    let model = config.require(request)?;
    let data: Value = serde_json::from_slice(bytes)
        .map_err(|_| ProviderError::response("provider returned a response that is not JSON"))?;
    if !data.is_object() {
        return Err(ProviderError::response(
            "provider returned a response that is not a JSON object",
        ));
    }
    if let Some(error) = error_frame(&data).or_else(|| finish_failure(first_choice(&data))) {
        return Err(error);
    }
    let choice = first_choice(&data);
    let message = &choice["message"];
    let routed = reasoning(message).trim_matches(pyspace).to_owned();
    let (inline, text) = split_reply(
        &content(message),
        routed.is_empty() && template_opened(model),
    );
    let tool_calls = calls(request, message, limits.tool_calls)?;
    let response = ModelResponse {
        provider_id: config.id.clone(),
        model: if truthy(&data["model"]) {
            response_string(&data["model"])?.expect("truthy model")
        } else {
            model.into()
        },
        text: text.trim_matches(pyspace).into(),
        reasoning: [routed, inline.trim_matches(pyspace).into()]
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join("\n\n"),
        reasoning_state: replay_state(config, model, message, &tool_calls),
        tool_calls,
        usage: usage(&data["usage"]),
        finish_reason: response_string(&choice["finish_reason"])?,
        provider_request_id: response_string(&data["id"])?,
    };
    if control_markup(&response.text) {
        return Err(ProviderError::new(
            ErrorKind::ToolCall,
            "Provider returned serialized tool control data; plain-answer recovery is required",
        ));
    }
    Ok(response)
}
#[derive(Default)]
struct CallPart {
    id: String,
    name: String,
    args: String,
    index: Option<Number>,
    extra: Option<Value>,
    extra_bytes: usize,
}
pub struct OpenAiAccumulator {
    config: ProviderConfig,
    request: ModelRequest,
    limits: ProtocolLimits,
    splitter: ReplySplitter,
    text: String,
    reasoning: String,
    usage: ModelUsage,
    finish: Option<String>,
    id: Option<String>,
    model: String,
    terminated: bool,
    parts: Vec<CallPart>,
    legacy: CallPart,
    raw_reasoning: Map<String, Value>,
    replay_oversized: bool,
    invalid_fields: [bool; 3],
}
impl std::fmt::Debug for OpenAiAccumulator {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OpenAiAccumulator")
            .field("terminated", &self.terminated)
            .finish_non_exhaustive()
    }
}
impl OpenAiAccumulator {
    pub fn new(
        config: ProviderConfig,
        request: ModelRequest,
        limits: ProtocolLimits,
    ) -> Result<Self> {
        let model = config.require(&request)?.to_owned();
        Ok(Self {
            config,
            request,
            limits,
            splitter: ReplySplitter::new(&model, limits.response_bytes),
            text: String::new(),
            reasoning: String::new(),
            usage: ModelUsage::default(),
            finish: None,
            id: None,
            model,
            terminated: false,
            parts: vec![],
            legacy: CallPart::default(),
            raw_reasoning: Map::new(),
            replay_oversized: false,
            invalid_fields: [false; 3],
        })
    }
    pub fn terminated(&self) -> bool {
        self.terminated
    }
    pub fn has_finish_reason(&self) -> bool {
        self.finish.is_some() || self.invalid_fields[2]
    }
    pub fn retained_bytes(&self) -> usize {
        self.text.len()
            + self.reasoning.len()
            + self.splitter.retained_bytes()
            + self
                .parts
                .iter()
                .map(|p| {
                    p.id.len()
                        + p.name.len()
                        + p.args.len()
                        + p.extra_bytes.saturating_mul(16)
                        + std::mem::size_of::<CallPart>()
                })
                .sum::<usize>()
            + self.id.as_ref().map_or(0, String::len)
            + self.model.len()
            + self.finish.as_ref().map_or(0, String::len)
            + self.legacy.args.len()
            + self.legacy.name.len()
            + self
                .raw_reasoning
                .values()
                .filter_map(Value::as_str)
                .map(str::len)
                .sum::<usize>()
    }
    fn pieces(
        &mut self,
        pieces: Vec<(bool, String)>,
        events: &mut Vec<ModelStreamEvent>,
    ) -> Result<()> {
        for (reasoning, text) in pieces {
            if text.len()
                > self
                    .limits
                    .response_bytes
                    .saturating_sub(self.retained_bytes())
            {
                return Err(ProviderError::capacity());
            }
            if reasoning {
                self.reasoning.push_str(&text)
            } else {
                self.text.push_str(&text)
            }
            events.push(ModelStreamEvent::delta(reasoning, text));
        }
        Ok(())
    }
    pub fn push(&mut self, frame: &str) -> Result<Vec<ModelStreamEvent>> {
        if self.terminated {
            return Ok(vec![]);
        }
        if frame.len() > self.limits.frame_bytes {
            return Err(ProviderError::capacity());
        }
        let frame = frame.trim_matches(pyspace);
        if frame == "[DONE]" {
            self.terminated = true;
            return Ok(vec![]);
        }
        let Some(data) = frame_data(frame)? else {
            return Ok(vec![]);
        };
        if let Some(e) = error_frame(&data).or_else(|| finish_failure(first_choice(&data))) {
            return Err(e);
        }
        if truthy(&data["id"]) {
            self.invalid_fields[0] = !data["id"].is_string();
            self.id = data["id"].as_str().map(str::to_owned);
        }
        if truthy(&data["model"]) {
            self.invalid_fields[1] = !data["model"].is_string();
            self.model = data["model"].as_str().unwrap_or("").to_owned();
        }
        if truthy(&data["usage"]) {
            self.usage = usage(&data["usage"])
        }
        let choice = first_choice(&data);
        if truthy(&choice["finish_reason"]) {
            self.invalid_fields[2] = !choice["finish_reason"].is_string();
            self.finish = choice["finish_reason"].as_str().map(str::to_owned);
        }
        let delta = &choice["delta"];
        let mut events = vec![];
        let reason = reasoning(delta);
        if !reason.is_empty() {
            let pieces = self.splitter.route_reasoning()?;
            self.pieces(pieces, &mut events)?;
            self.pieces(vec![(true, reason)], &mut events)?;
        }
        for key in ["reasoning_content", "reasoning"] {
            if let Some(text) = delta[key].as_str().filter(|_| !self.replay_oversized) {
                let old = self.raw_reasoning.entry(key).or_insert_with(|| json!(""));
                if let Value::String(s) = old
                    && s.len() + text.len() <= 256 * 1024
                {
                    s.push_str(text);
                    continue;
                }
                {
                    *old = Value::Null;
                    self.replay_oversized = true;
                }
            }
        }
        let text = content(delta);
        if !text.is_empty() {
            let pieces = self.splitter.push(&text)?;
            self.pieces(pieces, &mut events)?;
        }
        if let Some(items) = delta["tool_calls"].as_array() {
            for item in items.iter().filter(|v| v.is_object()) {
                let id = item["id"].as_str().unwrap_or("");
                let name = item["function"]["name"].as_str().unwrap_or("");
                let index = item["index"]
                    .as_number()
                    .filter(|n| !n.to_string().contains(['.', 'e', 'E']))
                    .cloned();
                let mut slot = (!id.is_empty())
                    .then(|| self.parts.iter().position(|p| p.id == id))
                    .flatten();
                if slot.is_none() {
                    if index.is_some() {
                        if let Some(i) = self.parts.iter().rposition(|p| p.index == index) {
                            let p = &self.parts[i];
                            if (id.is_empty() || p.id.is_empty())
                                && (name.is_empty() || p.name.is_empty() || p.name == name)
                            {
                                slot = Some(i)
                            }
                        }
                    } else if id.is_empty() && name.is_empty()
                        || !id.is_empty()
                            && name.is_empty()
                            && self.parts.last().is_some_and(|p| p.id.is_empty())
                    {
                        slot = self.parts.len().checked_sub(1)
                    }
                }
                let i = match slot {
                    Some(i) => i,
                    None => {
                        if self.parts.len() >= self.limits.tool_calls {
                            return Err(ProviderError::capacity());
                        }
                        self.parts.push(CallPart::default());
                        let i = self.parts.len() - 1;
                        self.parts[i].index = index;
                        i
                    }
                };
                let p = &mut self.parts[i];
                if p.id.is_empty() {
                    p.id = id.into()
                }
                if p.name.is_empty() {
                    p.name = name.into()
                }
                if item["extra_content"].is_object() {
                    p.extra_bytes = json_len(&item["extra_content"], 32 * 1024).unwrap_or(0);
                    p.extra = (p.extra_bytes > 0).then(|| item["extra_content"].clone());
                }
                let arg = &item["function"]["arguments"];
                if arg.is_object() {
                    p.args = String::from_utf8(bounded_json(arg, self.limits.response_bytes)?)
                        .expect("JSON UTF-8")
                } else if let Some(s) = arg.as_str() {
                    if p.args.len() + s.len() > self.limits.response_bytes {
                        return Err(ProviderError::capacity());
                    }
                    p.args.push_str(s);
                }
            }
        }
        if let Some(function) = delta["function_call"].as_object() {
            if self.legacy.name.is_empty() {
                self.legacy.name = function
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .into()
            }
            if let Some(args) = function.get("arguments").and_then(Value::as_str) {
                if self.legacy.args.len() + args.len() > self.limits.response_bytes {
                    return Err(ProviderError::capacity());
                }
                self.legacy.args.push_str(args);
            }
        }
        if self.retained_bytes() > self.limits.response_bytes {
            return Err(ProviderError::capacity());
        }
        Ok(events)
    }
    pub fn finish(mut self) -> Result<Vec<ModelStreamEvent>> {
        if !self.terminated && !self.has_finish_reason() {
            return Err(ProviderError::new(
                ErrorKind::Provider,
                "provider stream ended before the reply completed: no finish reason or [DONE] frame arrived",
            ));
        }
        let mut events = vec![];
        let pieces = self.splitter.finish();
        self.pieces(pieces, &mut events)?;
        let mut kept: Vec<CallPart> = vec![];
        for part in self.parts {
            if part.name.is_empty() && !kept.is_empty() {
                let prev = kept.last_mut().expect("present");
                let joined = format!("{}{}", prev.args, part.args);
                if arguments(&json!(joined)).is_ok() {
                    prev.args = joined;
                    continue;
                }
                if part.id.is_empty() || arguments(&json!(part.args)).is_err() {
                    continue;
                }
            }
            kept.push(part)
        }
        let mut items = vec![];
        for p in kept {
            items.push(json!({"id":p.id,"function":{"name":p.name,"arguments":p.args},"extra_content":p.extra}));
        }
        let message = json!({"tool_calls":items,"function_call":if self.legacy.name.is_empty(){Value::Null}else{json!({"name":self.legacy.name,"arguments":self.legacy.args})}});
        let calls = calls(&self.request, &message, self.limits.tool_calls)?;
        for call in &calls {
            events.push(ModelStreamEvent {
                event_type: EventType::ToolCall,
                tool_call: Some(call.clone()),
                ..ModelStreamEvent::started()
            });
        }
        let text = self.text.trim_matches(pyspace).to_owned();
        if control_markup(&text) {
            return Err(ProviderError::new(
                ErrorKind::ToolCall,
                "Provider returned serialized tool control data; plain-answer recovery is required",
            ));
        }
        if self.invalid_fields.iter().any(|v| *v) {
            return Err(ProviderError::new(
                ErrorKind::Provider,
                "Provider response fields failed validation",
            ));
        }
        let response = ModelResponse {
            provider_id: self.config.id.clone(),
            model: self.model,
            text,
            reasoning: self.reasoning.trim_matches(pyspace).into(),
            reasoning_state: replay_state(
                &self.config,
                self.request.model.as_deref().unwrap_or(""),
                &if self.replay_oversized {
                    Value::Null
                } else {
                    Value::Object(self.raw_reasoning)
                },
                &calls,
            ),
            tool_calls: calls,
            usage: self.usage,
            finish_reason: self.finish,
            provider_request_id: self.id,
        };
        events.push(ModelStreamEvent::completed(response));
        Ok(events)
    }
}
