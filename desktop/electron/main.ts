/**
 * GuidePilot desktop main process.
 *
 * Responsibilities:
 *  - supervise the local Python service (loopback, token authenticated)
 *  - own the assistant window and the highlight overlay windows
 *  - expose a narrow IPC surface to the sandboxed renderer
 *  - keep window placement, opacity, docking and shortcuts in sync with settings
 *
 * The renderer never talks to the service directly and never sees the token.
 */
import { BrowserWindow, app, dialog, globalShortcut, ipcMain, screen, session } from 'electron';
import { existsSync } from 'node:fs';
import * as path from 'node:path';
import { BackendClient, BackendError, resolvePaths } from './backend';
import { HighlightPayload, OverlayManager } from './overlay';
import {
  DockEdge,
  clampWindowBounds,
  compactBounds,
  dockedBounds,
  normalizeOpacity,
  overlayMode,
} from './lib/geometry';

app.setName('GuidePilot');
app.commandLine.appendSwitch('disable-features', 'HardwareMediaKeyHandling');

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
}

const paths = resolvePaths(app.isPackaged, process.resourcesPath, __dirname);
const backend = new BackendClient({
  appRoot: paths.appRoot,
  backendDir: paths.backendDir,
  logger: (line) => console.log(line),
});

let assistantWindow: BrowserWindow | null = null;
let overlay: OverlayManager | null = null;
let capabilities: any = null;
let settings: any = null;
let lastTaskState: any = null;
let quitting = false;

interface IpcResult<T = unknown> {
  ok: boolean;
  data?: T;
  error?: { code: string; message: string; detail?: unknown };
}

function ok<T>(data: T): IpcResult<T> {
  return { ok: true, data };
}

function failure(error: unknown): IpcResult<never> {
  if (error instanceof BackendError) {
    return { ok: false, error: { code: error.code, message: error.message, detail: error.detail } };
  }
  const message = error instanceof Error ? error.message : String(error);
  return { ok: false, error: { code: 'internal', message } };
}

async function wrap<T>(operation: () => Promise<T>): Promise<IpcResult<T>> {
  try {
    return ok(await operation());
  } catch (error) {
    console.error('[ipc]', error);
    return failure(error);
  }
}

function send(channel: string, payload: unknown): void {
  if (assistantWindow && !assistantWindow.isDestroyed()) {
    assistantWindow.webContents.send(channel, payload);
  }
}

function notify(message: string, level: 'info' | 'warning' | 'error' = 'info'): void {
  send('app:notice', { message, level, at: Date.now() });
}

/* -------------------------------------------------------------------------- */
/* Settings & window state                                                    */
/* -------------------------------------------------------------------------- */

async function loadSettings(): Promise<any> {
  try {
    settings = await backend.get('/settings');
  } catch (error) {
    console.error('Could not read settings:', error);
    settings = null;
  }
  return settings;
}

async function patchSettings(patch: any): Promise<any> {
  const updated = await backend.put('/settings', patch);
  settings = updated;
  return updated;
}

function uiSetting<T>(key: string, fallback: T): T {
  const value = settings?.ui?.[key];
  return (value ?? fallback) as T;
}

function initialBounds() {
  const workArea = screen.getPrimaryDisplay().workArea;
  const saved = uiSetting('window_bounds', { x: 80, y: 80, width: 430, height: 680 });
  return clampWindowBounds({ ...saved }, workArea, 340, 360);
}

let saveBoundsTimer: NodeJS.Timeout | null = null;
function scheduleBoundsSave(): void {
  if (saveBoundsTimer) clearTimeout(saveBoundsTimer);
  saveBoundsTimer = setTimeout(async () => {
    if (!assistantWindow || assistantWindow.isDestroyed()) return;
    const bounds = assistantWindow.getBounds();
    const edge = uiSetting('edge_docking', 'none');
    try {
      await patchSettings({ ui: { window_bounds: bounds, edge_docking: edge } });
    } catch (error) {
      console.warn('Could not persist window bounds:', error);
    }
  }, 500);
}

/* -------------------------------------------------------------------------- */
/* Windows                                                                    */
/* -------------------------------------------------------------------------- */

