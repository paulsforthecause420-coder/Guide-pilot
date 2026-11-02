#!/usr/bin/env node
/**
 * Live desktop acceptance driver for GuidePilot.
 *
 * This is NOT a mock test. It drives the real, running GuidePilot application
 * over the Chrome DevTools Protocol (so it exercises the exact IPC surface the
 * UI uses) and performs real mouse/keyboard input with xdotool on the real
 * deterministic demo application. Every highlight is checked against the
 * accessibility tree of the demo window by the application itself.
 *
 * Prerequisites (documented in docs/TESTING.md):
 *   1. X11 session with xdotool installed.
 *   2. The demo application running:  cd demo-app && npm start
 *   3. GuidePilot running with a debug port:
 *        cd desktop && ./node_modules/.bin/electron . --remote-debugging-port=9223
 *
 * Usage:
 *   node tests/acceptance.desktop.mjs [demo-window-name] [cdp-port]
 *
 * Exit code 0 means every step was verified from screen evidence.
 */

import { execFileSync } from 'node:child_process';

const DEMO_WINDOW = process.argv[2] ?? 'GuidePilot Demo Application';
const PORT = Number(process.argv[3] ?? 9223);
const OBJECTIVE = 'Walk me through configuring this application';
// The only information the user supplies up front. GuidePilot refuses to invent
// field values, so anything not listed here would be requested interactively.
const CONTEXT = {
  'App Name': 'GuidePilot Acceptance',
  'Callback URL': 'https://acceptance.example.com/api/oauth/callback',
  Region: 'EU',
  'Enable telemetry': 'yes',
  'Strict mode': 'yes',
};

const xdo = (...args) => execFileSync('xdotool', args, { encoding: 'utf8' });

function demoWindowId() {
  const ids = xdo('search', '--name', DEMO_WINDOW).trim().split('\n').filter(Boolean);
  if (!ids.length) throw new Error(`demo window "${DEMO_WINDOW}" not found — start demo-app first`);
  return ids[0];
}

function focusDemo(id) {
  // XFCE uses click-to-focus and the always-on-top assistant window can hold
  // focus, so retry until the target application really owns the keyboard.
  for (let attempt = 0; attempt < 6; attempt += 1) {
    xdo('windowactivate', id);
    xdo('windowraise', id);
    const focused = xdo('getwindowfocus', 'getwindowname').trim();
    if (focused === DEMO_WINDOW) return;
    execFileSync('sleep', ['0.4']);
  }
  throw new Error('could not give keyboard focus to the demo application');
}

/**
 * Locate a control in the demo window through the accessibility tree.
 * Setup only: the scenario itself never asks GuidePilot where to click, it
 * clicks the rectangles GuidePilot highlights.
 */
function a11yRect(label, role) {
  const script = `
import json, sys
from guidepilot.accessibility.atspi import AtspiClient
snap = AtspiClient().snapshot(window_title=${JSON.stringify(DEMO_WINDOW)})
for e in snap.elements:
    if e.name.strip() == ${JSON.stringify(label)} and (not ${JSON.stringify(role)} or e.role == ${JSON.stringify(role)}):
        print(json.dumps(e.rect.to_list()))
        break
`;
  const out = execFileSync('python3', ['-c', script], {
    cwd: `${process.cwd()}/backend`,
    encoding: 'utf8',
  }).trim();
  if (!out) throw new Error(`control '${label}' not found in the demo window`);
  return JSON.parse(out.split('\n').pop());
}

/** Put the deterministic demo form back into its initial, unsaved state. */
function resetDemo(id) {
  focusDemo(id);
  const rect = a11yRect('Reset Form', 'button');
  xdo('mousemove', String(Math.round(rect[0] + rect[2] / 2)), String(Math.round(rect[1] + rect[3] / 2)), 'sleep', '0.2', 'click', '1');
  execFileSync('sleep', ['0.6']);
  focusDemo(id);
}

