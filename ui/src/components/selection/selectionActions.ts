export const SELECTION_TEXT_LIMIT = 20_000;

export interface SelectionSource {
  /** Stable source category persisted with a chat or note attachment. */
  kind: string;
  /** Stable identifier when the source already has one. */
  id?: string;
  /** Short, user-facing label shown with the draft attachment. */
  label: string;
  metadata?: Readonly<Record<string, string>>;
}

export interface SelectionAnchor {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface SelectionActionDraft {
  text: string;
  originalLength: number;
  truncated: boolean;
  source: SelectionSource;
  anchor: SelectionAnchor;
}

/** Wire-compatible with api/types.ChatContextAttachment without coupling UI primitives to the API client. */
export interface HashedSelectionAttachment {
  sourceKind: string;
  sourceId?: string;
  sourceLabel: string;
  text: string;
  sha256: string;
  truncated: boolean;
}

export interface PresentSelectionInput {
  text: string;
  source: SelectionSource;
  anchorRect?: Pick<DOMRect, "left" | "top" | "right" | "bottom">;
}

export interface DomSelectionOptions {
  limit?: number;
  isSensitive?: (element: Element) => boolean;
  resolveSource?: (element: Element | null) => SelectionSource;
}

const DEFAULT_SOURCE: SelectionSource = {
  kind: "document",
  label: "Page selection",
};

const TEXT_INPUT_TYPES = new Set([
  "",
  "email",
  "search",
  "tel",
  "text",
  "url",
]);

function anchorFromRect(
  rect: Pick<DOMRect, "left" | "top" | "right" | "bottom"> | undefined,
): SelectionAnchor {
  if (!rect) return { left: 0, top: 0, right: 0, bottom: 0 };
  return {
    left: Number.isFinite(rect.left) ? rect.left : 0,
    top: Number.isFinite(rect.top) ? rect.top : 0,
    right: Number.isFinite(rect.right) ? rect.right : 0,
    bottom: Number.isFinite(rect.bottom) ? rect.bottom : 0,
  };
}

export function createSelectionDraft(
  input: PresentSelectionInput,
  limit = SELECTION_TEXT_LIMIT,
): SelectionActionDraft | undefined {
  const boundedLimit = Math.max(1, Math.floor(limit));
  if (!input.text.length) return undefined;
  let end = Math.min(input.text.length, boundedLimit);
  // Never leave an unpaired UTF-16 surrogate at the attachment boundary.
  if (end < input.text.length
    && end > 0
    && input.text.charCodeAt(end - 1) >= 0xd800
    && input.text.charCodeAt(end - 1) <= 0xdbff
    && input.text.charCodeAt(end) >= 0xdc00
    && input.text.charCodeAt(end) <= 0xdfff) {
    end -= 1;
  }
  return {
    text: input.text.slice(0, end),
    originalLength: input.text.length,
    truncated: input.text.length > boundedLimit,
    source: {
      ...input.source,
      metadata: input.source.metadata ? { ...input.source.metadata } : undefined,
    },
    anchor: anchorFromRect(input.anchorRect),
  };
}

export function isSensitiveSelectionElement(element: Element): boolean {
  if (element.closest("[data-nebula-sensitive], [data-selection-actions='off']")) return true;
  return element instanceof HTMLInputElement && element.type.toLowerCase() === "password";
}

function elementForNode(node: Node | null): Element | null {
  if (!node) return null;
  return node instanceof Element ? node : node.parentElement;
}

function defaultSource(element: Element | null): SelectionSource {
  const sourceElement = element?.closest<HTMLElement>("[data-selection-source-kind]");
  if (!sourceElement) return DEFAULT_SOURCE;
  return {
    kind: sourceElement.dataset.selectionSourceKind || "document",
    id: sourceElement.dataset.selectionSourceId || undefined,
    label: sourceElement.dataset.selectionSourceLabel || "Selected text",
  };
}

function rangeContainsSensitiveContent(
  range: Range,
  isSensitive: (element: Element) => boolean,
): boolean {
  const boundaryElements = [
    elementForNode(range.startContainer),
    elementForNode(range.endContainer),
    elementForNode(range.commonAncestorContainer),
  ];
  if (boundaryElements.some((element) => element && isSensitive(element))) return true;

  const fragment = range.cloneContents();
  const elements = Array.from(fragment.querySelectorAll("*"));
  return elements.some(isSensitive);
}

function rangeRect(range: Range, fallback?: Element | null): SelectionAnchor {
  if (typeof range.getBoundingClientRect === "function") {
    const rect = range.getBoundingClientRect();
    if (rect.width || rect.height) return anchorFromRect(rect);
  }
  return anchorFromRect(fallback?.getBoundingClientRect());
}

/**
 * Reads an ordinary DOM selection without modifying or clearing it. Selections
 * that touch a sensitive boundary are rejected as a whole.
 */
export function readDomSelection(
  selection: Selection | null,
  options: DomSelectionOptions = {},
): SelectionActionDraft | undefined {
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1) return undefined;
  const range = selection.getRangeAt(0);
  const sourceElement = elementForNode(range.commonAncestorContainer);
  const sensitive = (element: Element) => isSensitiveSelectionElement(element) || Boolean(options.isSensitive?.(element));
  if (rangeContainsSensitiveContent(range, sensitive)) return undefined;
  const text = selection.toString();
  const draft = createSelectionDraft({
    text,
    source: (options.resolveSource ?? defaultSource)(sourceElement),
    anchorRect: rangeRect(range, sourceElement),
  }, options.limit);
  return draft;
}

