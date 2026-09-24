//! Complete saved Goal/usage model contracts; no lifecycle transitions.
use super::*;
use serde::de::{DeserializeSeed, IgnoredAny, SeqAccess};
use std::collections::HashMap;

const fn int(name: &'static str, min: i64, nullable: bool) -> Field {
    Field {
        name,
        kind: FieldType::Integer { min, max: None },
        nullable,
    }
}
const fn field(name: &'static str, kind: FieldType) -> Field {
    Field {
        name,
        kind,
        nullable: false,
    }
}
const fn optional_time(name: &'static str) -> Field {
    Field {
        name,
        kind: FieldType::OptionalTime,
        nullable: true,
    }
}
pub(super) const USAGE_FIELDS: [Field; 3] = [
    int("input_tokens", 0, false),
    int("output_tokens", 0, false),
    int("total_tokens", 0, false),
];
pub(super) const FIELDS: [Field; 34] = [
    super::FIELDS[0],
    super::FIELDS[1],
    super::FIELDS[2],
    super::FIELDS[3],
    text("engagement_id", 0, None, false),
    text("session_id", 0, None, false),
    text("objective", 1, Some(20_000), false),
    field(
        "completion_criteria",
        FieldType::Strings { min: 1, max: 50 },
    ),
    field("plan", FieldType::Strings { min: 0, max: 200 }),
    int("current_step", 0, false),
    field("status", FieldType::GoalStatus),
    int("token_budget", 1, true),
    int("time_budget_seconds", 1, true),
    int("step_budget", 1, true),
    int("child_budget", 0, true),
    field("usage", FieldType::Usage),
    field("elapsed_seconds", FieldType::Float),
    int("children_started", 0, false),
    field(
        "linked_turn_ids",
        FieldType::Strings {
            min: 0,
            max: 10_000,
        },
    ),
    optional_time("started_at"),
    optional_time("active_since"),
    optional_time("paused_at"),
    optional_time("completed_at"),
    text("blocked_reason", 0, Some(2000), true),
    text("completion_summary", 0, Some(20_000), true),
    field("completion_evidence", FieldType::Dictionaries { max: 200 }),
    int("consecutive_stalls", 0, false),
    field("skill_snapshots", FieldType::Dictionaries { max: 20 }),
    text("parent_goal_id", 0, Some(200), true),
    field("child_session_ids", FieldType::Strings { min: 0, max: 32 }),
    text("execution_owner_id", 0, Some(200), true),
    text("execution_claim_id", 0, Some(200), true),
    optional_time("execution_claimed_at"),
    field("metadata", FieldType::Dictionary),
];

pub(super) fn default(model: Model, field: Field) -> Option<Value> {
    if model == Model::ChatTokenUsage {
        return Some(json!(0));
    }
    if model != Model::ChatGoal {
        return None;
    }
    match field.name {
        "current_step" | "children_started" | "consecutive_stalls" => Some(json!(0)),
        "elapsed_seconds" => Some(json!(0.0)),
        "status" => Some(json!("draft")),
        "usage" => Some(json!({"input_tokens":0,"output_tokens":0,"total_tokens":0})),
        "metadata" => Some(json!({})),
        "plan"
        | "linked_turn_ids"
        | "completion_evidence"
        | "skill_snapshots"
        | "child_session_ids" => Some(json!([])),
        _ => None,
    }
}

pub(super) fn coherence(p: &Map<String, Value>) -> Option<&'static str> {
    if p["status"] == "blocked" && p["blocked_reason"].as_str().is_none_or(str::is_empty) {
        return Some("blocked goals require a reason");
    }
    if p["status"] == "completed"
        && (p["completion_summary"].as_str().is_none_or(str::is_empty)
            || p["completion_evidence"]
                .as_array()
                .is_none_or(Vec::is_empty))
    {
        return Some("completed goals require a summary and evidence");
    }
    let count = [
        "execution_owner_id",
        "execution_claim_id",
        "execution_claimed_at",
    ]
    .iter()
    .filter(|name| !p[**name].is_null())
    .count();
    if count != 0 && count != 3 {
        return Some("goal execution ownership must be recorded atomically");
    }
    None
}

