//! Ordered diagnostics for retained Assistant models and completion requests.
//!
//! This is a bounded set of source contracts, not a general Pydantic interpreter. No factory
//! runs while reading retained data; constructors receive explicit factory
//! outputs. Reports share their input and never expose
//! it through Debug/Display; only their explicit Serialize interface emits it.
use crate::records::{MAX_RECORD_BYTES, RecordError};
use chrono::{DateTime, NaiveDateTime, SecondsFormat, Utc};
use serde::{
    Deserialize, Deserializer, Serialize, Serializer,
    de::{MapAccess, Visitor},
    ser::SerializeSeq,
};
use serde_json::{Map, Value, json, value::RawValue};
use speedate::{
    Date, DateTime as ParsedDateTime, DateTimeConfig, MicrosecondsPrecisionOverflowBehavior,
    TimeConfig,
};
use std::{collections::HashSet, fmt, io::Write, sync::Arc};
use strum::EnumMessage;

const MAX_ISSUES: usize = 10_000;
type Result<T> = std::result::Result<T, RecordError>;
pub mod completion;
mod fork;
mod goal;
mod session;
mod turn;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub enum Model {
    Entity,
    ChatSchedule,
    ChatGoal,
    ChatTokenUsage,
    ChatSession,
    ChatTurn,
    ChatMessage,
    ChatContentBlock,
    ChatCitation,
    ChatDecision,
    HarnessSession,
    ChatCompletionRequest,
    ChatRequestMessage,
    ChatContextAttachment,
    PendingProviderSubagent,
}
impl Model {
    pub const fn name(self) -> &'static str {
        match self {
            Self::Entity => "Entity",
            Self::ChatSchedule => "ChatSchedule",
            Self::ChatGoal => "ChatGoal",
            Self::ChatTokenUsage => "ChatTokenUsage",
            Self::ChatSession => "ChatSession",
            Self::ChatTurn => "ChatTurn",
            Self::ChatMessage => "ChatMessage",
            Self::ChatContentBlock => "ChatContentBlock",
            Self::ChatCitation => "ChatCitation",
            Self::ChatDecision => "ChatDecision",
            Self::HarnessSession => "HarnessSession",
            Self::ChatCompletionRequest => "ChatCompletionRequest",
            Self::ChatRequestMessage => "ChatRequestMessage",
            Self::ChatContextAttachment => "ChatContextAttachment",
            Self::PendingProviderSubagent => "PendingProviderSubagent",
        }
    }
    fn kind(self) -> &'static str {
        match self {
            Self::Entity => "entities",
            Self::ChatSchedule => "chat_schedules",
            Self::ChatGoal => "chat_goals",
            Self::ChatTokenUsage => "chat_token_usage",
            Self::ChatSession => "chat_sessions",
            Self::ChatTurn => "chat_turns",
            Self::ChatMessage => "chat_messages",
            Self::ChatContentBlock => "chat_content_blocks",
            Self::ChatCitation => "chat_citations",
            Self::ChatDecision => "chat_decisions",
            Self::HarnessSession => "harness_sessions",
            Self::ChatCompletionRequest => "chat_completion_requests",
            Self::ChatRequestMessage => "chat_request_messages",
            Self::ChatContextAttachment => "chat_context_attachments",
            Self::PendingProviderSubagent => "pending_provider_subagents",
        }
    }
    fn fields(self) -> &'static [Field] {
        match self {
            Self::Entity => &FIELDS[..4],
            Self::ChatSchedule => &FIELDS,
            Self::ChatGoal => &goal::FIELDS,
            Self::ChatTokenUsage => &goal::USAGE_FIELDS,
            Self::ChatSession => &session::FIELDS,
            Self::ChatTurn => &turn::FIELDS,
            Self::ChatMessage => &fork::MESSAGE_FIELDS,
            Self::ChatContentBlock => &fork::BLOCK_FIELDS,
            Self::ChatCitation => &fork::CITATION_FIELDS,
            Self::ChatDecision => &fork::DECISION_FIELDS,
            Self::HarnessSession => &fork::HARNESS_FIELDS,
            Self::ChatCompletionRequest => &completion::FIELDS,
            Self::ChatRequestMessage => &completion::MESSAGE_FIELDS,
            Self::ChatContextAttachment => &completion::ATTACHMENT_FIELDS,
            Self::PendingProviderSubagent => &completion::PENDING_FIELDS,
        }
    }
    fn is_entity(self) -> bool {
        !matches!(
            self,
            Self::ChatTokenUsage
                | Self::ChatContentBlock
                | Self::ChatCitation
                | Self::ChatCompletionRequest
                | Self::ChatRequestMessage
                | Self::ChatContextAttachment
                | Self::PendingProviderSubagent
        )
    }
}

/// Trusted constructor factory outputs, separate from original keyword inputs.
/// `id` is supplied only for a source constructor that omitted its UUID field.
#[derive(Clone)]
pub struct CreatedEntityDefaults {
    pub id: Option<String>,
    pub created_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    pub last_activity_at: Option<DateTime<Utc>>,
}

