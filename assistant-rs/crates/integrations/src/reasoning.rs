use crate::contracts::pyspace;
use crate::{ProviderError, Result};
const OPEN: [&str; 2] = ["<think>", "<thinking>"];
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum State {
    Opening,
    Thinking,
    Template,
    AnswerStart,
    Answer,
}
pub struct ReplySplitter {
    state: State,
    template: bool,
    held: String,
    close: &'static str,
    scanned: usize,
    limit: usize,
}
pub(crate) fn template_opened(model: &str) -> bool {
    let s = model.to_lowercase();
    s.contains("deepseek")
        || s.contains("glm")
        || s.contains("qwq")
        || s.find("qwen").is_some_and(|i| s[i..].contains("think"))
}
pub fn split_reply(text: &str, template: bool) -> (String, String) {
    let body = text.trim_start_matches(pyspace);
    for open in OPEN {
        if let Some(thought) = body.strip_prefix(open) {
            let close = if open == "<think>" {
                "</think>"
            } else {
                "</thinking>"
            };
            return match thought.find(close) {
                Some(i) => (thought[..i].into(), thought[i + close.len()..].into()),
                None => (thought.into(), String::new()),
            };
        }
    }
    if template
        && let Some(i) = text.find("</think>")
        && !OPEN.iter().any(|tag| text[..i].contains(tag))
    {
        return (text[..i].into(), text[i + 8..].into());
    }
    (String::new(), text.into())
}
impl ReplySplitter {
    pub fn new(model: &str, limit: usize) -> Self {
        Self {
            state: State::Opening,
            template: template_opened(model),
            held: String::new(),
            close: "",
            scanned: 0,
            limit,
        }
    }
    pub fn retained_bytes(&self) -> usize {
        self.held.len()
    }
    pub fn push(&mut self, text: &str) -> Result<Vec<(bool, String)>> {
        if text.len() > self.limit.saturating_sub(self.held.len()) {
            return Err(ProviderError::response(
                "Provider reasoning buffer exceeded its byte limit",
            ));
        }
        self.held.push_str(text);
        self.drain()
    }
    pub fn route_reasoning(&mut self) -> Result<Vec<(bool, String)>> {
        self.template = false;
        if self.state != State::Template {
            return Ok(vec![]);
        }
        self.state = State::Answer;
        self.drain()
    }
    pub fn finish(&mut self) -> Vec<(bool, String)> {
        let mut text = std::mem::take(&mut self.held);
        if self.state == State::AnswerStart {
            text = text.trim_start_matches(pyspace).into()
        }
        if text.is_empty() {
            vec![]
        } else {
            vec![(self.state == State::Thinking, text)]
        }
    }
    fn drain(&mut self) -> Result<Vec<(bool, String)>> {
        let mut pieces = vec![];
        loop {
            match self.state {
                State::Opening => {
                    let body = self.held.trim_start_matches(pyspace);
                    if let Some(open) = OPEN.iter().find(|o| body.starts_with(**o)) {
                        self.close = if *open == "<think>" {
                            "</think>"
                        } else {
                            "</thinking>"
                        };
                        self.held = body[open.len()..].into();
                        self.state = State::Thinking;
                    } else if body.is_empty() || OPEN.iter().any(|o| o.starts_with(body)) {
                        return Ok(pieces);
                    } else {
                        self.state = if self.template {
                            State::Template
                        } else {
                            State::Answer
                        };
                    }
                }
                State::Thinking => {
                    if let Some(end) = self.held.find(self.close) {
                        if end > 0 {
                            pieces.push((true, self.held[..end].into()))
                        }
                        self.held = self.held[end + self.close.len()..].into();
                        self.state = State::AnswerStart;
                    } else {
                        let suffix = (1..self.close.len())
                            .rev()
                            .find(|&i| self.held.ends_with(&self.close[..i]))
                            .unwrap_or(0);
                        let keep = self.held.len() - suffix;
                        if keep > 0 {
                            pieces.push((true, self.held[..keep].into()));
                            self.held = self.held[keep..].into();
                        }
                        return Ok(pieces);
                    }
                }
                State::Template => {
                    let mut start = self.scanned.saturating_sub(9);
                    while !self.held.is_char_boundary(start) {
                        start -= 1;
                    }
                    let end = self.held[start..].find("</think>").map(|i| i + start);
                    let limit = end.unwrap_or(self.held.len());
                    if OPEN.iter().any(|o| self.held[start..limit].contains(o)) {
                        self.state = State::Answer;
                    } else if let Some(end) = end {
                        if end > 0 {
                            pieces.push((true, self.held[..end].into()));
                        }
                        self.held = self.held[end + 8..].into();
                        self.state = State::AnswerStart;
                    } else {
                        self.scanned = self.held.len();
                        return Ok(pieces);
                    }
                }
                State::AnswerStart => {
                    self.held = self.held.trim_start_matches(pyspace).into();
                    if self.held.is_empty() {
                        return Ok(pieces);
                    }
                    self.state = State::Answer;
                }
                State::Answer => {
                    if !self.held.is_empty() {
                        pieces.push((false, std::mem::take(&mut self.held)));
                    }
                    return Ok(pieces);
                }
            }
        }
    }
}
/// Conservative control-frame detection for the no-tools execution boundary.
/// This function never executes or authorizes a parsed call.
pub fn control_markup(text: &str) -> bool {
    if [
        "<|DSML|",
        "<｜DSML｜",
        "</|DSML|",
        "</｜DSML｜",
        "<\\/|DSML|",
        "<\\/｜DSML｜",
        "<｜tool▁",
        "<|tool▁",
    ]
    .iter()
    .any(|tag| text.contains(tag))
    {
        return true;
    }
    // Forward-only marker cursors keep repeated unmatched openings linear.
    let mut closes = text.match_indices("</tool_call>").peekable();
    let mut keys = text.match_indices("<arg_key>").peekable();
    let mut values = text.match_indices("<arg_value>").peekable();
    let mut tail = text;
    while let Some(i) = tail.find("<tool_call>") {
        tail = &tail[i + 11..];
        let body = tail.trim_start_matches(pyspace);
        let offset = text.len() - tail.len();
        while closes.peek().is_some_and(|(pos, _)| *pos < offset) {
            closes.next();
        }
        while keys.peek().is_some_and(|(pos, _)| *pos < offset) {
            keys.next();
        }
        while values.peek().is_some_and(|(pos, _)| *pos < offset) {
            values.next();
        }
        let close = closes.peek().map_or(text.len(), |(pos, _)| *pos);
        if keys.peek().is_some_and(|(pos, _)| *pos < close)
            || values.peek().is_some_and(|(pos, _)| *pos < close)
        {
            return true;
        }
        if let Some(rest) = body.strip_prefix('{') {
            let rest = rest.trim_start_matches(pyspace);
            if ["\"name\"", "\"arguments\""].iter().any(|key| {
                rest.strip_prefix(key)
                    .is_some_and(|s| s.trim_start_matches(pyspace).starts_with(':'))
            }) {
                return true;
            }
        }
        if body
            .as_bytes()
            .first()
            .is_some_and(|b| b.is_ascii_alphabetic() || *b == b'_')
        {
            let end = body
                .find(|c: char| !(c.is_alphanumeric() || "_.-".contains(c)))
                .unwrap_or(body.len());
            let rest = &body[end..];
            if rest.trim_start_matches([' ', '\t']).starts_with('\n')
                || rest.trim_start_matches(pyspace).starts_with("</tool_call>")
            {
                return true;
            }
        }
    }
    false
}

impl std::fmt::Debug for ReplySplitter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("ReplySplitter")
            .field("state", &self.state)
            .finish_non_exhaustive()
    }
}