type Orders = Vec<(Vec<Location>, Vec<String>)>;

#[derive(Clone, Copy, Default)]
struct OrderCost {
    bytes: usize,
    objects: usize,
    entries: usize,
}
impl OrderCost {
    fn since(self, before: Self) -> Self {
        Self {
            bytes: self.bytes - before.bytes,
            objects: self.objects - before.objects,
            entries: self.entries - before.entries,
        }
    }
}
struct OrderBudget {
    cost: OrderCost,
    limit: usize,
}
impl OrderBudget {
    fn add(&mut self, bytes: usize, objects: usize, entries: usize) -> Result<()> {
        let next = OrderCost {
            bytes: self.cost.bytes.saturating_add(bytes),
            objects: self.cost.objects.saturating_add(objects),
            entries: self.cost.entries.saturating_add(entries),
        };
        // Outer array and inter-object commas are counted once. Each object's
        // pair, full path and key array are charged before retaining them.
        if next.entries > MAX_ISSUES
            || next.bytes.saturating_add(next.objects.saturating_sub(1)) + 2 > self.limit
        {
            return Err(RecordError::TooLarge);
        }
        self.cost = next;
        Ok(())
    }
    fn release(&mut self, cost: OrderCost) {
        self.cost.bytes -= cost.bytes;
        self.cost.objects -= cost.objects;
        self.cost.entries -= cost.entries;
    }
}
struct OrderNode {
    cost: OrderCost,
    kind: OrderKind,
}
enum OrderKind {
    Object(Vec<(String, Option<OrderNode>)>),
    Array(Vec<(usize, OrderNode)>),
}
impl OrderNode {
    fn flatten(self, path: &mut Vec<Location>, orders: &mut Orders) {
        match self.kind {
            OrderKind::Object(entries) => {
                let slot = if path.is_empty() {
                    None
                } else {
                    let slot = orders.len();
                    // The streaming pass charged this clone's exact path size.
                    orders.push((path.clone(), Vec::with_capacity(entries.len())));
                    Some(slot)
                };
                for (key, child) in entries {
                    if let Some(child) = child {
                        path.push(Location::Field(key.clone()));
                        child.flatten(path, orders);
                        path.pop();
                    }
                    if let Some(slot) = slot {
                        orders[slot].1.push(key);
                    }
                }
            }
            OrderKind::Array(entries) => {
                for (index, child) in entries {
                    path.push(Location::Index(index));
                    child.flatten(path, orders);
                    path.pop();
                }
            }
        }
    }
}

