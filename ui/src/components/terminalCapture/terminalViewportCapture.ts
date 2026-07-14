import type { IBufferCell, IBufferNamespace, IBufferRange, ITheme } from "@xterm/xterm";

const ANSI_THEME_KEYS = [
  "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
  "brightBlack", "brightRed", "brightGreen", "brightYellow", "brightBlue", "brightMagenta", "brightCyan", "brightWhite",
] as const;

const DEFAULT_ANSI = [
  "#071017", "#ff7f86", "#54d6a3", "#e3c877", "#7bbcf2", "#d596e8", "#62d2df", "#d9e5e9",
  "#53656d", "#ff9da3", "#7ce9bd", "#f0d990", "#a1d1f7", "#e8b3f2", "#8ce4ec", "#ffffff",
] as const;

export interface XtermViewportTerminal {
  readonly cols: number;
  readonly rows: number;
  readonly buffer: IBufferNamespace;
  readonly element?: HTMLElement;
  readonly options: {
    theme?: ITheme;
    fontFamily?: string;
    fontSize?: number;
    fontWeight?: string | number;
    fontWeightBold?: string | number;
    lineHeight?: number;
    cursorStyle?: "block" | "underline" | "bar";
    drawBoldTextInBrightColors?: boolean;
  };
  getSelectionPosition(): IBufferRange | undefined;
}

export interface TerminalCaptureTheme {
  foreground: string;
  background: string;
  cursor: string;
  cursorAccent: string;
  selectionBackground: string;
  selectionForeground?: string;
  ansi: readonly string[];
}

export interface TerminalViewportCell {
  col: number;
  row: number;
  chars: string;
  width: number;
  foreground: string;
  background: string;
  bold: boolean;
  italic: boolean;
  dim: boolean;
  underline: boolean;
  strikethrough: boolean;
  overline: boolean;
  invisible: boolean;
  selected: boolean;
  cursor: boolean;
}

export interface TerminalViewportSnapshot {
  cols: number;
  rows: number;
  viewportY: number;
  bufferType: "normal" | "alternate";
  cells: readonly TerminalViewportCell[];
  theme: TerminalCaptureTheme;
  fontFamily: string;
  fontSize: number;
  fontWeight: string | number;
  fontWeightBold: string | number;
  lineHeight: number;
  cursorStyle: "block" | "underline" | "bar";
}

export interface CaptureTerminalViewportOptions {
  theme?: ITheme;
  includeCursor?: boolean;
}

export interface TerminalViewportRenderOptions {
  /** Logical CSS pixels per terminal column. */
  cellWidth?: number;
  /** Logical CSS pixels per terminal row. */
  cellHeight?: number;
  /** PNG backing-store pixels per logical pixel. */
  scale?: number;
  padding?: number;
  createCanvas?: () => HTMLCanvasElement;
}

export interface TerminalPngCapture {
  blob: Blob;
  width: number;
  height: number;
  logicalWidth: number;
  logicalHeight: number;
  snapshot: TerminalViewportSnapshot;
}

function captureTheme(theme: ITheme = {}): TerminalCaptureTheme {
  const ansi = ANSI_THEME_KEYS.map((key, index) => theme[key] ?? DEFAULT_ANSI[index]);
  if (theme.extendedAnsi) ansi.push(...theme.extendedAnsi.slice(0, 240));
  return {
    foreground: theme.foreground ?? "#d9e5e9",
    background: theme.background ?? "#071017",
    cursor: theme.cursor ?? "#54d6a3",
    cursorAccent: theme.cursorAccent ?? "#071017",
    selectionBackground: theme.selectionBackground ?? "#245f55",
    selectionForeground: theme.selectionForeground,
    ansi,
  };
}

function rgbColor(value: number): string {
  return `#${(value & 0xffffff).toString(16).padStart(6, "0")}`;
}

function paletteColor(index: number, theme: TerminalCaptureTheme): string {
  if (theme.ansi[index]) return theme.ansi[index];
  if (index >= 16 && index <= 231) {
    const offset = index - 16;
    const steps = [0, 95, 135, 175, 215, 255];
    return rgbColor((steps[Math.floor(offset / 36)] << 16)
      | (steps[Math.floor(offset / 6) % 6] << 8)
      | steps[offset % 6]);
  }
  if (index >= 232 && index <= 255) {
    const channel = 8 + ((index - 232) * 10);
    return rgbColor((channel << 16) | (channel << 8) | channel);
  }
  return theme.foreground;
}

function foregroundFor(
  cell: IBufferCell,
  theme: TerminalCaptureTheme,
  drawBoldBright: boolean,
): string {
  if (cell.isFgRGB()) return rgbColor(cell.getFgColor());
  if (cell.isFgPalette()) {
    let index = cell.getFgColor();
    if (drawBoldBright && cell.isBold() && index >= 0 && index < 8) index += 8;
    return paletteColor(index, theme);
  }
  return theme.foreground;
}

