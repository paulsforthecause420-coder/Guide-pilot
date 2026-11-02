# User guide

## First run

1. Start GuidePilot (application menu, or `./scripts/dev-desktop.sh`).
2. Open the **Privacy** tab and turn on **Screen sharing**. Nothing is captured
   before you do this — the app is unusable, by design, until you consent.
3. Open the window you want help with and leave it in front.
4. In the **Guide** tab, describe the task, for example *"Walk me through
   configuring this application"*.
5. Press **Start guidance**.

If you enabled an AI provider, GuidePilot may ask for an extra acknowledgement
before any screen content leaves your machine.

## What the panel shows you

The instruction card is the whole product. For the current step it shows:

* **The step and its title** — "Step 2 of 6".
* **Highlighted target** — the label, the role, the confidence, and whether the
  rectangle came from accessibility, OCR or a model. A box is drawn over that
  control on screen (on X11) or the coordinates are shown in the panel (when the
  session cannot host a click-through overlay).
* **Exact text** — the precise value to type, with a copy button.
* **Expected result** — what should be true afterwards.
* **Why** — one sentence on why the step matters.
* **Verification result** — *Verified on screen*, *Could not confirm* or *Not yet
  confirmed*, with the evidence that produced it.
* **Show details** — the raw evidence, alternatives considered, and notes.

GuidePilot never types for you. Read the instruction, do it yourself, and the
app checks your work.

## Verifying, confirming, skipping

| Button | What it does |
| --- | --- |
| **Verify step** | Re-reads the screen and checks the step's rules. |
| **I did it** | Records the step as *confirmed by you* (not machine-verified) and moves on. |
| **Skip** | Moves on and records that no proof exists. |
| **Look again** | Re-reads the screen and re-grounds the highlight — use it after resizing or switching windows. |
| **Previous / Pause / Stop task** | Navigate and control the task; cancelling keeps the task in history. |

Automatic checks also run while the screen changes, so a step is often marked
verified the moment your action lands. If GuidePilot cannot read a field (some
apps publish no text), it compares the field's appearance before and after and
tells you which of the two it sees — it will not claim a value it cannot read.

If three attempts fail, the task stops with an explanation instead of repeating
itself. Read what it says, press **Look again**, or start the task again from the
current screen.

## When it needs information from you

Fields it will not guess — names, URLs, tokens, IDs — are listed under **Needs
your input**. Type the value, and the instruction and highlight update
immediately. A password field is different: GuidePilot never shows, stores or
verifies the value; it tells you to type it privately and only the presence of
content is considered.

Under **Values I already know** on the start form you can supply several values
up front, so a whole form can be walked through without interruption. Values can
also be read from a project's `.env`/config file when you point the task at a
project directory.

## Privacy controls

* **Screen sharing switch** — capture is opt-in and can be turned off at any time.
* **Emergency stop** — `Ctrl+Shift+Escape` stops capture immediately, clears the
  cached frames and pauses the task. Nothing is read from your screen afterwards.
* **Exclusion patterns** — window titles/classes matching these are never
  captured. Password managers are excluded by default.
* **Redaction** — recognisable credentials are masked before any model call.
* **Delete all local data** — removes tasks, workflows, audit events and
  preferences; also clears cached frames.
* **No screenshots on disk** — frames live in memory only.

## Shortcuts

| Shortcut | Action |
| --- | --- |
| `Ctrl+Shift+G` | Show/hide the assistant |
| `Ctrl+Shift+Escape` | Emergency stop |
| `Ctrl+Shift+R` | Say the current step again / re-ground it |

These defaults live in the settings file (`ui.shortcuts` in
`~/.config/guidepilot/settings.json`, written on first run); edit them there and
restart the app to change them. Push-to-talk has its own field in
**Settings → Voice**.

## Saved workflows

When a task completes, **Save as workflow** stores the procedure (steps, targets
and verification rules — never values or secrets). Open the **Library** tab to:

* **Start** a workflow again — it re-checks that the targets still exist on the
  current screen and warns you about drift before starting;
* **Export** it as JSON, or **Import** a workflow file (see
  [`workflows/examples/`](../workflows/examples/) for two ready-to-import ones);
* **Delete** it.

Workflows are a good fit for repeating the same configuration in a different
account or on another machine.

## Voice

Voice is optional and off by default (**Settings → Voice**):

* **Spoken steps** need `espeak-ng` or `spd-say`; each new instruction is read
  aloud, errors too.
* **Voice input** (push-to-talk, `Ctrl+Alt+Space`) needs a local transcription
  engine (`whisper`, `whisper-cpp`/`whisper-cli`) and a recorder (`arecord`,
  `pw-record` or `ffmpeg`). Audio never leaves the machine.

## Appearance

**Settings → Window & appearance** covers theme, opacity, compact mode, always
on top, edge docking and whether the highlight pulses. If the panel would cover
the control it is pointing at, it moves itself to the side with the most room —
you can always move it back.

## FAQ

**Does it click for me?** No. It highlights and instructs; you act. Nothing in the
app can move your mouse or send keystrokes.

**Can it run shell commands?** No. For terminal tasks it shows the command, says
whether it is read-only, a modification or destructive, and you decide whether to
run it.

**Why did it say "Not yet confirmed"?** The evidence it needs was not available
or not there yet: the app may publish no text, the window may have changed, or the
action has not landed. **Look again** re-reads; **I did it** records your own
confirmation honestly as such.

**Does it work on Wayland?** The panel and guidance do; native click-through
overlays are an X11 feature, so on Wayland the panel shows the coordinates
instead. Capture uses `grim` where available.

**What is sent to a model?** Only if you configured one: the objective, the
element labels/roles/values of the current window (redacted), the window title
and — with vision grounding enabled and a non-local provider explicitly
acknowledged — a screenshot. The **Privacy** tab spells out what goes to which
endpoint for your current configuration, and switching to a remote provider
requires an explicit acknowledgement the first time.