struct OrderSeed<'a, 'b> {
    input: &'a Value,
    budget: &'b mut OrderBudget,
    path_bytes: usize,
    root: bool,
}
impl<'de> DeserializeSeed<'de> for OrderSeed<'_, '_> {
    type Value = Option<OrderNode>;
    fn deserialize<D: Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> std::result::Result<Self::Value, D::Error> {
        if !self.input.is_object() && !self.input.is_array() {
            // In particular, do not mistake arbitrary-precision Number's
            // internal serde map for a dictionary supplied by the operator.
            IgnoredAny::deserialize(deserializer)?;
            return Ok(None);
        }
        deserializer.deserialize_any(self)
    }
}
impl<'de> Visitor<'de> for OrderSeed<'_, '_> {
    type Value = Option<OrderNode>;
    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("retained diagnostic input")
    }
    fn visit_map<A: MapAccess<'de>>(
        self,
        mut access: A,
    ) -> std::result::Result<Self::Value, A::Error> {
        let Some(input) = self.input.as_object() else {
            while access.next_entry::<IgnoredAny, IgnoredAny>()?.is_some() {}
            return Ok(None);
        };
        let before = self.budget.cost;
        if !self.root {
            self.budget
                .add(self.path_bytes.saturating_add(5), 1, 1)
                .map_err(serde::de::Error::custom)?;
        }
        let mut entries: Vec<(String, Option<OrderNode>)> = Vec::new();
        // Borrow final input keys: no second owned copy of every dictionary key.
        let mut positions: HashMap<&str, usize> = HashMap::new();
        while let Some(key) = access.next_key::<String>()? {
            let Some((canonical_key, value)) = input.get_key_value(&key) else {
                // A superseded duplicate object's key is absent from the final
                // parsed dictionary, so it cannot appear in any diagnostic.
                access.next_value::<IgnoredAny>()?;
                continue;
            };
            let key_bytes =
                encoded_len(&key, MAX_RECORD_BYTES).map_err(serde::de::Error::custom)?;
            let position = if let Some(&position) = positions.get(canonical_key.as_str()) {
                if let Some(previous) = entries[position].1.take() {
                    self.budget.release(previous.cost);
                }
                position
            } else {
                if !self.root {
                    self.budget
                        .add(key_bytes + usize::from(!entries.is_empty()), 0, 1)
                        .map_err(serde::de::Error::custom)?;
                }
                let position = entries.len();
                positions.insert(canonical_key.as_str(), position);
                entries.push((key, None));
                position
            };
            let path_bytes = self
                .path_bytes
                .saturating_add(usize::from(!self.root))
                .saturating_add(key_bytes);
            entries[position].1 = access.next_value_seed(OrderSeed {
                input: value,
                budget: self.budget,
                path_bytes,
                root: false,
            })?;
        }
        Ok(Some(OrderNode {
            cost: self.budget.cost.since(before),
            kind: OrderKind::Object(entries),
        }))
    }
    fn visit_seq<A: SeqAccess<'de>>(
        self,
        mut access: A,
    ) -> std::result::Result<Self::Value, A::Error> {
        let Some(input) = self.input.as_array() else {
            while access.next_element::<IgnoredAny>()?.is_some() {}
            return Ok(None);
        };
        let before = self.budget.cost;
        let mut entries = Vec::new();
        let mut index = 0usize;
        loop {
            let Some(value) = input.get(index) else {
                while access.next_element::<IgnoredAny>()?.is_some() {}
                break;
            };
            let path_bytes = self.path_bytes
                + usize::from(!self.root)
                + encoded_len(&index, MAX_RECORD_BYTES).map_err(serde::de::Error::custom)?;
            let Some(child) = access.next_element_seed(OrderSeed {
                input: value,
                budget: self.budget,
                path_bytes,
                root: false,
            })?
            else {
                break;
            };
            if let Some(child) = child {
                if entries.is_empty() {
                    // Sparse array nodes are temporary metadata too; bounding
                    // them prevents deep arrays from amplifying tree storage.
                    self.budget.add(0, 0, 1).map_err(serde::de::Error::custom)?;
                }
                entries.push((index, child));
            }
            index += 1;
        }
        Ok((!entries.is_empty()).then(|| OrderNode {
            cost: self.budget.cost.since(before),
            kind: OrderKind::Array(entries),
        }))
    }
    // Earlier values of duplicate keys may have a different type from the
    // surviving input. They carry no surviving order metadata.
    fn visit_bool<E: serde::de::Error>(self, _: bool) -> std::result::Result<Self::Value, E> {
        Ok(None)
    }
    fn visit_i64<E: serde::de::Error>(self, _: i64) -> std::result::Result<Self::Value, E> {
        Ok(None)
    }
    fn visit_u64<E: serde::de::Error>(self, _: u64) -> std::result::Result<Self::Value, E> {
        Ok(None)
    }
    fn visit_f64<E: serde::de::Error>(self, _: f64) -> std::result::Result<Self::Value, E> {
        Ok(None)
    }
    fn visit_str<E: serde::de::Error>(self, _: &str) -> std::result::Result<Self::Value, E> {
        Ok(None)
    }
    fn visit_unit<E: serde::de::Error>(self) -> std::result::Result<Self::Value, E> {
        Ok(None)
    }
}

pub(super) fn nested_orders(bytes: &[u8], input: &Value, limit: usize) -> Result<Orders> {
    let mut budget = OrderBudget {
        cost: OrderCost::default(),
        limit,
    };
    budget.add(0, 0, 0)?;
    let mut deserializer = serde_json::Deserializer::from_slice(bytes);
    let root = OrderSeed {
        input,
        budget: &mut budget,
        path_bytes: 2,
        root: true,
    }
    .deserialize(&mut deserializer)
    .map_err(|_| RecordError::TooLarge)?;
    let mut orders = Vec::with_capacity(budget.cost.objects);
    if let Some(root) = root {
        root.flatten(&mut Vec::new(), &mut orders);
    }
    orders.sort_unstable_by(|(left, _), (right, _)| left.cmp(right));
    Ok(orders)
}

