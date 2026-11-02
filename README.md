# GuidePilot

A screen-aware desktop instructor for Linux. It floats above the application you
are working in, finds the control you need, highlights it on screen, tells you
exactly what to type, and **checks that the step really worked** before moving on.

GuidePilot does not take over your mouse or keyboard. You do the clicking; it
tells you where and verifies the result. That keeps you in control and keeps the
verification honest — a step is only marked done when evidence on screen says so.

![GuidePilot icon](assets/guidepilot.png)

## What it actually does

* **Sees the window you are in.** The accessibility tree (AT-SPI over D-Bus) is
  the primary source; screen pixels are used for change detection, highlight
  geometry and — when `tesseract` is installed — an OCR fallback.
* **Grounds every instruction in a real control.** Labels, roles and rectangles
  from the live tree; no instruction is issued for a control it cannot point at.
  Ambiguous matches are refused rather than guessed.
* **Highlights the target.** A click-through always-on-top overlay draws the box
  and label over the control, on the right monitor, with display scaling and
  multi-monitor offsets handled.
* **Tells you exactly what to type.** Values come from what *you* supplied or
  from your project's config file. GuidePilot will not invent a name, URL, token
  or identifier: unknown fields become a question.
* **Verifies from evidence.** Field contents, checkbox state, expected text,
  window titles and measured screen change. If the evidence is unavailable it
  says "I can't confirm that" instead of pretending the step passed.
* **Adapts instead of looping.** After repeated failures the task stops with an
  explanation rather than repeating the same instruction forever.
* **Keeps your secrets off the wire.** Password/secret fields are never filled,
  never displayed and never sent; screen content is wrapped as untrusted data
  before any model sees it, and obvious credentials are redacted.
* **Works offline.** The local planner needs no API key. An AI provider is
  optional and only used when you configure one.

## Requirements

* Linux with X11 (XWayland works for the panel; overlay placement is native on
  X11), `python3` ≥ 3.10, AT-SPI enabled (`at-spi2-core`).
* ImageMagick (`import`) or `xwd` for captures; `xdotool` is only needed by the
  acceptance test, never by the app itself.
* Optional: `tesseract-ocr` (OCR fallback), `espeak-ng` (spoken steps),
  `whisper.cpp` + `arecord` (voice input).

## Quick start from source

```bash
npm install --prefix desktop          # Electron, React, Vite, TypeScript
./scripts/dev-desktop.sh              # builds and starts GuidePilot
./scripts/run-demo.sh                 # optional: the demo app to practise on
```

Then, in the panel: **Privacy → enable screen sharing**, open the application you
want help with, and type what you are trying to do.

## Packages

```bash
./packaging/build-deb.sh        # packaging/build/guidepilot_<version>_amd64.deb
./packaging/build-appimage.sh   # AppImage, or a portable directory tarball
```

See [docs/SETUP.md](docs/SETUP.md) for installation, removal and distro notes.

## Tests

```bash
./scripts/test.sh          # 105 backend tests, 19 desktop tests, type checks
./scripts/acceptance.sh    # live end-to-end run on the real desktop
```

The acceptance run drives the real application with real mouse and keyboard
input on a deterministic demo app and requires every step to be verified from
screen evidence. See [docs/TESTING.md](docs/TESTING.md).

## Documentation

| Document | Contents |
| --- | --- |
| [docs/SETUP.md](docs/SETUP.md) | installing, dependencies, AI provider, uninstall |
| [docs/USER-GUIDE.md](docs/USER-GUIDE.md) | using GuidePilot day to day |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how perception, grounding, guidance and verification fit together |
| [docs/DEVELOPER.md](docs/DEVELOPER.md) | code layout, conventions, extending it |
| [docs/TESTING.md](docs/TESTING.md) | test suites and the acceptance scenario |
| [docs/FEATURE-STATUS.md](docs/FEATURE-STATUS.md) | what is verified, what is environment-dependent, what is not implemented |

## Repository layout

```
backend/guidepilot/    Python service: perception, accessibility, grounding,
                       planning, verification, storage, local HTTP API
desktop/electron/      Electron main process, overlay windows, IPC, backend supervisor
desktop/renderer/      React panel, instruction card, settings, privacy, library
shared/schemas/        JSON Schemas for plans, guidance, workflows, grounding
demo-app/              deterministic target application for acceptance testing
tests/                 live desktop acceptance scenario
workflows/examples/    importable example workflows
packaging/             .deb and AppImage/portable builders, launcher, icon
docs/                  documentation
```

## Licence

MIT.