function createAssistantWindow(): BrowserWindow {
  const bounds = initialBounds();
  const theme = uiSetting<string>('theme', 'dark');
  const win = new BrowserWindow({
    ...bounds,
    minWidth: 340,
    minHeight: 360,
    frame: false,
    title: 'GuidePilot',
    backgroundColor: theme === 'light' ? '#f7f8fb' : '#0b1220',
    alwaysOnTop: uiSetting('always_on_top', true),
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false,
    },
  });
  win.setAlwaysOnTop(uiSetting('always_on_top', true), 'floating');
  win.setOpacity(normalizeOpacity(uiSetting('opacity', 0.97)));
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('file://')) {
      event.preventDefault();
      notify('GuidePilot blocked an unexpected navigation.', 'warning');
    }
  });
  win.on('moved', scheduleBoundsSave);
  win.on('resized', scheduleBoundsSave);
  // Restoring the saved placement can land the panel on top of the control the
  // current step points at; re-check once the window is actually shown.
  win.on('show', () => {
    if (lastHighlightRect) setTimeout(() => avoidCoveringTarget(lastHighlightRect!), 0);
  });
  win.on('closed', () => {
    assistantWindow = null;
  });
  win.once('ready-to-show', () => {
    win.show();
    if (process.env.GUIDEPILOT_DEVTOOLS === '1') win.webContents.openDevTools({ mode: 'detach' });
  });
  win.loadFile(path.join(paths.rendererDir, 'index.html')).catch((error) => {
    dialog.showErrorBox('GuidePilot', `Could not load the interface: ${error.message}`);
  });
  return win;
}

function applyThemeChrome(): void {
  if (!assistantWindow) return;
  const theme = uiSetting<string>('theme', 'dark');
  assistantWindow.setBackgroundColor(theme === 'light' ? '#f7f8fb' : '#0b1220');
}

async function refreshCapabilities(): Promise<any> {
  capabilities = await backend.get('/capabilities');
  const mode = overlayMode(capabilities?.display?.session_type ?? 'unknown', Boolean(capabilities?.display?.overlay_supported));
  if (!overlay) {
    overlay = new OverlayManager(paths.rendererDir, mode);
  }
  return capabilities;
}

function decorateTaskState(payload: any): any {
  if (!payload) return payload;
  return {
    ...payload,
    overlay_mode: overlay?.overlayMode ?? capabilities?.display?.overlay_backend ?? 'in-window',
    session_type: capabilities?.display?.session_type ?? 'unknown',
    monitors: capabilities?.display?.monitors ?? [],
    privacy: capabilities?.privacy ?? {},
  };
}

function publishTaskState(payload: any): any {
  lastTaskState = payload ?? null;
  const decorated = decorateTaskState(payload);
  send('task:state', decorated);
  updateOverlay(decorated);
  return decorated;
}

/**
 * Keep the assistant panel off the control it is pointing at.
 *
 * An always-on-top panel that covers the target makes the instruction useless:
 * the user cannot click what they cannot see. When the highlighted rectangle
 * overlaps the panel, the panel moves to the side of the target with the most
 * room on this monitor and stays there for the rest of the step.
 */
function avoidCoveringTarget(rect: number[]): void {
  const win = assistantWindow;
  if (!win || win.isDestroyed() || !Array.isArray(rect) || rect.length < 4) return;
  const [rx, ry, rw, rh] = rect.map(Number);
  if (![rx, ry, rw, rh].every((n) => Number.isFinite(n)) || rw <= 0 || rh <= 0) return;
  const current = win.getBounds();
  const overlaps = !(
    current.x + current.width <= rx ||
    rx + rw <= current.x ||
    current.y + current.height <= ry ||
    ry + rh <= current.y
  );
  if (!overlaps) return;
  const display = screen.getDisplayMatching({ x: rx, y: ry, width: rw, height: rh });
  const area = display.workArea;
  const gap = 12;
  const rightRoom = area.x + area.width - (rx + rw);
  const leftRoom = rx - area.x;
  const targetCenterY = Math.round(ry + rh / 2 - current.height / 2);
  const clampY = (y: number): number => Math.min(Math.max(y, area.y + gap), area.y + area.height - current.height - gap);
  let x: number;
  let y: number;
  if (Math.max(rightRoom, leftRoom) >= current.width + gap) {
    x = rightRoom >= leftRoom ? Math.round(rx + rw + gap) : Math.round(rx - current.width - gap);
    y = clampY(targetCenterY);
  } else {
    // No room beside the target: go above or below it instead.
    const aboveRoom = ry - area.y;
    const belowRoom = area.y + area.height - (ry + rh);
    x = Math.round(Math.min(Math.max(rx, area.x + gap), area.x + area.width - current.width - gap));
    y = aboveRoom >= belowRoom ? clampY(Math.round(ry - current.height - gap)) : clampY(Math.round(ry + rh + gap));
  }
  win.setBounds({ x, y, width: current.width, height: current.height });
}

