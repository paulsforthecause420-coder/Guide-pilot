# Feature status

This document separates what has been **verified by running it** from what is
implemented but depends on the machine or an external service, and from what is
deliberately not built. The verified column refers to the machine the project was
developed and tested on: X11 (XFCE), Python 3.14, Electron 33, the deterministic
demo application in `demo-app/`.

## Verified working

| Capability | Evidence |
| --- | --- |
| Window and monitor discovery, active-window tracking | X11 listing plus `xrandr` monitor geometry, used for every highlight |
| Screen and window capture | ImageMagick `import` backend, cross-checked with `xwd` presence |
| Accessibility element extraction | AT-SPI over D-Bus: labels, roles, values, checkbox state, screen rectangles |
| Form planning from the live screen | 6-step plan built from the demo form, values taken only from the user |
| Highlighting the exact control | Overlay rectangle matches the accessibility rectangle (checked by the acceptance run) |
| Exact-value instructions | Off-by-a-character values fail verification |
| Verification: field value, checkbox, expected text, window title, screen change | Full pytest coverage plus live checks |
| Honest inconclusive handling | Unreadable fields report "cannot read it back", with a pixel baseline to distinguish "nothing typed" from "typed but unreadable"; an empty accessibility read on a field whose pixels visibly changed is reported as inconclusive rather than as the user's mistake |
| Blocking instead of looping | Task becomes `BLOCKED` after three failed attempts, with an explanation |
| Task persistence and resume prompt | SQLite; the panel offers the unfinished task on next start |
| Saved workflows: save, start, drift check, export, import | Two example workflows in `workflows/examples/` import and run |
| Privacy: opt-in capture, exclusion patterns, emergency stop, data purge | Exercised by tests and the acceptance run |
| Local API security | Bearer token, loopback-only bind, foreign-origin rejection, rate limit — covered by tests against the real HTTP server |
| Secret handling | OS keyring/`0600` fallback; key never returned; sensitive fields never filled or echoed |
| Electron shell | Sandboxed renderer, context isolation, narrow IPC, click-through overlay, panel auto-moves off the target |
| Packaging | `.deb` and portable build produced and launched from the staged tree; the packaged app reports X11 capture + accessibility available |
| Offline operation | `ai.provider: none` throughout all tests; the local planner does the work |

## Implemented, environment-dependent

| Capability | Requirement | Behaviour when missing |
| --- | --- | --- |
| OCR fallback for apps with no accessibility | `tesseract-ocr` | Capability reports `available: false` with the reason; guidance relies on accessibility; not installed on the reference machine |
| Native click-through overlay | X11 (or XWayland with X11 placement) | Falls back to showing the target coordinates in the panel (`overlay_mode: in-window`) |
| Wayland capture | `grim` | Falls back to X11 tools when XWayland is present; otherwise capture reports unavailable |
| Spoken steps | `espeak-ng`, `espeak` or `spd-say` | Detected as available here via `espeak`; disabled by default in settings |
| Voice input (push-to-talk) | `whisper`/`whisper.cpp` + `arecord`/`pw-record`/`ffmpeg` | Reports "no local speech-to-text engine found"; recorder is present on the reference machine |
| AI planning, chat, vision grounding | An OpenAI-compatible endpoint and API key | Provider `none` keeps everything local; the gateway reports why it is unavailable instead of failing silently |
| Assisted paste into the focused field | `automation.assisted_actions_enabled` + `allow_paste`, plus a keystroke tool | Off by default; the copy button is always available instead |

## Deliberately not built

| Not implemented | Why |
| --- | --- |
| Clicking or typing *for* the user as a default behaviour | The product is an instructor: the user acts, GuidePilot verifies. Auto-actions would also make verification meaningless |
| Running shell commands it suggests | The app classifies risk and shows the command; execution stays with the user |
| Cloud speech or cloud storage of screen frames | No such backend exists in the code |
| Persisting screenshots | Frames are memory-only by design; retention is a settings flag that defaults off |
| Background daemon / tray-only mode | The panel is the product surface; closing it stops the service |
| Automatic updates | Not implemented; install from a package or rebuild |

## Known limitations

* Applications that publish neither accessibility data nor readable text can be
  highlighted but their field contents cannot be verified; the app says so rather
  than guessing.
* The live acceptance run stops at step 1 on this machine (it never claims an
  unverified pass). Measured with `GUIDEPILOT_DEBUG_A11Y=1` and
  `GUIDEPILOT_DEBUG_VERIFY=1`, two things are true at the same time inside the
  running product, while a standalone client reads the same field perfectly:
  Chromium answers `Text.GetText` with an empty string for an entry that visibly
  contains text, and the cropped field pixels are a flat block, because
  GuidePilot's own highlight is drawn over the field it is asking about. Running
  the identical launch and read outside GuidePilot yields the real value, so the
  trigger is the overlay window sitting on top of the target. Until the overlay
  is hidden for the measurement (or OCR reads the text back with
  `tesseract-ocr`), a text step on a Chromium target can only be reported as
  "cannot confirm", which is what the run reports.
* GTK/Qt applications publish no accessible tree on this machine at all (the GTK
  ATK bridge module is not installed), so only Chromium/Electron targets were
  available for live testing.
* Canvas-based interfaces (games, some design tools) have no elements to ground
  on; only OCR text can help, and only when `tesseract` is installed.
* The local planner understands forms and terminal-style work. Anything else is
  answered with a clarifying step unless a model provider is configured.
* Wayland users get guidance and coordinates, not a native overlay.
* Multi-monitor highlighting is computed and unit tested, but the reference
  machine has a single monitor; treat exotic layouts (negative offsets, mixed
  scaling) as needing verification on your hardware.
