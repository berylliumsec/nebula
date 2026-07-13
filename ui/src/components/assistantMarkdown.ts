import type { ExecutionLanguage } from "../api/types";

export const EXECUTION_LANGUAGE_ALIASES: Record<string, ExecutionLanguage> = {
  bash: "bash",
  shell: "bash",
  sh: "sh",
  python: "python",
  python3: "python",
  py: "python",
};

export interface ExactFence {
  ordinal: number;
  declaredLanguage: string;
  canonicalLanguage?: ExecutionLanguage;
  source: string;
  openStart: number;
  sourceStart: number;
  sourceEnd: number;
  closeEnd: number;
}

export interface FenceParseResult {
  blocks: ExactFence[];
  unmatchedStart?: number;
}

export function parseExactFences(markdown: string): FenceParseResult {
  const blocks: ExactFence[] = [];
  let cursor = 0;
  while (cursor < markdown.length) {
    const lineEnd = markdown.indexOf("\n", cursor);
    const lineStop = lineEnd < 0 ? markdown.length : lineEnd + 1;
    const line = markdown.slice(cursor, lineStop);
    const opening = /^( {0,3})(`{3,}|~{3,})([^\r\n]*)(\r?\n|$)/.exec(line);
    if (!opening) {
      cursor = lineStop;
      continue;
    }
    const fence = opening[2];
    const marker = fence[0];
    const declared = opening[3].trim().split(/\s+/, 1)[0]?.toLowerCase() ?? "";
    const sourceStart = cursor + opening[0].length;
    let search = sourceStart;
    let closeStart: number | undefined;
    let closeEnd: number | undefined;
    const escapedMarker = marker === "`" ? "\\`" : "~";
    const closing = new RegExp(`^ {0,3}${escapedMarker}{${fence.length},}[ \\t]*(?:\\r?\\n|$)`);
    while (search <= markdown.length) {
      const candidateEnd = markdown.indexOf("\n", search);
      const candidateStop = candidateEnd < 0 ? markdown.length : candidateEnd + 1;
      const candidate = markdown.slice(search, candidateStop);
      if (closing.test(candidate)) {
        closeStart = search;
        closeEnd = candidateStop;
        break;
      }
      if (candidateEnd < 0) break;
      search = candidateStop;
    }
    if (closeStart === undefined || closeEnd === undefined) {
      return { blocks, unmatchedStart: cursor };
    }
    blocks.push({
      ordinal: blocks.length,
      declaredLanguage: declared,
      canonicalLanguage: EXECUTION_LANGUAGE_ALIASES[declared],
      source: markdown.slice(sourceStart, closeStart),
      openStart: cursor,
      sourceStart,
      sourceEnd: closeStart,
      closeEnd,
    });
    cursor = closeEnd;
  }
  return { blocks };
}

export async function sha256(value: string): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function utf8Length(value: string): number {
  return new TextEncoder().encode(value).length;
}

const bidi = new Set([0x061c, 0x200e, 0x200f, 0x202a, 0x202b, 0x202c, 0x202d, 0x202e, 0x2066, 0x2067, 0x2068, 0x2069]);

export function visibleSource(value: string): string {
  return [...value].map((character) => {
    const code = character.codePointAt(0) ?? 0;
    if (character === "\n" || character === "\t") return character;
    if (character === "\r") return "<CR>";
    if (bidi.has(code)) return `<U+${code.toString(16).toUpperCase().padStart(4, "0")}>`;
    if (code < 32 || code === 127) return `<0x${code.toString(16).toUpperCase().padStart(2, "0")}>`;
    return character;
  }).join("");
}