let lastHighlightRect: number[] | null = null;

function updateOverlay(state: any): void {
  if (!overlay) return;
  const highlight = state?.guidance?.highlight as any;
  lastHighlightRect = Array.isArray(highlight?.rect) ? (highlight.rect as number[]) : null;
  const stopped = Boolean(settings?.privacy?.stop_capture || capabilities?.stopped);
  const shareEnabled = Boolean(settings?.privacy?.share_enabled);
  if (!highlight || stopped || !shareEnabled || state?.task?.state === 'COMPLETED' || state?.task?.state === 'CANCELLED') {
    overlay.hideAll();
    return;
  }
  const payload: HighlightPayload = {
    rect: highlight.rect,
    label: highlight.label ?? 'Target',
    instruction: highlight.instruction ?? state?.guidance?.instruction ?? '',
    number: highlight.number ?? null,
    confidence: highlight.confidence,
    source: highlight.source,
    pulsing: highlight.pulsing !== false,
  };
  if (highlight.rect) avoidCoveringTarget(highlight.rect);
  try {
    overlay.show(payload, capabilities?.display?.monitors ?? [], Boolean(uiSetting('dim_background', false)));
  } catch (error) {
    console.error('[overlay] show failed', error);
  }
}

/* -------------------------------------------------------------------------- */
/* Task actions                                                               */
/* -------------------------------------------------------------------------- */

async function performTaskAction(action: string, payload: any): Promise<any> {
  const state = lastTaskState;
  const taskId = state?.task?.id;
  const requireTask = (): string => {
    if (!taskId) throw new BackendError({ code: 'no_task', message: 'Start a task first.' });
    return taskId;
  };
  // A slow response for a task that is no longer current must never overwrite
  // the state of the task the user is actually working on.
  const publish = (result: any): any => {
    const currentId = lastTaskState?.task?.id;
    if (currentId && result?.task?.id && result.task.id !== currentId) return lastTaskState;
    return publishTaskState(result);
  };
  switch (action) {
    case 'look': {
      const result = await backend.post(`/tasks/${requireTask()}/observe`, { force: true });
      capabilities = await refreshCapabilities();
      return publish(result);
    }
    case 'verify': {
      const result = await backend.post(`/tasks/${requireTask()}/verify`, {
        user_confirmed: Boolean(payload?.userConfirmed),
      });
      return publish(result);
    }
    case 'watch': {
      const result = await backend.post(`/tasks/${requireTask()}/watch`, { auto_verify: payload?.autoVerify !== false });
      return publish(result);
    }
    case 'confirm':
      return publish(await backend.post(`/tasks/${requireTask()}/confirm`, {}));
    case 'skip':
      return publish(await backend.post(`/tasks/${requireTask()}/skip`, { reason: payload?.reason ?? 'Skipped by user' }));
    case 'next':
      return publish(await backend.post(`/tasks/${requireTask()}/next`, {}));
    case 'previous':
      return publish(await backend.post(`/tasks/${requireTask()}/previous`, {}));
    case 'pause':
      return publish(await backend.post(`/tasks/${requireTask()}/pause`, {}));
    case 'resume':
      return publish(await backend.post(`/tasks/${requireTask()}/resume`, {}));
    case 'cancel': {
      const result = await backend.post(`/tasks/${requireTask()}/cancel`, {});
      overlay?.hideAll();
      return publish(result);
    }
    case 'saveWorkflow': {
      const result = await backend.post(`/tasks/${requireTask()}/save-workflow`, { name: payload?.name ?? '' });
      notify('Workflow saved to your local library.');
      return result;
    }
    case 'setContext': {
      const result = await backend.post(`/tasks/${requireTask()}/context`, { values: payload?.values ?? {} });
      return publish(result);
    }
    default:
      throw new BackendError({ code: 'unknown_action', message: `Unknown action '${action}'` });
  }
}

