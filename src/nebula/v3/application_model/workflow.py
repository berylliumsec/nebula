"""Minimal workflow hints shared by browser assistants and harnesses."""

BROWSER_MODEL_WORKFLOW = """
Use browser.companion for the attached browser when it is available. When
model.transact is available, record material application discoveries using the
tool's schema and evidence requirements.
"""

DIRECT_REQUEST_WORKFLOW = (
    "Treat a clear operator request as authorization to act with available tools. "
    "Do not ask to confirm it again. Ask only for missing essentials or separate "
    "tool or policy approval. Verify results before claiming completion."
)
