import { Binary, Database, File, FileCode2, FileCog, FileJson, FileTerminal, FileText, Folder, Image, Palette } from "lucide-react";
import type { ComponentType, CSSProperties } from "react";

interface FileGlyph {
  Icon: ComponentType<{ size?: number | string; className?: string; style?: CSSProperties }>;
  /** A CSS colour token. Language colour is the fastest cue in a long list. */
  color: string;
}

const CODE = "var(--code-function)";
const DATA = "var(--code-number)";
const MARKUP = "var(--code-tag)";

const BY_EXTENSION: Record<string, FileGlyph> = {
  ts: { Icon: FileCode2, color: CODE },
  tsx: { Icon: FileCode2, color: CODE },
  js: { Icon: FileCode2, color: "var(--code-constant)" },
  cjs: { Icon: FileCode2, color: "var(--code-constant)" },
  mjs: { Icon: FileCode2, color: "var(--code-constant)" },
  jsx: { Icon: FileCode2, color: "var(--code-constant)" },
  py: { Icon: FileCode2, color: "var(--code-type)" },
  rb: { Icon: FileCode2, color: MARKUP },
  go: { Icon: FileCode2, color: CODE },
  rs: { Icon: FileCode2, color: "var(--code-escape)" },
  java: { Icon: FileCode2, color: MARKUP },
  c: { Icon: Binary, color: CODE },
  h: { Icon: Binary, color: CODE },
  cc: { Icon: Binary, color: CODE },
  cpp: { Icon: Binary, color: CODE },
  cxx: { Icon: Binary, color: CODE },
  hpp: { Icon: Binary, color: CODE },
  sh: { Icon: FileTerminal, color: "var(--code-string)" },
  bash: { Icon: FileTerminal, color: "var(--code-string)" },
  zsh: { Icon: FileTerminal, color: "var(--code-string)" },
  css: { Icon: Palette, color: "var(--code-keyword)" },
  html: { Icon: FileCode2, color: MARKUP },
  htm: { Icon: FileCode2, color: MARKUP },
  json: { Icon: FileJson, color: DATA },
  yaml: { Icon: FileCog, color: "var(--code-escape)" },
  yml: { Icon: FileCog, color: "var(--code-escape)" },
  toml: { Icon: FileCog, color: "var(--code-escape)" },
  ini: { Icon: FileCog, color: "var(--code-escape)" },
  sql: { Icon: Database, color: "var(--code-type)" },
  md: { Icon: FileText, color: "var(--code-comment)" },
  markdown: { Icon: FileText, color: "var(--code-comment)" },
  txt: { Icon: FileText, color: "var(--code-comment)" },
  log: { Icon: FileText, color: "var(--code-comment)" },
  png: { Icon: Image, color: "var(--code-keyword)" },
  jpg: { Icon: Image, color: "var(--code-keyword)" },
  jpeg: { Icon: Image, color: "var(--code-keyword)" },
  gif: { Icon: Image, color: "var(--code-keyword)" },
  svg: { Icon: Image, color: "var(--code-keyword)" },
  webp: { Icon: Image, color: "var(--code-keyword)" },
};

const FALLBACK: FileGlyph = { Icon: File, color: "var(--muted)" };
const DIRECTORY: FileGlyph = { Icon: Folder, color: "var(--blue)" };

export function fileGlyph(path: string, kind: "file" | "directory" = "file"): FileGlyph {
  if (kind === "directory") return DIRECTORY;
  const name = path.split("/").at(-1) ?? path;
  const extension = name.includes(".") ? name.split(".").pop()?.toLowerCase() ?? "" : "";
  return BY_EXTENSION[extension] ?? FALLBACK;
}

/** One icon for a workspace path, tinted by language. */
export function FileGlyphIcon({ path, kind = "file", size = 15 }: { path: string; kind?: "file" | "directory"; size?: number }) {
  const { Icon, color } = fileGlyph(path, kind);
  return <Icon size={size} className="file-glyph" style={{ color }} />;
}