/* -------------------------------------------------------------------------- */
/* IPC                                                                        */
/* -------------------------------------------------------------------------- */

function registerIpc(): void {
  ipcMain.handle('app:capabilities', async () =>
    wrap(async () => {
      await refreshCapabilities();
      return {
        ...capabilities,
        privacy: settings?.privacy ?? capabilities?.privacy,
        app_info: { name: app.getName(), version: app.getVersion(), electron: process.versions.electron },
        backend: backend.info,
      };
    }),
  );

  ipcMain.handle('app:settings', async () => wrap(async () => (settings ? { ...(await loadSettings()) } : await loadSettings())));

  ipcMain.handle('app:settings:update', async (_event, patch: any) =>
    wrap(async () => {
      const updated = await patchSettings(patch ?? {});
      if (assistantWindow) {
        assistantWindow.setOpacity(normalizeOpacity(updated?.ui?.opacity ?? 1));
        assistantWindow.setAlwaysOnTop(Boolean(updated?.ui?.always_on_top), 'floating');
        applyThemeChrome();
      }
      await refreshCapabilities();
      if (lastTaskState) updateOverlay(decorateTaskState(lastTaskState));
      return updated;
    }),
  );

  ipcMain.handle('app:apikey:set', async (_event, payload: any) =>
    wrap(async () => backend.post('/settings/api-key', { api_key: String(payload?.apiKey ?? '') })),
  );

  ipcMain.handle('app:apikey:delete', async () => wrap(async () => backend.del('/settings/api-key')));

  ipcMain.handle('app:usage', async () => wrap(async () => backend.get('/usage')));

  ipcMain.handle('app:audit', async (_event, payload: any) =>
    wrap(async () => backend.get(`/audit?limit=${Number(payload?.limit ?? 100)}`)),
  );

  ipcMain.handle('task:create', async (_event, payload: any) =>
    wrap(async () => {
      const result = await backend.post(
        '/tasks',
        {
          objective: String(payload?.objective ?? ''),
          context_values: payload?.contextValues ?? {},
          app_hint: payload?.appHint ?? '',
          project_dir: payload?.projectDir ?? undefined,
          prefer_model: payload?.preferModel !== false,
        },
        120000,
      );
      return publishTaskState(result);
    }),
  );

  ipcMain.handle('task:state', async () => wrap(async () => lastTaskState ?? { task: null }));
  ipcMain.handle('task:action', async (_event, payload: any) =>
    wrap(async () => performTaskAction(String(payload?.action ?? ''), payload?.payload)),
  );

  ipcMain.handle('task:chat', async (_event, payload: any) =>
    wrap(async () => {
      const result = await backend.post('/chat', { message: String(payload?.message ?? '') }, 120000);
      if (result?.task) publishTaskState(result.task);
      return result;
    }),
  );

  ipcMain.handle('task:list', async () => wrap(async () => backend.get('/tasks')));
  ipcMain.handle('task:resume-prompt', async () => wrap(async () => backend.get('/tasks/resume-prompt')));
  ipcMain.handle('task:resume', async (_event, payload: any) =>
    wrap(async () => {
      const result = await backend.post(
        `/tasks/${encodeURIComponent(String(payload?.taskId))}/resume`,
        { context_values: payload?.contextValues ?? {} },
        120000,
      );
      return publishTaskState(result);
    }),
  );
  ipcMain.handle('task:clear', async () => wrap(async () => backend.del('/tasks')));
  ipcMain.handle('task:delete', async (_event, payload: any) =>
    wrap(async () => backend.del(`/tasks/${encodeURIComponent(String(payload?.taskId))}`)),
  );
  ipcMain.handle('task:context', async (_event, payload: any) =>
    wrap(async () => performTaskAction('setContext', payload)),
  );

  ipcMain.handle('workflows:list', async () => wrap(async () => backend.get('/workflows')));
  ipcMain.handle('workflows:start', async (_event, payload: any) =>
    wrap(async () => {
      const result = await backend.post(
        `/workflows/${encodeURIComponent(String(payload?.workflowId))}/start`,
        { context_values: payload?.contextValues ?? {} },
        120000,
      );
      return publishTaskState(result);
    }),
  );
  ipcMain.handle('workflows:export', async (_event, payload: any) =>
    wrap(async () => backend.get(`/workflows/${encodeURIComponent(String(payload?.workflowId))}/export`)),
  );
  ipcMain.handle('workflows:import', async (_event, payload: any) =>
    wrap(async () => backend.post('/workflows/import', { workflow: payload?.workflow })),
  );
  ipcMain.handle('workflows:delete', async (_event, payload: any) =>
    wrap(async () => backend.del(`/workflows/${encodeURIComponent(String(payload?.workflowId))}`)),
  );

  ipcMain.handle('privacy:stop', async () =>
    wrap(async () => {
      const result = await backend.post('/privacy/stop', {});
      settings = result.settings ?? settings;
      overlay?.hideAll();
      await refreshCapabilities();
      send('app:notice', { level: 'warning', message: 'Emergency stop: screen capture and guidance halted.' });
      return result;
    }),
  );

  ipcMain.handle('privacy:enable', async (_event, payload: any) =>
    wrap(async () => {
      const result = await backend.post('/privacy/enable', { acknowledge_cloud_vision: Boolean(payload?.acknowledgeCloudVision) });
      settings = result.settings ?? settings;
      await refreshCapabilities();
      return result;
    }),
  );

  ipcMain.handle('privacy:purge', async () =>
    wrap(async () => {
      const result = await backend.post('/privacy/purge', {});
      lastTaskState = null;
      overlay?.hideAll();
      return result;
    }),
  );

  ipcMain.handle('speech:status', async () => wrap(async () => backend.get('/speech')));
  ipcMain.handle('speech:speak', async (_event, payload: any) =>
    wrap(async () => backend.post('/speech/speak', { text: String(payload?.text ?? '') })),
  );

  ipcMain.handle('actions:paste', async (_event, payload: any) =>
    wrap(async () => {
      const result = await backend.post('/actions/paste', {
        text: String(payload?.text ?? ''),
        confirmed: Boolean(payload?.confirmed),
      });
      send('app:notice', { level: 'info', message: 'Assisted paste completed and recorded in the audit log.' });
      return result;
    }),
  );

  ipcMain.handle('window:minimize', async () => wrap(async () => assistantWindow?.minimize()));
  ipcMain.handle('window:close', async () =>
    wrap(async () => {
      assistantWindow?.hide();
      return { hidden: true };
    }),
  );
  ipcMain.handle('window:collapse', async (_event, payload: any) =>
    wrap(async () => {
      if (!assistantWindow) return null;
      const bounds = compactBounds(assistantWindow.getBounds(), Boolean(payload?.compact));
      assistantWindow.setBounds(clampWindowBounds(bounds, screen.getDisplayMatching(bounds).workArea, 340, 200));
      return assistantWindow.getBounds();
    }),
  );
  ipcMain.handle('window:opacity', async (_event, payload: any) =>
    wrap(async () => {
      const opacity = normalizeOpacity(payload?.opacity);
      assistantWindow?.setOpacity(opacity);
      await patchSettings({ ui: { opacity } });
      return { opacity };
    }),
  );
  ipcMain.handle('window:always-on-top', async (_event, payload: any) =>
    wrap(async () => {
      const enabled = Boolean(payload?.enabled);
      assistantWindow?.setAlwaysOnTop(enabled, 'floating');
      await patchSettings({ ui: { always_on_top: enabled } });
      return { enabled };
    }),
  );
  ipcMain.handle('window:dock', async (_event, payload: any) =>
    wrap(async () => {
      const edge = String(payload?.edge ?? 'none') as DockEdge;
      if (!assistantWindow) return null;
      const current = assistantWindow.getBounds();
      const display = screen.getDisplayMatching(current);
      const docked = edge === 'none' ? current : dockedBounds(current, display.workArea, edge);
      assistantWindow.setBounds(clampWindowBounds(docked, display.workArea, 340, 360));
      await patchSettings({ ui: { edge_docking: edge } });
      return { edge, bounds: assistantWindow.getBounds() };
    }),
  );
  ipcMain.handle('overlay:hide', async () =>
    wrap(async () => {
      overlay?.hideAll();
      return { hidden: true };
    }),
  );

  ipcMain.handle('overlay:preview', async (_event, payload: any) =>
    wrap(async () => {
      if (!payload?.rect) {
        overlay?.hideAll();
        return { shown: false };
      }
      const mode = overlay?.show(
        {
          rect: payload.rect,
          label: payload.label ?? 'Preview',
          instruction: payload.instruction ?? 'Preview highlight',
          pulsing: true,
        },
        capabilities?.display?.monitors ?? [],
        Boolean(uiSetting('dim_background', false)),
      );
      return { shown: true, mode };
    }),
  );
}

