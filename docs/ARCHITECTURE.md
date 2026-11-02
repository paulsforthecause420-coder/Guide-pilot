# Architecture

GuidePilot is three processes and one hard rule.

```
┌──────────────────────────┐   IPC (narrow, typed)   ┌────────────────────────────┐
│ React renderer (sandbox) │ ──────────────────────▶ │ Electron main process      │
│ panel + overlay pages    │ ◀────────────────────── │ windows, shortcuts, client │
└──────────────────────────┘                         └─────────────┬──────────────┘
                                                                   │ HTTP + bearer token
                                                                   │ (127.0.0.1 only)
                                                     ┌─────────────▼──────────────┐
                                                     │ Python service (guidepilot)│
                                                     │ perception → grounding →   │
                                                     │ planning → verification    │
                                                     └────────────────────────────┘
```

**The rule:** nothing counts as done because a model, a planner or the user's
click said so. A step advances only on evidence read from a *fresh* observation,
or on an explicit, recorded user confirmation that never masquerades as a
verification.

The renderer never talks to the service and never sees the bearer token. It gets
data through the main process over IPC, which is the only component that owns
the HTTP client.

## Processes

### 1. Python service (`backend/guidepilot`)

Started by the Electron main process as `python3 -m guidepilot.serve --port 0
--print-ready`. It binds `127.0.0.1` on an ephemeral port, generates a per-run
token, writes it to `~/.config/guidepilot/runtime/token` with mode `0600`, and
prints a single JSON handshake line on stdout so the main process learns the port
and token path. All diagnostics go to stderr, keeping stdout machine readable.

Endpoints (all JSON, all token-gated except `GET /health`):

| Area | Endpoints |
| --- | --- |
| service | `GET /health`, `GET /capabilities`, `GET /usage`, `GET /audit` |
| settings | `GET/PUT /settings`, `POST/DELETE /settings/api-key` |
| perception | `POST /observe`, `GET /observation/latest` |
| tasks | `POST/GET/DELETE /tasks`, `GET /tasks/{id}`, `/verify`, `/confirm`, `/skip`, `/next`, `/previous`, `/watch`, `/pause`, `/resume`, `/cancel`, `/context`, `/save-workflow` |
| workflows | `GET /workflows`, `POST /workflows/import`, `GET /workflows/{id}/export`, `POST /workflows/{id}/start`, `DELETE /workflows/{id}` |
| privacy | `POST /privacy/enable`, `/privacy/stop`, `/privacy/purge` |
| speech | `GET /speech`, `POST /speech/speak` |

The HTTP handler also rejects requests whose `Origin` is not the app's own and
enforces a sliding-window rate limit, so a web page in the user's browser cannot
drive the local service.

### 2. Electron main process (`desktop/electron`)

Owns the assistant window, the overlay windows, global shortcuts, and the
`BackendClient` that starts and supervises the Python service. Every IPC handler
returns `{ ok, data }` or `{ ok: false, error }`; a backend failure surfaces in the
UI as a real error, never as an empty success.

Relevant files:

| File | Responsibility |
| --- | --- |
| `backend.ts` | spawn the service, read the handshake and token, HTTP client, path resolution for source and packaged layouts |
| `overlay.ts` | one click-through always-on-top window per monitor, drawing the highlight |
| `lib/geometry.ts` | pure maths: monitor choice, DIP conversion, clamping, label placement, docking |
| `main.ts` | window lifecycle, IPC handlers, shortcut registration, keeping the panel off the target |

The main process also moves the assistant panel out of the way when the panel
would cover the control it is pointing at — an instruction you cannot see the
target of is worthless.

### 3. React renderer (`desktop/renderer`)

A panel with the current instruction, the exact text to type (copyable), why the
step matters, the verification method and what was actually observed. Tabs for
the task list, saved workflows, settings and privacy. The overlay page is a
separate, deliberately tiny renderer that draws nothing but the highlight.

## The backend pipeline

```
observe ─▶ understand ─▶ highlight ─▶ instruct ─▶ verify ─▶ adapt
```

### Perception (`perception/`)

* `display.py` — session type (X11/Wayland), monitor list from `xrandr --listmonitors`,
  compositor detection, overlay support, and parsing of multi-monitor offsets.
* `capture.py` — capture backends in preference order: ImageMagick `import`,
  `xwd`, Wayland `grim`; every backend reports a reason when unavailable.
  Captures are byte buffers; nothing is written to disk unless the user asks for
  it, and scratch files are removed.
* `change.py` — dHash signature plus mean-difference ratio, so "did the screen
  change" is measured rather than assumed.
* `observer.py` — merges window listing, capture, accessibility and OCR into one
  `Observation`, with deduplication and source bookkeeping. It refuses to capture
  when screen sharing is off, when the user hit the emergency stop, or when the
  active window matches a privacy exclusion pattern (password managers by
  default).

### Accessibility and grounding (`accessibility/`, `grounding/`)

`atspi.py` speaks AT-SPI over D-Bus through `gdbus` (no PyGObject dependency) and
returns elements with label, role, value, checked state and a **screen-pixel**
rectangle. `matcher.py` scores candidates by label similarity, role compatibility,
geometry sanity and source confidence, refuses ambiguous matches, and records the
alternatives so the UI can explain itself. `geometry.py` converts capture pixels
to screen pixels for scaled and offset monitors and rejects rectangles that are
not on any known monitor.

