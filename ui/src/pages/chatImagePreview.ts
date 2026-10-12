import type { ReconciledConversationMessage } from "./chatMessageReconciliation";

export interface ChatImagePalette {
  background: string;
  text: string;
  muted: string;
  userBackground: string;
  fontFamily: string;
  theme: string;
}

export interface ChatImageTile {
  url: string;
  top: number;
  height: number;
}

export interface ChatImagePreview {
  key: string;
  width: number;
  height: number;
  bytes: number;
  revision?: number;
  messageTops: number[];
  tiles: ChatImageTile[];
  dispose(): void;
}

type PreviewMessage = Pick<ReconciledConversationMessage, "id" | "role" | "content" | "contentBlocks">;

interface DrawLine { text: string; y: number }
interface DrawMessage { top: number; height: number; x: number; width: number; role: string; lines: DrawLine[] }

const TILE_HEIGHT = 1024;
const MAX_TILES = 128;
const MAX_PREVIEW_BYTES = 16 * 1024 * 1024;

export function chatImagePalette(viewport: HTMLElement): ChatImagePalette {
  const style = getComputedStyle(viewport);
  const root = getComputedStyle(document.documentElement);
  const token = (name: string, fallback: string) => root.getPropertyValue(name).trim() || fallback;
  return {
    background: style.backgroundColor === "rgba(0, 0, 0, 0)" ? token("--canvas", "#111318") : style.backgroundColor,
    text: token("--text", "#f0f0f0"),
    muted: token("--muted-strong", "#a0a0a0"),
    userBackground: token("--surface-muted", "#22252b"),
    fontFamily: style.fontFamily || "system-ui, sans-serif",
    theme: document.documentElement.dataset.theme ?? "default",
  };
}

export function chatImageWidth(viewport: HTMLElement): number {
  const style = getComputedStyle(viewport);
  return Math.floor(viewport.clientWidth - parseFloat(style.paddingLeft || "0") - parseFloat(style.paddingRight || "0"));
}

/** A cheap identity check for the exact cached text, width and color scheme. */
export function chatImageKey(messages: PreviewMessage[], width: number, palette: ChatImagePalette): string {
  let hash = 2166136261;
  const add = (value: string) => {
    for (let index = 0; index < value.length; index += 1) hash = Math.imul(hash ^ value.charCodeAt(index), 16777619);
  };
  for (const message of messages) {
    add(message.id ?? ""); add(message.role); add(message.content);
    for (const block of message.contentBlocks ?? []) { add(block.type); add(block.text ?? ""); add(block.alt ?? ""); }
  }
  return `${width}:${palette.theme}:${palette.background}:${palette.text}:${palette.muted}:${palette.userBackground}:${palette.fontFamily}:${messages.length}:${hash >>> 0}`;
}

function wrapText(context: CanvasRenderingContext2D, text: string, maxWidth: number): string[] {
  const wrapped: string[] = [];
  for (const paragraph of text.replace(/\r\n?/g, "\n").split("\n")) {
    if (!paragraph) { wrapped.push(""); continue; }
    let line = "";
    for (const word of paragraph.split(/(\s+)/).filter(Boolean)) {
      const candidate = line + word;
      if (context.measureText(candidate).width <= maxWidth) { line = candidate; continue; }
      if (line) { wrapped.push(line.trimEnd()); line = ""; }
      if (context.measureText(word).width <= maxWidth) { line = word.trimStart(); continue; }
      // Break oversized URLs/code tokens without drawing outside the viewport.
      for (const character of word) {
        if (line && context.measureText(line + character).width > maxWidth) { wrapped.push(line); line = ""; }
        line += character;
      }
    }
    wrapped.push(line.trimEnd());
  }
  return wrapped;
}

function layoutMessages(messages: PreviewMessage[], width: number, fontFamily: string): {height: number; rows: DrawMessage[]} {
  const context = document.createElement("canvas").getContext("2d");
  if (!context) return {height: 0, rows: []};
  context.font = `13px ${fontFamily}`;
  const rows: DrawMessage[] = [];
  let top = 8;
  for (const message of messages) {
    const user = message.role === "user";
    const x = user ? Math.max(8, Math.floor(width * 0.16)) : 8;
    const rowWidth = width - x - 8;
    const textWidth = Math.max(40, rowWidth - 28);
    const attachments = (message.contentBlocks ?? [])
      .filter((block) => block.type === "image" || block.type === "artifact")
      .map((block) => `[${block.type}: ${block.alt || block.artifactId || "attachment"}]`);
    const lines = wrapText(context, [message.content, ...attachments].filter(Boolean).join("\n"), textWidth);
    const rowHeight = 35 + Math.max(1, lines.length) * 21 + 14;
    rows.push({top, height: rowHeight, x, width: rowWidth, role: message.role,
      lines: lines.map((line, index) => ({text: line, y: top + 39 + index * 21}))});
    top += rowHeight + 14;
  }
  return {height: Math.ceil(top + 8), rows};
}

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob | null> {
  return new Promise((resolve) => canvas.toBlob(resolve, "image/webp", 0.82));
}

