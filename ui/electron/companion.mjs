import { createHash } from "node:crypto";

const WORLD = 1001;
const script = (body, argumentsValue = {}) => `(() => {
  const args = ${JSON.stringify(argumentsValue)};
  if (!globalThis.__nebulaCompanion || globalThis.__nebulaCompanion.generation !== performance.timeOrigin) {
    globalThis.__nebulaCompanion = { generation: performance.timeOrigin,
      ids: new WeakMap(), nodes: new Map(), nextId: 0 };
  }
  const runtime = globalThis.__nebulaCompanion;
  ${body}
})()`;

async function evaluate(contents, body, args) {
  return contents.executeJavaScriptInIsolatedWorld(WORLD, [{ code: script(body, args) }]);
}

const captureBody = `
  const sensitive = (el) => el.getAttribute('type') === 'password' ||
    /password|secret|token|one.?time|cc-number|cc-csc/i.test([
      el.getAttribute('name'), el.getAttribute('autocomplete')].join(' '));
  const nodes = Array.from(document.querySelectorAll('a,button,input,select,textarea,[role="button"]')).slice(0,200);
  runtime.nodes = new Map();
  const elements = nodes.map((el) => {
    if (!runtime.ids.has(el)) runtime.ids.set(el, String(runtime.nextId++));
    const id = runtime.ids.get(el); runtime.nodes.set(id, el);
    return { id, tag: el.tagName.toLowerCase(),
      label: (el.getAttribute('aria-label') || el.labels?.[0]?.textContent ||
        el.textContent || el.getAttribute('placeholder') || el.tagName).trim().slice(0,200),
      value: !sensitive(el) && 'value' in el ? String(el.value).slice(0,12000) : null,
      sensitive: sensitive(el), type: el.getAttribute('type') || '' };
  });
  let selected = document.body;
  if (args.kind === 'element') selected = document.elementFromPoint(args.x, args.y);
  let text = (selected?.textContent || '').trim().slice(0,12000);
  if (args.kind === 'selection') text = String(window.getSelection() || '').slice(0,12000);
  return { generation: runtime.generation, url: location.href,
    title: document.title.slice(0,500), text,
    html: args.kind === 'element' && selected ? (() => {
      const copy = selected.cloneNode(true);
      for (const input of copy.querySelectorAll('input,textarea,select')) {
        input.removeAttribute('value');
        input.textContent = '';
      }
      copy.removeAttribute?.('value');
      return (copy.outerHTML || '').slice(0,24000);
    })() : '', elements,
    structure: selected ? { tag: selected.tagName.toLowerCase(),
      role: selected.getAttribute('role')?.slice(0,200) || null } : null };
`;

async function capture(tab, request) {
  const contents = tab.view.webContents;
  const full = await evaluate(contents, captureBody, { kind: "page", x: 0, y: 0 });
  const revision = createHash("sha256").update(JSON.stringify(full)).digest("hex");
  const requested = request.capture_kind === "page" || !request.capture_kind
    ? full : await evaluate(contents, captureBody, {
      kind: request.capture_kind, x: request.x ?? 0, y: request.y ?? 0,
    });
  const { generation: _generation, ...result } = requested;
  result.page_revision = revision;
  result.captured_at = new Date().toISOString();
  if (request.capture_kind === "region") {
    const { x = 0, y = 0, width = 0, height = 0 } = request;
    if (width < 1 || height < 1 || width > 1920 || height > 1080) {
      throw new Error("Select a nonempty screenshot region within the page.");
    }
    const image = await contents.capturePage({ x, y, width, height });
    const bytes = image.toPNG();
    if (bytes.length > 4 * 1024 * 1024) throw new Error("Select a smaller screenshot region.");
    result.image = bytes.toString("base64");
  }
  return result;
}

async function act(tab, request) {
  const contents = tab.view.webContents;
  const current = await capture(tab, { capture_kind: "page" });
  if (!request.page_revision || request.page_revision !== current.page_revision) {
    throw new Error("Page content changed. Capture the current page before acting.");
  }
  if (request.operation === "scroll") {
    await evaluate(contents, "window.scrollBy(0, args.delta); return true;", { delta: request.delta ?? 0 });
    return capture(tab, { capture_kind: "page" });
  }
  const id = request.element_id;
  if (typeof id !== "string" || !/^\d+$/.test(id) ||
      !current.elements.some((element) => element.id === id)) {
    throw new Error("Choose an element from the current page capture.");
  }
  const allowedKeys = new Set(["Enter", "Tab", "Escape", "ArrowDown", "ArrowUp", "Space"]);
  if (request.operation === "press" && !allowedKeys.has(request.text)) {
    throw new Error("Unsupported key.");
  }
  if (request.operation === "press") {
    await evaluate(contents, `
      const el = runtime.nodes.get(args.id);
      if (!el || !el.isConnected) throw new Error('The selected element is no longer present.');
      el.focus(); return true;
    `, { id });
    contents.focus();
    const keyCode = request.text === "Space" ? "Space" : request.text;
    contents.sendInputEvent({ type: "keyDown", keyCode });
    contents.sendInputEvent({ type: "keyUp", keyCode });
    return capture(tab, { capture_kind: "page" });
  }
  await evaluate(contents, `
    const el = runtime.nodes.get(args.id);
    if (!el || !el.isConnected) throw new Error('The selected element is no longer present.');
    if (args.operation === 'highlight') { el.scrollIntoView({ block: 'nearest' }); el.style.outline = '3px solid #7c6cff'; }
    else if (args.operation === 'click') { el.click(); }
    else if (args.operation === 'fill') {
      if (!('value' in el)) throw new Error('This element cannot be filled.');
      const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(el, args.text);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (args.operation === 'select') {
      if (!(el instanceof HTMLSelectElement)) throw new Error('Choose a select element.');
      const option = Array.from(el.options).find((item) => item.label === args.text);
      if (!option) throw new Error('The selected option is no longer available.');
      el.value = option.value; el.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (args.operation === 'upload') {
      if (!(el instanceof HTMLInputElement) || el.type !== 'file') throw new Error('Choose a file input.');
      const bytes = Uint8Array.from(atob(args.upload.content_base64), ch => ch.charCodeAt(0));
      const file = new File([bytes], args.upload.name, { type: args.upload.mime_type });
      const transfer = new DataTransfer(); transfer.items.add(file); el.files = transfer.files;
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else throw new Error('Unsupported browser operation.');
    return true;
  `, { id, operation: request.operation, text: request.text ?? "", upload: request.upload_file });
  return capture(tab, { capture_kind: "page" });
}

export async function companionOperation(host, identityId, request) {
  const projectId = host.identityProject(identityId);
  const project = host.project(projectId);
  if (request.operation === "new_tab") await host.newTab(projectId);
  if (request.operation === "close_tab") await host.closeTab(projectId, request.tab_id);
  if (["tabs", "new_tab", "close_tab"].includes(request.operation)) {
    const state = host.state(projectId);
    return { tabs: state.tabs.map(({ id, url, title }) => ({ id, url, title })),
      active_tab_id: project.activeTabId };
  }
  const tab = host.tab(projectId, request.tab_id);
  if (request.operation === "navigate") {
    await host.navigate(projectId, tab.id, request.url);
    return capture(tab, { capture_kind: "page" });
  }
  if (request.operation === "capture") return capture(tab, request);
  return act(tab, request);
}
