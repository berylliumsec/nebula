import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import styles from "./ImageEditor.module.css";
import {
  appendImageEdit,
  clampImagePoint,
  createImageEditHistory,
  createImageEditRecipe,
  imageDimensionsAfterOperations,
  MAX_IMAGE_EDIT_OPERATIONS,
  normalizeImageRect,
  redoImageEdit,
  renderImageEdits,
  type ImageEditHistory,
  type ImageEditOperation,
  type ImageEditRecipe,
  type ImagePoint,
  undoImageEdit,
  validateSupportedImageBlob,
} from "./imageEditModel";
import { DiagnosticErrorNotice, logCaughtDiagnostic } from "../../diagnostics";

export type ImageEditorTool = "pan" | "crop" | "rectangle" | "arrow" | "blur" | "redact" | "text";

export interface ImageEditorSaveResult {
  blob: Blob;
  width: number;
  height: number;
  recipe: ImageEditRecipe;
}

export interface ImageEditorProps {
  /** A trusted local/API Blob. Its MIME type and magic bytes are both checked. */
  source: Blob;
  initialOperations?: readonly ImageEditOperation[];
  onSave: (result: ImageEditorSaveResult) => void | Promise<void>;
  onCancel?: () => void;
  onOperationsChange?: (operations: readonly ImageEditOperation[]) => void;
  maxDecodedPixels?: number;
  maxDimension?: number;
  maxOperations?: number;
  saveLabel?: string;
}

interface LoadedImage {
  image: HTMLImageElement;
  width: number;
  height: number;
}

interface DrawGesture {
  kind: "draw";
  start: ImagePoint;
  current: ImagePoint;
}

interface PanGesture {
  kind: "pan";
  clientX: number;
  clientY: number;
  scrollLeft: number;
  scrollTop: number;
}

type EditorGesture = DrawGesture | PanGesture;