function backgroundFor(cell: IBufferCell, theme: TerminalCaptureTheme): string {
  if (cell.isBgRGB()) return rgbColor(cell.getBgColor());
  if (cell.isBgPalette()) return paletteColor(cell.getBgColor(), theme);
  return theme.background;
}

function comparePosition(a: { x: number; y: number }, b: { x: number; y: number }): number {
  return a.y === b.y ? a.x - b.x : a.y - b.y;
}

function selectedAt(range: IBufferRange | undefined, col: number, absoluteRow: number, width: number): boolean {
  if (!range) return false;
  const start = comparePosition(range.start, range.end) <= 0 ? range.start : range.end;
  const end = start === range.start ? range.end : range.start;
  for (let offset = 0; offset < Math.max(1, width); offset += 1) {
    const position = { x: col + offset + 1, y: absoluteRow + 1 };
    if (comparePosition(position, start) >= 0 && comparePosition(position, end) < 0) return true;
  }
  return false;
}

function emptyCell(
  col: number,
  row: number,
  absoluteRow: number,
  theme: TerminalCaptureTheme,
  selection: IBufferRange | undefined,
  cursor: boolean,
): TerminalViewportCell {
  return {
    col,
    row,
    chars: "",
    width: 1,
    foreground: theme.foreground,
    background: theme.background,
    bold: false,
    italic: false,
    dim: false,
    underline: false,
    strikethrough: false,
    overline: false,
    invisible: false,
    selected: selectedAt(selection, col, absoluteRow, 1),
    cursor,
  };
}

/** Copies the visible xterm buffer immediately so later terminal writes cannot mutate the capture. */
export function snapshotTerminalViewport(
  terminal: XtermViewportTerminal,
  options: CaptureTerminalViewportOptions = {},
): TerminalViewportSnapshot {
  const buffer = terminal.buffer.active;
  const theme = captureTheme({ ...terminal.options.theme, ...options.theme });
  const cells: TerminalViewportCell[] = [];
  const selection = terminal.getSelectionPosition();
  const cursorAbsoluteRow = buffer.baseY + buffer.cursorY;
  const includeCursor = options.includeCursor ?? true;
  const drawBoldBright = terminal.options.drawBoldTextInBrightColors ?? true;

  for (let row = 0; row < terminal.rows; row += 1) {
    const absoluteRow = buffer.viewportY + row;
    const line = buffer.getLine(absoluteRow);
    for (let col = 0; col < terminal.cols; col += 1) {
      const cursor = includeCursor && absoluteRow === cursorAbsoluteRow && col === buffer.cursorX;
      const cell = line?.getCell(col);
      if (!cell) {
        cells.push(emptyCell(col, row, absoluteRow, theme, selection, cursor));
        continue;
      }
      const width = cell.getWidth();
      if (width === 0) continue;
      let foreground = foregroundFor(cell, theme, drawBoldBright);
      let background = backgroundFor(cell, theme);
      if (cell.isInverse()) [foreground, background] = [background, foreground];
      cells.push({
        col,
        row,
        chars: cell.getChars(),
        width: Math.max(1, width),
        foreground,
        background,
        bold: Boolean(cell.isBold()),
        italic: Boolean(cell.isItalic()),
        dim: Boolean(cell.isDim()),
        underline: Boolean(cell.isUnderline()),
        strikethrough: Boolean(cell.isStrikethrough()),
        overline: Boolean(cell.isOverline()),
        invisible: Boolean(cell.isInvisible()),
        selected: selectedAt(selection, col, absoluteRow, width),
        cursor,
      });
    }
  }
  return {
    cols: terminal.cols,
    rows: terminal.rows,
    viewportY: buffer.viewportY,
    bufferType: buffer.type,
    cells,
    theme,
    fontFamily: terminal.options.fontFamily ?? '"Noto Sans Mono", "SFMono-Regular", Consolas, monospace',
    fontSize: terminal.options.fontSize ?? 13,
    fontWeight: terminal.options.fontWeight ?? "normal",
    fontWeightBold: terminal.options.fontWeightBold ?? "bold",
    lineHeight: terminal.options.lineHeight ?? 1.25,
    cursorStyle: terminal.options.cursorStyle ?? "block",
  };
}

function fontFor(cell: TerminalViewportCell, snapshot: TerminalViewportSnapshot): string {
  const style = cell.italic ? "italic" : "normal";
  const weight = cell.bold ? snapshot.fontWeightBold : snapshot.fontWeight;
  return `${style} ${weight} ${snapshot.fontSize}px ${snapshot.fontFamily}`;
}