/// Existing nested Python model instances supplied by a trusted clone caller.
/// Their field values have already been validated; only model-after hooks rerun.
#[derive(Clone, PartialEq, Serialize)]
pub struct TypedModelPath {
    pub path: Vec<Location>,
    pub model: Model,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum InputOrigin {
    RetainedJson,
    /// The caller merged model_dump(mode="python") and trusted writer values.
    /// Known datetime strings stand for datetime objects in diagnostic inputs.
    WriterModelDump,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(untagged)]
pub enum Location {
    Field(String),
    Index(usize),
}

#[derive(Clone, PartialEq, Serialize)]
struct Issue {
    kind: &'static str,
    loc: Vec<Location>,
    msg: String,
    /// Empty references the whole shared input; nested inputs share that owner.
    input_path: Vec<Location>,
    ctx: Option<Value>,
}

/// A borrowed wire view. Formatting this view implicitly would reveal input,
/// so it deliberately has no Debug implementation.
#[derive(Serialize)]
pub struct ValidationIssueRef<'a> {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub loc: &'a [Location],
    pub msg: &'a str,
    pub input: &'a Value,
    #[serde(skip)]
    pub input_path: &'a [Location],
    #[serde(skip_serializing_if = "Option::is_none")]
    pub ctx: Option<&'a Value>,
}

#[derive(Clone, PartialEq)]
struct InputProvenance {
    datetime_fields: Vec<&'static str>,
    typed_models: Vec<TypedModelPath>,
}

#[derive(Clone, PartialEq)]
pub struct ValidationReport {
    model: Model,
    origin: InputOrigin,
    input: Arc<Value>,
    input_order: Arc<[String]>,
    nested_order: Arc<Vec<(Vec<Location>, Vec<String>)>>,
    provenance: Arc<InputProvenance>,
    issues: Vec<Issue>,
    retained_bytes: usize,
}
impl fmt::Debug for ValidationReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("ValidationReport")
            .field("model", &self.model.name())
            .field("issues", &self.issues.len())
            .finish_non_exhaustive()
    }
}
impl fmt::Display for ValidationReport {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "retained {} failed {} model validation checks",
            self.model.name(),
            self.issues.len()
        )
    }
}
impl Serialize for ValidationReport {
    fn serialize<S: Serializer>(&self, serializer: S) -> std::result::Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(self.len()))?;
        for issue in self.issues() {
            seq.serialize_element(&issue)?;
        }
        seq.end()
    }
}
impl ValidationReport {
    pub const fn model_name(&self) -> &'static str {
        self.model.name()
    }
    pub const fn input_origin(&self) -> InputOrigin {
        self.origin
    }
    pub fn input_order(&self) -> &[String] {
        &self.input_order
    }
    pub fn input_order_at(&self, path: &[Location]) -> Option<&[String]> {
        if path.is_empty() {
            return Some(self.input_order());
        }
        self.nested_order
            .binary_search_by(|(at, _)| at.as_slice().cmp(path))
            .ok()
            .map(|index| self.nested_order[index].1.as_slice())
    }
    pub fn model_input_at(&self, path: &[Location]) -> Option<Model> {
        self.provenance
            .typed_models
            .binary_search_by(|entry| entry.path.as_slice().cmp(path))
            .ok()
            .map(|index| self.provenance.typed_models[index].model)
    }
    /// Only the trusted writer entry point supplies typed datetime values.
    /// Ordinary ISO-looking strings in retained JSON or metadata are not typed.
    pub fn is_datetime_input(&self, field: &str) -> bool {
        self.provenance.datetime_fields.contains(&field)
    }
    pub fn len(&self) -> usize {
        self.issues.len()
    }
    pub fn is_empty(&self) -> bool {
        self.issues.is_empty()
    }
    /// Shared input plus encoded issue metadata, for outer aggregate budgets.
    pub const fn retained_bytes(&self) -> usize {
        self.retained_bytes
    }
    pub fn issues(&self) -> impl ExactSizeIterator<Item = ValidationIssueRef<'_>> {
        self.issues.iter().map(|issue| ValidationIssueRef {
            kind: issue.kind,
            loc: &issue.loc,
            msg: &issue.msg,
            input: input_at(&self.input, &issue.input_path),
            input_path: &issue.input_path,
            ctx: issue.ctx.as_ref(),
        })
    }
    pub fn to_json_bytes(&self, limit: usize) -> Result<Vec<u8>> {
        let mut output = Limited::new(Vec::new(), limit.min(MAX_RECORD_BYTES));
        serde_json::to_writer(&mut output, self).map_err(|_| RecordError::TooLarge)?;
        Ok(output.inner)
    }
    fn add(
        &mut self,
        kind: &'static str,
        field: Option<&str>,
        missing: bool,
        msg: String,
        ctx: Option<Value>,
    ) -> Result<()> {
        if self.issues.len() >= MAX_ISSUES {
            return Err(RecordError::TooLarge);
        }
        let loc = field.map_or_else(Vec::new, |field| vec![Location::Field(field.into())]);
        let input_path = if missing { Vec::new() } else { loc.clone() };
        self.add_at(kind, loc, input_path, msg, ctx)
    }
    fn add_at(
        &mut self,
        kind: &'static str,
        loc: Vec<Location>,
        input_path: Vec<Location>,
        msg: String,
        ctx: Option<Value>,
    ) -> Result<()> {
        if self.issues.len() >= MAX_ISSUES {
            return Err(RecordError::TooLarge);
        }
        let issue = Issue {
            kind,
            loc,
            msg,
            input_path,
            ctx,
        };
        let bytes = encoded_len(&issue, MAX_RECORD_BYTES)?;
        self.retained_bytes = self.retained_bytes.saturating_add(bytes);
        if self.retained_bytes > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        self.issues.push(issue);
        Ok(())
    }
    fn error(self) -> Result<Value> {
        // The same large input may occur in many missing-field errors. Count
        // the wire representation before it can escape, without cloning it.
        encoded_len(&self, MAX_RECORD_BYTES)?;
        Err(RecordError::ModelValidation(self))
    }
}
fn input_at<'a>(root: &'a Value, path: &[Location]) -> &'a Value {
    path.iter().fold(root, |value, part| match part {
        Location::Field(key) => &value[key],
        Location::Index(index) => &value[*index],
    })
}

