//! Pure saved ProviderProfile -> provider configuration. No ambient environment,
//! vault, DNS, HTTP client, provider call or tool execution is used here.
use nebula_assistant_domain::dependencies::{DependencyKind, StoredDependency};
use nebula_assistant_integrations::{ModelRequest, ProviderConfig};
use serde::{
    Deserialize, Deserializer,
    de::{MapAccess, Visitor},
};
use serde_json::{Value, json, value::RawValue};
use std::{
    collections::{BTreeMap, HashMap, HashSet},
    fmt,
    net::{Ipv4Addr, Ipv6Addr},
    sync::LazyLock,
    time::Duration,
};

const BYTES: usize = 1024 * 1024;
const ENTRIES: usize = 10_000;
const SECRET_BYTES: usize = 16_384;
const HEADER_BYTES: usize = 64 * 1024;
type Result<T> = std::result::Result<T, ProfileError>;

pub struct ProfileError(Value);
impl ProfileError {
    pub fn details(&self) -> &Value {
        &self.0
    }
    fn source(kind: &'static str, detail: impl Into<String>) -> Self {
        let detail = detail.into();
        if detail.len() > BYTES {
            return Self::boundary();
        }
        let value = json!({"kind":kind,"detail":detail});
        if bounded(&value).is_err() {
            Self::boundary()
        } else {
            Self(value)
        }
    }
    fn boundary() -> Self {
        Self(
            json!({"kind":"ResourceBoundary","detail":"Provider configuration exceeds its supported ownership boundary"}),
        )
    }
}
impl fmt::Debug for ProfileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ProviderProfileError")
            .field("kind", &self.0["kind"])
            .finish_non_exhaustive()
    }
}
impl fmt::Display for ProfileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Provider configuration could not be prepared")
    }
}
impl std::error::Error for ProfileError {}

