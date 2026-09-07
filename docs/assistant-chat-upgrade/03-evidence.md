# Phase 3 validation

One URL-owned Context/Results drawer now preserves the chat/draft and becomes a modal sheet on mobile. Device document uploads reuse ingestion; project files use the host workspace API; knowledge selections are previewed before attaching. Results reference canonical code, citations, tool artifacts and retained file diffs. Raw downloads retain acknowledgement and bounded tool reads remain redacted.

Production build passed; results/source provenance tests passed (5 workspace tests). Chat API regression set passed (14 tests with workspace subset before final additions).
Real-Core production LAN matrix: 8 passed after correcting a discovered mobile bug. The legacy responsive inspector rule hid the new sheet; portaling the mobile dialog outside that layout fixed visibility and stacking. Tests cover actual document ingestion/preview, selected context, drawer navigation preserving draft, bookmarks and branching on desktop 1440/1024 and Chromium/WebKit 320/390/430.
Artifacts remain canonical; projections add no duplicated persisted output. No destructive migration. Physical-device and actual vendor-native runtime gates remain pending final acceptance.
