export const IMAGE_EDIT_RECIPE_VERSION = 1 as const;
export const MAX_IMAGE_EDIT_OPERATIONS = 200;
export const SUPPORTED_IMAGE_MIME_TYPES = ["image/png", "image/jpeg", "image/webp"] as const;

export type SupportedImageMimeType = typeof SUPPORTED_IMAGE_MIME_TYPES[number];

export interface ValidatedImageBlob {
  mimeType: SupportedImageMimeType;
  width: number;
  height: number;
}

function startsWithBytes(value: Uint8Array, signature: readonly number[], offset = 0): boolean {
  return signature.every((byte, index) => value[offset + index] === byte);
}

function uint16be(value: Uint8Array, offset: number): number {
  return (value[offset] << 8) | value[offset + 1];
}

function uint32be(value: Uint8Array, offset: number): number {
  return ((value[offset] * 0x1000000)
    + (value[offset + 1] << 16)
    + (value[offset + 2] << 8)
    + value[offset + 3]);
}

function jpegDimensions(value: Uint8Array): { width: number; height: number } | undefined {
  const startOfFrame = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);
  let offset = 2;
  while (offset + 8 < value.length) {
    while (offset < value.length && value[offset] !== 0xff) offset += 1;
    while (offset < value.length && value[offset] === 0xff) offset += 1;
    if (offset >= value.length) break;
    const marker = value[offset];
    offset += 1;
    if (marker === 0xd8 || marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (marker === 0xd9 || marker === 0xda || offset + 1 >= value.length) break;
    const segmentLength = uint16be(value, offset);
    if (segmentLength < 2 || offset + segmentLength > value.length) break;
    if (startOfFrame.has(marker) && segmentLength >= 7) {
      return { height: uint16be(value, offset + 3), width: uint16be(value, offset + 5) };
    }
    offset += segmentLength;
  }
  return undefined;
}

function webpDimensions(value: Uint8Array): { width: number; height: number } | undefined {
  const chunk = String.fromCharCode(...value.slice(12, 16));
  if (chunk === "VP8X" && value.length >= 30) {
    return {
      width: 1 + value[24] + (value[25] << 8) + (value[26] << 16),
      height: 1 + value[27] + (value[28] << 8) + (value[29] << 16),
    };
  }
  if (chunk === "VP8 " && value.length >= 30
    && startsWithBytes(value, [0x9d, 0x01, 0x2a], 23)) {
    return {
      width: (value[26] | (value[27] << 8)) & 0x3fff,
      height: (value[28] | (value[29] << 8)) & 0x3fff,
    };
  }
  if (chunk === "VP8L" && value.length >= 25 && value[20] === 0x2f) {
    return {
      width: 1 + value[21] + ((value[22] & 0x3f) << 8),
      height: 1 + (value[22] >> 6) + (value[23] << 2) + ((value[24] & 0x0f) << 10),
    };
  }
  return undefined;
}

