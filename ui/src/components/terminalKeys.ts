/** Keys a phone keyboard lacks, as the byte sequences a shell expects. */
export const TERMINAL_KEYS = [
  { id: "esc", label: "esc", name: "Escape", sequence: "\x1b" },
  { id: "tab", label: "tab", name: "Tab", sequence: "\t" },
  { id: "ctrl", label: "ctrl", name: "Control", sequence: "" },
  { id: "up", label: "↑", name: "Up arrow", sequence: "\x1b[A" },
  { id: "down", label: "↓", name: "Down arrow", sequence: "\x1b[B" },
  { id: "left", label: "←", name: "Left arrow", sequence: "\x1b[D" },
  { id: "right", label: "→", name: "Right arrow", sequence: "\x1b[C" },
  { id: "pipe", label: "|", name: "Pipe", sequence: "|" },
  { id: "tilde", label: "~", name: "Tilde", sequence: "~" },
  { id: "slash", label: "/", name: "Slash", sequence: "/" },
  { id: "dash", label: "-", name: "Dash", sequence: "-" },
] as const;

export type TerminalKeyId = typeof TERMINAL_KEYS[number]["id"];

/**
 * Applies a latched Control modifier to typed input. Letters and the usual
 * punctuation map to their C0 control codes (Ctrl+C → 0x03); anything else is
 * sent unchanged so a stray latch never swallows input.
 */
export function withControl(data: string): string {
  if (data.length !== 1) return data;
  const code = data.toUpperCase().charCodeAt(0);
  if (code >= 64 && code <= 95) return String.fromCharCode(code - 64);
  if (data === " " || data === "2") return "\x00";
  if (data === "/") return "\x1f";
  return data;
}
