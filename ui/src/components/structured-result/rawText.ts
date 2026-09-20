/**
 * The raw view's text, printed here rather than by `JSON.stringify`, so every
 * line can be tied back to the path that produced it. The output matches
 * `JSON.stringify(value, null, 2)` exactly: this is a printer, not a
 * transformation, and the values it prints are the published ones.
 */

import { joinPath } from "./normalize";

export interface RawDocument {
  lines: string[];
  /** Path to the line on which that value starts. */
  lineForPath: Map<string, number>;
  truncated: boolean;
}

/** Lines printed before the view stops and says the text was cut. */
export const MAX_RAW_LINES = 200_000;

function encodeScalar(value: unknown): string {
  const encoded = JSON.stringify(value);
  // `undefined`, functions and symbols have no JSON form; an array prints them
  // as null, which is what `JSON.stringify` does.
  return encoded === undefined ? "null" : encoded;
}

function omitted(value: unknown): boolean {
  return value === undefined || typeof value === "function" || typeof value === "symbol";
}

export function renderRaw(value: unknown): RawDocument {
  const lines: string[] = [];
  const lineForPath = new Map<string, number>();
  let truncated = false;

  const push = (text: string): boolean => {
    if (lines.length >= MAX_RAW_LINES) {
      truncated = true;
      return false;
    }
    lines.push(text);
    return true;
  };

  const emit = (
    current: unknown,
    path: string,
    indent: string,
    prefix: string,
    suffix: string,
    seen: readonly object[],
  ): void => {
    if (truncated) return;
    lineForPath.set(path, lines.length);
    const container = current !== null && typeof current === "object";
    if (container && seen.includes(current as object)) {
      push(`${indent}${prefix}"[circular reference]"${suffix}`);
      return;
    }
    if (Array.isArray(current)) {
      if (current.length === 0) {
        push(`${indent}${prefix}[]${suffix}`);
        return;
      }
      if (!push(`${indent}${prefix}[`)) return;
      const nested = [...seen, current as object];
      current.forEach((item, index) => {
        emit(omitted(item) ? null : item, joinPath(path, index), `${indent}  `, "", index === current.length - 1 ? "" : ",", nested);
      });
      push(`${indent}]${suffix}`);
      return;
    }
    if (container) {
      const entries = Object.entries(current as Record<string, unknown>).filter((entry) => !omitted(entry[1]));
      if (entries.length === 0) {
        push(`${indent}${prefix}{}${suffix}`);
        return;
      }
      if (!push(`${indent}${prefix}{`)) return;
      const nested = [...seen, current as object];
      entries.forEach(([key, item], index) => {
        emit(item, joinPath(path, key), `${indent}  `, `${JSON.stringify(key)}: `, index === entries.length - 1 ? "" : ",", nested);
      });
      push(`${indent}}${suffix}`);
      return;
    }
    push(`${indent}${prefix}${encodeScalar(current)}${suffix}`);
  };

  emit(value, "$", "", "", "", []);
  return { lines, lineForPath, truncated };
}