fn fail(
    report: &mut ValidationReport,
    path: Vec<Location>,
    error: Failure,
) -> Result<Option<Value>> {
    report.add_at(error.kind, path.clone(), path, error.msg, error.ctx)?;
    Ok(None)
}
fn dictionary(value: &Value, path: &[Location], report: &ValidationReport) -> FieldResult {
    let Some(object) = value.as_object() else {
        return Err(Failure::simple(
            "dict_type",
            "Input should be a valid dictionary",
        ));
    };
    let mut output = Map::new();
    if let Some(keys) = report.input_order_at(path) {
        for key in keys {
            output.insert(key.trim().into(), object[key].clone());
        }
    } else {
        for (key, value) in object {
            output.insert(key.trim().into(), value.clone());
        }
    }
    Ok(output.into())
}

pub(super) fn validate(
    field: Field,
    value: &Value,
    path: Vec<Location>,
    report: &mut ValidationReport,
) -> Result<Option<Value>> {
    if field.nullable && value.is_null() {
        return Ok(Some(Value::Null));
    }
    match field.kind {
        FieldType::Request(kind) => completion::validate(kind, value, path, report),
        FieldType::Usage => {
            if report.model_input_at(&path) == Some(Model::ChatTokenUsage) {
                return fork::nested(Model::ChatTokenUsage, value, path, report);
            }
            let Some(object) = value.as_object() else {
                return fail(
                    report,
                    path,
                    Failure::context(
                        "model_type",
                        "Input should be a valid dictionary or instance of ChatTokenUsage",
                        json!({"class_name":"ChatTokenUsage"}),
                    ),
                );
            };
            let before = report.len();
            let mut output = Map::new();
            for field in USAGE_FIELDS {
                let Some(value) = object.get(field.name) else {
                    output.insert(field.name.into(), json!(0));
                    continue;
                };
                let mut child = path.clone();
                child.push(Location::Field(field.name.into()));
                if let Some(value) = validate(field, value, child, report)? {
                    output.insert(field.name.into(), value);
                }
            }
            let keys: Vec<_> = report
                .input_order_at(&path)
                .map_or_else(|| object.keys().cloned().collect(), |keys| keys.to_vec());
            for key in keys {
                if !USAGE_FIELDS.iter().any(|f| f.name == key) {
                    let mut child = path.clone();
                    child.push(Location::Field(key));
                    fail(
                        report,
                        child,
                        Failure::simple("extra_forbidden", "Extra inputs are not permitted"),
                    )?;
                }
            }
            Ok((before == report.len()).then_some(output.into()))
        }
        FieldType::Strings { .. } | FieldType::Dictionaries { .. } => {
            let Some(items) = value.as_array() else {
                return fail(
                    report,
                    path,
                    Failure::simple("list_type", "Input should be a valid list"),
                );
            };
            let (min, max) = match field.kind {
                FieldType::Strings { min, max } => (min, max),
                FieldType::Dictionaries { max } => (0, max),
                _ => unreachable!(),
            };
            if items.len() > max {
                return fail(
                    report,
                    path,
                    Failure::context(
                        "too_long",
                        format!(
                            "List should have at most {max} items after validation, not {}",
                            items.len()
                        ),
                        json!({"field_type":"List","max_length":max,"actual_length":items.len()}),
                    ),
                );
            }
            if items.len() < min {
                return fail(
                    report,
                    path,
                    Failure::context(
                        "too_short",
                        format!(
                            "List should have at least {min} item{} after validation, not {}",
                            if min == 1 { "" } else { "s" },
                            items.len()
                        ),
                        json!({"field_type":"List","min_length":min,"actual_length":items.len()}),
                    ),
                );
            }
            if items.len() > MAX_ISSUES {
                return Err(RecordError::TooLarge);
            }
            let before = report.len();
            let mut output = Vec::with_capacity(items.len());
            for (index, item) in items.iter().enumerate() {
                let mut child = path.clone();
                child.push(Location::Index(index));
                let result = if matches!(field.kind, FieldType::Strings { .. }) {
                    validate_field(text(field.name, 0, None, false), item)
                } else {
                    dictionary(item, &child, report)
                };
                match result {
                    Ok(value) => output.push(value),
                    Err(error) => {
                        fail(report, child, error)?;
                    }
                }
            }
            Ok((before == report.len()).then_some(output.into()))
        }
        FieldType::Dictionary => match dictionary(value, &path, report) {
            Ok(value) => Ok(Some(value)),
            Err(error) => fail(report, path, error),
        },
        FieldType::Models { model, max } => fork::models(model, max, value, path, report),
        FieldType::Float => {
            if float_number(value).is_ok_and(|n| n == f64::INFINITY) {
                // Pydantic admits +infinity. Keep a private sentinel until all
                // true model errors/coherence hooks have run; it never escapes
                // hydration or becomes a silently persisted null.
                return Ok(Some(Value::Null));
            }
            match validate_field(field, value) {
                Ok(value) => Ok(Some(value)),
                Err(error) => fail(report, path, error),
            }
        }
        _ => match validate_field(field, value) {
            Ok(value) => Ok(Some(value)),
            Err(error) => fail(report, path, error),
        },
    }
}