function drawDecoration(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  cellHeight: number,
  cell: TerminalViewportCell,
  foreground: string,
) {
  context.fillStyle = foreground;
  const stroke = Math.max(1, Math.round(cellHeight / 16));
  if (cell.underline) context.fillRect(x, y + cellHeight - stroke - 1, width, stroke);
  if (cell.strikethrough) context.fillRect(x, y + (cellHeight / 2), width, stroke);
  if (cell.overline) context.fillRect(x, y + 1, width, stroke);
}

/** Draws a terminal snapshot without reading xterm's renderer DOM or private layers. */
export function renderTerminalViewport(
  snapshot: TerminalViewportSnapshot,
  options: TerminalViewportRenderOptions = {},
): HTMLCanvasElement {
  const scale = Math.max(0.25, options.scale ?? globalThis.devicePixelRatio ?? 1);
  const cellWidth = Math.max(1, options.cellWidth ?? Math.ceil(snapshot.fontSize * 0.62));
  const cellHeight = Math.max(1, options.cellHeight ?? Math.ceil(snapshot.fontSize * snapshot.lineHeight));
  const padding = Math.max(0, options.padding ?? 0);
  const logicalWidth = (snapshot.cols * cellWidth) + (padding * 2);
  const logicalHeight = (snapshot.rows * cellHeight) + (padding * 2);
  const canvas = options.createCanvas?.() ?? document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(logicalWidth * scale));
  canvas.height = Math.max(1, Math.round(logicalHeight * scale));
  canvas.style.width = `${logicalWidth}px`;
  canvas.style.height = `${logicalHeight}px`;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas 2D rendering is unavailable.");
  context.scale(scale, scale);
  context.fillStyle = snapshot.theme.background;
  context.fillRect(0, 0, logicalWidth, logicalHeight);
  context.textBaseline = "alphabetic";

  for (const cell of snapshot.cells) {
    const x = padding + (cell.col * cellWidth);
    const y = padding + (cell.row * cellHeight);
    const width = cell.width * cellWidth;
    context.globalAlpha = 1;
    context.fillStyle = cell.selected ? snapshot.theme.selectionBackground : cell.background;
    context.fillRect(x, y, width, cellHeight);

    let foreground = cell.selected && snapshot.theme.selectionForeground
      ? snapshot.theme.selectionForeground
      : cell.foreground;
    if (cell.cursor && snapshot.cursorStyle === "block") {
      context.fillStyle = snapshot.theme.cursor;
      context.fillRect(x, y, width, cellHeight);
      foreground = snapshot.theme.cursorAccent;
    }
    if (!cell.invisible && cell.chars) {
      context.font = fontFor(cell, snapshot);
      context.fillStyle = foreground;
      context.globalAlpha = cell.dim ? 0.55 : 1;
      const topOffset = Math.max(0, (cellHeight - snapshot.fontSize) / 2);
      context.fillText(cell.chars, x, y + topOffset + snapshot.fontSize, width);
      context.globalAlpha = 1;
      drawDecoration(context, x, y, width, cellHeight, cell, foreground);
    }
    if (cell.cursor && snapshot.cursorStyle !== "block") {
      context.fillStyle = snapshot.theme.cursor;
      if (snapshot.cursorStyle === "bar") context.fillRect(x, y, Math.max(1, cellWidth / 8), cellHeight);
      else context.fillRect(x, y + cellHeight - Math.max(1, cellHeight / 8), width, Math.max(1, cellHeight / 8));
    }
  }
  context.globalAlpha = 1;
  return canvas;
}

function pngBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("The terminal viewport could not be encoded as PNG."));
    }, "image/png");
  });
}

/** Captures the public xterm buffer and encodes it as a scaled PNG. */
export async function captureTerminalViewportPng(
  terminal: XtermViewportTerminal,
  captureOptions: CaptureTerminalViewportOptions = {},
  renderOptions: TerminalViewportRenderOptions = {},
): Promise<TerminalPngCapture> {
  const snapshot = snapshotTerminalViewport(terminal, captureOptions);
  await document.fonts?.ready;
  const elementRect = terminal.element?.getBoundingClientRect();
  const effectiveRenderOptions = {
    cellWidth: renderOptions.cellWidth ?? (elementRect?.width ? elementRect.width / terminal.cols : undefined),
    cellHeight: renderOptions.cellHeight ?? (elementRect?.height ? elementRect.height / terminal.rows : undefined),
    ...renderOptions,
  };
  const canvas = renderTerminalViewport(snapshot, effectiveRenderOptions);
  const scale = Math.max(0.25, effectiveRenderOptions.scale ?? globalThis.devicePixelRatio ?? 1);
  return {
    blob: await pngBlob(canvas),
    width: canvas.width,
    height: canvas.height,
    logicalWidth: canvas.width / scale,
    logicalHeight: canvas.height / scale,
    snapshot,
  };
}