struct Limited<W> {
    inner: W,
    written: usize,
    limit: usize,
}
impl<W> Limited<W> {
    fn new(inner: W, limit: usize) -> Self {
        Self {
            inner,
            written: 0,
            limit,
        }
    }
}
impl<W: Write> Write for Limited<W> {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        if bytes.len() > self.limit.saturating_sub(self.written) {
            return Err(std::io::Error::other("retained validation byte limit"));
        }
        self.inner.write_all(bytes)?;
        self.written += bytes.len();
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.inner.flush()
    }
}
fn encoded_len(value: &impl Serialize, limit: usize) -> Result<usize> {
    let mut output = Limited::new(std::io::sink(), limit);
    serde_json::to_writer(&mut output, value).map_err(|_| RecordError::TooLarge)?;
    Ok(output.written)
}

struct Keys(Vec<String>);
impl<'de> Deserialize<'de> for Keys {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> std::result::Result<Self, D::Error> {
        struct KeyVisitor;
        impl<'de> Visitor<'de> for KeyVisitor {
            type Value = Keys;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("retained model dictionary")
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut access: A,
            ) -> std::result::Result<Keys, A::Error> {
                let mut keys = Vec::new();
                let mut seen = HashSet::new();
                let mut count = 0;
                while let Some((key, _)) = access.next_entry::<String, &'de RawValue>()? {
                    count += 1;
                    if count > MAX_ISSUES {
                        return Err(serde::de::Error::custom("retained model entry limit"));
                    }
                    // JSON duplicate keys take their last value, but Python
                    // dictionaries keep the first insertion position.
                    if seen.insert(key.clone()) {
                        keys.push(key);
                    }
                }
                Ok(Keys(keys))
            }
        }
        deserializer.deserialize_map(KeyVisitor)
    }
}

#[derive(Clone, Copy)]
enum FieldType {
    Request(completion::RequestField),
    String {
        min: usize,
        max: Option<usize>,
    },
    Integer {
        min: i64,
        max: Option<i64>,
    },
    AnyInteger,
    Timestamp {
        schedule: bool,
    },
    Boolean,
    Archive,
    OptionalTime,
    Float,
    GoalStatus,
    ChatBackend,
    Strings {
        min: usize,
        max: usize,
    },
    Dictionaries {
        max: usize,
    },
    Dictionary,
    Usage,
    Models {
        model: Model,
        max: Option<usize>,
    },
    Enum(&'static [&'static str]),
    Literal(&'static [&'static str]),
    Pattern {
        values: &'static [&'static str],
        pattern: &'static str,
    },
}
#[derive(Clone, Copy)]
struct Field {
    name: &'static str,
    kind: FieldType,
    nullable: bool,
}
const fn text(name: &'static str, min: usize, max: Option<usize>, nullable: bool) -> Field {
    Field {
        name,
        kind: FieldType::String { min, max },
        nullable,
    }
}
const fn timestamp(name: &'static str, schedule: bool, nullable: bool) -> Field {
    Field {
        name,
        kind: FieldType::Timestamp { schedule },
        nullable,
    }
}
// Source Entity.model_fields order, followed by ChatSchedule.model_fields.
const FIELDS: [Field; 16] = [
    text("id", 1, Some(200), false),
    timestamp("created_at", false, false),
    timestamp("updated_at", false, false),
    Field {
        name: "revision",
        kind: FieldType::Integer { min: 1, max: None },
        nullable: false,
    },
    text("engagement_id", 0, None, false),
    text("session_id", 0, None, false),
    text("provider_profile_id", 0, None, false),
    text("model", 0, None, false),
    Field {
        name: "interval_seconds",
        kind: FieldType::Integer {
            min: 3600,
            max: Some(2_592_000),
        },
        nullable: false,
    },
    timestamp("next_run_at", true, false),
    Field {
        name: "enabled",
        kind: FieldType::Boolean,
        nullable: false,
    },
    Field {
        name: "paused_by",
        kind: FieldType::Archive,
        nullable: true,
    },
    timestamp("last_run_at", true, true),
    text("last_turn_id", 0, Some(200), true),
    text("last_status", 0, Some(40), true),
    text("skip_reason", 0, Some(1000), true),
];

