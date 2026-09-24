//! Passive saved-policy normalization and privacy decisions. No target access,
//! authorization grant, credential lookup, dispatch or tool capability is created.
use crate::{
    dependencies::{DependencyKind, StoredDependency},
    records::RecordError,
};
use serde::Deserialize;
use std::{
    collections::{BTreeSet, HashMap},
    net::{Ipv4Addr, Ipv6Addr},
    sync::LazyLock,
};

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub enum ProviderPrivacyViolation {
    #[error("engagement references a missing scope policy")]
    MissingPolicy,
    #[error("engagement scope policy belongs to a different engagement")]
    Ownership,
    #[error("engagement scope is local-only and cannot use a cloud provider")]
    LocalOnly,
}
/// `provider_local` is the prepared ModelProvider.config.local authority. An
/// absent policy is an error here: callers skip the lookup only when the already
/// hydrated Engagement has no truthy scope_policy_id, exactly as the source.
pub fn validate_provider_privacy(
    engagement_id: &str,
    policy: Option<&StoredDependency>,
    provider_local: bool,
) -> Result<(), ProviderPrivacyViolation> {
    let policy = policy
        .filter(|p| p.kind() == DependencyKind::ScopePolicy)
        .ok_or(ProviderPrivacyViolation::MissingPolicy)?;
    if policy.payload()["engagement_id"].as_str() != Some(engagement_id) {
        return Err(ProviderPrivacyViolation::Ownership);
    }
    if policy.payload()["local_only"] == true && !provider_local {
        return Err(ProviderPrivacyViolation::LocalOnly);
    }
    Ok(())
}
/// A projection for preparation's capability check, never permission to execute.
pub fn web_search_enabled(policy: &StoredDependency) -> Result<bool, RecordError> {
    if policy.kind() != DependencyKind::ScopePolicy {
        return Err(RecordError::UnknownKind);
    }
    Ok(policy.payload()["web_search"] == true && policy.payload()["local_only"] == false)
}

#[derive(Deserialize)]
struct Tables {
    mappings: HashMap<u32, String>,
    combining: HashMap<u32, u8>,
    compositions: HashMap<String, String>,
    prohibited: Vec<[u32; 2]>,
    bidi_r: Vec<[u32; 2]>,
    bidi_l: Vec<[u32; 2]>,
    nfkc_delimiters: Vec<u32>,
    nonprintable: Vec<[u32; 2]>,
}
static TABLES: LazyLock<Tables> = LazyLock::new(|| {
    serde_json::from_str(include_str!("scope_policy/static_data.json"))
        .expect("captured immutable Unicode tables")
});
fn inside(c: char, ranges: &[[u32; 2]]) -> bool {
    let point = c as u32;
    let i = ranges.partition_point(|r| r[0] <= point);
    i != 0 && point <= ranges[i - 1][1]
}
pub(crate) fn python_trim(value: &str) -> &str {
    value.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}
