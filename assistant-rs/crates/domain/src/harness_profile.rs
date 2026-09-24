//! Immutable historical HarnessProfile validation. No credential resolution,
//! endpoint connection, path access or process launch is performed here.
use crate::records::RecordError;
use serde_json::Value;
use std::net::Ipv6Addr;

type Result<T> = std::result::Result<T, RecordError>;
fn require(ok: bool) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(RecordError::Invariant("harness profile is incoherent"))
    }
}
fn nonempty(value: &Value) -> bool {
    value.as_str().is_some_and(|s| !s.is_empty())
}
fn secret(value: &str) -> bool {
    if let Some(name) = value.strip_prefix("env:") {
        let mut chars = name.bytes();
        return chars
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic() || c == b'_')
            && chars.all(|c| c.is_ascii_alphanumeric() || c == b'_');
    }
    value
        .strip_prefix("vault:")
        .or_else(|| value.strip_prefix("session:"))
        .is_some_and(|id| {
            id.len() == 32
                && id
                    .bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
}

// Python urlsplit does not normalize paths, decode escapes, enforce port ranges,
// or require a Unix authority to be empty. Preserve those retained validators.
// This parser is intentionally private and cannot authorize network access.
fn endpoint_valid(endpoint: &str, unix: bool) -> bool {
    if unix && !endpoint.starts_with("unix://") {
        return false;
    }
    let normalized = endpoint
        .trim_start_matches(|c| c <= '\u{20}')
        .replace(['\t', '\r', '\n'], "");
    let Some((scheme, rest)) = normalized.split_once(':') else {
        return false;
    };
    if !scheme.eq_ignore_ascii_case(if unix { "unix" } else { "ws" }) {
        return false;
    }
    let Some(rest) = rest.strip_prefix("//") else {
        return false;
    };
    let split = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..split];
    let tail = &rest[split..];
    let (tail, fragment) = tail.split_once('#').unwrap_or((tail, ""));
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    if !query.is_empty() || !fragment.is_empty() {
        return false;
    }
    // Complete Unicode15 NFKC delimiter set, generated using CPython3.12:
    // cp>=128 where normalize('NFKC', chr(cp)) contains one of /?#@:.
    // urlsplit rejects such an authority before hostname access.
    if authority.chars().any(|c| {
        matches!(
            c as u32,
            0x2047
                | 0x2048
                | 0x2049
                | 0x2100
                | 0x2101
                | 0x2105
                | 0x2106
                | 0x2a74
                | 0xfe13
                | 0xfe16
                | 0xfe55
                | 0xfe56
                | 0xfe5f
                | 0xfe6b
                | 0xff03
                | 0xff0f
                | 0xff1a
                | 0xff1f
                | 0xff20
        )
    }) {
        return false;
    }
    if authority.contains('[') != authority.contains(']') {
        return false;
    }
    let host_port = authority
        .rsplit_once('@')
        .map_or(authority, |(_, host)| host);
    if let Some((_, bracketed)) = authority.split_once('[') {
        let Some((address, _)) = bracketed.split_once(']') else {
            return false;
        };
        let valid_address = if let Some(future) = address.strip_prefix('v') {
            future.split_once('.').is_some_and(|(version, host)| {
                !version.is_empty()
                    && version.bytes().all(|c| c.is_ascii_hexdigit())
                    && !host.is_empty()
            })
        } else {
            let (address, scope) = address
                .split_once('%')
                .map_or((address, None), |(a, s)| (a, Some(s)));
            scope.is_none_or(|s| !s.is_empty() && !s.contains('%'))
                && address.parse::<Ipv6Addr>().is_ok()
        };
        if !valid_address {
            return false;
        }
    }
    if unix {
        return path.starts_with('/');
    }
    if let Some((userinfo, _)) = authority.rsplit_once('@') {
        let (username, password) = userinfo.split_once(':').unwrap_or((userinfo, ""));
        if !username.is_empty() || !password.is_empty() {
            return false;
        }
    }
    let hostname = if let Some((_, rest)) = host_port.split_once('[') {
        rest.split_once(']').map_or("", |(host, _)| host)
    } else {
        host_port
            .split_once(':')
            .map_or(host_port, |(host, _)| host)
    };
    hostname.eq_ignore_ascii_case("localhost") || hostname == "127.0.0.1" || hostname == "::1"
}

pub(crate) fn validate(p: &mut Value) -> Result<()> {
    if let Some(executable) = p["executable"].as_str() {
        require(executable.starts_with('/'))?;
    }
    if let Some(home) = p["home_directory"].as_str() {
        require(
            home.starts_with('/')
                && !home.contains(['\0', '\r', '\n'])
                && !home.split('/').any(|part| part == ".."),
        )?;
        let normalized = home.trim_end_matches('/');
        p["home_directory"] = if normalized.is_empty() {
            "/"
        } else {
            normalized
        }
        .into();
    }
    if let Some(value) = p["secret_ref"].as_str() {
        require(secret(value))?;
    }
    require(
        p["privacy"]["auto_share_tool_results"] != true
            || p["privacy"]["permits_sensitive_data"] == true,
    )?;
    for model in p["capabilities"]["model_options"]
        .as_array()
        .ok_or(RecordError::Invariant("harness model options are missing"))?
    {
        for (default, options) in [
            ("default_reasoning_effort", "reasoning_efforts"),
            ("default_service_tier", "service_tiers"),
        ] {
            if nonempty(&model[default]) {
                require(model[options].as_array().is_some_and(|options| {
                    options.iter().any(|option| option["id"] == model[default])
                }))?;
            }
        }
    }
    let spawn = p["connection_mode"] == "spawn";
    let codex = p["kind"] == "codex_app_server";
    let grok = p["kind"] == "grok_acp";
    let claude = p["kind"] == "claude_agent_sdk";
    let existing = p["auth_mode"] == "existing_session";
    if nonempty(&p["home_directory"]) {
        require(spawn && (codex || grok))?;
    }
    if grok {
        require(spawn && nonempty(&p["executable"]) && existing)?;
    }
    let native = &p["native_capabilities"];
    if claude {
        require(
            native["browser"] != true
                && native["computer_use"] != true
                && native["image_generation"] != true,
        )?;
        require(native["shell"] != true || p["auth_mode"] != "secret_ref")?;
    }
    require(!codex || native["web_fetch"] != true)?;
    require(existing != nonempty(&p["secret_ref"]))?;
    if spawn {
        require(
            p["endpoint"].is_null()
                && p["transport"] == "stdio"
                && p["auth_mode"] != "endpoint_bearer",
        )?;
        if codex || grok {
            require(nonempty(&p["executable"]))?;
        }
    } else {
        require(codex && p["executable"].is_null() && nonempty(&p["endpoint"]))?;
        require(
            matches!(p["transport"].as_str(), Some("unix" | "websocket"))
                && p["auth_mode"] != "secret_ref",
        )?;
        require(endpoint_valid(
            p["endpoint"].as_str().unwrap_or_default(),
            p["transport"] == "unix",
        ))?;
    }
    Ok(())
}