pub struct Secret(String);
impl Secret {
    pub fn new(value: String) -> Result<Self> {
        if value.len() > SECRET_BYTES {
            Err(ProfileError::boundary())
        } else {
            Ok(Self(value))
        }
    }
}
impl fmt::Debug for Secret {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("ProviderSecret([redacted])")
    }
}
pub enum CredentialFailure {
    Locked(String),
    Unavailable(String),
}
impl fmt::Debug for CredentialFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("CredentialFailure([redacted])")
    }
}
pub trait ManagedCredentialResolver {
    fn resolve(&mut self, reference: &str) -> std::result::Result<Secret, CredentialFailure>;
}
pub trait EnvironmentInputs {
    fn variable(&mut self, name: &str) -> Option<String>;
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdapterSupport {
    OpenAiCompatible,
    Unavailable(AdapterKind),
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdapterKind {
    Responses,
    Anthropic,
    Gemini,
    Bedrock,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RequestMode {
    Complete,
    Stream,
}

pub struct PreparedProvider {
    config: Value,
    metadata: Box<RawValue>,
    managed_ref: Option<String>,
    managed_secret: Option<Secret>,
    adapter: AdapterSupport,
}
impl fmt::Debug for PreparedProvider {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PreparedProvider")
            .field("adapter", &self.adapter)
            .finish_non_exhaustive()
    }
}
/// Header projections remain private to this owned configuration and have no
/// Serialize/Debug implementation. Borrowing them requires trusted caller scope.
pub struct WireConfiguration {
    runtime: ProviderConfig,
    headers: BTreeMap<String, String>,
    mode: RequestMode,
}
impl fmt::Debug for WireConfiguration {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("WireConfiguration([redacted])")
    }
}
impl WireConfiguration {
    pub fn runtime(&self) -> &ProviderConfig {
        &self.runtime
    }
    pub fn into_runtime(self) -> ProviderConfig {
        self.runtime
    }
    pub fn headers(&self) -> &BTreeMap<String, String> {
        &self.headers
    }
    pub fn mode(&self) -> RequestMode {
        self.mode
    }
}

#[derive(Deserialize)]
struct Catalog {
    catalog: Vec<Value>,
    locality: Value,
    decimal_zeroes: Vec<u32>,
    unicode_tables: UnicodeTables,
}
#[derive(Deserialize)]
struct UnicodeTables {
    lower: Vec<(u32, String)>,
    cased: Vec<(u32, u32)>,
    case_ignorable: Vec<(u32, u32)>,
    nfkc_delimiters: Vec<u32>,
}
static DATA: LazyLock<Catalog> = LazyLock::new(|| {
    serde_json::from_str(include_str!("provider_profile_static.json"))
        .expect("captured provider catalog")
});
fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key].as_str().ok_or_else(ProfileError::boundary)
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(v) => v.as_f64().is_none_or(|n| n != 0.0),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}
fn repr(s: &str) -> Result<String> {
    crate::subagents::python_string_repr(s).map_err(|_| ProfileError::boundary())
}
// Logical encoded ownership bounds are not a resident-memory promise. Both the
// already hydrated profile and new metadata parsing have finite structural work.
fn bounded(v: &Value) -> Result<()> {
    fn nodes(v: &Value, left: &mut usize) -> Result<()> {
        *left = left.checked_sub(1).ok_or_else(ProfileError::boundary)?;
        match v {
            Value::Array(values) => {
                for item in values {
                    nodes(item, left)?;
                }
            }
            Value::Object(values) => {
                for item in values.values() {
                    nodes(item, left)?;
                }
            }
            _ => {}
        }
        Ok(())
    }
    let mut remaining = ENTRIES;
    nodes(v, &mut remaining)?;
    struct Count(usize);
    impl std::io::Write for Count {
        fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
            self.0 = self.0.saturating_add(b.len());
            if self.0 > BYTES {
                Err(std::io::Error::other("profile bound"))
            } else {
                Ok(b.len())
            }
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    serde_json::to_writer(Count(0), v).map_err(|_| ProfileError::boundary())
}

// Count lexical JSON tokens before constructing a second DOM. String contents
// are skipped in one pass; duplicate keys and ignored descriptor entries still
// consume this budget. A finite token bound also bounds any borrowed row vector.
fn raw_bound(raw: &str) -> Result<()> {
    if raw.len() > BYTES {
        return Err(ProfileError::boundary());
    }
    let bytes = raw.as_bytes();
    let mut cursor = 0;
    let mut tokens = 0usize;
    while cursor < bytes.len() {
        if bytes[cursor].is_ascii_whitespace() {
            cursor += 1;
            continue;
        }
        tokens += 1;
        if tokens > ENTRIES {
            return Err(ProfileError::boundary());
        }
        match bytes[cursor] {
            b'"' => {
                cursor += 1;
                while cursor < bytes.len() {
                    match bytes[cursor] {
                        b'\\' => cursor = cursor.saturating_add(2),
                        b'"' => {
                            cursor += 1;
                            break;
                        }
                        _ => cursor += 1,
                    }
                }
            }
            b'{' | b'}' | b'[' | b']' | b':' | b',' => cursor += 1,
            _ => {
                cursor += 1;
                while cursor < bytes.len()
                    && !bytes[cursor].is_ascii_whitespace()
                    && !matches!(
                        bytes[cursor],
                        b'{' | b'}' | b'[' | b']' | b':' | b',' | b'"'
                    )
                {
                    cursor += 1;
                }
            }
        }
    }
    Ok(())
}

/// A metadata fragment bound to an already hydrated ProviderProfile. Opaque
/// child objects retain their source insertion order and lexical JSON tokens.
pub struct BoundMetadata(Box<RawValue>);
impl BoundMetadata {
    pub fn as_str(&self) -> &str {
        self.0.get()
    }
}
impl fmt::Debug for BoundMetadata {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("BoundProviderMetadata([redacted])")
    }
}
/// Use the original payload from the same detached SQL row used for the single
/// dependency hydration. This does not rerun a clock or any profile validator.
/// Raw duplicate keys collapse before typed-key trimming, like json.loads then
/// ProviderProfile.model_validate; child dictionaries remain opaque and intact.
pub fn bound_metadata(profile: &StoredDependency, raw_profile: &str) -> Result<BoundMetadata> {
    if profile.kind() != DependencyKind::ProviderProfile {
        return Err(ProfileError::boundary());
    }
    bounded(profile.payload())?;
    raw_bound(raw_profile)?;
    let root: &RawValue =
        serde_json::from_str(raw_profile).map_err(|_| ProfileError::boundary())?;
    let fields = pairs(root)?;
    let raw = find(&fields, "metadata").map_or("{}", RawValue::get);
    let raw: &RawValue = serde_json::from_str(raw).map_err(|_| ProfileError::boundary())?;
    let mut output = String::from("{");
    let mut positions: HashMap<String, usize> = HashMap::new();
    let mut entries: Vec<(String, &RawValue)> = Vec::new();
    for (key, value) in pairs(raw)? {
        let key = key.trim().to_owned();
        if let Some(index) = positions.get(&key).copied() {
            entries[index].1 = value;
        } else {
            positions.insert(key.clone(), entries.len());
            entries.push((key, value));
        }
    }
    for (index, (key, value)) in entries.iter().enumerate() {
        let encoded = serde_json::to_string(key).map_err(|_| ProfileError::boundary())?;
        if output
            .len()
            .saturating_add(encoded.len())
            .saturating_add(value.get().len())
            .saturating_add(3)
            > BYTES
        {
            return Err(ProfileError::boundary());
        }
        if index != 0 {
            output.push(',');
        }
        output.push_str(&encoded);
        output.push(':');
        output.push_str(value.get());
    }
    output.push('}');
    let hydrated: Value = serde_json::from_str(&output).map_err(|_| ProfileError::boundary())?;
    if hydrated != profile.payload()["metadata"] {
        return Err(ProfileError::source(
            "BindingError",
            "Provider metadata does not match its hydrated owner",
        ));
    }
    Ok(BoundMetadata(
        RawValue::from_string(output).map_err(|_| ProfileError::boundary())?,
    ))
}