/** Rejects SVG, mislabeled input, and unverifiable dimensions before browser decoding. */
export async function validateSupportedImageBlob(blob: Blob): Promise<ValidatedImageBlob> {
  const mimeType = blob.type.toLowerCase();
  if (!(SUPPORTED_IMAGE_MIME_TYPES as readonly string[]).includes(mimeType)) {
    throw new Error("Only PNG, JPEG, and WebP images can be edited.");
  }
  const header = new Uint8Array(await blob.slice(0, Math.min(blob.size, 1_048_576)).arrayBuffer());
  const valid = mimeType === "image/png"
    ? startsWithBytes(header, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
      && startsWithBytes(header, [0x49, 0x48, 0x44, 0x52], 12)
    : mimeType === "image/jpeg"
      ? startsWithBytes(header, [0xff, 0xd8, 0xff])
      : startsWithBytes(header, [0x52, 0x49, 0x46, 0x46])
        && startsWithBytes(header, [0x57, 0x45, 0x42, 0x50], 8);
  if (!valid) throw new Error("The image contents do not match the declared raster format.");
  const dimensions = mimeType === "image/png"
    ? (header.length >= 24 ? { width: uint32be(header, 16), height: uint32be(header, 20) } : undefined)
    : mimeType === "image/jpeg"
      ? jpegDimensions(header)
      : webpDimensions(header);
  if (!dimensions?.width || !dimensions.height) {
    throw new Error("The raster dimensions could not be verified safely.");
  }
  return { mimeType: mimeType as SupportedImageMimeType, ...dimensions };
}

export interface ImagePoint {
  x: number;
  y: number;
}

export interface ImageRect extends ImagePoint {
  width: number;
  height: number;
}

interface BaseOperation {
  id: string;
}

export interface CropOperation extends BaseOperation {
  type: "crop";
  rect: ImageRect;
}

export interface RectangleOperation extends BaseOperation {
  type: "rectangle";
  rect: ImageRect;
  color: string;
  thickness: number;
}

export interface ArrowOperation extends BaseOperation {
  type: "arrow";
  from: ImagePoint;
  to: ImagePoint;
  color: string;
  thickness: number;
}

export interface BlurOperation extends BaseOperation {
  type: "blur";
  rect: ImageRect;
  radius: number;
}

export interface RedactOperation extends BaseOperation {
  type: "redact";
  rect: ImageRect;
  color: string;
}

export interface TextOperation extends BaseOperation {
  type: "text";
  at: ImagePoint;
  text: string;
  color: string;
  fontSize: number;
}

export type ImageEditOperation =
  | CropOperation
  | RectangleOperation
  | ArrowOperation
  | BlurOperation
  | RedactOperation
  | TextOperation;

export interface ImageEditHistory {
  readonly operations: readonly ImageEditOperation[];
  readonly undone: readonly ImageEditOperation[];
}

export type ImageEditRecipe = Readonly<{
  version: typeof IMAGE_EDIT_RECIPE_VERSION;
  sourceWidth: number;
  sourceHeight: number;
  outputWidth: number;
  outputHeight: number;
  operations: readonly ImageEditOperation[];
}>;

export type ImageCanvasFactory = () => HTMLCanvasElement;

const HEX_COLOR = /^#[0-9a-f]{6}$/i;

function finite(value: number, fallback = 0): number {
  return Number.isFinite(value) ? value : fallback;
}

function integer(value: number): number {
  return Math.round(finite(value));
}

export function clampImagePoint(point: ImagePoint, width: number, height: number): ImagePoint {
  return {
    x: Math.max(0, Math.min(integer(width), integer(point.x))),
    y: Math.max(0, Math.min(integer(height), integer(point.y))),
  };
}

/** Normalizes reverse drags and clips them to the current image bounds. */
export function normalizeImageRect(
  start: ImagePoint,
  end: ImagePoint,
  width: number,
  height: number,
): ImageRect {
  const a = clampImagePoint(start, width, height);
  const b = clampImagePoint(end, width, height);
  return {
    x: Math.min(a.x, b.x),
    y: Math.min(a.y, b.y),
    width: Math.abs(b.x - a.x),
    height: Math.abs(b.y - a.y),
  };
}

export function createImageEditHistory(
  operations: readonly ImageEditOperation[] = [],
): ImageEditHistory {
  return { operations: operations.map(cloneImageEditOperation), undone: [] };
}

export function appendImageEdit(
  history: ImageEditHistory,
  operation: ImageEditOperation,
  limit = MAX_IMAGE_EDIT_OPERATIONS,
): ImageEditHistory {
  if (history.operations.length >= limit) throw new Error(`An image edit is limited to ${limit} operations.`);
  return {
    operations: [...history.operations, cloneImageEditOperation(operation)],
    undone: [],
  };
}

export function undoImageEdit(history: ImageEditHistory): ImageEditHistory {
  const operation = history.operations.at(-1);
  if (!operation) return history;
  return {
    operations: history.operations.slice(0, -1),
    undone: [cloneImageEditOperation(operation), ...history.undone],
  };
}

export function redoImageEdit(history: ImageEditHistory): ImageEditHistory {
  const operation = history.undone[0];
  if (!operation) return history;
  return {
    operations: [...history.operations, cloneImageEditOperation(operation)],
    undone: history.undone.slice(1),
  };
}

export function cloneImageEditOperation(operation: ImageEditOperation): ImageEditOperation {
  switch (operation.type) {
    case "crop": return { ...operation, rect: { ...operation.rect } };
    case "rectangle": return { ...operation, rect: { ...operation.rect } };
    case "arrow": return { ...operation, from: { ...operation.from }, to: { ...operation.to } };
    case "blur": return { ...operation, rect: { ...operation.rect } };
    case "redact": return { ...operation, rect: { ...operation.rect } };
    case "text": return { ...operation, at: { ...operation.at } };
  }
}

function validRect(rect: ImageRect, width: number, height: number): boolean {
  return Number.isInteger(rect.x)
    && Number.isInteger(rect.y)
    && Number.isInteger(rect.width)
    && Number.isInteger(rect.height)
    && rect.x >= 0
    && rect.y >= 0
    && rect.width > 0
    && rect.height > 0
    && rect.x + rect.width <= width
    && rect.y + rect.height <= height;
}

function validPoint(point: ImagePoint, width: number, height: number): boolean {
  return Number.isInteger(point.x)
    && Number.isInteger(point.y)
    && point.x >= 0
    && point.y >= 0
    && point.x <= width
    && point.y <= height;
}

function assertColor(color: string): void {
  if (!HEX_COLOR.test(color)) throw new Error("Image annotation colors must be opaque six-digit hex values.");
}

function assertThickness(thickness: number): void {
  if (!Number.isInteger(thickness) || thickness < 1 || thickness > 64) {
    throw new Error("Image annotation thickness must be an integer from 1 to 64.");
  }
}

/** Validates a recipe in operation order, including coordinates after each crop. */
export function imageDimensionsAfterOperations(
  sourceWidth: number,
  sourceHeight: number,
  operations: readonly ImageEditOperation[],
): { width: number; height: number } {
  if (!Number.isInteger(sourceWidth) || !Number.isInteger(sourceHeight) || sourceWidth < 1 || sourceHeight < 1) {
    throw new Error("The source image dimensions are invalid.");
  }
  if (operations.length > MAX_IMAGE_EDIT_OPERATIONS) throw new Error("The image edit recipe contains too many operations.");
  let width = sourceWidth;
  let height = sourceHeight;
  for (const operation of operations) {
    if (!operation.id || operation.id.length > 128) throw new Error("Every image operation needs a bounded identifier.");
    switch (operation.type) {
      case "crop":
        if (!validRect(operation.rect, width, height)) throw new Error("A crop lies outside the current image.");
        width = operation.rect.width;
        height = operation.rect.height;
        break;
      case "rectangle":
        if (!validRect(operation.rect, width, height)) throw new Error("A rectangle lies outside the current image.");
        assertColor(operation.color);
        assertThickness(operation.thickness);
        break;
      case "arrow":
        if (!validPoint(operation.from, width, height) || !validPoint(operation.to, width, height)) {
          throw new Error("An arrow lies outside the current image.");
        }
        assertColor(operation.color);
        assertThickness(operation.thickness);
        break;
      case "blur":
        if (!validRect(operation.rect, width, height)) throw new Error("A blur lies outside the current image.");
        if (!Number.isInteger(operation.radius) || operation.radius < 1 || operation.radius > 64) {
          throw new Error("Blur radius must be an integer from 1 to 64.");
        }
        break;
      case "redact":
        if (!validRect(operation.rect, width, height)) throw new Error("A redaction lies outside the current image.");
        assertColor(operation.color);
        break;
      case "text":
        if (!validPoint(operation.at, width, height)) throw new Error("Text lies outside the current image.");
        if (!operation.text.length || operation.text.length > 1_000) throw new Error("Image text must contain 1 to 1,000 characters.");
        assertColor(operation.color);
        if (!Number.isInteger(operation.fontSize) || operation.fontSize < 8 || operation.fontSize > 256) {
          throw new Error("Image text size must be an integer from 8 to 256 pixels.");
        }
        break;
    }
  }
  return { width, height };
}

function contextFor(canvas: HTMLCanvasElement): CanvasRenderingContext2D {
  const context = canvas.getContext("2d");
  if (!context) throw new Error("Canvas 2D rendering is unavailable.");
  return context;
}

function drawArrow(context: CanvasRenderingContext2D, operation: ArrowOperation): void {
  const dx = operation.to.x - operation.from.x;
  const dy = operation.to.y - operation.from.y;
  const angle = Math.atan2(dy, dx);
  const head = Math.max(10, operation.thickness * 4);
  context.save();
  context.strokeStyle = operation.color;
  context.fillStyle = operation.color;
  context.lineWidth = operation.thickness;
  context.lineCap = "round";
  context.lineJoin = "round";
  context.beginPath();
  context.moveTo(operation.from.x, operation.from.y);
  context.lineTo(operation.to.x, operation.to.y);
  context.stroke();
  context.beginPath();
  context.moveTo(operation.to.x, operation.to.y);
  context.lineTo(operation.to.x - (head * Math.cos(angle - Math.PI / 6)), operation.to.y - (head * Math.sin(angle - Math.PI / 6)));
  context.lineTo(operation.to.x - (head * Math.cos(angle + Math.PI / 6)), operation.to.y - (head * Math.sin(angle + Math.PI / 6)));
  context.closePath();
  context.fill();
  context.restore();
}

function drawBlur(
  canvas: HTMLCanvasElement,
  context: CanvasRenderingContext2D,
  operation: BlurOperation,
  createCanvas: ImageCanvasFactory,
): void {
  const copy = createCanvas();
  copy.width = canvas.width;
  copy.height = canvas.height;
  contextFor(copy).drawImage(canvas, 0, 0);
  context.save();
  context.beginPath();
  context.rect(operation.rect.x, operation.rect.y, operation.rect.width, operation.rect.height);
  context.clip();
  context.filter = `blur(${operation.radius}px)`;
  context.drawImage(copy, 0, 0);
  context.filter = "none";
  context.restore();
}

function drawText(context: CanvasRenderingContext2D, operation: TextOperation): void {
  context.save();
  context.fillStyle = operation.color;
  context.font = `600 ${operation.fontSize}px sans-serif`;
  context.textBaseline = "top";
  const lineHeight = operation.fontSize * 1.2;
  operation.text.split("\n").forEach((line, index) => {
    context.fillText(line, operation.at.x, operation.at.y + (index * lineHeight));
  });
  context.restore();
}

/** Replays an immutable edit list against the original image into a new canvas. */
export function renderImageEdits(
  source: CanvasImageSource,
  sourceWidth: number,
  sourceHeight: number,
  operations: readonly ImageEditOperation[],
  createCanvas: ImageCanvasFactory = () => document.createElement("canvas"),
): HTMLCanvasElement {
  imageDimensionsAfterOperations(sourceWidth, sourceHeight, operations);
  let canvas = createCanvas();
  canvas.width = sourceWidth;
  canvas.height = sourceHeight;
  contextFor(canvas).drawImage(source, 0, 0, sourceWidth, sourceHeight);

  for (const operation of operations) {
    if (operation.type === "crop") {
      const cropped = createCanvas();
      cropped.width = operation.rect.width;
      cropped.height = operation.rect.height;
      contextFor(cropped).drawImage(
        canvas,
        operation.rect.x,
        operation.rect.y,
        operation.rect.width,
        operation.rect.height,
        0,
        0,
        operation.rect.width,
        operation.rect.height,
      );
      canvas = cropped;
      continue;
    }
    const context = contextFor(canvas);
    switch (operation.type) {
      case "rectangle":
        context.save();
        context.strokeStyle = operation.color;
        context.lineWidth = operation.thickness;
        context.strokeRect(operation.rect.x, operation.rect.y, operation.rect.width, operation.rect.height);
        context.restore();
        break;
      case "arrow": drawArrow(context, operation); break;
      case "blur": drawBlur(canvas, context, operation, createCanvas); break;
      case "redact":
        context.save();
        context.fillStyle = operation.color;
        context.fillRect(operation.rect.x, operation.rect.y, operation.rect.width, operation.rect.height);
        context.restore();
        break;
      case "text": drawText(context, operation); break;
    }
  }
  return canvas;
}

export function createImageEditRecipe(
  sourceWidth: number,
  sourceHeight: number,
  operations: readonly ImageEditOperation[],
): ImageEditRecipe {
  const output = imageDimensionsAfterOperations(sourceWidth, sourceHeight, operations);
  return {
    version: IMAGE_EDIT_RECIPE_VERSION,
    sourceWidth,
    sourceHeight,
    outputWidth: output.width,
    outputHeight: output.height,
    operations: operations.map(cloneImageEditOperation),
  };
}
