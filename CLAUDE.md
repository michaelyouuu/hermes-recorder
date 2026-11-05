# CLAUDE.md: Hermes Recorder (Hermes Agent Plugin Hackathon entry)

Read this whole file before writing any code. It is the scope, the research behind it, the verified facts about the Hermes plugin system, the architecture, and the rules for how to build. When something here conflicts with your assumptions about Hermes, this file wins, because every API fact below was verified against the real `hermes-agent` source on 2026-11-05 (commit `3093bc3117d49e885d584420af6920473767f5e7`). When something here conflicts with the current Hermes docs, re-check the docs and tell me.

---

## 0. How to work in this repo

1. **Plan before code.** For every milestone, write a short plan (files to touch, interfaces, tests) and show it to me before implementing. Use plan mode for anything touching more than two files.
2. **Spikes before features.** Milestone M0 (Section 11) is three throwaway spikes on my Mac. Do not build plugin features until they pass, because their results can change the design.
3. **Small, verified steps.** After each change: run the unit tests, run `hermes plugins validate .`, and for anything touching capture, run it for real on macOS and report what you observed (not what you expect).
4. **Never claim something works without running it.** If you cannot run it (needs a permission prompt, needs my Mac, needs a UI click), say exactly what I need to do and what output to send back.
5. **Commit in small units** with clear messages. One concern per commit. Never commit secrets, recordings, transcripts, model weights or `.pyc` files.
6. **Ask me before:** adding a dependency not listed here, changing a public tool name or schema, widening what is captured, sending any data off-device, or cutting a milestone.
7. **Keep this file current.** When a decision is made or a fact changes, update the relevant section and the Decision Log (Section 16) in the same commit.

---

## 1. Mission and constraints

**Goal:** win the Nous Research Hermes Agent Plugin Hackathon.

| Item | Detail |
| --- | --- |
| Deadline | **5 PM EST, Tuesday Nov 11, 2026** |
| Entry | The plugin itself, listed via a PR to the Hermes plugin catalog |
| Also required | Demo video on X tagging @NousResearch (under 90 seconds, showing it working), then both links in the Typeform |
| Judging | Usefulness, reliability, polish, creativity. Judges **favor plugins that do more than a prompt or a skill could** |
| Prizes | 1st $10k, 2nd $5k, 3rd $2.5k, 4th $1.5k, 5th $1k, 5 runners up |
| Review | Catalog checks run when a maintainer starts them; entries reviewed every few days. **Open the PR early** (target Nov 9) so failing checks can be fixed before the deadline |

---

## 2. What we are building

**Working name:** `hermes-recorder`. One local capture helper gives Hermes eyes and ears on the whole Mac desktop, and two features share it.

### Ask mode (must ship)
While I watch or listen to anything (a YouTube lecture, a Zoom or FaceTime call, a Twitch stream, a podcast), the plugin keeps a rolling, local transcript of the chosen apps' audio. From the Hermes HUD (Cmd+Shift+H), chat or Telegram I can ask "what did he just say about X?", "summarize the last 10 minutes" or "what time did we agree on?" and Hermes answers from the buffer.

### Teach mode (target, the differentiator)
The same buffer also records UI events (clicks, focus changes, typed values with secrets masked) and my spoken narration. I can say "turn what I just did into a skill" **after** doing a task, with no need to press record first. The plugin drafts a `SKILL.md` (goal, inputs that vary, steps, success check), asks me 2 or 3 edge-case questions, saves it with Hermes's `skill_manage`, and Hermes can later replay it with `computer_use` and the browser tools, pausing for approval before anything irreversible.

### Why this wins (the pitch)
- Hermes can already **learn** (`/learn`, `skill_manage`) and **replay** (`computer_use`, browser tools). This plugin adds the missing piece: **seeing and hearing the whole desktop, locally**.
- The HUD today only knows the app name and window title under it (`read_window_below`: metadata only, never pixels, no audio, no history). We add audio, recent history and UI events.
- **Retroactive teaching** (teach from the rolling buffer after the fact) is something none of the competitors do. They all require pressing record first.
- Open source, local-first, any model, works in CLI, Desktop and messaging. Competitors are vendor-locked.