/// raw_metadata is used only for Python str()/iteration order. It must decode
/// exactly to this already hydrated profile's metadata; no factories run twice.
pub fn prepare(
    profile: &StoredDependency,
    raw_metadata: &str,
    mut resolver: Option<&mut dyn ManagedCredentialResolver>,
) -> Result<PreparedProvider> {
    if profile.kind() != DependencyKind::ProviderProfile || raw_metadata.len() > BYTES {
        return Err(ProfileError::boundary());
    }
    let p = profile.payload();
    bounded(p)?;
    raw_bound(raw_metadata)?;
    let metadata: Value =
        serde_json::from_str(raw_metadata).map_err(|_| ProfileError::boundary())?;
    if metadata != p["metadata"] {
        return Err(ProfileError::source(
            "BindingError",
            "Provider metadata does not match its hydrated owner",
        ));
    }
    let provider_type = string(p, "provider_type")?;
    let flavor = match provider_type {
        "openai-compatible" | "openai_compatible" => "custom",
        "openai-responses" | "openai_responses" => "openai",
        other => other,
    };
    let entry = DATA
        .catalog
        .iter()
        .find(|entry| entry["flavor"] == flavor)
        .ok_or_else(|| {
            ProfileError::source(
                "ValueError",
                format!(
                    "unknown provider type {}; use provider-catalog values",
                    repr(provider_type).unwrap_or_default()
                ),
            )
        })?;
    let mut secret_env = None;
    let mut managed_ref = None;
    let mut managed_secret = None;
    if let Some(reference) = p["secret_ref"].as_str().filter(|v| !v.is_empty()) {
        if reference.len() > 4096 {
            return Err(ProfileError::boundary());
        }
        if let Some(name) = reference.strip_prefix("env:") {
            secret_env = Some(name.to_owned());
        } else if ["systemd:", "vault:", "session:"]
            .iter()
            .any(|prefix| reference.starts_with(prefix))
        {
            managed_ref = Some(reference.to_owned());
            if let Some(resolver) = resolver.as_mut() {
                managed_secret = Some(resolver.resolve(reference).map_err(
                    |failure| match failure {
                        CredentialFailure::Locked(detail) => {
                            ProfileError::source("ProviderCredentialLockedError", detail)
                        }
                        CredentialFailure::Unavailable(detail) => {
                            ProfileError::source("ProviderError", detail)
                        }
                    },
                )?);
            }
        } else {
            return Err(ProfileError::source(
                "ValueError",
                "provider secret_ref must use env:NAME, systemd:NAME, vault:ID, or session:ID",
            ));
        }
    }
    let endpoint = p["endpoint"]
        .as_str()
        .filter(|s| !s.is_empty())
        .or(entry["default_base_url"].as_str())
        .ok_or_else(|| {
            ProfileError::source(
                "ValueError",
                format!(
                    "{} requires an explicit endpoint",
                    entry["display_name"].as_str().unwrap_or_default()
                ),
            )
        })?;
    let local = p["is_local"] == true || entry["local"] == true;
    if managed_ref.is_none() && secret_env.as_deref().is_none_or(str::is_empty) {
        secret_env = entry["suggested_key_env"].as_str().map(str::to_owned);
    }
    let mut options = metadata["options"].as_object().cloned().unwrap_or_default();
    if flavor == "azure_openai" {
        options.entry("api_key_header").or_insert("api-key".into());
        options.entry("api_key_scheme").or_insert("".into());
    }
    let mut caps = serde_json::Map::new();
    for (to, from) in [
        ("streaming", "streaming"),
        ("tools", "tool_calling"),
        ("strict_tools", "strict_structured_output"),
        ("parallel_tools", "parallel_tool_calls"),
        ("structured_output", "strict_structured_output"),
        ("vision", "vision"),
        ("documents", "documents"),
        ("audio", "audio"),
        ("embeddings", "embeddings"),
        ("reasoning_controls", "reasoning_controls"),
    ] {
        caps.insert(to.into(), p["capabilities"][from].clone());
    }
    caps.insert("usage".into(), true.into());
    for key in ["context_window", "max_output_tokens"] {
        caps.insert(
            key.into(),
            options
                .get(key)
                .filter(|v| v.is_number() && !v.to_string().contains(['.', 'e', 'E']))
                .cloned()
                .unwrap_or(Value::Null),
        );
    }
    let default = metadata
        .get("default_model")
        .filter(|v| truthy(v))
        .cloned()
        .or_else(|| {
            p["model_allowlist"]
                .as_array()
                .and_then(|v| v.first())
                .cloned()
        })
        .unwrap_or(Value::Null);
    let original = json!({"id":p["id"],"kind":entry["adapter"],"flavor":flavor,"base_url":endpoint,"api_key_env":secret_env,"api_key_value":managed_secret.as_ref().map(|s| if s.0.is_empty() { "" } else { "**********" }),"credential_ref":managed_ref,"local":local,"capabilities":caps,"options":options,"default_model":default,"model_allowlist":p["model_allowlist"],"enabled":p["enabled"],"data_residency":p["privacy"]["residency"].as_array().and_then(|v|v.first()),"data_retention":p["privacy"]["retention"]});
    let mut issues = Vec::new();
    if !endpoint.starts_with("http://") && !endpoint.starts_with("https://") {
        issues.push(issue(
            Some("base_url"),
            "value_error",
            "base_url must use http or https",
            Value::String(endpoint.into()),
        ));
    }
    if !default.is_null() && !default.is_string() {
        issues.push(issue(
            Some("default_model"),
            "string_type",
            "Input should be a valid string",
            default.clone(),
        ));
    }
    if !issues.is_empty() {
        return Err(validation(issues)?);
    }
    let base_url = endpoint.trim_end_matches('/');
    if let Err(detail) = validate_endpoint(base_url, local) {
        return Err(validation(vec![issue(
            None,
            "value_error",
            &detail,
            original,
        )])?);
    }
    if p["privacy"]["local_only"] == true && !local {
        return Err(ProfileError::source(
            "ValueError",
            "a local-only privacy profile cannot use a cloud provider",
        ));
    }
    let mut model_parameters = BTreeMap::new();
    let mut mandatory = Vec::new();
    let raw = RawValue::from_string(raw_metadata.into()).map_err(|_| ProfileError::boundary())?;
    let metadata_pairs = pairs(&raw)?;
    if let Some(descriptors) = find(&metadata_pairs, "model_descriptors")
        && descriptors.get().starts_with('[')
    {
        let values: Vec<&RawValue> =
            serde_json::from_str(descriptors.get()).map_err(|_| ProfileError::boundary())?;
        if values.len() > ENTRIES {
            return Err(ProfileError::boundary());
        }
        for item in values {
            if !item.get().starts_with('{') {
                continue;
            }
            let object = pairs(item)?;
            let Some(id) =
                find(&object, "id").and_then(|v| serde_json::from_str::<String>(v.get()).ok())
            else {
                continue;
            };
            let parameters = if let Some(value) = find(&object, "supported_parameters") {
                iter_strings(value)?
            } else {
                Vec::new()
            };
            model_parameters.insert(id.clone(), parameters);
            if find(&object, "reasoning_mandatory").is_some_and(|v| v.get() == "true") {
                mandatory.push(id);
            }
        }
    }
    let config = json!({"id":p["id"],"kind":entry["adapter"],"flavor":flavor,"base_url":base_url,"default_model":default,"model_allowlist":p["model_allowlist"],"api_key_env":secret_env,"extra_headers":{},"timeout_seconds":120.0,"local":local,"data_residency":original["data_residency"],"data_retention":p["privacy"]["retention"],"enabled":p["enabled"],"capabilities":caps,"options":options,"model_parameters":model_parameters,"reasoning_mandatory_models":mandatory});
    bounded(&config)?;
    let adapter = match entry["adapter"].as_str() {
        Some("openai_compatible") => AdapterSupport::OpenAiCompatible,
        Some("openai_responses") => AdapterSupport::Unavailable(AdapterKind::Responses),
        Some("anthropic") => AdapterSupport::Unavailable(AdapterKind::Anthropic),
        Some("gemini") => AdapterSupport::Unavailable(AdapterKind::Gemini),
        Some("bedrock") => AdapterSupport::Unavailable(AdapterKind::Bedrock),
        _ => return Err(ProfileError::boundary()),
    };
    Ok(PreparedProvider {
        config,
        metadata: raw,
        managed_ref,
        managed_secret,
        adapter,
    })
}
fn issue(field: Option<&str>, kind: &str, detail: &str, input: Value) -> Value {
    let mut value = json!({"type":kind,"loc":field.into_iter().collect::<Vec<_>>(),"msg":if kind=="value_error"{format!("Value error, {detail}")}else{detail.into()},"input":input});
    if kind == "value_error" {
        value["ctx"] = json!({"error":{}});
    }
    value
}
fn validation(issues: Vec<Value>) -> Result<ProfileError> {
    let value = json!({"kind":"ValidationError","issues":issues});
    bounded(&value)?;
    Ok(ProfileError(value))
}