pub(super) fn float_number(value: &Value) -> std::result::Result<f64, Failure> {
    match value {
        Value::Bool(v) => Ok(if *v { 1.0 } else { 0.0 }),
        Value::Number(n) => Ok(n.as_f64().unwrap_or_else(|| {
            if n.to_string().starts_with('-') {
                f64::NEG_INFINITY
            } else {
                f64::INFINITY
            }
        })),
        Value::String(text) => {
            let trimmed = text.trim();
            // The baseline first parses trimmed text, then retries underscore
            // removal on the original text. Padding plus underscores therefore
            // fails, although either normalization on its own is accepted.
            if let Ok(number) = trimmed.parse() {
                return Ok(number);
            }
            let bytes = trimmed.as_bytes();
            if !bytes.is_ascii()
                || bytes.iter().enumerate().any(|(i, &b)| {
                    b == b'_'
                        && (i == 0
                            || i + 1 == bytes.len()
                            || !bytes[i - 1].is_ascii_digit()
                            || !bytes[i + 1].is_ascii_digit())
                })
            {
                return Err(Failure::simple(
                    "float_parsing",
                    "Input should be a valid number, unable to parse string as a number",
                ));
            }
            text.replace('_', "").parse().map_err(|_| {
                Failure::simple(
                    "float_parsing",
                    "Input should be a valid number, unable to parse string as a number",
                )
            })
        }
        _ => Err(Failure::simple(
            "float_type",
            "Input should be a valid number",
        )),
    }
}
pub(super) fn scalar(kind: FieldType, value: &Value) -> FieldResult {
    match kind {
        FieldType::OptionalTime => datetime(value).map(Value::String),
        FieldType::Float => {
            let number = float_number(value)?;
            if number.is_nan() || number < 0.0 {
                return Err(Failure::context(
                    "greater_than_equal",
                    "Input should be greater than or equal to 0",
                    json!({"ge":0.0}),
                ));
            }
            serde_json::Number::from_f64(number)
                .map(Value::Number)
                .ok_or_else(|| Failure::simple("finite_number", "Input should be a finite number"))
        }
        FieldType::GoalStatus => {
            const VALUES: [&str; 6] = [
                "draft",
                "running",
                "paused",
                "blocked",
                "completed",
                "cancelled",
            ];
            if value.as_str().is_some_and(|v| VALUES.contains(&v)) {
                return Ok(value.clone());
            }
            Err(Failure::context(
                "enum",
                "Input should be 'draft', 'running', 'paused', 'blocked', 'completed' or 'cancelled'",
                json!({"expected":"'draft', 'running', 'paused', 'blocked', 'completed' or 'cancelled'"}),
            ))
        }
        _ => unreachable!("Goal scalar type"),
    }
}
