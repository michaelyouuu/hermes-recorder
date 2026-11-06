# hermes-recorder

**Local screen and audio memory for [Hermes Agent](https://hermes-agent.nousresearch.com).**
Ask about any call or video you just watched, and (soon) teach Hermes a task by doing it once.

> Status: **in development** for the Nous Research Hermes Agent Plugin Hackathon. **Ask mode works**
> (M2); teach mode is next. Not yet in the plugin catalog.

## What it does
- **Ask mode (works now):** while you watch or listen to anything (YouTube, Zoom, FaceTime, a podcast), the plugin keeps
  a rolling, local transcript of the apps you choose. Ask Hermes "what did he just say about X?" or
  "what time did we agree on?" from the HUD, chat or messaging.
- **Teach mode (in progress):** turn the last few minutes of clicks, typing and narration into a reusable `SKILL.md`, after
  the fact, without pressing record first.

## Tools
| Tool | Use |
| --- | --- |
| `screen_recall` | Read the recent transcript (`minutes`, `query`, `app`, `speaker`) |
| `recorder_status` | Is capture on, which apps, buffer length, permissions |
| `recorder_control` | `start`, `stop`, `pause`, `resume`, `set_apps` |
| `recorder_draft_skill` | Draft a skill from a recorded demonstration (M3) |

Commands: `/recorder on|off|pause|resume|status|forget [apps]`, `/teach [minutes]`, `hermes recorder status`.

## Requirements
- macOS 14+ on Apple Silicon (the microphone needs macOS 15+). Tested on macOS 26.6 with an M4.
- Hermes `>=0.22.7`.

## Setup
```sh
hermes plugins install michaelyouuu/hermes-recorder   # once it's in the catalog
hermes plugins enable hermes-recorder
hermes recorder setup     # builds the helper, downloads the model, asks for permissions
hermes recorder doctor    # every check should be ✓
```
Then in any Hermes surface: `/recorder on facetime` (or `zoom`, `edge`, `chrome`, `"Microsoft Teams"`, or any app's
bundle id such as `com.google.Chrome`) and ask away. `/recorder off` stops it.

## Calls
Each app's audio is captured with a macOS **process tap** on exactly the processes that play it, so calls work
even when the audio comes from a helper or system process, and you keep hearing everything normally:
- **FaceTime and Phone** (iPhone calls on your Mac): macOS plays these through its call service, `avconferenced`.
  Choosing FaceTime or Phone captures **every call routed through your Mac** (FaceTime audio and video, and
  iPhone calls relayed to the Mac), not just one conversation.
- **Zoom, Teams, Slack, Discord, WhatsApp, Webex, Signal**, including their audio helper processes.
- **Browser calls** (Google Meet, etc.): choose the browser.

The other side shows up as "them" lines and your microphone as "me". Headphones give the cleanest split.

`setup` creates, under `~/.hermes/plugin-data/hermes-recorder/`:
- a private Python environment for the helper (faster-whisper, pyobjc). Hermes's own environment isn't touched;
- **Hermes Recorder Helper.app**, generated on your Mac from Hermes's own Python and ad-hoc signed. It exists so
  macOS asks for permissions in its name, whoever started Hermes. Nothing compiled ships with the plugin;
- the transcription model (`base.en`, ~280 MB on disk), downloaded once from Hugging Face.

## Recorder panel and hotkey
`hermes recorder setup` offers a **⌃⌥R** hotkey (or run `hermes recorder hotkey on`). Press it in any app and
the **recorder panel** pops up over that app. It's Q&A only (no scrolling transcript):
- ask "what did he just say about X?" or "what time did we agree on?" over the last 2, 5, 10 or 30 minutes.
  Your Hermes model answers from the transcript alone (a quick lookup, not a full agent turn; for that, ask
  Hermes in chat or the HUD). It knows which lines came from the app and which from your microphone;
- Pause/Resume and Stop/Start buttons.

If capture is off, the press also starts recording (Tink). It records the apps you last chose, and the very first
time, **the app in front of you**. Press ⌃⌥R again (or Esc) to hide the panel; recording continues until you press
Stop. The panel doesn't take focus from your call or video, floats on every Space, and remembers where you put it.
- The hotkey is a small login item that listens for that one key combination. It needs no permissions, captures
  nothing itself, and `hermes recorder hotkey off` removes it. Change the key with the `hotkey` setting
  (e.g. `cmd+shift+r`), then run `hermes recorder hotkey on`.
- Right after Hermes restarts, the recorder starts listening once Hermes has handled its first message or
  chat. Until then the hotkey plays Basso and the panel says so.
- Prefer your own launcher? Bind `hermes recorder toggle` in Shortcuts or Raycast instead.

## Permissions
| Permission | Needed for | How it's granted |
| --- | --- | --- |
| Screen & System Audio Recording | hearing the apps and calls you choose | switch on **Hermes Recorder Helper** in System Settings |
| Microphone | your own voice ("me" lines) | Allow on the prompt |
| Accessibility, Input Monitoring | teach mode only | switch on in System Settings when you enable teach mode |

`hermes recorder doctor` shows what's missing and links straight to the right Settings page. If Hermes's Python
is updated, `setup` rebuilds the helper and macOS asks again.

## Privacy and disclosure
- **Off by default**, per app, and password managers are never captured, even if listed.
- **Audio stays on your Mac.** It's transcribed locally and only text reaches Hermes. The live buffer is RAM
  only (30 min by default); `/recorder forget` clears it. The helper runs offline: only `setup` downloads.
- **What can leave your Mac:** if your Hermes model is cloud-hosted, the transcript lines the agent reads
  through `screen_recall`, and the lines from the window you ask about in the recorder panel, are sent to that model.
- **A background helper process** runs only while capture is on. The macOS recording indicator shows while
  it captures, and `/recorder pause` turns it off.
- **Recording laws vary.** Some places require everyone on a call to consent. Tell people you're transcribing.

## Known issues
- Process taps need macOS 14.2+. On macOS 14.0–14.1, and always for **Safari** (its audio plays in a process
  shared with other apps), capture falls back to ScreenCaptureKit, which only sees apps that own a window.
- Without headphones, your speakers can bleed into the mic. A filter drops "me" lines that repeat the app audio,
  but it's heuristic.
- Safari lines (ScreenCaptureKit fallback) are attributed to the frontmost captured app when several are captured.
- Video isn't captured, so DRM doesn't matter. Audio from protected video is transcribed normally.

## Development
```sh
uv venv --python ~/.hermes/tools/python-3.14.*/bin/python3 .venv
uv pip install --python .venv/bin/python -r pyproject.toml --extra dev
.venv/bin/python -m pytest                                              # unit tests
~/.hermes/tools/python-3.14.*/bin/python3 -I tests/integration/run_in_hermes.py -q   # real Hermes loader
HERMES_RECORDER_E2E=1 .venv/bin/python -m pytest tests/test_helper_e2e.py   # real capture (plays audio)
hermes plugins validate --install-deps .
```

## License
MIT
