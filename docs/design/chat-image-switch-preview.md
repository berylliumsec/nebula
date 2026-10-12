# Image preview for warm conversation switches

Entry: open a conversation, switch to another, then return through Conversations.

| Journey | Observable invariant | State authority | Test layer |
| --- | --- | --- | --- |
| Discover/select | The URL, title, and preview identify the selected conversation; a prepared image appears while history refreshes | URL, transient image cache, Core | focused browser |
| Create/use/stream | Only settled durable messages enter an image; pending decisions and current work remain live | Core, existing chat state | browser and real Core |
| Copy/edit | Activating the image immediately reveals the cached text and its existing controls; no OCR is required | transient text preview, then Core | browser keyboard and touch |
| Refresh/reconnect | Core history atomically replaces the image; the reading position remains near the same message | Core, transient scroll anchor | browser and real Core |
| Failure/retry | A failed refresh leaves the preview readable, offers the existing retry, and does not authorize actions | Core error contract | browser |
| Delete/revoke | Deletion and denied history invalidate both image and text previews | Core | cache unit and browser |
| Mobile/theme | Images are sized to the chat viewport and current theme, with no horizontal clipping | viewport and theme | browser matrix |

Image tiles are presentation-only, stored in bounded device memory. The existing text cache remains the immediate fallback when an image has not been prepared. Core's SQLite-backed messages remain authoritative, and URL identity is unchanged. Rendering happens after a settled read, during idle time, and never blocks switching. The experiment captures the durable messages already loaded in the conversation; older pages still use the existing Load earlier messages flow. The compact canvas rendering is a readable preview of text and attachment labels, not a pixel-perfect rendering of Markdown or interactive work cards.

Selected test layers: renderer/cache unit tests; the focused conversation-switch browser journey in the permanent desktop Chromium and mobile Chromium/WebKit profiles; a real-Core production journey; and a non-loopback LAN check. No provider or harness execution is required to verify the preview. Physical devices are outside the available test environment.