impl PreparedProvider {
    pub fn public_config(&self) -> &Value {
        &self.config
    }
    pub fn adapter_support(&self) -> AdapterSupport {
        self.adapter
    }
    pub fn managed_reference(&self) -> Option<&str> {
        self.managed_ref.as_deref()
    }
    pub fn local(&self) -> bool {
        self.config["local"] == true
    }
    pub fn require<'a>(&'a self, request: &'a ModelRequest) -> Result<&'a str> {
        if request
            .model
            .as_ref()
            .is_some_and(|model| model.len() > BYTES)
            || request
                .tools
                .len()
                .saturating_add(request.tool_results.len())
                > ENTRIES
        {
            return Err(ProfileError::boundary());
        }
        if let Some(schema) = &request.response_schema {
            bounded(schema)?;
        }
        let provider = repr(string(&self.config, "id")?)?;
        if self.config["enabled"] != true {
            return Err(ProfileError::source(
                "ProviderError",
                format!("provider {provider} is disabled"),
            ));
        }
        let model = request
            .model
            .as_deref()
            .filter(|s| !s.is_empty())
            .or(self.config["default_model"].as_str())
            .filter(|s| !s.is_empty())
            .ok_or_else(|| {
                ProfileError::source(
                    "ProviderError",
                    format!("provider {provider} requires an explicit model"),
                )
            })?;
        let allow = self.config["model_allowlist"]
            .as_array()
            .ok_or_else(ProfileError::boundary)?;
        if !allow.is_empty() && !allow.iter().any(|v| v == model) {
            return Err(ProfileError::source(
                "ProviderError",
                format!(
                    "model {} is not allowed by provider {provider}",
                    repr(model)?
                ),
            ));
        }
        let mut required = Vec::new();
        if !request.tools.is_empty() || !request.tool_results.is_empty() {
            required.push("tools");
            if request.tools.iter().any(|t| t.strict) {
                required.push("strict_tools");
            }
        }
        if request.response_schema.as_ref().is_some_and(truthy) {
            required.push("structured_output");
        }
        let missing: Vec<_> = required
            .into_iter()
            .filter(|key| self.config["capabilities"][key] != true)
            .collect();
        if !missing.is_empty() {
            return Err(ProfileError::source(
                "UnsupportedCapability",
                format!(
                    "provider {provider} does not support: {}",
                    missing.join(", ")
                ),
            ));
        }
        Ok(model)
    }
    pub fn openai_config(
        &self,
        request: &ModelRequest,
        mode: RequestMode,
        environment: &mut dyn EnvironmentInputs,
    ) -> Result<WireConfiguration> {
        self.require(request)?;
        if self.adapter != AdapterSupport::OpenAiCompatible {
            return Err(ProfileError::source(
                "AdapterUnavailable",
                "This provider's native adapter has not been implemented in Rust",
            ));
        }
        let id = string(&self.config, "id")?;
        let key = if let Some(secret) = &self.managed_secret {
            Some(secret.0.clone())
        } else if self.managed_ref.as_deref().is_some_and(|s| !s.is_empty()) {
            return Err(ProfileError::source(
                "ProviderError",
                format!("provider {} credential reference is unavailable", repr(id)?),
            ));
        } else if let Some(name) = self.config["api_key_env"]
            .as_str()
            .filter(|s| !s.is_empty())
        {
            Some(
                environment_value(environment, name)?
                    .filter(|s| !s.is_empty())
                    .ok_or_else(|| {
                        ProfileError::source(
                            "ProviderError",
                            format!(
                                "provider {} requires environment variable {}",
                                repr(id).unwrap_or_default(),
                                repr(name).unwrap_or_default()
                            ),
                        )
                    })?,
            )
        } else {
            None
        };
        let mut headers = BTreeMap::from([("Content-Type".into(), "application/json".into())]);
        if let Some(key) = key.filter(|s| !s.is_empty()) {
            let metadata = pairs(&self.metadata)?;
            let options = find(&metadata, "options")
                .filter(|v| v.get().starts_with('{'))
                .map(pairs)
                .transpose()?
                .unwrap_or_default();
            let header = find(&options, "api_key_header")
                .map(py_string)
                .transpose()?
                .unwrap_or_else(|| "Authorization".into());
            let scheme = find(&options, "api_key_scheme")
                .map(py_string)
                .transpose()?
                .unwrap_or_else(|| "Bearer ".into());
            if header
                .len()
                .saturating_add(scheme.len())
                .saturating_add(key.len())
                > HEADER_BYTES
            {
                return Err(ProfileError::boundary());
            }
            headers.insert(header, format!("{scheme}{key}"));
        }
        let options = &self.config["options"];
        let completion = if mode == RequestMode::Complete {
            tuning(
                options,
                "request_timeout_seconds",
                "NEBULA_PROVIDER_REQUEST_TIMEOUT_SECONDS",
                600.0,
                3600.0,
                environment,
            )?
            .max(0.0)
        } else {
            600.0
        };
        let completion = if completion > 0.0 { completion } else { 600.0 };
        let attempts = tuning(
            options,
            "retry_attempts",
            "NEBULA_PROVIDER_RETRY_ATTEMPTS",
            3.0,
            8.0,
            environment,
        )?
        .trunc()
        .max(1.0) as u8;
        let backoff = tuning(
            options,
            "retry_backoff_seconds",
            "NEBULA_PROVIDER_RETRY_BACKOFF_SECONDS",
            0.5,
            20.0,
            environment,
        )?;
        let mut config = ProviderConfig::new(id, string(&self.config, "base_url")?);
        config.flavor = string(&self.config, "flavor")?.into();
        config.default_model = self.config["default_model"].as_str().map(str::to_owned);
        config.model_allowlist = self.config["model_allowlist"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_owned())
            .collect();
        config.enabled = self.config["enabled"] == true;
        config.tools = self.config["capabilities"]["tools"] == true;
        config.strict_tools = self.config["capabilities"]["strict_tools"] == true;
        config.structured_output = self.config["capabilities"]["structured_output"] == true;
        config.model_parameters = serde_json::from_value(self.config["model_parameters"].clone())
            .map_err(|_| ProfileError::boundary())?;
        config.reasoning_mandatory_models =
            serde_json::from_value(self.config["reasoning_mandatory_models"].clone())
                .map_err(|_| ProfileError::boundary())?;
        config.openrouter_allowed_providers = allowed_providers(options)?;
        config.completion_timeout = Duration::from_secs_f64(completion);
        config.retry_attempts = attempts;
        config.retry_backoff = Duration::from_secs_f64(backoff);
        config = config.with_headers(headers.clone()).map_err(|_| {
            ProfileError::source(
                "TransportBoundary",
                "Provider headers cannot be represented by the Rust HTTP adapter",
            )
        })?;
        Ok(WireConfiguration {
            runtime: config,
            headers,
            mode,
        })
    }
}
fn environment_value(
    environment: &mut dyn EnvironmentInputs,
    name: &str,
) -> Result<Option<String>> {
    let value = environment.variable(name);
    if value.as_ref().is_some_and(|v| v.len() > SECRET_BYTES) {
        Err(ProfileError::boundary())
    } else {
        Ok(value)
    }
}
fn tuning(
    options: &Value,
    key: &str,
    env: &str,
    fallback: f64,
    maximum: f64,
    environment: &mut dyn EnvironmentInputs,
) -> Result<f64> {
    // Python evaluates the default argument to dict.get even when key exists.
    let environment = environment_value(environment, env)?
        .map(Value::String)
        .unwrap_or(Value::Null);
    let value = options.get(key).unwrap_or(&environment);
    let number = match value {
        Value::Bool(v) => Some(if *v { 1.0 } else { 0.0 }),
        Value::Number(n) => {
            let value = n.to_string();
            let f = value.parse::<f64>().ok();
            if !value.contains(['.', 'e', 'E']) && f.is_some_and(|n| !n.is_finite()) {
                return Err(ProfileError::source(
                    "OverflowError",
                    "int too large to convert to float",
                ));
            }
            f
        }
        Value::String(s) => {
            let s: String = s
                .trim()
                .chars()
                .map(|c| {
                    let cp = c as u32;
                    DATA.decimal_zeroes
                        .iter()
                        .find(|zero| cp >= **zero && cp < **zero + 10)
                        .and_then(|zero| char::from_u32(u32::from(b'0') + cp - *zero))
                        .unwrap_or(c)
                })
                .collect();
            let bytes = s.as_bytes();
            if bytes.iter().enumerate().any(|(i, b)| {
                *b == b'_'
                    && (i == 0
                        || i + 1 == bytes.len()
                        || !bytes[i - 1].is_ascii_digit()
                        || !bytes[i + 1].is_ascii_digit())
            }) {
                None
            } else {
                s.replace('_', "").parse::<f64>().ok()
            }
        }
        _ => None,
    };
    Ok(match number {
        Some(n) if !n.is_nan() && n >= 0.0 => n.min(maximum),
        _ => fallback,
    })
}
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
fn scalar_in(ranges: &[(u32, u32)], c: char) -> bool {
    let cp = c as u32;
    let pos = ranges.partition_point(|(_, end)| *end < cp);
    ranges.get(pos).is_some_and(|(start, _)| *start <= cp)
}
fn python_lower(value: &str) -> Result<String> {
    let tables = &DATA.unicode_tables;
    let mut result = String::new();
    let mut preceding_cased = false;
    let mut chars = value.chars();
    while let Some(c) = chars.next() {
        let mut scalar = [0u8; 4];
        let lowered = if c == 'Σ' {
            let followed_cased = chars
                .clone()
                .find(|c| !scalar_in(&tables.case_ignorable, *c))
                .is_some_and(|c| scalar_in(&tables.cased, c));
            if preceding_cased && !followed_cased {
                "ς"
            } else {
                "σ"
            }
        } else if let Ok(index) = tables
            .lower
            .binary_search_by_key(&(c as u32), |(cp, _)| *cp)
        {
            tables.lower[index].1.as_str()
        } else {
            c.encode_utf8(&mut scalar)
        };
        if result.len().saturating_add(lowered.len()) > BYTES {
            return Err(ProfileError::boundary());
        }
        result.push_str(lowered);
        if !scalar_in(&tables.case_ignorable, c) {
            preceding_cased = scalar_in(&tables.cased, c);
        }
    }
    Ok(result)
}
fn allowed_providers(options: &Value) -> Result<Vec<String>> {
    let Some(values) = options["openrouter_providers"].as_array() else {
        return Ok(Vec::new());
    };
    if values.len() > ENTRIES {
        return Err(ProfileError::boundary());
    }
    let mut result = Vec::new();
    let mut seen = HashSet::new();
    for s in values.iter().filter_map(Value::as_str) {
        let s = python_lower(s.trim_matches(py_space))?;
        if !s.is_empty() && seen.insert(s.clone()) {
            result.push(s);
        }
    }
    Ok(result)
}

