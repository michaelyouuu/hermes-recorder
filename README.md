# hermes-recorder

**Local screen and audio memory for [Hermes Agent](https://hermes-agent.nousresearch.com).**
Ask about any call or video you just watched, and (soon) teach Hermes a task by doing it once.

> Status: **in development** for the Nous Research Hermes Agent Plugin Hackathon. Milestone M1 (plugin
> skeleton, buffer, tools) is done; real capture arrives in M2. Not yet in the plugin catalog.

## What it will do
- **Ask mode:** while you watch or listen to anything (YouTube, Zoom, FaceTime, a podcast), the plugin keeps
  a rolling, local transcript of the apps you choose. Ask Hermes "what did he just say about X?" or
  "what time did we agree on?" from the HUD, chat or messaging.
- **Teach mode:** turn the last few minutes of clicks, typing and narration into a reusable `SKILL.md`, after
  the fact, without pressing record first.

## Tools
| Tool | Use |
| --- | --- |
| `screen_recall` | Read the recent transcript (`minutes`, `query`, `app`, `speaker`) |
| `recorder_status` | Is capture on, which apps, buffer length, permissions |
| `recorder_control` | `start`, `stop`, `pause`, `resume`, `set_apps` |
| `recorder_draft_skill` | Draft a skill from a recorded demonstration (M3) |

Commands: `/recorder on|off|pause|resume|status|forget [apps]`, `/teach [minutes]`, `hermes recorder status`.

## Privacy defaults
- **Off by default.** Nothing is captured until you start it and choose apps.
- **Per-app allowlist.** Password managers are never captured, even if listed.
- **Passwords never recorded.** macOS withholds keystrokes in password fields, and the plugin also drops
  anything typed into secure fields. Card numbers, one-time codes and API keys are masked.
- **RAM only.** The live buffer stays in memory; `/recorder forget` clears it.
- **Local transcription** with faster-whisper. Audio never leaves your Mac. If your Hermes model is
  cloud-hosted, the transcript lines the agent reads through `screen_recall` are sent to that model.

## Requirements
macOS 14+ on Apple Silicon, Hermes `>=0.22.7`. Tested on macOS 26.6 (M4).

## Development
```sh
uv venv --python ~/.hermes/tools/python-3.14.*/bin/python3 .venv
uv pip install --python .venv/bin/python -r pyproject.toml --extra dev
.venv/bin/python -m pytest                                              # unit tests
~/.hermes/tools/python-3.14.*/bin/python3 -I scripts/run_integration.py -q   # real Hermes loader
hermes plugins validate --install-deps .
```

## License
MIT
