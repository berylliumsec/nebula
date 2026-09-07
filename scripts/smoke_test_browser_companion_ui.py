"""Production UI journey against the isolated real Core and managed browser.

Called by smoke_test_browser_companion_core.py; no API routes are mocked.
"""

from __future__ import annotations

from contextlib import suppress
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect


async def exercise_ui(
    origin: str,
    pairing: dict[str, str],
    conversation_id: str,
    evidence_root: Path,
    target_url: str = "http://browserd-smoke.example.test/",
    profile: str = "desktop",
) -> dict[str, object]:
    profiles = {
        "desktop": ("chromium", None, 1440, 900),
        "compact": ("chromium", None, 1024, 700),
        "chromium-landscape": ("chromium", "Pixel 5", 844, 390),
        "webkit-landscape": ("webkit", "iPhone 13", 844, 390),
        **{
            f"{engine}-{width}": (
                engine,
                "Pixel 5" if engine == "chromium" else "iPhone 13",
                width,
                height,
            )
            for engine in ("chromium", "webkit")
            for width, height in ((320, 700), (390, 844), (430, 932))
        },
    }
    engine, device, width, height = profiles[profile]
    evidence_root = evidence_root / profile
    evidence_root.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await getattr(playwright, engine).launch(headless=False)
        context = await browser.new_context(
            **{
                **(playwright.devices[device] if device else {}),
                "viewport": {"width": width, "height": height},
                "reduced_motion": "reduce",
            }
        )
        await context.tracing.start(screenshots=True, snapshots=True, sources=True)
        page = await context.new_page()
        page.set_default_timeout(20000)
        try:
            print(f"UI journey ({profile}): opening the browser workspace", flush=True)
            await page.goto(
                f"{origin}/#pair={pairing['secret']}&code={pairing['confirmation_code']}"
            )
            await page.get_by_label("Device name").fill("Browser UI validation")
            await page.get_by_role("button", name="Pair device", exact=True).click()
            await expect(
                page.get_by_role("button", name="More workbench views", exact=True)
                if width <= 760
                else page.get_by_role("tab", name="Project browser", exact=True)
            ).to_be_visible(timeout=20000)
            await page.goto(f"{origin}/?view=browser&session={conversation_id}")
            await expect(page.get_by_label("Browser engine")).to_have_value(
                "managed", timeout=30000
            )
            panel = page.get_by_role(
                "complementary", name="Browser Assistant", exact=True
            )
            await expect(panel).to_be_visible(timeout=30000)
            if device:
                await panel.get_by_role(
                    "button", name="Collapse browser Assistant"
                ).click()
            await expect(page.locator(".managed-browser-screen img")).to_be_visible()
            await page.get_by_label("Browser address").fill(target_url)
            await page.get_by_role("button", name="Go", exact=True).click()
            preview = page.get_by_role("region", name="Browser context preview")
            await expect(preview).to_contain_text("Ready")
            await preview.get_by_role("button", name="Discard", exact=True).click()
            print(
                "UI journey: selecting an element, text, and screenshot region",
                flush=True,
            )
            screen = page.locator(".managed-browser-screen img")
            await page.get_by_role("button", name="Pick element", exact=True).click()

            # Coordinates belong to the controlled fixture's visible button. Scale
            # through the actual image, just as an operator's pointer does.
            async def screen_point(x: float, y: float) -> dict[str, float]:
                await screen.scroll_into_view_if_needed()
                return await screen.evaluate(
                    "(image, point) => { const r = image.getBoundingClientRect(); return {x:point.x*r.width/image.naturalWidth, y:point.y*r.height/image.naturalHeight}; }",
                    {"x": x, "y": y},
                )

            point = await screen_point(20, 18)
            if device:
                await screen.tap(position=point)
            else:
                await screen.click(position=point)
            await expect(preview.locator("pre")).to_have_text("Ready")
            await preview.get_by_role("button", name="Discard", exact=True).click()
            await screen.focus()
            await screen.press("Control+a")
            await page.get_by_role(
                "button", name="Ask about selected text", exact=True
            ).click()
            await expect(preview.locator("pre")).to_contain_text("Ready")
            await preview.get_by_role("button", name="Discard", exact=True).click()
            await page.get_by_role("button", name="Select region", exact=True).click()
            start = await screen_point(5, 5)
            end = await screen_point(65, 35)
            await screen.drag_to(screen, source_position=start, target_position=end)
            await expect(
                preview.get_by_role("img", name="Selected page region")
            ).to_be_visible()
            await preview.get_by_role("button", name="Discard", exact=True).click()
            await expect(preview).to_be_hidden()
            await page.get_by_role("button", name="Ask about page", exact=True).click()
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
            if device:
                await panel.get_by_role(
                    "button", name="Collapse browser Assistant"
                ).click()
            resume = page.get_by_role(
                "button", name="Resume assistant control", exact=True
            )
            if await resume.is_visible():
                await resume.click()
            if device:
                await page.get_by_role("button", name="Assistant", exact=True).click()
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
            if device:
                await panel.get_by_role(
                    "button", name="Collapse browser Assistant"
                ).click()
            await page.get_by_role("button", name="Ask about page", exact=True).click()
            await expect(preview).to_contain_text("Saved")
            await preview.get_by_role("button", name="Discard", exact=True).click()
            print("UI journey: checking reload and durable conversation", flush=True)
            await page.reload()
            await expect(panel).to_be_visible(timeout=30000)
            await expect(panel.locator(".chat-message.assistant").last).to_contain_text(
                "Saved", timeout=30000
            )
            await expect(page.locator(".managed-browser-screen img")).to_be_visible()
            assert parse_qs(urlsplit(page.url).query)["session"] == [conversation_id]
            assert "handoff" not in parse_qs(urlsplit(page.url).query), (
                "A cleared context handoff remained in the reload URL."
            )
            await expect(panel.locator("#analyst-message")).to_be_in_viewport(ratio=1)
            await expect(
                panel.get_by_role("button", name="Send message", exact=True)
            ).to_be_in_viewport(ratio=1)
            assert not await page.evaluate(
                "document.documentElement.scrollWidth > innerWidth + 1"
            ), "The browser workspace overflows the mobile viewport horizontally."
            await page.screenshot(
                path=str(evidence_root / "real-browser-ui-desktop.png"), full_page=True
            )
            await page.goto(f"{origin}/?view=chat&session={conversation_id}")
            await expect(page.locator(".chat-message.assistant").last).to_contain_text(
                "Saved", timeout=30000
            )
            assert parse_qs(urlsplit(page.url).query)["session"] == [conversation_id]
            await page.goto(f"{origin}/?view=browser&session={conversation_id}")
            await expect(panel.locator(".chat-message.assistant").last).to_contain_text(
                "Saved", timeout=30000
            )
            result = {
                "production_ui_real_core": True,
                "production_ui_live_codex": True,
                "ui_origin": origin,
                "ui_viewport": f"{width}x{height}",
                "ui_engine": f"headed {engine}",
                "ui_browser_version": browser.version,
                "ui_build_sha256": hashlib.sha256(
                    (
                        Path(__file__).resolve().parents[1] / "ui/dist/index.html"
                    ).read_bytes()
                ).hexdigest(),
                "ui_device_emulation": device,
                "ui_main_assistant_same_conversation": True,
                "ui_same_conversation_reload": True,
                "ui_inline_approval_page_change": True,
                "ui_element_text_region_selection": True,
            }
            (evidence_root / "result.json").write_text(json.dumps(result, indent=2))
            return result
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
