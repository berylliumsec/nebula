"""Production UI journey against the isolated real Core and managed browser.

Called by smoke_test_browser_companion_core.py; no API routes are mocked.
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect


async def exercise_ui(
    origin: str,
    pairing: dict[str, str],
    conversation_id: str,
    evidence_root: Path,
) -> dict[str, object]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900}, reduced_motion="reduce"
        )
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        page.set_default_timeout(20000)
        try:
            print("UI journey: opening the browser workspace", flush=True)
            await page.goto(
                f"{origin}/#pair={pairing['secret']}&code={pairing['confirmation_code']}"
            )
            await page.get_by_label("Device name").fill("Browser UI validation")
            await page.get_by_role("button", name="Pair device", exact=True).click()
            await expect(
                page.get_by_role("tab", name="Project browser", exact=True)
            ).to_be_visible(timeout=20000)
            await page.goto(f"{origin}/?view=browser&session={conversation_id}")
            await expect(page.get_by_label("Browser engine")).to_have_value("managed")
            panel = page.get_by_role(
                "complementary", name="Browser Assistant", exact=True
            )
            await expect(panel).to_be_visible()
            await expect(page.locator(".managed-browser-screen img")).to_be_visible()
            await page.get_by_label("Browser address").fill(
                "http://browserd-smoke.example.test/"
            )
            await page.get_by_role("button", name="Go", exact=True).click()
            await page.get_by_role("button", name="Ask about page", exact=True).click()
            preview = page.get_by_role("region", name="Browser context preview")
            await expect(preview).to_contain_text("Ready")
            await preview.get_by_role(
                "button", name="Attach to Assistant", exact=True
            ).click()
            await expect(
                panel.get_by_role("region", name="Selected context pack")
            ).to_be_visible()

            async def send(text: str) -> None:
                await panel.locator("#analyst-message").fill(text)
                await panel.get_by_role(
                    "button", name="Send message", exact=True
                ).click()
                # This is the real operator consent dialog, not an API bypass.
                consent = page.get_by_role("button", name="Allow this turn", exact=True)
                if await consent.is_visible():
                    await consent.click()
                await expect(
                    panel.get_by_role("button", name="Stop response", exact=True)
                ).to_be_visible(timeout=30000)

            print("UI journey: asking about attached page context", flush=True)
            await send(
                "Read the attached page context. What is the label on its only button? Reply only with that label and do not use tools."
            )
            await expect(
                panel.get_by_role("button", name="Stop response", exact=True)
            ).to_be_hidden(timeout=180000)
            await expect(panel.locator(".chat-message.assistant").last).to_contain_text(
                "Ready"
            )
            assert parse_qs(urlsplit(page.url).query)["view"] == ["browser"]
            assert parse_qs(urlsplit(page.url).query)["session"] == [conversation_id]
            resume = page.get_by_role(
                "button", name="Resume assistant control", exact=True
            )
            if await resume.is_visible():
                await resume.click()
            print("UI journey: requesting an approved browser action", flush=True)
            await send(
                "Using browser.companion only, capture fresh page context, click the Ready button once, wait for operator approval, and report its new label."
            )
            approval = page.get_by_role("region", name="Browser action approval")
            await expect(approval).to_be_visible(timeout=120000)
            await expect(approval).to_contain_text("click")
            await approval.get_by_role(
                "button", name="Approve action", exact=True
            ).click()
            await expect(
                panel.get_by_role("button", name="Stop response", exact=True)
            ).to_be_hidden(timeout=180000)
            await expect(panel.locator(".chat-message.assistant").last).to_contain_text(
                "Saved"
            )
            await page.get_by_role("button", name="Ask about page", exact=True).click()
            await expect(preview).to_contain_text("Saved")
            await preview.get_by_role("button", name="Discard", exact=True).click()
            print("UI journey: checking reload and durable conversation", flush=True)
            await page.reload()
            await expect(panel).to_be_visible()
            await expect(panel.locator(".chat-message.assistant").last).to_contain_text(
                "Saved", timeout=30000
            )
            await expect(page.locator(".managed-browser-screen img")).to_be_visible()
            assert parse_qs(urlsplit(page.url).query)["session"] == [conversation_id]
            assert "handoff" not in parse_qs(urlsplit(page.url).query)
            await expect(panel.locator("#analyst-message")).to_be_in_viewport(ratio=1)
            await expect(
                panel.get_by_role("button", name="Send message", exact=True)
            ).to_be_in_viewport(ratio=1)
            assert not await page.evaluate(
                "document.documentElement.scrollWidth > innerWidth + 1"
            )
            await page.screenshot(
                path=str(evidence_root / "real-browser-ui-desktop.png"), full_page=True
            )
            return {
                "production_ui_real_core": True,
                "production_ui_live_codex": True,
                "ui_origin": origin,
                "ui_viewport": "1440x900",
                "ui_engine": "headed Chromium",
                "ui_same_conversation_reload": True,
                "ui_inline_approval_page_change": True,
            }
        except Exception:
            with suppress(Exception):
                await page.screenshot(
                    path=str(evidence_root / "real-browser-ui-failure.png"),
                    full_page=True,
                )
            raise
        finally:
            await context.tracing.stop(
                path=str(evidence_root / "real-browser-ui-trace.zip")
            )
            await browser.close()