struct Failure {
    kind: &'static str,
    msg: String,
    ctx: Option<Value>,
}
impl Failure {
    fn simple(kind: &'static str, msg: impl Into<String>) -> Self {
        Self {
            kind,
            msg: msg.into(),
            ctx: None,
        }
    }
    fn context(kind: &'static str, msg: impl Into<String>, ctx: Value) -> Self {
        Self {
            kind,
            msg: msg.into(),
            ctx: Some(ctx),
        }
    }
    fn value(msg: &'static str) -> Self {
        Self::context(
            "value_error",
            format!("Value error, {msg}"),
            json!({"error":{}}),
        )
    }
}
type FieldResult = std::result::Result<Value, Failure>;

/// Hydrate one of the complete retained contracts, preserving field-error order
/// and original diagnostic input. Missing factory-backed identity/time fields
/// remain a strict retained-data error even though Python can invent defaults.
pub fn hydrate(model: Model, origin: InputOrigin, bytes: &[u8]) -> Result<Value> {
    hydrate_inner(model, origin, bytes, None)
}

/// Trusted constructor factories are supplied separately so error inputs remain
/// the caller's original keyword arguments, without invented fields.
pub fn hydrate_created_goal(
    bytes: &[u8],
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
) -> Result<Value> {
    hydrate_created_entity(Model::ChatGoal, bytes, created_at, updated_at)
}

pub(crate) fn hydrate_created_session(
    bytes: &[u8],
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
) -> Result<Value> {
    hydrate_created_entity(Model::ChatSession, bytes, created_at, updated_at)
}

fn hydrate_created_entity(
    model: Model,
    bytes: &[u8],
    created_at: DateTime<Utc>,
    updated_at: DateTime<Utc>,
) -> Result<Value> {
    let defaults = json!({"created_at":created_at.to_rfc3339_opts(SecondsFormat::Micros,true),"updated_at":updated_at.to_rfc3339_opts(SecondsFormat::Micros,true),"revision":1});
    hydrate_inner(model, InputOrigin::RetainedJson, bytes, Some(&defaults))
}

fn hydrate_inner(
    model: Model,
    origin: InputOrigin,
    bytes: &[u8],
    factory_defaults: Option<&Value>,
) -> Result<Value> {
    hydrate_context(model, origin, bytes, factory_defaults, &[], None)
}

/// Fork constructors supply every trusted factory result outside their original
/// kwargs. Nested model provenance is restricted to the three clone input types.
pub fn hydrate_fork_created(
    model: Model,
    bytes: &[u8],
    defaults: &CreatedEntityDefaults,
    typed_paths: &[TypedModelPath],
) -> Result<Value> {
    if !matches!(
        model,
        Model::ChatSession
            | Model::ChatMessage
            | Model::ChatDecision
            | Model::ChatGoal
            | Model::HarnessSession
    ) {
        return Err(RecordError::UnknownKind);
    }
    hydrate_created_with_defaults(model, bytes, defaults, typed_paths)
}

/// Turn construction with explicit factory outputs and optional validated Usage
/// provenance. Retained reads never gain access to these factory defaults.
pub fn hydrate_created_turn(
    bytes: &[u8],
    defaults: &CreatedEntityDefaults,
    typed_paths: &[TypedModelPath],
) -> Result<Value> {
    if defaults.last_activity_at.is_some() {
        return Err(RecordError::Invariant("Turn has no last-activity factory"));
    }
    hydrate_created_with_defaults(Model::ChatTurn, bytes, defaults, typed_paths)
}

fn hydrate_created_with_defaults(
    model: Model,
    bytes: &[u8],
    defaults: &CreatedEntityDefaults,
    typed_paths: &[TypedModelPath],
) -> Result<Value> {
    let mut factories = json!({"created_at":defaults.created_at.to_rfc3339_opts(SecondsFormat::Micros,true),"updated_at":defaults.updated_at.to_rfc3339_opts(SecondsFormat::Micros,true),"revision":1});
    if let Some(id) = &defaults.id {
        if id.len() > MAX_RECORD_BYTES {
            return Err(RecordError::TooLarge);
        }
        factories["id"] = id.clone().into();
    }
    if let Some(time) = defaults.last_activity_at {
        factories["last_activity_at"] = time.to_rfc3339_opts(SecondsFormat::Micros, true).into();
    }
    hydrate_context(
        model,
        InputOrigin::RetainedJson,
        bytes,
        Some(&factories),
        typed_paths,
        None,
    )
}

/// Direct retained HarnessSession hydration with an explicitly supplied clock.
/// Missing last_activity_at is sampled at its declared field, including when
/// preceding independent fields already produced diagnostics.
pub fn hydrate_harness_session(
    bytes: &[u8],
    environment: &mut dyn crate::dependencies::DependencyEnvironment,
) -> Result<Value> {
    hydrate_context(
        Model::HarnessSession,
        InputOrigin::RetainedJson,
        bytes,
        None,
        &[],
        Some(environment),
    )
}

