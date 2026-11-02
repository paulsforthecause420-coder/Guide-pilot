/**
 * Static checks on the desktop shell.
 *
 * The renderer may only talk to the backend through the handlers registered in
 * the main process, so every channel the preload exposes must have a handler
 * (and vice versa). These tests read the TypeScript sources, so they need no
 * Electron runtime and cannot be fooled by a stale build.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { test } from 'node:test';

const here = dirname(fileURLToPath(import.meta.url));
const electronDir = join(here, '..', 'electron');
const read = (name) => readFileSync(join(electronDir, name), 'utf8');

const channelsIn = (source, pattern) => new Set([...source.matchAll(pattern)].map((match) => match[1]));

const main = read('main.ts');
const preload = read('preload.ts');
const overlayPreload = read('overlay-preload.ts');
const overlay = read('overlay.ts');

const handled = channelsIn(main, /ipcMain\.handle\(\s*'([^']+)'/g);
const exposed = channelsIn(preload, /invoke\(\s*'([^']+)'/g);

test('every channel the preload invokes has a main-process handler', () => {
  assert.ok(handled.size > 10, 'no IPC handlers were found — did the source move?');
  const missing = [...exposed].filter((channel) => !handled.has(channel));
  assert.deepEqual(missing, [], `renderer can call unhandled channels: ${missing.join(', ')}`);
});

test('no handler is unreachable from the renderer', () => {
  const unused = [...handled].filter((channel) => !exposed.has(channel));
  assert.deepEqual(unused, [], `handlers no renderer can reach: ${unused.join(', ')}`);
});

test('the renderer is sandboxed: no node integration, context isolation on', () => {
  assert.match(main, /contextIsolation:\s*true/);
  assert.match(main, /nodeIntegration:\s*false/);
  assert.doesNotMatch(main, /nodeIntegration:\s*true/);
  assert.doesNotMatch(main, /enableRemoteModule/);
});

test('the preload exposes a narrow, frozen API instead of raw ipcRenderer', () => {
  assert.match(preload, /contextBridge\.exposeInMainWorld/);
  assert.doesNotMatch(preload, /exposeInMainWorld\([^,]+,\s*ipcRenderer\s*\)/);
  assert.doesNotMatch(preload, /process\./);
});

test('the overlay window is click-through and always on top', () => {
  assert.match(overlay, /setIgnoreMouseEvents\(\s*true/);
  assert.match(overlay, /setAlwaysOnTop\(\s*true/);
  assert.match(overlay, /setVisibleOnAllWorkspaces/);
});

test('the overlay preload exposes only highlight control', () => {
  const overlayChannels = channelsIn(overlayPreload, /on\(\s*'([^']+)'/g);
  const allowed = new Set(['overlay:render']);
  const unexpected = [...overlayChannels].filter((channel) => !allowed.has(channel));
  assert.deepEqual(unexpected, [], `unexpected overlay channels: ${unexpected.join(', ')}`);
});

test('the main process never builds a shell command from renderer input', () => {
  assert.doesNotMatch(main, /execSync\(|exec\(|spawnSync\(/, 'the shell must not be reachable from the UI');
  assert.doesNotMatch(main, /shell\.openPath/, 'the UI must not be able to open arbitrary paths');
});

test('window options do not silently reload or navigate to remote content', () => {
  assert.doesNotMatch(main, /loadURL\(\s*['"]https?:\/\//, 'the renderer must load local content only');
  assert.match(main, /sandbox:\s*true/, 'the renderer must stay sandboxed');
  assert.match(main, /setWindowOpenHandler\(\(\)\s*=>\s*\(\{\s*action:\s*'deny'\s*\}\)\)/);
  assert.match(main, /will-navigate/);
});