fn repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut output = String::from(quote);
    for c in value.chars() {
        match c {
            '\\' => output.push_str("\\\\"),
            '\n' => output.push_str("\\n"),
            '\r' => output.push_str("\\r"),
            '\t' => output.push_str("\\t"),
            c if c == quote => {
                output.push('\\');
                output.push(c);
            }
            c if inside(c, &TABLES.nonprintable) => {
                use std::fmt::Write;
                let n = c as u32;
                if n <= 255 {
                    write!(&mut output, "\\x{n:02x}").unwrap();
                } else if n <= 65535 {
                    write!(&mut output, "\\u{n:04x}").unwrap();
                } else {
                    write!(&mut output, "\\U{n:08x}").unwrap();
                }
            }
            c => output.push(c),
        }
    }
    output.push(quote);
    output
}
fn weight(c: char) -> u8 {
    TABLES.combining.get(&(c as u32)).copied().unwrap_or(0)
}
fn compose(a: char, b: char) -> Option<char> {
    let (a, b) = (a as u32, b as u32);
    // Algorithmic Hangul composition from Unicode 3.2, the source IDNA codec.
    if (0x1100..0x1113).contains(&a) && (0x1161..0x1176).contains(&b) {
        return char::from_u32(0xac00 + ((a - 0x1100) * 21 + b - 0x1161) * 28);
    }
    if (0xac00..0xd7a4).contains(&a)
        && (a - 0xac00).is_multiple_of(28)
        && (0x11a8..0x11c3).contains(&b)
    {
        return char::from_u32(a + b - 0x11a7);
    }
    let key: String = [char::from_u32(a)?, char::from_u32(b)?]
        .into_iter()
        .collect();
    TABLES.compositions.get(&key).and_then(|v| v.chars().next())
}
fn nameprep(label: &str) -> Result<String, ()> {
    let mut chars = Vec::new();
    for c in label.chars() {
        if let Some(mapped) = TABLES.mappings.get(&(c as u32)) {
            chars.extend(mapped.chars());
        } else {
            chars.push(c);
        }
    }
    // Canonical reorder each run once; avoid insertion-sort quadratic behavior
    // for a long retained combining-mark label.
    let mut start = 0;
    for i in 0..chars.len() {
        if weight(chars[i]) == 0 {
            chars[start..i].sort_by_key(|c| weight(*c));
            start = i + 1;
        }
    }
    chars[start..].sort_by_key(|c| weight(*c));
    let mut output: Vec<char> = Vec::with_capacity(chars.len());
    let mut starter = None;
    let mut last_weight = 0;
    for c in chars {
        let w = weight(c);
        if let Some(i) = starter
            && (last_weight < w || last_weight == 0)
            && let Some(composed) = compose(output[i], c)
        {
            output[i] = composed;
            continue;
        }
        if w == 0 {
            starter = Some(output.len());
        }
        output.push(c);
        last_weight = w;
    }
    if output.iter().any(|c| inside(*c, &TABLES.prohibited)) {
        return Err(());
    }
    if output.iter().any(|c| inside(*c, &TABLES.bidi_r))
        && (output.iter().any(|c| inside(*c, &TABLES.bidi_l))
            || output.first().is_none_or(|c| !inside(*c, &TABLES.bidi_r))
            || output.last().is_none_or(|c| !inside(*c, &TABLES.bidi_r)))
    {
        return Err(());
    }
    Ok(output.into_iter().collect())
}
fn idna(value: &str) -> Result<String, ()> {
    if value.is_empty() {
        return Ok(String::new());
    }
    let labels: Vec<_> = value
        .split(['.', '\u{3002}', '\u{ff0e}', '\u{ff61}'])
        .collect();
    let mut output = Vec::with_capacity(labels.len());
    for (i, label) in labels.iter().enumerate() {
        if label.is_empty() && i + 1 == labels.len() {
            output.push(String::new());
            continue;
        }
        let mapped = if label.is_ascii() {
            (*label).to_owned()
        } else {
            nameprep(label)?
        };
        let ascii = if mapped.is_ascii() {
            mapped
        } else {
            if mapped.starts_with("xn--") {
                return Err(());
            }
            format!("xn--{}", idna::punycode::encode_str(&mapped).ok_or(())?)
        };
        if ascii.is_empty() || ascii.len() > 63 {
            return Err(());
        }
        output.push(ascii);
    }
    Ok(output.join(".").to_ascii_lowercase())
}