fn hydrate_context(
    model: Model,
    origin: InputOrigin,
    bytes: &[u8],
    factory_defaults: Option<&Value>,
    typed_paths: &[TypedModelPath],
    mut environment: Option<&mut dyn crate::dependencies::DependencyEnvironment>,
) -> Result<Value> {
    if bytes.len() > MAX_RECORD_BYTES {
        return Err(RecordError::TooLarge);
    }
    let mut input: Value = serde_json::from_slice(bytes).map_err(|_| RecordError::Json)?;
    if input.is_object() && model.is_entity() {
        for field in ["id", "revision", "created_at", "updated_at"] {
            if input.get(field).is_none() && factory_defaults.and_then(|v| v.get(field)).is_none() {
                return Err(RecordError::Shape(model.kind()));
            }
        }
    }
    let mut keys = if input.is_object() {
        serde_json::from_slice::<Keys>(bytes)
            .map_err(|_| RecordError::TooLarge)?
            .0
    } else {
        Vec::new()
    };
    let datetime_fields = if origin == InputOrigin::WriterModelDump {
        let datetime_fields = writer_datetime_inputs(model, &mut input);
        let mut ordered: Vec<String> = model
            .fields()
            .iter()
            .filter(|field| input.get(field.name).is_some())
            .map(|field| field.name.into())
            .collect();
        ordered.extend(
            keys.into_iter()
                .filter(|key| !model.fields().iter().any(|field| field.name == key)),
        );
        keys = ordered;
        datetime_fields
    } else {
        Vec::new()
    };
    let input_bytes = encoded_len(&input, MAX_RECORD_BYTES)?;
    let base_bytes = input_bytes
        .saturating_add(encoded_len(&keys, MAX_RECORD_BYTES)?)
        .saturating_add(encoded_len(&datetime_fields, MAX_RECORD_BYTES)?);
    let mut nested_order = if !matches!(model, Model::Entity | Model::ChatSchedule) {
        goal::nested_orders(bytes, &input, MAX_RECORD_BYTES.saturating_sub(base_bytes))?
    } else {
        Vec::new()
    };
    if typed_paths.len() > MAX_ISSUES {
        return Err(RecordError::TooLarge);
    }
    let typed_bytes = encoded_len(&typed_paths, MAX_RECORD_BYTES)?;
    let mut typed_models = typed_paths.to_vec();
    typed_models.sort_unstable_by(|left, right| left.path.cmp(&right.path));
    for (index, entry) in typed_models.iter().enumerate() {
        let clone_field = matches!(model, Model::ChatMessage | Model::ChatTurn)
            && match (entry.model, entry.path.as_slice()) {
                (Model::ChatTokenUsage, [Location::Field(field)]) => field == "usage",
                (Model::ChatContentBlock, [Location::Field(field), Location::Index(_)]) => {
                    model == Model::ChatMessage && field == "content_blocks"
                }
                (Model::ChatCitation, [Location::Field(field), Location::Index(_)]) => {
                    model == Model::ChatMessage && field == "citations"
                }
                _ => false,
            };
        if !matches!(
            entry.model,
            Model::ChatTokenUsage | Model::ChatContentBlock | Model::ChatCitation
        ) || entry.path.is_empty()
            || !clone_field
            || index > 0 && typed_models[index - 1].path == entry.path
        {
            return Err(RecordError::Invariant("invalid clone model provenance"));
        }
        let Some(object) = input_at(&input, &entry.path).as_object() else {
            return Err(RecordError::Invariant(
                "clone model input must be a validated object",
            ));
        };
        if object.len() != entry.model.fields().len()
            || entry
                .model
                .fields()
                .iter()
                .any(|field| !object.contains_key(field.name))
        {
            return Err(RecordError::Invariant(
                "clone model input must contain canonical fields",
            ));
        }
        let order = entry
            .model
            .fields()
            .iter()
            .map(|field| field.name.to_owned())
            .collect();
        match nested_order.binary_search_by(|(path, _)| path.cmp(&entry.path)) {
            Ok(index) => nested_order[index].1 = order,
            Err(_) => {
                return Err(RecordError::Invariant(
                    "clone model order metadata is missing",
                ));
            }
        }
    }
    let retained_bytes = base_bytes
        .saturating_add(encoded_len(&nested_order, MAX_RECORD_BYTES)?)
        .saturating_add(typed_bytes);
    if retained_bytes > MAX_RECORD_BYTES {
        return Err(RecordError::TooLarge);
    }
    let keys: Arc<[String]> = keys.into();
    let input = Arc::new(input);
    let mut report = ValidationReport {
        model,
        origin,
        input: input.clone(),
        input_order: keys.clone(),
        nested_order: Arc::new(nested_order),
        provenance: Arc::new(InputProvenance {
            datetime_fields,
            typed_models,
        }),
        issues: Vec::new(),
        retained_bytes,
    };
    let Some(fields) = input.as_object() else {
        report.add(
            "model_type",
            None,
            false,
            format!(
                "Input should be a valid dictionary or instance of {}",
                model.name()
            ),
            Some(json!({"class_name":model.name()})),
        )?;
        return report.error();
    };
    let mut output = Map::new();
    for field in model.fields() {
        let Some(value) = fields
            .get(field.name)
            .or_else(|| factory_defaults.and_then(|v| v.get(field.name)))
        else {
            if model == Model::HarnessSession && field.name == "last_activity_at" {
                let Some(environment) = environment.as_deref_mut() else {
                    return Err(RecordError::Shape(model.kind()));
                };
                let time = environment.now();
                output.insert(
                    field.name.into(),
                    time.to_rfc3339_opts(
                        if time.timestamp_subsec_micros() == 0 {
                            SecondsFormat::Secs
                        } else {
                            SecondsFormat::Micros
                        },
                        true,
                    )
                    .into(),
                );
            } else if let Some(value) = session::default(model, *field)
                .or_else(|| goal::default(model, *field))
                .or_else(|| fork::default(model, *field))
                .or_else(|| completion::default(model, *field))
                .or_else(|| turn::default(model, *field))
            {
                output.insert(field.name.into(), value);
            } else if field.name == "enabled" {
                output.insert(field.name.into(), true.into());
            } else if field.nullable {
                output.insert(field.name.into(), Value::Null);
            } else {
                report.add(
                    "missing",
                    Some(field.name),
                    true,
                    "Field required".into(),
                    None,
                )?;
            }
            continue;
        };
        if let Some(value) = goal::validate(
            *field,
            value,
            vec![Location::Field(field.name.into())],
            &mut report,
        )? {
            output.insert(field.name.into(), value);
        }
    }
    for key in keys.iter() {
        if !model.fields().iter().any(|field| field.name == key) {
            report.add(
                "extra_forbidden",
                Some(key),
                false,
                "Extra inputs are not permitted".into(),
                None,
            )?;
        }
    }
    if !report.is_empty() {
        return report.error();
    }
    if model.is_entity() {
        let created =
            DateTime::parse_from_rfc3339(output["created_at"].as_str().ok_or(RecordError::Schema)?)
                .map_err(|_| RecordError::Schema)?;
        let updated =
            DateTime::parse_from_rfc3339(output["updated_at"].as_str().ok_or(RecordError::Schema)?)
                .map_err(|_| RecordError::Schema)?;
        if updated < created {
            let error = Failure::value("updated_at cannot be earlier than created_at");
            report.add(error.kind, None, false, error.msg, error.ctx)?;
            return report.error();
        }
    }
    if model == Model::ChatGoal
        && let Some(message) = goal::coherence(&output)
    {
        let error = Failure::value(message);
        report.add(error.kind, None, false, error.msg, error.ctx)?;
        return report.error();
    }
    if model == Model::ChatSession
        && let Some(message) = session::coherence(&output)
    {
        let error = Failure::value(message);
        report.add(error.kind, None, false, error.msg, error.ctx)?;
        return report.error();
    }
    if let Some(message) = fork::coherence(model, &output) {
        let error = Failure::value(message);
        report.add(error.kind, None, false, error.msg, error.ctx)?;
        return report.error();
    }
    if model == Model::ChatTurn
        && let Some(message) = turn::coherence(&output)
    {
        let error = Failure::value(message);
        report.add(error.kind, None, false, error.msg, error.ctx)?;
        return report.error();
    }
    if let Some(message) = completion::coherence(model, &output) {
        let error = Failure::value(message);
        report.add(error.kind, None, false, error.msg, error.ctx)?;
        return report.error();
    }
    if model == Model::ChatGoal && output["elapsed_seconds"].is_null() {
        return Err(RecordError::Invariant(
            "nonfinite Goal elapsed value cannot be retained as canonical JSON",
        ));
    }
    let output = Value::Object(output);
    encoded_len(&output, MAX_RECORD_BYTES)?;
    Ok(output)
}

