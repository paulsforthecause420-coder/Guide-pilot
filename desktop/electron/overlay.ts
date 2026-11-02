/**
 * Highlight overlay windows.
 *
 * One transparent, click-through, always-on-top window is created per display
 * on demand. Each window covers exactly one monitor, which keeps coordinate
 * math local and avoids spanning the virtual screen (poorly supported and prone
 * to clipping).
 *
 * On Wayland the compositor does not let an application place such a window;
 * the caller falls back to in-window highlighting and the UI says so explicitly.
 */
import { BrowserWindow, screen } from 'electron';
import * as path from 'node:path';
import {
  DisplayLike,
  MonitorLike,
  Rect,
  clampLocalRect,
  labelPosition,
  matchDisplay,
  monitorForRect,
  overlayWindowBounds,
  toLocalRect,
} from './lib/geometry';

export interface HighlightPayload {
  rect: [number, number, number, number];
  label: string;
  instruction: string;
  number?: number | null;
  confidence?: number;
  source?: string;
  pulsing?: boolean;
}

interface OverlayContent extends HighlightPayload {
  localRect: Rect;
  labelBox: { x: number; y: number; placement: 'above' | 'below' };
  monitorName: string;
  viewport: { width: number; height: number };
  dim: boolean;
}

export class OverlayManager {
  private readonly windows = new Map<string, BrowserWindow>();
  private readonly rendererDir: string;
  private readonly mode: 'x11-overlay' | 'xwayland-overlay' | 'in-window';
  private lastPayload: HighlightPayload | null = null;

  constructor(rendererDir: string, mode: 'x11-overlay' | 'xwayland-overlay' | 'in-window') {
    this.rendererDir = rendererDir;
    this.mode = mode;
  }

  get isEnabled(): boolean {
    return this.mode !== 'in-window';
  }

  get overlayMode(): string {
    return this.mode;
  }

  private createWindow(display: DisplayLike): BrowserWindow {
    const bounds = overlayWindowBounds(display);
    const win = new BrowserWindow({
      x: Math.round(bounds.x),
      y: Math.round(bounds.y),
      width: Math.round(bounds.width),
      height: Math.round(bounds.height),
      frame: false,
      transparent: true,
      backgroundColor: '#00000000',
      hasShadow: false,
      show: false,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      focusable: false,
      acceptFirstMouse: false,
      alwaysOnTop: true,
      title: 'GuidePilot Overlay',
      webPreferences: {
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: true,
        devTools: false,
        backgroundThrottling: false,
      },
    });
    win.setAlwaysOnTop(true, 'screen-saver');
    // Click-through only. Forwarding (`{ forward: true }`) replays every mouse
    // event to the window below through XTest on X11, which delivers a user's
    // click twice — a checkbox toggles twice and a single click is lost. The
    // overlay never needs hover feedback, so the input region is simply empty
    // and the real application receives the click itself.
    win.setIgnoreMouseEvents(true);
    win.setSkipTaskbar(true);
    win.setMenuBarVisibility(false);
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
    win.loadFile(path.join(this.rendererDir, 'overlay.html')).catch((error) => {
      console.error('[overlay] failed to load overlay.html', error);
    });
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
    return win;
  }

  private windowForDisplay(display: DisplayLike): BrowserWindow {
    const key = String(display.id ?? display.label ?? `${display.bounds.x},${display.bounds.y}`);
    const existing = this.windows.get(key);
    if (existing && !existing.isDestroyed()) return existing;
    const created = this.createWindow(display);
    this.windows.set(key, created);
    return created;
  }

  private displayKey(display: DisplayLike): string {
    return String(display.id ?? display.label ?? `${display.bounds.x},${display.bounds.y}`);
  }

  private render(win: BrowserWindow, display: DisplayLike, monitor: MonitorLike | null, payload: HighlightPayload, dim: boolean): void {
    if (win.isDestroyed()) return;
    const bounds = overlayWindowBounds(display);
    const local = clampLocalRect(
      toLocalRect(
        { x: payload.rect[0], y: payload.rect[1], width: payload.rect[2], height: payload.rect[3] },
        monitor,
        display,
      ),
      { width: bounds.width, height: bounds.height },
    );
    const labelBox = labelPosition(local, { width: bounds.width, height: bounds.height }, { width: 340, height: 96 });
    const content: OverlayContent = {
      ...payload,
      localRect: local,
      labelBox,
      monitorName: monitor?.name ?? display.label ?? 'display',
      viewport: { width: bounds.width, height: bounds.height },
      dim,
    };
    const send = () => {
      if (win.isDestroyed()) return;
      win.webContents.send('overlay:render', content);
      if (!win.isVisible()) win.showInactive();
    };
    if (win.webContents.isLoading()) {
      win.webContents.once('did-finish-load', send);
    } else {
      send();
    }
  }

  /**
   * Display a highlight on the monitor that contains it.
   * Returns the overlay mode used; 'in-window' means no overlay window exists.
   */
  show(payload: HighlightPayload, monitors: MonitorLike[], dim: boolean): string {
    this.lastPayload = payload;
    if (!this.isEnabled) return 'in-window';
    const rect: Rect = { x: payload.rect[0], y: payload.rect[1], width: payload.rect[2], height: payload.rect[3] };
    const monitor = monitorForRect(rect, monitors);
    const index = monitor ? Math.max(0, monitors.indexOf(monitor)) : 0;
    const displays = screen.getAllDisplays() as unknown as DisplayLike[];
    const display = matchDisplay(monitor, index, displays);
    if (!display) return 'in-window';
    const win = this.windowForDisplay(display);
    this.render(win, display, monitor, payload, dim);
    const activeKey = this.displayKey(display);
    for (const [key, other] of this.windows.entries()) {
      if (key !== activeKey && !other.isDestroyed() && other.isVisible()) other.hide();
    }
    return this.mode;
  }

  /** Re-render after a display configuration change. */
  refresh(): void {
    if (!this.lastPayload) return;
    for (const win of this.windows.values()) {
      if (!win.isDestroyed()) win.reload();
    }
  }

  hideAll(): void {
    this.lastPayload = null;
    for (const win of this.windows.values()) {
      if (!win.isDestroyed() && win.isVisible()) win.hide();
    }
  }

  destroyAll(): void {
    this.lastPayload = null;
    for (const win of this.windows.values()) {
      if (!win.isDestroyed()) win.destroy();
    }
    this.windows.clear();
  }
}