struct Url {
    scheme: String,
    host: String,
    port: String,
    credentials: bool,
    path: String,
    query: String,
    fragment: String,
}
fn split_url(value: &str) -> Result<Url, String> {
    let value = value
        .trim_start_matches(|c| c <= '\u{20}')
        .replace(['\t', '\r', '\n'], "");
    let mut rest = value.as_str();
    let mut scheme = String::new();
    if let Some((candidate, tail)) = rest.split_once(':')
        && candidate
            .as_bytes()
            .first()
            .is_some_and(u8::is_ascii_alphabetic)
        && candidate
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"+.-".contains(&b))
    {
        scheme = candidate.to_ascii_lowercase();
        rest = tail;
    }
    let authority = if let Some(tail) = rest.strip_prefix("//") {
        let end = tail.find(['/', '?', '#']).unwrap_or(tail.len());
        rest = &tail[end..];
        &tail[..end]
    } else {
        ""
    };
    let (tail, fragment) = rest.split_once('#').unwrap_or((rest, ""));
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    // urlsplit validates bracketed hosts before its NFKC authority guard.
    if authority.contains('[') != authority.contains(']') {
        return Err("Invalid IPv6 URL".into());
    }
    let host_authority = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    if authority.contains('[') {
        let address = if let Some((prefix, tail)) = host_authority.split_once('[') {
            let (address, suffix) = tail.split_once(']').ok_or("Invalid IPv6 URL")?;
            if !prefix.is_empty() || !suffix.is_empty() && !suffix.starts_with(':') {
                return Err("Invalid IPv6 URL".into());
            }
            address
        } else {
            host_authority
                .split_once(':')
                .map_or(host_authority, |(host, _)| host)
        };
        if let Some(future) = address.strip_prefix('v') {
            if !future.split_once('.').is_some_and(|(v, h)| {
                !v.is_empty() && v.bytes().all(|c| c.is_ascii_hexdigit()) && !h.is_empty()
            }) {
                return Err("IPvFuture address is invalid".into());
            }
        } else {
            let (ip, scope) = address
                .split_once('%')
                .map_or((address, None), |(a, b)| (a, Some(b)));
            if ip.parse::<Ipv4Addr>().is_ok() {
                return Err("An IPv4 address cannot be in brackets".into());
            }
            if scope.is_some_and(|s| s.is_empty() || s.contains('%'))
                || ip.parse::<Ipv6Addr>().is_err()
            {
                return Err(format!(
                    "{} does not appear to be an IPv4 or IPv6 address",
                    repr(address)
                ));
            }
        }
    }
    if authority
        .chars()
        .any(|c| TABLES.nfkc_delimiters.contains(&(c as u32)))
    {
        return Err(format!(
            "netloc {} contains invalid characters under NFKC normalization",
            repr(authority)
        ));
    }
    let (credentials, host_port) = authority
        .rsplit_once('@')
        .map_or((false, authority), |(_, h)| (true, h));
    let (host, port) = if let Some((_, tail)) = host_port.split_once('[') {
        let (host, tail) = tail.split_once(']').ok_or("Invalid IPv6 URL")?;
        (host, tail.strip_prefix(':').unwrap_or(""))
    } else {
        host_port.split_once(':').unwrap_or((host_port, ""))
    };
    Ok(Url {
        scheme,
        host: host.into(),
        port: port.into(),
        credentials,
        path: path.into(),
        query: query.into(),
        fragment: fragment.into(),
    })
}
fn port(value: &str) -> Result<Option<u16>, String> {
    if value.is_empty() {
        return Ok(None);
    }
    if !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!(
            "Port could not be cast to integer value as {}",
            repr(value)
        ));
    }
    if value.len() > 4300 {
        return Err(format!(
            "Exceeds the limit (4300 digits) for integer string conversion: value has {} digits; use sys.set_int_max_str_digits() to increase the limit",
            value.len()
        ));
    }
    let significant = value.trim_start_matches('0');
    if significant.is_empty() {
        return Ok(Some(0));
    }
    significant
        .parse::<u16>()
        .map(Some)
        .map_err(|_| "Port out of range 0-65535".into())
}
pub(crate) fn domain(value: &str) -> Result<String, String> {
    let candidate = python_trim(value);
    let host;
    let candidate = if candidate.contains("://") {
        let url = split_url(candidate)?;
        if !["http", "https"].contains(&url.scheme.as_str())
            || url.host.is_empty()
            || url.credentials
            || port(&url.port)?.is_some()
            || !["", "/"].contains(&url.path.as_str())
            || !url.query.is_empty()
            || !url.fragment.is_empty()
        {
            return Err(format!(
                "domain URL must contain only an HTTP(S) hostname: {value}"
            ));
        }
        host = url.host;
        host.as_str()
    } else {
        candidate
    };
    let normalized =
        idna(candidate.trim_end_matches('.')).map_err(|_| format!("invalid domain: {value}"))?;
    let plain = normalized.strip_prefix("*.").unwrap_or(&normalized);
    if plain.is_empty()
        || plain.len() > 253
        || plain.split('.').any(|label| {
            label.is_empty()
                || label.len() > 63
                || !label
                    .as_bytes()
                    .first()
                    .is_some_and(u8::is_ascii_alphanumeric)
                || !label
                    .as_bytes()
                    .last()
                    .is_some_and(u8::is_ascii_alphanumeric)
                || !label
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-')
        })
    {
        return Err(format!("invalid domain: {value}"));
    }
    Ok(normalized)
}
pub(crate) fn url(value: &str) -> Result<String, String> {
    let url = split_url(value).map_err(|_| format!("invalid scoped URL: {value}"))?;
    let parsed_port = port(&url.port).map_err(|_| format!("invalid scoped URL: {value}"))?;
    if !["http", "https"].contains(&url.scheme.as_str()) {
        return Err("scoped URLs must use http or https".into());
    }
    if url.host.is_empty() {
        return Err("scoped URLs require a hostname".into());
    }
    if url.credentials {
        return Err("scoped URLs cannot contain credentials".into());
    }
    if !url.fragment.is_empty() {
        return Err("scoped URLs cannot contain fragments".into());
    }
    if value.chars().any(|c| (c as u32) < 32) {
        return Err("scoped URLs cannot contain control characters".into());
    }
    let host = idna(&url.host).map_err(|_| format!("invalid scoped URL hostname: {value}"))?;
    let host = if host.contains(':') {
        format!("[{host}]")
    } else {
        host
    };
    let port = parsed_port.map_or(String::new(), |p| format!(":{p}"));
    let path = if url.path.is_empty() { "/" } else { &url.path };
    let query = if url.query.is_empty() {
        String::new()
    } else {
        format!("?{}", url.query)
    };
    Ok(format!("{}://{host}{port}{path}{query}", url.scheme))
}
pub(crate) fn cidr(value: &str) -> Result<String, String> {
    cidr_inner(value).ok_or_else(|| {
        format!(
            "{} does not appear to be an IPv4 or IPv6 network",
            repr(value)
        )
    })
}
fn prefix_integer(value: &str) -> Option<u32> {
    // ipaddress applies an ASCII-digit gate before int(); generic Python int
    // accepts Unicode decimal digits, but the network-prefix contract does not.
    if value.is_empty() || value.len() > 4300 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    value.bytes().try_fold(0u32, |number, digit| {
        number.checked_mul(10)?.checked_add(u32::from(digit - b'0'))
    })
}

