# Phase 2 validation

Added transcript search with project/session boundaries and pagination; revisioned bookmarks; edit-before-message forks; conservative title generation; readable approvals/free-text questions; session labels.

Backend: `PYTHONPATH=src /home/agent/nebula/.venv/bin/python -m pytest tests/v3/test_chat_workspace.py tests/v3/test_chat.py tests/v3/test_chat_api.py -q` — 32 passed. The old rename test assumed a greeting triggered a naming revision; changed it to use the actual returned revision while retaining stale-write coverage.
Production build passed. Existing foundation browser matrix: 8 passed.
Permanent `assistant-real-*` projects exercise real Core on an isolated dynamic LAN origin with a local HTTP model fixture: 8 passed (desktop 1440/1024 and Chromium/WebKit 320/390/430). Journey verifies bookmarks survive relaunch, search returns the exact message, edit-first-message creates an empty branch with an editable draft, and parent navigation is visible.
Physical device and vendor-native lifecycle gates remain pending final acceptance. No live service changes.
Storage compatibility: bookmarks use existing revisioned entity storage; no table alteration or destructive backfill. Old clients remain compatible. Conversation deletion removes owned bookmarks.
