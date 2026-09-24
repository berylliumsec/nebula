//! Strict retained ToolResultReceipt hydration. This codec does not grant
//! execution authority; reducers additionally require matching terminal facts.
use jsonschema::Validator;
use num_bigint::BigInt;
use num_traits::{FromPrimitive, Zero};
use serde_json::{Value, json};
use std::{collections::HashSet, io::Write, sync::LazyLock};

pub const MAX_BYTES: usize = 16 * 1024 * 1024;
const MAX_INTEGER_DIGITS: usize = 4300;
#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum ReceiptError {
    #[error("Recorded tool receipt is invalid")]
    Invalid,
    #[error("Recorded tool receipt exceeds its byte bound")]
    TooLarge,
    #[error("Recorded tool receipt schema is unavailable")]
    Schema,
}
type Result<T> = std::result::Result<T, ReceiptError>;
static SCHEMA: LazyLock<Result<Value>> = LazyLock::new(|| {
    let fixture: Value =
        serde_json::from_str(include_str!("../../../compatibility/python-recovery.json"))
            .map_err(|_| ReceiptError::Schema)?;
    Ok(fixture["receipt_schema"].clone())
});
static VALIDATOR: LazyLock<Result<Validator>> = LazyLock::new(|| {
    jsonschema::draft202012::options()
        .build(SCHEMA.as_ref().map_err(|_| ReceiptError::Schema)?)
        .map_err(|_| ReceiptError::Schema)
});
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NonFinite {
    pub path: Vec<String>,
    pub value: &'static str,
}
#[derive(Clone)]
pub struct ToolReceipt {
    payload: Value,
    nonfinite: Vec<NonFinite>,
}
impl std::fmt::Debug for ToolReceipt {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ToolReceipt { redacted }")
    }
}
struct Counter(usize);
impl Write for Counter {
    fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
        self.0 = self.0.saturating_add(b.len());
        if self.0 > MAX_BYTES {
            Err(std::io::Error::other("Receipt limit"))
        } else {
            Ok(b.len())
        }
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
pub fn bounded(value: &Value) -> Result<usize> {
    let mut counter = Counter(0);
    serde_json::to_writer(&mut counter, value).map_err(|_| ReceiptError::TooLarge)?;
    Ok(counter.0)
}
fn reference<'a>(schema: &'a Value, root: &'a Value) -> Result<&'a Value> {
    if let Some(path) = schema["$ref"].as_str() {
        root.pointer(path.strip_prefix('#').ok_or(ReceiptError::Schema)?)
            .ok_or(ReceiptError::Schema)
    } else {
        Ok(schema)
    }
}
fn number_value(value: &BigInt) -> Result<Value> {
    serde_json::from_str(&value.to_string()).map_err(|_| ReceiptError::Invalid)
}
pub fn exact_integer(value: &Value) -> Option<BigInt> {
    let number = value.as_number()?.to_string();
    if number.contains(['.', 'e', 'E'])
        || number.strip_prefix('-').unwrap_or(&number).len() > MAX_INTEGER_DIGITS
    {
        return None;
    }
    BigInt::parse_bytes(number.as_bytes(), 10)
}
fn float(value: &Value) -> Option<f64> {
    match value {
        Value::Bool(v) => Some(if *v { 1.0 } else { 0.0 }),
        Value::Number(v) => v.to_string().parse().ok(),
        Value::String(v) => clean_numeric(v)?.parse().ok(),
        _ => None,
    }
}
fn clean_numeric(value: &str) -> Option<String> {
    let value = value.trim();
    if !value.is_ascii() {
        return None;
    }
    let bytes = value.as_bytes();
    let mut output = String::with_capacity(value.len());
    for (i, &c) in bytes.iter().enumerate() {
        if c == b'_' {
            if i == 0
                || i + 1 == bytes.len()
                || !bytes[i - 1].is_ascii_digit()
                || !bytes[i + 1].is_ascii_digit()
            {
                return None;
            }
        } else {
            output.push(c as char);
        }
    }
    Some(output)
}
fn decimal(c: char) -> Option<u8> {
    let cp = c as u32;
    let at = DECIMAL_ZERO.partition_point(|&start| start <= cp);
    (at != 0 && cp < DECIMAL_ZERO[at - 1] + 10).then(|| (cp - DECIMAL_ZERO[at - 1]) as u8)
}
fn ascii_space(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}
fn int_text(text: &str) -> Option<BigInt> {
    let mut ascii = String::with_capacity(text.len());
    for c in text.chars() {
        if let Some(digit) = decimal(c) {
            ascii.push((b'0' + digit) as char);
        } else if c as u32 > 127 && c.is_whitespace() {
            ascii.push(' ');
        } else if c.is_ascii() {
            ascii.push(c);
        } else {
            return None;
        }
    }
    let ascii = ascii.trim_matches(|c: char| c.is_ascii() && ascii_space(c as u8));
    let (negative, digits) = ascii.strip_prefix('-').map_or_else(
        || (false, ascii.strip_prefix('+').unwrap_or(ascii)),
        |s| (true, s),
    );
    let mut canonical = String::new();
    if negative {
        canonical.push('-');
    }
    let bytes = digits.as_bytes();
    let mut count = 0;
    for (i, &b) in bytes.iter().enumerate() {
        if b.is_ascii_digit() {
            canonical.push(b as char);
            count += 1;
        } else if b == b'_'
            && i > 0
            && i + 1 < bytes.len()
            && bytes[i - 1].is_ascii_digit()
            && bytes[i + 1].is_ascii_digit()
        {
        } else {
            return None;
        }
    }
    if count == 0 || count > MAX_INTEGER_DIGITS {
        return None;
    }
    BigInt::parse_bytes(canonical.as_bytes(), 10)
}
/// Python int(value), used for retained history sort keys. String conversion
/// deliberately differs from Pydantic receipt-field coercion.
pub fn python_int(value: &Value) -> Result<BigInt> {
    match value {
        Value::Bool(v) => Ok(BigInt::from(u8::from(*v))),
        Value::Number(_) => exact_integer(value)
            .or_else(|| float(value).and_then(BigInt::from_f64))
            .ok_or(ReceiptError::Invalid),
        Value::String(text) => int_text(text).ok_or(ReceiptError::Invalid),
        _ => Err(ReceiptError::Invalid),
    }
}
fn coerced_int(value: &Value) -> Result<BigInt> {
    if let Value::String(text) = value {
        // Pydantic also accepts an integral decimal spelling such as "12.0".
        let text = clean_numeric(text).ok_or(ReceiptError::Invalid)?;
        if let Some(v) = int_text(&text) {
            return Ok(v);
        }
        if let Some((integer, fraction)) = text.split_once('.')
            && !fraction.is_empty()
            && fraction.bytes().all(|b| b == b'0')
        {
            return int_text(integer).ok_or(ReceiptError::Invalid);
        }
        return Err(ReceiptError::Invalid);
    }
    if let Some(v) = exact_integer(value) {
        return Ok(v);
    }
    let f = float(value).ok_or(ReceiptError::Invalid)?;
    if !f.is_finite() || f.fract() != 0.0 || f <= i64::MIN as f64 || f >= -(i64::MIN as f64) {
        return Err(ReceiptError::Invalid);
    }
    BigInt::from_f64(f).ok_or(ReceiptError::Invalid)
}
fn coerce(
    schema: &Value,
    root: &Value,
    p: &mut Value,
    path: &mut Vec<String>,
    nonfinite: &mut Vec<NonFinite>,
) -> Result<()> {
    if path.len() > 128 {
        return Err(ReceiptError::Invalid);
    }
    let schema = reference(schema, root)?;
    if let Some(field) = schema["discriminator"]["propertyName"].as_str() {
        let tag = p
            .get(field)
            .and_then(Value::as_str)
            .ok_or(ReceiptError::Invalid)?;
        let target = schema["discriminator"]["mapping"]
            .get(tag)
            .and_then(Value::as_str)
            .ok_or(ReceiptError::Invalid)?;
        return coerce(
            root.pointer(target.strip_prefix('#').ok_or(ReceiptError::Schema)?)
                .ok_or(ReceiptError::Schema)?,
            root,
            p,
            path,
            nonfinite,
        );
    }
    if let Some(branches) = schema["anyOf"].as_array() {
        if p.is_null() && branches.iter().any(|b| b["type"] == "null") {
            return Ok(());
        }
        let branch = branches
            .iter()
            .find(|b| b["type"] != "null")
            .ok_or(ReceiptError::Invalid)?;
        return coerce(branch, root, p, path, nonfinite);
    }
    match schema["type"].as_str() {
        Some("object") => {
            let object = p.as_object_mut().ok_or(ReceiptError::Invalid)?;
            if schema["title"] == "ToolResultReceipt" {
                for (key, value) in [
                    ("observations", json!([])),
                    ("timing", json!({})),
                    ("artifacts", json!([])),
                    ("parser", json!({})),
                    ("warnings", json!([])),
                    (
                        "next_actions",
                        json!(["tool_output.search", "tool_output.read"]),
                    ),
                ] {
                    object.entry(key).or_insert(value);
                }
            }
            if let Some(properties) = schema["properties"].as_object() {
                for (key, field) in properties {
                    if !object.contains_key(key)
                        && let Some(default) = field.get("default")
                    {
                        object.insert(key.clone(), default.clone());
                    }
                    if let Some(value) = object.get_mut(key) {
                        path.push(key.clone());
                        coerce(field, root, value, path, nonfinite)?;
                        path.pop();
                    }
                }
            }
        }
        Some("array") => {
            let items = p.as_array_mut().ok_or(ReceiptError::Invalid)?;
            if schema["maxItems"]
                .as_u64()
                .is_some_and(|limit| items.len() as u64 > limit)
            {
                return Err(ReceiptError::Invalid);
            }
            for (i, item) in items.iter_mut().enumerate() {
                path.push(i.to_string());
                coerce(&schema["items"], root, item, path, nonfinite)?;
                path.pop();
            }
        }
        Some("integer") => {
            *p = number_value(&coerced_int(p)?)?;
        }
        Some("number") => {
            let f = float(p).ok_or(ReceiptError::Invalid)?;
            if f.is_nan() || f == f64::NEG_INFINITY {
                return Err(ReceiptError::Invalid);
            }
            if f == f64::INFINITY {
                nonfinite.push(NonFinite {
                    path: path.clone(),
                    value: "Infinity",
                });
                *p = json!(0.0); // Validate ge0; retain the nonfinite value outside JSON.
            } else {
                *p = serde_json::Number::from_f64(f)
                    .ok_or(ReceiptError::Invalid)?
                    .into();
            }
        }
        Some("boolean") => {
            let b = match p {
                Value::Bool(v) => Some(*v),
                Value::Number(_) => float(p).and_then(|f| {
                    if f == 0.0 {
                        Some(false)
                    } else if f == 1.0 {
                        Some(true)
                    } else {
                        None
                    }
                }),
                Value::String(v) => match v.to_ascii_lowercase().as_str() {
                    "0" | "off" | "f" | "false" | "n" | "no" => Some(false),
                    "1" | "on" | "t" | "true" | "y" | "yes" => Some(true),
                    _ => None,
                },
                _ => None,
            }
            .ok_or(ReceiptError::Invalid)?;
            *p = b.into();
        }
        Some("string") => {
            if !p.is_string() {
                return Err(ReceiptError::Invalid);
            }
        }
        Some("null") => {
            if !p.is_null() {
                return Err(ReceiptError::Invalid);
            }
        }
        _ => {}
    }
    Ok(())
}
impl ToolReceipt {
    pub fn decode(value: &Value) -> Result<Self> {
        bounded(value)?;
        let schema = SCHEMA.as_ref().map_err(|_| ReceiptError::Schema)?;
        let mut payload = value.clone();
        let mut nonfinite = Vec::new();
        coerce(
            schema,
            schema,
            &mut payload,
            &mut Vec::new(),
            &mut nonfinite,
        )?;
        if !VALIDATOR
            .as_ref()
            .map_err(|_| ReceiptError::Schema)?
            .is_valid(&payload)
        {
            return Err(ReceiptError::Invalid);
        }
        for special in &nonfinite {
            let pointer = format!("/{}", special.path.join("/"));
            *payload.pointer_mut(&pointer).ok_or(ReceiptError::Invalid)? = Value::Null;
        }
        bounded(&payload)?;
        Ok(Self { payload, nonfinite })
    }
    /// JSON-safe model fields; nonfinite_paths retains values JSON cannot hold.
    pub fn payload(&self) -> &Value {
        &self.payload
    }
    pub fn nonfinite_paths(&self) -> &[NonFinite] {
        &self.nonfinite
    }
    pub fn serialize_model_result(&self) -> Result<String> {
        serialize(&self.payload, &self.nonfinite)
    }
}

fn numeric(value: &Value) -> bool {
    value.is_number() || value.is_boolean()
}
fn integer_equal(value: &Value) -> Option<BigInt> {
    if value.is_boolean() {
        return Some(BigInt::from(u8::from(value.as_bool()?)));
    }
    exact_integer(value).or_else(|| {
        let f = float(value)?;
        (f.is_finite() && f.fract() == 0.0)
            .then(|| BigInt::from_f64(f))
            .flatten()
    })
}
pub fn python_equal(left: &Value, right: &Value) -> bool {
    if numeric(left) && numeric(right) {
        if let (Some(a), Some(b)) = (integer_equal(left), integer_equal(right)) {
            return a == b;
        }
        if integer_equal(left).is_some() || integer_equal(right).is_some() {
            return false;
        }
        return float(left) == float(right);
    }
    match (left, right) {
        (Value::Array(a), Value::Array(b)) => {
            a.len() == b.len() && a.iter().zip(b).all(|(x, y)| python_equal(x, y))
        }
        (Value::Object(a), Value::Object(b)) => {
            a.len() == b.len()
                && a.iter()
                    .all(|(key, v)| b.get(key).is_some_and(|other| python_equal(v, other)))
        }
        _ => left == right,
    }
}
#[derive(PartialEq, Eq, Hash)]
enum HashValue {
    Null,
    Integer(BigInt),
    Float(u64),
    Text(String),
}
#[derive(PartialEq, Eq, Hash)]
pub struct Hashable(HashValue);
pub fn hashable(value: &Value) -> Result<Hashable> {
    Ok(Hashable(match value {
        Value::Null => HashValue::Null,
        Value::String(v) => HashValue::Text(v.clone()),
        v if numeric(v) => {
            if let Some(integer) = integer_equal(v) {
                HashValue::Integer(integer)
            } else {
                HashValue::Float(float(v).ok_or(ReceiptError::Invalid)?.to_bits())
            }
        }
        _ => return Err(ReceiptError::Invalid),
    }))
}
pub fn python_unique(values: impl IntoIterator<Item = Value>) -> Result<Vec<Value>> {
    let mut seen = HashSet::new();
    let mut result = Vec::new();
    for value in values {
        if seen.insert(hashable(&value)?) {
            result.push(value);
        }
    }
    Ok(result)
}
pub fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(v) => *v,
        Value::Number(_) => exact_integer(value)
            .map_or_else(|| float(value).is_none_or(|f| f != 0.0), |n| !n.is_zero()),
        Value::String(v) => !v.is_empty(),
        Value::Array(v) => !v.is_empty(),
        Value::Object(v) => !v.is_empty(),
    }
}