function idle(): Promise<void> {
  return new Promise((resolve) => {
    if ("requestIdleCallback" in window) window.requestIdleCallback(() => resolve(), {timeout: 300});
    else globalThis.setTimeout(resolve, 0);
  });
}

/** Renders every loaded durable message into small, lazily decoded image tiles. */
export async function renderChatImagePreview(
  messages: PreviewMessage[], width: number, palette: ChatImagePalette, revision?: number, signal?: AbortSignal,
): Promise<ChatImagePreview | undefined> {
  if (!messages.length || width < 160 || signal?.aborted) return undefined;
  const layout = layoutMessages(messages, width, palette.fontFamily);
  const tileCount = Math.ceil(layout.height / TILE_HEIGHT);
  if (!layout.rows.length || tileCount > MAX_TILES) return undefined;
  const tiles: ChatImageTile[] = [];
  let bytes = 0;
  try {
    for (let index = 0; index < tileCount; index += 1) {
      if (signal?.aborted) return undefined;
      await idle();
      if (signal?.aborted) return undefined;
      const top = index * TILE_HEIGHT;
      const height = Math.min(TILE_HEIGHT, layout.height - top);
      const scale = Math.min(window.devicePixelRatio || 1, 1.5);
      const canvas = document.createElement("canvas");
      canvas.width = Math.ceil(width * scale);
      canvas.height = Math.ceil(height * scale);
      const context = canvas.getContext("2d", {alpha: false});
      if (!context) return undefined;
      context.scale(scale, scale);
      context.fillStyle = palette.background;
      context.fillRect(0, 0, width, height);
      for (const row of layout.rows) {
        if (row.top >= top + height) break;
        if (row.top + row.height <= top) continue;
        if (row.role === "user") {
          context.fillStyle = palette.userBackground;
          context.fillRect(row.x, row.top - top, row.width, row.height);
        }
        context.font = `600 12px ${palette.fontFamily}`;
        context.fillStyle = palette.muted;
        context.fillText(row.role === "user" ? "You" : row.role === "assistant" ? "Assistant" : row.role, row.x + 14, row.top + 23 - top);
        context.font = `13px ${palette.fontFamily}`;
        context.fillStyle = palette.text;
        for (const line of row.lines) {
          if (line.y < top - 16 || line.y > top + height + 16) continue;
          context.fillText(line.text, row.x + 14, line.y - top);
        }
      }
      const blob = await canvasBlob(canvas);
      if (!blob || signal?.aborted) return undefined;
      bytes += blob.size;
      if (bytes > MAX_PREVIEW_BYTES) return undefined;
      tiles.push({url: URL.createObjectURL(blob), top, height});
    }
    return {
      key: chatImageKey(messages, width, palette), width, height: layout.height, bytes, revision,
      messageTops: layout.rows.map((row) => row.top), tiles,
      dispose: () => tiles.forEach((tile) => URL.revokeObjectURL(tile.url)),
    };
  } finally {
    if (tiles.length !== tileCount) tiles.forEach((tile) => URL.revokeObjectURL(tile.url));
  }
}

/** Bounded encoded-memory cache. The browser decodes only visible <img> tiles. */
export class ChatImagePreviewCache {
  private readonly entries = new Map<string, ChatImagePreview>();
  private bytes = 0;

  constructor(private readonly capacity = 10, private readonly maxBytes = 24 * 1024 * 1024) {}

  get(id: string): ChatImagePreview | undefined {
    const image = this.entries.get(id);
    if (image) { this.entries.delete(id); this.entries.set(id, image); }
    return image;
  }

  set(id: string, image: ChatImagePreview): void {
    this.delete(id);
    if (image.bytes > this.maxBytes) { image.dispose(); return; }
    this.entries.set(id, image);
    this.bytes += image.bytes;
    while (this.entries.size > this.capacity || this.bytes > this.maxBytes) this.delete(this.entries.keys().next().value!);
  }

  delete(id: string): void {
    const image = this.entries.get(id);
    if (!image) return;
    this.entries.delete(id);
    this.bytes -= image.bytes;
    image.dispose();
  }

  clear(): void { for (const id of [...this.entries.keys()]) this.delete(id); }
}