/* -------------------------------------------------------------------------- */
/* Shortcuts                                                                  */
/* -------------------------------------------------------------------------- */

function registerShortcuts(): void {
  globalShortcut.unregisterAll();
  const shortcuts = settings?.ui?.shortcuts ?? {};
  const accelerators: Array<[string, () => void]> = [
    [
      shortcuts.toggle ?? 'CommandOrControl+Shift+G',
      () => {
        if (!assistantWindow) return;
        if (assistantWindow.isVisible()) assistantWindow.hide();
        else {
          assistantWindow.show();
          assistantWindow.focus();
        }
      },
    ],
    [
      shortcuts.stop ?? 'CommandOrControl+Shift+Escape',
      () => {
        void backend
          .post('/privacy/stop', {})
          .then((result: any) => {
            settings = result.settings ?? settings;
            overlay?.hideAll();
            send('app:shortcut', { action: 'stop' });
          })
          .catch((error) => console.error('stop shortcut failed', error));
      },
    ],
    [
      shortcuts.repeat ?? 'CommandOrControl+Shift+R',
      () => {
        if (assistantWindow) {
          assistantWindow.show();
          send('app:shortcut', { action: 'repeat' });
        }
      },
    ],
  ];
  for (const [accelerator, handler] of accelerators) {
    try {
      globalShortcut.register(accelerator, handler);
    } catch (error) {
      console.warn(`Could not register shortcut ${accelerator}:`, error);
    }
  }
}

