use crate::{ProviderError, Result};
/// CR/LF parser; no Unicode splitlines and no allocation proportional to the body.
pub struct SseDecoder {
    line: Vec<u8>,
    data: Vec<String>,
    data_bytes: usize,
    event: String,
    cr: bool,
    limit: usize,
    max_frames: usize,
}
impl SseDecoder {
    pub fn new(limit: usize, max_frames: usize) -> Result<Self> {
        if limit == 0 || limit > 16 * 1024 * 1024 || max_frames == 0 || max_frames > 10000 {
            return Err(ProviderError::capacity());
        }
        Ok(Self {
            line: vec![],
            data: vec![],
            data_bytes: 0,
            event: String::new(),
            cr: false,
            limit,
            max_frames,
        })
    }
    pub fn retained_bytes(&self) -> usize {
        self.line.len() + self.data_bytes + self.event.len()
    }
    pub fn push(&mut self, chunk: &[u8]) -> Result<Vec<String>> {
        self.push_inner(chunk, false)
    }
    /// Chat Completions ends at [DONE], even if the same read contains trailing bytes.
    /// Generic SSE framing above deliberately has no application terminal semantics.
    pub fn push_reply(&mut self, chunk: &[u8]) -> Result<Vec<String>> {
        self.push_inner(chunk, true)
    }
    fn push_inner(&mut self, chunk: &[u8], stop_done: bool) -> Result<Vec<String>> {
        let mut frames = vec![];
        let mut bytes = 0;
        for &b in chunk {
            if self.cr {
                self.cr = false;
                if b == b'\n' {
                    continue;
                }
            }
            if b == b'\r' || b == b'\n' {
                self.cr = b == b'\r';
                let before = frames.len();
                self.take_line(&mut frames)?;
                let done = stop_done
                    .then(|| {
                        frames[before..]
                            .iter()
                            .position(|s| s.trim_matches(crate::contracts::pyspace) == "[DONE]")
                    })
                    .flatten();
                if let Some(index) = done {
                    frames.truncate(before + index + 1);
                }
                bytes += frames[before..].iter().map(String::len).sum::<usize>();
                if done.is_some() {
                    if frames.len() > self.max_frames || bytes > self.limit {
                        return Err(ProviderError::response(
                            "Provider SSE chunk exceeded its frame budget",
                        ));
                    }
                    break;
                }
            } else {
                if self.retained_bytes() >= self.limit {
                    return Err(ProviderError::response(
                        "Provider SSE frame exceeded its byte limit",
                    ));
                }
                self.line.push(b)
            }
            if frames.len() > self.max_frames || bytes > self.limit {
                return Err(ProviderError::response(
                    "Provider SSE chunk exceeded its frame budget",
                ));
            }
        }
        Ok(frames)
    }
    pub fn finish(&mut self) -> Result<Vec<String>> {
        let mut out = vec![];
        if !self.line.is_empty() {
            self.take_line(&mut out)?;
        }
        self.dispatch(&mut out)?;
        Ok(out)
    }
    fn take_line(&mut self, out: &mut Vec<String>) -> Result<()> {
        let raw = std::mem::take(&mut self.line);
        let line = String::from_utf8_lossy(&raw);
        if line.len() + self.data_bytes + self.event.len() > self.limit {
            return Err(ProviderError::capacity());
        }
        if line.is_empty() {
            self.dispatch(out)?
        } else if let Some(data) = line.strip_prefix("data:") {
            let data = data.strip_prefix(' ').unwrap_or(data);
            if self.data.len() >= 10000 {
                return Err(ProviderError::capacity());
            }
            self.data_bytes += data.len() + 1;
            self.data.push(data.into());
        } else if let Some(event) = line.strip_prefix("event:") {
            self.event = event.trim().into();
        }
        Ok(())
    }
    fn dispatch(&mut self, out: &mut Vec<String>) -> Result<()> {
        let event = std::mem::take(&mut self.event);
        if self.data.is_empty() {
            return Ok(());
        }
        let lines = std::mem::take(&mut self.data);
        self.data_bytes = 0;
        let payload = lines.join("\n");
        if event == "error" {
            let data = serde_json::from_str::<serde_json::Value>(payload.trim()).ok();
            let value = match data {
                Some(v) if v.is_object() => {
                    if v.get("error").is_some_and(crate::contracts::truthy) {
                        v
                    } else {
                        serde_json::json!({"error":v})
                    }
                }
                _ => {
                    serde_json::json!({"error":{"message":if payload.trim().is_empty(){"provider sent an error event"}else{payload.trim()}}})
                }
            };
            let bytes = crate::contracts::bounded_json(&value, self.limit)?;
            out.push(String::from_utf8(bytes).expect("JSON UTF-8"));
        } else if lines.len() > 1 && serde_json::from_str::<serde_json::Value>(&payload).is_err() {
            let pieces: Vec<_> = lines.iter().filter(|s| !s.trim().is_empty()).collect();
            if pieces.iter().all(|s| {
                s.trim() == "[DONE]" || serde_json::from_str::<serde_json::Value>(s).is_ok()
            }) {
                out.extend(pieces.into_iter().cloned());
            } else {
                out.push(payload)
            }
        } else {
            out.push(payload)
        }
        if out.len() > self.max_frames {
            return Err(ProviderError::capacity());
        }
        Ok(())
    }
}

impl std::fmt::Debug for SseDecoder {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SseDecoder")
            .field("retained_bytes", &self.retained_bytes())
            .finish_non_exhaustive()
    }
}
