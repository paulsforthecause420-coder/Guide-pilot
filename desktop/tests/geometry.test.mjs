/**
 * Placement and coordinate maths used to put the overlay exactly on a control.
 *
 * These run against the compiled main-process helpers (`npm run build:electron`
 * first), which is the same code Electron loads at runtime.
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { test } from 'node:test';

const require = createRequire(import.meta.url);

let geometry;
try {
  geometry = require('../dist/electron/lib/geometry.js');
} catch (error) {
  throw new Error(`Build the main process first (npm run build:electron): ${error.message}`);
}

const display = { scaleFactor: 1, bounds: { x: 0, y: 0, width: 1920, height: 1080 }, workArea: { x: 0, y: 0, width: 1920, height: 1040 } };

test('monitorForRect picks the monitor holding most of the rect', () => {
  const monitors = [
    { id: 'm0', name: 'DP-1', x: 0, y: 0, width: 1920, height: 1080 },
    { id: 'm1', name: 'HDMI-1', x: 1920, y: 0, width: 2560, height: 1440 },
  ];
  assert.equal(geometry.monitorForRect({ x: 100, y: 100, width: 50, height: 20 }, monitors).id, 'm0');
  assert.equal(geometry.monitorForRect({ x: 2000, y: 200, width: 250, height: 40 }, monitors).id, 'm1');
  // A rect straddling the seam belongs to the monitor it mostly covers (40 of
  // its 60 columns are on HDMI-1).
  assert.equal(geometry.monitorForRect({ x: 1900, y: 10, width: 60, height: 30 }, monitors).id, 'm1');
  assert.equal(geometry.monitorForRect({ x: 10, y: 10, width: 5, height: 5 }, []), null);
});

test('matchDisplay prefers a name match and falls back to index', () => {
  const displays = [
    { ...display, label: 'HDMI-1' },
    { ...display, label: 'DisplayPort-0 DP-1' },
  ];
  assert.equal(geometry.matchDisplay({ name: 'DP-1' }, 0, displays).label, 'DisplayPort-0 DP-1');
  assert.equal(geometry.matchDisplay({ name: 'unknown' }, 0, displays).label, 'HDMI-1');
  assert.equal(geometry.matchDisplay(null, 7, displays).label, 'HDMI-1');
  assert.equal(geometry.matchDisplay(null, 0, []), null);
});

test('toLocalRect honours display scaling', () => {
  const scaled = { scaleFactor: 2, bounds: { x: 0, y: 0, width: 1920, height: 1080 }, workArea: { x: 0, y: 0, width: 1920, height: 1080 } };
  const local = geometry.toLocalRect({ x: 400, y: 200, width: 100, height: 40 }, { x: 0, y: 0, width: 3840, height: 2160 }, scaled);
  assert.deepEqual(local, { x: 200, y: 100, width: 50, height: 20 });
});

test('toLocalRect subtracts a second monitor offset', () => {
  const local = geometry.toLocalRect(
    { x: 2100, y: 60, width: 120, height: 30 },
    { x: 1920, y: 0, width: 2560, height: 1440 },
    display,
  );
  assert.deepEqual(local, { x: 180, y: 60, width: 120, height: 30 });
});

test('clampLocalRect keeps a highlight inside the overlay', () => {
  const clamped = geometry.clampLocalRect({ x: -40, y: 2000, width: 100, height: 30 }, { width: 1920, height: 1080 });
  assert.equal(clamped.x, 0);
  assert.equal(clamped.y, 1050);
  assert.equal(clamped.width, 100);
});

test('labelPosition flips below the control when there is no room above', () => {
  const above = geometry.labelPosition({ x: 100, y: 200, width: 80, height: 30 }, { width: 800, height: 600 }, { width: 120, height: 24 });
  assert.equal(above.placement, 'above');
  assert.ok(above.y + 24 <= 200);

  const below = geometry.labelPosition({ x: 100, y: 4, width: 80, height: 30 }, { width: 800, height: 600 }, { width: 120, height: 24 });
  assert.equal(below.placement, 'below');
  assert.ok(below.y >= 34);
});

test('overlayMode reflects what the session can actually do', () => {
  assert.equal(geometry.overlayMode('x11', true), 'x11-overlay');
  assert.equal(geometry.overlayMode('wayland', true), 'xwayland-overlay');
  assert.equal(geometry.overlayMode('wayland', false), 'in-window');
  assert.equal(geometry.overlayMode('unknown', false), 'in-window');
});

test('clampWindowBounds keeps the assistant reachable', () => {
  const clamped = geometry.clampWindowBounds({ x: 5000, y: -800, width: 100, height: 100 }, display.workArea);
  assert.equal(clamped.width, 340);
  assert.equal(clamped.height, 360);
  assert.equal(clamped.x, 1920 - 340);
  assert.equal(clamped.y, 0);
});

test('dockedBounds hugs the requested edge', () => {
  const bounds = { x: 100, y: 100, width: 400, height: 600 };
  assert.equal(geometry.dockedBounds(bounds, display.workArea, 'right').x, 1920 - 400 - 12);
  assert.equal(geometry.dockedBounds(bounds, display.workArea, 'left').x, 12);
  assert.equal(geometry.dockedBounds(bounds, display.workArea, 'bottom').y, 1040 - 600 - 12);
  assert.deepEqual(geometry.dockedBounds(bounds, display.workArea, 'none'), bounds);
});

test('compactBounds collapses and restores the panel', () => {
  const compact = geometry.compactBounds({ x: 0, y: 0, width: 900, height: 900 }, true);
  assert.equal(compact.width, 380);
  assert.equal(compact.height, 240);
  const expanded = geometry.compactBounds({ x: 0, y: 0, width: 200, height: 200 }, false);
  assert.equal(expanded.width, 430);
  assert.equal(expanded.height, 620);
});

test('normalizeOpacity rejects nonsense and clamps the range', () => {
  assert.equal(geometry.normalizeOpacity(undefined), 1);
  assert.equal(geometry.normalizeOpacity('not a number'), 1);
  assert.equal(geometry.normalizeOpacity(0.1), 0.35);
  assert.equal(geometry.normalizeOpacity(2), 1);
  assert.equal(geometry.normalizeOpacity(0.8), 0.8);
});
