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
Then in any Hermes surface: `/recorder on com.google.Chrome` (any app's bundle id; `us.zoom.xos`,
`com.apple.FaceTime`, `com.microsoft.edgemac`…) and ask away. `/recorder off` stops it.

`setup` creates, under `~/.hermes/plugin-data/hermes-recorder/`:
- a private Python environment for the helper (faster-whisper, pyobjc). Hermes's own environment isn't touched;
- **Hermes Recorder Helper.app**, generated on your Mac from Hermes's own Python and ad-hoc signed. It exists so
  macOS asks for permissions in its name, whoever started Hermes. Nothing compiled ships with the plugin;
- the transcription model (`base.en`, ~280 MB on disk), downloaded once from Hugging Face.

## Permissions
| Permission | Needed for | How it's granted |
| --- | --- | --- |
| Screen & System Audio Recording | hearing the apps you choose | switch on **Hermes Recorder Helper** in System Settings |
| Microphone | your own voice ("me" lines) | Allow on the prompt |
| Accessibility, Input Monitoring | teach mode only | switch on in System Settings when you enable teach mode |

`hermes recorder doctor` shows what's missing and links straight to the right Settings page. If Hermes's Python
is updated, `setup` rebuilds the helper and macOS asks again.

## Privacy and disclosure
- **Off by default**, per app, and password managers are never captured, even if listed.
- **Audio stays on your Mac.** It's transcribed locally and only text reaches Hermes. The live buffer is RAM
  only (30 min by default); `/recorder forget` clears it. The helper runs offline: only `setup` downloads.
- **What can leave your Mac:** if your Hermes model is cloud-hosted, the transcript lines the agent reads
  through `screen_recall` are sent to that model as part of the conversation.
- **A background helper process** runs only while capture is on. The macOS recording indicator shows while
  it captures, and `/recorder pause` turns it off.
- **Recording laws vary.** Some places require everyone on a call to consent. Tell people you're transcribing.

## Known issues
- Apps are captured only while they own a window (a macOS ScreenCaptureKit rule). Background-only audio
  apps can't be selected.
- Without headphones, your speakers can bleed into the mic. A filter drops "me" lines that repeat the app audio,
  but it's heuristic.
- When several apps are captured together, each line is attributed to the frontmost of them.
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