struct Pairs<'a>(Vec<(String, &'a RawValue)>);
impl<'de> Deserialize<'de> for Pairs<'de> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Pairs<'de>;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("bounded object")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut map: A,
            ) -> std::result::Result<Self::Value, A::Error> {
                let mut values: Vec<(String, &RawValue)> = Vec::new();
                let mut positions: HashMap<String, usize> = HashMap::new();
                while let Some((key, value)) = map.next_entry::<String, &RawValue>()? {
                    if values.len() >= ENTRIES {
                        return Err(serde::de::Error::custom("profile entry bound"));
                    }
                    if let Some(index) = positions.get(&key).copied() {
                        values[index].1 = value;
                    } else {
                        positions.insert(key.clone(), values.len());
                        values.push((key, value));
                    }
                }
                Ok(Pairs(values))
            }
        }
        d.deserialize_map(V)
    }
}
fn pairs(raw: &RawValue) -> Result<Vec<(String, &RawValue)>> {
    serde_json::from_str::<Pairs<'_>>(raw.get())
        .map(|v| v.0)
        .map_err(|_| ProfileError::boundary())
}
fn find<'a>(pairs: &[(String, &'a RawValue)], key: &str) -> Option<&'a RawValue> {
    pairs.iter().find(|(k, _)| k == key).map(|(_, v)| *v)
}
fn py_string(raw: &RawValue) -> Result<String> {
    let value: Value = serde_json::from_str(raw.get()).map_err(|_| ProfileError::boundary())?;
    let special = match &value {
        Value::Null => Some("None".into()),
        Value::Bool(false) => Some("False".into()),
        Value::Array(v) if v.is_empty() => Some("[]".into()),
        Value::Object(v) if v.is_empty() => Some("{}".into()),
        Value::Number(v) if v.as_f64() == Some(0.0) => Some(
            if v.to_string().contains(['.', 'e', 'E']) {
                if v.as_f64().unwrap().is_sign_negative() {
                    "-0.0"
                } else {
                    "0.0"
                }
            } else {
                "0"
            }
            .into(),
        ),
        _ => None,
    };
    special.map_or_else(
        || crate::subagents::python_json_string(raw.get()).map_err(|_| ProfileError::boundary()),
        Ok,
    )
}
fn iter_strings(raw: &RawValue) -> Result<Vec<String>> {
    let value: Value = serde_json::from_str(raw.get()).map_err(|_| ProfileError::boundary())?;
    if !truthy(&value) {
        return Ok(Vec::new());
    }
    let result = match value {
        Value::Array(_) => {
            let values: Vec<&RawValue> =
                serde_json::from_str(raw.get()).map_err(|_| ProfileError::boundary())?;
            if values.len() > ENTRIES {
                return Err(ProfileError::boundary());
            }
            values
                .into_iter()
                .map(py_string)
                .collect::<Result<Vec<_>>>()?
        }
        Value::Object(_) => pairs(raw)?.into_iter().map(|(k, _)| k).collect(),
        Value::String(s) => s.chars().take(ENTRIES + 1).map(|c| c.to_string()).collect(),
        Value::Bool(_) => {
            return Err(ProfileError::source(
                "TypeError",
                "'bool' object is not iterable",
            ));
        }
        Value::Number(n) => {
            return Err(ProfileError::source(
                "TypeError",
                if n.to_string().contains(['.', 'e', 'E']) {
                    "'float' object is not iterable"
                } else {
                    "'int' object is not iterable"
                },
            ));
        }
        Value::Null => Vec::new(),
    };
    if result.len() > ENTRIES {
        return Err(ProfileError::boundary());
    }
    Ok(result)
}
fn in_network(address: u128, network: &str, bits: u32) -> bool {
    let Some((ip, prefix)) = network.split_once('/') else {
        return false;
    };
    let Ok(prefix) = prefix.parse::<u32>() else {
        return false;
    };
    let mask = if prefix == 0 {
        0
    } else {
        u128::MAX << (bits - prefix)
    };
    let base = if bits == 32 {
        ip.parse::<Ipv4Addr>().map(|v| u32::from(v) as u128).ok()
    } else {
        ip.parse::<Ipv6Addr>().map(u128::from).ok()
    };
    base.is_some_and(|base| address & mask == base & mask)
}
fn local_host(host: &str) -> bool {
    let host = host.trim_end_matches('.').to_lowercase();
    if host == "localhost" {
        return true;
    }
    let (ip, scope) = host
        .split_once('%')
        .map_or((host.as_str(), None), |(ip, s)| (ip, Some(s)));
    if scope.is_some_and(|s| s.is_empty() || s.contains('%')) {
        return false;
    }
    let (address, bits) = if let Ok(ip) = ip.parse::<Ipv4Addr>() {
        if scope.is_some() {
            return false;
        }
        (u32::from(ip) as u128, 32)
    } else if let Ok(ip) = ip.parse::<Ipv6Addr>() {
        // CPython 3.12.11 delegates all four locality properties for mapped IPv4.
        if let Some(mapped) = ip.to_ipv4_mapped() {
            (u32::from(mapped) as u128, 32)
        } else {
            (u128::from(ip), 128)
        }
    } else {
        return false;
    };
    let table = &DATA.locality[if bits == 32 { "4" } else { "6" }];
    let includes = |name: &str| {
        table[name].as_array().is_some_and(|v| {
            v.iter()
                .any(|n| in_network(address, n.as_str().unwrap_or_default(), bits))
        })
    };
    (includes("_private_networks") && !includes("_private_networks_exceptions"))
        || includes("_reserved_networks")
}
fn validate_endpoint(endpoint: &str, local: bool) -> std::result::Result<(), String> {
    let normalized = endpoint
        .trim_start_matches(|c| c <= '\u{20}')
        .replace(['\t', '\r', '\n'], "");
    let (scheme, rest) = normalized.split_once("://").unwrap_or(("", ""));
    let split = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..split];
    let tail = &rest[split..];
    if authority.contains('[') != authority.contains(']') {
        return Err("Invalid IPv6 URL".into());
    }
    if authority.contains('[') && authority.contains(']') {
        let hostname_and_port = authority.rsplit_once('@').map_or(authority, |(_, h)| h);
        let address = if let Some((before, bracketed)) = hostname_and_port.split_once('[') {
            let (hostname, port) = bracketed.split_once(']').unwrap_or((bracketed, ""));
            if !before.is_empty() || (!port.is_empty() && !port.starts_with(':')) {
                return Err("Invalid IPv6 URL".into());
            }
            hostname
        } else {
            hostname_and_port
                .split_once(':')
                .map_or(hostname_and_port, |(h, _)| h)
        };
        if let Some(rest) = address.strip_prefix('v') {
            if !rest.split_once('.').is_some_and(|(v, h)| {
                !v.is_empty() && v.bytes().all(|b| b.is_ascii_hexdigit()) && !h.is_empty()
            }) {
                return Err("IPvFuture address is invalid".into());
            }
        } else {
            if address.parse::<Ipv4Addr>().is_ok() {
                return Err("An IPv4 address cannot be in brackets".into());
            }
            let (ip, scope) = address
                .split_once('%')
                .map_or((address, None), |(ip, s)| (ip, Some(s)));
            if ip.parse::<Ipv6Addr>().is_err()
                || scope.is_some_and(|s| s.is_empty() || s.contains('%'))
            {
                return Err(format!(
                    "{} does not appear to be an IPv4 or IPv6 address",
                    repr(address).unwrap_or_default()
                ));
            }
        }
    }
    if authority.chars().any(|c| {
        DATA.unicode_tables
            .nfkc_delimiters
            .binary_search(&(c as u32))
            .is_ok()
    }) {
        return Err(format!(
            "netloc '{authority}' contains invalid characters under NFKC normalization"
        ));
    }
    let (userinfo, host_port) = authority
        .rsplit_once('@')
        .map_or(("", authority), |(u, h)| (u, h));
    let (username, password) = userinfo.split_once(':').unwrap_or((userinfo, ""));
    let host = if let Some((_, b)) = host_port.split_once('[') {
        b.split_once(']').map_or("", |(h, _)| h)
    } else {
        host_port.split_once(':').map_or(host_port, |(h, _)| h)
    };
    if host.is_empty() || !username.is_empty() || !password.is_empty() {
        return Err("provider endpoint must have a host and no URL credentials".into());
    }
    let (before_fragment, fragment) = tail.split_once('#').unwrap_or((tail, ""));
    let query = before_fragment.split_once('?').map_or("", |(_, q)| q);
    if !query.is_empty() || !fragment.is_empty() {
        return Err("provider endpoint cannot contain query parameters or fragments".into());
    }
    let address = local_host(host);
    if address && !local {
        return Err(
            "private/link-local provider endpoints must be explicitly labeled local".into(),
        );
    }
    if local && !address {
        return Err("local provider endpoints must use localhost or a private, link-local, or reserved IP address".into());
    }
    if scheme == "http" && !address {
        return Err(
            "unencrypted provider endpoints are allowed only on local/private addresses".into(),
        );
    }
    Ok(())
}