export type SelectableTextControl = HTMLInputElement | HTMLTextAreaElement;

export function isSelectableTextControl(target: EventTarget | null): target is SelectableTextControl {
  if (target instanceof HTMLTextAreaElement) return true;
  return target instanceof HTMLInputElement && TEXT_INPUT_TYPES.has(target.type.toLowerCase());
}

/** Reads the exact selection from a permitted input or textarea. */
export function readTextControlSelection(
  control: SelectableTextControl,
  options: DomSelectionOptions = {},
): SelectionActionDraft | undefined {
  const sensitive = (element: Element) => isSensitiveSelectionElement(element) || Boolean(options.isSensitive?.(element));
  if (sensitive(control)) return undefined;
  const start = control.selectionStart;
  const end = control.selectionEnd;
  if (start === null || end === null || start === end) return undefined;
  const source = (options.resolveSource ?? defaultSource)(control);
  return createSelectionDraft({
    text: control.value.slice(Math.min(start, end), Math.max(start, end)),
    source,
    anchorRect: control.getBoundingClientRect(),
  }, options.limit);
}

export async function copySelectionText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("The selected text could not be copied.");
}

/** Hashes the exact bounded draft only when the user submits it. */
export async function createHashedSelectionAttachment(
  draft: SelectionActionDraft,
): Promise<HashedSelectionAttachment> {
  if (!/^[a-z0-9._-]+$/.test(draft.source.kind) || draft.source.kind.length > 100) {
    throw new Error("Selection source kinds must be 1 to 100 lowercase identifier characters.");
  }
  if (!draft.source.label.length || draft.source.label.length > 500) {
    throw new Error("Selection source labels must contain 1 to 500 characters.");
  }
  if (draft.source.id && draft.source.id.length > 200) {
    throw new Error("Selection source identifiers cannot exceed 200 characters.");
  }
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error("Web Crypto is required to attach selected context.");
  const digest = new Uint8Array(await subtle.digest("SHA-256", new TextEncoder().encode(draft.text)));
  return {
    sourceKind: draft.source.kind,
    sourceId: draft.source.id,
    sourceLabel: draft.source.label,
    text: draft.text,
    sha256: Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join(""),
    truncated: draft.truncated,
  };
}