function operationId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `image-edit-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function loadImage(
  source: Blob,
  maxDecodedPixels: number,
  maxDimension: number,
): { promise: Promise<LoadedImage>; dispose: () => void } {
  let objectUrl: string | undefined;
  let disposed = false;
  const image = new Image();
  image.decoding = "async";
  const promise = validateSupportedImageBlob(source).then((validated) => new Promise<LoadedImage>((resolve, reject) => {
    if (disposed) {
      reject(new Error("Image loading was cancelled."));
      return;
    }
    if (validated.width > maxDimension || validated.height > maxDimension
      || validated.width * validated.height > maxDecodedPixels) {
      reject(new Error("The decoded image is too large to edit safely."));
      return;
    }
    objectUrl = URL.createObjectURL(source);
    image.onload = () => {
      if (!image.naturalWidth || !image.naturalHeight) {
        reject(new Error("The image has invalid dimensions."));
        return;
      }
      if (image.naturalWidth > maxDimension || image.naturalHeight > maxDimension
        || image.naturalWidth * image.naturalHeight > maxDecodedPixels) {
        reject(new Error("The decoded image is too large to edit safely."));
        return;
      }
      resolve({ image, width: image.naturalWidth, height: image.naturalHeight });
    };
    image.onerror = () => reject(new Error("The image could not be decoded."));
    image.src = objectUrl;
  }));
  return {
    promise,
    dispose: () => {
      disposed = true;
      image.onload = null;
      image.onerror = null;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    },
  };
}

function canvasBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("The edited image could not be encoded as PNG."));
    }, "image/png");
  });
}

function pointerPoint(canvas: HTMLCanvasElement, event: ReactPointerEvent<HTMLCanvasElement>): ImagePoint {
  const rect = canvas.getBoundingClientRect();
  return clampImagePoint({
    x: ((event.clientX - rect.left) / Math.max(1, rect.width)) * canvas.width,
    y: ((event.clientY - rect.top) / Math.max(1, rect.height)) * canvas.height,
  }, canvas.width, canvas.height);
}

function operationFromGesture(
  tool: ImageEditorTool,
  gesture: DrawGesture,
  width: number,
  height: number,
  color: string,
  thickness: number,
): ImageEditOperation | undefined {
  const rect = normalizeImageRect(gesture.start, gesture.current, width, height);
  if (tool !== "arrow" && (rect.width < 2 || rect.height < 2)) return undefined;
  switch (tool) {
    case "crop": return { id: operationId(), type: "crop", rect };
    case "rectangle": return { id: operationId(), type: "rectangle", rect, color, thickness };
    case "arrow": {
      if (gesture.start.x === gesture.current.x && gesture.start.y === gesture.current.y) return undefined;
      return { id: operationId(), type: "arrow", from: gesture.start, to: gesture.current, color, thickness };
    }
    case "blur": return { id: operationId(), type: "blur", rect, radius: Math.min(64, Math.max(1, thickness * 2)) };
    case "redact": return { id: operationId(), type: "redact", rect, color };
    default: return undefined;
  }
}

function drawGesturePreview(
  context: CanvasRenderingContext2D,
  gesture: DrawGesture,
  tool: ImageEditorTool,
  color: string,
  thickness: number,
  width: number,
  height: number,
): void {
  const rect = normalizeImageRect(gesture.start, gesture.current, width, height);
  context.save();
  context.lineWidth = Math.max(1, thickness);
  context.strokeStyle = color;
  context.fillStyle = color;
  if (tool === "crop") {
    context.setLineDash([8, 5]);
    context.strokeStyle = "#ffffff";
    context.strokeRect(rect.x, rect.y, rect.width, rect.height);
  } else if (tool === "rectangle") {
    context.strokeRect(rect.x, rect.y, rect.width, rect.height);
  } else if (tool === "arrow") {
    context.beginPath();
    context.moveTo(gesture.start.x, gesture.start.y);
    context.lineTo(gesture.current.x, gesture.current.y);
    context.stroke();
  } else if (tool === "blur") {
    context.globalAlpha = 0.35;
    context.fillStyle = "#79b8ff";
    context.fillRect(rect.x, rect.y, rect.width, rect.height);
  } else if (tool === "redact") {
    context.fillRect(rect.x, rect.y, rect.width, rect.height);
  }
  context.restore();
}

const TOOLS: readonly { id: ImageEditorTool; label: string; title: string }[] = [
  { id: "pan", label: "Pan", title: "Drag the image within the viewport" },
  { id: "crop", label: "Crop", title: "Drag a new image boundary" },
  { id: "rectangle", label: "Rectangle", title: "Draw an outline" },
  { id: "arrow", label: "Arrow", title: "Draw an arrow" },
  { id: "blur", label: "Blur", title: "Blur pixels; not suitable for irreversible redaction" },
  { id: "redact", label: "Redact", title: "Cover pixels with an opaque color" },
  { id: "text", label: "Text", title: "Place the entered text on the image" },
];

export function ImageEditor({
  source,
  initialOperations = [],
  onSave,
  onCancel,
  onOperationsChange,
  maxDecodedPixels = 50_000_000,
  maxDimension = 16_384,
  maxOperations = MAX_IMAGE_EDIT_OPERATIONS,
  saveLabel = "Save copy",
}: ImageEditorProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [loaded, setLoaded] = useState<LoadedImage>();
  const [history, setHistory] = useState<ImageEditHistory>(() => createImageEditHistory(initialOperations));
  const [tool, setTool] = useState<ImageEditorTool>("pan");
  const [color, setColor] = useState("#ff3b4f");
  const [thickness, setThickness] = useState(4);
  const [text, setText] = useState("");
  const [zoom, setZoom] = useState(1);
  const [gesture, setGesture] = useState<EditorGesture>();
  const [error, setError] = useState<string>();
  const [saving, setSaving] = useState(false);
  const requestedOperationLimit = Number.isFinite(maxOperations) ? Math.floor(maxOperations) : MAX_IMAGE_EDIT_OPERATIONS;
  const operationLimit = Math.max(1, Math.min(MAX_IMAGE_EDIT_OPERATIONS, requestedOperationLimit));

  useEffect(() => {
    setLoaded(undefined);
    setError(undefined);
    setGesture(undefined);
    setHistory(createImageEditHistory(initialOperations));
    let resource: ReturnType<typeof loadImage>;
    try {
      resource = loadImage(source, maxDecodedPixels, maxDimension);
    } catch (reason) {
      void logCaughtDiagnostic("interface.image_editor.caught_failure_01", "A handled interface operation failed.", reason, "image_editor");
      setError(reason instanceof Error ? reason.message : "The image could not be loaded.");
      return;
    }
    let active = true;
    void resource.promise.then((image) => {
      if (active) setLoaded(image);
    }, (reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "The image could not be loaded.");
    });
    return () => {
      active = false;
      resource.dispose();
    };
  }, [source, maxDecodedPixels, maxDimension]);

  const dimensionResult = useMemo((): { value?: { width: number; height: number }; error?: string } => {
    if (!loaded) return {};
    try {
      return { value: imageDimensionsAfterOperations(loaded.width, loaded.height, history.operations) };
    } catch (reason) {
      void logCaughtDiagnostic("interface.image_editor.caught_failure_02", "A handled interface operation failed.", reason, "image_editor");
      return { error: reason instanceof Error ? reason.message : "The edit recipe is invalid." };
    }
  }, [loaded, history.operations]);
  const dimensions = dimensionResult.value;

  useEffect(() => {
    if (dimensionResult.error) setError(dimensionResult.error);
  }, [dimensionResult.error]);

  useEffect(() => onOperationsChange?.(history.operations), [history.operations, onOperationsChange]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !loaded || !dimensions) return;
    try {
      const rendered = renderImageEdits(loaded.image, loaded.width, loaded.height, history.operations);
      canvas.width = rendered.width;
      canvas.height = rendered.height;
      canvas.style.width = `${rendered.width * zoom}px`;
      canvas.style.height = `${rendered.height * zoom}px`;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("Canvas 2D rendering is unavailable.");
      context.drawImage(rendered, 0, 0);
      if (gesture?.kind === "draw") {
        drawGesturePreview(context, gesture, tool, color, thickness, dimensions.width, dimensions.height);
      }
    } catch (reason) {
      void logCaughtDiagnostic("interface.image_editor.caught_failure_03", "A handled interface operation failed.", reason, "image_editor");
      setError(reason instanceof Error ? reason.message : "The image could not be rendered.");
    }
  }, [loaded, dimensions, history.operations, gesture, tool, color, thickness, zoom]);

  const addOperation = useCallback((operation: ImageEditOperation) => {
    try {
      setHistory((current) => appendImageEdit(current, operation, operationLimit));
      setError(undefined);
    } catch (reason) {
      void logCaughtDiagnostic("interface.image_editor.caught_failure_04", "A handled interface operation failed.", reason, "image_editor");
      setError(reason instanceof Error ? reason.message : "The edit could not be added.");
    }
  }, [operationLimit]);

  const handlePointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0 || !dimensions) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    if (tool === "pan") {
      const viewport = viewportRef.current;
      if (!viewport) return;
      setGesture({
        kind: "pan",
        clientX: event.clientX,
        clientY: event.clientY,
        scrollLeft: viewport.scrollLeft,
        scrollTop: viewport.scrollTop,
      });
      return;
    }
    const point = pointerPoint(event.currentTarget, event);
    if (tool === "text") {
      const boundedText = text.slice(0, 1_000);
      if (!boundedText.length) {
        setError("Enter text before placing it on the image.");
        return;
      }
      addOperation({
        id: operationId(),
        type: "text",
        at: point,
        text: boundedText,
        color,
        fontSize: Math.max(8, Math.min(256, 12 + (thickness * 4))),
      });
      return;
    }
    setGesture({ kind: "draw", start: point, current: point });
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!gesture) return;
    if (gesture.kind === "pan") {
      const viewport = viewportRef.current;
      if (!viewport) return;
      viewport.scrollLeft = gesture.scrollLeft - (event.clientX - gesture.clientX);
      viewport.scrollTop = gesture.scrollTop - (event.clientY - gesture.clientY);
      return;
    }
    setGesture({ ...gesture, current: pointerPoint(event.currentTarget, event) });
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (!gesture || !dimensions) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (gesture.kind === "draw") {
      const finished = { ...gesture, current: pointerPoint(event.currentTarget, event) };
      const operation = operationFromGesture(tool, finished, dimensions.width, dimensions.height, color, thickness);
      if (operation) addOperation(operation);
    }
    setGesture(undefined);
  };

  const save = async () => {
    if (!loaded || saving) return;
    setSaving(true);
    setError(undefined);
    try {
      const rendered = renderImageEdits(loaded.image, loaded.width, loaded.height, history.operations);
      const recipe = createImageEditRecipe(loaded.width, loaded.height, history.operations);
      await onSave({
        blob: await canvasBlob(rendered),
        width: rendered.width,
        height: rendered.height,
        recipe,
      });
    } catch (reason) {
      void logCaughtDiagnostic("interface.image_editor.caught_failure_05", "A handled interface operation failed.", reason, "image_editor");
      setError(reason instanceof Error ? reason.message : "The edited image could not be saved.");
    } finally {
      setSaving(false);
    }
  };

  if (error && !loaded) return <div className={styles.editor}><DiagnosticErrorNotice error={error} fallback="The image editor could not load the capture." /></div>;
  if (loaded && dimensionResult.error) return <div className={styles.editor}><DiagnosticErrorNotice error={dimensionResult.error} fallback="The capture dimensions are invalid." /></div>;
  if (!loaded || !dimensions) return <div className={styles.editor}><div className={styles.loading} aria-live="polite">Loading image editor…</div></div>;

  return <section className={styles.editor} aria-label="Image editor">
    <div className={styles.toolbar} role="toolbar" aria-label="Image editing tools">
      <div className={styles.group}>
        {TOOLS.map((candidate) => <button
          key={candidate.id}
          className={styles.tool}
          type="button"
          title={candidate.title}
          aria-pressed={tool === candidate.id}
          onClick={() => {
            setGesture(undefined);
            setTool(candidate.id);
          }}
        >{candidate.label}</button>)}
      </div>
      <div className={styles.group}>
        <label className={styles.control}>Color <input
          className={styles.color}
          type="color"
          value={color}
          onChange={(event) => setColor(event.target.value)}
        /></label>
        <label className={styles.control}>Size <input
          className={styles.range}
          type="range"
          min="1"
          max="24"
          value={thickness}
          onChange={(event) => setThickness(Number(event.target.value))}
        /><span>{thickness}</span></label>
      </div>
      {tool === "text" && <div className={styles.group}>
        <label className={styles.control}>Text <input
          className={styles.textInput}
          type="text"
          maxLength={1_000}
          value={text}
          placeholder="Enter text, then click the image"
          onChange={(event) => setText(event.target.value)}
        /></label>
      </div>}
      <div className={styles.group}>
        <button className={styles.button} type="button" disabled={!history.operations.length} onClick={() => {
          setGesture(undefined);
          setHistory(undoImageEdit);
        }}>Undo</button>
        <button className={styles.button} type="button" disabled={!history.undone.length} onClick={() => {
          setGesture(undefined);
          setHistory(redoImageEdit);
        }}>Redo</button>
      </div>
      <div className={styles.group}>
        <button className={styles.button} type="button" aria-label="Zoom out" disabled={zoom <= 0.25} onClick={() => setZoom((value) => Math.max(0.25, value - 0.25))}>−</button>
        <span className={styles.status}>{Math.round(zoom * 100)}%</span>
        <button className={styles.button} type="button" aria-label="Zoom in" disabled={zoom >= 4} onClick={() => setZoom((value) => Math.min(4, value + 0.25))}>+</button>
      </div>
    </div>
    <div ref={viewportRef} className={styles.viewport}>
      <canvas
        ref={canvasRef}
        className={styles.canvas}
        role="img"
        aria-label={`Editable image, ${dimensions.width} by ${dimensions.height} pixels`}
        style={{ cursor: tool === "pan" ? (gesture?.kind === "pan" ? "grabbing" : "grab") : "crosshair" }}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={() => setGesture(undefined)}
      />
    </div>
    <div className={styles.footer}>
      <span className={styles.status} aria-live="polite">
        {dimensions.width} × {dimensions.height}px · {history.operations.length}/{operationLimit} edits
        {error ? <DiagnosticErrorNotice error={error} fallback="The image edit could not be completed." compact /> : null}
      </span>
      <div>
        {onCancel && <button className={styles.button} type="button" disabled={saving} onClick={onCancel}>Cancel</button>}
        <button className={`${styles.button} ${styles.primary}`} type="button" disabled={saving} onClick={() => void save()}>
          {saving ? "Saving…" : saveLabel}
        </button>
      </div>
    </div>
  </section>;
}