/** Keep the assistant panel off the controls it is highlighting. */
function moveAssistantAside() {
  const ids = xdo('search', '--name', '^GuidePilot$').trim().split('\n').filter(Boolean);
  if (!ids.length) return;
  const [width, height] = xdo('getdisplaygeometry').trim().split(' ').map(Number);
  xdo('windowmove', ids[0], String(width - 460), '60');
  xdo('windowsize', ids[0], '430', String(Math.min(680, height - 100)));
}

function clickRect(rect, times = 1) {
  const x = Math.round(rect[0] + rect[2] / 2);
  const y = Math.round(rect[1] + rect[3] / 2);
  // The pointer must really arrive before the button is pressed: a click sent in
  // the same instant as the move can be delivered to the previous location.
  // XFCE's click-to-focus model can also swallow the activation click, so a text
  // field needs a second click before the caret is really inside it.
  xdo('mousemove', String(x), String(y), 'sleep', '0.15', 'click', '--repeat', String(times), '--delay', '180', '1');
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---- CDP plumbing ------------------------------------------------------- */

const pages = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
const page = pages.find((p) => p.type === 'page' && p.title === 'GuidePilot');
if (!page) throw new Error('GuidePilot renderer not found on the debug port');

const ws = new WebSocket(page.webSocketDebuggerUrl);
let nextId = 0;
const pending = new Map();
ws.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
  }
});
await new Promise((resolve) => ws.addEventListener('open', resolve));

async function call(method, params = {}) {
  const id = ++nextId;
  const reply = await new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
  if (reply.result?.exceptionDetails) throw new Error(JSON.stringify(reply.result.exceptionDetails));
  return reply.result?.result?.value;
}

async function evaluate(expression) {
  const value = await call('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    timeout: 180000,
  });
  if (value === undefined) return undefined;
  return JSON.parse(value);
}

/* ---- scenario ----------------------------------------------------------- */

const results = [];
const demo = demoWindowId();
console.log(`demo window: ${demo} "${DEMO_WINDOW}"`);
moveAssistantAside();
resetDemo(demo);

const enabled = await evaluate('window.guidepilot.privacy.enable(true).then((r) => JSON.stringify(r.ok))');
if (enabled !== true) throw new Error('could not enable screen sharing');

focusDemo(demo);
await sleep(600);

let state = await evaluate(
  `window.guidepilot.task.create(${JSON.stringify({ objective: OBJECTIVE, contextValues: CONTEXT })}).then((r) => JSON.stringify(r.ok ? r.data : { error: r.error }))`,
);
if (state?.error) throw new Error(`task creation failed: ${JSON.stringify(state.error)}`);
console.log(`task ${state.task.id}: ${state.task.steps.length} steps, state ${state.task.state}`);

async function currentState() {
  return evaluate('window.guidepilot.task.state().then((r) => JSON.stringify(r.data))');
}

async function verifyCurrent() {
  return evaluate(
    `window.guidepilot.task.action('verify').then((r) => JSON.stringify({ outcome: r.data?.verification?.outcome, summary: r.data?.verification?.summary, state: r.data?.task?.state, next: r.data?.task?.current_step?.title, error: r.error }))`,
  );
}

/** Perform the user's part of a step: the same clicks and keystrokes a person would make. */
function perform(highlight, guidance) {
  const typing = Boolean(guidance.text_to_enter) && !guidance.text_to_enter.sensitive;
  const selecting = guidance.action_type === 'select';
  const clickX = Math.round(highlight.rect[0] + highlight.rect[2] / 2);
  const clickY = Math.round(highlight.rect[1] + highlight.rect[3] / 2);
  xdo('mousemove', String(clickX), String(clickY));
  console.log(`  pointer: ${xdo('getmouselocation').trim()} focus: ${xdo('getactivewindow', 'getwindowname').trim()}`);
  clickRect(highlight.rect, typing && !selecting ? 2 : 1);
  execFileSync('sleep', ['0.3']);
  console.log(`  after click focus: ${xdo('getactivewindow', 'getwindowname').trim()}`);
  if (selecting) {
    // A native combo box opens on click; typing jumps to the option and Return
    // commits the choice.
    focusDemo(demo);
    if (typing) xdo('type', '--clearmodifiers', '--delay', '40', guidance.text_to_enter.value);
    else xdo('key', 'Down');
    execFileSync('sleep', ['0.25']);
    xdo('key', 'Return');
    execFileSync('sleep', ['0.4']);
  } else if (typing) {
    focusDemo(demo);
    // Replace whatever the field already contains rather than appending.
    xdo('key', '--clearmodifiers', 'ctrl+a');
    xdo('type', '--clearmodifiers', '--delay', '25', guidance.text_to_enter.value);
    execFileSync('sleep', ['0.9']);
  } else {
    execFileSync('sleep', ['0.3']);
  }
}