fn writer_datetime_inputs(model: Model, input: &mut Value) -> Vec<&'static str> {
    let mut typed = Vec::new();
    if !input.is_object() {
        return typed;
    }
    for field in model.fields().iter().filter(|f| {
        matches!(
            f.kind,
            FieldType::Timestamp { .. } | FieldType::OptionalTime
        )
    }) {
        let Some(value) = input.get_mut(field.name) else {
            continue;
        };
        let Some(text) = value.as_str() else {
            continue;
        };
        let normalized = if let Ok(time) = DateTime::parse_from_rfc3339(text) {
            Some(time.to_rfc3339_opts(
                if time.timestamp_subsec_micros() == 0 {
                    SecondsFormat::Secs
                } else {
                    SecondsFormat::Micros
                },
                false,
            ))
        } else if let Ok(time) = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f") {
            Some(
                time.format(if time.and_utc().timestamp_subsec_micros() == 0 {
                    "%Y-%m-%dT%H:%M:%S"
                } else {
                    "%Y-%m-%dT%H:%M:%S%.6f"
                })
                .to_string(),
            )
        } else {
            None
        };
        if let Some(normalized) = normalized {
            *value = normalized.into();
            typed.push(field.name);
        }
    }
    typed
}

fn validate_field(field: Field, value: &Value) -> FieldResult {
    if field.nullable && value.is_null() {
        return Ok(Value::Null);
    }
    match field.kind {
        FieldType::String { min, max } => {
            let Some(text) = value.as_str() else {
                return Err(Failure::simple(
                    "string_type",
                    "Input should be a valid string",
                ));
            };
            // Pydantic's str_strip_whitespace uses Unicode White_Space, unlike
            // Python str.strip which also strips U+001C through U+001F.
            let text = text.trim();
            let len = text.chars().count();
            if len < min {
                return Err(Failure::context(
                    "string_too_short",
                    format!(
                        "String should have at least {min} character{}",
                        if min == 1 { "" } else { "s" }
                    ),
                    json!({"min_length":min}),
                ));
            }
            if let Some(max) = max.filter(|max| len > *max) {
                return Err(Failure::context(
                    "string_too_long",
                    format!(
                        "String should have at most {max} character{}",
                        if max == 1 { "" } else { "s" }
                    ),
                    json!({"max_length":max}),
                ));
            }
            Ok(text.into())
        }
        FieldType::Integer { min, max } => {
            let number = integer(value)?;
            let text = number.to_string();
            if text.starts_with('-') || number.as_i64().is_some_and(|value| value < min) {
                return Err(Failure::context(
                    "greater_than_equal",
                    format!("Input should be greater than or equal to {min}"),
                    json!({"ge":min}),
                ));
            }
            if let Some(max) = max.filter(|max| number.as_i64().is_none_or(|value| value > *max)) {
                return Err(Failure::context(
                    "less_than_equal",
                    format!("Input should be less than or equal to {max}"),
                    json!({"le":max}),
                ));
            }
            Ok(number)
        }
        FieldType::Boolean => boolean(value).map(Value::Bool),
        FieldType::Archive => {
            if value == "archive" {
                Ok(value.clone())
            } else {
                Err(Failure::context(
                    "literal_error",
                    "Input should be 'archive'",
                    json!({"expected":"'archive'"}),
                ))
            }
        }
        FieldType::Timestamp { schedule } => {
            let parsed = datetime(value)?;
            let aware = DateTime::parse_from_rfc3339(&parsed).map_err(|_| {
                Failure::value(if schedule {
                    "schedule times must include a timezone"
                } else {
                    "timestamps must include a timezone"
                })
            })?;
            let utc = aware.with_timezone(&Utc);
            Ok(utc
                .to_rfc3339_opts(
                    if utc.timestamp_subsec_micros() == 0 {
                        SecondsFormat::Secs
                    } else {
                        SecondsFormat::Micros
                    },
                    true,
                )
                .into())
        }
        FieldType::OptionalTime | FieldType::Float | FieldType::GoalStatus => {
            goal::scalar(field.kind, value)
        }
        FieldType::ChatBackend => session::backend(value),
        FieldType::AnyInteger
        | FieldType::Enum(_)
        | FieldType::Literal(_)
        | FieldType::Pattern { .. } => fork::scalar(field.kind, value),
        FieldType::Strings { .. }
        | FieldType::Request(_)
        | FieldType::Dictionaries { .. }
        | FieldType::Dictionary
        | FieldType::Usage
        | FieldType::Models { .. } => {
            unreachable!("compound field validated through goal::validate")
        }
    }
}