### Non-goals (do not build)
- Windows or Linux capture (macOS only; mention as future work).
- Remote gateways (capture helper runs on the same Mac as Hermes).
- Video or screen recording to disk. Snapshots are optional, low rate, in RAM, and off by default.
- Joining meetings as a bot (that is the core `google_meet` plugin's approach; we capture locally instead).
- Cloud transcription by default. Local only unless the user opts in.
- Desktop-only cosmetics (themes, sidebar tweaks). They are the most crowded, least rewarded corner of the catalog.

### Open decision (confirm with me at the start)
Which mode leads the demo and the README. **Default recommendation:** ship ask mode as the solid base (M1 to M2), build teach mode as the headline (M3 to M4), and lead the demo with teach mode if M4 passes its acceptance checks by Nov 9 at noon; otherwise lead with ask mode and show teach as a preview.

---

## 3. Research context (why the scope is shaped this way)

Full reports live in the claude.ai Project "Hermes Agent Hackathon" (`research/marketplace-landscape.md`, `research/workflow-recording-market.md`, `research/recorder-feasibility.md`). Key points:

### Marketplace landscape (as of Nov 4, 2026)
- 976 catalog plugins from 667 maintainers; 947 community, 20 official, 9 verified. Median plugin has 1 GitHub star; 351 of 950 have zero.
- Categories: tools 350, desktop 217, models 90, memory 76, general 62, automation 56, web 50, platform 49, voice 26.
- Biggest independent hits give Hermes a **new real-world capability or surface**: `phone-harness` (drives a real phone, 4.3k stars), `letsfg` (books flights, 2.2k), `gadget` (ESP32 devices), `omh` (orchestration, 5.1k).
- What users reward: visibility (what the agent did and cost), reach (works with their apps), recall, approval-gated actions.
- What users punish: latency, setup burden, cloud dependence, fragmented installs.

### Meeting and screen coverage today
- Core `google_meet`: a Playwright bot joins Meet and scrapes captions. Meet URLs only.
- Core `teams_pipeline`: Graph API transcripts after the meeting. No live capture.
- `meeting-overlay` (community, 0 stars): macOS mic and system audio to an Obsidian vault; declares **no tools or hooks**, so the agent cannot use it.
- Nothing provides a general rolling screen and audio context the agent can query.

### Workflow recording market
- Codex Record & Replay (OpenAI, Jun 18 2026, macOS only, 30 min cap), Claude Record a Skill (Anthropic, Jul 21 2026, narration), Microsoft Skill Recorder (Aug 2026, open source, local Whisper narration). All three turn a recording into a **written SKILL.md**, not a click macro, and replay by intent.
- Others: open-record-replay (accessibility events), Workflow Use (browser, alpha), classic RPA (UiPath, Power Automate: brittle selectors, maintenance estimated 60 to 70% of effort), Scribe and Tango (docs only, no execution).
- Hermes has `workflow-record` (Hermes Labs, unsupported): one Chromium tab over CDP, terminal-started, no narration, no desktop apps. Masks secrets as `{SECRET:name}`; copy that pattern.
- Known complaints about AI recorders and how we answer them:

| Complaint | Our answer |
| --- | --- |
| Recordings only capture the happy path | After drafting, ask 2 or 3 edge-case questions and write answers into the skill |
| Narration is vague ("I click here, then there") | Ask "why" at decision points in the draft review instead of relying on free talk |
| Inputs change between runs | Detect variable values (amounts, dates, names) and have the user confirm them as inputs |
| Long tasks replay unreliably | Encourage short demos; offer a test run on a new input right after saving |
| Vendor lock-in | Plain SKILL.md, any model, any Hermes surface |
| Privacy of screen capture | Local only, RAM buffer, per-app opt-in, masked secure fields, visible indicator, review before saving |
| Forgetting to press record | Retroactive teach from the rolling buffer |

---

## 4. Verified facts (smoke tests run 2026-11-05)

These were tested on a real Hermes install from source, not inferred from docs.

### Plugin system (all PASS)
| Test | Result |
| --- | --- |
| `hermes plugins validate` on a skeleton with 2 tools, 2 hooks, a slash command, a CLI command, `config_schema`, a bundled skill, `desktop/plugin.js` and `dashboard/plugin_api.py` | All 16 checks passed |
| Same skeleton plus capture-helper code (`CGEventTapCreate`, Accessibility calls, `subprocess.Popen`, loopback socket, ScreenCaptureKit) | **Security scan: safe.** No core override, desktop surface OK |
| `validate --install-deps` with `faster-whisper` + pyobjc deps using `; sys_platform == 'darwin'` markers | Passed on Linux CI in 54 s (markers keep pyobjc off Linux) |
| `hermes plugins doctor` | Discovery, manifest, import, registration OK |
| Tools via `registry.dispatch` | `screen_recall` and `recorder_status` returned correct JSON |
| `pre_llm_call` returning `{"context": "..."}` | Injected as expected |
| `pre_llm_call` kwargs received | `session_id, task_id, turn_id, user_message, conversation_history, is_first_turn, model, platform, parent_session_id, sender_id, telemetry_schema_version`. **No HUD/surface flag.** |
| `on_session_end` kwargs | `session_id, completed, interrupted, model, platform, telemetry_schema_version` |
| `register_command("teach", ...)` | Handler called with raw args |
| `register_cli_command("recorder", ...)` | `hermes recorder status` ran |
| `plugin_db` / `plugin_data_dir` | Created under `<HERMES_HOME>/plugin-data/hermes-recorder/` |
| `ctx.get_config` default from `config_schema` | Returned 30 |
| `ctx.llm`, `ctx.dispatch_tool` | Present |
| Background thread stopped by `ctx.on_unload` | Unload returned True; thread joined |
| `asyncio` loop during `register()` | **None.** `ctx.spawn_task` cannot be called from `register()` |
| `skill_manage` create (with built-in tools loaded) | Wrote `<HERMES_HOME>/skills/<name>/SKILL.md` |
| Root `__init__.py` importing a subpackage (`from .hermes_recorder import tools`) | Loads and validates |
| Discovery with declared deps not installed | Plugin still loads with a warning telling the user to run `hermes pm repair`; handle missing imports gracefully |

### Skill lint rules (from `skill_manage` warnings)
A saved skill should have frontmatter `name`, `description` (house style: 60 characters or fewer), `version`, `author`, `license`, `metadata.hermes.tags`, `metadata.hermes.related_skills`, and a `## When to Use` section near the top.

### Plugin host isolation (important)
`validate` reports how the plugin behaves under `plugins.isolation: host` (default is `in_process`, but users and multi-profile gateways can run plugins in a separate host process). Under host mode:
- **Websocket routes are refused (HTTP 501).** Do not rely on `ctx.socket`; the Desktop half must poll `ctx.rest` (every 1 to 2 s) or listen to gateway events.
- Dashboard API responses are buffered; handlers time out after 60 s.
- `ctx.register_cli_command()` is skipped in the host.
- Module globals are not shared with the gateway. Keep state in the plugin, accessed through our own tools and API routes.

### macOS frameworks via pyobjc 13.0 (wheels inspected, cp313 and cp314 universal2)
All present: ScreenCaptureKit (`capturesAudio`, `excludesCurrentProcessAudio`, `captureMicrophone`, `SCStreamOutputTypeAudio`, `SCStreamOutputTypeMicrophone`, `SCContentFilter ... includingApplications`, `SCScreenshotManager`), Quartz (`CGEventTapCreate`, `CGPreflightListenEventAccess`, `CGRequestListenEventAccess`, `CGPreflightScreenCaptureAccess`, `CGRequestScreenCaptureAccess`), HIServices (`AXObserverCreate`, `AXIsProcessTrustedWithOptions`, `AXUIElementCopyElementAtPosition`, `kAXFocusedUIElementChangedNotification`, `AXSecureTextField`), CoreAudio process taps (`AudioHardwareCreateProcessTap`, `CATapDescription`, aggregate devices), CoreMedia (`CMSampleBufferGetDataBuffer`, `CMBlockBufferCopyDataBytes`, `CMAudioFormatDescriptionGetStreamBasicDescription`), AVFoundation (`AVAudioEngine`, `requestAccessForMediaType`).

### Transcription
- Core Hermes pins `faster-whisper==1.2.1` in its voice extras and **excludes Intel Macs** (no ctranslate2 wheels). Its own transcription functions are **not** documented plugin API; ship our own dependency instead of importing `tools.transcription_*`.
- Wheels resolve on Apple Silicon for Python 3.13 and 3.14 (`ctranslate2 4.8.5`, `onnxruntime 1.31.1`, `av 19.0.2`; `onnxruntime` and `av` wheels target **macOS 14+**). `mlx-whisper 0.4.3` is available as an Apple Silicon fallback.
- **Measured in S2 (M4):** `base.en` runs at RTF 0.063, about 3% of the machine (see M0 findings below). Model weights download from Hugging Face on first load (tiny.en 149 MB, base.en 282 MB, small.en 927 MB on disk).
- Hermes's own interpreter is Python 3.14 (`.python-version`).

### Target platform
**macOS 14+ on Apple Silicon.** Declare `platforms: [macos]` and say so in the README.
M0 spikes actually ran on **macOS 26.6.2, Apple M4, Hermes v0.22.7 (`b200f5bc60e3`)**, newer than the commit above. Re-run the plugin smoke checks in M1, and only claim the OS versions we test.

### M0 spike findings (2026-11-05, details in `spikes/RESULTS.md`)
- **Permission attribution:** the gateway runs as a one-off launchd job via `/usr/bin/osascript` (`hermes_cli/gateway_launchd_runtime.py`), and **Desktop chats run tools in that gateway** too. A plain child of the gateway is attributed to **osascript**, which is unusable (granting osascript Screen Recording or Input Monitoring would hand them to every AppleScript). A helper launched through LaunchServices as its own `.app` is responsible for itself no matter who launches it, and all four permissions granted to it persist across fresh launches.
- **Audio:** SCK app audio arrives as the requested 16 kHz mono float32. **Mic (`captureMicrophone`) ignores the requested rate and arrives at 44.1 kHz**, so resample it. Per-app filtering works with browsers (Edge).
- **Transcription on M4:** `base.en` int8 takes 0.30 s per 5 s window (RTF 0.063), about 32% of one core (≈3% of the machine), WER about 3% vs `small.en`. The helper's RSS is about 0.7 GB, almost all of it the model. The 1 s window overlap duplicates phrases (merge them); the first window can hallucinate a filler word (drop it).
- **Events:** AX names native and Chromium controls well (Chromium needs `AXManualAccessibility = true` on the app). **macOS Secure Event Input withholds all key events from event taps while a password field is focused**, so password characters are unreachable. Div-heavy sites yield unlabeled `AXGroup`s. Reading a field's whole AXValue over-captures; record deltas. A single AX call can block for up to its 0.25 s messaging timeout.
- Accessibility and Input Monitoring must be switched on in System Settings (no usable inline prompt). Deep links: `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility` and `?Privacy_ListenEvent`.
- `open --stdout ...` fails with LaunchServices `-10810`. One gateway-launched run silently produced no output, so the manager must verify startup.

---

## 5. Architecture

```
+-------------------------------------------------------------------------------+
| YOUR MAC: Hermes Recorder Helper.app (own TCC identity, launched via `open`)   |
|  App audio (ScreenCaptureKit, per app) | Microphone (narration, own stream)   |
|  UI events (Quartz event tap + Accessibility) | Snapshots (optional, off)     |
|  Local transcription (faster-whisper + VAD)                                   |
+--------------------------------------+----------------------------------------+
                                       | newline-delimited JSON over a Unix socket
                                       v
+-------------------------------------------------------------------------------+
| HERMES RECORDER PLUGIN (inside the Hermes gateway, or the plugin host)        |
|  Rolling buffer: last N minutes in RAM; saved clips in SQLite (plugin_db)     |
|  Tools: screen_recall, recorder_status, recorder_control, recorder_draft_skill|
|  Hooks: pre_llm_call (one-line "capture is live" note), on_session_end        |
|  Commands: /recorder, /teach ; CLI: hermes recorder setup|doctor|status       |
|  Compiler: buffer slice -> normalized steps -> ctx.llm draft -> SKILL.md      |
|  dashboard/plugin_api.py: status + recent transcript over HTTP (polled)       |
+---------+----------------------------+------------------------+---------------+
          | tools + note               | skill_manage           | ctx.rest (poll)
          v                            v                        v
+---------------------------+  +----------------------+  +------------------------+
| HERMES CORE: agent turn   |  | Skills library       |  | Desktop half           |
| HUD, chat, Telegram       |  | SKILL.md, editable,  |  | desktop/plugin.js:     |
| replays via computer_use  |  | loaded on replay     |  | status chip, pause,    |
| + browser + approvals     |  |                      |  | per-app toggles, pane  |
+---------------------------+  +----------------------+  +------------------------+
```

### Components and responsibilities

**Capture helper (`helper/`)**: its own Python process so a crash or permission failure never takes the agent down. It runs inside **`Hermes Recorder Helper.app`**, a bundle that `hermes recorder setup` generates locally: an Info.plist (bundle id `io.github.michaelyouuu.hermes-recorder.helper`, `LSUIElement`, mic/audio/screen usage strings) plus a copy of Hermes's own python interpreter as the executable, ad-hoc signed with `codesign -s -`. It lives under `plugin_data_dir`. Nothing compiled is committed (catalog rule 17). It is launched with `open -n --env PYTHONHOME=... --env PYTHONPATH=... -a <app> --args helper/main.py --socket <path> --token <t>` so macOS treats it as its own app; permissions are granted to "Hermes Recorder Helper" once. Rebuild it when the interpreter's hash changes (the user then re-grants), and have doctor detect that.
- `capture_audio.py`: ScreenCaptureKit stream with `capturesAudio=True`, `excludesCurrentProcessAudio=True`, filter including only allowed apps, 16 kHz mono. Microphone via `captureMicrophone` (macOS 15+) or `AVAudioEngine` (macOS 14) as a **separate stream** so transcript lines are labeled "me" vs "them". Mic arrives at 44.1 kHz: resample to 16 kHz.
- `capture_events.py`: listen-only `CGEventTapCreate` for clicks and key-downs; resolve the element under the pointer or the focused element with Accessibility (`role`, `title`, `subrole`, app, window title). **If `subrole == AXSecureTextField`, never record characters**; emit `{SECRET}` (macOS Secure Event Input already withholds those keys; this is the second guard). Coalesce key-downs into "typed into field X" events: single-line fields record their short final value, multi-line fields record only the **delta** (inserted text, deleted count) against a snapshot taken before the first key, never the whole field. The tap callback only enqueues; AX look-ups run on a worker thread. Climb from static text or groups to the actionable ancestor (link, button); set `AXManualAccessibility` on Chromium apps; fall back to DOM id or nearby text for unlabeled `AXGroup`s; record selection ranges (not contents).
- `capture_frames.py` (optional, off by default): `SCScreenshotManager` snapshot on app switch or significant change, downscaled, RAM only.
- `transcribe.py`: faster-whisper with VAD, 5 s windows with ~1 s overlap, model from config (`base.en` default, verified in S2). Merge overlapping words at window edges; drop very short first-window segments and low-confidence ones (`avg_logprob`, `no_speech_prob`).
- `main.py`: connects to the plugin's socket, sends `hello` with versions and permission status, streams events, obeys `pause`, `resume`, `set_apps` and `stop` commands, and exits when the socket closes (no orphans). Launched via `open`, there is no stdin or stdout pipe: the socket is the only channel, and its `hello` carries the helper's pid.

**Plugin (`hermes_recorder/` package at repo root)**
- `__init__.py`: `register(ctx)` only registers. **No side effects in `register()`**: the validator runs it in isolation, and it may run in a plugin host. Start the helper lazily on `recorder_control start`, `/recorder on`, or the first tool call when `autostart` is true.
- `buffer.py`: thread-safe ring buffer of typed events (`transcript`, `ui_event`, `app_switch`, `snapshot_ref`), trimmed by time; `slice(start, end)`; `search(query, minutes)`. Saved clips go to SQLite via `plugin_db`.
- `helper_manager.py`: launches the helper app with `subprocess.run(["/usr/bin/open", "-n", ...])` (absolute paths, no shell, no `-W`, no `--stdout`), owns the socket, **waits for `hello` with a timeout** (reporting a clear error if it doesn't arrive), tracks the helper pid from `hello`, restarts with backoff (max 3), and stops it on `on_unload` and on `recorder_control stop` (control `stop`, then SIGTERM to the pid as a fallback).
- `tools.py`: tool schemas and handlers (Section 6). Handlers return JSON strings and never raise.
- `hooks.py`: `pre_llm_call` reads only an in-memory flag and counters (target under 5 ms; hooks are skipped after 30 s). Returns at most one line. `on_session_end` optionally writes a recap note for pinned clips.
- `compiler.py`: buffer slice to normalized steps to `ctx.llm.complete_structured` with a JSON schema to `SKILL.md` text that passes the skill lint rules (Section 4).
- `privacy.py`: masking, redaction of obvious secrets in transcripts (reuse patterns via `ctx.register_redaction_patterns` if useful), per-app allowlist enforcement.
- `dashboard/manifest.json` + `dashboard/plugin_api.py`: FastAPI `router` with `GET /status`, `GET /transcript?since=`, `POST /control`. Plain `def` handlers or `async` with no blocking.
- `desktop/plugin.js`: no JSX, imports only `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`. Status-bar chip (recording dot, app name, pause), a right pane with live transcript and a "Teach from last N min" button. Polls `ctx.rest('/status')`. Stays inside the SDK surface (no DOM reaching into core UI).

### Socket and protocol
- Prefer a **Unix domain socket** in `plugin_data_dir("hermes-recorder")`, file mode 0600. macOS limits socket paths to 104 bytes; if the path is too long, fall back to a socket under `$TMPDIR` with a random name, or loopback TCP on a random port with a per-session token in the `hello`.
- Messages: one JSON object per line, each with `v` (protocol version), `type`, `ts` (epoch seconds), `app` (bundle id), and type-specific fields. Plugin to helper: `{"type":"control","op":"pause|resume|stop|set_apps","apps":[...]}`.

### Data flow: ask mode
1. User enables capture for Chrome and Zoom (`/recorder on chrome zoom`, settings form, or Desktop chip).
2. Helper streams transcript lines into the buffer.
3. On every turn while capture is live, `pre_llm_call` adds: `[Recorder: live capture of Google Chrome; last 12 min available via screen_recall]`.
4. The model calls `screen_recall(minutes=2, query=...)` and answers from the lines.

### Data flow: teach mode
1. The user does a task normally (capture running with events enabled).
2. The user says "turn the last 5 minutes into a skill" (or `/teach 5`).
3. The model calls `recorder_draft_skill(minutes=5)`. The plugin normalizes the slice (drops noise, merges typing, attaches app and element names, keeps narration lines with timestamps) and calls `ctx.llm.complete_structured` with the schema in Section 6. It returns the draft plus `questions` and `detected_inputs`.
4. The model asks me the questions (it can use `clarify`), revises, shows me the final SKILL.md, and on my OK calls `skill_manage` (`action: create`).
5. Offer a test run on a new input. Replay uses `computer_use` and the browser tools with normal approvals.

---

## 6. Hermes plugin API usage (verified patterns)

### Manifest (`plugin.yaml`)
```yaml
name: hermes-recorder
version: 0.1.0
manifest_version: 2
description: "Local screen and audio memory for Hermes: ask about any call or video, and teach Hermes a task by doing it once."
author: Michael Mi
license: MIT
homepage: https://github.com/michaelyouuu/hermes-recorder
requires_hermes: ">=0.22.7"   # the version we test against (CI pins b200f5bc)
platforms: [macos]
tags: [voice, automation, desktop, recording, skills]
provides_tools: [screen_recall, recorder_status, recorder_control, recorder_draft_skill]
provides_hooks: [pre_llm_call, on_session_end]
config_schema:
  apps: {type: list, default: [], label: "Apps to capture", description: "Bundle ids, e.g. com.google.Chrome, us.zoom.xos"}
  buffer_minutes: {type: int, default: 30, label: "Buffer length (minutes)"}
  whisper_model: {type: str, choices: [tiny.en, base.en, small.en], default: base.en}
  capture_mic: {type: bool, default: true, label: "Capture my microphone (narration)"}
  capture_events: {type: bool, default: false, label: "Capture clicks and typing (needed for teach mode)"}
  capture_snapshots: {type: bool, default: false}
  autostart: {type: bool, default: false}
```
`provides_tools` and `provides_hooks` must exactly match what `register()` registers or validation fails.

### Dependencies (`pyproject.toml`)
```toml
[project]
name = "hermes-recorder"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
  "faster-whisper>=1.2.1,<1.3; sys_platform == 'darwin' and platform_machine == 'arm64'",
  "pyobjc-framework-ScreenCaptureKit>=13,<14; sys_platform == 'darwin'",
  "pyobjc-framework-Quartz>=13,<14; sys_platform == 'darwin'",
  "pyobjc-framework-ApplicationServices>=13,<14; sys_platform == 'darwin'",
  "pyobjc-framework-CoreMedia>=13,<14; sys_platform == 'darwin'",
  "pyobjc-framework-AVFoundation>=13,<14; sys_platform == 'darwin'",
]
```
Rules: upper bound every dependency; floor on the oldest compatible version; stay compatible with core's `faster-whisper==1.2.1` pin. If the dependency union causes trouble, switch the helper to `python_runtime: external` with its own venv (ask me first).

### Registration skeleton
```python
def register(ctx):
    ctx.register_tool(name="screen_recall", toolset="recorder", schema=RECALL_SCHEMA, handler=tools.screen_recall)
    ctx.register_tool(name="recorder_status", toolset="recorder", schema=STATUS_SCHEMA, handler=tools.recorder_status)
    ctx.register_tool(name="recorder_control", toolset="recorder", schema=CONTROL_SCHEMA, handler=tools.recorder_control)
    ctx.register_tool(name="recorder_draft_skill", toolset="recorder", schema=DRAFT_SCHEMA, handler=tools.draft_skill)
    ctx.register_hook("pre_llm_call", hooks.pre_llm_call)        # accept **kwargs
    ctx.register_hook("on_session_end", hooks.on_session_end)    # accept **kwargs
    ctx.register_command("recorder", commands.recorder, description="Start, stop or check local capture", args_hint="[on|off|status] [apps]")
    ctx.register_command("teach", commands.teach, description="Draft a skill from the last N minutes", args_hint="[minutes]")
    ctx.register_cli_command("recorder", "Recorder setup and diagnostics", cli.setup_parser, cli.run)
    ctx.register_skill("replay-recorded", SKILLS / "replay-recorded" / "SKILL.md")
    ctx.on_unload(helper_manager.stop)
```

### Tools (names, behavior, return shape)
| Tool | Args | Returns |
| --- | --- | --- |
| `screen_recall` | `minutes` (default 5, max buffer), `query` (optional), `app` (optional), `speaker` (`me`/`them`/`any`) | `{"lines":[{"t":"-01:12","app":"...","speaker":"them","text":"..."}], "truncated":bool}`; cap output around 6,000 characters |
| `recorder_status` | none | running, apps, buffer span, permission status, helper health |
| `recorder_control` | `op` (`start`/`stop`/`pause`/`resume`/`set_apps`), `apps` | new status |
| `recorder_draft_skill` | `minutes` or `start`/`end`, `goal_hint` (optional) | `{"draft_md":"...","detected_inputs":[...],"questions":[...],"steps":[...]}` |

Tool descriptions must say exactly when to use the tool (for example, "Use when the user asks about something they just heard or watched on screen"). Handlers return JSON strings and catch every exception, returning `{"error": "..."}`.

### `pre_llm_call` contract
- Return `None` when capture is off. When on, return one line, for example `{"context": "[Recorder: live capture of Zoom; last 8 min (incl. your narration) via screen_recall; teach with recorder_draft_skill]"}`.
- Never transcribe, query SQLite or call the helper inside the hook.
- Context over 10,000 characters spills to a file. Stay far below.
- There is no HUD flag. Do not import `gateway.session_surface` (private; flagged by validation). File a GitHub issue asking for `surface` in `pre_llm_call` kwargs (catalog rule 9 says ask for the seam) and link it in the README.

### `ctx.llm` for the compiler
```python
result = ctx.llm.complete_structured(
    instructions=COMPILER_INSTRUCTIONS,
    input=[{"type": "text", "text": normalized_slice_json}],
    json_schema=DRAFT_JSON_SCHEMA,
    purpose="hermes-recorder: draft a skill from a recorded demonstration",
)
draft = result.parsed if result.content_type == "json" else None
```
Do not pass `provider`, `model`, `agent_id` or `profile` (they raise `PluginLlmTrustError` without operator opt-in). `DRAFT_JSON_SCHEMA` fields: `name` (kebab-case, at most 64 characters), `description` (60 characters or fewer), `when_to_use` (list), `inputs` (list of `{name, description, example}`), `steps` (list of `{n, app, action, target, value_ref, why}`), `verify` (list), `edge_case_questions` (2 or 3), `approval_points` (list of step numbers that send, pay, delete or publish).

### Saving the skill
Render the draft to SKILL.md with frontmatter `name, description, version: 0.1.0, author, license: MIT, metadata.hermes.tags, metadata.hermes.related_skills: [hermes-recorder:replay-recorded]`, then sections in this order: `## When to Use`, `## Inputs`, `## Steps`, `## Approval points`, `## Verify`, `## Edge cases`, `## Recorded from` (date, apps, duration; never raw transcript). The agent saves it with `skill_manage` after I approve it. A plugin may also call `ctx.dispatch_tool("skill_manage", {"operations":[{"name": ..., "action": "create", "content": ...}]})`, but prefer letting the agent do it in-turn so I see and approve it.

### Settings, state, storage
- User-facing settings: `config_schema` (rendered as a form in Desktop under Settings, Plugins) read with `ctx.get_config`.
- Plugin-owned runtime state (cursors, last permission check): `ctx.state` (10 MiB cap).
- Saved clips and metadata: `plugin_db("hermes-recorder")` (SQLite, WAL). Never write into the plugin's install directory.
- Secrets: none expected. If any appear, use `config_schema` type `secret` and `agent.secret_scope.get_secret`, never read `.env` files.

---

## 7. Repository layout

```
hermes-recorder/
├── plugin.yaml
├── pyproject.toml
├── __init__.py                 # register(ctx) only
├── hermes_recorder/            # plugin package (imported by __init__.py)
│   ├── buffer.py
│   ├── helper_manager.py
│   ├── tools.py  hooks.py  commands.py  cli.py
│   ├── compiler.py  privacy.py  schemas.py
│   └── protocol.py             # shared message types, version
├── helper/
│   ├── main.py  capture_audio.py  capture_events.py  capture_frames.py  transcribe.py
│   └── permissions.py          # preflight + request helpers, human-readable status
├── dashboard/
│   ├── manifest.json           # {"name": "hermes-recorder", "api": "plugin_api.py"}
│   └── plugin_api.py           # module-level `router = APIRouter()`
├── desktop/plugin.js
├── skills/replay-recorded/SKILL.md
├── spikes/                     # M0 throwaway scripts + RESULTS.md
├── tests/                      # pytest; no macOS needed (fake helper)
├── docs/                       # screenshots, demo script
├── .github/workflows/ci.yml    # pytest + hermes plugins validate --install-deps
└── README.md                   # what, permissions, privacy, disclosure, known issues
```
Verified: a root `__init__.py` that imports from the `hermes_recorder/` subpackage with a relative import (`from .hermes_recorder import tools`) passes validation and loads under the Hermes loader.

---

## 8. macOS capture and permissions

| Permission | Needed for | API to preflight | Notes |
| --- | --- | --- | --- |
| Screen & System Audio Recording | App audio, snapshots, window titles | `CGPreflightScreenCaptureAccess` / `CGRequestScreenCaptureAccess` | Required for ScreenCaptureKit audio |
| Microphone | Narration | `AVCaptureDevice.authorizationStatusForMediaType_` / `requestAccessForMediaType` | |
| Accessibility | Element names for events | `AXIsProcessTrustedWithOptions` | Teach mode only |
| Input Monitoring | Event tap | `CGPreflightListenEventAccess` / `CGRequestListenEventAccess` | Teach mode only |

**Attribution (settled by S1):** macOS grants these to the "responsible" app. A plain child of the gateway counts as `/usr/bin/osascript` (how Hermes launches the gateway), or as Terminal or VS Code if the gateway was started in the foreground there. That is why the helper is its own `.app` launched through LaunchServices: every prompt and Settings entry says **"Hermes Recorder Helper"**, whoever started Hermes. Mic and Screen & System Audio prompt normally. Accessibility and Input Monitoring must be switched on in System Settings. `hermes recorder doctor` runs the preflight *inside the helper app* and prints, per permission, granted or not plus the exact Settings deep link (`x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`, `?Privacy_ListenEvent`, `?Privacy_ScreenCapture`, `?Privacy_Microphone`), and offers to reveal the app for the "+" button.

Other rules:
- Ask for teach-mode permissions only when teach mode is first enabled, not at install.
- If a permission is missing, tools return a clear error with the fix; never crash or hang.
- Use `excludesCurrentProcessAudio` so Hermes's own TTS is not transcribed.
- DRM-protected video may capture as black frames; audio usually still works. Do not promise video.

---

## 9. Privacy and safety requirements (non-negotiable)

1. **Off by default.** Nothing is captured until I enable it and choose apps. Events (clicks and typing) have their own switch, off by default.
2. **Per-app allowlist.** Only listed bundle ids are captured. Never capture password managers (1Password, Bitwarden, Keychain Access) even if listed; keep a hard denylist.
3. **Secure fields never recorded.** `AXSecureTextField` input becomes `{SECRET}`. Also mask values that look like card numbers, one-time codes and API keys before they enter the buffer.
4. **RAM first.** The live buffer is memory only. Only clips I explicitly save are written to SQLite. A `forget` command clears the buffer and saved clips.
5. **Local only.** Audio and events never leave the Mac. The only off-device path is when the user's chosen Hermes model is cloud-hosted and the agent reads buffer content through a tool; state this plainly in the README and disclosure.
6. **Visible indicator.** The Desktop chip shows recording state; the helper also shows the standard macOS recording indicator. `recorder_status` reports the truth.
7. **Approvals.** Replay never bypasses Hermes approvals. Skills mark approval points for send, pay, delete and publish.
8. **Calls and consent.** README notes that recording laws vary (some places require everyone's consent) and suggests telling participants.

---

## 10. Catalog admission checklist

Run `hermes plugins validate --install-deps .` in CI on every push. Before opening the catalog PR:
- [ ] Rule 6: `provides_tools` / `provides_hooks` match registrations exactly.
- [ ] Rule 7: security scan verdict is `safe` (or `caution` with every finding explained in the PR).
- [ ] Rule 9: no imports of private core modules (`gateway.*`, `tools.transcription_*`, `agent.*` internals), no monkeypatching.
- [ ] Rule 12: no auto-approval, no YOLO children, nothing hangs in cron or gateway runs (commands that need a person return cleanly).
- [ ] Rule 13: README and PR disclose: local recording of audio, mic and (optionally) input events; a long-running helper process; local model download from Hugging Face on first run; what may reach a cloud model.
- [ ] Rule 14: `requires_hermes` floor is truthful and the same in `plugin.yaml` and the catalog entry; `version` matches the code.
- [ ] Rule 17: no compiled binaries, no `.pyc`, no model weights in the repo.
- [ ] Desktop half stays inside the SDK (no `document` queries into core UI, no `eval`, only allowed imports).
- [ ] Catalog entry YAML: exact 40-character SHA, `tier: community`, `category` (pick `automation` or `voice`; decide with me), `platforms: [macos]`, accurate `capabilities` block, `docs_url`, `image`, `screenshots`, `known_issues`, and a Disclosure line in `description`.
- [ ] Submission: the PR is from the repo owner (me).

---

## 11. Milestones (today is Thu Nov 5; deadline Tue Nov 11, 5 PM EST)

Each milestone ends with a demo to me and an update to Section 16. If a milestone slips by more than half a day, stop and propose a cut.

### M0: Spikes (Thu Nov 5 evening to Fri Nov 6 midday). Throwaway code in `spikes/`, results in `spikes/RESULTS.md`
- **S1 Permissions.** Script that preflights and requests all four permissions, launched (a) from Terminal and (b) as a child of the Hermes gateway started by Hermes Desktop. Record which app each prompt names and whether capture works after restarting the app. **Pass:** each permission can be granted once and persists.
- **S2 Audio + latency.** Capture Chrome playing a YouTube lecture via ScreenCaptureKit for 3 minutes, transcribe 5 s windows with faster-whisper `tiny.en`, `base.en`, `small.en`. Record lag (speech end to text available), real-time factor, CPU %, memory, and a rough word accuracy check. **Pass:** `base.en` or smaller under about 5 s lag with CPU low enough to leave on. Fallback: `mlx-whisper`.
- **S3 Events.** Record a 2-minute task in a native app (Notes or Reminders) and in Chrome via the event tap plus Accessibility. **Pass:** clicks name elements ("New Note button"), typing coalesces into field values, a password field yields only `{SECRET}`. Then run the normalized slice through a model and judge whether the SKILL.md draft is usable.

### M1: Plugin skeleton and buffer (Fri Nov 6)
Manifest, pyproject, `register()`, buffer, fake helper for tests, all tools returning real JSON from the fake, hooks, commands, CI with pytest and validate. **Done when:** validate passes in CI and `hermes` in a terminal shows the tools.

### M2: Ask mode end to end (Sat Nov 7)
Real helper (audio + mic + transcription), helper manager (start, stop, restart, unload), permissions doctor, `/recorder`, settings form. **Done when:** I ask the HUD about a YouTube video I am watching and get a correct answer citing the last minute; Zoom or FaceTime works with "me" vs "them".

### M3: Teach mode capture and compiler (Sun Nov 8)
Event capture with masking, normalization, `recorder_draft_skill`, SKILL.md rendering that passes skill lint, bundled `replay-recorded` skill. **Done when:** a 2-minute demo becomes a skill I would accept after answering the questions.

### M4: Replay and polish (Mon Nov 9 to noon)
Test-run flow, Desktop chip and pane, README with screenshots, disclosure, known issues. **Open the catalog PR by Mon Nov 9 evening.** **Done when:** a recorded skill replays on a new input with approval at the right step.

### M5: Demo and submission (Mon Nov 9 evening to Tue Nov 11 noon)
Record the demo, fix review feedback, post on X, submit the Typeform. Keep Tuesday morning as buffer.

### Cut lines (in order, if behind)
1. Snapshots (already optional).
2. Desktop pane (keep the chip).
3. Replay test-run flow (show a saved skill instead).
4. Teach mode entirely (ship ask mode with mic narration and recap; mention teach as roadmap).

---

## 12. Engineering practices

- **Python:** target 3.11+ (Hermes runs 3.14). Type hints everywhere, `from __future__ import annotations`, dataclasses for events, `logging.getLogger(__name__)`, no prints in plugin code.
- **Plugin code is a guest in Hermes:** no global side effects at import or in `register()`; no blocking on the gateway event loop; thread-safe state with locks; every hook and tool accepts `**kwargs`; nothing raises out of a handler.
- **Helper process:** no shell, absolute interpreter path, parent-death detection (exit when the socket closes; there is no stdin when launched via `open`), bounded queues with drop-oldest under backpressure, structured JSON logs to `plugin_data_dir/logs/helper.log` with rotation.
- **Performance budgets:** `pre_llm_call` under 5 ms; `screen_recall` under 50 ms for a 30-minute buffer; helper CPU under about 15% on an M-series Mac with `base.en` (S2: about 32% of one core, ≈3% of an M4; helper RSS ≈0.7 GB, mostly the model); RAM buffer under 50 MB for 30 minutes of transcript and events.
- **Testing:** pytest with a fake helper that replays recorded JSONL fixtures (YouTube transcript, Zoom call, a teach demo). Unit-test buffer trimming and search, masking, normalization, SKILL.md rendering against the lint rules, tool JSON shapes, hook output length, unload cleanup. Integration test that installs the plugin into a temp `HERMES_HOME`, enables it, dispatches tools and invokes hooks (the smoke-test harness pattern: `hermes_cli.plugins.discover_plugins(force=True)`, `tools.registry.registry.dispatch(...)`, `hermes_cli.plugins.invoke_hook(...)`; `import model_tools` first when built-in tools like `skill_manage` are needed). macOS-only tests are marked and skipped on Linux CI.
- **Validation:** `hermes plugins validate --install-deps .` and `hermes plugins doctor .` in CI; `HERMES_PLUGINS_DEBUG=1 hermes plugins list` when discovery acts up.
- **Dependencies:** pinned ranges with upper bounds, a lockfile for CI, no new dependency without asking.
- **Docs as you go:** README sections are written in the milestone that creates the feature, not at the end.
- **Secrets and data hygiene:** `.gitignore` covers recordings, transcripts, model caches, `.venv`, `__pycache__`, `*.pyc`, `.DS_Store`.

---

## 13. Definition of done (for the submission)

- [ ] Fresh install on a clean macOS 14+ Apple Silicon machine: `hermes plugins install michaelyouuu/hermes-recorder`, enable, grant permissions with `hermes recorder doctor` guidance, working in under 5 minutes.
- [ ] Ask mode answers correctly about the last minute of a YouTube video and a FaceTime or Zoom call from the HUD.
- [ ] Teach mode turns a short demonstration into a lint-clean SKILL.md after 2 or 3 questions, and the skill replays on a new input with approval at the right step.
- [ ] Nothing captured when off; secure fields never appear anywhere; `forget` clears everything.
- [ ] CI green: tests plus `validate --install-deps`.
- [ ] README: what it does, 3 screenshots, permissions table, privacy model, disclosure, known issues, link to the `surface` hook issue.
- [ ] Catalog PR open with all checks passing; demo video posted; Typeform submitted.

---

## 14. Demo script (under 90 seconds)

1. (0 to 10 s) Hook line: "Hermes can now see and hear your whole Mac, locally. Watch."
2. (10 to 30 s) YouTube lecture playing. Cmd+Shift+H: "What did he just say about the learning rate?" Correct answer quoting the last 20 seconds.
3. (30 to 45 s) FaceTime or Zoom: "What time did we agree on?" Answer labeled "them".
4. (45 to 75 s) I file a quick expense (or another 4-step task) without pressing anything. Then: "Turn what I just did into a skill." Draft appears, one question answered, saved.
5. (75 to 88 s) "Do it again for this receipt." Hermes replays, pauses for approval at Submit.
6. End card: "Local. Any app. Teach after the fact. hermes-recorder."

---

## 15. Known risks

| Risk | Mitigation |
| --- | --- |
| Permission attribution to the wrong app | Settled in S1: helper runs as its own generated `.app`; doctor prints exact fix steps |
| Ad-hoc grants reset when the interpreter changes (Hermes python update) | Rebuild the app on hash change; doctor detects lost grants and opens the right Settings pages |
| Helper launch via `open` silently fails | Wait for `hello` with a timeout, retry with backoff, return a clear tool error |
| Transcription too slow or CPU heavy | S2 passed with `base.en` on M4; `tiny.en` is the fallback, mlx-whisper not needed |
| Accessibility names are poor in some apps | Fall back to window title + snapshot; keep demo apps known-good |
| Replay unreliability (bounded by computer_use) | Short demo tasks; verify steps; approval points |
| Plugin host isolation (no websockets, no CLI) | Poll HTTP; tools work in both modes; test with `plugins.isolation: host` |
| "Always recording" perception | Off by default, per-app, RAM-only, visible indicator, clear README |
| Core later adds HUD audio | Our value is rolling history, teach mode and local-first; mention the issue we filed |
| Scope overrun | Cut lines in Section 11 |

---

## 16. Decision log

| Date | Decision | Why |
| --- | --- | --- |
| 2026-11-05 | macOS 14+ Apple Silicon only | faster-whisper wheels exclude Intel Macs; onnxruntime and av wheels target macOS 14 |
| 2026-11-05 | Ship own faster-whisper, not core's transcription | Core functions are not documented plugin API |
| 2026-11-05 | Desktop half polls HTTP, no websockets | Plugin host mode refuses websocket routes |
| 2026-11-05 | Helper started lazily, never in `register()` | Validator runs `register()` in isolation; no event loop there |
| 2026-11-05 | Inject a one-line note every turn while live, no HUD detection | No surface flag in hook kwargs; private module import would be flagged |
| 2026-11-05 | Demo lead: use the default rule (teach mode leads if M4 passes by Nov 9 noon, else ask mode leads with teach as preview) | Confirmed by Michael at M0 kickoff |
| 2026-11-05 | Helper runs as a locally generated, ad-hoc-signed `Hermes Recorder Helper.app` launched via LaunchServices, not a `Popen` child | S1b: gateway children are attributed to osascript; S1c: the helper app owns its own grants and they persist. Approved by Michael |
| 2026-11-05 | Socket is the helper's only channel; manager verifies `hello` and tracks pid from it | `open` gives no pipes; one launch silently failed in S1c |
| 2026-11-05 | `base.en` stays the default model | S2: 0.3 s per 5 s window, ≈3% of an M4, ~3% WER vs small.en |
| 2026-11-05 | Typed text recorded as deltas, never whole field values | S3: whole AXValue captured the entire note |
| (pending) | Mask email addresses by default in typed values? | S3 recorded an email verbatim; decide in M3 |
| 2026-11-05 | `requires_hermes: ">=0.22.7"`; CI pins Hermes `b200f5bc` | Only version tested; catalog rule 14 needs a truthful floor. Approved by Michael |
| 2026-11-05 | Integration tests run inside Hermes's runtime via `tests/integration/run_in_hermes.py` (bootstrap first, repo venv appended for pytest) | `hermes --run-module` runs Python isolated and has no pytest; pytest mis-parses absolute paths containing `[...]` (this repo's folder name) |
| 2026-11-05 | `screen_recall` returns the whole window when a query has no keyword hits, with a note | Paraphrased questions ("what time did we agree on") share no words with the answer ("3 PM Thursday") |
| 2026-11-05 | Dev install = symlink `~/.hermes/plugins/hermes-recorder` -> repo; restart the gateway with `hermes gateway stop` (Hermes relaunches it via osascript), never `hermes gateway restart` from a terminal | `restart` from a terminal ran the gateway in that terminal's foreground, attributed to VS Code |
| 2026-11-05 | `HERMES_RECORDER_DEV_FIXTURE` swaps in the fake helper (only fixtures under `tests/fixtures`) | Lets tools be exercised live before the real helper exists |
| 2026-11-05 | CI installs Hermes with `uv sync --frozen` and checks out plugin and Hermes side by side | pip can't resolve Hermes's pins; a Hermes checkout inside the plugin dir made the security scan report "dangerous" (it scanned Hermes) |
| 2026-11-05 | Repo `michaelyouuu/hermes-recorder` (private until the catalog PR); commits use the GitHub noreply email | Approved by Michael |
| (open, M2) | Where the helper's Python deps live: Hermes's plugin env vs. its own venv | Gateway warns the deps aren't in Hermes's env; the helper .app needs them on its PYTHONPATH |
| (pending) | Catalog category: `automation` vs `voice` | Discuss with me |

---

## 17. References

- Plugin guide: https://hermes-agent.nousresearch.com/docs/developer-guide/plugins
- Catalog submission rules: `website/docs/developer-guide/plugins/catalog-submission.md` in `NousResearch/hermes-agent`
- Desktop Plugin SDK: `website/docs/developer-guide/desktop-plugin-sdk.md`
- Plugin LLM access (`ctx.llm`): `website/docs/developer-guide/plugin-llm-access.md`
- Dashboard plugin backends (`plugin_api.py`, isolation): `website/docs/user-guide/features/extending-the-dashboard.md`, `website/docs/user-guide/features/plugins.md` (section "Running plugins out of process")
- Hooks reference: `website/docs/user-guide/features/hooks.md`
- Skills and `/learn`: `website/docs/user-guide/features/skills.md`
- Computer use: `website/docs/user-guide/features/computer-use.md`
- HUD context today: `tools/read_window_tool.py`, `apps/desktop/electron/window-below.ts`, `gateway/session_surface.py`, `agent/prompt_builder.py` (`hud_surface_note`)
- Prior art to study: `workflow-record` in `NousResearch/hermes-labs-plugins` (secret masking, recording format), core `plugins/google_meet` (process manager, node host, audio bridge)
- Hackathon announcement: https://x.com/NousResearch/status/2118094256438936062
- Marketplace: https://hermes-agent.nousresearch.com/marketplace
