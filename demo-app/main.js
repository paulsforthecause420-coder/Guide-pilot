/**
 * GuidePilot demo application (deterministic acceptance target).
 *
 * A small desktop app with a configuration form, a modal dialog, validation
 * errors, loading state and a success message. Accessibility metadata is fully
 * populated so GuidePilot can ground real controls precisely.
 *
 * The app also mirrors its internal state to a JSON file when
 * GUIDEPILOT_DEMO_STATE is set. That file exists purely so automated tests can
 * assert what really happened in the app; GuidePilot itself never reads it.
 */
const { app, BrowserWindow, ipcMain } = require('electron');
const fs = require('fs');
const path = require('path');

// A distinct application name keeps the accessibility tree unambiguous when
// GuidePilot itself (also an Electron app) is running.
app.setName('GuidePilot Demo');
app.commandLine.appendSwitch('force-renderer-accessibility');

const stateFile = process.env.GUIDEPILOT_DEMO_STATE || '';

function writeState(state) {
  if (!stateFile) return;
  try {
    fs.writeFileSync(stateFile, JSON.stringify({ ...state, at: Date.now() }, null, 2));
  } catch (error) {
    console.error('demo-state-write-failed', error.message);
  }
}

function createWindow() {
  const win = new BrowserWindow({
    width: 760,
    height: 620,
    x: 160,
    y: 120,
    title: 'GuidePilot Demo Application',
    backgroundColor: '#ffffff',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, 'preload.js'),
    },
  });
  win.loadFile(path.join(__dirname, 'index.html'));
  win.on('closed', () => app.quit());
  return win;
}

app.whenReady().then(() => {
  // Chromium only publishes an AT-SPI tree when it has a reason to; this is the
  // Electron API that guarantees it (the CLI switch alone is not honoured by
  // every Electron build). The demo exists to be inspected, so it opts in.
  try {
    app.setAccessibilitySupportEnabled(true);
  } catch (error) {
    console.error('accessibility-enable-failed', error.message);
  }
  console.error('accessibility-support', app.isAccessibilitySupportEnabled());
  writeState({ phase: 'started', saved: false });
  createWindow();
  ipcMain.on('demo-state', (_event, payload) => {
    writeState(payload);
    console.log('demo-state', JSON.stringify(payload));
  });
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => app.quit());
