# Conversation activity markers

The conversation list communicates lifecycle state without adding status words to every row.

- **Working:** a blue dot pulses on a calm two-second ease-in-out cycle.
- **Waiting for you:** a static filled diamond marks an approval or interaction that needs operator action.
- **Idle:** a static hollow circle marks a conversation with no active turn.

Core is the authority for these states. The UI loads the engagement's saved-session activity when the conversation list opens, refreshes it after lifecycle events, on window focus, and every five seconds while the list remains visible. Temporary Ask Nebula sessions are excluded.

The shapes remain distinguishable without color. Every marker exposes an accessible name and tooltip. Under `prefers-reduced-motion: reduce`, the working marker is a static filled blue dot.
