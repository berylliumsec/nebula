//! Pure adoption of already-recorded terminal facts. Returned changes grant no
//! execution permission; callers own phase ordering, CAS retries and commits.
use crate::{Error, Result};
use chrono::{DateTime, NaiveDateTime, SecondsFormat};
use nebula_assistant_domain::{
    dependencies::{DependencyKind, StoredDependency},
    records::{AssistantKind, StoredAssistantRecord},
    tool_receipt::{
        self, ReceiptError, ToolReceipt, exact_integer, python_equal, python_int, python_unique,
        truthy,
    },
};
use nebula_assistant_storage::entities::Error as StorageError;
use num_bigint::BigInt;
use serde_json::{Map, Value, json};
use std::{
    borrow::Cow,
    collections::{HashMap, HashSet},
};

pub struct HookPatch {
    pub id: String,
    pub expected_revision: i64,
    pub changes: Map<String, Value>,
}
pub struct HookReduction {
    pub turn_changes: Map<String, Value>,
    pub late_hooks: Vec<HookPatch>,
}
fn receipt_error(error: ReceiptError) -> Error {
    match error {
        ReceiptError::TooLarge => StorageError::ReadLimit.into(),
        ReceiptError::Invalid | ReceiptError::Schema => Error::LegacyUnhandled,
    }
}
fn charge(value: &Value) -> Result<usize> {
    tool_receipt::bounded(value).map_err(receipt_error)
}
fn number(value: &BigInt) -> Result<Value> {
    serde_json::from_str(&value.to_string()).map_err(|_| Error::LegacyUnhandled)
}
fn field_integer(p: &Value, key: &str) -> Result<BigInt> {
    exact_integer(&p[key]).ok_or(Error::LegacyUnhandled)
}
fn phase<'a>(
    turn: &'a StoredAssistantRecord,
    key: &str,
) -> Result<Option<(&'a Value, &'a Vec<Value>)>> {
    if turn.kind() != AssistantKind::Turn {
        return Err(Error::LegacyUnhandled);
    }
    let p = turn.payload();
    let recovery = &p["request_snapshot"]["recovery"];
    if p["status"] != "interrupted" || !recovery.is_object() {
        return Ok(None);
    }
    Ok(recovery
        .get(key)
        .and_then(Value::as_array)
        .filter(|ids| !ids.is_empty())
        .map(|ids| (recovery, ids)))
}
fn remaining(unknown: &[Value], settled: &[String]) -> Vec<Value> {
    let settled: HashSet<_> = settled.iter().map(String::as_str).collect();
    unknown
        .iter()
        .filter(|v| !v.as_str().is_some_and(|s| settled.contains(s)))
        .cloned()
        .collect()
}
fn recorded(
    recovery: &Value,
    key: &str,
    settled: &[String],
) -> std::result::Result<Vec<Value>, ReceiptError> {
    python_unique(
        recovery[key]
            .as_array()
            .into_iter()
            .flatten()
            .cloned()
            .chain(settled.iter().cloned().map(Value::String)),
    )
}
fn snapshot(
    p: &Value,
    recovery: &Value,
    key: &str,
    left: Vec<Value>,
    recorded_key: &str,
    recorded: Vec<Value>,
) -> Result<Value> {
    let mut recovery = recovery.as_object().ok_or(Error::LegacyUnhandled)?.clone();
    recovery.insert(key.into(), left.into());
    recovery.insert(recorded_key.into(), recorded.into());
    let mut request = p["request_snapshot"]
        .as_object()
        .ok_or(Error::LegacyUnhandled)?
        .clone();
    request.insert("recovery".into(), recovery.into());
    Ok(request.into())
}
pub fn result_summary(output: &Value) -> Result<String> {
    if output["schema"] == "nebula.tool-result/v2" {
        if output["status"] == "timed_out" {
            return Ok("Tool execution timed out; partial output is available".into());
        }
        if output["status"] == "failed" {
            let exit = if output["exit_code"].is_null() {
                "None".into()
            } else if let Some(s) = output["exit_code"].as_str() {
                s.into()
            } else {
                output["exit_code"].to_string()
            };
            return Ok(format!("Tool execution failed with exit code {exit}"));
        }
        if truthy(&output["incomplete"]) {
            return Ok("Tool execution completed with incomplete captured output".into());
        }
        if output["warnings"].as_array().is_some_and(|w| !w.is_empty()) {
            return Ok("Tool execution completed with parser/capture warnings".into());
        }
        return Ok("Tool execution completed; inspect artifacts with tool_output.search".into());
    }
    let mut keys: Vec<_> = output
        .as_object()
        .ok_or(Error::LegacyUnhandled)?
        .keys()
        .map(String::as_str)
        .collect();
    keys.sort_unstable();
    keys.truncate(6);
    Ok(if keys.is_empty() {
        "Capability completed".into()
    } else {
        format!("Result fields: {}", keys.join(", "))
    })
}
fn history_step(value: &Value) -> Result<BigInt> {
    python_int(value).map_err(|error|{
        if let Some(text)=value.as_str(){
            let digits=text.chars().filter(|c|c.is_ascii_digit()).count();
            if digits>4300 {return Error::LegacyValueError(format!("Exceeds the limit (4300 digits) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"));}
            match crate::subagents::python_string_repr(text){Ok(repr)=>Error::LegacyValueError(format!("invalid literal for int() with base 10: {repr}")),Err(error)=>error}
        }else{receipt_error(error)}
    })
}

