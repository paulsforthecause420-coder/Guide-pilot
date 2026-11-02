# Setup

## 1. System dependencies

GuidePilot needs Python, an accessibility bus, a capture tool and a few runtime
libraries. On Debian/Ubuntu:

```bash
sudo apt install python3 at-spi2-core imagemagick x11-utils \
     libgtk-3-0 libnss3 libxss1 libasound2
# optional
sudo apt install tesseract-ocr espeak-ng xdotool
```

Fedora:

```bash
sudo dnf install python3 at-spi2-core ImageMagick xorg-x11-utils gtk3 nss libXScrnSaver
```

Arch:

```bash
sudo pacman -S python at-spi2-core imagemagick xorg-x11-utils gtk3 nss libxss
```

What each piece is for:

| Package | Why |
| --- | --- |
| `python3` (≥ 3.10) | the local guidance service |
| `at-spi2-core` | the accessibility tree — GuidePilot's primary source of controls |
| `imagemagick` (`import`) or `xwd` | screen and window capture |
| `tesseract-ocr` | optional OCR fallback for applications without accessibility |
| `espeak-ng` | optional spoken step summaries |
| `xdotool` | optional — only the acceptance test uses it |

Verify the environment from the app: the **Privacy** tab lists the session type,
the capture backend, accessibility and OCR availability, including the reason
when something is missing.

> If the accessibility tree looks empty, enable it for your session:
> `gsettings set org.gnome.desktop.interface toolkit-accessibility true`
> (GNOME), or ensure `at-spi-bus-launcher` is running. Chromium and Electron apps
> additionally need `--force-renderer-accessibility` (the bundled demo app sets
> this itself).

## 2. Install from source

```bash
git clone <this repository> guidepilot
cd guidepilot
npm install --prefix desktop
npm run build --prefix desktop      # or: ./scripts/dev-desktop.sh
```

Run it:

```bash
./scripts/dev-desktop.sh            # builds, then starts GuidePilot
./scripts/run-demo.sh               # in another shell: the demo target app
```

Add `--remote-debugging-port=9223` for a DevTools port, or set
`GUIDEPILOT_DEVTOOLS=1` to open the DevTools window at start-up.

## 3. Install the .deb

```bash
./packaging/build-deb.sh
sudo apt install ./packaging/build/guidepilot_1.0.0_amd64.deb
```

The package installs to `/opt/guidepilot` with a launcher in
`/usr/share/applications/guidepilot.desktop`, so **GuidePilot** appears in your
application menu. Remove it with:

```bash
sudo apt remove guidepilot
```

Your data (tasks, workflows, settings) lives in `~/.local/share/guidepilot` and
`~/.config/guidepilot` and is left in place; use **Privacy → Delete all local
data** before removing the package if you want it gone.

## 4. Portable build instead of installing

```bash
./packaging/build-appimage.sh
# single file if appimagetool is installed, otherwise a relocatable tarball:
tar xzf packaging/build/GuidePilot-1.0.0-x86_64.tar.gz
./GuidePilot.AppDir/AppRun
```

Nothing is written outside your home directory, so the portable build is safe to
try before installing.

## 5. Optional AI provider

GuidePilot works fully offline; the built-in planner needs no key. A model adds
free-form planning, chat and vision grounding for unusual applications.

1. **Settings → AI provider** → choose `openai_compat` (works with any
   OpenAI-compatible endpoint, including a local server) and set the base URL and
   model names.
2. Paste your key into **Settings → AI provider → API key**. It goes to the OS
   keyring (libsecret, or `keyring`); if neither exists it is written to
   `~/.config/guidepilot/secrets.json` with mode `0600` and the interface tells you
   that it is a file rather than an OS keyring.
3. The key is never returned by any endpoint or shown in the UI again — only
   "a key is configured" is exposed.

Settings that matter:

| Setting | Meaning |
| --- | --- |
| `ai.provider` | `none`, `openai_compat` or `mock` (deterministic, for tests) |
| `ai.base_url` | endpoint; `http://127.0.0.1:...` counts as local traffic |
| `ai.model_reasoning` / `model_vision` / `model_instruction` | per-role models |
| `ai.call_budget` | hard cap on model calls for safety |
| `ai.vision_grounding` | whether a model may help locate a control in a screenshot |
| `privacy.local_only` | refuse to send screen pixels to a non-local endpoint |

When the provider is remote and you enable screen sharing, the app requires an
explicit acknowledgement (`privacy.acknowledge_cloud_vision`) before it will send
anything.

## 6. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| "Screen sharing is off" | Expected on first run; enable it in the Privacy tab. |
| "No labelled elements" | Accessibility is unavailable for that app. Install `at-spi2-core`, enable toolkit accessibility, and start Chromium/Electron apps with `--force-renderer-accessibility`. |
| Highlight appears in the wrong place | Check for display scaling: `xrandr --listmonitors` should match your layout. **Settings → Screen observation → Monitor** lists the monitors GuidePilot detected. Report those values in a bug. |
| Highlight does not appear at all | On Wayland the overlay falls back to an in-panel indicator (the panel shows `overlay_mode`). X11 (or XWayland with X11 window placement) is needed for a native overlay. |
| Capture available but blank | Some compositors block window capture; `imagemagick`/`xwd` must be able to read the window. Try `import -window <id>` manually. |
| Gateway errors (`502`) | Provider unreachable or key rejected. The message comes from the provider; the app falls back to the local planner. |
| Voice commands do nothing | Voice is opt-in: install `espeak-ng` (output) or `whisper.cpp` + `arecord` (input) and enable it in Settings → Voice. |

Logs: `~/.local/share/guidepilot/logs/`. Service diagnostics go to stderr and are
forwarded into the main-process log.
