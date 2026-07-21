import { describe, expect, it, vi } from "vitest";
import {
  appendImageEdit,
  createImageEditHistory,
  createImageEditRecipe,
  imageDimensionsAfterOperations,
  normalizeImageRect,
  redoImageEdit,
  renderImageEdits,
  undoImageEdit,
  validateSupportedImageBlob,
  type ImageEditOperation,
} from "./imageEditModel";

const crop: ImageEditOperation = {
  id: "crop-1",
  type: "crop",
  rect: { x: 10, y: 20, width: 100, height: 80 },
};

const rectangle: ImageEditOperation = {
  id: "rectangle-1",
  type: "rectangle",
  rect: { x: 2, y: 3, width: 20, height: 10 },
  color: "#ff0000",
  thickness: 3,
};

describe("image edit operation model", () => {
  it("accepts supported raster signatures and rejects SVG or disguised content", async () => {
    const png = new Blob([
      new Uint8Array([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52,
        0, 0, 0, 64, 0, 0, 0, 32,
      ]),
    ], { type: "image/png" });
    await expect(validateSupportedImageBlob(png)).resolves.toEqual({ mimeType: "image/png", width: 64, height: 32 });

    const jpegBytes = new Uint8Array(21);
    jpegBytes.set([0xff, 0xd8, 0xff, 0xc0, 0, 17, 8, 0, 32, 0, 64]);
    await expect(validateSupportedImageBlob(new Blob([jpegBytes], { type: "image/jpeg" }))).resolves.toEqual({
      mimeType: "image/jpeg", width: 64, height: 32,
    });

    const webpBytes = new Uint8Array(30);
    webpBytes.set([0x52, 0x49, 0x46, 0x46], 0);
    webpBytes.set([0x57, 0x45, 0x42, 0x50], 8);
    webpBytes.set([0x56, 0x50, 0x38, 0x58], 12);
    webpBytes[24] = 63;
    webpBytes[27] = 31;
    await expect(validateSupportedImageBlob(new Blob([webpBytes], { type: "image/webp" }))).resolves.toEqual({
      mimeType: "image/webp", width: 64, height: 32,
    });

    await expect(validateSupportedImageBlob(new Blob(["<svg/>"], { type: "image/svg+xml" }))).rejects.toThrow("Only PNG");
    await expect(validateSupportedImageBlob(new Blob(["<svg/>"], { type: "image/png" }))).rejects.toThrow("do not match");
  });

  it("normalizes reverse drags and clips to image bounds", () => {
    expect(normalizeImageRect({ x: 90, y: 70 }, { x: -10, y: 120 }, 100, 80)).toEqual({
      x: 0,
      y: 70,
      width: 90,
      height: 10,
    });
  });

  it("keeps append, undo, and redo histories immutable", () => {
    const empty = createImageEditHistory();
    const one = appendImageEdit(empty, crop);
    const two = appendImageEdit(one, rectangle);
    const undone = undoImageEdit(two);
    const redone = redoImageEdit(undone);
    expect(empty.operations).toEqual([]);
    expect(one.operations).toEqual([crop]);
    expect(two.operations).toEqual([crop, rectangle]);
    expect(undone.operations).toEqual([crop]);
    expect(undone.undone).toEqual([rectangle]);
    expect(redone.operations).toEqual([crop, rectangle]);
    expect(redone.undone).toEqual([]);
    expect(redone.operations).not.toBe(two.operations);
  });

  it("validates all operation classes in current post-crop coordinates", () => {
    const operations: ImageEditOperation[] = [
      crop,
      rectangle,
      { id: "arrow", type: "arrow", from: { x: 0, y: 0 }, to: { x: 99, y: 79 }, color: "#00ff00", thickness: 2 },
      { id: "blur", type: "blur", rect: { x: 30, y: 30, width: 10, height: 10 }, radius: 8 },
      { id: "redact", type: "redact", rect: { x: 40, y: 30, width: 10, height: 10 }, color: "#000000" },
      { id: "text", type: "text", at: { x: 5, y: 5 }, text: "Finding", color: "#ffffff", fontSize: 20 },
    ];
    expect(imageDimensionsAfterOperations(200, 160, operations)).toEqual({ width: 100, height: 80 });
    expect(() => imageDimensionsAfterOperations(200, 160, [
      crop,
      { ...rectangle, rect: { x: 90, y: 70, width: 20, height: 20 } },
    ])).toThrow("outside the current image");
  });

  it("builds a versioned, deep-cloned immutable evidence recipe", () => {
    const recipe = createImageEditRecipe(200, 160, [crop, rectangle]);
    expect(recipe).toMatchObject({
      version: 1,
      sourceWidth: 200,
      sourceHeight: 160,
      outputWidth: 100,
      outputHeight: 80,
    });
    expect(recipe.operations).not.toBe([crop, rectangle]);
    expect(recipe.operations[0]).not.toBe(crop);
    crop.rect.x = 0;
    expect((recipe.operations[0] as typeof crop).rect.x).toBe(10);
  });

  it("replays crop and annotation operations on new canvases without changing the source", () => {
    const contexts: CanvasRenderingContext2D[] = [];
    const createCanvas = () => {
      const canvas = document.createElement("canvas");
      const methods = new Map<PropertyKey, ReturnType<typeof vi.fn>>();
      const context = new Proxy({ filter: "none" } as unknown as CanvasRenderingContext2D, {
        get(target, property) {
          const value = Reflect.get(target, property);
          if (value !== undefined) return value;
          if (!methods.has(property)) methods.set(property, vi.fn());
          return methods.get(property);
        },
        set(target, property, value) {
          return Reflect.set(target, property, value);
        },
      });
      contexts.push(context);
      Object.defineProperty(canvas, "getContext", { value: () => context });
      return canvas;
    };
    const source = document.createElement("canvas");
    source.width = 200;
    source.height = 160;
    const operations: ImageEditOperation[] = [
      { ...crop, rect: { x: 10, y: 20, width: 100, height: 80 } },
      rectangle,
      { id: "arrow", type: "arrow", from: { x: 0, y: 0 }, to: { x: 50, y: 40 }, color: "#00ff00", thickness: 2 },
      { id: "blur", type: "blur", rect: { x: 30, y: 30, width: 10, height: 10 }, radius: 8 },
      { id: "redact", type: "redact", rect: { x: 40, y: 30, width: 10, height: 10 }, color: "#000000" },
      { id: "text", type: "text", at: { x: 5, y: 5 }, text: "Finding", color: "#ffffff", fontSize: 20 },
    ];
    const rendered = renderImageEdits(source, 200, 160, operations, createCanvas);
    expect(rendered).not.toBe(source);
    expect(rendered.width).toBe(100);
    expect(rendered.height).toBe(80);
    expect(contexts).toHaveLength(3); // source canvas, crop canvas, and blur snapshot
    expect(contexts[1].strokeRect).toHaveBeenCalled();
    expect(contexts[1].fillRect).toHaveBeenCalled();
    expect(contexts[1].fillText).toHaveBeenCalledWith("Finding", 5, 5);
    expect(contexts[1].filter).toBe("none");
    expect(source.width).toBe(200);
    expect(source.height).toBe(160);
  });
});
