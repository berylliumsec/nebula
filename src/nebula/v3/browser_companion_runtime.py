"""Fixed, bounded page operations for the interactive Chromium workspace."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Any
from weakref import WeakKeyDictionary

from .browser_companion import CompanionRequest


CAPTURE = """({kind, x, y}) => {
  const sensitive = el => !!el.closest('input,textarea,[contenteditable],[data-sensitive],[autocomplete="one-time-code"]');
  const text = el => {
    const clone = el.cloneNode(true);
    clone.querySelectorAll('script,style,input,textarea,[contenteditable],[data-sensitive]').forEach(n => n.remove());
    return (clone.textContent || '').trim().slice(0,12000);
  };
  const nodes = Array.from(document.querySelectorAll('a,button,input,select,textarea,[role="button"]')).slice(0,200);
  currentNodes = new Map();
  const nodeId = el => {
    if (!nodeIds.has(el)) nodeIds.set(el, String(nextId++));
    const id = nodeIds.get(el); currentNodes.set(id, el); return id;
  };
  const elements = nodes.map(el => ({id:nodeId(el), tag:el.tagName.toLowerCase(),
    label:(el.getAttribute('aria-label') || el.labels?.[0]?.textContent || (sensitive(el) ? '' : el.textContent) || el.getAttribute('placeholder') || el.tagName).trim().slice(0,200),
    sensitive:el.getAttribute('type') === 'password' || /password|secret|token|one.?time|cc-number|cc-csc/i.test([el.getAttribute('name'),el.getAttribute('autocomplete')].join(' ')), type:el.getAttribute('type') || ''}));
  let chosen = document.body;
  if (kind === 'element') chosen = document.elementFromPoint(x,y);
  let content = chosen && !sensitive(chosen) ? text(chosen) : '';
  if (kind === 'selection') {
    const selection = window.getSelection();
    const anchor = selection?.anchorNode?.parentElement;
    const focus = selection?.focusNode?.parentElement;
    content = anchor && focus && !sensitive(anchor) && !sensitive(focus) ? selection.toString().slice(0,12000) : '';
  }
  return {url:location.href, title:document.title.slice(0,500), text:content, elements,
    structure:chosen ? {tag:chosen.tagName.toLowerCase(), role:chosen.getAttribute('role')?.slice(0,200) || null} : null};
}"""


_page_contexts: WeakKeyDictionary[Any, tuple[Any, Any]] = WeakKeyDictionary()


async def page_runtime(page: Any) -> tuple[Any, Any]:
    """Keep node identity in a private browser handle, never in page attributes."""
    generation = await page.evaluate("performance.timeOrigin")
    cached = _page_contexts.get(page)
    if cached is not None and cached[0] == generation:
        return cached
    if cached is not None:
        await cached[1].dispose()
    runtime = await page.evaluate_handle(
        "(() => { const nodeIds = new WeakMap(); let nextId = 0; "
        "let currentNodes = new Map(); return {capture: " + CAPTURE + ", "
        "element: id => currentNodes.get(id) || null}; })()"
    )
    _page_contexts[page] = (generation, runtime)
    return generation, runtime


async def capture(page: Any, request: CompanionRequest) -> dict[str, Any]:
    generation, runtime = await page_runtime(page)
    result = await runtime.evaluate(
        "(runtime, args) => runtime.capture(args)",
        {"kind": request.capture_kind, "x": request.x, "y": request.y},
    )
    # Every action must use a fresh capture; URL alone is not an element identity.
    import json

    revision_source = (
        result
        if request.capture_kind == "page"
        else await runtime.evaluate(
            "runtime => runtime.capture({kind: 'page', x: 0, y: 0})"
        )
    )
    fingerprint = hashlib.sha256(
        json.dumps([generation, revision_source], sort_keys=True).encode()
    ).hexdigest()
    result["page_revision"] = fingerprint
    result["captured_at"] = datetime.now(timezone.utc).isoformat()
    if request.capture_kind == "region":
        if request.width < 1 or request.height < 1:
            raise ValueError("Select a nonempty screenshot region.")
        image = await page.screenshot(
            mask=[page.locator("input,textarea,[contenteditable],[data-sensitive]")],
            clip={
                "x": request.x,
                "y": request.y,
                "width": request.width,
                "height": request.height,
            },
        )
        result["image"] = base64.b64encode(image).decode()
    return result


async def operate(
    manager: Any, identity_id: str, request: CompanionRequest
) -> dict[str, Any]:
    from .browserd import _network_url

    receipt = await manager.ensure_identity(identity_id)
    selected = None
    if request.operation == "new_tab":
        if len(receipt.tab_ids) >= 16:
            raise ValueError(
                "Close a tab before opening another; this session supports 16 tabs."
            )
        from uuid import uuid4

        selected = str(uuid4())
        first = await manager.page_for_screencast(identity_id, receipt.tab_ids[0])
        manager._tabs[(identity_id, selected)] = await first.context.new_page()
        receipt = await manager.ensure_identity(identity_id)
    elif request.operation == "close_tab":
        page = await manager.page_for_screencast(identity_id, request.tab_id)
        if len(receipt.tab_ids) == 1:
            await page.goto("about:blank")
            selected = request.tab_id
        else:
            await page.close()
            manager._tabs.pop((identity_id, request.tab_id), None)
        receipt = await manager.ensure_identity(identity_id)
        selected = selected or receipt.tab_ids[0]
    if request.operation in {"tabs", "new_tab", "close_tab"}:
        tabs = []
        for tab_id in receipt.tab_ids:
            page = await manager.page_for_screencast(identity_id, tab_id)
            tabs.append(
                {"id": tab_id, "url": page.url, "title": (await page.title())[:500]}
            )
        return {"tabs": tabs, **({"active_tab_id": selected} if selected else {})}
    page = await manager.page_for_screencast(identity_id, request.tab_id)
    if request.operation == "navigate":
        await page.goto(
            _network_url(request.url), wait_until="domcontentloaded", timeout=20000
        )
        return await capture(
            page, CompanionRequest(operation="capture", tab_id=request.tab_id)
        )
    if request.operation == "capture":
        return await capture(page, request)
    current = await capture(
        page, CompanionRequest(operation="capture", tab_id=request.tab_id)
    )
    if not request.page_revision or request.page_revision != current["page_revision"]:
        raise ValueError(
            "Page content changed. Capture the current page before acting."
        )
    if request.operation == "scroll":
        await page.mouse.wheel(0, request.delta)
    else:
        if request.element_id is None or not request.element_id.isdecimal():
            raise ValueError("Choose an element from the current accessible page view.")
        if not any(item["id"] == request.element_id for item in current["elements"]):
            raise ValueError("The selected element is no longer present.")
        _, runtime = await page_runtime(page)
        handle = await runtime.evaluate_handle(
            "(runtime, id) => runtime.element(id)", request.element_id
        )
        element = handle.as_element()
        if element is None:
            await handle.dispose()
            raise ValueError("The selected element is no longer present.")
        try:
            if request.operation == "highlight":
                await element.scroll_into_view_if_needed()
                await element.evaluate(
                    "el => { el.style.outline = '3px solid #7c6cff'; }"
                )
            elif request.operation == "click":
                await element.click(timeout=5000)
            elif request.operation == "fill":
                await element.fill(request.text, timeout=5000)
            elif request.operation == "select":
                await element.select_option(label=request.text, timeout=5000)
            elif request.operation == "press":
                if request.text not in {
                    "Enter",
                    "Tab",
                    "Escape",
                    "ArrowDown",
                    "ArrowUp",
                    "Space",
                }:
                    raise ValueError("Unsupported key.")
                await element.press(request.text, timeout=5000)
        finally:
            await handle.dispose()
    return await capture(
        page, CompanionRequest(operation="capture", tab_id=request.tab_id)
    )