pub fn tools<'a, F>(
    turn: &StoredAssistantRecord,
    mut lookup: F,
) -> Result<Option<Map<String, Value>>>
where
    F: FnMut(&str) -> Result<Option<&'a StoredDependency>>,
{
    let Some((recovery, unknown)) = phase(turn, "unknown_tool_call_ids")? else {
        return Ok(None);
    };
    let p = turn.payload();
    let source_history = p["tool_history"].as_array().ok_or(Error::LegacyUnhandled)?;
    let mut history: Vec<Option<Cow<'_, Value>>> = source_history
        .iter()
        .map(|v| Some(Cow::Borrowed(v)))
        .collect();
    let mut positions: HashMap<String, Vec<usize>> = HashMap::new();
    let mut history_bytes = 2usize;
    for (i, item) in source_history.iter().enumerate() {
        history_bytes = history_bytes
            .saturating_add(charge(item)?)
            .saturating_add(1);
        if let Some(id) = item.get("tool_call_id").and_then(Value::as_str) {
            positions.entry(id.into()).or_default().push(i);
        }
    }
    let mut next = field_integer(p, "next_step")?;
    let mut execution = field_integer(p, "execution_tool_calls")?;
    let mut artifacts = field_integer(p, "artifact_queries")?;
    let mut settled = Vec::new();
    for reference in unknown {
        let Some(id) = reference.as_str() else {
            continue;
        };
        let Some(record) = lookup(id)? else {
            continue;
        };
        if record.kind() != DependencyKind::ToolCall {
            return Err(Error::LegacyUnhandled);
        }
        let call = record.payload();
        if call["chat_turn_id"] != p["id"]
            || !matches!(call["status"].as_str(), Some("complete" | "failed"))
            || !call["result"].is_object()
            || call["result"]["schema"] != "nebula.tool-result/v2"
        {
            continue;
        }
        let receipt = match ToolReceipt::decode(&call["result"]) {
            Ok(receipt) => receipt,
            Err(ReceiptError::Invalid) => continue,
            Err(error) => return Err(receipt_error(error)),
        };
        let output = receipt.payload();
        if output["tool_call_id"] != call["id"]
            || output["tool_name"] != call["tool_name"]
            || truthy(&output["results_url"])
            || (call["status"] == "complete") != (output["status"] == "completed")
        {
            continue;
        }
        let Some(step) = exact_integer(&call["metadata"]["provider_step"])
            .filter(|step| step >= &BigInt::from(0))
        else {
            continue;
        };
        let Some(model_call) = call["metadata"]["provider_call_id"]
            .as_str()
            .filter(|id| !id.is_empty())
        else {
            continue;
        };
        let call_id = call["id"].as_str().ok_or(Error::LegacyUnhandled)?;
        let candidate = &call["metadata"]["provider_history_intent"];
        let valid_intent = candidate.is_object()
            && python_equal(&candidate["tool_call_id"], &call["id"])
            && candidate["model_call_id"] == model_call
            && python_equal(&candidate["step"], &call["metadata"]["provider_step"])
            && candidate["name"] == call["tool_name"]
            && python_equal(&candidate["arguments"], &call["arguments"]);
        let mut entry = if valid_intent {
            candidate.as_object().unwrap().clone()
        } else {
            json!({"step":call["metadata"]["provider_step"],"model_call_id":model_call,"tool_call_id":call["id"],"name":call["tool_name"],"arguments":call["arguments"],"budget_class":call["metadata"].get("budget_class").cloned().unwrap_or_else(||json!("execution"))}).as_object().unwrap().clone()
        };
        let prior = positions
            .get(call_id)
            .and_then(|slots| slots.first())
            .and_then(|&index| history[index].as_deref());
        let existed = prior.is_some();
        if let Some(prior) = prior {
            entry.extend(prior.as_object().ok_or(Error::LegacyUnhandled)?.clone());
        }
        entry.extend(json!({"status":if call["status"]=="complete"{"complete"}else{"failed"},"provider_result":receipt.serialize_model_result().map_err(receipt_error)?,"trusted_result":false,"result_artifact_id":call["result_artifact_id"],"artifacts":output.get("artifacts").cloned().unwrap_or_else(||json!([])),"result_summary":result_summary(output)?,"recovered_from_recorded_result":true}).as_object().unwrap().clone());
        if !existed {
            if entry.get("budget_class") == Some(&json!("artifact_query")) {
                artifacts += 1;
            } else if entry.get("budget_class") == Some(&json!("execution")) {
                execution += 1;
            }
        }
        let entry = Value::Object(entry);
        let entry_bytes = charge(&entry)?;
        if let Some(slots) = positions.remove(call_id) {
            for index in slots {
                if let Some(previous) = history[index].take() {
                    history_bytes =
                        history_bytes.saturating_sub(charge(&previous)?.saturating_add(1));
                }
            }
        }
        history_bytes = history_bytes.saturating_add(entry_bytes).saturating_add(1);
        if history_bytes > tool_receipt::MAX_BYTES {
            return Err(StorageError::ReadLimit.into());
        }
        positions.insert(call_id.into(), vec![history.len()]);
        history.push(Some(Cow::Owned(entry)));
        next = next.max(step + 1);
        settled.push(call_id.into());
    }
    if settled.is_empty() {
        return Ok(None);
    }
    let left = remaining(unknown, &settled);
    let mut sorted = Vec::new();
    let zero = json!(0);
    for entry in history.into_iter().flatten() {
        let key = history_step(entry.get("step").unwrap_or(&zero))?;
        sorted.push((key, entry.into_owned()));
    }
    sorted.sort_by(|a, b| a.0.cmp(&b.0));
    let history: Vec<_> = sorted.into_iter().map(|(_, value)| value).collect();
    let recorded =
        recorded(recovery, "recorded_tool_result_ids", &settled).map_err(receipt_error)?;
    let tool_ids = python_unique(
        p["tool_call_ids"]
            .as_array()
            .ok_or(Error::LegacyUnhandled)?
            .iter()
            .cloned()
            .chain(settled.iter().cloned().map(Value::String)),
    )
    .map_err(receipt_error)?;
    let error = if left.is_empty() && !truthy(&recovery["unknown_hook_execution_ids"]) {
        json!(
            "Core recovered the recorded tool result and will resume this response automatically."
        )
    } else {
        p["error"].clone()
    };
    let changes = json!({"tool_call_ids":tool_ids,"tool_history":history,"next_step":number(&next)?,"execution_tool_calls":number(&execution)?,"artifact_queries":number(&artifacts)?,"error":error,"request_snapshot":snapshot(p,recovery,"unknown_tool_call_ids",left,"recorded_tool_result_ids",recorded)?});
    charge(&changes)?;
    Ok(Some(changes.as_object().unwrap().clone()))
}
fn outcome_time(value: &Value) -> Result<Value> {
    let raw = value.as_str().ok_or(Error::LegacyUnhandled)?;
    let output = if let Ok(time) = DateTime::parse_from_rfc3339(raw) {
        time.to_rfc3339_opts(
            if time.timestamp_subsec_micros() == 0 {
                SecondsFormat::Secs
            } else {
                SecondsFormat::Micros
            },
            true,
        )
    } else {
        let time = NaiveDateTime::parse_from_str(raw, "%Y-%m-%dT%H:%M:%S%.f")
            .map_err(|_| Error::LegacyUnhandled)?;
        time.format(if time.and_utc().timestamp_subsec_micros() == 0 {
            "%Y-%m-%dT%H:%M:%S"
        } else {
            "%Y-%m-%dT%H:%M:%S%.6f"
        })
        .to_string()
    };
    Ok(output.into())
}
pub fn hooks<'a, F>(turn: &StoredAssistantRecord, mut lookup: F) -> Result<Option<HookReduction>>
where
    F: FnMut(&str) -> Result<Option<&'a StoredDependency>>,
{
    let Some((recovery, unknown)) = phase(turn, "unknown_hook_execution_ids")? else {
        return Ok(None);
    };
    let p = turn.payload();
    let mut settled = Vec::new();
    let mut late_hooks = Vec::new();
    // Account for the complete reduction envelope, including patch identity and
    // revisions. The turn changes replace this initial null value below.
    let mut bytes = charge(&json!({"turn_changes":null,"late_hooks":[]}))? - 4;
    for reference in unknown {
        let Some(id) = reference.as_str() else {
            continue;
        };
        let Some(record) = lookup(id)? else {
            continue;
        };
        if record.kind() != DependencyKind::NativeHookExecution {
            return Err(Error::LegacyUnhandled);
        }
        let hook = record.payload();
        if hook["chat_turn_id"] != p["id"] {
            continue;
        }
        if hook["status"] == "complete" && python_equal(&hook["exit_code"], &json!(0)) {
            settled.push(hook["id"].as_str().ok_or(Error::LegacyUnhandled)?.into());
        } else if hook["status"] == "interrupted"
            && !hook["late_outcome"].is_null()
            && hook["late_outcome"]["status"] == "complete"
            && python_equal(&hook["late_outcome"]["exit_code"], &json!(0))
        {
            let outcome = &hook["late_outcome"];
            let changes = json!({"status":"complete","completed_at":outcome_time(&outcome["observed_at"])?,"exit_code":outcome["exit_code"],"stdout":outcome["stdout"],"stderr":outcome["stderr"],"error":null});
            let id = hook["id"]
                .as_str()
                .ok_or(Error::LegacyUnhandled)?
                .to_owned();
            let expected_revision = hook["revision"].as_i64().ok_or(Error::LegacyUnhandled)?;
            let envelope_bytes =
                charge(&json!({"id":id,"expected_revision":expected_revision,"changes":null}))? - 4;
            bytes = bytes
                .saturating_add(envelope_bytes)
                .saturating_add(charge(&changes)?)
                .saturating_add(usize::from(!late_hooks.is_empty()));
            if bytes > tool_receipt::MAX_BYTES {
                return Err(StorageError::ReadLimit.into());
            }
            settled.push(id.clone());
            late_hooks.push(HookPatch {
                id,
                expected_revision,
                changes: changes.as_object().unwrap().clone(),
            });
        }
    }
    if settled.is_empty() {
        return Ok(None);
    }
    let left = remaining(unknown, &settled);
    // Python builds this dict.fromkeys inside its transaction, so malformed
    // hashable identity lists retain the storage diagnostic feature.
    let recorded =
        recorded(recovery, "recorded_hook_outcome_ids", &settled).map_err(|error| match error {
            ReceiptError::TooLarge => StorageError::ReadLimit.into(),
            _ => Error::LegacyStorageUnhandled,
        })?;
    let error = if left.is_empty() && !truthy(&recovery["unknown_tool_call_ids"]) {
        json!(
            "Core recovered the recorded hook outcome and will resume this response automatically."
        )
    } else {
        p["error"].clone()
    };
    let changes = json!({"error":error,"request_snapshot":snapshot(p,recovery,"unknown_hook_execution_ids",left,"recorded_hook_outcome_ids",recorded)?});
    bytes = bytes.saturating_add(charge(&changes)?);
    if bytes > tool_receipt::MAX_BYTES {
        return Err(StorageError::ReadLimit.into());
    }
    Ok(Some(HookReduction {
        turn_changes: changes.as_object().unwrap().clone(),
        late_hooks,
    }))
}
