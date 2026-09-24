//! Bounded provider protocol adapters. Tool-shaped output is inert data only.
mod contracts;
mod openai;
mod reasoning;
mod sse;
mod transport;

pub use contracts::*;
pub use openai::{OpenAiAccumulator, decode_completion, openai_payload};
pub use reasoning::{ReplySplitter, control_markup, split_reply};
pub use sse::SseDecoder;
pub use transport::{
    Cancellation, OpenAiProvider, PoolLimits, ProviderPool, ProviderStream, ReceivedEvent,
};
