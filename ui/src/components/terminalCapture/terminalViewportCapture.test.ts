import type { IBufferCell, IBufferLine, IBufferNamespace } from "@xterm/xterm";
import { describe, expect, it, vi } from "vitest";
import {
  captureTerminalViewportPng,
  renderTerminalViewport,
  snapshotTerminalViewport,
  type XtermViewportTerminal,
} from "./terminalViewportCapture";

interface CellOptions {
  chars?: string;
  width?: number;
  fg?: number;
  bg?: number;
  fgMode?: "default" | "palette" | "rgb";
  bgMode?: "default" | "palette" | "rgb";
  bold?: boolean;
  inverse?: boolean;
}

function fakeCell(options: CellOptions = {}): IBufferCell {
  const fgMode = options.fgMode ?? "default";
  const bgMode = options.bgMode ?? "default";
  return {
    getWidth: () => options.width ?? 1,
    getChars: () => options.chars ?? "",
    getCode: () => 0,
    getFgColorMode: () => 0,
    getBgColorMode: () => 0,
    getFgColor: () => options.fg ?? 0,
    getBgColor: () => options.bg ?? 0,
    isBold: () => Number(options.bold ?? false),
    isItalic: () => 0,
    isDim: () => 0,
    isUnderline: () => 0,
    isBlink: () => 0,
    isInverse: () => Number(options.inverse ?? false),
    isInvisible: () => 0,
    isStrikethrough: () => 0,
    isOverline: () => 0,
    isFgRGB: () => fgMode === "rgb",
    isBgRGB: () => bgMode === "rgb",
    isFgPalette: () => fgMode === "palette",
    isBgPalette: () => bgMode === "palette",
    isFgDefault: () => fgMode === "default",
    isBgDefault: () => bgMode === "default",
    isAttributeDefault: () => false,
  };
}

function fakeLine(cells: readonly (IBufferCell | undefined)[]): IBufferLine {
  return {
    isWrapped: false,
    length: cells.length,
    getCell: (index) => cells[index],
    translateToString: () => cells.map((cell) => cell?.getChars() ?? " ").join(""),
  };
}

function terminalFixture(): XtermViewportTerminal {
  const lines = new Map([
    [3, fakeLine([
      fakeCell({ chars: "A", fg: 2, fgMode: "palette", bold: true }),
      fakeCell({ chars: "界", width: 2, fg: 0x123456, fgMode: "rgb" }),
      fakeCell({ width: 0 }),
      fakeCell({ chars: "!", fg: 1, bg: 4, fgMode: "palette", bgMode: "palette", inverse: true }),
    ])],
    [4, fakeLine([fakeCell({ chars: "$" })])],
  ]);
  const active = {
    type: "alternate" as const,
    cursorY: 1,
    cursorX: 1,
    viewportY: 3,
    baseY: 3,
    length: 5,
    getLine: (row: number) => lines.get(row),
    getNullCell: () => fakeCell(),
  };
  return {
    cols: 4,
    rows: 2,
    buffer: { active, normal: active, alternate: active, onBufferChange: vi.fn() } as unknown as IBufferNamespace,
    options: {
      fontSize: 14,
      lineHeight: 1.2,
      theme: { green: "#008800", brightGreen: "#00ff00", selectionBackground: "#abcdef" },
    },
    getSelectionPosition: () => ({ start: { x: 2, y: 4 }, end: { x: 4, y: 4 } }),
  };
}

describe("terminal viewport capture", () => {
  it("copies ANSI, RGB, inverse, wide-cell, selection, alternate-buffer, and cursor state", () => {
    const snapshot = snapshotTerminalViewport(terminalFixture());
    expect(snapshot.bufferType).toBe("alternate");
    expect(snapshot.viewportY).toBe(3);
    expect(snapshot.cells).toHaveLength(7);
    expect(snapshot.cells[0]).toMatchObject({ chars: "A", foreground: "#00ff00", bold: true });
    expect(snapshot.cells[1]).toMatchObject({ chars: "界", width: 2, foreground: "#123456", selected: true });
    expect(snapshot.cells.find((cell) => cell.chars === "!")).toMatchObject({
      foreground: "#7bbcf2",
      background: "#ff7f86",
    });
    expect(snapshot.cells.find((cell) => cell.cursor)).toMatchObject({ row: 1, col: 1 });
  });

  it("renders at an explicit display scale with deterministic grid dimensions", () => {
    const snapshot = snapshotTerminalViewport(terminalFixture());
    const fillRect = vi.fn();
    const fillText = vi.fn();
    const context = {
      scale: vi.fn(),
      fillRect,
      fillText,
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      stroke: vi.fn(),
      fill: vi.fn(),
      save: vi.fn(),
      restore: vi.fn(),
      setLineDash: vi.fn(),
    } as unknown as CanvasRenderingContext2D;
    const canvas = document.createElement("canvas");
    Object.defineProperty(canvas, "getContext", { value: () => context });
    const rendered = renderTerminalViewport(snapshot, {
      cellWidth: 8,
      cellHeight: 16,
      padding: 4,
      scale: 2,
      createCanvas: () => canvas,
    });
    expect(rendered.width).toBe(80);
    expect(rendered.height).toBe(80);
    expect(rendered.style.width).toBe("40px");
    expect(context.scale).toHaveBeenCalledWith(2, 2);
    expect(fillText).toHaveBeenCalledWith("界", 12, expect.any(Number), 16);
    expect(fillRect).toHaveBeenCalledWith(12, 4, 16, 16);
  });

  it("uses the rendered screen bounds, caps high-DPI output, and falls back when canvas.toBlob is unavailable", async () => {
    const terminal = terminalFixture();
    const root = document.createElement("div");
    const screen = document.createElement("div");
    screen.className = "xterm-screen";
    root.append(screen);
    Object.defineProperty(root, "getBoundingClientRect", {
      value: () => ({ width: 900, height: 400, x: 0, y: 0, top: 0, right: 900, bottom: 400, left: 0, toJSON: () => ({}) }),
    });
    Object.defineProperty(screen, "getBoundingClientRect", {
      value: () => ({ width: 800, height: 320, x: 0, y: 0, top: 0, right: 800, bottom: 320, left: 0, toJSON: () => ({}) }),
    });
    Object.defineProperty(terminal, "element", { value: root });

    const context = {
      scale: vi.fn(),
      fillRect: vi.fn(),
      fillText: vi.fn(),
    } as unknown as CanvasRenderingContext2D;
    const canvas = document.createElement("canvas");
    Object.defineProperty(canvas, "getContext", { value: () => context });
    Object.defineProperty(canvas, "toBlob", { value: undefined });
    Object.defineProperty(canvas, "toDataURL", {
      value: () => "data:image/png;base64,iVBORw0KGgo=",
    });

    const capture = await captureTerminalViewportPng(terminal, {}, {
      scale: 10,
      createCanvas: () => canvas,
    });

    expect(context.scale).toHaveBeenCalledWith(2, 2);
    expect(capture.width).toBe(1_600);
    expect(capture.height).toBe(640);
    expect(capture.logicalWidth).toBe(800);
    expect(capture.logicalHeight).toBe(320);
    expect(capture.blob).toMatchObject({ type: "image/png" });
  });

  it("reports a terminal that has not established its grid yet", () => {
    const terminal = terminalFixture();
    Object.defineProperty(terminal, "cols", { value: 0 });
    expect(() => snapshotTerminalViewport(terminal)).toThrow("not ready to capture");
  });
});
