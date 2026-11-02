/**
 * Pure placement and coordinate helpers used by the Electron main process.
 *
 * No Electron imports: these functions are unit tested with `node --test`
 * against the compiled output. Physical-pixel rectangles come from the Python
 * perception layer; these helpers convert them into device-independent pixels
 * (DIP) for Electron windows, which is where multi-monitor offsets and display
 * scaling have to be handled exactly.
 */

export interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export type DockEdge = 'none' | 'left' | 'right' | 'top' | 'bottom';

export interface MonitorLike extends Rect {
  id?: string;
  name?: string;
  scale?: number;
}

export interface DisplayLike {
  id?: number;
  label?: string;
  scaleFactor: number;
  bounds: Rect;
  workArea: Rect;
}

const clamp = (value: number, low: number, high: number): number => Math.max(low, Math.min(high, value));

/** Pick the monitor containing the largest part of a physical rect. */
export function monitorForRect<T extends MonitorLike>(rect: Rect, monitors: T[]): T | null {
  if (!Array.isArray(monitors) || monitors.length === 0) return null;
  let best: T | null = null;
  let bestArea = 0;
  for (const monitor of monitors) {
    const x0 = Math.max(rect.x, monitor.x);
    const y0 = Math.max(rect.y, monitor.y);
    const x1 = Math.min(rect.x + rect.width, monitor.x + monitor.width);
    const y1 = Math.min(rect.y + rect.height, monitor.y + monitor.height);
    const area = Math.max(0, x1 - x0) * Math.max(0, y1 - y0);
    if (area > bestArea) {
      bestArea = area;
      best = monitor;
    }
  }
  return best ?? monitors[0];
}

/**
 * Match a backend monitor (physical pixels) with an Electron display (DIP).
 * X11 display labels usually contain the monitor name; index order is the
 * fallback so multi-monitor placement still works when labels do not match.
 */
export function matchDisplay<T extends DisplayLike>(monitor: MonitorLike | null, index: number, displays: T[]): T | null {
  if (!displays || displays.length === 0) return null;
  if (monitor?.name) {
    const byName = displays.find((display) => display.label && display.label.includes(monitor.name as string));
    if (byName) return byName;
  }
  if (index >= 0 && index < displays.length) return displays[index];
  return displays[0];
}

/** Electron window bounds (DIP) for a full-monitor overlay. */
export function overlayWindowBounds(display: DisplayLike): Rect {
  return { ...display.bounds };
}

/**
 * Convert a physical screen rect into a rect local to an overlay window.
 * Applies the display scale factor so the highlight lands on the control even
 * when the desktop is scaled (HiDPI / X11 scaling).
 */
export function toLocalRect(rect: Rect, monitor: MonitorLike | null, display: DisplayLike): Rect {
  const scale = display.scaleFactor || 1;
  const monitorX = monitor ? monitor.x : display.bounds.x * scale;
  const monitorY = monitor ? monitor.y : display.bounds.y * scale;
  return {
    x: (rect.x - monitorX) / scale,
    y: (rect.y - monitorY) / scale,
    width: rect.width / scale,
    height: rect.height / scale,
  };
}

/** Clamp a rect into the overlay's local client area. */
export function clampLocalRect(rect: Rect, viewport: { width: number; height: number }): Rect {
  const width = Math.min(rect.width, viewport.width);
  const height = Math.min(rect.height, viewport.height);
  return {
    x: clamp(rect.x, 0, Math.max(0, viewport.width - width)),
    y: clamp(rect.y, 0, Math.max(0, viewport.height - height)),
    width,
    height,
  };
}

/** Place a label near a highlight without covering the target. */
export function labelPosition(
  rect: Rect,
  viewport: { width: number; height: number },
  labelSize: { width: number; height: number },
): { x: number; y: number; placement: 'above' | 'below' } {
  const padding = 10;
  const above = rect.y - labelSize.height - padding;
  const below = rect.y + rect.height + padding;
  const fitsBelow = below + labelSize.height <= viewport.height;
  const y = above >= 0 ? above : fitsBelow ? below : Math.max(0, rect.y - padding);
  const x = clamp(rect.x, 0, Math.max(0, viewport.width - labelSize.width));
  return { x, y, placement: above >= 0 ? 'above' : 'below' };
}

/** Whether the compositor can host a click-through always-on-top overlay. */
export function overlayMode(sessionType: string, overlaySupported: boolean): 'x11-overlay' | 'xwayland-overlay' | 'in-window' {
  if (sessionType === 'x11' && overlaySupported) return 'x11-overlay';
  if (sessionType === 'wayland' && overlaySupported) return 'xwayland-overlay';
  return 'in-window';
}

/** Keep the assistant window inside the work area after moves/resizes. */
export function clampWindowBounds(bounds: Rect, workArea: Rect, minWidth = 340, minHeight = 360): Rect {
  const width = clamp(bounds.width, minWidth, workArea.width);
  const height = clamp(bounds.height, minHeight, workArea.height);
  return {
    x: clamp(bounds.x, workArea.x, workArea.x + workArea.width - width),
    y: clamp(bounds.y, workArea.y, workArea.y + workArea.height - height),
    width,
    height,
  };
}

/** Dock the assistant to a screen edge (used by the edge-docking setting). */
export function dockedBounds(bounds: Rect, workArea: Rect, edge?: string): Rect {
  if (!edge || edge === 'none') return bounds;
  const margin = 12;
  if (edge === 'left') return { ...bounds, x: workArea.x + margin };
  if (edge === 'right') return { ...bounds, x: workArea.x + workArea.width - bounds.width - margin };
  if (edge === 'top') return { ...bounds, y: workArea.y + margin };
  if (edge === 'bottom') return { ...bounds, y: workArea.y + workArea.height - bounds.height - margin };
  return bounds;
}

/** Compact vs expanded window sizes for collapse mode. */
export function compactBounds(bounds: Rect, compact: boolean): Rect {
  if (compact) return { ...bounds, width: Math.min(bounds.width, 380), height: 240 };
  return { ...bounds, width: Math.max(bounds.width, 430), height: Math.max(bounds.height, 620) };
}

/** Normalise the requested window opacity to a safe range. */
export function normalizeOpacity(value: unknown): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 1;
  return clamp(numeric, 0.35, 1);
}