/// Scalar coercion shared with the immutable contextual dependency codecs.
/// Errors remain sanitized; these storage reads do not expose direct reports.
pub(crate) fn dependency_scalar(schema: &Value, value: &Value) -> Result<Value> {
    let invalid = || RecordError::Invariant("retained dependency field is invalid");
    let kind = match schema["type"].as_str() {
        Some("string") if schema["format"] == "date-time" => {
            FieldType::Timestamp { schedule: false }
        }
        Some("string") if schema["enum"].is_array() => {
            return if schema["enum"].as_array().unwrap().contains(value) {
                Ok(value.clone())
            } else {
                Err(invalid())
            };
        }
        Some("string") => FieldType::String {
            min: schema["minLength"].as_u64().unwrap_or(0) as usize,
            max: schema["maxLength"].as_u64().map(|v| v as usize),
        },
        Some("integer") => FieldType::Integer {
            min: schema["minimum"].as_i64().unwrap_or(0),
            max: schema["maximum"].as_i64(),
        },
        Some("boolean") => FieldType::Boolean,
        _ => return Ok(value.clone()),
    };
    validate_field(
        Field {
            name: "dependency",
            kind,
            nullable: false,
        },
        value,
    )
    .map_err(|_| invalid())
}

fn boolean(value: &Value) -> std::result::Result<bool, Failure> {
    match value {
        Value::Bool(value) => return Ok(*value),
        Value::Number(number) => {
            // Pydantic extracts exact integer tokens as i64, while float-to-i64
            // conversion rejects both endpoints. Converting i64::MAX to f64
            // first would incorrectly classify its rounded value as overflow.
            let signed_integer = if number.to_string().contains(['.', 'e', 'E']) {
                number.as_f64().is_some_and(|value| {
                    value.is_finite()
                        && value.fract() == 0.0
                        && value > i64::MIN as f64
                        && value < i64::MAX as f64
                })
            } else {
                number.as_i64().is_some()
            };
            if !signed_integer {
                return Err(Failure::simple(
                    "bool_type",
                    "Input should be a valid boolean",
                ));
            }
            if number.as_f64() == Some(1.0) {
                return Ok(true);
            }
            if number.as_f64() == Some(0.0) {
                return Ok(false);
            }
        }
        Value::String(text) => match text.to_ascii_lowercase().as_str() {
            "1" | "true" | "t" | "yes" | "y" | "on" => return Ok(true),
            "0" | "false" | "f" | "no" | "n" | "off" => return Ok(false),
            _ => {}
        },
        _ => {
            return Err(Failure::simple(
                "bool_type",
                "Input should be a valid boolean",
            ));
        }
    }
    Err(Failure::simple(
        "bool_parsing",
        "Input should be a valid boolean, unable to interpret input",
    ))
}

