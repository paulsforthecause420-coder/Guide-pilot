# Developer guide

## Where things live

```
backend/
  guidepilot/
    serve.py            entry point: --port, --print-ready, JSON handshake on stdout
    server.py           transport-independent API handlers + the HTTP handler
    config.py           settings schema, validation, persistence, runtime token
    models.py           dataclasses + allowed state transitions (the domain model)
    validation.py       a small JSON-Schema subset over shared/schemas/*.json
    util.py             logging, atomic writes, text normalisation, commands
    perception/         display detection, capture backends, change detection, observer
    accessibility/      AT-SPI client (gdbus), window listing, OCR
    grounding/          candidate ranking + coordinate transforms
    task_engine/        engine.py (state machine), planner.py, values.py, workflows.py
    verification/       verification rules
    providers/          gateway, OpenAI-compatible client, mock provider
    security/           secret store, redaction, prompt-injection defence, command risk
    storage/            SQLite schema and accessors
    speech/             espeak/spd-say output, whisper input
  tests/                pytest suites (conftest.py has the fake observer)
desktop/
  electron/             main process, overlay manager, IPC, backend supervisor
  electron/lib/         pure helpers (unit tested)
  renderer/             React panel + overlay page
  tests/                node:test suites for geometry and the IPC surface
shared/schemas/         JSON Schemas: plan, guidance, workflow, grounding
demo-app/               deterministic Electron target app for acceptance runs
tests/                  acceptance.desktop.mjs (live desktop scenario)
workflows/examples/     importable workflow JSON
packaging/              stage.sh, build-deb.sh, build-appimage.sh, launcher, icon
scripts/                dev-desktop.sh, run-demo.sh, test.sh, acceptance.sh
```

## Running

```bash
npm install --prefix desktop
./scripts/dev-desktop.sh                      # build + start
npm run build --prefix desktop                # build only
(cd desktop && ./node_modules/electron/dist/electron . --remote-debugging-port=9223)
```

The main process starts the Python service itself; there is no separate service
command to run. For backend-only work:

```bash
cd backend && python3 -m guidepilot.serve --port 8765 --print-ready
```

## Code conventions

* **Never claim a step is done without evidence.** New features route through
  `Verifier`; if the evidence does not exist, return `INCONCLUSIVE`.
* **Never invent a value.** Use `values.resolve_value`; a missing value becomes a
  question in `missing_input`, not a guess. Model-supplied values with
  `source: model_assumption` are dropped.
* **Rectangles are explicit about their space.** Physical screen pixels between
  the backend and the main process; DIP inside Electron. Convert with
  `grounding/geometry.py` and `electron/lib/geometry.ts`.
* **Untrusted text is data.** Anything from the screen is wrapped with
  `security.injection.wrap_untrusted` before it reaches a model.
* **Failures are visible.** The engine returns `warnings`, `paused_reason`,
  `notes` or `BLOCKED`; the UI shows them. No silent fallbacks that look like
  success.
* Backend: standard library only, type hints on public functions, no PyGObject.
* Renderer: no `any` in new props; IPC results are `{ ok, data }` or
  `{ ok: false, error }` and every call site handles both.

## Adding a verification rule

1. Add the rule to `Verifier._evaluate_rule` in
   `backend/guidepilot/verification/verifier.py` returning
   `Evidence(kind, True | False | None, detail)`. `None` means "I could not
   tell" and must be used whenever the data source was unavailable.
2. Teach `describe_rules` to phrase it for the UI.
3. Add the rule to `shared/schemas/plan.schema.json` (the `verification` items
   enum) so a model may use it.
4. Add a test in `backend/tests/test_verification.py` covering pass, fail and
   unavailable-evidence cases.

## Adding a planner capability

The local planner must stay honest offline. Extend `detect_form`/`local_plan` for
new control types (comboboxes, tables, trees) and always attach a rule that can be
checked from the accessibility tree. Anything that cannot be verified belongs in
the model planner path, which is validated before use.

## Changing the IPC surface

Every channel must exist in three places: `ipcMain.handle` in
`electron/main.ts`, `contextBridge` in `electron/preload.ts`, and the
`Window['guidepilot']` types in `renderer/src/global.d.ts`. The node test
`desktop/tests/ipc.test.mjs` fails if the first two drift apart.

## Packaging

```bash
./packaging/stage.sh              # portable tree in packaging/build/stage
./packaging/build-deb.sh          # .deb via dpkg-deb
./packaging/build-appimage.sh     # AppImage, or a portable tarball
```

The launcher layout is `<prefix>/resources/guidepilot` (application code and
`backend/`) plus `<prefix>/electron` (the bundled runtime). `resolvePaths` in
`electron/backend.ts` handles both the source and the packaged layout, so test a
packaged build by running `packaging/build/stage/guidepilot` directly — it starts
the same way an installed copy does.

## Debugging

* `GUIDEPILOT_DEVTOOLS=1` opens DevTools at start-up.
* `--remote-debugging-port=9223` exposes CDP; `http://127.0.0.1:9223/json/list`
  lists pages, and `tests/acceptance.desktop.mjs` shows how to evaluate
  `window.guidepilot.*` from node.
* Logs: `~/.local/share/guidepilot/logs/` (service diagnostics go to stderr and
  are forwarded into the main-process log).
* The service writes its token to `~/.config/guidepilot/runtime/token`; you can
  drive the API by hand:

  ```bash
  TOKEN=$(cat ~/.config/guidepilot/runtime/token)
  curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:<port>/capabilities | jq
  ```

* `GUIDEPILOT_PYTHON=/path/to/python3` overrides the interpreter the main process
  spawns.
