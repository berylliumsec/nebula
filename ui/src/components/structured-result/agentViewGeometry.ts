/**
 * Where the floating Agent view sits and how large it is. It is a per-device
 * convenience, so storage that is missing, full or blocked costs nothing
 * more than starting from the defaults.
 */

export interface AgentViewRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface AgentViewPoint {
  x: number;
  y: number;
}

export interface AgentViewViewport {
  left: number;
  top: number;
  width: number;
  height: number;
}

const GEOMETRY_KEY = "nebula.agent-view.geometry";
const LAUNCHER_KEY = "nebula.agent-view.launcher";

/** Gap kept between a floating surface and the edge of the window. */
export const EDGE = 12;
/** One arrow-key step when moving or resizing from the keyboard. */
export const STEP = 24;
export const MIN_WIDTH = 360;
export const MIN_HEIGHT = 320;
const DEFAULT_WIDTH = 480;
const DEFAULT_HEIGHT = 620;

function storage(): Storage | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined; // diagnostic-expected: storage blocked; the defaults apply for this session.
  }
}

function read(key: string): unknown {
  try {
    const raw = storage()?.getItem(key);
    return raw ? JSON.parse(raw) : undefined;
  } catch {
    return undefined; // diagnostic-expected: an unreadable saved layout falls back to the defaults.
  }
}

function write(key: string, value: unknown) {
  try {
    storage()?.setItem(key, JSON.stringify(value));
  } catch {
    // diagnostic-expected: a private window or a full store keeps this session's layout only.
  }
}

function finite(...values: unknown[]): boolean {
  return values.every((value) => typeof value === "number" && Number.isFinite(value));
}

/** The viewport a floating surface has to stay inside, zoom included. */
export function currentViewport(): AgentViewViewport {
  const visual = window.visualViewport;
  return {
    left: visual?.offsetLeft ?? 0,
    top: visual?.offsetTop ?? 0,
    width: visual?.width ?? window.innerWidth,
    height: visual?.height ?? window.innerHeight,
  };
}

/**
 * Fit a rectangle inside the viewport: never smaller than the minimum, never
 * larger than the window, and always with its move handle on screen.
 */
export function clampRect(rect: AgentViewRect, viewport: AgentViewViewport): AgentViewRect {
  const maxWidth = Math.max(MIN_WIDTH, viewport.width - EDGE * 2);
  const maxHeight = Math.max(MIN_HEIGHT, viewport.height - EDGE * 2);
  const width = Math.min(Math.max(rect.width, MIN_WIDTH), maxWidth);
  const height = Math.min(Math.max(rect.height, MIN_HEIGHT), maxHeight);
  const x = Math.min(Math.max(rect.x, viewport.left + EDGE), viewport.left + viewport.width - width - EDGE);
  const y = Math.min(Math.max(rect.y, viewport.top + EDGE), viewport.top + viewport.height - height - EDGE);
  return { x: Math.max(viewport.left, x), y: Math.max(viewport.top, y), width, height };
}

/** A small surface, such as the minimized launcher, kept fully on screen. */
export function clampPoint(point: AgentViewPoint, size: { width: number; height: number }, viewport: AgentViewViewport): AgentViewPoint {
  return {
    x: Math.max(viewport.left + EDGE, Math.min(point.x, viewport.left + viewport.width - size.width - EDGE)),
    y: Math.max(viewport.top + EDGE, Math.min(point.y, viewport.top + viewport.height - size.height - EDGE)),
  };
}

/** What a first placement should keep clear: the page's toolbar and composer. */
export interface AgentViewClearance {
  /** The bottom of whatever the panel should open below. */
  below?: number;
  /** The top of whatever the panel should open above. */
  above?: number;
}

/**
 * The panel's first position on a device: the upper right of the window,
 * under the conversation's toolbar and clear of the composer where there is
 * room for both.
 */
export function defaultRect(viewport: AgentViewViewport, clearance: AgentViewClearance = {}): AgentViewRect {
  const y = Math.max(viewport.top + 96, (clearance.below ?? 0) + EDGE);
  const room = clearance.above === undefined ? DEFAULT_HEIGHT : clearance.above - y - EDGE;
  return clampRect({
    x: viewport.left + viewport.width - DEFAULT_WIDTH - EDGE * 2,
    y,
    width: DEFAULT_WIDTH,
    height: Math.max(MIN_HEIGHT, Math.min(DEFAULT_HEIGHT, room)),
  }, viewport);
}

/** Where the operator last left the panel, or the first placement. */
export function readRect(viewport: AgentViewViewport, clearance: AgentViewClearance = {}): AgentViewRect {
  const saved = read(GEOMETRY_KEY) as Partial<AgentViewRect> | undefined;
  if (!saved || !finite(saved.x, saved.y, saved.width, saved.height)) return defaultRect(viewport, clearance);
  return clampRect(saved as AgentViewRect, viewport);
}

export function writeRect(rect: AgentViewRect) {
  write(GEOMETRY_KEY, rect);
}

export function readLauncher(): AgentViewPoint | undefined {
  const saved = read(LAUNCHER_KEY) as Partial<AgentViewPoint> | undefined;
  return saved && finite(saved.x, saved.y) ? saved as AgentViewPoint : undefined;
}

export function writeLauncher(point: AgentViewPoint) {
  write(LAUNCHER_KEY, point);
}

/** Arrow keys to a unit direction, or undefined for any other key. */
export function arrowDirection(key: string): [number, number] | undefined {
  return ({ ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] } as Record<string, [number, number]>)[key];
}