### Planning (`task_engine/planner.py`, `task_engine/values.py`)

Two planners:

* **Local** (always available) — detects a form from the observed accessibility
  elements and builds a step per field in visual order, plus the submit button,
  each with a verification rule. For terminal work it produces read-only checks
  separated from the system-changing command. For anything else it *asks*
  instead of inventing steps.
* **Model** (optional) — used only when a provider is configured; its JSON is
  validated against `shared/schemas/plan.schema.json` before a single step is
  accepted, and `model_assumption` values are dropped rather than shown.

`values.py` resolves a field's value from (in order) what the user supplied, the
project's `.env`/config, a labelled derivation, or nothing. Sensitive labels never
read from config and never return a value to the UI.

### Verification (`verification/verifier.py`)

Rules are evaluated against a **new** observation, never the one that produced the
instruction:

| Rule | Evidence |
| --- | --- |
| `element_appears` / `element_disappears` | presence in the accessibility tree or OCR |
| `element_value_equals` | exact field value (presence only for secrets) |
| `element_checked` | checkbox/radio state from accessibility |
| `text_present` | expected text (or any of several) on screen |
| `window_title_matches` | active window title regex |
| `screen_changed` | measured frame difference above a threshold |
| `user_confirmation` | the user said so — recorded as such, never as a machine verification |
| `any_of` | one of several accepted outcomes |

Outcomes are `VERIFIED`, `FAILED` and `INCONCLUSIVE`. Unavailable evidence is
`INCONCLUSIVE`, never silently upgraded: when an application publishes no text
(as Chromium does in basic accessibility mode), the verifier says it cannot read
the field, and the engine compares the field's *pixels* to the baseline it took
before the interaction to tell "nothing typed yet" from "typed, but unreadable".

### Task engine (`task_engine/engine.py`)

A state machine per task (`CREATED → OBSERVING → PLANNING → READY_FOR_USER →
VERIFYING → … → COMPLETED`, with `RECOVERING`, `BLOCKED`, `PAUSED`, `CANCELLED`).
Invariants enforced centrally:

* `create_task` grounds the first step before returning, so the UI always has a
  real rectangle to draw.
* A task is pinned to the window it started on; focus changes do not move the
  guide to another application.
* `next_step` refuses to advance an unverified step; skipping records that no
  proof exists.
* After `MAX_STEP_ATTEMPTS` failures the task becomes `BLOCKED` with an
  explanation instead of repeating itself.
* The emergency stop cancels the task, clears cached frames and turns capture off.

`workflows.py` turns a finished task into a reusable, credential-free procedure
(with a fingerprint of its targets), checks it for drift against the live screen
before starting, and exports/imports it as validated JSON.

### Storage (`storage/db.py`)

SQLite in `~/.local/share/guidepilot/`, schema-versioned: tasks, steps, workflows,
audit events, model-call usage, preferences, and observation *metadata* only.
Pixels are never persisted. `purge_all` is the single "delete my data" path used by
the Privacy panel.

## Security model

* **Loopback only.** The service refuses to bind anything but `127.0.0.1`.
* **Per-run bearer token** in a `0600` file; held by the main process, never the
  renderer, never in browser storage, never logged.
* **Origin check** on every HTTP request; foreign origins get `403`.
* **Untrusted screen content.** Element text is wrapped in an explicitly labelled
  block before it reaches a model, injection markers are detected and reported,
  and the prompt instructs the model to treat that text as data.
* **Redaction.** Recognisable credentials are masked before anything is sent,
  including `api_key`/`clientSecret`-style keys — punctuation is normalised first so
  a key cannot slip through as `api_key`.
* **Secrets stay local.** The API key lives in the OS keyring (libsecret/keyring)
  with a `0600` file fallback, and is never returned by any endpoint.
* **No shell.** The main process cannot execute commands; the app never runs the
  commands it suggests, and classifies their risk (`inspect` / `modify` /
  `destructive`) so the user can see what they are about to run.
* **Privacy by default.** Capture is off until the user enables it; an emergency
  stop is one shortcut away; window-title exclusions default to password managers.

## Failure handling

| Situation | Behaviour |
| --- | --- |
| Accessibility unavailable | OCR (if installed) + pixels; instructions still shown but grounding confidence drops and the UI says so |
| Capture unavailable | observation records the reason; no instruction is invented from nothing |
| Target not found | step reports that it cannot find the control and offers to re-inspect |
| Verification inconclusive | task moves to `WAITING_FOR_ACTION`, the panel asks the user to confirm or correct |
| Repeated failure | task `BLOCKED` with an explanation after three attempts |
| Backend crash | main process reports it in the UI and restarts on next action |
| Sensitive window focused | observation skipped entirely; nothing is captured |

## Coordinates

Everything crossing a boundary is explicit about its space:

* Accessibility and window rectangles are **physical screen pixels**.
* Captures may be downscaled; `CaptureGeometry` carries the scale and both
  directions of the mapping.
* Electron works in **DIP**; `toLocalRect` divides by the display scale factor and
  subtracts the monitor origin, and `clampLocalRect` keeps a highlight inside the
  overlay window.

A rectangle that is not on a known monitor is rejected rather than drawn.
