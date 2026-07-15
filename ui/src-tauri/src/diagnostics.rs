//! Native Nebula 3 diagnostics owner.
//!
//! The desktop owns `desktop.log`, `interface.log`, and the aggregate
//! `errors.log`. Core errors arrive as already-sanitized JSON frames on the
//! supervised stderr channel and are copied without adding payload fields.

use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
    time::{Duration, SystemTime},
};

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use tauri::{AppHandle, State};
use tauri_plugin_opener::OpenerExt;
use time::{OffsetDateTime, format_description::well_known::Rfc3339};

pub(crate) const ERROR_MIRROR_PREFIX: &str = "NEBULA_DIAGNOSTIC_ERROR ";
const RECORD_SCHEMA: &str = "nebula.diagnostic/v1";
const SETTINGS_SCHEMA: &str = "nebula.diagnostics-settings/v1";
const MAX_FILE_BYTES: u64 = 5 * 1024 * 1024;
const MAX_ROTATIONS: usize = 2;
const MAX_ROTATION_AGE: Duration = Duration::from_secs(14 * 24 * 60 * 60);
const MAX_DIRECTORY_BYTES: u64 = 256 * 1024 * 1024;
const MAX_STRING_BYTES: usize = 2_048;
const MAX_METADATA_ITEMS: usize = 64;
const MAX_METADATA_DEPTH: usize = 5;
const MAX_RECENT_ERRORS: usize = 500;

const FEATURES: &[&str] = &[
    "desktop",
    "interface",
    "api",
    "setup",
    "storage",
    "projects",
    "terminal",
    "terminal-audit",
    "workspace",
    "notes",
    "capture",
    "providers",
    "chat",
    "knowledge",
    "harnesses",
    "missions",
    "toolbox",
    "sandbox",
    "executions",
    "findings",
    "evidence",
    "reports",
    "diagnostics",
];

const SAFE_METADATA_KEYS: &[&str] = &[
    "action",
    "adapter",
    "attempt",
    "available",
    "backend",
    "batch_count",
    "byte_count",
    "capability",
    "category",
    "chunk_count",
    "code",
    "collection_count",
    "component",
    "connection_state",
    "count",
    "current_revision",
    "decision",
    "digest",
    "direction",
    "disk_bytes",
    "dropped_count",
    "entity_count",
    "entity_id",
    "entity_type",
    "expected_revision",
    "feature",
    "fingerprint",
    "format",
    "health",
    "http_status",
    "image_digest",
    "installed",
    "item_count",
    "kind",
    "limit",
    "method",
    "mode",
    "model_id",
    "operation",
    "origin",
    "policy",
    "port_class",
    "provider",
    "queue_depth",
    "reason_code",
    "record_count",
    "recovered_count",
    "result",
    "retry_count",
    "revision",
    "route",
    "runner",
    "sequence_end",
    "sequence_start",
    "size_class",
    "state",
    "status",
    "step",
    "target_fingerprint",
    "task_count",
    "timeout_seconds",
    "tool_id",
    "transport",
    "truncated",
    "validation",
    "vendor_request_id",
    "version",
    "warning_count",
];

const DENIED_KEY_PARTS: &[&str] = &[
    "secret",
    "credential",
    "authorization",
    "cookie",
    "header",
    "body",
    "prompt",
    "content",
    "source",
    "command",
    "argv",
    "stdout",
    "stderr",
    "document",
    "terminal_bytes",
    "terminal_output",
    "evidence_bytes",
    "private_key",
    "password",
    "passwd",
    "api_key",
    "access_token",
    "refresh_token",
    "filename",
    "file_path",
    "path",
    "query",
    "sql",
    "payload",
    "selected_text",
];

#[derive(Debug, Clone, Copy, Default, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "lowercase")]
pub(crate) enum DiagnosticLevel {
    Debug,
    Info,
    Warning,
    #[default]
    Error,
    Critical,
}

