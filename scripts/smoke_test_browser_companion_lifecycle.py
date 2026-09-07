"""Visible cancellation, retry, takeover, and reconnect against the real Core."""

from __future__ import annotations

from contextlib import suppress
import json
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect


async def exercise_lifecycle(
    origin: str,
    pairing: dict[str, str],
    conversation_id: str,
    evidence_root: Path,
    target_url: str,
    restart_browser: Callable[[], Awaitable[None]],
) -> dict[str, object]:
    evidence_root = evidence_root / "lifecycle"
    evidence_root.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
        )
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        page.set_default_timeout(20000)
        try:
            await page.goto(
                f"{origin}/#pair={pairing['secret']}&code={pairing['confirmation_code']}"
            )
            await page.get_by_label("Device name").fill("Browser lifecycle validation")
            await page.get_by_role("button", name="Pair device", exact=True).click()
            await expect(
                page.get_by_role("tab", name="Project browser", exact=True)
            ).to_be_visible()
            url = f"{origin}/?view=browser&session={conversation_id}"
            await page.goto(url)
            panel = page.get_by_role(
                "complementary", name="Browser Assistant", exact=True
            )
            await expect(page.locator(".managed-browser-screen img")).to_be_visible()
            await page.get_by_label("Browser address").fill(target_url)
            await page.get_by_role("button", name="Go", exact=True).click()
            preview = page.get_by_role("region", name="Browser context preview")
            await expect(preview.locator("pre")).to_have_text("Ready")
            await preview.get_by_role("button", name="Discard", exact=True).click()

            async def resume() -> None:
                button = page.get_by_role(
                    "button", name="Resume assistant control", exact=True
                )
                if await button.is_visible():
                    await button.click()

            async def request_click() -> None:
                await resume()
                await panel.locator("#analyst-message").fill(
                    "Use browser.companion only. Capture fresh context, click Ready once, wait for my inline approval, and report the resulting label."
                )
                await panel.get_by_role(
                    "button", name="Send message", exact=True
                ).click()
                consent = page.get_by_role("button", name="Allow this turn", exact=True)
                if await consent.is_visible():
                    await consent.click()
                await expect(
                    panel.get_by_role("button", name="Stop response", exact=True)
                ).to_be_visible(timeout=30000)
                await expect(
                    page.get_by_role("region", name="Browser action approval")
                ).to_be_visible(timeout=120000)

            print(
                "Lifecycle: stop a turn while its page action awaits approval",
                flush=True,
            )
            await request_click()
            await panel.get_by_role("button", name="Stop response", exact=True).click()
            await expect(
                panel.get_by_role("button", name="Stop response", exact=True)
            ).to_be_hidden(timeout=30000)
            await expect(
                page.get_by_role("region", name="Browser action approval")
            ).to_be_hidden(timeout=15000)
            await page.get_by_role("button", name="Ask about page", exact=True).click()
            await expect(preview.locator("pre")).to_have_text("Ready")
            await preview.get_by_role("button", name="Discard", exact=True).click()

            print(
                "Lifecycle: explicitly retry the cancelled turn with a fresh approval",
                flush=True,
            )
            await resume()
            await panel.get_by_role(
                "button", name="Retry as linked turn", exact=True
            ).last.click()
            await page.get_by_role("button", name="Start retry", exact=True).click()
            approval = page.get_by_role("region", name="Browser action approval")
            await expect(approval).to_be_visible(timeout=120000)
            await approval.get_by_role(
                "button", name="Approve action", exact=True
            ).click()
            await expect(
                panel.get_by_role("button", name="Stop response", exact=True)
            ).to_be_hidden(timeout=180000)
            await expect(panel.locator(".chat-message.assistant").last).to_contain_text(
                "Saved", timeout=30000
            )

            # Re-navigation is an explicit operator action and resets the fixture.
            await page.get_by_label("Browser address").fill(target_url)
            await page.get_by_role("button", name="Go", exact=True).click()
            await expect(preview.locator("pre")).to_have_text("Ready")
            await preview.get_by_role("button", name="Discard", exact=True).click()
            original_tab = await page.get_by_label(
                "Browser tab", exact=True
            ).input_value()
            print(
                "Lifecycle: manual tab creation takes over a queued assistant action",
                flush=True,
            )
            await request_click()
            await page.get_by_role("button", name="New tab", exact=True).click()
            await expect(approval).to_be_hidden(timeout=15000)
            await expect(
                page.get_by_label("Browser tab", exact=True)
            ).not_to_have_value(original_tab)
            await expect(
                panel.get_by_role("button", name="Stop response", exact=True)
            ).to_be_hidden(timeout=180000)
            assert parse_qs(urlsplit(page.url).query)["session"] == [conversation_id]
            await page.get_by_label("Browser tab", exact=True).select_option(
                original_tab
            )
            await page.get_by_role("button", name="Ask about page", exact=True).click()
            await expect(preview.locator("pre")).to_have_text("Ready")
            await preview.get_by_role("button", name="Discard", exact=True).click()

            print(
                "Lifecycle: a second viewer detaches without losing the browser or conversation",
                flush=True,
            )
            second = await context.new_page()
            await second.goto(url)
            await expect(second.locator(".managed-browser-screen img")).to_be_visible()
            await expect(second.get_by_label("Browser tab", exact=True)).to_have_value(
                original_tab
            )
            await second.close()
            await page.get_by_role("button", name="Reconnect view", exact=True).click()
            await expect(page.locator(".managed-browser-screen img")).to_be_visible()
            assert parse_qs(urlsplit(page.url).query)["session"] == [conversation_id]

            print(
                "Lifecycle: restart lost Chromium tabs while retaining saved history",
                flush=True,
            )
            await restart_browser()
            await expect(
                page.get_by_role("status").filter(has_text="Browser view disconnected")
            ).to_be_visible(timeout=20000)
            await page.get_by_role("button", name="Reconnect view", exact=True).click()
            await expect(
                page.get_by_role("alert").filter(
                    has_text="previous live tabs were lost"
                )
            ).to_be_visible(timeout=30000)
            await expect(page.locator(".managed-browser-screen img")).to_be_visible()
            assert parse_qs(urlsplit(page.url).query)["session"] == [conversation_id]
            await expect(
                page.get_by_role("button", name="Resume assistant control", exact=True)
            ).to_be_visible()
            await page.screenshot(
                path=str(evidence_root / "result.png"), full_page=True
            )
            result = {
                "ui_cancel_revokes_pending_action": True,
                "ui_explicit_linked_retry": True,
                "ui_tab_takeover_preserves_target": True,
                "ui_concurrent_viewer_reconnect": True,
                "ui_chromium_restart_preserves_conversation": True,
                "origin": origin,
                "browser": browser.version,
                "viewport": "1440x900",
            }
            (evidence_root / "result.json").write_text(json.dumps(result, indent=2))
            return result
        except Exception:
            with suppress(Exception):
                await page.screenshot(
                    path=str(evidence_root / "failure.png"), full_page=True
                )
            raise
        finally:
            await context.tracing.stop(path=str(evidence_root / "trace.zip"))
            await browser.close()
