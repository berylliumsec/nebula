use serde::{Deserialize, Serialize};
use serde_json::{Number, Value};
use std::{collections::BTreeMap, fmt, time::Duration};

pub type Result<T> = std::result::Result<T, ProviderError>;
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    Refusal,
    ContextLength,
    Quota,
    Overloaded,
    ToolCall,
    MalformedToolCall,
    Response,
    Capacity,
    Cancelled,
    Configuration,
    Transport,
    Provider,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ProviderError {
    pub kind: ErrorKind,
    pub detail: String,
    pub status_code: Option<u16>,
    pub retry_after: Option<f64>,
}
impl ProviderError {
    pub fn new(kind: ErrorKind, detail: impl Into<String>) -> Self {
        Self {
            kind,
            detail: detail.into(),
            status_code: None,
            retry_after: None,
        }
    }
    pub fn capacity() -> Self {
        Self::new(
            ErrorKind::Capacity,
            "Provider resource capacity is full; retry after active work finishes",
        )
    }
    pub fn response(detail: &'static str) -> Self {
        Self::new(ErrorKind::Response, detail)
    }
    pub fn cancelled() -> Self {
        Self::new(ErrorKind::Cancelled, "Provider request cancelled")
    }
}
impl fmt::Debug for ProviderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProviderError")
            .field("kind", &self.kind)
            .field("status_code", &self.status_code)
            .finish_non_exhaustive()
    }
}
impl fmt::Display for ProviderError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "Provider request failed ({:?})", self.kind)
    }
}
impl std::error::Error for ProviderError {}
#[derive(Clone, Serialize, Deserialize)]
pub struct ModelMessage {
    pub role: String,
    pub content: Value,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ToolDefinition {
    pub name: String,
    #[serde(default)]
    pub description: String,
    pub input_schema: Value,
    #[serde(default)]
    pub strict: bool,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ModelToolResult {
    pub call_id: String,
    pub name: String,
    #[serde(default)]
    pub arguments: BTreeMap<String, Value>,
    pub output: Value,
    #[serde(default)]
    pub is_error: bool,
    #[serde(default)]
    pub response_group: Option<String>,
    #[serde(default)]
    pub response_text: Option<String>,
    #[serde(default)]
    pub reasoning_state: Option<Value>,
    #[serde(default)]
    pub provider_metadata: Option<Value>,
    #[serde(default, skip_serializing)]
    pub attachments: Vec<Value>,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ModelRequest {
    pub messages: Vec<ModelMessage>,
    #[serde(default)]
    pub model: Option<String>,
    #[serde(default)]
    pub instructions: Option<String>,
    #[serde(default)]
    pub tools: Vec<ToolDefinition>,
    #[serde(default)]
    pub tool_results: Vec<ModelToolResult>,
    #[serde(default = "auto")]
    pub tool_choice: String,
    #[serde(default)]
    pub max_output_tokens: Option<u64>,
    #[serde(default)]
    pub temperature: Option<f64>,
    #[serde(default)]
    pub parallel_tool_calls: bool,
    #[serde(default)]
    pub response_schema: Option<Value>,
    #[serde(default)]
    pub reasoning_effort: Option<String>,
    #[serde(default)]
    pub reasoning_max_tokens: Option<u64>,
    #[serde(default)]
    pub metadata: BTreeMap<String, String>,
}
fn auto() -> String {
    "auto".into()
}
impl ModelRequest {
    pub fn text(model: impl Into<String>, messages: Vec<ModelMessage>) -> Self {
        Self {
            messages,
            model: Some(model.into()),
            instructions: None,
            tools: vec![],
            tool_results: vec![],
            tool_choice: auto(),
            max_output_tokens: None,
            temperature: None,
            parallel_tool_calls: false,
            response_schema: None,
            reasoning_effort: None,
            reasoning_max_tokens: None,
            metadata: BTreeMap::new(),
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub struct InertToolCall {
    pub id: String,
    pub name: String,
    pub arguments: Value,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub invalid_reason: Option<String>,
    #[serde(default, skip_serializing)]
    pub provider_metadata: Option<Value>,
}
fn zero() -> Number {
    Number::from(0)
}
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelUsage {
    #[serde(default = "zero")]
    pub input_tokens: Number,
    #[serde(default = "zero")]
    pub output_tokens: Number,
    #[serde(default = "zero")]
    pub total_tokens: Number,
}
impl Default for ModelUsage {
    fn default() -> Self {
        Self {
            input_tokens: zero(),
            output_tokens: zero(),
            total_tokens: zero(),
        }
    }
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ModelResponse {
    pub provider_id: String,
    pub model: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub reasoning: String,
    #[serde(default)]
    pub tool_calls: Vec<InertToolCall>,
    #[serde(default)]
    pub usage: ModelUsage,
    #[serde(default)]
    pub finish_reason: Option<String>,
    #[serde(default)]
    pub provider_request_id: Option<String>,
    #[serde(default, skip_serializing)]
    pub reasoning_state: Option<Value>,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EventType {
    Started,
    TextDelta,
    ReasoningDelta,
    ToolCall,
    Completed,
    Error,
}
#[derive(Clone, Serialize, Deserialize)]
pub struct ModelStreamEvent {
    #[serde(rename = "type")]
    pub event_type: EventType,
    pub delta: Option<String>,
    pub tool_call: Option<InertToolCall>,
    pub response: Option<ModelResponse>,
    pub error: Option<String>,
    pub context_length_exceeded: bool,
    pub retryable: bool,
    pub tool_call_rejected: bool,
    pub error_kind: Option<ErrorKind>,
}
impl ModelStreamEvent {
    pub fn started() -> Self {
        Self {
            event_type: EventType::Started,
            delta: None,
            tool_call: None,
            response: None,
            error: None,
            context_length_exceeded: false,
            retryable: false,
            tool_call_rejected: false,
            error_kind: None,
        }
    }
    pub fn delta(reasoning: bool, text: String) -> Self {
        Self {
            event_type: if reasoning {
                EventType::ReasoningDelta
            } else {
                EventType::TextDelta
            },
            delta: Some(text),
            ..Self::started()
        }
    }
    pub fn completed(response: ModelResponse) -> Self {
        Self {
            event_type: EventType::Completed,
            response: Some(response),
            ..Self::started()
        }
    }
    pub fn failure(error: ProviderError) -> Self {
        Self {
            event_type: EventType::Error,
            error: Some(error.detail),
            error_kind: matches!(
                error.kind,
                ErrorKind::Refusal
                    | ErrorKind::ContextLength
                    | ErrorKind::Quota
                    | ErrorKind::Overloaded
                    | ErrorKind::ToolCall
                    | ErrorKind::MalformedToolCall
                    | ErrorKind::Response
            )
            .then_some(error.kind),
            context_length_exceeded: error.kind == ErrorKind::ContextLength,
            retryable: error.kind == ErrorKind::Overloaded,
            tool_call_rejected: error.kind == ErrorKind::ToolCall,
            ..Self::started()
        }
    }
}
macro_rules! redacted {($($ty:ty),*)=>{$(impl fmt::Debug for $ty{fn fmt(&self,f:&mut fmt::Formatter<'_>)->fmt::Result{f.debug_struct(stringify!($ty)).finish_non_exhaustive()}})*};}
redacted!(
    ModelMessage,
    ToolDefinition,
    ModelToolResult,
    ModelRequest,
    InertToolCall,
    ModelResponse,
    ModelStreamEvent
);
#[derive(Clone)]
pub struct ProviderConfig {
    pub id: String,
    pub flavor: String,
    pub base_url: String,
    pub default_model: Option<String>,
    pub model_allowlist: Vec<String>,
    pub enabled: bool,
    pub tools: bool,
    pub strict_tools: bool,
    pub structured_output: bool,
    pub model_parameters: BTreeMap<String, Vec<String>>,
    pub reasoning_mandatory_models: Vec<String>,
    pub openrouter_allowed_providers: Vec<String>,
    pub timeout: Duration,
    pub completion_timeout: Duration,
    pub retry_attempts: u8,
    pub retry_backoff: Duration,
    pub(crate) headers: reqwest::header::HeaderMap,
}
impl fmt::Debug for ProviderConfig {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProviderConfig")
            .field("flavor", &self.flavor)
            .finish_non_exhaustive()
    }
}
impl ProviderConfig {
    pub fn new(id: impl Into<String>, base_url: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            flavor: "custom".into(),
            base_url: base_url.into(),
            default_model: None,
            model_allowlist: vec![],
            enabled: true,
            tools: false,
            strict_tools: false,
            structured_output: false,
            model_parameters: BTreeMap::new(),
            reasoning_mandatory_models: vec![],
            openrouter_allowed_providers: vec![],
            timeout: Duration::from_secs(120),
            completion_timeout: Duration::from_secs(600),
            retry_attempts: 3,
            retry_backoff: Duration::from_millis(500),
            headers: reqwest::header::HeaderMap::new(),
        }
    }
    /// Already-resolved trusted credentials. Never accepted from the chat body.
    pub fn with_headers(
        mut self,
        headers: impl IntoIterator<Item = (String, String)>,
    ) -> Result<Self> {
        for (k, v) in headers {
            let name = reqwest::header::HeaderName::from_bytes(k.as_bytes()).map_err(|_| {
                ProviderError::new(ErrorKind::Configuration, "Invalid provider header name")
            })?;
            let mut value = reqwest::header::HeaderValue::from_str(&v).map_err(|_| {
                ProviderError::new(ErrorKind::Configuration, "Invalid provider header value")
            })?;
            value.set_sensitive(true);
            self.headers.insert(name, value);
        }
        Ok(self)
    }
    pub fn require<'a>(&'a self, request: &'a ModelRequest) -> Result<&'a str> {
        if !self.enabled {
            return Err(ProviderError::new(
                ErrorKind::Configuration,
                "Provider is disabled",
            ));
        }
        let model = request
            .model
            .as_deref()
            .filter(|s| !s.is_empty())
            .or(self.default_model.as_deref())
            .ok_or_else(|| {
                ProviderError::new(
                    ErrorKind::Configuration,
                    "Provider requires an explicit model",
                )
            })?;
        if !self.model_allowlist.is_empty() && !self.model_allowlist.iter().any(|m| m == model) {
            return Err(ProviderError::new(
                ErrorKind::Configuration,
                "Model is not allowed by provider",
            ));
        }
        if (!request.tools.is_empty() || !request.tool_results.is_empty()) && !self.tools
            || request.tools.iter().any(|t| t.strict) && !self.strict_tools
            || request
                .response_schema
                .as_ref()
                .is_some_and(|schema| !schema.as_object().is_some_and(|fields| fields.is_empty()))
                && !self.structured_output
        {
            return Err(ProviderError::new(
                ErrorKind::Configuration,
                "Provider does not support required capabilities",
            ));
        }
        if request.tool_choice == "required" && request.tools.is_empty() {
            return Err(ProviderError::new(
                ErrorKind::Configuration,
                "tool_choice=required requires at least one tool",
            ));
        }
        if !["auto", "required", "none"].contains(&request.tool_choice.as_str())
            || request.max_output_tokens == Some(0)
            || request.reasoning_max_tokens == Some(0)
            || request.temperature.is_some_and(|n| !n.is_finite())
            || request.reasoning_effort.as_deref().is_some_and(|s| {
                !["none", "minimal", "low", "medium", "high", "xhigh"].contains(&s)
            })
        {
            return Err(ProviderError::new(
                ErrorKind::Configuration,
                "Invalid normalized provider request",
            ));
        }
        Ok(model)
    }
}
#[derive(Clone, Copy, Debug)]
pub struct ProtocolLimits {
    pub frame_bytes: usize,
    pub response_bytes: usize,
    pub events_per_chunk: usize,
    pub tool_calls: usize,
}
impl Default for ProtocolLimits {
    fn default() -> Self {
        Self {
            frame_bytes: 256 * 1024,
            response_bytes: 2 * 1024 * 1024,
            events_per_chunk: 1024,
            tool_calls: 128,
        }
    }
}
pub(crate) fn bounded_json<T: Serialize>(value: &T, limit: usize) -> Result<Vec<u8>> {
    struct Writer {
        bytes: Vec<u8>,
        limit: usize,
    }
    impl std::io::Write for Writer {
        fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
            if b.len() > self.limit.saturating_sub(self.bytes.len()) {
                return Err(std::io::Error::other("provider JSON limit"));
            }
            self.bytes.extend_from_slice(b);
            Ok(b.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut writer = Writer {
        bytes: Vec::new(),
        limit,
    };
    serde_json::to_writer(&mut writer, value).map_err(|_| ProviderError::capacity())?;
    Ok(writer.bytes)
}
pub(crate) fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
pub(crate) fn pyspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

pub(crate) fn json_len<T: Serialize>(value: &T, limit: usize) -> Result<usize> {
    struct Counter {
        size: usize,
        limit: usize,
    }
    impl std::io::Write for Counter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if bytes.len() > self.limit.saturating_sub(self.size) {
                return Err(std::io::Error::other("provider JSON limit"));
            }
            self.size += bytes.len();
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut counter = Counter { size: 0, limit };
    serde_json::to_writer(&mut counter, value).map_err(|_| ProviderError::capacity())?;
    Ok(counter.size)
}