impl DiagnosticLevel {
    fn label(self) -> &'static str {
        match self {
            Self::Debug => "DEBUG",
            Self::Info => "INFO",
            Self::Warning => "WARNING",
            Self::Error => "ERROR",
            Self::Critical => "CRITICAL",
        }
    }

    fn parse(value: &str) -> Result<Self, String> {
        match value.to_ascii_lowercase().as_str() {
            "debug" => Ok(Self::Debug),
            "info" => Ok(Self::Info),
            "warning" => Ok(Self::Warning),
            "error" => Ok(Self::Error),
            "critical" => Ok(Self::Critical),
            _ => Err("unsupported diagnostics level".to_string()),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct DiagnosticSettings {
    schema: String,
    global_level: DiagnosticLevel,
    feature_levels: BTreeMap<String, DiagnosticLevel>,
}

impl Default for DiagnosticSettings {
    fn default() -> Self {
        Self {
            schema: SETTINGS_SCHEMA.to_string(),
            global_level: DiagnosticLevel::Error,
            feature_levels: BTreeMap::new(),
        }
    }
}

impl DiagnosticSettings {
    fn validate(&self) -> Result<(), String> {
        if self.schema != SETTINGS_SCHEMA {
            return Err("unsupported diagnostics settings schema".to_string());
        }
        if self.feature_levels.len() > FEATURES.len()
            || self
                .feature_levels
                .keys()
                .any(|feature| !FEATURES.contains(&feature.as_str()))
        {
            return Err("diagnostics settings contain an unknown feature".to_string());
        }
        Ok(())
    }

    fn effective_level(
        &self,
        feature: &str,
        process_override: Option<DiagnosticLevel>,
    ) -> DiagnosticLevel {
        process_override
            .or_else(|| self.feature_levels.get(feature).copied())
            .unwrap_or(self.global_level)
    }
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FrontendStackFrame {
    module: String,
    function: String,
    line: u32,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct FrontendDiagnosticRecord {
    schema: String,
    level: DiagnosticLevel,
    feature: String,
    event_code: String,
    message: String,
    request_id: Option<String>,
    operation_id: Option<String>,
    parent_operation_id: Option<String>,
    error_id: Option<String>,
    project_id: Option<String>,
    run_id: Option<String>,
    execution_id: Option<String>,
    session_id: Option<String>,
    outcome: Option<String>,
    stage: Option<String>,
    duration_ms: Option<f64>,
    retryable: Option<bool>,
    safe_failure_cause: Option<String>,
    exception_type: Option<String>,
    #[serde(default)]
    stack_frames: Vec<FrontendStackFrame>,
    #[serde(default)]
    metadata: Map<String, Value>,
}

impl FrontendDiagnosticRecord {
    fn validate(&self) -> Result<(), String> {
        if self.schema != RECORD_SCHEMA || self.feature != "interface" {
            return Err("unsupported frontend diagnostic record".to_string());
        }
        if !valid_event_code(&self.event_code)
            || self.message.is_empty()
            || self.message.len() > MAX_STRING_BYTES
            || self.stack_frames.len() > 32
            || self.metadata.len() > MAX_METADATA_ITEMS
            || self
                .duration_ms
                .is_some_and(|value| !value.is_finite() || !(0.0..=86_400_000.0).contains(&value))
        {
            return Err("invalid frontend diagnostic record".to_string());
        }
        for value in [
            self.request_id.as_deref(),
            self.operation_id.as_deref(),
            self.parent_operation_id.as_deref(),
            self.error_id.as_deref(),
            self.project_id.as_deref(),
            self.run_id.as_deref(),
            self.execution_id.as_deref(),
            self.session_id.as_deref(),
        ] {
            if value.is_some_and(|identifier| !valid_identifier(identifier)) {
                return Err("invalid frontend diagnostic correlation identifier".to_string());
            }
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct DiagnosticFile {
    name: String,
    size_bytes: u64,
    modified_at: String,
}

struct Diagnostics {
    log_dir: PathBuf,
    settings_path: PathBuf,
    settings: DiagnosticSettings,
    process_override: Option<DiagnosticLevel>,
    process_override_invalid: bool,
    launch_id: String,
    sequence: u64,
    degraded: bool,
    last_failure: Option<String>,
    last_rotation: Option<String>,
    dropped_records: u64,
    memory_errors: VecDeque<Value>,
}

#[derive(Clone, Default)]
pub(crate) struct DiagnosticsState {
    inner: Arc<Mutex<Option<Diagnostics>>>,
}

impl DiagnosticsState {
    pub(crate) fn initialize(&self, app_data_dir: &Path) -> Result<(), String> {
        let initialized = (|| {
            let mut manager = Diagnostics::new(app_data_dir)?;
            let settings_invalid = manager.load_settings().is_err();
            if settings_invalid {
                manager.settings = DiagnosticSettings::default();
                manager.atomic_write_settings()?;
            }
            manager.record(
                DiagnosticLevel::Info,
                "desktop",
                "desktop.diagnostics.initialized",
                "Native local diagnostics initialized.",
                DiagnosticFields {
                    outcome: Some("success"),
                    ..DiagnosticFields::default()
                },
            )?;
            if settings_invalid {
                manager.record(
                    DiagnosticLevel::Error,
                    "desktop",
                    "desktop.diagnostics.settings_invalid",
                    "Diagnostics preferences were unreadable; Error logging is active.",
                    DiagnosticFields {
                        outcome: Some("fallback"),
                        stage: Some("settings-load"),
                        retryable: Some(true),
                        ..DiagnosticFields::default()
                    },
                )?;
            }
            if manager.process_override_invalid {
                manager.record(
                    DiagnosticLevel::Error,
                    "desktop",
                    "desktop.diagnostics.process_override_invalid",
                    "A process-only diagnostics override was invalid; saved preferences are active.",
                    DiagnosticFields {
                        outcome: Some("fallback"),
                        stage: Some("configuration"),
                        retryable: Some(false),
                        ..DiagnosticFields::default()
                    },
                )?;
            }
            Ok::<Diagnostics, String>(manager)
        })();
        let (manager, failure) = match initialized {
            Ok(manager) => (manager, None),
            Err(error) => (Diagnostics::memory_only(app_data_dir), Some(error)),
        };
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| "the native diagnostics lock is unavailable".to_string())?;
        *guard = Some(manager);
        match failure {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    fn with_manager<T>(
        &self,
        callback: impl FnOnce(&mut Diagnostics) -> Result<T, String>,
    ) -> Result<T, String> {
        let mut guard = self
            .inner
            .lock()
            .map_err(|_| "the native diagnostics lock is unavailable".to_string())?;
        let manager = guard
            .as_mut()
            .ok_or_else(|| "native diagnostics are not initialized".to_string())?;
        callback(manager)
    }

    // Keeping the stable record fields visible at call sites makes severity and
    // retryability reviewable without constructing an intermediate payload.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn record_desktop(
        &self,
        level: DiagnosticLevel,
        event_code: &str,
        message: &str,
        outcome: Option<&str>,
        stage: Option<&str>,
        retryable: Option<bool>,
        metadata: Map<String, Value>,
    ) -> Option<String> {
        match self.with_manager(|manager| {
            manager.record(
                level,
                "desktop",
                event_code,
                message,
                DiagnosticFields {
                    outcome,
                    stage,
                    retryable,
                    metadata,
                    ..DiagnosticFields::default()
                },
            )
        }) {
            Ok(value) => value,
            Err(error) => {
                eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE {error}");
                None
            }
        }
    }

    pub(crate) fn aggregate_core_error(&self, frame: &str) -> Result<String, String> {
        self.with_manager(|manager| manager.aggregate_core_error(frame))
    }

    pub(crate) fn log_directory(&self) -> Result<PathBuf, String> {
        self.with_manager(|manager| Ok(manager.log_dir.clone()))
    }
}

#[derive(Default)]
struct DiagnosticFields<'a> {
    request_id: Option<&'a str>,
    operation_id: Option<&'a str>,
    parent_operation_id: Option<&'a str>,
    error_id: Option<&'a str>,
    project_id: Option<&'a str>,
    run_id: Option<&'a str>,
    execution_id: Option<&'a str>,
    session_id: Option<&'a str>,
    outcome: Option<&'a str>,
    stage: Option<&'a str>,
    duration_ms: Option<f64>,
    retryable: Option<bool>,
    safe_failure_cause: Option<&'a str>,
    exception_type: Option<&'a str>,
    stack_frames: Vec<FrontendStackFrame>,
    metadata: Map<String, Value>,
}

impl Diagnostics {
    fn memory_only(app_data_dir: &Path) -> Self {
        let launch_id = random_id("launch")
            .unwrap_or_else(|_| "launch_00000000000000000000000000000000".to_string());
        let error_id =
            random_id("err").unwrap_or_else(|_| "err_00000000000000000000000000000000".to_string());
        let mut record = Map::new();
        record.insert("schema".into(), Value::String(RECORD_SCHEMA.into()));
        record.insert("timestamp".into(), Value::String(timestamp()));
        record.insert("sequence".into(), Value::from(1));
        record.insert("level".into(), Value::String("CRITICAL".into()));
        record.insert("feature".into(), Value::String("desktop".into()));
        record.insert("source".into(), Value::String("desktop".into()));
        record.insert(
            "event_code".into(),
            Value::String("desktop.diagnostics.initialization_failed".into()),
        );
        record.insert(
            "message".into(),
            Value::String("Native local diagnostics could not initialize.".into()),
        );
        record.insert(
            "application_version".into(),
            Value::String(env!("CARGO_PKG_VERSION").into()),
        );
        record.insert("launch_id".into(), Value::String(launch_id.clone()));
        record.insert("error_id".into(), Value::String(error_id));
        record.insert("outcome".into(), Value::String("degraded".into()));
        record.insert(
            "stage".into(),
            Value::String("logger-initialization".into()),
        );
        record.insert("retryable".into(), Value::Bool(true));
        record.insert(
            "safe_failure_cause".into(),
            Value::String("A required native diagnostics path was unavailable.".into()),
        );
        record.insert(
            "exception_type".into(),
            Value::String("NativeDiagnosticsInitializationError".into()),
        );
        Self {
            log_dir: app_data_dir.join("logs"),
            settings_path: app_data_dir.join("diagnostics-settings.json"),
            settings: DiagnosticSettings::default(),
            process_override: None,
            process_override_invalid: false,
            launch_id,
            sequence: 1,
            degraded: true,
            last_failure: Some("native diagnostic storage is unavailable".to_string()),
            last_rotation: None,
            dropped_records: 0,
            memory_errors: VecDeque::from([Value::Object(record)]),
        }
    }

    fn new(app_data_dir: &Path) -> Result<Self, String> {
        secure_directory(app_data_dir)?;
        let log_dir = app_data_dir.join("logs");
        secure_directory(&log_dir)?;
        for name in ["desktop.log", "interface.log", "errors.log"] {
            secure_file(&log_dir.join(name))?;
        }
        let settings_path = app_data_dir.join("diagnostics-settings.json");
        if !settings_path.exists() {
            atomic_write_json(&settings_path, &DiagnosticSettings::default())?;
        } else {
            secure_existing_file(&settings_path)?;
        }
        let (process_override, process_override_invalid) =
            match std::env::var("NEBULA_DIAGNOSTICS_LEVEL") {
                Ok(value) => match DiagnosticLevel::parse(&value) {
                    Ok(level) => (Some(level), false),
                    Err(_) => (None, true),
                },
                Err(_) => (None, false),
            };
        let mut manager = Self {
            log_dir,
            settings_path,
            settings: DiagnosticSettings::default(),
            process_override,
            process_override_invalid,
            launch_id: random_id("launch")?,
            sequence: 0,
            degraded: false,
            last_failure: None,
            last_rotation: None,
            dropped_records: 0,
            memory_errors: VecDeque::new(),
        };
        manager.prune()?;
        Ok(manager)
    }

    fn load_settings(&mut self) -> Result<(), String> {
        let file = File::open(&self.settings_path)
            .map_err(|error| format!("cannot read diagnostics preferences: {error}"))?;
        let settings: DiagnosticSettings = serde_json::from_reader(file)
            .map_err(|_| "diagnostics preferences are invalid".to_string())?;
        settings.validate()?;
        self.settings = settings;
        Ok(())
    }

    fn atomic_write_settings(&self) -> Result<(), String> {
        atomic_write_json(&self.settings_path, &self.settings)
    }

    fn enabled(&self, level: DiagnosticLevel, feature: &str) -> bool {
        level
            >= self
                .settings
                .effective_level(feature, self.process_override)
    }

    fn record(
        &mut self,
        level: DiagnosticLevel,
        feature: &str,
        event_code: &str,
        message: &str,
        fields: DiagnosticFields<'_>,
    ) -> Result<Option<String>, String> {
        if !matches!(feature, "desktop" | "interface")
            || !valid_event_code(event_code)
            || !self.enabled(level, feature)
        {
            return Ok(None);
        }
        self.sequence = self.sequence.saturating_add(1);
        // A lower-level interface record may refer to an error that Core has
        // already recorded. Preserve that identifier for cross-layer
        // correlation without copying the handled record into errors.log.
        let error_id = match fields.error_id.filter(|value| valid_identifier(value)) {
            Some(value) => Some(value.to_string()),
            None if level >= DiagnosticLevel::Error => Some(random_id("err")?),
            None => None,
        };
        let mut record = Map::new();
        record.insert("schema".into(), Value::String(RECORD_SCHEMA.into()));
        record.insert("timestamp".into(), Value::String(timestamp()));
        record.insert("sequence".into(), Value::from(self.sequence));
        record.insert("level".into(), Value::String(level.label().into()));
        record.insert("feature".into(), Value::String(feature.into()));
        record.insert("source".into(), Value::String("desktop".into()));
        record.insert("event_code".into(), Value::String(event_code.into()));
        record.insert("message".into(), Value::String(sanitize_text(message)));
        record.insert(
            "application_version".into(),
            Value::String(env!("CARGO_PKG_VERSION").into()),
        );
        record.insert("launch_id".into(), Value::String(self.launch_id.clone()));
        insert_identifier(&mut record, "request_id", fields.request_id);
        insert_identifier(&mut record, "operation_id", fields.operation_id);
        insert_identifier(
            &mut record,
            "parent_operation_id",
            fields.parent_operation_id,
        );
        insert_identifier(&mut record, "project_id", fields.project_id);
        insert_identifier(&mut record, "run_id", fields.run_id);
        insert_identifier(&mut record, "execution_id", fields.execution_id);
        insert_identifier(&mut record, "session_id", fields.session_id);
        if let Some(value) = error_id.as_ref() {
            record.insert("error_id".into(), Value::String(value.clone()));
        }
        insert_safe_text(&mut record, "outcome", fields.outcome);
        insert_safe_text(&mut record, "stage", fields.stage);
        if let Some(value) = fields
            .duration_ms
            .filter(|value| value.is_finite() && *value >= 0.0)
        {
            record.insert(
                "duration_ms".into(),
                json!((value * 1000.0).round() / 1000.0),
            );
        }
        if let Some(value) = fields.retryable {
            record.insert("retryable".into(), Value::Bool(value));
        }
        insert_safe_text(&mut record, "safe_failure_cause", fields.safe_failure_cause);
        insert_safe_text(&mut record, "exception_type", fields.exception_type);
        if !fields.stack_frames.is_empty() {
            record.insert(
                "stack_frames".into(),
                Value::Array(
                    fields
                        .stack_frames
                        .into_iter()
                        .take(32)
                        .map(|frame| {
                            json!({
                                "module": sanitize_text(&frame.module),
                                "function": sanitize_text(&frame.function),
                                "line": frame.line,
                            })
                        })
                        .collect(),
                ),
            );
        }
        let metadata = sanitize_metadata(&fields.metadata, 0);
        if !metadata.is_empty() {
            record.insert("metadata".into(), Value::Object(metadata));
        }
        let record = Value::Object(record);
        let line = serde_json::to_vec(&record)
            .map_err(|error| format!("cannot encode a diagnostic record: {error}"))?;
        let feature_path = self.log_dir.join(format!("{feature}.log"));
        let mut failures = Vec::new();
        if let Err(error) = self.append(&feature_path, &line, level >= DiagnosticLevel::Error) {
            failures.push(error);
        }
        if level >= DiagnosticLevel::Error {
            let errors_path = self.log_dir.join("errors.log");
            if let Err(error) = self.append(&errors_path, &line, true) {
                failures.push(error);
            }
            if !failures.is_empty() {
                // Error records are never evicted from the emergency in-memory
                // sink. They remain available to the viewer for this launch.
                self.memory_errors.push_back(record);
            }
        }
        if !failures.is_empty() {
            return Err(failures.join("; "));
        }
        Ok(error_id)
    }

    fn append(&mut self, path: &Path, line: &[u8], sync: bool) -> Result<(), String> {
        self.rotate_if_needed(path, line.len() as u64)?;
        let mut options = OpenOptions::new();
        options.create(true).append(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options.open(path).map_err(|error| {
            self.degraded = true;
            self.last_failure = Some("a native diagnostic log is unavailable".to_string());
            format!("cannot append native diagnostics: {error}")
        })?;
        file.write_all(line)
            .and_then(|_| file.write_all(b"\n"))
            .and_then(|_| file.flush())
            .and_then(|_| if sync { file.sync_data() } else { Ok(()) })
            .map_err(|error| {
                self.degraded = true;
                self.last_failure = Some("a native diagnostic write failed".to_string());
                format!("cannot write native diagnostics: {error}")
            })?;
        Ok(())
    }

    fn rotate_if_needed(&mut self, path: &Path, incoming: u64) -> Result<(), String> {
        let current = fs::metadata(path).map(|value| value.len()).unwrap_or(0);
        if current.saturating_add(incoming + 1) <= MAX_FILE_BYTES {
            return Ok(());
        }
        let oldest = rotation_path(path, MAX_ROTATIONS);
        if oldest.exists() {
            fs::remove_file(&oldest)
                .map_err(|error| format!("cannot remove an old diagnostic rotation: {error}"))?;
        }
        for index in (1..MAX_ROTATIONS).rev() {
            let source = rotation_path(path, index);
            if source.exists() {
                fs::rename(&source, rotation_path(path, index + 1))
                    .map_err(|error| format!("cannot advance a diagnostic rotation: {error}"))?;
            }
        }
        if path.exists() {
            fs::rename(path, rotation_path(path, 1))
                .map_err(|error| format!("cannot rotate a native diagnostic log: {error}"))?;
        }
        secure_file(path)?;
        self.last_rotation = Some(timestamp());
        Ok(())
    }

    fn prune(&mut self) -> Result<(), String> {
        let now = SystemTime::now();
        let mut files = Vec::new();
        for entry in fs::read_dir(&self.log_dir)
            .map_err(|error| format!("cannot inspect the diagnostics directory: {error}"))?
        {
            let entry =
                entry.map_err(|error| format!("cannot inspect a diagnostics file: {error}"))?;
            let path = entry.path();
            let metadata = entry
                .metadata()
                .map_err(|error| format!("cannot inspect a diagnostics file: {error}"))?;
            if !metadata.is_file() || metadata.file_type().is_symlink() {
                continue;
            }
            let is_rotation = path
                .file_name()
                .and_then(|value| value.to_str())
                .is_some_and(|name| name.ends_with(".log.1") || name.ends_with(".log.2"));
            if is_rotation
                && metadata
                    .modified()
                    .ok()
                    .and_then(|modified| now.duration_since(modified).ok())
                    .is_some_and(|age| age > MAX_ROTATION_AGE)
            {
                fs::remove_file(path).map_err(|error| {
                    format!("cannot prune an expired diagnostic rotation: {error}")
                })?;
                continue;
            }
            files.push((
                path,
                metadata.len(),
                metadata.modified().unwrap_or(SystemTime::UNIX_EPOCH),
                is_rotation,
            ));
        }
        let mut total: u64 = files.iter().map(|(_, size, _, _)| size).sum();
        files.sort_by_key(|(_, _, modified, _)| *modified);
        for (path, size, _, rotation) in files {
            if total <= MAX_DIRECTORY_BYTES {
                break;
            }
            if rotation {
                fs::remove_file(path).map_err(|error| {
                    format!("cannot enforce the diagnostics directory cap: {error}")
                })?;
                total = total.saturating_sub(size);
            }
        }
        Ok(())
    }

    fn aggregate_core_error(&mut self, frame: &str) -> Result<String, String> {
        if frame.len() > 64 * 1024 {
            return Err("Core diagnostic error frame is oversized".to_string());
        }
        let value: Value = serde_json::from_str(frame)
            .map_err(|_| "Core diagnostic error frame is malformed".to_string())?;
        let value = sanitize_core_error(value)?;
        let line = serde_json::to_vec(&value)
            .map_err(|error| format!("cannot encode a Core diagnostic error: {error}"))?;
        if let Err(error) = self.append(&self.log_dir.clone().join("errors.log"), &line, true) {
            self.memory_errors.push_back(value);
            return Err(error);
        }
        String::from_utf8(line)
            .map_err(|_| "a sanitized Core diagnostic error was not UTF-8".to_string())
    }

    fn files(&self) -> Result<Vec<DiagnosticFile>, String> {
        let mut result = Vec::new();
        for entry in fs::read_dir(&self.log_dir)
            .map_err(|error| format!("cannot inspect the diagnostics directory: {error}"))?
        {
            let entry =
                entry.map_err(|error| format!("cannot inspect a diagnostics file: {error}"))?;
            let metadata = entry
                .metadata()
                .map_err(|error| format!("cannot inspect a diagnostics file: {error}"))?;
            if !metadata.is_file() || metadata.file_type().is_symlink() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            if !valid_log_name(&name) {
                continue;
            }
            result.push(DiagnosticFile {
                name,
                size_bytes: metadata.len(),
                modified_at: metadata.modified().map(system_time).unwrap_or_default(),
            });
        }
        result.sort_by(|left, right| left.name.cmp(&right.name));
        Ok(result)
    }

    fn recent_errors(
        &mut self,
        feature: Option<&str>,
        after: Option<&str>,
        limit: usize,
    ) -> Result<Vec<Value>, String> {
        if feature.is_some_and(|value| !FEATURES.contains(&value)) {
            return Err("unknown diagnostics feature".to_string());
        }
        let limit = limit.clamp(1, MAX_RECENT_ERRORS);
        let base = self.log_dir.join("errors.log");
        let mut records = Vec::new();
        let mut invalid_records = 0_u64;
        for path in [rotation_path(&base, 2), rotation_path(&base, 1), base] {
            if !path.exists() {
                continue;
            }
            let file = File::open(path)
                .map_err(|error| format!("cannot read recent diagnostic errors: {error}"))?;
            for line in BufReader::new(file).lines() {
                let Ok(line) = line else {
                    invalid_records = invalid_records.saturating_add(1);
                    continue;
                };
                let Ok(value) = serde_json::from_str::<Value>(&line) else {
                    invalid_records = invalid_records.saturating_add(1);
                    continue;
                };
                let Some(object) = value.as_object() else {
                    invalid_records = invalid_records.saturating_add(1);
                    continue;
                };
                if feature.is_some_and(|selected| {
                    object.get("feature").and_then(Value::as_str) != Some(selected)
                }) {
                    continue;
                }
                if after.is_some_and(|cutoff| {
                    object
                        .get("timestamp")
                        .and_then(Value::as_str)
                        .is_none_or(|value| value <= cutoff)
                }) {
                    continue;
                }
                records.push(value);
            }
        }
        records.extend(self.memory_errors.iter().cloned());
        records.retain(|record| {
            let object = record.as_object();
            let feature_matches = feature.is_none_or(|selected| {
                object
                    .and_then(|value| value.get("feature"))
                    .and_then(Value::as_str)
                    == Some(selected)
            });
            let after_matches = after.is_none_or(|cutoff| {
                object
                    .and_then(|value| value.get("timestamp"))
                    .and_then(Value::as_str)
                    .is_some_and(|value| value > cutoff)
            });
            feature_matches && after_matches
        });
        if invalid_records > 0 {
            if let Err(error) = self.record(
                DiagnosticLevel::Error,
                "desktop",
                "desktop.diagnostics.viewer_record_invalid",
                "A local diagnostic error record was malformed.",
                DiagnosticFields {
                    outcome: Some("failure"),
                    stage: Some("viewer-read"),
                    retryable: Some(false),
                    metadata: Map::from_iter([("count".to_string(), Value::from(invalid_records))]),
                    ..DiagnosticFields::default()
                },
            ) {
                eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE {error}");
            }
        }
        let mut seen = BTreeSet::new();
        records.retain(|record| {
            let key = record
                .get("error_id")
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| record.to_string());
            seen.insert(key)
        });
        records.sort_by(|left, right| {
            let left_key = (
                left.get("timestamp").and_then(Value::as_str).unwrap_or(""),
                left.get("sequence").and_then(Value::as_u64).unwrap_or(0),
            );
            let right_key = (
                right.get("timestamp").and_then(Value::as_str).unwrap_or(""),
                right.get("sequence").and_then(Value::as_u64).unwrap_or(0),
            );
            left_key.cmp(&right_key)
        });
        let start = records.len().saturating_sub(limit);
        Ok(records.split_off(start))
    }

    fn status(&mut self) -> Value {
        let disk_usage: u64 = match self.files() {
            Ok(files) => files.iter().map(|file| file.size_bytes).sum(),
            Err(_) => {
                self.degraded = true;
                self.last_failure =
                    Some("the native diagnostics directory could not be inspected".to_string());
                if let Err(error) = self.record(
                    DiagnosticLevel::Error,
                    "desktop",
                    "desktop.diagnostics.status_failed",
                    "Native diagnostics health could not be measured.",
                    DiagnosticFields {
                        outcome: Some("degraded"),
                        stage: Some("health"),
                        retryable: Some(true),
                        ..DiagnosticFields::default()
                    },
                ) {
                    eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE {error}");
                }
                0
            }
        };
        json!({
            "schema": "nebula.diagnostics-status/v1",
            "writable": !self.degraded,
            "degraded": self.degraded,
            "global_level": self.settings.global_level,
            "feature_levels": self.settings.feature_levels,
            "process_override": self.process_override,
            "disk_usage_bytes": disk_usage,
            "last_rotation": self.last_rotation,
            "dropped_record_count": self.dropped_records,
            "last_failure": self.last_failure,
        })
    }
}

#[tauri::command]
pub(crate) fn diagnostics_get_settings(
    state: State<'_, DiagnosticsState>,
) -> Result<DiagnosticSettings, String> {
    state.with_manager(|manager| Ok(manager.settings.clone()))
}

#[tauri::command]
pub(crate) fn diagnostics_update_settings(
    settings: DiagnosticSettings,
    state: State<'_, DiagnosticsState>,
) -> Result<DiagnosticSettings, String> {
    settings.validate()?;
    state.with_manager(|manager| {
        let previous = manager.settings.clone();
        manager.settings = settings.clone();
        if let Err(error) = manager.atomic_write_settings() {
            manager.settings = previous;
            if let Err(log_error) = manager.record(
                DiagnosticLevel::Error,
                "desktop",
                "desktop.diagnostics.settings_write_failed",
                "Diagnostics preferences could not be saved.",
                DiagnosticFields {
                    outcome: Some("failure"),
                    stage: Some("settings-write"),
                    retryable: Some(true),
                    ..DiagnosticFields::default()
                },
            ) {
                eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE {log_error}");
            }
            return Err(error);
        }
        if let Err(log_error) = manager.record(
            DiagnosticLevel::Info,
            "desktop",
            "desktop.diagnostics.settings_updated",
            "Diagnostics preferences were updated.",
            DiagnosticFields {
                outcome: Some("success"),
                stage: Some("settings-write"),
                ..DiagnosticFields::default()
            },
        ) {
            eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE {log_error}");
        }
        Ok(settings)
    })
}

#[tauri::command]
pub(crate) fn diagnostics_log_frontend(
    record: FrontendDiagnosticRecord,
    state: State<'_, DiagnosticsState>,
) -> Result<Option<String>, String> {
    record.validate()?;
    state.with_manager(|manager| {
        manager.record(
            record.level,
            "interface",
            &record.event_code,
            &record.message,
            DiagnosticFields {
                request_id: record.request_id.as_deref(),
                operation_id: record.operation_id.as_deref(),
                parent_operation_id: record.parent_operation_id.as_deref(),
                error_id: record.error_id.as_deref(),
                project_id: record.project_id.as_deref(),
                run_id: record.run_id.as_deref(),
                execution_id: record.execution_id.as_deref(),
                session_id: record.session_id.as_deref(),
                outcome: record.outcome.as_deref(),
                stage: record.stage.as_deref(),
                duration_ms: record.duration_ms,
                retryable: record.retryable,
                safe_failure_cause: record.safe_failure_cause.as_deref(),
                exception_type: record.exception_type.as_deref(),
                stack_frames: record.stack_frames,
                metadata: record.metadata,
            },
        )
    })
}

#[tauri::command]
pub(crate) fn diagnostics_files(
    state: State<'_, DiagnosticsState>,
) -> Result<Vec<DiagnosticFile>, String> {
    state.with_manager(|manager| match manager.files() {
        Ok(files) => Ok(files),
        Err(error) => {
            if let Err(log_error) = manager.record(
                DiagnosticLevel::Error,
                "desktop",
                "desktop.diagnostics.files_failed",
                "The diagnostics file inventory could not be read.",
                DiagnosticFields {
                    outcome: Some("failure"),
                    stage: Some("viewer-read"),
                    retryable: Some(true),
                    ..DiagnosticFields::default()
                },
            ) {
                eprintln!("NEBULA_DIAGNOSTICS_UNAVAILABLE {log_error}");
            }
            Err(error)
        }
    })
}

#[tauri::command]
pub(crate) fn diagnostics_recent_errors(
    feature: Option<String>,
    after: Option<String>,
    limit: Option<usize>,
    state: State<'_, DiagnosticsState>,
) -> Result<Vec<Value>, String> {
    state.with_manager(|manager| {
        manager.recent_errors(feature.as_deref(), after.as_deref(), limit.unwrap_or(100))
    })
}

#[tauri::command]
pub(crate) fn diagnostics_status(state: State<'_, DiagnosticsState>) -> Result<Value, String> {
    state.with_manager(|manager| Ok(manager.status()))
}

#[tauri::command]
pub(crate) fn diagnostics_reveal_logs(
    app: AppHandle,
    state: State<'_, DiagnosticsState>,
) -> Result<(), String> {
    let directory = state.log_directory()?;
    let result = app
        .opener()
        .open_path(directory.to_string_lossy().to_string(), None::<&str>)
        .map_err(|error| format!("cannot reveal the Nebula diagnostics directory: {error}"));
    if result.is_err() {
        state.record_desktop(
            DiagnosticLevel::Error,
            "desktop.diagnostics.reveal_failed",
            "The diagnostics folder could not be revealed.",
            Some("failure"),
            Some("open-folder"),
            Some(true),
            Map::new(),
        );
    }
    result
}

pub(crate) fn install_panic_hook(state: DiagnosticsState) {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |information| {
        state.record_desktop(
            DiagnosticLevel::Critical,
            "desktop.panic",
            "The native desktop encountered an unrecoverable panic.",
            Some("failure"),
            Some("panic"),
            Some(false),
            Map::new(),
        );
        previous(information);
    }));
}

fn sanitize_core_error(value: Value) -> Result<Value, String> {
    const ALLOWED_KEYS: &[&str] = &[
        "schema",
        "timestamp",
        "sequence",
        "level",
        "feature",
        "source",
        "event_code",
        "message",
        "application_version",
        "launch_id",
        "request_id",
        "operation_id",
        "parent_operation_id",
        "error_id",
        "project_id",
        "run_id",
        "execution_id",
        "session_id",
        "outcome",
        "stage",
        "duration_ms",
        "retryable",
        "safe_failure_cause",
        "exception_type",
        "exception_chain",
        "stack_frames",
        "metadata",
    ];
    const IDENTIFIER_KEYS: &[&str] = &[
        "launch_id",
        "request_id",
        "operation_id",
        "parent_operation_id",
        "error_id",
        "project_id",
        "run_id",
        "execution_id",
        "session_id",
    ];
    const TEXT_KEYS: &[&str] = &[
        "timestamp",
        "source",
        "message",
        "application_version",
        "outcome",
        "stage",
        "safe_failure_cause",
        "exception_type",
    ];

    let object = value
        .as_object()
        .ok_or_else(|| "Core diagnostic error frame is not an object".to_string())?;
    if object
        .keys()
        .any(|key| !ALLOWED_KEYS.contains(&key.as_str()))
    {
        return Err("Core diagnostic error frame contains an unsupported field".to_string());
    }
    let schema_valid = object.get("schema").and_then(Value::as_str) == Some(RECORD_SCHEMA);
    let level_valid = object
        .get("level")
        .and_then(Value::as_str)
        .is_some_and(|level| matches!(level, "ERROR" | "CRITICAL"));
    let feature_valid = object
        .get("feature")
        .and_then(Value::as_str)
        .is_some_and(|feature| {
            FEATURES.contains(&feature) && !matches!(feature, "desktop" | "interface")
        });
    let event_valid = object
        .get("event_code")
        .and_then(Value::as_str)
        .is_some_and(valid_event_code);
    let sequence_valid = object.get("sequence").and_then(Value::as_u64).is_some();
    let required_text_valid = ["timestamp", "source", "message", "application_version"]
        .iter()
        .all(|key| object.get(*key).and_then(Value::as_str).is_some());
    let identifiers_valid = IDENTIFIER_KEYS.iter().all(|key| {
        object
            .get(*key)
            .is_none_or(|item| item.as_str().is_some_and(valid_identifier))
    });
    if !(schema_valid
        && level_valid
        && feature_valid
        && event_valid
        && sequence_valid
        && required_text_valid
        && identifiers_valid
        && object.get("launch_id").is_some()
        && object.get("error_id").is_some())
    {
        return Err("Core diagnostic error frame failed validation".to_string());
    }

    let mut safe = Map::new();
    for (key, item) in object {
        let value = if matches!(key.as_str(), "schema" | "level" | "feature" | "event_code")
            || IDENTIFIER_KEYS.contains(&key.as_str())
        {
            item.clone()
        } else if TEXT_KEYS.contains(&key.as_str()) {
            let text = item
                .as_str()
                .ok_or_else(|| format!("Core diagnostic {key} must be text"))?;
            Value::String(sanitize_text(text))
        } else {
            match key.as_str() {
                "sequence" => item.clone(),
                "duration_ms" => {
                    let number = item
                        .as_f64()
                        .filter(|value| value.is_finite() && (0.0..=86_400_000.0).contains(value));
                    if number.is_none() {
                        return Err("Core diagnostic duration is invalid".to_string());
                    }
                    item.clone()
                }
                "retryable" => {
                    if !item.is_boolean() {
                        return Err("Core diagnostic retryability is invalid".to_string());
                    }
                    item.clone()
                }
                "metadata" => {
                    let metadata = item
                        .as_object()
                        .ok_or_else(|| "Core diagnostic metadata is invalid".to_string())?;
                    Value::Object(sanitize_metadata(metadata, 0))
                }
                "exception_chain" => {
                    let chain = item
                        .as_array()
                        .ok_or_else(|| "Core diagnostic exception chain is invalid".to_string())?;
                    if chain.len() > 8 {
                        return Err("Core diagnostic exception chain is oversized".to_string());
                    }
                    Value::Array(
                        chain
                            .iter()
                            .map(|entry| {
                                entry
                                    .as_str()
                                    .map(|text| Value::String(sanitize_text(text)))
                                    .ok_or_else(|| {
                                        "Core diagnostic exception chain is invalid".to_string()
                                    })
                            })
                            .collect::<Result<Vec<_>, _>>()?,
                    )
                }
                "stack_frames" => {
                    let frames = item
                        .as_array()
                        .ok_or_else(|| "Core diagnostic stack frames are invalid".to_string())?;
                    if frames.len() > 32 {
                        return Err("Core diagnostic stack frames are oversized".to_string());
                    }
                    Value::Array(
                        frames
                            .iter()
                            .map(sanitize_core_stack_frame)
                            .collect::<Result<Vec<_>, _>>()?,
                    )
                }
                _ => item.clone(),
            }
        };
        safe.insert(key.clone(), value);
    }
    Ok(Value::Object(safe))
}

fn sanitize_core_stack_frame(value: &Value) -> Result<Value, String> {
    let frame = value
        .as_object()
        .ok_or_else(|| "Core diagnostic stack frame is invalid".to_string())?;
    if frame.len() != 3
        || frame
            .keys()
            .any(|key| !matches!(key.as_str(), "module" | "function" | "line"))
    {
        return Err("Core diagnostic stack frame is invalid".to_string());
    }
    let module = frame
        .get("module")
        .and_then(Value::as_str)
        .ok_or_else(|| "Core diagnostic stack module is invalid".to_string())?;
    let function = frame
        .get("function")
        .and_then(Value::as_str)
        .ok_or_else(|| "Core diagnostic stack function is invalid".to_string())?;
    let line = frame
        .get("line")
        .and_then(Value::as_u64)
        .filter(|line| *line <= 10_000_000)
        .ok_or_else(|| "Core diagnostic stack line is invalid".to_string())?;
    Ok(json!({
        "module": sanitize_text(module),
        "function": sanitize_text(function),
        "line": line,
    }))
}

fn valid_event_code(value: &str) -> bool {
    if value.len() < 3 || value.len() > 160 || !value.contains('.') {
        return false;
    }
    value.bytes().all(|byte| {
        byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'.' | b'_' | b'-')
    }) && value.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
        && !value.contains("..")
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b':' | b'-'))
}

fn insert_identifier(record: &mut Map<String, Value>, key: &str, value: Option<&str>) {
    if let Some(value) = value.filter(|value| valid_identifier(value)) {
        record.insert(key.into(), Value::String(value.into()));
    }
}

fn insert_safe_text(record: &mut Map<String, Value>, key: &str, value: Option<&str>) {
    if let Some(value) = value.filter(|value| !value.is_empty()) {
        record.insert(key.into(), Value::String(sanitize_text(value)));
    }
}

fn sanitize_text(input: &str) -> String {
    if input.contains("-----BEGIN") && input.contains("PRIVATE KEY-----") {
        return "[REDACTED PRIVATE KEY]".to_string();
    }
    static BEARER: OnceLock<Regex> = OnceLock::new();
    static JWT: OnceLock<Regex> = OnceLock::new();
    static TOKEN: OnceLock<Regex> = OnceLock::new();
    static ASSIGNMENT: OnceLock<Regex> = OnceLock::new();
    let mut value = input.replace('\0', "�");
    value = BEARER
        .get_or_init(|| {
            Regex::new(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
                .expect("valid bearer redaction expression")
        })
        .replace_all(&value, "Bearer [REDACTED]")
        .into_owned();
    value = JWT
        .get_or_init(|| {
            Regex::new(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
                .expect("valid JWT redaction expression")
        })
        .replace_all(&value, "[REDACTED JWT]")
        .into_owned();
    value = TOKEN
        .get_or_init(|| {
            Regex::new(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})\b")
                .expect("valid token redaction expression")
        })
        .replace_all(&value, "[REDACTED TOKEN]")
        .into_owned();
    value = ASSIGNMENT
        .get_or_init(|| Regex::new(r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|secret)\s*[:=]\s*[^\s,;]{8,}").expect("valid assignment redaction expression"))
        .replace_all(&value, "$1=[REDACTED]")
        .into_owned();
    if value.len() > MAX_STRING_BYTES {
        let mut boundary = MAX_STRING_BYTES - 3;
        while !value.is_char_boundary(boundary) {
            boundary -= 1;
        }
        value.truncate(boundary);
        value.push('…');
    }
    value
}

fn sanitize_metadata(input: &Map<String, Value>, depth: usize) -> Map<String, Value> {
    if depth >= MAX_METADATA_DEPTH {
        return Map::new();
    }
    input
        .iter()
        .take(MAX_METADATA_ITEMS)
        .filter_map(|(key, value)| {
            let normalized = key.to_ascii_lowercase().replace('-', "_");
            if DENIED_KEY_PARTS
                .iter()
                .any(|part| normalized.contains(part))
                || !SAFE_METADATA_KEYS.contains(&normalized.as_str())
            {
                return None;
            }
            Some((normalized, sanitize_value(value, depth + 1)))
        })
        .collect()
}

fn sanitize_value(value: &Value, depth: usize) -> Value {
    if depth >= MAX_METADATA_DEPTH {
        return Value::String("[MAX_DEPTH]".into());
    }
    match value {
        Value::Null | Value::Bool(_) | Value::Number(_) => value.clone(),
        Value::String(value) => Value::String(sanitize_text(value)),
        Value::Array(values) => Value::Array(
            values
                .iter()
                .take(MAX_METADATA_ITEMS)
                .map(|value| sanitize_value(value, depth + 1))
                .collect(),
        ),
        Value::Object(values) => Value::Object(sanitize_metadata(values, depth + 1)),
    }
}

fn timestamp() -> String {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .unwrap_or_else(|_| "1970-01-01T00:00:00Z".to_string())
}

fn system_time(value: SystemTime) -> String {
    let duration = value
        .duration_since(SystemTime::UNIX_EPOCH)
        .unwrap_or_default();
    OffsetDateTime::from_unix_timestamp(duration.as_secs() as i64)
        .ok()
        .and_then(|value| value.format(&Rfc3339).ok())
        .unwrap_or_default()
}

fn random_id(prefix: &str) -> Result<String, String> {
    let mut bytes = [0_u8; 16];
    getrandom::fill(&mut bytes)
        .map_err(|error| format!("cannot create a diagnostic correlation identifier: {error}"))?;
    let encoded: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    Ok(format!("{prefix}_{encoded}"))
}

fn rotation_path(path: &Path, index: usize) -> PathBuf {
    path.with_file_name(format!(
        "{}.{}",
        path.file_name().unwrap_or_default().to_string_lossy(),
        index
    ))
}

fn secure_directory(path: &Path) -> Result<(), String> {
    fs::create_dir_all(path)
        .map_err(|error| format!("cannot create the diagnostics directory: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|error| format!("cannot secure the diagnostics directory: {error}"))?;
    }
    Ok(())
}

fn secure_file(path: &Path) -> Result<(), String> {
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    options
        .open(path)
        .map_err(|error| format!("cannot create a diagnostics file: {error}"))?;
    secure_existing_file(path)
}

fn secure_existing_file(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))
            .map_err(|error| format!("cannot secure a diagnostics file: {error}"))?;
    }
    Ok(())
}

fn atomic_write_json(path: &Path, value: &impl Serialize) -> Result<(), String> {
    let parent = path
        .parent()
        .ok_or_else(|| "diagnostics preferences have no parent directory".to_string())?;
    secure_directory(parent)?;
    let temporary = parent.join(format!(".diagnostics-settings-{}.tmp", random_id("write")?));
    let result = (|| {
        let mut options = OpenOptions::new();
        options.create_new(true).write(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            options.mode(0o600);
        }
        let mut file = options
            .open(&temporary)
            .map_err(|error| format!("cannot create diagnostics preferences: {error}"))?;
        serde_json::to_writer(&mut file, value)
            .map_err(|error| format!("cannot encode diagnostics preferences: {error}"))?;
        file.write_all(b"\n")
            .and_then(|_| file.flush())
            .and_then(|_| file.sync_all())
            .map_err(|error| format!("cannot write diagnostics preferences: {error}"))?;
        fs::rename(&temporary, path)
            .map_err(|error| format!("cannot replace diagnostics preferences: {error}"))?;
        secure_existing_file(path)
    })();
    match result {
        Ok(()) => Ok(()),
        Err(error) => match fs::remove_file(&temporary) {
            Ok(()) if temporary.exists() => Err(format!(
                "{error}; temporary diagnostics preferences still exist"
            )),
            Ok(()) => Err(error),
            Err(cleanup_error) if cleanup_error.kind() == std::io::ErrorKind::NotFound => {
                Err(error)
            }
            Err(cleanup_error) => Err(format!(
                "{error}; temporary diagnostics preferences cleanup failed: {cleanup_error}"
            )),
        },
    }
}

fn valid_log_name(name: &str) -> bool {
    matches!(
        name,
        "desktop.log"
            | "desktop.log.1"
            | "desktop.log.2"
            | "interface.log"
            | "interface.log.1"
            | "interface.log.2"
            | "errors.log"
            | "errors.log.1"
            | "errors.log.2"
            | "nebula-core-startup.log"
            | "nebula-core-startup.log.1"
            | "nebula-core-startup.log.2"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    const SHARED_CONTRACT: &str =
        include_str!("../../../tests/v3/fixtures/diagnostics_contract.json");

    fn temporary_directory(label: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!(
            "nebula-diagnostics-{label}-{}",
            random_id("test").expect("test identifier should be available")
        ));
        fs::create_dir_all(&directory).expect("test directory should be created");
        directory
    }

    #[test]
    fn frontend_metadata_is_allowlisted_and_sensitive_keys_are_removed() {
        let input = serde_json::from_value::<Map<String, Value>>(json!({
            "count": 2,
            "token": "CANARY",
            "body": "CANARY",
            "route": "/api/v1/projects/{id}",
            "unknown": "ignored",
        }))
        .expect("metadata should decode");
        let safe = sanitize_metadata(&input, 0);
        assert_eq!(safe.get("count"), Some(&Value::from(2)));
        assert!(safe.contains_key("route"));
        assert!(!safe.contains_key("token"));
        assert!(!safe.contains_key("body"));
        assert!(!safe.contains_key("unknown"));
    }

    #[test]
    fn shared_cross_language_contract_matches_native_diagnostics() {
        let contract: Value =
            serde_json::from_str(SHARED_CONTRACT).expect("shared contract should decode");
        assert_eq!(contract["record_schema"], RECORD_SCHEMA);
        assert_eq!(
            contract["settings"],
            serde_json::to_value(DiagnosticSettings::default())
                .expect("default settings should encode")
        );
        assert_eq!(
            contract["features"],
            serde_json::to_value(FEATURES).expect("features should encode")
        );
        let metadata = contract["metadata_input"]
            .as_object()
            .expect("contract metadata should be an object");
        assert_eq!(
            Value::Object(sanitize_metadata(metadata, 0)),
            contract["metadata_expected"]
        );
    }

    #[test]
    fn stable_codes_and_identifiers_are_strict() {
        assert!(valid_event_code("interface.runtime.failed"));
        assert!(!valid_event_code("Interface failed"));
        assert!(valid_identifier("op_0123456789abcdef"));
        assert!(!valid_identifier("contains a space"));
    }

    #[test]
    fn handled_core_errors_keep_their_reference_on_lower_level_interface_records() {
        let record: FrontendDiagnosticRecord = serde_json::from_value(json!({
            "schema": RECORD_SCHEMA,
            "level": "debug",
            "feature": "interface",
            "event_code": "interface.api.handled_failure",
            "message": "A previously recorded Core error was shown by the interface.",
            "request_id": "req_core_123",
            "error_id": "err_core_123"
        }))
        .expect("frontend record should decode");

        record
            .validate()
            .expect("a lower-level correlation reference should be valid");

        let directory = temporary_directory("handled-core-error");
        let mut manager = Diagnostics::new(&directory).expect("diagnostics should initialize");
        manager.settings.global_level = DiagnosticLevel::Debug;
        let error_id = manager
            .record(
                record.level,
                "interface",
                &record.event_code,
                &record.message,
                DiagnosticFields {
                    request_id: record.request_id.as_deref(),
                    error_id: record.error_id.as_deref(),
                    ..DiagnosticFields::default()
                },
            )
            .expect("handled Core record should be written");

        assert_eq!(error_id.as_deref(), Some("err_core_123"));
        let interface = fs::read_to_string(manager.log_dir.join("interface.log"))
            .expect("interface log should be readable");
        let errors = fs::read_to_string(manager.log_dir.join("errors.log"))
            .expect("aggregate log should be readable");
        assert!(interface.contains("err_core_123"));
        assert!(errors.is_empty());
        fs::remove_dir_all(directory).expect("test directory should be removed");
    }

    #[test]
    fn error_default_is_secure_sanitized_and_correlated_across_native_files() {
        let directory = temporary_directory("routing");
        let mut manager = Diagnostics::new(&directory).expect("diagnostics should initialize");
        manager
            .load_settings()
            .expect("default settings should load");

        let ignored = manager
            .record(
                DiagnosticLevel::Info,
                "desktop",
                "desktop.test.started",
                "A test started.",
                DiagnosticFields::default(),
            )
            .expect("filtered records are valid");
        assert!(ignored.is_none());
        let error_id = manager
            .record(
                DiagnosticLevel::Error,
                "desktop",
                "desktop.test.failed",
                "Provider failed for sk-abcdefghijklmnopqrstuvwxyz123456.",
                DiagnosticFields {
                    metadata: Map::from_iter([
                        ("count".into(), Value::from(2)),
                        ("authorization".into(), Value::String("CANARY".into())),
                    ]),
                    ..DiagnosticFields::default()
                },
            )
            .expect("error should be written")
            .expect("error should receive an identifier");

        let desktop = fs::read_to_string(manager.log_dir.join("desktop.log"))
            .expect("desktop log should be readable");
        let errors = fs::read_to_string(manager.log_dir.join("errors.log"))
            .expect("aggregate log should be readable");
        assert_eq!(desktop, errors);
        assert!(desktop.contains(&error_id));
        assert!(!desktop.contains("sk-abcdefghijklmnopqrstuvwxyz123456"));
        assert!(!desktop.contains("CANARY"));
        assert!(desktop.contains("[REDACTED TOKEN]"));
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(manager.log_dir.join("errors.log"))
                    .expect("aggregate metadata should exist")
                    .permissions()
                    .mode()
                    & 0o777,
                0o600
            );
        }
        fs::remove_dir_all(directory).expect("test directory should be removed");
    }

    #[test]
    fn core_error_aggregation_rejects_payload_fields_and_resanitizes_text() {
        let directory = temporary_directory("aggregation");
        let mut manager = Diagnostics::new(&directory).expect("diagnostics should initialize");
        let base = json!({
            "schema": RECORD_SCHEMA,
            "timestamp": "2026-07-14T12:00:00.000Z",
            "sequence": 7,
            "level": "ERROR",
            "feature": "storage",
            "source": "core",
            "event_code": "storage.test.failed",
            "message": "Storage failed for Bearer top-secret-token-value.",
            "application_version": "3.0.0-alpha.1",
            "launch_id": "launch_123",
            "error_id": "err_shared_123",
            "metadata": {"component": "database", "count": 1},
        });
        let mut unsafe_frame = base.clone();
        unsafe_frame
            .as_object_mut()
            .expect("frame should be an object")
            .insert("body".into(), Value::String("CANARY-PAYLOAD".into()));
        assert!(
            manager
                .aggregate_core_error(&unsafe_frame.to_string())
                .is_err()
        );

        manager
            .aggregate_core_error(&base.to_string())
            .expect("safe Core frame should aggregate");
        let aggregate = fs::read_to_string(manager.log_dir.join("errors.log"))
            .expect("aggregate should be readable");
        assert!(aggregate.contains("err_shared_123"));
        assert!(!aggregate.contains("top-secret-token-value"));
        assert!(!aggregate.contains("CANARY-PAYLOAD"));
        fs::remove_dir_all(directory).expect("test directory should be removed");
    }

    #[test]
    fn unwritable_native_sink_retains_errors_in_memory_for_the_viewer() {
        let directory = temporary_directory("fallback");
        let mut manager = Diagnostics::new(&directory).expect("diagnostics should initialize");
        fs::remove_dir_all(&manager.log_dir).expect("log directory should be removable");
        fs::write(&manager.log_dir, b"blocked").expect("blocking file should be created");

        assert!(
            manager
                .record(
                    DiagnosticLevel::Error,
                    "desktop",
                    "desktop.test.unwritable",
                    "The native diagnostics sink is unavailable.",
                    DiagnosticFields::default(),
                )
                .is_err()
        );
        assert!(manager.degraded);
        let recent = manager
            .recent_errors(None, None, 10)
            .expect("memory fallback should remain readable");
        assert_eq!(recent.len(), 1);
        assert_eq!(recent[0]["event_code"], "desktop.test.unwritable");
        fs::remove_file(&manager.log_dir).expect("blocking file should be removed");
        fs::remove_dir_all(directory).expect("test directory should be removed");
    }

    #[test]
    fn failed_native_initialization_remains_degraded_and_visible_in_memory() {
        let directory = temporary_directory("initialization-fallback");
        let blocked = directory.join("blocked-app-data");
        fs::write(&blocked, b"not a directory").expect("blocking file should be created");
        let state = DiagnosticsState::default();

        assert!(state.initialize(&blocked).is_err());
        let (status, recent) = state
            .with_manager(|manager| Ok((manager.status(), manager.recent_errors(None, None, 10)?)))
            .expect("memory-only diagnostics should remain queryable");

        assert_eq!(status["degraded"], true);
        assert_eq!(status["writable"], false);
        assert!(recent.iter().any(|record| {
            record["event_code"] == "desktop.diagnostics.initialization_failed"
                && record["level"] == "CRITICAL"
                && record["error_id"].as_str().is_some()
        }));
        fs::remove_dir_all(directory).expect("test directory should be removed");
    }

    #[test]
    fn native_rotation_keeps_two_secure_generations() {
        let directory = temporary_directory("rotation");
        let mut manager = Diagnostics::new(&directory).expect("diagnostics should initialize");
        let path = manager.log_dir.join("desktop.log");
        fs::write(&path, vec![b'x'; MAX_FILE_BYTES as usize])
            .expect("rotation fixture should be written");
        manager
            .record(
                DiagnosticLevel::Error,
                "desktop",
                "desktop.test.rotation",
                "A native diagnostic rotation was required.",
                DiagnosticFields::default(),
            )
            .expect("rotated error should be written");
        assert!(rotation_path(&path, 1).is_file());
        assert!(!rotation_path(&path, 3).exists());
        fs::remove_dir_all(directory).expect("test directory should be removed");
    }
}