fn integer(value: &Value) -> FieldResult {
    let parse = || {
        Failure::simple(
            "int_parsing",
            "Input should be a valid integer, unable to parse string as an integer",
        )
    };
    let size = || {
        Failure::simple(
            "int_parsing_size",
            "Unable to parse input string as an integer, exceeded maximum size",
        )
    };
    if let Value::Bool(value) = value {
        return Ok(json!(i64::from(*value)));
    }
    if let Value::Number(value) = value {
        if value.as_i64() == Some(0) || value.as_f64() == Some(0.0) {
            return Ok(json!(0));
        }
        let raw = value.to_string();
        if !raw.contains(['.', 'e', 'E']) {
            return Ok(Value::Number(value.clone()));
        }
        let number = value.as_f64().ok_or_else(size)?;
        if !number.is_finite() {
            return Err(Failure::simple(
                "finite_number",
                "Input should be a finite number",
            ));
        }
        if number.fract() != 0.0 {
            return Err(Failure::simple(
                "int_from_float",
                "Input should be a valid integer, got a number with a fractional part",
            ));
        }
        if number <= i64::MIN as f64 || number >= i64::MAX as f64 {
            return Err(size());
        }
        return format!("{number:.0}")
            .parse::<serde_json::Number>()
            .map(Value::Number)
            .map_err(|_| size());
    }
    let Some(raw) = value.as_str() else {
        return Err(Failure::simple(
            "int_type",
            "Input should be a valid integer",
        ));
    };
    if raw.len() > 4300 {
        return Err(size());
    }
    let mut text = raw.trim();
    if let Some(rest) = text.strip_prefix('+') {
        if rest.starts_with('-') {
            return Err(parse());
        }
        text = rest;
    }
    let negative = text.starts_with('-');
    if negative {
        text = &text[1..];
    }
    if text.is_empty() || !text.as_bytes()[0].is_ascii_digit() {
        return Err(parse());
    }
    while text.len() > 1
        && (text.starts_with('0') || text.starts_with('_'))
        && !text[1..].starts_with('.')
    {
        text = &text[1..];
    }
    if let Some((whole, fraction)) = text.split_once('.') {
        if fraction.is_empty() || !fraction.bytes().all(|b| b == b'0') {
            return Err(parse());
        }
        text = whole;
    }
    if text.starts_with('_')
        || text.ends_with('_')
        || text.contains("__")
        || !text.bytes().all(|b| b.is_ascii_digit() || b == b'_')
    {
        return Err(parse());
    }
    let digits = text.replace('_', "");
    let digits = digits.trim_start_matches('0');
    let canonical = if digits.is_empty() {
        "0".into()
    } else {
        format!("{}{digits}", if negative { "-" } else { "" })
    };
    canonical
        .parse::<serde_json::Number>()
        .map(Value::Number)
        .map_err(|_| parse())
}

fn datetime(value: &Value) -> std::result::Result<String, Failure> {
    let failure = |kind: &'static str, detail: Option<&'static str>| match detail {
        Some(detail) => Failure::context(
            kind,
            format!(
                "Input should be a valid datetime{}, {detail}",
                if kind == "datetime_from_date_parsing" {
                    " or date"
                } else {
                    ""
                }
            ),
            json!({"error":detail}),
        ),
        None => Failure::simple(kind, "Input should be a valid datetime"),
    };
    let config = DateTimeConfig::builder()
        .time_config(
            TimeConfig::builder()
                .unix_timestamp_offset(Some(0))
                .microseconds_precision_overflow_behavior(
                    MicrosecondsPrecisionOverflowBehavior::Truncate,
                )
                .build(),
        )
        .build();
    let parsed = match value {
        Value::String(text) => match ParsedDateTime::parse_str_with_config(text, &config) {
            Ok(date) => Ok(date),
            Err(_) => match Date::parse_str(text) {
                Ok(date) if date.year == 0 => {
                    return Err(failure("datetime_parsing", Some("year 0 is out of range")));
                }
                Ok(date) => return Ok(format!("{date}T00:00:00")),
                Err(error) => {
                    return Err(failure(
                        "datetime_from_date_parsing",
                        error.get_documentation(),
                    ));
                }
            },
        },
        Value::Number(number) => ParsedDateTime::from_float_with_config(
            number.as_f64().unwrap_or(f64::INFINITY),
            &config,
        ),
        _ => return Err(failure("datetime_type", None)),
    };
    parsed
        .map_err(|error| failure("datetime_parsing", error.get_documentation()))
        .and_then(|date| {
            if date.date.year == 0 {
                Err(failure("datetime_parsing", Some("year 0 is out of range")))
            } else {
                Ok(date.to_string())
            }
        })
}