/* -------------------------------------------------------------------------- */
/* Bootstrap                                                                  */
/* -------------------------------------------------------------------------- */

async function bootstrap(): Promise<void> {
  session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
    const voiceEnabled = Boolean(settings?.voice?.enabled);
    callback(permission === 'media' && voiceEnabled);
  });
  session.defaultSession.setPermissionCheckHandler((_wc, permission) => {
    const voiceEnabled = Boolean(settings?.voice?.enabled);
    return permission === 'media' && voiceEnabled;
  });

  try {
    await backend.start();
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    dialog.showErrorBox(
      'GuidePilot could not start',
      `The local GuidePilot service failed to start.\n\n${message}\n\n` +
        'Check that python3 and the packages in backend/requirements.txt are available, then relaunch.',
    );
    app.quit();
    return;
  }

  await loadSettings();
  await refreshCapabilities();
  registerIpc();
  assistantWindow = createAssistantWindow();
  registerShortcuts();

  screen.on('display-added', () => void refreshCapabilities().then(() => overlay?.refresh()));
  screen.on('display-removed', () => void refreshCapabilities().then(() => overlay?.refresh()));
  screen.on('display-metrics-changed', () => void refreshCapabilities().then(() => overlay?.refresh()));

  if (!capabilities?.capture?.available) {
    notify(
      `Screen capture is unavailable: ${capabilities?.capture?.reason ?? 'unknown reason'}`,
      'warning',
    );
  }
}

app.whenReady().then(bootstrap);

app.on('second-instance', () => {
  if (assistantWindow) {
    assistantWindow.show();
    assistantWindow.focus();
  }
});

app.on('activate', () => {
  if (!assistantWindow) assistantWindow = createAssistantWindow();
  else assistantWindow.show();
});

app.on('window-all-closed', () => {
  app.quit();
});

app.on('before-quit', (event) => {
  if (quitting) return;
  quitting = true;
  event.preventDefault();
  overlay?.destroyAll();
  globalShortcut.unregisterAll();
  void backend.stop().finally(() => app.exit(0));
});

process.on('uncaughtException', (error) => {
  console.error('Uncaught exception in main process:', error);
});