fn cidr_inner(value: &str) -> Option<String> {
    let (address, prefix) = value
        .split_once('/')
        .map_or((value, None), |(a, p)| (a, Some(p)));
    if let Ok(ip) = address.parse::<Ipv4Addr>() {
        let prefix = match prefix {
            None => 32,
            Some(p) => {
                if let Some(prefix) = prefix_integer(p) {
                    prefix
                } else {
                    let mask = u32::from(p.parse::<Ipv4Addr>().ok()?);
                    if mask.leading_ones() + mask.trailing_zeros() == 32 {
                        mask.leading_ones()
                    } else if mask.leading_zeros() + mask.trailing_ones() == 32 {
                        mask.leading_zeros()
                    } else {
                        return None;
                    }
                }
            }
        };
        if prefix > 32 {
            return None;
        }
        let mask = if prefix == 0 {
            0
        } else {
            u32::MAX << (32 - prefix)
        };
        return Some(format!(
            "{}/{}",
            Ipv4Addr::from(u32::from(ip) & mask),
            prefix
        ));
    }
    let (address, scope) = address
        .split_once('%')
        .map_or((address, None), |(a, s)| (a, Some(s)));
    if scope.is_some_and(|s| s.is_empty() || s.contains('%')) {
        return None;
    }
    let ip = address.parse::<Ipv6Addr>().ok()?;
    let prefix = match prefix {
        None => 128,
        Some(p) => prefix_integer(p)?,
    };
    if prefix > 128 {
        return None;
    }
    let mask = if prefix == 0 {
        0
    } else {
        u128::MAX << (128 - prefix)
    };
    let original = u128::from(ip);
    let net = original & mask;
    let scope = if original == net {
        scope.map_or(String::new(), |s| format!("%{s}"))
    } else {
        String::new()
    };
    Some(format!("{}{scope}/{prefix}", Ipv6Addr::from(net)))
}
pub(crate) fn sorted_strings(values: impl Iterator<Item = String>) -> Vec<String> {
    values.collect::<BTreeSet<_>>().into_iter().collect()
}