struct JsonOutput {
    bytes: usize,
    prefix: Vec<u8>,
}
impl JsonOutput {
    fn write(&mut self, bytes: &[u8]) {
        self.bytes = self.bytes.saturating_add(bytes.len());
        if self.prefix.len() < 8193 {
            let n = bytes.len().min(8193 - self.prefix.len());
            self.prefix.extend_from_slice(&bytes[..n]);
        }
    }
    fn string(&mut self, text: &str) {
        self.write(b"\"");
        let mut start = 0;
        for (at, c) in text.char_indices() {
            if c >= '\u{20}' && !matches!(c, '"' | '\\') {
                continue;
            }
            self.write(&text.as_bytes()[start..at]);
            match c {
                '"' => self.write(b"\\\""),
                '\\' => self.write(b"\\\\"),
                '\u{8}' => self.write(b"\\b"),
                '\u{c}' => self.write(b"\\f"),
                '\n' => self.write(b"\\n"),
                '\r' => self.write(b"\\r"),
                '\t' => self.write(b"\\t"),
                c => {
                    const HEX: &[u8; 16] = b"0123456789abcdef";
                    self.write(&[
                        b'\\',
                        b'u',
                        b'0',
                        b'0',
                        HEX[(c as usize) >> 4],
                        HEX[c as usize & 15],
                    ]);
                }
            }
            start = at + c.len_utf8();
        }
        self.write(&text.as_bytes()[start..]);
        self.write(b"\"");
    }
    fn value(
        &mut self,
        value: &Value,
        path: &mut Vec<String>,
        special: &[NonFinite],
    ) -> Result<()> {
        if path.len() > 128 {
            return Err(ReceiptError::Invalid);
        }
        if let Some(nonfinite) = special.iter().find(|n| &n.path == path) {
            self.write(nonfinite.value.as_bytes());
            return Ok(());
        }
        match value {
            Value::Null => self.write(b"null"),
            Value::Bool(v) => self.write(if *v { b"true" } else { b"false" }),
            Value::String(v) => self.string(v),
            Value::Number(v) => {
                let raw = v.to_string();
                if raw.contains(['.', 'e', 'E']) {
                    let f: f64 = raw.parse().map_err(|_| ReceiptError::Invalid)?;
                    if f.is_infinite() {
                        self.write(if f.is_sign_negative() {
                            b"-Infinity"
                        } else {
                            b"Infinity"
                        });
                    } else {
                        let shortest = format!("{f:?}");
                        if let Some((mantissa, exponent)) = shortest.split_once('e') {
                            let exponent: i32 =
                                exponent.parse().map_err(|_| ReceiptError::Invalid)?;
                            self.write(
                                format!(
                                    "{mantissa}e{}{:02}",
                                    if exponent < 0 { '-' } else { '+' },
                                    exponent.unsigned_abs()
                                )
                                .as_bytes(),
                            );
                        } else {
                            self.write(shortest.as_bytes());
                        }
                    }
                } else {
                    self.write(if raw == "-0" { b"0" } else { raw.as_bytes() });
                }
            }
            Value::Array(values) => {
                self.write(b"[");
                for (i, v) in values.iter().enumerate() {
                    if i != 0 {
                        self.write(b", ");
                    }
                    path.push(i.to_string());
                    self.value(v, path, special)?;
                    path.pop();
                }
                self.write(b"]");
            }
            Value::Object(values) => {
                self.write(b"{");
                let mut keys: Vec<_> = values.keys().collect();
                keys.sort_unstable();
                for (i, key) in keys.into_iter().enumerate() {
                    if i != 0 {
                        self.write(b", ");
                    }
                    self.string(key);
                    self.write(b": ");
                    path.push(key.clone());
                    self.value(&values[key], path, special)?;
                    path.pop();
                }
                self.write(b"}");
            }
        }
        Ok(())
    }
}
fn serialize(value: &Value, special: &[NonFinite]) -> Result<String> {
    bounded(value)?;
    let mut writer = JsonOutput {
        bytes: 0,
        prefix: Vec::new(),
    };
    writer.value(value, &mut Vec::new(), special)?;
    if writer.bytes <= 8192 {
        String::from_utf8(writer.prefix).map_err(|_| ReceiptError::Invalid)
    } else {
        let fallback = json!({"schema":"nebula.bounded-result/v1","status":"incomplete","warning":"trusted result exceeded the model-delivery bound","original_bytes":writer.bytes});
        let mut fallback_writer = JsonOutput {
            bytes: 0,
            prefix: Vec::new(),
        };
        fallback_writer.value(&fallback, &mut Vec::new(), &[])?;
        String::from_utf8(fallback_writer.prefix).map_err(|_| ReceiptError::Invalid)
    }
}
pub fn serialize_model_result(value: &Value) -> Result<String> {
    serialize(value, &[])
}

// Generated with CPython3.12 / Unicode15: every scalar whose
// unicodedata.decimal(chr(cp), None)==0; each starts a ten-scalar decimal block.
const DECIMAL_ZERO: &[u32] = &[
    0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
    0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
    0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0, 0xff10,
    0x104a0, 0x10d30, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450, 0x114d0, 0x11650,
    0x116c0, 0x11730, 0x118e0, 0x11950, 0x11c50, 0x11d50, 0x11da0, 0x11f50, 0x16a60, 0x16ac0,
    0x16b50, 0x1d7ce, 0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e950,
    0x1fbf0,
];
