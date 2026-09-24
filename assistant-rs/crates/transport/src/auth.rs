use crate::ApiError;
use axum::http::{HeaderMap, Method};
use chrono::{DateTime, Utc};
use nebula_assistant_storage::entities::{Error as StorageError, SqliteAssistantStore};
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;

#[derive(Clone)]
pub struct Authentication {
    token: String,
    pub(crate) scheme: &'static str,
    allow_unauthenticated: bool,
}
impl Authentication {
    pub fn new(token: String, https: bool) -> Result<Self, &'static str> {
        if token.is_empty() || token.len() > 8192 {
            return Err("Core token must contain 1 to 8192 bytes");
        }
        Ok(Self {
            token,
            scheme: if https { "https" } else { "http" },
            allow_unauthenticated: false,
        })
    }
    /// Explicit caller opt-in corresponding to Core's local unauthenticated
    /// mode. Never selected automatically when credentials are missing/invalid.
    pub fn allow_unauthenticated(mut self, enabled: bool) -> Self {
        self.allow_unauthenticated = enabled;
        self
    }
    pub(crate) async fn authenticate(
        &self,
        store: &SqliteAssistantStore,
        headers: &HeaderMap,
        method: &Method,
        now: DateTime<Utc>,
    ) -> Result<Principal, ApiError> {
        if self.allow_unauthenticated {
            return Ok(Principal { device_id: None });
        }
        if let Some(value) = header(headers, "authorization")
            && let Some((scheme, token)) = value.split_once(' ')
            && scheme.eq_ignore_ascii_case("bearer")
            && secret_matches(token, &self.token)
        {
            return Ok(Principal { device_id: None });
        }
        let cookies = parse_cookies(header(headers, "cookie").as_deref().unwrap_or(""));
        let token = cookies
            .get("nebula_device")
            .filter(|s| !s.is_empty())
            .ok_or_else(ApiError::unauthorized)?;
        let digest = format!("{:x}", Sha256::digest(token.as_bytes()));
        for attempt in 0..2 {
            let device = store
                .paired_device(&digest)
                .await
                .map_err(ApiError::storage)?
                .filter(|d| d.valid_at(now))
                .ok_or_else(ApiError::unauthorized)?;
            let host = header(headers, "host").unwrap_or_default();
            if !valid_host(&host) {
                return Err(ApiError::http(400, "invalid paired-device Host header"));
            }
            let expected_origin = format!("{}://{host}", self.scheme);
            let origin = header(headers, "origin");
            if let Some(origin) = origin.as_deref().filter(|s| !s.is_empty())
                && !secret_matches(origin, &expected_origin)
            {
                return Err(ApiError::http(
                    403,
                    "paired-device origin validation failed",
                ));
            }
            if !matches!(*method, Method::GET | Method::HEAD | Method::OPTIONS) {
                let csrf = header(headers, "x-nebula-csrf").unwrap_or_default();
                let cookie = cookies.get("nebula_csrf").map_or("", String::as_str);
                let hash = format!("{:x}", Sha256::digest(csrf.as_bytes()));
                if csrf.is_empty()
                    || cookie.is_empty()
                    || !secret_matches(&csrf, cookie)
                    || !secret_matches(&hash, device.csrf_sha256())
                {
                    return Err(ApiError::http(403, "paired-device CSRF validation failed"));
                }
                if origin.as_deref().is_none_or(str::is_empty) {
                    return Err(ApiError::http(
                        403,
                        "paired-device mutation requires Origin",
                    ));
                }
            }
            if device.refresh_due(now) {
                match store
                    .touch_device(device.id(), device.revision(), now)
                    .await
                {
                    Ok(true) => {}
                    Ok(false) => return Err(ApiError::unauthorized()),
                    Err(StorageError::Conflict) if attempt == 0 => continue,
                    Err(error) => return Err(ApiError::storage(error)),
                }
            }
            return Ok(Principal {
                device_id: Some(device.id().into()),
            });
        }
        Err(ApiError::http(
            409,
            "Device authentication changed; retry the request",
        ))
    }
}

#[derive(Clone)]
pub(crate) struct Principal {
    pub device_id: Option<String>,
}
fn secret_matches(candidate: &str, expected: &str) -> bool {
    // Hashes are fixed length, avoiding credential-length dependent comparison.
    let left = Sha256::digest(candidate.as_bytes());
    let right = Sha256::digest(expected.as_bytes());
    bool::from(left.as_slice().ct_eq(right.as_slice()))
}
pub(crate) fn header(headers: &HeaderMap, name: &str) -> Option<String> {
    // Match ASGI/Starlette's latin-1 header decoding, including malformed UTF-8.
    headers
        .get(name)
        .map(|v| v.as_bytes().iter().map(|b| char::from(*b)).collect())
}
fn valid_host(host: &str) -> bool {
    if host.is_empty()
        || host
            .chars()
            .any(|c| matches!(c, '/' | '?' | '#' | '@') || c.is_control() || c.is_whitespace())
    {
        return false;
    }
    if let Some(rest) = host.strip_prefix('[') {
        let Some((address, tail)) = rest.split_once(']') else {
            return false;
        };
        let address = address.split_once('%').map_or(address, |(ip, _)| ip);
        address.parse::<std::net::Ipv6Addr>().is_ok() && (tail.is_empty() || tail.starts_with(':'))
    } else {
        !host.contains(['[', ']'])
            && host.split(':').next().is_some_and(|h| !h.is_empty())
            && host.matches(':').count() <= 1
    }
}
fn parse_cookies(value: &str) -> std::collections::HashMap<String, String> {
    let mut result = std::collections::HashMap::new();
    for chunk in value.split(';') {
        let (key, value) = chunk.split_once('=').unwrap_or(("", chunk));
        let (key, value) = (key.trim(), value.trim());
        if !key.is_empty() || !value.is_empty() {
            result.insert(key.into(), unquote(value));
        }
    }
    result
}
fn unquote(value: &str) -> String {
    if value.len() < 2 || !value.starts_with('"') || !value.ends_with('"') {
        return value.into();
    }
    let chars: Vec<_> = value[1..value.len() - 1].chars().collect();
    let mut out = String::new();
    let mut index = 0;
    while index < chars.len() {
        if chars[index] == '\\' && index + 1 < chars.len() {
            if index + 3 < chars.len()
                && ('0'..='3').contains(&chars[index + 1])
                && ('0'..='7').contains(&chars[index + 2])
                && ('0'..='7').contains(&chars[index + 3])
            {
                let value = (chars[index + 1] as u8 - b'0') * 64
                    + (chars[index + 2] as u8 - b'0') * 8
                    + (chars[index + 3] as u8 - b'0');
                out.push(char::from(value));
                index += 4;
            } else {
                out.push(chars[index + 1]);
                index += 2;
            }
        } else {
            out.push(chars[index]);
            index += 1;
        }
    }
    out
}