state = await currentState();
for (let guard = 0; guard < 24; guard += 1) {
  const step = state.task?.current_step;
  if (!step || state.task.state === 'COMPLETED') break;
  const guidance = state.guidance ?? {};
  const highlight = guidance.highlight;
  console.log(`\n[step ${step.index + 1}] ${step.title} (${guidance.action_type})`);
  console.log(`  instruction: ${guidance.instruction}`);
  console.log(`  target: ${highlight ? `${highlight.label} @ ${highlight.rect.join(',')} (${highlight.source}, ${Math.round(highlight.confidence * 100)}%)` : 'none'}`);
  if (guidance.text_to_enter) console.log(`  exact text: ${guidance.text_to_enter.value} [${guidance.text_to_enter.source}]`);

  // The assistant panel is always on top; keep it clear of the demo window so
  // the clicks below reach the application rather than the panel.
  moveAssistantAside();
  focusDemo(demo);
  await sleep(300);
  if (highlight) perform(highlight, guidance);

  let verified = await verifyCurrent();
  console.log(`  verification: ${verified.outcome ?? verified.error?.code} — ${verified.summary ?? verified.error?.message ?? ''}`);
  results.push({ step: step.title, outcome: verified.outcome ?? 'ERROR', summary: verified.summary ?? '' });

  // Typing can be lost to a swallowed focus click on a window manager that
  // activates on click; a person would simply click the field again. Only
  // repeatable interactions are retried, never a checkbox toggle.
  const repeatable = guidance.action_type === 'type' || guidance.action_type === 'select';
  if (verified.outcome !== 'VERIFIED' && repeatable && highlight) {
    console.log('  retrying the interaction once (focus may have been swallowed)');
    focusDemo(demo);
    await sleep(300);
    perform(highlight, guidance);
    verified = await verifyCurrent();
    console.log(`  retry verification: ${verified.outcome ?? verified.error?.code} — ${verified.summary ?? verified.error?.message ?? ''}`);
    results.push({ step: step.title, outcome: verified.outcome ?? 'ERROR', summary: verified.summary ?? '', retry: true });
  }

  if (verified.outcome !== 'VERIFIED') {
    console.log('  stopping: verification did not pass, which is the honest outcome to report');
    break;
  }
  state = await currentState();
}

const finalState = await evaluate('window.guidepilot.task.state().then((r) => JSON.stringify(r.data))');
const steps = finalState.task?.steps ?? [];
console.log(`\nfinal task state: ${finalState.task?.state}`);
for (const step of steps) console.log(`  ${step.index + 1}. ${step.title} — ${step.status}`);

const verifiedCount = steps.filter((step) => step.status === 'verified').length;
console.log(`verified steps: ${verifiedCount}/${steps.length}`);

// Every step must carry a VERIFIED status in the persisted task and the task
// itself must be completed: a skipped or user-confirmed step is not a pass.
const allVerified = steps.length > 0 && steps.every((step) => step.status === 'verified') && finalState.task?.state === 'COMPLETED';
console.log(allVerified ? 'ACCEPTANCE: PASS' : 'ACCEPTANCE: INCOMPLETE');

ws.close();
process.exit(allVerified ? 0 : 1);
