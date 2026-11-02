# Testing

Three suites, each answering a different question.

| Suite | Command | Question |
| --- | --- | --- |
| Backend (pytest) | `npm run test:backend` or `./scripts/test.sh` | Does the pipeline behave correctly, and does it refuse to fake success? |
| Desktop (node:test) | `npm run test:node` | Is the placement maths right, and is the IPC/security surface intact? |
| Acceptance (live desktop) | `./scripts/acceptance.sh` | Does the whole product actually guide a real application on a real screen? |

`./scripts/test.sh` runs the first two plus the TypeScript type checks.

## Backend suite — `backend/tests`

105 tests, no display, no accessibility bus, no network, no real user data.
`conftest.py` provides an isolated settings file, a temporary SQLite database and
a `FakeObserver` that returns a screen the test wrote — the engine itself is
never mocked, so the real state machine, planner, verifier and storage are
exercised.

| File | Covers |
| --- | --- |
| `test_verification.py` | every rule, including the important negative cases: missing evidence is `INCONCLUSIVE`, never `VERIFIED`; unreadable field contents are not reported as a wrong value; secrets are presence-checked only |
| `test_engine.py` | task creation, grounding, advancing only on evidence, blocking after repeated failure, skip/confirm/pause/resume/cancel, emergency stop, secret redaction in payloads |
| `test_planner.py` | form plan derived from the observed elements, exact values only from the user, unknown fields becoming questions, no shell commands for non-terminal objectives, risk labels |
| `test_security.py` | command risk levels, credential redaction (including `api_key`-style keys), prompt-injection detection and neutralisation |
| `test_storage.py` | task/workflow round trips, audit trail, usage, purge, workflow capture without secrets, export/import, drift |
| `test_api.py` | token gating on every endpoint, validation errors, settings patches, secret never echoed, plus the real threaded HTTP server: 401 without a token, 403 for a foreign `Origin`, JSON-only bodies |
| `test_geometry_config.py` | capture↔screen mapping with scaling, multi-monitor parsing, clamping, grounding ranking and refusal, screen-change measurement, settings validation/persistence, JSON schemas |

```bash
cd backend && python3 -m pytest tests -q          # whole suite
cd backend && python3 -m pytest tests/test_engine.py -q -k blocked
```

## Desktop suite — `desktop/tests`

19 tests running against the compiled main-process helpers
(`npm run build:electron` first; the test script does this for you).

* `geometry.test.mjs` — monitor choice for straddling rectangles, DIP conversion
  with display scaling, second-monitor offsets, clamping, label placement
  flipping around the target, docking, compact sizing, opacity normalisation.
* `test_ocr.py` — the single-field OCR reader, driven by a stub binary: one line
  in, cleaned text out, failures surfaced instead of swallowed, and a missing
  Tesseract explained rather than crashing.
* `ipc.test.mjs` — static checks on the sources: every channel the preload
  invokes has a main-process handler and vice versa; `contextIsolation` on,
  `nodeIntegration` off, `sandbox` on; the overlay window is click-through and
  always on top; no `child_process`, no `shell.openPath`, no remote `loadURL`; the
  renderer cannot be navigated away from local content.

## Acceptance suite — `tests/acceptance.desktop.mjs`

The acceptance run is the only test that proves the product works, because it is
the only one that uses a screen.

What it does:

1. Starts the deterministic demo application (`demo-app/`) and GuidePilot with a
   CDP port.
2. Enables screen sharing through the real UI IPC surface, then creates a task
   with an objective and the values a user would supply.
3. For each step: reads the guidance, moves the pointer to the **highlighted
   rectangle**, clicks, types the exact text with `xdotool`, then calls
   `verify` and records the outcome and the evidence summary.
4. Fails the run if any step is not `VERIFIED` from screen evidence. A step that
   cannot be verified is reported as such — the test never marks it done.

The demo app writes its own state to `$GUIDEPILOT_DEMO_STATE` so the run can be
cross-checked against what the application really did (fields, checkbox states,
the success message). GuidePilot never reads that file.

Requirements: an X11 session, `xdotool`, `python3`, a built desktop bundle, and
no other GuidePilot instance running (the app holds a single-instance lock).

This suite drives the **real** pointer and keyboard: it raises the demo window,
clicks the controls and types into the form. Do not run it while you are using
the machine, and expect the opened windows to take focus for the duration.

```bash
./scripts/acceptance.sh            # default CDP port 9223
./scripts/acceptance.sh 9300       # custom port
```

Logs land in `$TMPDIR/guidepilot-acceptance/` (`demo.log`, `app.log`).

### Flakiness and how it is handled

Real desktops are asynchronous. The scenario therefore:

* waits for keyboard focus to actually reach the demo window before typing
  (XFCE's click-to-focus can swallow the activation click);
* retries a `type`/`select` interaction once if verification failed, because a
  lost focus click is a real user problem, not a product bug — a checkbox toggle
  is never retried;
* stops at the first genuine failure and reports the evidence, instead of pushing
  on and reporting a green run built on unverified steps.

If the run fails, re-run it with diagnostics: `GUIDEPILOT_DEBUG_A11Y=1` logs
every failed accessibility call and every empty text read, and
`GUIDEPILOT_DEBUG_VERIFY=1` logs what the pixel fallback measured (field
change ratio, ink before/after, image sizes). Both appear in `app.log` with a
`[backend]` prefix. `GUIDEPILOT_NO_A11Y_STATUS=1` suppresses the accessibility
status announcement, for platforms that already treat GuidePilot as assistive
technology.

Then check `app.log` for `[backend]` lines, confirm
`xdotool getactivewindow getwindowname` reports the demo window, and verify the
accessibility tree is populated:

```bash
gdbus call --session --dest org.a11y.Bus --object-path /org/a11y/bus \
  --method org.a11y.Bus.GetAddress
```

## Manual checks worth doing after a change

* Turn screen sharing off mid-task: the highlight disappears and the panel says
  why.
* Press the emergency stop: the task pauses, capture is off, and no further
  observations happen.
* Point a task at a password-manager window: nothing is captured.
* Resize/move the target window, then press **Look again**: the highlight follows
  or the step honestly reports it cannot find the control.
